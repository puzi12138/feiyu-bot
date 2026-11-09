"""Small native-client adapter for the official website's image/text posting API.

Contracts were checked against the public Xiaoheihe website's post editor and
COS upload code. No browser cookies are exported and write requests are never
automatically retried. The existing adapter owns the logged-in client.
"""

import hashlib
import hmac
import importlib
import json
import math
import re
import time
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote, unquote, urlsplit

import httpx

DEFAULT_TOPIC = "58144"
_AUTO_TOPIC = "auto"
_TOPIC_ID_RE = re.compile(r"\d{1,12}\Z")
PUBLICATION_HASHTAG = "分享我的养鱼日常"
_CONTRACTS = {
    "limits": ("GET", "/bbs/app/profile/post/limits"),
    "topics": ("GET", "/bbs/app/api/topic/index"),
    "hashtag_search": ("GET", "/bbs/app/api/post_editor/topic_selection/search"),
    "publish": ("POST", "/bbs/app/api/link/post"),
    "verify": ("GET", "/bbs/app/link/edit/info"),
    "own_posts": ("GET", "/bbs/web/profile/post/links"),
    "upload_info": ("POST", "/bbs/app/api/qcloud/cos/upload/info/v2"),
    "upload_token": ("POST", "/bbs/app/api/qcloud/cos/upload/token/v2"),
    "upload_callback": ("POST", "/bbs/app/api/qcloud/cos/upload/callback/v2"),
}
_WEB = dict(app="heybox", os_type="web", x_app="heybox_website",
            x_client_type="web", x_os_type="Windows", x_client_version="",
            client_type="web", web_version="3.0", version="999.0.4")


class PostBlocked(ValueError):
    """Contains a local reason code only, never upstream text or credentials."""


_AUTOMATIC_POST_GUARD = ContextVar("heybox_automatic_post_guard", default=None)


@contextmanager
def automatic_post_guard(check):
    """Bind a request-local automatic-action check without affecting owner calls."""
    token = _AUTOMATIC_POST_GUARD.set(check)
    try:
        yield
    finally:
        _AUTOMATIC_POST_GUARD.reset(token)


def _check_automatic_post_guard():
    check = _AUTOMATIC_POST_GUARD.get()
    if check is None:
        return
    try:
        allowed = check() is True
    except Exception:
        allowed = False
    if not allowed:
        raise PostBlocked("automatic_action_paused")


async def _authorize_guarded_request():
    """Recheck after the adapter's limiter and before every HTTP attempt."""
    _check_automatic_post_guard()
    return True


def _body(payload):
    if not isinstance(payload, dict):
        raise PostBlocked("post_response_contract_changed")
    result = payload.get("result", payload.get("data", payload))
    if not isinstance(result, dict):
        raise PostBlocked("post_response_contract_changed")
    return result


def public_image_url(value):
    try:
        p = urlsplit(str(value))
        return bool(p.scheme == "https" and p.hostname
                    and (p.hostname == "max-c.com" or p.hostname.endswith(".max-c.com")
                         or re.fullmatch(r"[a-z0-9-]+\.cos\.[a-z0-9-]+\.myqcloud\.com", p.hostname))
                    and p.port in (None, 443) and not p.username and not p.password
                    and not p.fragment and not re.search(r"(?i)(?:token|secret|signature|credential|q-sign)=", p.query)
                    and len(value) <= 4096)
    except (TypeError, ValueError):
        return False


def post_form(draft, images=()):
    """Build the observed image_text contract; no arbitrary API fields allowed."""
    topic = str(draft.get("topic_id", DEFAULT_TOPIC))
    if not re.fullmatch(r"\d{1,12}", topic):
        raise PostBlocked("post_topic_invalid")
    if "topic_ids" in draft or "hashtags" in draft:
        topic_ids, hashtags = validate_post_metadata(topic, draft.get("topic_ids"), draft.get("hashtags"))
        topic = ",".join(topic_ids)
    else:
        hashtags = []
    cards = [{"type": "text", "text": draft["text"]}]
    for image in images:
        if not public_image_url(image.get("url", "")):
            raise PostBlocked("post_image_url_invalid")
        cards.append({"type": "img", "url": image["url"],
                      "width": int(image["width"]), "height": int(image["height"])})
    form = {
        "text": json.dumps(cards, ensure_ascii=False, separators=(",", ":")),
        "desc": draft["text"][:100], "words_count": len(draft["text"]),
        "topic_ids": topic, "link_tag": 28 if topic == DEFAULT_TOPIC else 27,
        "hashtags": json.dumps(hashtags, ensure_ascii=False, separators=(",", ":")),
        "view_limit": 1, "post_type": 1,
        "extra_declaration": 0, "declaration": 0, "schedule_ts": 0,
    }
    if draft.get("title"):
        form["title"] = draft["title"]
    return form


