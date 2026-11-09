"""Owner-private Xiaoheihe drafts and durable, at-most-once post publication.

This module exposes a posting interface. The separate autonomous worker may
use an exact in-memory capability backed by the owner's persistent grant;
external posts and group members cannot authorize publication.
"""

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from .admin_info import owner_private
from .heybox_owner_images import owner_reply_image_sha256
from .heybox_post_transport import (DEFAULT_TOPIC, NativeHeyboxPostTransport, PostBlocked,
                                    automatic_post_guard, choose_post_topic, choose_publication_metadata)
from .peak_policy import automatic_peak_paused
from .peak_runtime import owner_scope_active, business_paused
from .policy import PUBLIC_COMMENT_SENSITIVE
from .public_comment_guard import internal_comment_reason


def _manual_post_allowed(event):
    """Only the live verified owner event may bypass the peak gate."""
    return not automatic_peak_paused() or owner_scope_active(event)


def _autonomous_readback_allowed(host, operation, capability):
    """Only the exact active extra operation may use the current owner scope."""
    if operation.get("publication_origin") != "owner_extra_ai_friends":
        return not automatic_peak_paused()
    return (not business_paused() and capability is not None
            and host.authorized_autonomous_heybox_post(capability, operation) is True)

_UNRESOLVED = {"dispatching", "unknown", "published_unverified"}
_TERMINAL = {"verified", "rejected"}
_MAX_IMAGE_BYTES = 10_000_000
_MAX_IMAGE_DIMENSION = 8192
_SAFE_CATEGORIES = {"credential_invalid", "forbidden", "rate_limited", "upstream_rejected",
                    "network", "server", "http", "response_shape", "send_unknown", "unavailable"}
_AUTONOMOUS_ORIGINS = frozenset({"scheduled_autopost", "owner_immediate",
                                 "owner_immediate_replacement", "owner_extra_ai_friends"})
_MAX_AUTONOMOUS_IMAGES = 4
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _result(**data):
    return json.dumps(data, ensure_ascii=False)


def _category(exc):
    value = str(getattr(exc, "category", "unavailable"))
    return value if value in _SAFE_CATEGORIES else "unavailable"


