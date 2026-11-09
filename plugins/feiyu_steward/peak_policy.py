"""Runtime guard for the verified DeepSeek flash peak tariff calendar."""

import time
import re
from datetime import datetime

from .admin_info import owner_private
from .policy import OWNER_UMO
from .qq_chat_pause import qq_chat_paused
from .qq_friend_policy import (
    FRIEND,
    FRIEND_UMO,
    SELF,
    friend_event_allowed,
    friend_message_payload_allowed,
    friend_model_allowed,
    friend_send_allowed,
)
from .friend_time_notice import notice_action_allowed, notice_scope_present
from .token_pricing import TZ, _peak


class PeakTariffPaused(PermissionError):
    """The action was blocked locally before any platform request."""


class QQChatPaused(PeakTariffPaused):
    """The independent manual QQ pause blocked an action locally."""


def peak_active(at=None):
    """Return whether the verified tariff marks this instant as peak."""
    return _peak(time.time() if at is None else at) is True


def automatic_peak_paused(at=None):
    """Lazy re-export of the process-wide fail-closed peak gate."""

    from .peak_runtime import automatic_peak_paused as _automatic_peak_paused

    return _automatic_peak_paused(at)


def owner_private_direct_event(event):
    """Lazy re-export of the strict current owner transport check."""

    from .peak_runtime import owner_private_direct_event as _owner_private_direct_event

    return _owner_private_direct_event(event)


def business_paused(event=None, *, automatic=False, at=None):
    from .peak_runtime import business_paused as _business_paused

    return _business_paused(event, automatic=automatic, at=at)


def provider_allowed(session_id=None, event=None, *, automatic=False, at=None):
    from .peak_runtime import provider_allowed as _provider_allowed

    return _provider_allowed(session_id, event, automatic=automatic, at=at)


def outbound_allowed(action, params=None, event=None, *, automatic=False, at=None):
    from .peak_runtime import outbound_allowed as _outbound_allowed

    return _outbound_allowed(action, params, event, automatic=automatic, at=at)


def maintenance_allowed(kind):
    from .peak_runtime import maintenance_allowed as _maintenance_allowed

    return _maintenance_allowed(kind)


def peak_status(at=None):
    """Return only safe tariff and calendar metadata for the current instant."""
    timestamp = time.time() if at is None else at
    value = _peak(timestamp)
    return {
        "now": datetime.fromtimestamp(timestamp, TZ).isoformat(timespec="seconds"),
        "tariff": "peak" if value is True else "off_peak" if value is False else "unknown",
        "calendar": "verified_2026" if value is not None else "unverified",
        "timezone": "Asia/Shanghai",
        "status": "paused" if value is True else "resumed" if value is False else "unknown_paused",
        "automatic_paused": value is not False,
    }


def owner_private_event(event):
    """Identify only a current, transport-verified owner private event."""
    return owner_private_direct_event(event)


def manual_owner_event(event):
    """Exclude scheduler-generated events from verified human requests."""
    return owner_private_direct_event(event)


def manual_bili_request(event):
    """Recognize a specific owner request to browse or watch Bilibili."""
    if not manual_owner_event(event):
        return False
    text = str(getattr(event, "message_str", "") or "").strip()
    return bool(re.search(
        r"^(?:/?bili(?:主动|看番)(?:\s|$)|"
        r"(?:你去|你现在|帮我|帮忙|麻烦|请|去|能不能去|能不能帮|可不可以去|一起|给我|来).{0,12}"
        r"(?:刷|看|浏览|查|分析|找).{0,16}(?:b站|bilibili|小破站|视频|番剧)|"
        r"(?:刷|看|浏览|查|分析|找).{0,16}(?:b站|bilibili|小破站|视频|番剧))",
        text, re.IGNORECASE))


def blocked_qq_event(event, at=None, *, manual_paused=None):
    """Block QQ group and non-owner private events during a QQ pause."""
    origin = str(getattr(event, "unified_msg_origin", ""))
    manual = qq_chat_paused() if manual_paused is None else manual_paused
    if origin == FRIEND_UMO:
        return not friend_event_allowed(event, at)
    return ((manual or peak_active(at)) and origin.startswith("snowluma-qq:")
            and not owner_private_event(event))