def validate_post_metadata(topic_id, topic_ids, hashtags):
    """Validate bounded editor fields on newly created art drafts.

    Args:
        topic_id: Primary community ID.
        topic_ids: Ordered list of one or two community IDs.
        hashtags: Independent topic names, separate from community IDs.

    Returns:
        Validated detached lists.

    Raises:
        PostBlocked: Fields are invalid or mix the dynamic default with a community.
    """
    if (not isinstance(topic_ids, list) or not 1 <= len(topic_ids) <= 2
            or any(not isinstance(value, str) or not _TOPIC_ID_RE.fullmatch(value) for value in topic_ids)
            or len(set(topic_ids)) != len(topic_ids) or topic_ids[0] != str(topic_id)
            or (DEFAULT_TOPIC in topic_ids and len(topic_ids) > 1)):
        raise PostBlocked("post_topics_invalid")
    if (not isinstance(hashtags, list) or len(hashtags) > 5
            or any(not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 80
                   or any(ord(char) < 32 or ord(char) == 127 for char in value) for value in hashtags)
            or len(set(hashtags)) != len(hashtags)):
        raise PostBlocked("post_hashtags_invalid")
    return list(topic_ids), list(hashtags)


def choose_publication_metadata(rows, primary, *, comic_profile=None):
    """Resolve the art preferences from this draft's authenticated catalog.

    Args:
        rows: Current authenticated community directory.
        primary: Existing original community selection.
        comic_profile: Validated daily comic profile, if present.

    Returns:
        Primary community, ordered community IDs, independent topic and fixed reason.

    Raises:
        PostBlocked: A required name is missing or ambiguous.
    """
    catalog = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        identifier, name = str(row.get("topic_id", "")), str(row.get("name", "")).strip()
        if _TOPIC_ID_RE.fullmatch(identifier) and name:
            if identifier in catalog and catalog[identifier] != name:
                raise PostBlocked("post_topic_catalog_ambiguous")
            catalog[identifier] = name
    ai = [identifier for identifier, name in catalog.items() if name == "AI"]
    if len(ai) != 1:
        raise PostBlocked("post_ai_topic_unavailable")
    primary = str(primary)
    reason = "original_community_plus_ai"
    if primary == DEFAULT_TOPIC:
        if comic_profile in {"ai_friends", "dragon_helpers"}:
            daily = [identifier for identifier, name in catalog.items() if name == "沙雕日常"]
            if len(daily) != 1:
                raise PostBlocked("post_daily_topic_unavailable")
            primary = daily[0]
            reason = "daily_comic_community_plus_ai"
        else:
            # The dynamic default is not an additional selected community.
            primary = ai[0]
            reason = "ai_without_unverified_original_community"
    elif primary not in catalog:
        raise PostBlocked("post_topic_not_available")
    topics = list(dict.fromkeys([primary, ai[0]]))
    validate_post_metadata(primary, topics, [PUBLICATION_HASHTAG])
    return {"topic_id": primary, "topic_ids": topics, "hashtags": [PUBLICATION_HASHTAG],
            "topic_names": {identifier: catalog[identifier] for identifier in topics}, "reason": reason}