def _autonomous_image_manifest(context):
    """Return the ordered, immutable image binding for an autonomous draft.

    The worker may provide either the legacy path/hash arrays or a richer
    ``asset_manifest`` list.  We accept both so single-image slots remain
    compatible while comic pages get an explicit, contiguous page order.
    """
    if not isinstance(context, dict):
        raise PostBlocked("post_source_hash_guard_invalid")
    origin = context.get("publication_origin", "scheduled_autopost")
    if origin not in _AUTONOMOUS_ORIGINS:
        raise PostBlocked("post_source_hash_guard_invalid")
    profile = context.get("comic_profile")
    if (profile is not None and profile not in {"ai_friends", "dragon_helpers"}
            or origin == "owner_extra_ai_friends" and profile != "ai_friends"):
        raise PostBlocked("post_source_hash_guard_invalid")
    manifest = context.get("asset_manifest")
    if manifest is not None:
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= _MAX_AUTONOMOUS_IMAGES:
            raise PostBlocked("post_source_hash_guard_invalid")
        if profile is not None and len(manifest) != 4:
            raise PostBlocked("post_source_hash_guard_invalid")
        paths, hashes, normalized = [], [], []
        for index, item in enumerate(manifest, start=1):
            if (not isinstance(item, dict) or type(item.get("page")) is not int
                    or item["page"] != index):
                raise PostBlocked("post_source_hash_guard_invalid")
            path, digest = item.get("path"), item.get("sha256")
            if (not isinstance(path, str) or not path or len(path) > 1024
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
                raise PostBlocked("post_source_hash_guard_invalid")
            paths.append(path)
            hashes.append(digest)
            normalized.append({"page": index, "sha256": digest})
        return paths, hashes, normalized, origin
    paths = context.get("asset_paths", [context.get("asset_path")])
    hashes = context.get("asset_hashes", [context.get("asset_sha256")])
    if (not isinstance(paths, list) or not 1 <= len(paths) <= _MAX_AUTONOMOUS_IMAGES
            or profile is not None and len(paths) != 4
            or not isinstance(hashes, list) or len(paths) != len(hashes)
            or any(not isinstance(path, str) or not path or len(path) > 1024 for path in paths)
            or any(not isinstance(digest, str) or not _SHA256.fullmatch(digest) for digest in hashes)):
        raise PostBlocked("post_source_hash_guard_invalid")
    normalized = [{"page": index, "sha256": digest} for index, digest in enumerate(hashes, start=1)]
    return list(paths), list(hashes), normalized, origin


_OWNER_LEAD = (r"(?:(?:(?:大肥鱼|肥鱼)(?!说)|嗯+|恩+|唔+|哦+|啊+|你|请|帮我|替我|给我|麻烦|现在|去|直接|试着|试试|赶紧|赶快|"
               r"快点|立刻|立即|马上|尽快|先|再|就)[，,:：\s]*){0,8}")
_SPACE = r"(?:QQ\s*)?空间"
_QZONE_JOIN = r"(?:\s*[，,、;；]\s*(?:(?:同时|一并|一起|顺便|也|并且|并)\s*(?:也)?\s*)?|\s*(?:同时|一并|一起|顺便|也|并且|并)\s*(?:也)?\s*)"
_QZONE_ACTION = r"(?:发|发布|同步|转发)\s*(?:到|至)?\s*" + _SPACE
_NEGATIVE = r"(?:不要|不用|无需|不需要|先不|暂不|不许|不想|别|取消|停止|不)"
_QZONE_DENIAL = (r"(?:(?:请|千万|务必)?\s*" + _NEGATIVE + r"\s*(?:再|也|一起|同时)?\s*"
                   r"(?:发|发布|转|转发)\s*(?:到|至|去)?\s*" + _SPACE + r"|"
                   r"(?:请|千万|务必)?\s*" + _NEGATIVE + r"\s*(?:再|也|一起|同时)?\s*"
                   r"(?:同步|转发)(?:\s*(?:到|至)?\s*" + _SPACE + r")?|"
                   + _NEGATIVE + r"\s*" + _SPACE + r"|"
                   + _SPACE + r"\s*(?:千万|务必|先|暂时|这次|就)?\s*"
                   r"(?:不发|不用|不要|不必|不需要|取消|别发|停止|不同步|"
                   r"(?:别|不|不要|停止|取消)\s*(?:再)?\s*(?:发|发布|同步|转发)))")
_OWNER_END = r"[呀啊吧呗了哦啦。！!，,\s]*"
_POST_TARGET = (r"(?:(?:发|发布|投稿)(?:一下|一篇|一条|一个|个|篇|条)?(?:到|去|往)?"
                r"小黑盒(?:上)?(?:的)?(?:帖子|帖|动态)?|"
                r"(?:在|往|到)?小黑盒(?:上)?(?:发|发布|投稿)(?:一下|一篇|一条|一个|个|篇|条)?(?:帖子|帖|动态)?|"
                r"(?:发|发布|投稿)(?:一下|一篇|一条|一个|个|篇|条)?(?:帖子|帖|动态)(?:到|去|往)小黑盒(?:上)?)")


def _owner_instruction_text(event):
    getter = getattr(event, "get_extra", lambda _key: None)
    text = str(getter("feiyu_owner_original_text") or getattr(event, "message_str", "") or "").strip()
    text = re.sub(r"“[^”]*”|‘[^’]*’|\"[^\"]*\"|'[^']*'", "", text)
    text = re.split(r"(?:正文|内容|文案|标题)\s*(?:写|是|为)?\s*[:：]|"
                    r"(?:在)?(?:帖子|帖|正文|文案|标题|内容)(?:里|中|上)(?:写|说|提(?:到)?|讲|注明)|"
                    r"(?:正文|文案|标题|内容)(?:写|说|提(?:到)?|讲|注明)", text, maxsplit=1)[0]
    return re.split(r"[“‘\"']", text, maxsplit=1)[0].strip()


def _owner_target_operation(event):
    """An ID binds only when it is the object of the owner's direct command."""
    text = _owner_instruction_text(event)
    target = (r"(?:发|发布|投稿)\s*(?:这条|这篇|这个)?\s*(?:小黑盒)?(?:草稿|帖子|稿件)"
              r"\s*(?:编号|号|ID|id)?\s*[:：#]?\s*(?P<id>[a-f0-9]{32})"
              r"(?![a-zA-Z0-9])(?:\s*(?:到|去)小黑盒)?")
    suffix = r"(?:" + _QZONE_JOIN + r"(?:" + _QZONE_ACTION + r"|" + _QZONE_DENIAL + r"))?"
    match = re.fullmatch(_OWNER_LEAD + target + suffix + _OWNER_END, text, re.IGNORECASE)
    return match.group("id").lower() if match else None


def _owner_post_intent(event, *, followup=False, for_draft=False):
    """Use the transport's original owner text, never the model's paraphrase."""
    getter = getattr(event, "get_extra", lambda _key: None)
    text = str(getter("feiyu_owner_original_text") or getattr(event, "message_str", "") or "").strip()
    # Conversational acknowledgements do not change the following command's
    # meaning. Remove only a bounded leading filler, never quoted/report text.
    text = re.sub(r"^(?:(?:嗯+|恩+|唔+|哦+|噢+|啊+|呃+|诶+|哎+|欸+|哈+|嘿+|喂+)[，,:：。\s]*){1,3}", "", text)
    text = re.sub(r"^(?:大肥鱼|肥鱼)(?!说)[，,:：\s]*", "", text)
    # Preserve a prohibition that explicitly covers both platforms. A Qzone
    # only prohibition must not negate a separate Xiaoheihe command.
    if re.search(_NEGATIVE + r".{0,8}(?:发|投稿|同步|转发).{0,10}"
                 r"(?:空间.{0,5}小黑盒|小黑盒.{0,5}空间)", text, re.IGNORECASE):
        return False
    text = re.sub(_QZONE_DENIAL + r"(?!\s*(?:和|与|及|、)\s*小黑盒)", "", text, flags=re.IGNORECASE)
    if (not text or re.match(r"^(?:肥鱼说|bot说|机器人说|他说|她说|引用|转发|例如|比如|[‘“\"'])", text)
            or re.search(r"(?:不要|不用|无需|无须|不需要|先不|不许|取消|停止|别).{0,6}(?:发|写|投稿)", text)
            or re.search(r"接口|权限|插件|功能|为什么|怎么(?:还|会|又)|无法|报错|失败", text)):
        return False
    if _owner_target_operation(event):
        return True
    # A capability question or a report of somebody's request is not a new
    # instruction to publish. Keep this independent of the generated draft.
    if (re.search(r"(?:能不能|会不会|是否|可不可以|支持).{0,14}(?:发|投稿)|(?:发|投稿).{0,16}(?:吗|么|不[？?])|(?:可以|能).{0,14}(?:发|投稿).{0,6}[？?]", text)
            and not re.search(r"(?:帮我|替我|给我).{0,12}(?:发|投稿)", text)):
        return False
    if re.search(r"(?:发|投稿).{0,20}(?:是不是|是否|会不会|能不能|的教程|的方法|的流程|的步骤)", text):
        return False
    if re.match(r"^(?:我(?:让|叫|说|要求|想知道|(?:的)?(?:朋友|同事|同学|家人))|有人|别人|大家|群里|看到|听说|他|她|(?:我)?(?:昨天|上次|之前|刚才|已经|曾经)|如果|假如|假设).{0,30}(?:发|投稿)", text):
        return False
    text = re.sub(r"^(?:能不能|可不可以)(?=(?:帮我|替我|给我|请你|麻烦你))", "", text)
    if not for_draft and re.search(r"草稿|预览|先写|只写|起草", text) and not re.search(
            r"发出去|直接发|发布|发到|发小黑盒|然后.{0,5}发|写完.{0,5}发|写好.{0,5}发", text):
        return False
    lead = r"(?:(?:你|请|帮我|替我|给我|麻烦|现在|去|直接|试着|试试|赶紧|赶快|赶忙|快点|立刻|立即|立马|马上|尽快|尽量|务必|快|先|再|就)[，,:：\s]*){0,6}"
    # A command may refer to material already in the conversation. Keep this
    # anchored to an explicit target and platform, never model-generated text.
    reference_command = re.match(
        r"^" + lead + r"(?:拿|用|把)(?:这个|这条)(?:图片|帖子|素材|内容|作品|图)?"
        r"(?:拿去|带去|去|来|直接)?(?:发(?:发|一下|一篇|一条|一个|个|篇|条|点)?|发布|投稿)"
        r"(?:到|去|往)?小黑盒", text)
    if reference_command:
        if (re.search(r"什么意思|啥意思|什么含义|啥含义|怎么理解|如何理解|能不能|可不可以|会不会|是否|是不是|支持|"
                      r"[吗么](?:[？?，,。！!\s]|$)|[？?]\s*$|先不|暂不|取消|停止|不发|"
                      r"(?:不要|不用|无需|无须|不需要|不许|别)(?:了|吧|[。！!\s]*$)", text)
                or (not for_draft and re.search(r"(?:先|只|仅)\s*(?:给我|让我|供)?\s*(?:预览|看(?:看|一下|一眼))", text))):
            return False
        return bool(re.match(r"(?:[，,:：\s]|$|上|吧|试|看|。|！|!)", text[reference_command.end():]))
    if re.search(r"^(?:(?:帮我|替我|请|给我|你)?\s*去)?小黑盒[，,:：\s]*发帖(?:[呀啊吧。！!\s]*$|[：:])", text):
        return for_draft or not re.search(r"(?:先|只|仅)(?:给我|让我)?预览", text)
    if re.search(r"(?:在|往|到|发|投).{0,6}小黑盒.{0,8}(?:发|帖|动态|投稿)|"
                 r"(?:发|投稿|发布).{0,10}(?:到小黑盒|小黑盒(?:帖子|动态))|"
                 r"^(?:帮我|替我|你去|请|给我)?\s*(?:发|发布|投稿)(?:一篇|一条|一个|篇|条|个)?(?:帖子|动态)", text):
        return True
    # Natural direct commands may omit the noun 'post': 发小黑盒/发个帖/
    # 小黑盒发一下. Anchor the command so ordinary talk about posting is not
    # an authorization. Original owner text is retained across model/tool calls.
    if re.match(r"^" + lead + r"(?:发(?:发|一下|一篇|一条|一个|个|篇|条|点)?|发布|投稿)(?:到|去|往)?小黑盒(?:[，,:：\s]|$|上|吧|试|看|。|！|!)", text):
        return True
    if re.match(r"^" + lead + r"小黑盒(?:上)?[，,:：\s]*(?:发(?:发|一下|一篇|一条|一个|个|篇|条|点)?|发布|投稿)", text):
        return True
    if re.match(r"^" + lead + r"(?:发|发布|投稿)(?:到|去|往)?小黑盒\s*(?:和|与|及|、)\s*(?:QQ\s*)?空间(?:[，,:：\s]|$|吧|。|！|!)",
                text, re.IGNORECASE):
        return True
    if re.match(r"^" + lead + r"(?:发|发布|投稿)(?:一下|一篇|一条|一个|个|篇|条)?(?:帖|帖子|动态)(?:[，,:：\s]|$|吧|试|看|。|！|!)", text):
        return True
    if re.match(r"^" + lead + r"(?:结合|根据|参考|围绕|使用|用|通过|挑|找|搞|做|写).{1,24}(?:发(?:一下|一篇|一条|一个|个|篇|条)?|发布|投稿)(?:到)?小黑盒(?:[。！!\s]*$|[，,:：])", text):
        return True
    if for_draft and re.search(r"(?:写|起草).{0,8}(?:小黑盒|帖子|动态)|(?:小黑盒|帖子).{0,8}(?:草稿|预览)", text):
        return True
    return followup and bool(re.fullmatch(
        r"(?:这条|这个|草稿|帖子)?(?:(?:就这样|直接|继续|试着|现在|给我|帮我|替我|随便|赶紧|赶快|快点|马上|立即|那就|可以|允许|确认|好的|好|那|你|来|请|再|就)[，,:：\s]*){0,3}"
        r"(?:发(?:一下|一个|一篇|一条|个|篇|条)?|发出去|发布|发帖|可以发了)"
        r"(?:试试|看看)?[呀啊吧呗了。！!\s]*", text))


def _owner_image_intent(event):
    original = getattr(event, "get_extra", lambda _key: None)("feiyu_owner_original_text")
    text = str(original or getattr(event, "message_str", "") or "")
    if re.search(r"图|照片|配图|这张|这些", text):
        return True
    message = getattr(event, "message_obj", None)
    return any(part.__class__.__name__.lower() == "image" for part in getattr(message, "message", []) or [])


def _owner_qzone_intent(event, *, followup=False):
    """Return True, False or None for this owner's current sync instruction."""
    text = _owner_instruction_text(event)
    if re.search(_QZONE_DENIAL, text, re.IGNORECASE):
        return False
    if not _owner_post_intent(event, followup=followup, for_draft=True):
        return None
    if re.search(r"(?:以前|之前|曾经|上次|昨天|过去).{0,16}(?:空间|同步|发过)", text):
        return None
    # Require an affirmative, direct parallel command in its entirety. A
    # question, explanation or unquoted post prose cannot grant permission.
    explicit = (re.fullmatch(_OWNER_LEAD + _POST_TARGET + _QZONE_JOIN + _QZONE_ACTION + _OWNER_END,
                             text, re.IGNORECASE)
        or re.fullmatch(_OWNER_LEAD + r"(?:发|发布|投稿)(?:到|去|往)?小黑盒\s*(?:和|与|及|、)\s*"
                        + _SPACE + _OWNER_END, text, re.IGNORECASE)
        or (_owner_target_operation(event) and re.search(_QZONE_ACTION + _OWNER_END + r"$", text, re.IGNORECASE)))
    return True if explicit else None


def _owner_mentions_operation(event, identifier):
    return bool(isinstance(identifier, str) and _owner_target_operation(event) == identifier)


def _owner_event_hash(event):
    """Use the incoming platform message ID, never model-supplied tool input."""
    message = getattr(event, "message_obj", None)
    identifier = getattr(message, "message_id", None)
    if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
        return None
    identifier = str(identifier)
    if not re.fullmatch(r"-?[0-9]{1,24}", identifier):
        return None
    origin = str(getattr(event, "unified_msg_origin", ""))
    if not origin or len(origin) > 300:
        return None
    return hashlib.sha256((origin + "\0" + identifier).encode("utf-8")).hexdigest()


def _owner_draft_bound(host, event, identifier):
    """Bind by a direct ID or a uniquely consumed platform message."""
    target = _owner_target_operation(event)
    if target is not None:
        return target == identifier
    text = _owner_instruction_text(event)
    # An ID mentioned in some other context is not an affirmative target.
    if re.search(r"(?<![a-zA-Z0-9])[a-f0-9]{32}(?![a-zA-Z0-9])", text, re.IGNORECASE):
        return False
    event_hash = _owner_event_hash(event)
    if not event_hash:
        return False
    # The durable hash prevents reuse across restarts; it is not a durable
    # publication grant. Same-event authorization exists only in this process
    # and expires after ten minutes.
    binding = host._heybox_owner_draft_events.get(identifier)
    age = time.monotonic() - binding[1] if binding else -1
    if not binding or binding[0] is not event or not 0 <= age <= 600:
        return False
    matching = [key for key, operation in host._heybox_posts_ops.items()
                if operation.get("owner_request_event_hash") == event_hash]
    return matching == [identifier]


def _guard_content(title, text, *, trusted_reference_url=None):
    if not isinstance(title, str) or not isinstance(text, str):
        raise PostBlocked("post_content_invalid")
    title, text = title.strip(), text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or len(text) > 30000 or len(title) > 30:
        raise PostBlocked("post_content_length_invalid")
    content = title + "\n" + text
    if trusted_reference_url is not None:
        # Only the eventless scheduled path can supply this operation-bound
        # reference. Ignore its numeric post ID for sensitive-number matching,
        # while checking every other character with the unchanged guard.
        if (not isinstance(trusted_reference_url, str)
                or not re.fullmatch(r"https://www\.xiaoheihe\.cn/app/bbs/link/[A-Za-z0-9_-]{1,64}", trusted_reference_url)):
            raise PostBlocked("post_content_invalid")
        marker = "\n\n创作参考：" + trusted_reference_url
        if text.count(marker) > 1:
            raise PostBlocked("post_content_invalid")
        # New posts keep source metadata locally. Historical drafts retain
        # their exact old content and only that verified marker is exempted.
        if marker in text:
            content = content.replace(marker, "\n\n创作参考：[已核验来源]", 1)
    if PUBLIC_COMMENT_SENSITIVE.search(content):
        raise PostBlocked("sensitive_post_content")
    reason = internal_comment_reason(content)
    if reason:
        raise PostBlocked(reason)
    # The existing comment guard is intentionally comment-specific. Posts also
    # reject the assistant's execution/permission handoff descriptions.
    if re.search(r"(?:帖子|发帖|发布工具|投稿接口).{0,24}(?:权限不(?:足|够)|没有权限|失败|报错|等.{0,5}号主|需要.{0,5}主人)", content):
        raise PostBlocked("internal_post_diagnostic")
    return title, text


def _stage_image(source, directory, allowed_roots, expected_sha256=None):
    source = Path(source).resolve(strict=True)
    if not any(source.is_relative_to(root) for root in allowed_roots):
        raise PostBlocked("post_image_path_not_allowed")
    if not source.is_file() or not 0 < source.stat().st_size <= _MAX_IMAGE_BYTES:
        raise PostBlocked("post_image_size_invalid")
    # One bounded read closes the source-file race; staged public pixels never
    # include the source file's metadata, animation or trailing payload.
    with source.open("rb") as handle:
        original = handle.read(_MAX_IMAGE_BYTES + 1)
    if not 0 < len(original) <= _MAX_IMAGE_BYTES:
        raise PostBlocked("post_image_size_invalid")
    if expected_sha256 is not None and hashlib.sha256(original).hexdigest() != expected_sha256:
        raise PostBlocked("post_approved_image_changed")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(original)) as probe:
            if probe.format not in {"PNG", "JPEG", "WEBP"}:
                raise PostBlocked("post_image_type_unsupported")
            if (not 0 < probe.width <= _MAX_IMAGE_DIMENSION
                    or not 0 < probe.height <= _MAX_IMAGE_DIMENSION):
                raise PostBlocked("post_image_dimensions_invalid")
            probe.verify()
        with Image.open(BytesIO(original)) as image:
            image.seek(0)
            image = ImageOps.exif_transpose(image)
            image.load()
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            width, height = image.size
    data = output.getvalue()
    if len(data) > _MAX_IMAGE_BYTES:
        raise PostBlocked("post_image_size_invalid")
    digest = hashlib.sha256(data).hexdigest()
    destination = directory / (digest + ".png")
    if not destination.exists():
        with destination.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    return {"name": destination.name, "sha256": digest, "width": width, "height": height,
            "mime": "image/png", "size": len(data)}


