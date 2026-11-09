"""Bounded current-message Tieba link / share-card recognition.

Adapted under the MIT license, copyright (c) 2026 mizuku003.
See vendor/LICENSE.mizuku003 and vendor/NOTICE.md for attribution.
No network access occurs here. Short-link redirects are checked by client.py.
"""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit

MAX_STRING = 65_536
MAX_CHARS = 131_072
MAX_URL = 2_048
MAX_NODES = 1_024
MAX_DEPTH = 12
_HOSTS = {"tieba.baidu.com", "m.tieba.baidu.com", "wapp.tieba.baidu.com"}
_SHORT_HOSTS = {"tb.baidu.com"}
_ID = re.compile(r"[0-9]{1,20}\Z", re.ASCII)
_USER = re.compile(r"[A-Za-z0-9_.-]{1,192}\Z", re.ASCII)
_URL_TEXT = re.compile(
    r"(?<![\w.@/:-])(?:https?://|//)?"
    r"(?:(?:m|wapp)\.tieba\.baidu\.com|tieba\.baidu\.com|tb\.baidu\.com)"
    r"[^\s<>\"'\[\]{}\x00-\x1f\x7f]*", re.IGNORECASE,
)
_TRAILING = ".,!;:，。！？；：)]}＞>\"'"
_SKIP_KEYS = {
    "reply", "replies", "reply_to", "replyto", "quote", "quoted", "quotemessage",
    "quote_message", "forward", "forwarded", "forward_message", "forwardmessages",
    "history", "messages", "nodes", "message_info", "user_info", "group_info",
    "replymessage", "reply_message", "quotedmessage", "origin_message", "originmessage",
}
_SKIP_TYPES = {"reply", "quote", "forward", "forwarded", "node", "forward_node", "history"}
_MESSAGE_KEYS = {
    "text", "content", "message", "raw_message", "processed_plain_text", "message_str",
    "segments", "message_segments", "components", "json", "xml",
}
_URL_KEYS = {
    "url", "link", "href", "jumpurl", "jump_url", "qqdocurl", "shareurl", "share_url",
    "targeturl", "target_url", "weburl", "web_url", "linkurl", "contentjumpurl", "pcjumpurl",
}
_PAGE_KEYS = {"pagepath", "page_path", "path"}
_APP_KEYS = {"appname", "app_name", "applicationname", "sourcename", "sourcedisplayname", "source"}
_APP_IDS = {"com.baidu.tieba", "com.baidu.tieba.miniapp", "百度贴吧", "贴吧", "wx7a2de562948f667b"}
_APP_ID_KEYS = {"appid", "app_id", "app", "package", "username", "mini_app_id"}
_MINI_TYPES = {"miniapp", "mini_app", "miniprogram", "mini_program", "weapp", "lightapp"}
_POST_PATHS = {
    "pages/pb/pb", "pages/pb/index", "pages/thread/index", "pages/thread/thread",
    "pages/post/index", "pages/post/post",
    "pages/uni-pb/uni-pb.html",
}
_FORUM_PATHS = {"pages/frs/frs", "pages/frs/index", "pages/forum/index", "pages/forum/forum"}


def _decode(value: str) -> str:
    """Decode a bounded wrapper (never use this on a split query value)."""
    for _ in range(3):
        decoded = html.unescape(unquote(value))
        if decoded == value:
            break
        value = decoded
    return value


def _number(value: str) -> str | None:
    if not _ID.fullmatch(value):
        return None
    value = value.lstrip("0")
    return value if value and int(value) <= (1 << 63) - 1 else None


def _query(query: str) -> dict | None:
    try:
        return parse_qs(query, keep_blank_values=True, max_num_fields=64)
    except ValueError:
        return None


def _one(params: dict, keys: tuple[str, ...], numeric: bool = False) -> str | None:
    values = [v for key in keys for v in params.get(key, ())]
    if not values:
        return None
    if numeric:
        values = [_number(v) for v in values]
    if any(v is None or v == "" for v in values):
        return None
    return values[0] if len(set(values)) == 1 else None