def _metadata_readback_matches(link, draft):
    """Match selected fields for new drafts while retaining legacy verification.

    Args:
        link: Owner-scoped edit-info response link.
        draft: Persisted operation.

    Returns:
        Whether every requested community and independent topic was read back.
    """
    if "topic_ids" not in draft and "hashtags" not in draft:
        return True
    try:
        topics, hashtags = validate_post_metadata(draft["topic_id"], draft.get("topic_ids"), draft.get("hashtags"))
    except (PostBlocked, KeyError):
        return False
    actual_topics, actual_hashtags = link.get("topics"), link.get("hashtags")
    if (not isinstance(actual_topics, list) or not isinstance(actual_hashtags, list)
            or any(not isinstance(row, dict) or not _TOPIC_ID_RE.fullmatch(str(row.get("topic_id", "")))
                   for row in actual_topics)
            or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                   for row in actual_hashtags)):
        return False
    return (sorted(str(row["topic_id"]) for row in actual_topics) == sorted(topics)
            and sorted(row["name"] for row in actual_hashtags) == sorted(hashtags))


def _number_range(value, maximum):
    if not isinstance(value, dict):
        raise PostBlocked("post_limits_contract_changed")
    low, high = value.get("min"), value.get("max")
    if (type(low) is not int or type(high) is not int
            or not 0 <= low <= high <= maximum):
        raise PostBlocked("post_limits_contract_changed")
    return {"min": low, "max": high}


def _topic_rows(value, depth=0):
    if depth > 6:
        return []
    rows = []
    if isinstance(value, list):
        for item in value[:300]:
            rows.extend(_topic_rows(item, depth + 1))
    elif isinstance(value, dict):
        identifier = value.get("topic_id", value.get("topicid"))
        if (identifier is not None and value.get("valid", 1) not in (0, False, "0")
                and re.fullmatch(r"\d{1,12}", str(identifier))):
            rows.append({"topic_id": str(identifier),
                         "name": str(value.get("name", value.get("topic_name", "")))[:80]})
        for key, item in list(value.items())[:100]:
            if isinstance(item, (dict, list)):
                rows.extend(_topic_rows(item, depth + 1))
    return rows


def choose_post_topic(rows, *, title="", text="", explicit_topic=None,
                      default_topic=DEFAULT_TOPIC):
    """Choose only from the authenticated preflight topic catalog.

    The publisher never asks a model to invent a topic ID.  An explicit ID
    remains authoritative; ``auto`` uses one exact, unique topic-name match
    and otherwise falls back to the platform's dynamic-post default.
    """
    catalog = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("topic_id", "")).strip()
        name = str(row.get("name", "")).strip()
        if _TOPIC_ID_RE.fullmatch(identifier) and 1 <= len(name) <= 80:
            catalog.setdefault(identifier, name)
    default = str(default_topic).strip()
    if not _TOPIC_ID_RE.fullmatch(default):
        return {"ok": False, "reason": "topic_catalog_invalid"}
    requested = "" if explicit_topic is None else str(explicit_topic).strip()
    if requested and requested != _AUTO_TOPIC:
        if not _TOPIC_ID_RE.fullmatch(requested):
            return {"ok": False, "reason": "post_topic_invalid"}
        if requested != default and requested not in catalog:
            return {"ok": False, "reason": "post_topic_not_available"}
        return {"ok": True, "topic_id": requested, "topic_name": catalog.get(requested),
                "mode": "explicit", "reason": "explicit_topic"}
    corpus = " ".join(value for value in (str(title).strip(), str(text).strip()) if value)
    matches = [(len(name), identifier, name) for identifier, name in catalog.items()
               if identifier != default and len(name) >= 2 and name in corpus]
    if len(matches) == 1:
        _, identifier, name = matches[0]
        return {"ok": True, "topic_id": identifier, "topic_name": name,
                "mode": "catalog_phrase", "reason": "unique_topic_name_match"}
    return {"ok": True, "topic_id": default, "topic_name": catalog.get(default),
            "mode": "default", "reason": "ambiguous_topic_name" if len(matches) > 1
            else ("empty_content" if not corpus else "no_topic_name_match")}