def blocked_qq_session(session, at=None, *, manual_paused=None):
    """Apply the same pause to subsequent provider rounds for a QQ session."""
    origin = str(session or "")
    manual = qq_chat_paused() if manual_paused is None else manual_paused
    if origin == FRIEND_UMO:
        return not friend_model_allowed(at)
    return (manual or peak_active(at)) and origin.startswith("snowluma-qq:") and origin != OWNER_UMO


def blocked_qq_action(action, params, at=None, *, manual_paused=None):
    """Block message sends except verified owner's private destination."""
    if notice_scope_present():
        return not notice_action_allowed(action, params)
    manual = qq_chat_paused() if manual_paused is None else manual_paused
    peak = peak_active(at)
    action = str(action).lower().lstrip("./_")
    for suffix in ("_async", "_rate_limited"):
        if action.endswith(suffix):
            action = action[:-len(suffix)]
    params = params if isinstance(params, dict) else {}
    recipient = str(params.get("user_id") or "")
    context = params.get("context") if action == "handle_quick_operation" else None
    context_recipient = str(context.get("user_id") or "") if isinstance(context, dict) else ""
    friend_restricted = FRIEND in {recipient, context_recipient}
    if friend_restricted:
        if params.get("self_id") is not None and str(params["self_id"]) != SELF:
            return True
        if action == "handle_quick_operation":
            operation = params.get("operation")
            if (context_recipient != FRIEND or not isinstance(operation, dict)
                    or not operation.get("reply")
                    or not friend_message_payload_allowed(operation.get("reply"))
                    or set(operation) - {"reply", "auto_escape", "at_sender"}):
                return True
            if ("auto_escape" in operation and not isinstance(operation["auto_escape"], bool)
                    or "at_sender" in operation and not isinstance(operation["at_sender"], bool)):
                return True
            if (context.get("self_id") is not None
                    and str(context["self_id"]) != SELF):
                return True
            if context.get("group_id") or str(context.get("message_type") or context.get("type") or "").lower() == "group":
                return True
            return not friend_send_allowed(at)
        kind = str(params.get("message_type") or params.get("type") or "").lower()
        if action in {"send_private_msg", "send_msg", "send_message"}:
            if params.get("group_id") or kind == "group":
                return True
            if not friend_message_payload_allowed(params.get("message")):
                return True
            return not friend_send_allowed(at)
        # The exception is ordinary text chat only: no files, forwards, pokes,
        # history reads, or any other direct action may target this friend.
        return True
    if not manual and not peak:
        return False
    if action == "handle_quick_operation":
        operation = params.get("operation")
        context = params.get("context")
        if not isinstance(operation, dict) or not operation.get("reply"):
            return False
        return (not isinstance(context, dict)
                or blocked_qq_action("send_msg", context, at, manual_paused=manual))
    # Qzone publication has an independent authorization and is not QQ chat.
    if manual and not peak and action.startswith(("send_qzone", "send_qqzone", "upload_qzone")):
        return False
    kind = str(params.get("message_type") or params.get("type") or "").lower()
    targeted_upload = (action.startswith(("upload_group_", "upload_private_"))
                       or (action.startswith("upload") and
                           (params.get("group_id") or params.get("user_id") or kind in {"group", "private"})))
    if manual and (targeted_upload or action in {"group_poke", "friend_poke"}):
        if (action == "group_poke" or "group" in action or kind == "group"
                or params.get("group_id")):
            return True
        return str(params.get("user_id") or "") != OWNER_UMO.rsplit(":", 1)[-1]
    if not action.startswith("send"):
        return False
    if action.startswith("send_group_"):
        return True
    if action.startswith("send_private_"):
        if kind == "group" or (manual and params.get("group_id")):
            return True
        return str(params.get("user_id") or "") != OWNER_UMO.rsplit(":", 1)[-1]
    if "group" in action or kind == "group" or params.get("group_id"):
        return True
    return str(params.get("user_id") or "") != OWNER_UMO.rsplit(":", 1)[-1]