def _forum(name: str) -> dict | None:
    name = name.strip()
    if not name or len(name) > 96 or any(ord(c) < 32 for c in name):
        return None
    return {"kind": "forum", "id": name, "name": name,
            "url": "https://tieba.baidu.com/f?kw=" + quote(name, safe="")}


def parse_url(value: str) -> dict | None:
    """Recognise exact Tieba hosts and canonicalise legacy HTTP input to HTTPS.

    Credentials, non-default ports, control characters, encoded host confusion,
    unknown paths and disagreeing IDs are rejected. A target is a description,
    never permission for the caller to fetch an arbitrary URL.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_URL:
        return None
    value = html.unescape(value.strip())
    # A card may percent-encode its entire URL. Ordinary query values are left
    # encoded until parse_qs, so a forum name containing '&' stays one value.
    if re.match(r"https?%3a", value, re.IGNORECASE):
        value = _decode(value)
    if any(ord(c) < 33 for c in value) or "\\" in value or len(value) > MAX_URL:
        return None
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        port = parts.port
        host = parts.hostname
    except (ValueError, UnicodeError):
        return None
    if (parts.scheme not in {"http", "https"} or parts.username is not None
            or parts.password is not None or parts.netloc.endswith(":")
            or port is not None and port != (443 if parts.scheme == "https" else 80)):
        return None
    if host in _SHORT_HOSTS:
        if not re.fullmatch(r"/[A-Za-z0-9_-]{1,128}/?", parts.path):
            return None
        # Tracking parameters aren't needed to resolve a tb.baidu.com token.
        return {"kind": "short", "id": parts.path.strip("/"),
                "url": "https://tb.baidu.com" + parts.path, "is_short": True}
    if host not in _HOSTS:
        return None
    path = unquote(parts.path).rstrip("/")
    params = _query(parts.query)
    if params is None:
        return None
    match = re.fullmatch(r"/p/([0-9]{1,20})", path)
    tid = _number(match.group(1)) if match else None
    if match:
        if any(k in params for k in ("tid", "kz")) and _one(params, ("tid", "kz"), True) != tid:
            return None
    elif path in {"", "/p", "/mo/q/m"}:
        tid = _one(params, ("tid", "kz"), True)
    if tid:
        return {"kind": "thread", "id": tid, "url": f"https://tieba.baidu.com/p/{tid}"}
    if path in {"/f", "/mo/q/m"}:
        name = _one(params, ("kw", "word"))
        return _forum(name) if name else None
    if path in {"/home/LOCAL_USER", "/home"}:
        uid = _one(params, ("id", "uid"))
        if uid and uid.isascii() and uid.isdigit():
            uid = _number(uid)
        if uid and _USER.fullmatch(uid):
            return {"kind": "user", "id": uid,
                    "url": "https://tieba.baidu.com/home/main?id=" + quote(uid, safe="")}
        # Legacy username homes are recognisable; availability is checked by client.
        name = _one(params, ("un",))
        if name and 0 < len(name) <= 96 and not any(ord(c) < 32 for c in name):
            return {"kind": "user", "id": "name:" + name, "name": name,
                    "url": "https://tieba.baidu.com/home/main?un=" + quote(name, safe="")}
    return None


def _mini_target(value: str) -> dict | None:
    if len(value) > MAX_URL:
        return None
    value = html.unescape(value).lstrip("/")
    if re.match(r"(?:%2f)?pages%2f", value, re.IGNORECASE):
        value = unquote(value).lstrip("/")
    if "://" in value or "\\" in value or any(ord(c) < 33 for c in value):
        return None
    parts = urlsplit(value)
    params = _query(parts.query)
    if params is None:
        return None
    path = parts.path.rstrip("/")
    if path in _POST_PATHS:
        tid = _one(params, ("tid", "kz", "thread_id"), True)
        if tid:
            return {"kind": "thread", "id": tid, "url": f"https://tieba.baidu.com/p/{tid}"}
    if path in _FORUM_PATHS:
        name = _one(params, ("kw", "word", "forum_name"))
        return _forum(name) if name else None
    return None


def parse_miniapp_path(value: str) -> dict | None:
    """A Tieba page path from a separately verified mini-program identity."""
    return _mini_target(value) if isinstance(value, str) else None


def parse_miniapp_share_url(value: str) -> dict | None:
    """Validate an opaque QQ share address; its app identity is checked separately.

    This is deliberately absent from plain-text URL matching. Only an explicit
    Tieba card may request QQ's read-only mini-program link metadata.
    """
    if (not isinstance(value, str) or not value or len(value) > MAX_URL
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return None
    value = html.unescape(value.strip())
    if any(ord(c) < 33 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme == "miniapp" and parts.netloc == "open" and parts.path == "/1036":
            params = _query(parts.query)
            value = _one(params, ("url",)) if params is not None else None
            if not value:
                return None
            parts = urlsplit(value)
        token = re.fullmatch(r"/a/s/([0-9a-f]{32})/?", parts.path, re.IGNORECASE)
        if (not token or parts.scheme != "https" or parts.hostname != "m.q.qq.com"
                or parts.username is not None or parts.password is not None
                or parts.port not in {None, 443} or parts.netloc.endswith(":")
                or parts.fragment or "\\" in value or any(ord(c) < 33 for c in value)):
            return None
    except (ValueError, UnicodeError):
        return None
    share_id = token.group(1).lower()
    return {"kind": "miniapp", "id": share_id, "url": "https://m.q.qq.com/a/s/" + share_id}


class _Extractor:
    def __init__(self, limit: int):
        self.limit = limit
        self.result: list[dict] = []
        self.seen: set[tuple[str, str]] = set()
        self.nodes = self.chars = 0

    def room(self, depth: int) -> bool:
        self.nodes += 1
        return depth <= MAX_DEPTH and self.nodes <= MAX_NODES and len(self.result) < self.limit

    def string(self, value) -> str | None:
        if not isinstance(value, str) or len(value) > MAX_STRING:
            return None
        self.chars += len(value)
        return value if self.chars <= MAX_CHARS else None

    def add(self, target):
        if target is None or len(self.result) >= self.limit:
            return
        key = target["kind"], target["id"]
        if key not in self.seen:
            self.seen.add(key)
            self.result.append(target)

    def text(self, value):
        value = self.string(value)
        if value is None:
            return False
        found = False
        for match in _URL_TEXT.finditer(html.unescape(value)):
            target = parse_url(match.group().rstrip(_TRAILING))
            found |= target is not None
            self.add(target)
            if len(self.result) >= self.limit:
                break
        return found

    def payload(self, value, depth: int):
        if not self.room(depth):
            return
        if isinstance(value, str):
            value = self.string(value)
            if value is None:
                return
            stripped = value.lstrip()
            structured = (stripped.startswith(("{", '"'))
                          or re.match(r"\[\s*[\[{\"]", stripped) is not None)
            if structured:
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    return
                if parsed != value:
                    self.payload(parsed, depth + 1)
            elif stripped.startswith("<"):
                self.xml(value, depth + 1)
            else:
                self.text(value)
        elif isinstance(value, Mapping):
            self.card(value, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value[:MAX_NODES]:
                self.payload(child, depth + 1)

    def card(self, root: Mapping, depth: int):
        entries = []
        identity = False
        mini = False

        def collect(node, level, parent=""):
            nonlocal identity, mini
            if not self.room(level):
                return
            if isinstance(node, Mapping):
                kind = str(node.get("type", "")).lower()
                if kind in _SKIP_TYPES:
                    return
                app = str(node.get("app", "")).lower()
                mini |= kind in _MINI_TYPES or parent in _MINI_TYPES or "miniapp" in app or "miniprogram" in app
                for index, (key, child) in enumerate(node.items()):
                    if index >= MAX_NODES or not self.room(level + 1):
                        break
                    if not isinstance(key, str) or key.lower() in _SKIP_KEYS:
                        continue
                    key = key.lower()
                    entries.append((key, child))
                    if isinstance(child, str):
                        identity |= (key in _APP_KEYS | _APP_ID_KEYS and child.strip().lower() in _APP_IDS)
                        identity |= (key in {"name", "title"} and parent in {"source", "detail_1", "weappinfo"}
                                     and child.strip() in {"贴吧", "百度贴吧"})
                        if key == "ark_reserved1":
                            reserved = self.string(child)
                            if reserved:
                                try:
                                    collect(json.loads(reserved), level + 1, key)
                                except (ValueError, RecursionError):
                                    pass
                    elif isinstance(child, (Mapping, list, tuple)):
                        collect(child, level + 1, key)
            elif isinstance(node, (list, tuple)):
                for child in node[:MAX_NODES]:
                    collect(child, level + 1, parent)

        collect(root, depth)
        direct = False
        shares = []
        for key, child in entries[:MAX_NODES]:
            if key in _URL_KEYS:
                direct |= self.text(child)
                if identity and mini:
                    shares.append(parse_miniapp_share_url(child))
            elif key in _PAGE_KEYS and identity:
                path = self.string(child)
                if path:
                    target = _mini_target(path)
                    direct |= target is not None
                    self.add(target)
            elif key in {"data", "json", "xml"} and isinstance(child, str):
                self.payload(child, depth + 1)
            elif key in _MESSAGE_KEYS and not mini:
                self.message(child, depth + 1)
        if not direct:
            for share in shares:
                self.add(share)

    def xml(self, value: str, depth: int):
        if "<!DOCTYPE" in value.upper() or "<!ENTITY" in value.upper():
            return
        try:
            root = ET.fromstring(value)
        except (ET.ParseError, ValueError, RecursionError):
            return
        fields = []
        identity = False

        def collect(element, level):
            nonlocal identity
            if not self.room(level):
                return
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag in _SKIP_KEYS | _SKIP_TYPES:
                return
            for key, child in list(element.attrib.items())[:64]:
                key = key.lower()
                fields.append((key, child))
                identity |= (key in _APP_KEYS | _APP_ID_KEYS and child.strip().lower() in _APP_IDS)
                identity |= (key == "name" and tag in {"source", "weappinfo"} and child.strip() in {"贴吧", "百度贴吧"})
            if element.text:
                fields.append((tag, element.text))
                identity |= tag in _APP_KEYS | _APP_ID_KEYS and element.text.strip().lower() in _APP_IDS
            for child in list(element)[:MAX_NODES]:
                collect(child, level + 1)

        collect(root, depth)
        for key, child in fields[:MAX_NODES]:
            if key in _URL_KEYS:
                self.text(child)
            elif key in _PAGE_KEYS and identity:
                self.add(_mini_target(child))

    def message(self, value, depth: int = 0):
        if not self.room(depth):
            return
        if isinstance(value, str):
            self.payload(value, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value[:MAX_NODES]:
                self.message(child, depth + 1)
        elif isinstance(value, Mapping):
            kind = str(value.get("type", "")).lower()
            if kind in _SKIP_TYPES:
                return
            if kind in {"json", "xml", "card", "share", "app"} | _MINI_TYPES:
                self.payload(value.get("data", value.get("content", value)), depth + 1)
                return
            if kind == "text":
                self.message(value.get("data", value.get("text", "")), depth + 1)
                return
            if kind == "dict":
                self.payload(value.get("data"), depth + 1)
                return
            if kind in {"seglist", "message_sequence", "message", "segments"}:
                self.message(value.get("data", value.get("components", [])), depth + 1)
            self.card(value, depth + 1)


def extract_targets(payload, limit: int = 3) -> list[dict]:
    """First distinct targets in current text/cards, excluding quotes/history.

    Input processing is bounded by depth, node count, string and total character
    budgets. Mini-program paths require explicit Tieba identity. Unknown app IDs
    and arbitrary numeric 'tid' values are never treated as links.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    extractor = _Extractor(min(limit, 8))
    extractor.message(payload)
    return extractor.result