def cos_authorization(host, key, token):
    """Sign a host-only COS PUT using temporary upload credentials."""
    if not re.fullmatch(r"[A-Za-z0-9_./-]{1,1024}", key) or ".." in key.split("/"):
        raise PostBlocked("post_upload_key_invalid")
    start, end = token.get("startTime"), token.get("expiredTime")
    credentials = token.get("credentials", {})
    if not isinstance(credentials, dict) or not all(
            isinstance(credentials.get(k), str) and credentials[k]
            for k in ("tmpSecretId", "tmpSecretKey", "sessionToken")):
        raise PostBlocked("post_upload_credentials_contract_changed")
    if type(start) is not int or type(end) is not int or not 0 < end - start <= 86400:
        raise PostBlocked("post_upload_token_time_invalid")
    sign_time = f"{start};{end}"
    canonical = f"put\n/{quote(key, safe='/_.-')}\n\nhost={quote(host, safe='').lower()}\n"
    to_sign = "sha1\n" + sign_time + "\n" + hashlib.sha1(canonical.encode()).hexdigest() + "\n"
    sign_key = hmac.new(credentials["tmpSecretKey"].encode(), sign_time.encode(), hashlib.sha1).hexdigest()
    signature = hmac.new(sign_key.encode(), to_sign.encode(), hashlib.sha1).hexdigest()
    authorization = (f"q-sign-algorithm=sha1&q-ak={credentials['tmpSecretId']}"
                     f"&q-sign-time={sign_time}&q-key-time={sign_time}"
                     f"&q-header-list=host&q-url-param-list=&q-signature={signature}")
    return authorization, credentials["sessionToken"]


_ATTRIBUTION_URLS = (
    "https://selinlee.github.io/ai-moe-atlas/zh/characters/deep-whale-maid/",
    "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "https://selinlee.github.io/ai-moe-atlas/zh/characters/zipzippipe-chatgpt/",
)


def _link_ids_match(link, post_id):
    values = [str(link[key]) for key in ("linkid", "link_id", "id") if key in link]
    return bool(values) and all(value == str(post_id) for value in values)


def _author_ids(link):
    rows = [link] + [link[key] for key in ("user", "user_info", "author") if isinstance(link.get(key), dict)]
    return [str(row[key]) for row in rows for key in ("userid", "user_id", "uid", "heybox_id") if key in row]


def _author_matches(link, expected_uid):
    values = _author_ids(link)
    return bool(values) and all(value == str(expected_uid) for value in values)


def _attribution_text_matches(actual, expected):
    if actual == expected:
        return True
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    # Observed owner edit-info converts attribution URLs into plain
    # non-clickable text. No general whitespace, punctuation or URL folding.
    choices = [expected]
    for url in _ATTRIBUTION_URLS:
        choices += [value.replace(url, url[len("https://"):].replace(".", "。")) for value in choices]
    return actual in choices


def _image_object(value, *, transformed):
    if not isinstance(value, str) or not public_image_url(value):
        return None
    parsed = urlsplit(value)
    if not re.fullmatch(r"imgheybox[1-9]?\.max-c\.com", parsed.hostname or ""):
        return None
    match = re.fullmatch(r"(/web/bbs/\d{4}/\d{2}/\d{2}/[a-f0-9]{32})(?:/thumb)?\.png", parsed.path)
    if not match:
        return None
    if not transformed and ("/thumb.png" in parsed.path or parsed.query):
        return None
    if parsed.query and not re.fullmatch(
            r"imageMogr2/auto-orient/ignore-error/1/format/(?:jpg|png|webp)(?:/thumbnail/[1-9]\d{0,3}x[1-9]\d{0,3}>)?",
            unquote(parsed.query)):
        return None
    return match.group(1)


def _image_url_matches(actual, expected):
    if actual == expected:
        return True
    wanted = _image_object(expected, transformed=False)
    return bool(wanted and _image_object(actual, transformed=True) == wanted)


def _cards_readback_matches(actual, expected):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    for current, wanted in zip(actual, expected):
        if not isinstance(current, dict) or current.keys() != wanted.keys() or current.get("type") != wanted.get("type"):
            return False
        if wanted["type"] == "text":
            if not _attribution_text_matches(current.get("text"), wanted.get("text")):
                return False
        elif wanted["type"] == "img":
            if (not _image_url_matches(current.get("url"), wanted.get("url"))
                    or any(current[key] != value for key, value in wanted.items() if key != "url")):
                return False
        elif current != wanted:
            return False
    return True


