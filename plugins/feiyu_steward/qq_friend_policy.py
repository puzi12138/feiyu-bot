"""Fail-closed, time-limited QQ friend chat exception."""

import functools
import base64
import hashlib
import itertools
import json
import math
import os
import sqlite3
from contextlib import aclosing, closing, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import time
from urllib.parse import urlparse
import uuid
from weakref import WeakKeyDictionary

from .policy import OWNER_UMO
from .qq_chat_pause import read_qq_chat_pause
from .qq_friend_vision import (IMAGE_DIAGNOSTIC_VERSION, MAX_IMAGES, MAX_PIXELS,
                               MAX_SIDE, NormalizedImage,
                               image_diagnostic_status, load_qq_image,
                               record_image_diagnostic, valid_image_data)
from .token_pricing import TZ, _peak


FRIEND = "0"
FRIEND_UMO = f"snowluma-qq:FriendMessage:{FRIEND}"
SELF = "0"
STATE_FILE = (Path(__file__).resolve().parents[2] / "plugin_data" /
              "feiyu_steward" / "qq-friend-exception.json")
EVENT_DB = STATE_FILE.with_name("qq-friend-inbound.sqlite")
_guards_current = lambda: False
_friend_outbound = ContextVar("feiyu_friend_outbound", default=None)
_friend_model = ContextVar("feiyu_friend_model", default=None)
_friend_scope_valid = lambda grant: False
_friend_history = []
_friend_history_epoch = None
_friend_image_events = WeakKeyDictionary()
FRIEND_PROJECTION_FILE = Path(__file__).with_name("friend_persona_projection.json")
FRIEND_PROJECTION_SHA256 = ""
_friend_persona_resolution = "not_run"
FRIEND_SYSTEM_PROMPT = (
    "对方不是主人，不可使用工具、执行管理或发布操作，也不要透露别人的私人信息。"
)
FRIEND_IMAGE_FAILURE_TEXT = "这次没能识别图片内容，我还没看清。"
_friend_image_diagnostic_last_write = "not_run"
_IMAGE_DIAGNOSTIC_STAGES = frozenset(
    {"persona", "load", "model", "system_dns", "secure_dns", "download", "decode"})
_IMAGE_DIAGNOSTIC_REASONS = frozenset({
    "none", "other_error", "timeout", "tls_error", "permission_error", "response_error",
    "network_error", "value_error", "image_reference", "image_dns", "image_dns_fake",
    "image_dns_answer", "image_dns_question", "image_dns_response", "image_dns_size",
    "image_dns_header", "image_dns_status", "image_dns_encoding", "image_header",
    "image_status", "image_encoding", "image_size", "image_chunk", "image_empty",
    "image_dimensions", "image_format", "image_animation", "image_side",
    "image_pixels", "image_decode",
})


def _friend_image_failure_text(stage, error=None):
    """Explain the failed stage without exposing exception text or retrying."""
    if stage == "model":
        return "图片已读取，但这次识别没完成，我还不能确定图里的内容。"
    if stage != "load":
        return FRIEND_IMAGE_FAILURE_TEXT
    reason = None
    if (type(error) is ValueError and len(error.args) == 1
            and type(error.args[0]) is str
            and error.args[0] in _IMAGE_DIAGNOSTIC_REASONS):
        reason = error.args[0]
    if reason in {"image_dimensions", "image_side", "image_pixels"}:
        return "这张图片的尺寸超出处理范围，我还没看清；可以缩小或分段截图后再发。"
    if reason == "image_format":
        return "暂时不支持这张图片的格式，可以转成普通 JPG 或 PNG 后再发。"
    if reason == "image_animation":
        return "这张动图没能提取出静态画面，可以截一张图再发。"
    if reason == "image_decode":
        return "这张图片没能正常解码，我还没看清；可以转成普通 JPG 或 PNG 后再发。"
    if reason == "image_size":
        return "这张图片的数据太大，暂时处理不了；可以压缩或缩小后再发。"
    if isinstance(error, TimeoutError):
        return "图片下载超时，这次还没读到图片。"
    if (isinstance(error, OSError) or reason in {
            "image_dns", "image_dns_fake", "image_dns_answer", "image_dns_question",
            "image_dns_response", "image_dns_size", "image_dns_header", "image_dns_status",
            "image_dns_encoding", "image_header", "image_status", "image_encoding",
            "image_chunk", "image_empty"}):
        return "这次没能下载到图片，我还没看到图里的内容。"
    return FRIEND_IMAGE_FAILURE_TEXT


def _read_friend_image_diagnostic():
    """Read only the last safe stage result, never restore process counters."""
    try:
        path = STATE_FILE.with_name("qq-friend-image-diagnostics.json")
        with path.open("rb") as stream:
            raw = stream.read(1025)
    except FileNotFoundError:
        return {"status": "missing", "last_result": None}
    except Exception:
        return {"status": "unavailable", "last_result": None}
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_key")
                result[key] = value
            return result

        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if (len(raw) > 1024 or not isinstance(data, dict)
                or set(data) != {"version", "stage", "outcome", "reason"}
                or data["version"] not in (IMAGE_DIAGNOSTIC_VERSION, "friend_image_dns_v1")
                or data["stage"] not in _IMAGE_DIAGNOSTIC_STAGES
                or data["outcome"] not in {"success", "failure"}
                or data["reason"] not in _IMAGE_DIAGNOSTIC_REASONS
                or (data["outcome"] == "success") != (data["reason"] == "none")):
            raise ValueError("invalid_image_diagnostic")
        return {"status": "ok", "last_result": data}
    except Exception:
        return {"status": "invalid", "last_result": None}