class HeyboxPostsMixin:
    def init_heybox_posts(self, *, transport=None, allowed_image_roots=None):
        self._heybox_posts_root = (Path(self.root) / "heybox-posts").resolve()
        self._heybox_posts_root.mkdir(parents=True, exist_ok=True)
        self._heybox_posts_images = self._heybox_posts_root / "images"
        self._heybox_posts_images.mkdir(exist_ok=True)
        self._heybox_posts_path = self._heybox_posts_root / "operations.json"
        self._heybox_posts_lock = asyncio.Lock()
        self._heybox_post_transport_override = transport
        self._heybox_post_image_roots = tuple(Path(p).resolve() for p in (
            allowed_image_roots or [r"D:/QQBot/data/astrbot", self.root]))
        self._heybox_posts_storage_fault = False
        self._heybox_posts_ops = {}
        self._heybox_owner_draft_events = {}
        if self._heybox_posts_path.exists():
            try:
                payload = json.loads(self._heybox_posts_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or payload.get("schema") != 1 or not isinstance(payload.get("operations"), dict):
                    raise ValueError("schema")
                self._heybox_posts_ops = payload["operations"]
                if any(not isinstance(op, dict) or op.get("id") != key
                       or op.get("phase") not in _UNRESOLVED | _TERMINAL | {"draft"}
                       or not all(field in op for field in ("title", "text", "topic_id", "account_uid", "images"))
                       for key, op in self._heybox_posts_ops.items()):
                    raise ValueError("operation")
            except (OSError, ValueError, TypeError):
                self._heybox_posts_storage_fault = True

    def _post_persist(self):
        if self._heybox_posts_storage_fault:
            raise PostBlocked("post_storage_unavailable")
        temporary = self._heybox_posts_path.with_name("operations." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump({"schema": 1, "operations": self._heybox_posts_ops}, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._heybox_posts_path)
        except (OSError, TypeError, ValueError):
            self._heybox_posts_storage_fault = True
            raise PostBlocked("post_storage_unavailable") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _post_transport(self):
        if self._heybox_post_transport_override is not None:
            return self._heybox_post_transport_override
        plugin = self.plugin("astrbot_plugin_xiaoheihe_adapter")
        if plugin is None:
            raise PostBlocked("heybox_plugin_unavailable")
        return NativeHeyboxPostTransport(plugin)

    def _post_summary(self, operation):
        summary = {key: operation[key] for key in (
            "id", "phase", "title", "text", "topic_id", "topic_ids", "hashtags", "topic_names",
            "created_at", "updated_at", "post_id", "url", "reason")
            if key in operation} | {"image_count": len(operation.get("images", [])),
                                    "no_automatic_resend": operation.get("phase") in _UNRESOLVED}
        manifest = operation.get("autonomous_image_manifest")
        if isinstance(manifest, list):
            summary["image_manifest"] = [dict(row) for row in manifest]
        if operation.get("publication_origin") in _AUTONOMOUS_ORIGINS:
            summary["publication_origin"] = operation.get("publication_origin")
        return summary

    async def tool_heybox_post_status(self, event):
        if not owner_private(event):
            return _result(ok=False, reason="owner_private_only")
        async with self._heybox_posts_lock:
            rows = sorted(self._heybox_posts_ops.values(), key=lambda op: op.get("created_at", 0), reverse=True)
            return _result(ok=not self._heybox_posts_storage_fault, storage_ready=not self._heybox_posts_storage_fault,
                operations=[self._post_summary(op) for op in rows[:15]],
                capabilities=["preflight", "draft", "publish", "verify"],
                owner_reply_image_binding=True,
                autonomous=getattr(self, "heybox_autopost_status", lambda: {"enabled": False})(),
                image_sources="本机机器人数据目录中的 PNG/JPEG/WEBP，最长边8192，单张10MB以内")

    async def tool_heybox_post_preflight(self, event, topic_id=DEFAULT_TOPIC):
        if not owner_private(event):
            return _result(ok=False, reason="owner_private_only")
        with automatic_post_guard(lambda: _manual_post_allowed(event)):
            return await self._heybox_post_preflight(topic_id)

    async def _heybox_post_preflight(self, topic_id=DEFAULT_TOPIC):
        """Shared read-only preflight; background calls never impersonate an event."""
        try:
            if str(topic_id).strip() == "auto":
                result = await self._post_transport().preflight(DEFAULT_TOPIC)
                selected = choose_post_topic(result.get("topics", []), explicit_topic="auto")
                return _result(**{**result, "topic_selection": selected})
            if not re.fullmatch(r"\d{1,12}", str(topic_id)):
                raise PostBlocked("post_topic_invalid")
            result = await self._post_transport().preflight(str(topic_id))
            return _result(**result)
        except PostBlocked as exc:
            return _result(ok=False, reason=str(exc))
        except Exception as exc:
            return _result(ok=False, reason="heybox_preflight_failed", category=_category(exc))

    async def tool_draft_heybox_post(self, event, text, title="", image_paths_json="[]", topic_id="auto",
                                     *, approved_source_hashes=None):
        if not owner_private(event):
            return _result(ok=False, reason="owner_private_only")
        return await self._draft_heybox_post(event, text, title, image_paths_json, topic_id,
                                             approved_source_hashes=approved_source_hashes)

    async def _draft_heybox_post(self, event, text, title="", image_paths_json="[]", topic_id=DEFAULT_TOPIC,
                                 *, approved_source_hashes=None, autonomous_capability=None):
        autonomous = bool(autonomous_capability is not None and
            getattr(self, "authorized_autonomous_heybox_draft", lambda *_: False)(autonomous_capability))
        if not autonomous and (event is None or not owner_private(event)):
            return _result(ok=False, reason="owner_private_only")
        async with self._heybox_posts_lock:
            try:
                if self._heybox_posts_storage_fault:
                    raise PostBlocked("post_storage_unavailable")
                reference_url = self._heybox_autopost_context.get("reference_url") if autonomous else None
                title, text = _guard_content(title, text, trusted_reference_url=reference_url)
                if str(topic_id).strip() != "auto" and not re.fullmatch(r"\d{1,12}", str(topic_id)):
                    raise PostBlocked("post_topic_invalid")
                paths = json.loads(image_paths_json)
                if not isinstance(paths, list) or any(not isinstance(p, str) or not p or len(p) > 1024 for p in paths):
                    raise PostBlocked("post_image_paths_invalid")
                autonomous_manifest = None
                autonomous_origin = None
                topic_selection = None
                publication_metadata = None
                if str(topic_id).strip() == "auto":
                    if autonomous:
                        with automatic_post_guard(lambda: self.authorized_autonomous_heybox_draft(autonomous_capability)):
                            base_preflight = await self._post_transport().preflight(DEFAULT_TOPIC)
                    else:
                        with automatic_post_guard(lambda: _manual_post_allowed(event)):
                            base_preflight = await self._post_transport().preflight(DEFAULT_TOPIC)
                    topic_selection = choose_post_topic(base_preflight.get("topics", []),
                                                        title=title, text=text, explicit_topic="auto")
                    if topic_selection.get("ok") is not True:
                        raise PostBlocked(topic_selection.get("reason", "post_topic_invalid"))
                    topic_id = topic_selection["topic_id"]
                fingerprint_topic_id = str(topic_id)
                if autonomous:
                    context = self._heybox_autopost_context
                    expected_paths, expected_hashes, autonomous_manifest, autonomous_origin = (
                        _autonomous_image_manifest(context))
                    if paths != expected_paths or approved_source_hashes != expected_hashes:
                        raise PostBlocked("post_source_hash_guard_invalid")
                    # Only newly authorized art drafts receive these fields.
                    # Existing operations retain their original request and receipt.
                    if topic_selection is not None:
                        publication_metadata = choose_publication_metadata(
                            base_preflight.get("topics", []), topic_id,
                            comic_profile=context.get("comic_profile"))
                        topic_id = publication_metadata["topic_id"]
                if approved_source_hashes is not None and (not autonomous
                        or not isinstance(approved_source_hashes, list)
                        or len(approved_source_hashes) != len(paths)
                        or any(not isinstance(value, str) or not _SHA256.fullmatch(value)
                               for value in approved_source_hashes)):
                    raise PostBlocked("post_source_hash_guard_invalid")
                owner_requested = not autonomous and _owner_post_intent(event, for_draft=True)
                reply_source_sha256 = (owner_reply_image_sha256(event, paths, self._heybox_post_image_roots)
                                       if owner_requested else None)
                if (reply_source_sha256 and approved_source_hashes is not None
                        and approved_source_hashes != [reply_source_sha256]):
                    raise PostBlocked("post_approved_image_changed")
                if autonomous:
                    with automatic_post_guard(lambda: self.authorized_autonomous_heybox_draft(autonomous_capability)):
                        preflight = await self._post_transport().preflight(str(topic_id), **(
                            {key: publication_metadata[key] for key in ("topic_ids", "hashtags", "topic_names")}
                            if publication_metadata is not None else {}))
                else:
                    with automatic_post_guard(lambda: _manual_post_allowed(event)):
                        preflight = await self._post_transport().preflight(str(topic_id))
                if autonomous and not self.authorized_autonomous_heybox_draft(autonomous_capability):
                    raise PostBlocked("autonomous_post_authorization_changed")
                limits = preflight["limits"]
                if (not limits["text"]["min"] <= len(text) <= limits["text"]["max"]
                        or not limits["title"]["min"] <= len(title) <= limits["title"]["max"]
                        or len(paths) > limits["images"]):
                    raise PostBlocked("post_platform_limits_exceeded")
                images = [_stage_image(p, self._heybox_posts_images, self._heybox_post_image_roots,
                                       approved_source_hashes[index] if approved_source_hashes is not None
                                       else reply_source_sha256)
                          for index, p in enumerate(paths)]
                if autonomous:
                    # Staging decodes and re-encodes the source image. Keep
                    # both source and staged digests bound to each page.
                    autonomous_manifest = [
                        {**row, "staged_sha256": image["sha256"]}
                        for row, image in zip(autonomous_manifest, images)
                    ]
                # Keep the original selection in the legacy fingerprint. New
                # preferences must not make an existing exact draft publish again.
                fingerprint = hashlib.sha256(json.dumps([preflight["account_uid"], fingerprint_topic_id, title, text,
                    [row["sha256"] for row in images]], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                existing = next((op for op in self._heybox_posts_ops.values()
                                 if op.get("fingerprint") == fingerprint and op.get("phase") != "rejected"), None)
                if existing:
                    existing_autonomous = bool(existing.get("autonomous_grant_id")
                                               or existing.get("publication_origin") in _AUTONOMOUS_ORIGINS)
                    # A later genuine owner reply can bind the same image to
                    # an existing draft. Never change an issued receipt.
                    if not existing_autonomous and existing.get("phase") == "draft" and reply_source_sha256 and (
                            not existing.get("owner_requested") or not existing.get("owner_requested_images")):
                        existing.update(owner_requested=True, owner_requested_images=True, updated_at=time.time())
                        self._post_persist()
                    if not autonomous and not existing_autonomous and existing.get("phase") == "draft" and owner_requested:
                        qzone_intent = _owner_qzone_intent(event)
                        if (qzone_intent is not None
                                and _owner_draft_bound(self, event, existing["id"])
                                and existing.get("owner_requested_qzone") is not qzone_intent):
                            existing.update(owner_requested=True, owner_requested_qzone=qzone_intent, updated_at=time.time())
                            existing.pop("owner_qzone_confirmation_required", None)
                            self._post_persist()
                    return _result(ok=True, duplicate=True, operation=self._post_summary(existing))
                now = time.time()
                identifier = uuid.uuid4().hex
                if autonomous and not self.authorized_autonomous_heybox_draft(autonomous_capability):
                    raise PostBlocked("autonomous_post_authorization_changed")
                operation = {"id": identifier, "phase": "draft", "account_uid": preflight["account_uid"],
                    "topic_id": str(topic_id), "title": title, "text": text, "images": images,
                    "fingerprint": fingerprint, "created_at": now, "updated_at": now,
                    "owner_requested": owner_requested,
                    "owner_requested_images": bool(not autonomous and (_owner_image_intent(event) or reply_source_sha256))}
                if topic_selection is not None:
                    operation["topic_selection"] = {key: topic_selection[key] for key in
                                                     ("topic_name", "mode", "reason")
                                                     if key in topic_selection}
                if publication_metadata is not None:
                    operation.update({key: publication_metadata[key]
                                      for key in ("topic_ids", "hashtags", "topic_names")})
                    operation["post_metadata_policy"] = publication_metadata["reason"]
                if autonomous:
                    operation["autonomous_image_manifest"] = autonomous_manifest
                qzone_intent = _owner_qzone_intent(event) if owner_requested else None
                if owner_requested:
                    event_hash = _owner_event_hash(event)
                    if event_hash:
                        operation["owner_request_event_hash"] = event_hash
                    now_monotonic = time.monotonic()
                    self._heybox_owner_draft_events = {
                        key: binding for key, binding in self._heybox_owner_draft_events.items()
                        if now_monotonic - binding[1] <= 600 and key in self._heybox_posts_ops
                    }
                    related = [key for key, existing_op in self._heybox_posts_ops.items()
                               if event_hash and existing_op.get("owner_request_event_hash") == event_hash]
                    ambiguous = bool(related and (qzone_intent is not None or any(
                        "owner_requested_qzone" in self._heybox_posts_ops[key]
                        or self._heybox_posts_ops[key].get("owner_qzone_confirmation_required") for key in related)))
                    if qzone_intent is not None and not event_hash:
                        ambiguous = True
                    if (qzone_intent is not None and re.search(
                            r"(?<![a-zA-Z0-9])[a-f0-9]{32}(?![a-zA-Z0-9])",
                            _owner_instruction_text(event), re.IGNORECASE)):
                        # A new generated draft cannot be the previously named ID.
                        ambiguous = True
                    if ambiguous:
                        # Never guess which of several drafts the owner meant.
                        # Persist the uncertainty so a restart cannot erase it.
                        for key in related:
                            if self._heybox_posts_ops[key].get("phase") == "draft":
                                self._heybox_posts_ops[key].pop("owner_requested_qzone", None)
                                self._heybox_posts_ops[key]["owner_qzone_confirmation_required"] = True
                        operation["owner_qzone_confirmation_required"] = True
                        qzone_intent = None
                    if len(self._heybox_owner_draft_events) < 32:
                        self._heybox_owner_draft_events[identifier] = (event, now_monotonic)
                if qzone_intent is not None:
                    operation["owner_requested_qzone"] = qzone_intent
                if autonomous:
                    context = self._heybox_autopost_context
                    if preflight["account_uid"] != context["account_uid"]:
                        raise PostBlocked("heybox_account_changed")
                    operation.update(autonomous_grant_id=context["grant_id"],
                                     autonomous_asset_id=context["asset_id"],
                                     publication_origin=autonomous_origin,
                                     autonomous_reference_url=context["reference_url"])
                    if context.get("comic_profile") is not None:
                        operation["comic_profile"] = context["comic_profile"]
                    if autonomous_origin == "owner_extra_ai_friends":
                        extra_id = context.get("owner_extra_authorization_id")
                        if not isinstance(extra_id, str) or not re.fullmatch(r"[a-f0-9]{32}", extra_id):
                            raise PostBlocked("autonomous_post_authorization_changed")
                        operation["owner_extra_authorization_id"] = extra_id
                    if autonomous_origin == "scheduled_autopost":
                        operation["schedule_slot"] = context["slot_key"]
                    else:
                        operation["comic_task_key"] = context.get("task_key", context.get("slot_key"))
                self._heybox_posts_ops[identifier] = operation
                self._post_persist()
                return _result(ok=True, operation=self._post_summary(operation), published=False)
            except (PostBlocked, json.JSONDecodeError) as exc:
                return _result(ok=False, reason=str(exc) if isinstance(exc, PostBlocked) else "post_image_paths_invalid")
            except Exception as exc:
                return _result(ok=False, reason="heybox_draft_failed", category=_category(exc))

    def _post_image_bytes(self, image):
        # Only our content-addressed immutable staging names are read here.
        if not re.fullmatch(r"[a-f0-9]{64}\.png", str(image.get("name", ""))):
            raise PostBlocked("post_staged_image_invalid")
        path = self._heybox_posts_images / image["name"]
        if path.resolve(strict=True).parent != self._heybox_posts_images.resolve():
            raise PostBlocked("post_staged_image_invalid")
        with path.open("rb") as handle:
            data = handle.read(_MAX_IMAGE_BYTES + 1)
        if not 0 < len(data) <= _MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != image["sha256"]:
            raise PostBlocked("post_staged_image_changed")
        return {**image, "bytes": data}

    def _post_local_readback_proof(self, operation):
        local = []
        uploaded = operation.get("uploaded_images")
        if not isinstance(uploaded, list) or len(uploaded) != len(operation["images"]):
            return None
        for record, receipt in zip(operation["images"], uploaded):
            image = self._post_image_bytes(record)
            if (type(record.get("size")) is not int or record["size"] != len(image["bytes"])
                    or not isinstance(receipt, dict)
                    or any(type(record.get(key)) is not int or type(receipt.get(key)) is not int
                           or not 0 < record[key] <= _MAX_IMAGE_DIMENSION
                           or receipt[key] != record[key] for key in ("width", "height"))):
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(image["bytes"])) as probe:
                    if probe.format != "PNG" or getattr(probe, "n_frames", 1) != 1:
                        return None
                    probe.verify()
                with Image.open(BytesIO(image["bytes"])) as decoded:
                    decoded.load()
                    if decoded.size != (record["width"], record["height"]):
                        return None
            local.append({key: record[key] for key in ("sha256", "width", "height")})
        return {"operation_id": str(operation["id"]), "local_images": local,
                "uploaded_images": [dict(row) for row in uploaded]}

    async def _verify_post(self, operation, transport):
        if not operation.get("post_id"):
            return False
        # Only this local reader mints the operation-bound proof. Missing local
        # evidence retains the old strict path, never the legacy normalization.
        native = isinstance(transport, NativeHeyboxPostTransport)
        if native:
            transport._local_readback_proof = None
        try:
            if native and operation.get("images"):
                try:
                    transport._local_readback_proof = self._post_local_readback_proof(operation)
                except (OSError, ValueError, KeyError, TypeError,
                        Image.DecompressionBombWarning, Image.DecompressionBombError):
                    transport._local_readback_proof = None
            verified = await transport.verify(operation["post_id"], operation, operation.get("uploaded_images", []))
        finally:
            if native:
                transport._local_readback_proof = None
        if verified:
            operation.update(phase="verified", updated_at=time.time(), reason="")
            self._post_persist()
        return bool(verified)

    async def tool_publish_heybox_post(self, event, draft_id):
        if not owner_private(event):
            return _result(ok=False, reason="owner_private_only")
        return await self._publish_heybox_post(event, draft_id)

    async def _publish_heybox_post(self, event, draft_id, *, autonomous_capability=None):
        # Only an active in-process capability may use the eventless path.
        if event is None and autonomous_capability is None:
            return _result(ok=False, reason="owner_private_only")
        if event is not None and not owner_private(event):
            return _result(ok=False, reason="owner_private_only")
        async with self._heybox_posts_lock:
            operation = self._heybox_posts_ops.get(str(draft_id))
            if not operation:
                return _result(ok=False, reason="post_draft_not_found")
            dispatched = operation.get("phase") in _UNRESOLVED | _TERMINAL
            try:
                if self._heybox_posts_storage_fault:
                    raise PostBlocked("post_storage_unavailable")
                transport = self._post_transport()
                if dispatched:
                    if operation["phase"] in _UNRESOLVED:
                        if event is None:
                            with automatic_post_guard(lambda: _autonomous_readback_allowed(
                                    self, operation, autonomous_capability)):
                                await self._verify_post(operation, transport)
                        else:
                            with automatic_post_guard(lambda: _manual_post_allowed(event)):
                                await self._verify_post(operation, transport)
                    return _result(ok=operation["phase"] == "verified", operation=self._post_summary(operation), resent=False)
                autonomous = bool(autonomous_capability is not None and getattr(
                    self, "authorized_autonomous_heybox_post", lambda *_: False)(autonomous_capability, operation))
                if event is None and not autonomous:
                    raise PostBlocked("autonomous_post_authorization_changed")
                if not autonomous and not _owner_post_intent(event, followup=bool(operation.get("owner_requested")
                        and 0 <= time.time() - operation["created_at"] <= 86400)):
                    raise PostBlocked("owner_post_request_required")
                if not autonomous:
                    qzone_intent = _owner_qzone_intent(event, followup=True)
                    automated_draft = bool(operation.get("autonomous_grant_id")
                                           or operation.get("publication_origin") in _AUTONOMOUS_ORIGINS)
                    if automated_draft and qzone_intent is not None:
                        raise PostBlocked("owner_post_autonomous_draft_forbidden")
                    if not automated_draft:
                        if not _owner_draft_bound(self, event, operation["id"]):
                            raise PostBlocked("owner_post_draft_binding_required")
                        if operation.get("owner_qzone_confirmation_required") and qzone_intent is None:
                            raise PostBlocked("owner_post_qzone_confirmation_required")
                        if qzone_intent is not None and (operation.get("owner_requested_qzone") is not qzone_intent
                                or operation.get("owner_qzone_confirmation_required")
                                or not operation.get("owner_requested")):
                            operation.update(owner_requested=True, owner_requested_qzone=qzone_intent)
                            operation.pop("owner_qzone_confirmation_required", None)
                            self._post_persist()
                if operation["images"] and not (autonomous or operation.get("owner_requested_images") or _owner_image_intent(event)):
                    raise PostBlocked("owner_post_image_request_required")
                reference_url = self._heybox_autopost_context.get("reference_url") if autonomous else None
                if autonomous and operation.get("autonomous_reference_url") != reference_url:
                    raise PostBlocked("autonomous_post_authorization_changed")
                if autonomous:
                    manifest = operation.get("autonomous_image_manifest")
                    images = operation.get("images")
                    _, _, expected_manifest, expected_origin = _autonomous_image_manifest(
                        self._heybox_autopost_context)
                    if (operation.get("comic_profile") != self._heybox_autopost_context.get("comic_profile")
                            or expected_origin == "owner_extra_ai_friends" and
                            operation.get("owner_extra_authorization_id") !=
                            self._heybox_autopost_context.get("owner_extra_authorization_id")):
                        raise PostBlocked("autonomous_post_authorization_changed")
                    if (not isinstance(images, list) or not isinstance(manifest, list)
                            or len(manifest) != len(images)
                            or not 1 <= len(manifest) <= _MAX_AUTONOMOUS_IMAGES
                            or operation.get("publication_origin") != expected_origin
                            or [{"page": row.get("page"), "sha256": row.get("sha256")}
                                for row in manifest if isinstance(row, dict)] != expected_manifest
                            or any((not isinstance(row, dict) or type(row.get("page")) is not int
                                    or row["page"] != index or not isinstance(image, dict)
                                    or row.get("staged_sha256") != image.get("sha256"))
                                   for index, (row, image) in enumerate(zip(manifest, images), start=1))):
                        raise PostBlocked("post_source_hash_guard_invalid")
                _guard_content(operation["title"], operation["text"], trusted_reference_url=reference_url)
                metadata_args = ({key: operation.get(key) for key in ("topic_ids", "hashtags", "topic_names")}
                                 if "topic_ids" in operation or "hashtags" in operation else {})
                if autonomous:
                    with automatic_post_guard(lambda: self.authorized_autonomous_heybox_post(autonomous_capability, operation)):
                        preflight = await transport.preflight(operation["topic_id"], **metadata_args)
                else:
                    with automatic_post_guard(lambda: _manual_post_allowed(event)):
                        preflight = await transport.preflight(operation["topic_id"], **metadata_args)
                if preflight["account_uid"] != operation["account_uid"]:
                    raise PostBlocked("heybox_account_changed")
                limits = preflight["limits"]
                if (not limits["text"]["min"] <= len(operation["text"]) <= limits["text"]["max"]
                        or not limits["title"]["min"] <= len(operation["title"]) <= limits["title"]["max"]
                        or len(operation["images"]) > limits["images"]):
                    raise PostBlocked("post_platform_limits_exceeded")
                uploaded = operation.get("uploaded_images", [])
                if len(uploaded) != len(operation["images"]):
                    uploaded = []
                    for image in operation["images"]:
                        if autonomous:
                            if not self.authorized_autonomous_heybox_post(autonomous_capability, operation):
                                raise PostBlocked("autonomous_post_authorization_changed")
                            with automatic_post_guard(lambda: self.authorized_autonomous_heybox_post(autonomous_capability, operation)):
                                uploaded.append(await transport.upload_image(self._post_image_bytes(image)))
                        else:
                            with automatic_post_guard(lambda: _manual_post_allowed(event)):
                                uploaded.append(await transport.upload_image(self._post_image_bytes(image)))
                    operation["uploaded_images"] = uploaded
                if metadata_args:
                    # Uploads may take time. Re-read selected communities and
                    # the independent activity before the durable write intent.
                    if autonomous:
                        with automatic_post_guard(lambda: self.authorized_autonomous_heybox_post(autonomous_capability, operation)):
                            final_preflight = await transport.preflight(operation["topic_id"], **metadata_args)
                    else:
                        with automatic_post_guard(lambda: _manual_post_allowed(event)):
                            final_preflight = await transport.preflight(operation["topic_id"], **metadata_args)
                    if final_preflight["account_uid"] != operation["account_uid"]:
                        raise PostBlocked("heybox_account_changed")
                    limits = final_preflight["limits"]
                    if (not limits["text"]["min"] <= len(operation["text"]) <= limits["text"]["max"]
                            or not limits["title"]["min"] <= len(operation["title"]) <= limits["title"]["max"]
                            or len(operation["images"]) > limits["images"]):
                        raise PostBlocked("post_platform_limits_exceeded")
                # Recheck a revocable background grant after all awaited reads
                # and uploads, immediately before the durable public-write edge.
                if autonomous and not self.authorized_autonomous_heybox_post(autonomous_capability, operation):
                    raise PostBlocked("autonomous_post_authorization_changed")
                # Durable intent is written BEFORE crossing the public write
                # boundary. A crash at any subsequent point disables resends.
                dispatch_started_at = time.time()
                operation.update(phase="dispatching", updated_at=dispatch_started_at,
                                 dispatch_started_at=dispatch_started_at)
                self._post_persist()
                dispatched = True
                if autonomous:
                    with automatic_post_guard(lambda: self.authorized_autonomous_heybox_post(autonomous_capability, operation)):
                        post_id = await transport.publish(operation, uploaded)
                else:
                    with automatic_post_guard(lambda: _manual_post_allowed(event)):
                        post_id = await transport.publish(operation, uploaded)
                operation.update(phase="published_unverified", post_id=post_id,
                    url=f"https://example.invalid/app/bbs/link/{post_id}", updated_at=time.time())
                self._post_persist()
                if autonomous:
                    try:
                        with automatic_post_guard(lambda: _autonomous_readback_allowed(
                                self, operation, autonomous_capability)):
                            await self._verify_post(operation, transport)
                    except PostBlocked as exc:
                        if str(exc) != "automatic_action_paused":
                            raise
                        return _result(ok=True, operation=self._post_summary(operation), sent=True,
                                       verified=False, reason="peak_tariff")
                else:
                    with automatic_post_guard(lambda: _manual_post_allowed(event)):
                        await self._verify_post(operation, transport)
                return _result(ok=True, operation=self._post_summary(operation), sent=True,
                               verified=operation["phase"] == "verified")
            except Exception as exc:
                reason = str(exc) if isinstance(exc, PostBlocked) else "heybox_post_write_failed"
                if dispatched and operation.get("phase") not in _TERMINAL:
                    # Only explicit upstream rejection is a known non-send.
                    # Network, server, malformed receipt and readback failure
                    # remain unresolved and are NEVER automatically resent.
                    rejected = getattr(exc, "category", "") in {
                        "credential_invalid", "forbidden", "rate_limited", "upstream_rejected"}
                    phase = "rejected" if rejected and operation.get("phase") == "dispatching" else (
                        "published_unverified" if operation.get("post_id") else "unknown")
                    operation.update(phase=phase, reason=reason, updated_at=time.time())
                    try:
                        self._post_persist()
                    except PostBlocked:
                        pass
                result = dict(ok=False, reason=reason, operation=self._post_summary(operation),
                              resent=False, no_automatic_resend=dispatched)
                if reason == "owner_post_image_request_required":
                    result["guidance"] = "请附上要发布的图片，或引用包含该图片的完整消息。"
                elif reason in {"owner_post_draft_binding_required", "owner_post_qzone_confirmation_required"}:
                    result["guidance"] = "请使用完整草稿编号明确指定要发布的草稿，并说明是否同步到空间。"
                return _result(**result)

    async def tool_verify_heybox_post(self, event, operation_id):
        if not owner_private(event):
            return _result(ok=False, reason="owner_private_only")
        async with self._heybox_posts_lock:
            operation = self._heybox_posts_ops.get(str(operation_id))
            if not operation:
                return _result(ok=False, reason="post_operation_not_found")
            try:
                if operation.get("phase") != "verified":
                    with automatic_post_guard(lambda: _manual_post_allowed(event)):
                        await self._verify_post(operation, self._post_transport())
                return _result(ok=operation.get("phase") == "verified", operation=self._post_summary(operation),
                    resent=False, reason="" if operation.get("phase") == "verified" else "post_receipt_unconfirmed")
            except Exception:
                return _result(ok=False, reason="post_readback_unavailable", operation=self._post_summary(operation), resent=False)