def _observed_legacy_cards_match(actual, expected):
    """Match the bounded old editor card shape seen in owner readbacks.

    The old editor adds ``checked=false`` to cards and serializes image
    display dimensions as positive strings.  Those dimensions are presentation
    metadata; the local proof below checks the original staged dimensions.
    """
    if (not isinstance(actual, list) or not isinstance(expected, list)
            or len(actual) != len(expected) or not actual):
        return False
    current_text, wanted_text = actual[0], expected[0]
    if (isinstance(current_text, dict)
            and set(current_text) == {"type", "text", "checked"}
            and current_text.get("type") == "text"
            and current_text.get("checked") is False):
        text_card = {"type": "text", "text": current_text.get("text")}
        if not _cards_readback_matches([text_card], [wanted_text]):
            return False
    elif not _cards_readback_matches([current_text], [wanted_text]):
        return False

    # A checked text card may accompany otherwise normal image cards.
    if _cards_readback_matches(actual[1:], expected[1:]):
        return True
    for current, wanted in zip(actual[1:], expected[1:]):
        if (not isinstance(current, dict) or not isinstance(wanted, dict)
                or set(current) != {"type", "url", "width", "height", "checked"}
                or set(wanted) != {"type", "url", "width", "height"}
                or current.get("type") != "img" or wanted.get("type") != "img"
                or current.get("checked") is not False
                or not _image_url_matches(current.get("url"), wanted.get("url"))):
            return False
        for key in ("width", "height"):
            value = current.get(key)
            if (not isinstance(value, str) or not re.fullmatch(r"[1-9]\d{0,3}", value)
                    or int(value) > 8192):
                return False
    return True


def _safe_epoch(value, now=None):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 1_000_000_000:
        return None
    if now is not None and value > now + 5:
        return None
    return value


def _local_proof_matches(proof, draft, images):
    """Check a one-shot proof minted by HeyboxPostsMixin.

    The proof contains only operation-bound hashes, URLs and dimensions.  It
    never carries model output, credentials or image bytes.
    """
    if not isinstance(proof, dict) or proof.get("operation_id") != str(draft.get("id", "")):
        return False
    local, uploaded = proof.get("local_images"), proof.get("uploaded_images")
    stored, receipts = draft.get("images"), draft.get("uploaded_images")
    if (not isinstance(images, list) or not images
            or any(not isinstance(rows, list) or len(rows) != len(images)
                   for rows in (local, uploaded, stored, receipts))):
        return False
    for source, expected, remote, receipt, requested in zip(local, stored, uploaded, receipts, images):
        if (not isinstance(source, dict) or not isinstance(expected, dict)
                or not re.fullmatch(r"[a-f0-9]{64}", str(source.get("sha256", "")))
                or source.get("sha256") != expected.get("sha256")
                or any(type(source.get(key)) is not int or type(expected.get(key)) is not int
                       or not 0 < source[key] <= 8192 or source[key] != expected[key]
                       for key in ("width", "height"))):
            return False
        if (not isinstance(remote, dict) or not isinstance(receipt, dict) or not isinstance(requested, dict)
                or remote != receipt or remote != requested
                or any(type(remote.get(key)) is not int or remote[key] != source[key]
                       for key in ("width", "height"))
                or not public_image_url(remote.get("url", ""))):
            return False
    return True


def _local_own_post_matches(payload, post_id, draft, images, detail_link):
    """Strict list/detail corroboration for the local-image readback path."""
    if payload.get("status") != "ok" or not _author_matches(payload.get("user", {}), draft["account_uid"]):
        return False
    rows = payload.get("post_links")
    if not isinstance(rows, list) or len(rows) > 20:
        return False
    now = time.time()
    detail_created = _safe_epoch(detail_link.get("create_at"), now)
    if detail_created is None:
        return False
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        link = row.get("link", row)
        if (not isinstance(link, dict) or str(post_id) not in
                [str(link[key]) for key in ("linkid", "link_id", "id") if key in link]):
            continue
        if not _link_ids_match(link, post_id):
            matches.append(False)
            continue
        listed_created = _safe_epoch(link.get("create_at"), now)
        pictures = link.get("imgs")
        if listed_created is None or listed_created != detail_created:
            matches.append(False)
            continue
        if "dispatch_started_at" in draft:
            dispatch = _safe_epoch(draft["dispatch_started_at"], now)
            time_ok = dispatch is not None and dispatch - 2 <= detail_created <= dispatch + 120
        else:
            created = _safe_epoch(draft.get("created_at"), now)
            updated = _safe_epoch(draft.get("updated_at"), now)
            time_ok = (created is not None and updated is not None
                       and created <= detail_created <= updated + 120
                       and 0 <= updated - created <= 86400
                       and 0 <= detail_created - created <= 86400)
        prefix_ok = (isinstance(pictures, list) and bool(pictures)
                     and len(pictures) <= len(images)
                     and all(_image_url_matches(actual, wanted.get("url"))
                             for actual, wanted in zip(pictures, images)))
        matches.append(bool(_author_matches(link, draft["account_uid"])
                           and link.get("title") == draft.get("title")
                           and time_ok and prefix_ok))
    return matches == [True]