def _record_friend_image_diagnostic(stage, outcome, error=None):
    """Keep process counters and atomically replace one bounded safe disk result.

    The disk result survives reloads. Counters reset only when the vision module
    is initialized, not on guard reinstallation. Last writer wins across
    processes. Diagnostic failure never changes chat or retry behavior.
    """
    global _friend_image_diagnostic_last_write
    temp = None
    try:
        if stage not in _IMAGE_DIAGNOSTIC_STAGES or outcome not in {"success", "failure"}:
            return
        record_image_diagnostic(stage, outcome, error)
        snapshot = image_diagnostic_status()
        result = {"version": IMAGE_DIAGNOSTIC_VERSION, "stage": stage,
                  "outcome": outcome, "reason": snapshot["stages"][stage]["last_reason"]}
        if (result["reason"] not in _IMAGE_DIAGNOSTIC_REASONS
                or (outcome == "success") != (result["reason"] == "none")):
            raise ValueError("invalid_image_diagnostic")
        path = STATE_FILE.with_name("qq-friend-image-diagnostics.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _friend_image_diagnostic_last_write = "success"
    except Exception:
        _friend_image_diagnostic_last_write = "failure"
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass


def read_friend_policy(path=None):
    """Read the separately persisted exception; absence or damage denies it."""
    path = Path(path) if path is not None else STATE_FILE
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return {"valid": True, "present": False, "enabled": False, "effective_from": None}
    except OSError:
        return {"valid": False, "present": True, "enabled": False, "effective_from": None}
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_key")
                result[key] = value
            return result

        if len(raw) > 4096:
            raise ValueError("state_too_large")
        state = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if (not isinstance(state, dict)
                or set(state) != {"version", "enabled", "effective_from"}
                or type(state["version"]) is not int or state["version"] != 1
                or not isinstance(state["enabled"], bool)
                or type(state["effective_from"]) is not int
                or state["effective_from"] < 0):
            raise ValueError("invalid_state")
        return {"valid": True, "present": True, **state}
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return {"valid": False, "present": True, "enabled": False, "effective_from": None}


def set_friend_policy(enabled, path=None, now=None):
    """Persist a new activation boundary without modifying pause history."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled_must_be_boolean")
    path = Path(path) if path is not None else STATE_FILE
    state = read_friend_policy(path)
    if not state["valid"]:
        raise ValueError("invalid_friend_policy")
    stamp = time.time() if now is None else now
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
        raise ValueError("invalid_time")
    # Keep an active boundary on repeat enable; re-enable starts a new period.
    start = state["effective_from"] if enabled and state["enabled"] else math.ceil(stamp)
    data = {"version": 1, "enabled": enabled, "effective_from": start}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return read_friend_policy(path)


def friend_window(at=None, *, policy=None):
    """Identify an authorized continuous daytime off-peak window, if any."""
    at = time.time() if at is None else at
    policy = read_friend_policy() if policy is None else policy
    pause = read_qq_chat_pause()
    if (not policy.get("valid") or not policy.get("enabled")
            or not pause["valid"] or not pause["present"] or not pause["paused"]
            or not _guards_current()
            or type(at) not in (int, float) or not math.isfinite(at)
            or at < policy["effective_from"] or _peak(at) is not False):
        return None
    try:
        current_pause = pause["intervals"][-1]
        pause_started = current_pause.get("start_exact", current_pause["start"])
        if at < math.ceil(pause_started):
            return None
        local = datetime.fromtimestamp(at, TZ)
        if not 8 <= local.hour < 22:
            return None
        morning_peak = local.replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
        afternoon_peak = local.replace(hour=15, minute=0, second=0, microsecond=0).timestamp()
        if _peak(morning_peak) is False and _peak(afternoon_peak) is False:
            slot = "day"
        elif _peak(morning_peak) is True and _peak(afternoon_peak) is True:
            slot = "morning" if local.hour < 9 else "noon" if local.hour < 14 else "evening"
        else:
            return None
        return (local.date().isoformat(), slot)
    except (IndexError, KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def friend_raw_allowed(raw, at=None):
    """Reject stale, non-friend or non-text data before adapter conversion."""
    now = time.time() if at is None else at
    try:
        if (not isinstance(raw, dict) or raw.get("post_type") != "message"
                or raw.get("message_type") != "private" or raw.get("group_id")
                or raw.get("sub_type") != "friend"
                or str(raw.get("user_id", "")) != FRIEND
                or str(raw.get("self_id", "")) != SELF):
            return False
        if not _friend_inbound_payload_allowed(raw.get("message")):
            return False
        sender = raw.get("sender")
        if not isinstance(sender, dict) or str(sender.get("user_id", "")) != FRIEND:
            return False
        message_id = raw.get("message_id")
        if (type(message_id) not in (int, str) or len(str(message_id)) > 32
                or not str(message_id).isascii() or not str(message_id).lstrip("-").isdigit()
                or int(message_id) == 0):
            return False
        stamp = raw.get("time")
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or type(now) not in (int, float) or not math.isfinite(now)
                or stamp > now + 60 or now - stamp > 300):
            return False
        policy = read_friend_policy()
        current = friend_window(now, policy=policy)
        return current is not None and current == friend_window(stamp, policy=policy)
    except Exception:
        return False


def friend_event_allowed(event, at=None):
    """Recheck the raw proof and independently converted event at each boundary."""
    try:
        if (str(getattr(event, "unified_msg_origin", "")) != FRIEND_UMO
                or str(event.get_sender_id()) != FRIEND
                or str(getattr(event, "get_group_id", lambda: "")() or "")):
            return False
        obj = getattr(event, "message_obj", None)
        if (str(getattr(obj, "group_id", "") or "")
                or str(getattr(obj, "self_id", "") or "") != SELF
                or str(getattr(getattr(obj, "sender", None), "user_id", "")) != FRIEND
                or not friend_raw_allowed(getattr(obj, "raw_message", None), at)):
            return False
        from astrbot.core.message.components import Plain
        components = getattr(obj, "message", None)
        state = _friend_image_events.get(event)
        if state is not None and _friend_inbound_identity(event) != state["identity"]:
            return False
        raw_parts = obj.raw_message["message"]
        has_images = any(part["type"] == "image" for part in raw_parts)
        if not has_images:
            return (state is None and isinstance(components, list) and bool(components)
                    and all(isinstance(item, Plain) for item in components))
        from astrbot.core.message.components import Image
        expected = []
        for kind, group in itertools.groupby(raw_parts, key=lambda item: item["type"]):
            group = list(group)
            if kind == "text":
                text = "".join(part["data"]["text"] for part in group).strip()
                if text:
                    expected.append(("text", text))
            else:
                expected.extend(("image", part["data"]) for part in group)
        if not isinstance(components, list) or len(components) != len(expected):
            return False
        for component, (kind, value) in zip(components, expected):
            if kind == "text":
                if not isinstance(component, Plain) or component.text != value:
                    return False
            elif state is not None and state["neutralized"]:
                if not isinstance(component, Plain) or component.text != "[图片]":
                    return False
            elif (not isinstance(component, Image) or component.file != value["file"]
                  or component.url != value["url"] or getattr(component, "path", "")):
                return False
        return True
    except Exception:
        return False


def _friend_text_allowed(value):
    """Bound one outbound friend reply to plain, non-control text."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4000:
        return False
    try:
        return (len(value.encode("utf-8")) <= 12000
                and "[CQ:" not in value.upper()
                and not any("\x00" <= char <= "\x1f" and char not in "\t\n\r"
                            for char in value))
    except UnicodeError:
        return False


def friend_message_payload_allowed(message):
    """Validate a OneBot private-send message without accepting media."""
    if isinstance(message, str):
        return _friend_text_allowed(message)
    if not isinstance(message, list) or not message:
        return False
    text = []
    for part in message:
        if (not isinstance(part, dict) or set(part) != {"type", "data"}
                or part.get("type") != "text" or not isinstance(part.get("data"), dict)
                or set(part["data"]) != {"text"}):
            return False
        value = part["data"]["text"]
        if not isinstance(value, str):
            return False
        text.append(value)
    return _friend_text_allowed("".join(text))


def _friend_inbound_payload_allowed(message):
    """Accept bounded text and current-event QQ images only on ingress."""
    if not isinstance(message, list) or not message:
        return False
    if all(isinstance(part, dict) and part.get("type") == "text" for part in message):
        return friend_message_payload_allowed(message)
    if len(message) > 64:
        return False
    texts, images = [], 0
    for part in message:
        if not isinstance(part, dict) or set(part) != {"type", "data"}:
            return False
        data = part["data"]
        if part["type"] == "text":
            if not isinstance(data, dict) or set(data) != {"text"} or not isinstance(data["text"], str):
                return False
            texts.append(data["text"])
        elif part["type"] == "image" and valid_image_data(data):
            images += 1
        else:
            return False
    text = "".join(texts)
    return bool(1 <= images <= MAX_IMAGES and (not text or _friend_text_allowed(text)))


def friend_text(event):
    """Use only the current event's raw text, never previous conversations."""
    return "".join(part["data"]["text"] for part in event.message_obj.raw_message["message"]
                   if part["type"] == "text")


def _friend_inbound_identity(event):
    """Freeze the admitted event identity and text without persisting either."""
    raw = event.message_obj.raw_message
    message_id, timestamp = raw["message_id"], raw["time"]
    parts = tuple((part["type"], tuple((key, type(value), value)
                                      for key, value in sorted(part["data"].items())))
                  for part in raw["message"])
    return type(message_id), message_id, type(timestamp), timestamp, parts, friend_text(event)


def friend_history_snapshot():
    """Keep only bounded, locally completed turns from this activation."""
    global _friend_history_epoch
    policy = read_friend_policy()
    pause = read_qq_chat_pause()
    try:
        if not policy["valid"] or not policy["enabled"] or not pause["valid"] or not pause["paused"]:
            return []
        interval = pause["intervals"][-1]
        epoch = (policy["effective_from"], interval.get("start_exact", interval["start"]))
        if epoch != _friend_history_epoch:
            _friend_history.clear()
            _friend_history_epoch = epoch
        return [dict(row) for row in _friend_history]
    except (KeyError, TypeError, IndexError):
        return []


def remember_friend_turn(event, chain):
    """Remember only after a local send returns; no delivery claim is made."""
    if not friend_event_allowed(event) or not friend_message_chain_allowed(chain):
        return
    friend_history_snapshot()
    _friend_history.extend((
        {"role": "user", "content": friend_text(event) or "[图片]"},
        {"role": "assistant", "content": "".join(part.text for part in chain.chain)},
    ))
    del _friend_history[:-12]


def restrict_friend_request(event, req):
    """Discard inherited persona, history, content parts and all tools."""
    req.prompt = friend_text(event) or "[图片]"
    req.system_prompt = FRIEND_SYSTEM_PROMPT
    req.contexts = []
    req.image_urls = []
    req.audio_urls = []
    req.extra_user_content_parts = []
    req.func_tool = None
    req.conversation = None
    req.tool_calls_result = None
    req.session_id = FRIEND_UMO
    req.model = None


def _read_friend_projection():
    """Read only the reviewed projection and reject any unreviewed edit."""
    with FRIEND_PROJECTION_FILE.open("rb") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768 or hashlib.sha256(raw).hexdigest() != FRIEND_PROJECTION_SHA256:
        raise ValueError("projection_hash")
    projection = json.loads(raw.decode("utf-8"))
    keys = {"persona_id", "persona_source_sha256", "reply_style_source_sha256",
            "persona_projection", "reply_style_projection"}
    if (not isinstance(projection, dict) or set(projection) != keys | {"version"}
            or type(projection.get("version")) is not int or projection["version"] != 1
            or not all(isinstance(projection.get(key), str) and projection[key] for key in keys)
            or any(len(projection[key]) != 64
                   or any(char not in "0123456789abcdef" for char in projection[key])
                   for key in ("persona_source_sha256", "reply_style_source_sha256"))):
        raise ValueError("projection_format")
    return projection


async def _friend_system_prompt(host, event):
    """Resolve an approved, shareable projection of the owner's persona."""
    global _friend_persona_resolution
    try:
        projection = _read_friend_projection()

        from sqlalchemy import select
        from astrbot.core.db.po import ConversationV2

        manager = host.context.conversation_manager
        conversation_id = await manager.get_curr_conversation_id(OWNER_UMO)
        conversation_persona_id = None
        if conversation_id:
            # Fetch only persona metadata; never materialize the owner's history.
            async with manager.db.get_db() as db:
                result = await db.execute(select(ConversationV2.persona_id).where(
                    ConversationV2.conversation_id == conversation_id,
                    ConversationV2.user_id == OWNER_UMO,
                ))
                row = result.one_or_none()
            if row is None:
                raise ValueError("conversation_missing")
            conversation_persona_id = row[0]
        selected, persona, _, webchat_default = (
            await host.context.persona_manager.resolve_selected_persona(
                umo=OWNER_UMO, conversation_persona_id=conversation_persona_id,
                platform_name=event.get_platform_name(), provider_settings=None,
            )
        )
        if (webchat_default or selected != projection["persona_id"]
                or not isinstance(persona, dict) or persona.get("name") != selected
                or not isinstance(persona.get("prompt"), str)
                or hashlib.sha256(persona["prompt"].encode("utf-8")).hexdigest()
                   != projection["persona_source_sha256"]):
            raise ValueError("persona_unreviewed")

        companion = host.plugin("astrbot_plugin_private_companion")
        if companion is None or getattr(companion, "enable_multi_persona_mode", None) is not False:
            raise ValueError("companion_unavailable")
        style = companion.persona_setting("reply_style_prompt", "", persona_id=selected)
        if (not isinstance(style, str)
                or hashlib.sha256(style.encode("utf-8")).hexdigest()
                   != projection["reply_style_source_sha256"]):
            raise ValueError("style_unreviewed")
        voice_enabled = companion.persona_setting("enable_persona_voice_channels", True, persona_id=selected)
        voice = companion.persona_setting("persona_conversation_voice_prompt", "", persona_id=selected)
        if (not isinstance(voice_enabled, bool) or not isinstance(voice, str)
                or (voice_enabled and voice.strip())):
            raise ValueError("voice_unreviewed")
        prompt = (projection["persona_projection"] + "\n\n"
                  + projection["reply_style_projection"] + "\n\n" + FRIEND_SYSTEM_PROMPT)
        _friend_persona_resolution = "verified"
        return prompt
    except Exception:
        _friend_persona_resolution = "unavailable"
        # Do not log or expose persona content, database errors or private metadata.
        raise PermissionError("friend_persona_unavailable_or_unreviewed") from None


def _friend_sdk_payload_allowed(kwargs, snapshot):
    """Permit only the exact immutable snapshot at the final SDK boundary."""
    if any(kwargs.get(key) for key in ("tools", "functions", "tool_choice", "function_call")):
        return False
    extra = kwargs.get("extra_body")
    if extra and (not isinstance(extra, dict)
                  or set(extra) & {"messages", "tools", "functions", "tool_choice", "function_call", "model"}):
        return False
    messages = kwargs.get("messages")
    expected = [{"role": role, "content": content} for role, content in snapshot]
    if not isinstance(messages, list) or len(messages) != len(expected):
        return False
    for message, item in zip(messages, expected):
        if not isinstance(message, dict) or message.get("role") != item["role"]:
            return False
        fields = set(message)
        if fields != {"role", "content"} and not (
            item["role"] == "assistant"
            and fields == {"role", "content", "reasoning_content"}
            and message["reasoning_content"] == ""
        ):
            return False
        content = message["content"]
        expected_content = item["content"]
        if isinstance(expected_content, str):
            if content != expected_content and content != [{"type": "text", "text": expected_content}]:
                return False
        elif isinstance(expected_content, tuple):
            parts = []
            for kind, value in expected_content:
                if kind == "text":
                    parts.append({"type": "text", "text": value})
                elif kind == "image_url":
                    try:
                        prefix = "data:image/jpeg;base64,"
                        if not value.startswith(prefix):
                            return False
                        decoded = base64.b64decode(value[len(prefix):], validate=True)
                        if not decoded.startswith(b"\xff\xd8\xff") or not decoded.endswith(b"\xff\xd9"):
                            return False
                    except (ValueError, TypeError):
                        return False
                    parts.append({"type": "image_url", "image_url": {"url": value}})
                else:
                    return False
            if content != parts:
                return False
        else:
            return False
    return True


def friend_message_chain_allowed(chain):
    """Validate the AstrBot outbound chain before it reaches OneBot."""
    try:
        from astrbot.core.message.components import Plain
        parts = getattr(chain, "chain", None)
        if not isinstance(parts, list) or not parts or not all(isinstance(part, Plain) for part in parts):
            return False
        return _friend_text_allowed("".join(part.text for part in parts))
    except (AttributeError, ImportError, TypeError, UnicodeError):
        return False


def reserve_friend_event(event):
    """Persist a hash once before dispatch, without retaining message text."""
    if not friend_event_allowed(event):
        return False
    message_id = str(event.message_obj.raw_message["message_id"])
    key = hashlib.sha256((FRIEND_UMO + "\0" + message_id).encode("ascii")).hexdigest()
    try:
        EVENT_DB.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(EVENT_DB, timeout=1)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS inbound (key TEXT PRIMARY KEY, admitted_at REAL NOT NULL)")
                cursor = db.execute("INSERT OR IGNORE INTO inbound (key, admitted_at) VALUES (?, ?)",
                                    (key, time.time()))
                if cursor.rowcount != 1:
                    return False
        if any(part["type"] == "image" for part in event.message_obj.raw_message["message"]):
            _friend_image_events[event] = {"identity": _friend_inbound_identity(event),
                                           "neutralized": False, "attempted": False}
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False


@contextmanager
def friend_send_scope(event):
    """Tie one send to its originating event and revoke descendant grants."""
    grant = {"event": event, "active": True}
    token = _friend_outbound.set(grant)
    try:
        yield
    finally:
        grant["active"] = False
        _friend_outbound.reset(token)


def friend_send_allowed(at=None):
    """Deny background and stale sends outside the live event's call."""
    grant = _friend_outbound.get()
    return bool(grant and grant["active"] and friend_event_allowed(grant["event"], at))


def friend_model_allowed(at=None):
    try:
        grant = _friend_model.get()
        if (not _friend_scope_valid(grant) or not friend_event_allowed(grant[1], at)
                or _friend_inbound_identity(grant[1]) != grant[3]):
            return False
        content = grant[2][-1]
        image_count = sum(kind == "image" for kind, _ in grant[3][4])
        if not image_count:
            return content == ("user", grant[3][-1])
        expected_text = (("text", grant[3][-1] or "请看看这张图片。"),)
        return (content[0] == "user" and isinstance(content[1], tuple)
                and content[1][:len(expected_text)] == expected_text
                and len(content[1]) == len(expected_text) + image_count
                and all(part[0] == "image_url" for part in content[1][len(expected_text):]))
    except Exception:
        return False


def friend_policy_status():
    """Expose policy readiness without message contents or other state."""
    state = read_friend_policy()
    try:
        projection_version = _read_friend_projection()["version"]
        projection_ready = True
    except Exception:
        projection_version = None
        projection_ready = False
    return {"configured": state["present"], "valid": state["valid"],
            "enabled": state["enabled"], "effective_from": state["effective_from"],
            "guards_ready": bool(_guards_current()), "open_now": friend_window() is not None,
            "image_input": {"enabled": True, "max_images": MAX_IMAGES,
                            "scope": "current_event", "max_sdk_attempts": 1},
            "image_compatibility": {"version": IMAGE_DIAGNOSTIC_VERSION,
                                    "animation": "single_display_frame",
                                    "max_source_pixels": MAX_PIXELS,
                                    "max_source_side": MAX_SIDE},
            "image_diagnostics": {"current_process": image_diagnostic_status(),
                                  "persisted": _read_friend_image_diagnostic(),
                                  "last_write": _friend_image_diagnostic_last_write},
            "owner_reviewed_persona": {"guard_version": 1,
                                       "projection_version": projection_version,
                                       "projection_ready": projection_ready,
                                       "last_resolution": _friend_persona_resolution}}


def install_friend_guards(host, waking_stage=None, executor=None, internal_module=None, runner=None,
                          third_party=None):
    """Keep the exception on the ordinary chat path and deny all tools."""
    global _guards_current, _friend_scope_valid, _friend_persona_resolution
    if waking_stage is None:
        from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
        waking_stage = WakingCheckStage
    if executor is None:
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
        executor = FunctionToolExecutor
    if internal_module is None:
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal as internal_module
    if runner is None:
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        runner = ToolLoopAgentRunner
    if third_party is None:
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.third_party import ThirdPartyAgentSubStage
        third_party = ThirdPartyAgentSubStage
    original_wake = getattr(waking_stage, "process", None)
    descriptor = executor.__dict__.get("execute")
    original_build = getattr(internal_module, "build_main_agent", None)
    original_iter = getattr(runner, "_iter_llm_responses", None)
    original_third_party = getattr(third_party, "process", None)
    provider = getattr(host, "_group_budget_provider", None)
    completions = getattr(getattr(getattr(provider, "client", None), "chat", None), "completions", None)
    original_create = getattr(completions, "create", None)
    original_transform = getattr(provider, "_transform_content_part", None)
    original_recover = getattr(provider, "_handle_api_error", None)
    if (not callable(original_wake) or not isinstance(descriptor, classmethod)
            or not callable(original_build) or not callable(original_iter)
            or not callable(original_third_party) or not callable(original_create)
            or not callable(original_transform) or not callable(original_recover)):
        raise TypeError("friend_guards_unsupported")
    original_tool = descriptor.__get__(None, executor)
    lifetime = {"active": True}
    scope_seal = object()
    active_scopes = {}

    def valid_scope(grant):
        return bool(lifetime["active"] and isinstance(grant, tuple) and len(grant) == 4
                    and grant[0] is scope_seal and isinstance(grant[2], tuple)
                    and active_scopes.get(id(grant)) is grant)

    @contextmanager
    def friend_model_scope(event, snapshot, inbound_identity):
        # Only this closure can grant a scope, and its snapshot cannot be replaced.
        grant = (scope_seal, event, snapshot, inbound_identity)
        active_scopes[id(grant)] = grant
        token = _friend_model.set(grant)
        try:
            yield
        finally:
            active_scopes.pop(id(grant), None)
            _friend_model.reset(token)

    @functools.wraps(original_wake)
    async def wake(stage, event):
        if not lifetime["active"] or str(getattr(event, "unified_msg_origin", "")) != FRIEND_UMO:
            return await original_wake(stage, event)
        if not friend_event_allowed(event):
            event.stop_event()
            return
        raw_parts = event.message_obj.raw_message["message"]
        if any(part["type"] == "image" for part in raw_parts):
            state = _friend_image_events.get(event)
            if state is None or state["neutralized"]:
                event.stop_event()
                return
            from astrbot.core.message.components import Image, Plain
            event.message_obj.message[:] = [Plain(text="[图片]") if isinstance(part, Image) else part
                                            for part in event.message_obj.message]
            state["neutralized"] = True
            if not friend_event_allowed(event):
                event.stop_event()
                return
        # Avoid command and regex handlers altogether; retain only steward's
        # request and result hooks for this one ordinary private chat.
        event.role = "member"
        event.message_str = str(getattr(event, "message_str", "") or "").strip() or "[图片]"
        event.plugins_name = ["feiyu_steward"]
        event.is_wake = True
        event.is_at_or_wake_command = True
        event.set_extra("activated_handlers", [])
        event.set_extra("handlers_parsed_params", {})
        event.set_extra("provider_request", None)
        event.set_extra("selected_model", None)
        event.set_extra("enable_streaming", False)

    @functools.wraps(original_tool)
    async def execute(cls, tool, run_context, **tool_args):
        event = getattr(getattr(run_context, "context", None), "event", None)
        if lifetime["active"] and str(getattr(event, "unified_msg_origin", "")) == FRIEND_UMO:
            from mcp.types import CallToolResult, TextContent
            yield CallToolResult(content=[TextContent(
                type="text", text='{"ok":false,"reason":"friend_chat_only"}')], isError=True)
            return
        async with aclosing(original_tool(tool, run_context, **tool_args)) as results:
            async for item in results:
                yield item

    @functools.wraps(original_build)
    async def build(*args, **kwargs):
        event = kwargs.get("event")
        if not lifetime["active"] or str(getattr(event, "unified_msg_origin", "")) != FRIEND_UMO:
            return await original_build(*args, **kwargs)
        if (args or kwargs.get("plugin_context") is not host.context
                or kwargs.get("apply_reset") is not False or kwargs.get("req") is not None
                or not friend_event_allowed(event)):
            event.stop_event()
            return None
        try:
            config = kwargs["config"]
            settings = dict(config.provider_settings)
            settings["default_image_caption_provider_id"] = ""
            kwargs["config"] = replace(
                config, provider_settings=settings, fallback_provider_ids=[],
                request_max_retries=1, file_extract_enabled=False,
                context_limit_reached_strategy="truncate_by_turns", llm_compress_provider_id="",
                kb_agentic_mode=True, computer_use_runtime="none", add_cron_tools=False,
                max_quoted_fallback_images=0,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            event.stop_event()
            return None
        kwargs["provider"] = provider
        event.set_extra("selected_model", None)
        result = await original_build(*args, **kwargs)
        if result is None:
            return None
        if (getattr(result, "provider", None) is not provider
                or not friend_event_allowed(event)
                or getattr(result, "provider_request", None) is None):
            reset_coro = getattr(result, "reset_coro", None)
            if reset_coro is not None:
                reset_coro.close()
            event.stop_event()
            return None
        restrict_friend_request(event, result.provider_request)
        return result

    @functools.wraps(original_iter)
    async def model_iter(instance, *args, **kwargs):
        event = getattr(getattr(getattr(instance, "run_context", None), "context", None), "event", None)
        request = getattr(instance, "req", None)
        session = str(getattr(request, "session_id", "") or "")
        origin = str(getattr(event, "unified_msg_origin", ""))
        if not lifetime["active"] or (origin != FRIEND_UMO and session != FRIEND_UMO):
            async with aclosing(original_iter(instance, *args, **kwargs)) as results:
                async for item in results:
                    yield item
            return
        image_state = _friend_image_events.get(event)
        if (origin != FRIEND_UMO or session != FRIEND_UMO or not friend_event_allowed(event)
                or getattr(instance, "provider", None) is not provider
                or getattr(request, "model", None) is not None
                or getattr(instance, "fallback_providers", None)
                or getattr(instance, "streaming", None) is not False
                or getattr(instance, "request_max_retries", None) != 1
                or (image_state is None and any(part["type"] == "image"
                    for part in event.message_obj.raw_message["message"]))
                or (image_state is not None and (not image_state["neutralized"]
                                                or image_state["attempted"]
                                                or getattr(provider.client, "max_retries", None) != 0))):
            raise PermissionError("friend_model_guard")
        # Rebuild from reviewed persona/style and this friend's own history only.
        from astrbot.core.agent.message import Message
        inbound_identity = _friend_inbound_identity(event)
        try:
            system_prompt = await _friend_system_prompt(host, event)
        except Exception as error:
            if image_state is not None:
                _record_friend_image_diagnostic("persona", "failure", error)
            if image_state is not None and friend_event_allowed(event):
                from astrbot.core.provider.entities import LLMResponse
                yield LLMResponse(role="assistant", completion_text=FRIEND_IMAGE_FAILURE_TEXT)
                return
            raise
        if image_state is not None:
            _record_friend_image_diagnostic("persona", "success")
        if not friend_event_allowed(event) or _friend_inbound_identity(event) != inbound_identity:
            raise PermissionError("friend_model_guard")
        image_uris = []
        static_frame_images = []
        if image_state is not None:
            try:
                for kind, fields in inbound_identity[4]:
                    if kind == "image":
                        loaded = await load_qq_image(
                            {key: value for key, _, value in fields}, with_metadata=True)
                        if (not isinstance(loaded, NormalizedImage)
                                or type(loaded.data_uri) is not str
                                or type(loaded.first_frame_only) is not bool):
                            raise ValueError("image_decode")
                        image_uris.append(loaded.data_uri)
                        if loaded.first_frame_only:
                            static_frame_images.append(len(image_uris))
                        if not friend_event_allowed(event) or _friend_inbound_identity(event) != inbound_identity:
                            raise ValueError("image_event_changed")
            except Exception as error:
                _record_friend_image_diagnostic("load", "failure", error)
                if friend_event_allowed(event) and _friend_inbound_identity(event) == inbound_identity:
                    from astrbot.core.provider.entities import LLMResponse
                    yield LLMResponse(role="assistant", completion_text=_friend_image_failure_text("load", error))
                    return
                raise PermissionError("friend_model_guard") from None
            _record_friend_image_diagnostic("load", "success")
        if static_frame_images:
            frame_numbers = "、".join(str(number) for number in static_frame_images)
            system_prompt += (
                f"\n当前第{frame_numbers}张图片仅提供一张静态画面，不能据此判断完整动态过程。"
                "如果对方询问动画动作或变化，应明确说明只能看到这张静态画面，不要编造后续帧。")
        restrict_friend_request(event, request)
        request.system_prompt = system_prompt
        history = friend_history_snapshot()
        current_content = friend_text(event)
        if image_state is not None:
            current_content = (("text", current_content or "请看看这张图片。"),) + tuple(
                ("image_url", uri) for uri in image_uris)
        snapshot = (("system", system_prompt),
                    *((row["role"], row["content"]) for row in history),
                    ("user", current_content))
        instance.run_context.messages = []
        for role, content in snapshot:
            if isinstance(content, tuple):
                from astrbot.core.agent.message import ImageURLPart, TextPart
                parts = [TextPart(text=value) if kind == "text"
                         else ImageURLPart(image_url=ImageURLPart.ImageURL(url=value)).mark_as_temp()
                         for kind, value in content]
                instance.run_context.messages.append(Message(role=role, content=parts))
            else:
                instance.run_context.messages.append(Message(role=role, content=content))
        responses = []
        image_model_failed = False
        try:
            with friend_model_scope(event, snapshot, inbound_identity):
                async with aclosing(original_iter(instance, *args, **kwargs)) as results:
                    async for item in results:
                        if responses or getattr(item, "is_chunk", False):
                            raise PermissionError("friend_model_guard")
                        role = getattr(item, "role", None)
                        if image_state is not None and (role == "err" or (
                                role == "assistant" and (not (getattr(item, "completion_text", "") or "").strip()
                                                         or getattr(item, "tools_call_name", None)))):
                            image_model_failed = True
                            from astrbot.core.provider.entities import LLMResponse
                            item = LLMResponse(role="assistant", completion_text=_friend_image_failure_text("model"))
                        responses.append(item)
            if image_state is not None:
                _record_friend_image_diagnostic(
                    "model", "failure" if image_model_failed or not responses else "success")
        except Exception as error:
            if image_state is not None:
                _record_friend_image_diagnostic("model", "failure", error)
            if (image_state is not None and friend_event_allowed(event)
                    and _friend_inbound_identity(event) == inbound_identity):
                from astrbot.core.provider.entities import LLMResponse
                responses = [LLMResponse(role="assistant", completion_text=_friend_image_failure_text("model", error))]
            else:
                raise
        for item in responses:
            yield item

    @functools.wraps(original_third_party)
    async def third_party_process(stage, event, *args, **kwargs):
        if lifetime["active"] and str(getattr(event, "unified_msg_origin", "")) == FRIEND_UMO:
            event.stop_event()
            return
        async with aclosing(original_third_party(stage, event, *args, **kwargs)) as results:
            async for item in results:
                yield item

    @functools.wraps(original_create)
    async def create(*args, **kwargs):
        grant = _friend_model.get()
        if lifetime["active"] and grant is not None and (args or not friend_model_allowed()
                                  or kwargs.get("model") != "deepseek-flash"
                                  or not _friend_sdk_payload_allowed(kwargs, grant[2])):
            raise PermissionError("friend_model_guard")
        if lifetime["active"] and grant is not None:
            state = _friend_image_events.get(grant[1])
            if state is not None:
                if state["attempted"] or getattr(provider.client, "max_retries", None) != 0:
                    raise PermissionError("friend_model_guard")
                state["attempted"] = True
        return await original_create(*args, **kwargs)

    @functools.wraps(original_transform)
    async def transform(part):
        grant = _friend_model.get()
        if lifetime["active"] and grant is not None:
            if not friend_model_allowed() or not isinstance(part, dict):
                raise PermissionError("friend_model_guard")
            if part.get("type") == "text":
                allowed_texts = []
                for _, content in grant[2]:
                    if isinstance(content, str):
                        allowed_texts.append(content)
                    elif isinstance(content, tuple):
                        allowed_texts.extend(value for kind, value in content if kind == "text")
                if set(part) != {"type", "text"} or part["text"] not in allowed_texts:
                    raise PermissionError("friend_model_guard")
                return part
            if part.get("type") != "image_url" or _friend_image_events.get(grant[1]) is None:
                raise PermissionError("friend_model_guard")
            image_url = part.get("image_url")
            allowed = tuple(value for kind, value in grant[2][-1][1] if kind == "image_url")
            if (set(part) != {"type", "image_url"} or not isinstance(image_url, dict)
                    or set(image_url) != {"url", "id"} or image_url["id"] is not None
                    or image_url["url"] not in allowed):
                raise PermissionError("friend_model_guard")
            # The JPEG is already normalized. Avoid the generic resolver's
            # failure path, which can log the entire data URI.
            return {"type": "image_url", "image_url": {"url": image_url["url"]}}
        return await original_transform(part)

    @functools.wraps(original_recover)
    async def recover(*args, **kwargs):
        grant = _friend_model.get()
        if lifetime["active"] and grant is not None and _friend_image_events.get(grant[1]) is not None:
            # Do not let Provider strip an image, rotate keys or print details.
            raise PermissionError("friend_image_request_failed") from None
        return await original_recover(*args, **kwargs)

    installed_tool = classmethod(execute)
    # Validate all hook targets before changing any of them. The caller installs
    # this last, after other executor/provider wrappers have been registered.
    try:
        waking_stage.process = wake
        executor.execute = installed_tool
        internal_module.build_main_agent = build
        runner._iter_llm_responses = model_iter
        third_party.process = third_party_process
        completions.create = create
        provider._transform_content_part = transform
        provider._handle_api_error = recover
    except BaseException:
        if waking_stage.process is wake:
            waking_stage.process = original_wake
        if executor.__dict__.get("execute") is installed_tool:
            executor.execute = descriptor
        if internal_module.build_main_agent is build:
            internal_module.build_main_agent = original_build
        if runner._iter_llm_responses is model_iter:
            runner._iter_llm_responses = original_iter
        if third_party.process is third_party_process:
            third_party.process = original_third_party
        if completions.create is create:
            completions.create = original_create
        if provider._transform_content_part is transform:
            provider._transform_content_part = original_transform
        if provider._handle_api_error is recover:
            provider._handle_api_error = original_recover
        raise

    def current():
        try:
            if not lifetime["active"] or _friend_scope_valid is not valid_scope:
                return False
            def includes(value, expected):
                for _ in range(20):
                    if value is expected:
                        return True
                    value = getattr(value, "__wrapped__", None)
                    if value is None:
                        break
                return False

            guard = getattr(host, "_reconnect_guard", None)
            outbound = getattr(host, "_group_outbound_restore", None)
            budget = getattr(host, "_group_provider_restore", None)
            provider = getattr(host, "_group_budget_provider", None)
            runtime = getattr(host, "_peak_runtime_handle", None)
            background = getattr(host, "_peak_background_handle", None)
            if (runtime is None or runtime.status().get("ready") is not True
                    or background is None
                    or background.status().get("installed") is not True
                    or background.status().get("missing_targets")
                    or background.status().get("not_current_bindings")):
                return False
            if (not isinstance(guard, tuple) or len(guard) < 9
                    or not includes(guard[0]._convert_handle_message_event, guard[8])
                    or waking_stage.process is not wake or executor.__dict__.get("execute") is not installed_tool
                    or internal_module.build_main_agent is not build or runner._iter_llm_responses is not model_iter
                    or third_party.process is not third_party_process
                    or provider.client.chat.completions is not completions or completions.create is not create
                    or provider._transform_content_part is not transform
                    or provider._handle_api_error is not recover
                    or provider.get_model() != "deepseek-flash"
                    or urlparse(str(provider.provider_config.get("api_base") or "")).hostname != "api.deepseek.com"
                    or urlparse(str(provider.client.base_url)).hostname != "api.deepseek.com"
                    or not guard or not outbound or not budget or provider is None
                    or host.context.get_platform_inst("snowluma-qq") is not guard[0]
                    or host.context.get_provider_by_id("deepseek-flash") is not provider
                    or guard[0].get_client() is not guard[3]
                    or not includes(guard[0].create_event, guard[2])
                    or guard[0].commit_event is not guard[6]
                    or not (
                        includes(guard[3].call_action, getattr(outbound, "wrapped", None))
                        if getattr(outbound, "wrapped", None) is not None
                        else (getattr(guard[3].call_action, "__wrapped__", None) is not None
                              and getattr(getattr(guard[3].call_action, "__wrapped__", None), "__wrapped__", None) is not None)
                    )
                    or set(budget.wrapped) != {"text_chat", "text_chat_stream",
                                              "_apply_provider_specific_request_overrides"}):
                return False
            return all(includes(getattr(provider, name, None), wrapper)
                       for name, wrapper in budget.wrapped.items())
        except Exception:  # A broken readiness check must never open the exception.
            return False

    _guards_current = current
    _friend_scope_valid = valid_scope
    _friend_persona_resolution = "not_run"

    def restore():
        global _guards_current, _friend_scope_valid
        lifetime["active"] = False
        if waking_stage.process is wake:
            waking_stage.process = original_wake
        if executor.__dict__.get("execute") is installed_tool:
            executor.execute = descriptor
        if internal_module.build_main_agent is build:
            internal_module.build_main_agent = original_build
        if runner._iter_llm_responses is model_iter:
            runner._iter_llm_responses = original_iter
        if third_party.process is third_party_process:
            third_party.process = original_third_party
        if completions.create is create:
            completions.create = original_create
        if provider._transform_content_part is transform:
            provider._transform_content_part = original_transform
        if provider._handle_api_error is recover:
            provider._handle_api_error = original_recover
        if _guards_current is current:
            _guards_current = lambda: False
        if _friend_scope_valid is valid_scope:
            _friend_scope_valid = lambda grant: False

    restore.is_current = current
    return restore