def _own_post_matches(payload, post_id, draft, images):
    if payload.get("status") != "ok" or not _author_matches(payload.get("user", {}), draft["account_uid"]):
        return False
    rows = payload.get("post_links")
    if not isinstance(rows, list) or len(rows) > 20:
        return False
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        link = row.get("link", row)
        if (not isinstance(link, dict) or str(post_id) not in
                [str(link[key]) for key in ("linkid", "link_id", "id") if key in link]):
            continue
        if not _link_ids_match(link, post_id):
            matches.append(False)
            continue
        pictures = link.get("imgs")
        try:
            created = float(link.get("create_at"))
            started = float(draft.get("dispatch_started_at", draft.get("created_at")))
        except (TypeError, ValueError):
            return False
        matches.append(bool(_author_matches(link, draft["account_uid"])
            and link.get("title") == draft.get("title")
            and math.isfinite(created) and math.isfinite(started)
            and (-2 if "dispatch_started_at" in draft else 0) <= created - started <= 120
            and isinstance(pictures, list) and len(pictures) == len(images)
            and all(_image_url_matches(actual, wanted.get("url")) for actual, wanted in zip(pictures, images))))
    return matches == [True]


class NativeHeyboxPostTransport:
    def __init__(self, plugin):
        self.plugin = plugin
        self.expected_uid = ""
        package = plugin.__class__.__module__.rsplit(".", 1)[0] + ".xiaoheihe"
        self.endpoints = importlib.import_module(package + ".endpoints")
        for key, (method, path) in _CONTRACTS.items():
            self.endpoints.ENDPOINTS["feiyu_post_" + key] = self.endpoints.Endpoint(
                method, path, True, method == "GET", self.endpoints.API_BASE_URL)

    async def client(self):
        client = await self.plugin.runtime.get_client("default")
        credentials = getattr(client, "credentials", None)
        if credentials is None or not re.fullmatch(r"\d{1,20}", str(credentials.uid)):
            raise PostBlocked("heybox_login_required")
        if self.expected_uid and str(credentials.uid) != self.expected_uid:
            raise PostBlocked("heybox_account_changed")
        return client

    async def _guarded_request(self, client, name, **kwargs):
        try:
            # The native adapter can wait on its limiter and retry safe GETs.
            # Its callback runs after each wait, immediately before HTTP.
            return await client._request(name, before_request=_authorize_guarded_request, **kwargs)
        except Exception as exc:
            # The adapter wraps a refused callback in RequestNotAuthorized.
            # Keep the local reason so callers can retain a received receipt.
            if isinstance(exc.__cause__, PostBlocked):
                raise PostBlocked(str(exc.__cause__)) from None
            raise

    async def _call(self, key, *, params=None, form=None):
        _check_automatic_post_guard()
        client = await self.client()
        params = {**_WEB, **(params or {})}
        if form is not None:
            method, path = _CONTRACTS[key]
            signed = client._signer.sign(
                method, path, params=params, json_body=form,
                signing_key=client.credentials.signing_key, device_id=client._device_id)
            params = dict(signed.params)
            # Public web protocol constant; this is not an account credential.
            public_key = b"Z7mFG4tQp9Ws2LxB8H"
            message = public_key + f"{params['nonce']}{params['_time']}:{params['nonce']}".encode()
            params["_rnd"] = "15:" + hmac.new(public_key, message, hashlib.sha256).hexdigest()
        _check_automatic_post_guard()
        response = await self._guarded_request(client, "feiyu_post_" + key, params=params, form_body=form)
        return dict(response.payload)

    async def preflight(self, topic_id=DEFAULT_TOPIC, *, topic_ids=None, hashtags=None, topic_names=None):
        _check_automatic_post_guard()
        metadata_requested = topic_ids is not None or hashtags is not None
        if metadata_requested:
            topic_ids, hashtags = validate_post_metadata(topic_id, topic_ids, hashtags)
            if (not isinstance(topic_names, dict) or set(topic_names) != set(topic_ids)
                    or any(not isinstance(name, str) or not 1 <= len(name) <= 80
                           for name in topic_names.values())):
                raise PostBlocked("post_topic_names_invalid")
        client = await self.client()
        self.expected_uid = str(client.credentials.uid)
        # This authenticated GET is independent of the cached login flag.
        _check_automatic_post_guard()
        account = await self._guarded_request(client, self.endpoints.EndpointName.CURRENT_USER)
        payload = account.payload
        if not isinstance(payload, Mapping):
            raise PostBlocked("post_response_contract_changed")
        body = payload.get("result", payload.get("data", payload))
        if not isinstance(body, Mapping):
            raise PostBlocked("post_response_contract_changed")
        payload = await self._call("limits")
        limits = _body(payload)
        if payload.get("status") != "ok" or limits.get("can_post_link") is not True:
            raise PostBlocked("heybox_post_permission_unavailable")
        raw = limits.get("pic_link_limit" if topic_id == DEFAULT_TOPIC else "cate_link_limit")
        if not isinstance(raw, dict):
            raise PostBlocked("post_limits_contract_changed")
        parsed = {"text": _number_range(raw.get("text_limit"), 1000000),
                  "title": _number_range(raw.get("title_limit"), 10000)}
        picture = raw.get("pic_limit", {}).get("max")
        if type(picture) is not int or not 0 <= picture <= 100:
            raise PostBlocked("post_limits_contract_changed")
        parsed["images"] = picture
        topics_payload = await self._call("topics", params={
            "is_post": 1, "post_tab": 1, "is_new_style": 1,
            "type": "list", "need_award_bubble_info": 1})
        if topics_payload.get("status") != "ok":
            raise PostBlocked("post_topic_catalog_unavailable")
        topics = _topic_rows(_body(topics_payload))
        # Dynamic posts have their own official default partition. Other
        # communities must be present in the account's current editor metadata.
        selected = topic_ids if metadata_requested else [topic_id]
        if any(identifier != DEFAULT_TOPIC and identifier not in {row["topic_id"] for row in topics}
               for identifier in selected):
            raise PostBlocked("post_topic_not_available")
        if metadata_requested:
            for identifier, name in topic_names.items():
                names = {row["name"] for row in topics if row["topic_id"] == identifier}
                if names != {name}:
                    raise PostBlocked("post_topic_name_changed")
            activities = 0
            for name in hashtags:
                search = await self._call("hashtag_search", params={"q": name, "only_hashtag": 0})
                rows = _body(search).get("search_result")
                if search.get("status") != "ok" or not isinstance(rows, list):
                    raise PostBlocked("post_hashtag_catalog_unavailable")
                matches = [row for row in rows if isinstance(row, dict)
                           and row.get("search_type") == "hashtag" and row.get("name") == name
                           and row.get("valid", 1) not in (0, False, "0")]
                if len(matches) != 1:
                    raise PostBlocked("post_hashtag_not_available")
                if matches[0].get("is_act_hashtag") in (True, 1, "1") or matches[0].get("sub_title") == "活动":
                    activities += 1
            if activities > 1:
                raise PostBlocked("post_hashtag_activity_limit")
        result = {"ok": True, "account_uid": self.expected_uid,
                "topic_id": topic_id, "limits": parsed,
                "topics": list({r["topic_id"]: r for r in topics}.values())[:100]}
        if metadata_requested:
            result.update(topic_ids=topic_ids, hashtags=hashtags)
        return result

    async def upload_image(self, image):
        info = _body(await self._call("upload_info", form={
            "file_infos": json.dumps([{"name": image["name"], "mimetype": image["mime"],
                "fsize": len(image["bytes"]), "width": image["width"], "height": image["height"]}]),
            "scope": "bbs", "need_cache": "0"}))
        bucket, region, keys = info.get("bucket"), info.get("region"), info.get("keys")
        if (not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9-]{3,80}", bucket)
                or not isinstance(region, str) or not re.fullmatch(r"[a-z0-9-]{3,40}", region)
                or not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str)):
            raise PostBlocked("post_upload_info_contract_changed")
        key = keys[0]
        token = _body(await self._call("upload_token", form={
            "bucket": bucket, "keys": json.dumps(keys),
            "mimetypes": json.dumps([image["mime"]]), "is_multipart_upload": "0"}))
        host = f"{bucket}.cos.{region}.myqcloud.com"
        authorization, session = cos_authorization(host, key, token)
        # Separate unauthenticated HTTP client: native account cookies and
        # bearer credentials must never travel to object storage.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=45) as http:
            _check_automatic_post_guard()
            response = await http.put(f"https://{host}/{quote(key, safe='/_.-')}",
                content=image["bytes"], headers={"Authorization": authorization,
                "Content-Type": image["mime"], "x-cos-security-token": session})
            if response.status_code not in (200, 201, 204):
                raise PostBlocked("post_image_upload_failed")
        result = _body(await self._call("upload_callback", params={"is_finished": "true"},
                                       form={"keys": json.dumps(keys)}))
        previews = result.get("preview_urls")
        if isinstance(previews, list) and len(previews) == 1:
            url = previews[0]
        else:
            raise PostBlocked("post_upload_callback_contract_changed")
        if not public_image_url(url):
            raise PostBlocked("post_image_url_invalid")
        return {"url": url, "width": image["width"], "height": image["height"]}

    async def publish(self, draft, images):
        self.expected_uid = draft["account_uid"]
        payload = await self._call("publish", form=post_form(draft, images))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        identifier = str(payload.get("link_id", result.get("link_id", "")))
        if payload.get("status") != "ok" or not re.fullmatch(r"\d{5,20}", identifier):
            raise PostBlocked("post_publish_receipt_unavailable")
        return identifier

    async def verify(self, post_id, draft, images):
        # The mixin mints this one-shot, operation-bound proof after checking
        # every staged file's SHA and actual decoded dimensions.  Consume it
        # before any network read so it cannot bleed into another operation.
        local_proof = getattr(self, "_local_readback_proof", None)
        self._local_readback_proof = None
        if not re.fullmatch(r"\d{5,20}", str(post_id)):
            return False
        self.expected_uid = draft["account_uid"]
        payload = await self._call("verify", params={"link_id": post_id})
        if payload.get("status") != "ok":
            return False
        result = _body(payload)
        link = result.get("link")
        if not isinstance(link, dict):
            return False
        identifiers = [str(link[key]) for key in ("linkid", "link_id", "id") if key in link]
        if not identifiers or any(value != str(post_id) for value in identifiers):
            return False
        # The authenticated edit-info API is owner-scoped. If it also returns
        # author identifiers, every alias must agree with the bound account.
        if _author_ids(link) and not _author_matches(link, draft["account_uid"]):
            return False
        try:
            cards = json.loads(link.get("text", ""))
        except (TypeError, json.JSONDecodeError):
            return False
        expected = json.loads(post_form(draft, images)["text"])
        if str(link.get("title", "")) != draft.get("title", ""):
            return False
        if not _metadata_readback_matches(link, draft):
            return False
        if cards == expected:
            return True
        # A normalized readback additionally requires the authenticated own-post
        # listing to corroborate the post, author, time, title and image objects.
        if not _author_matches(link, draft["account_uid"]):
            return False
        if local_proof is not None:
            if (not _local_proof_matches(local_proof, draft, images)
                    or not _observed_legacy_cards_match(cards, expected)):
                return False
        elif not _cards_readback_matches(cards, expected):
            return False
        own = await self._call("own_posts", params={"heybox_id": draft["account_uid"],
            "userid": draft["account_uid"], "limit": "20", "offset": "0", "post_type": "1"})
        if local_proof is not None:
            return _local_own_post_matches(own, post_id, draft, images, link)
        return _own_post_matches(own, post_id, draft, images)
