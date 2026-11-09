"""Shared peak runtime gate for the owner-only high-tariff mode.

The older peak helpers protect the QQ adapter and the group provider wrapper.
This module supplies the process-wide boundary.  It deliberately keeps
the owner grant in a task-local ``ContextVar``: a session id, an owner-looking
cron event, or a copied prompt cannot grant the high-peak exception.

``install_peak_runtime`` installs reversible
hooks on the AstrBot pipeline, loaded chat providers, tool executor, native
message tool, cron send path, and loaded platform clients.  It does not start a
service, call a model, or send a message while installing.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import math
import re
import time
from collections.abc import MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from . import heybox_peak
from .native_private_integration import _audited_capture_original
from .peak_policy import PeakTariffPaused
from .friend_time_notice import notice_action_allowed, notice_scope_present
from .policy import OWNER_UMO
from .token_pricing import TZ, _peak

OWNER = OWNER_UMO.rsplit(":", 1)[-1]
SELF = "0"
_MESSAGE_ID = re.compile(r"-?[1-9]\d{0,23}\Z")


class PeakRuntimePaused(PeakTariffPaused):
    """A business call was stopped before an external request."""


class OwnerScopeRequired(PermissionError):
    """A peak exception was requested without a verified live owner event."""


@dataclass
class OwnerScope:
    """The minimum identity needed to bind a tool/provider call to one event."""

    event: Any
    source_id: str
    origin: str = OWNER_UMO
    # ContextVars copy the reference into child tasks.  A mutable tombstone
    # lets the parent scope revoke that grant when the event/tool lifetime
    # ends, so a background task cannot retain an owner exemption forever.
    active: bool = True
    # ``owner_tool_scope`` keeps the historical event-bound shape.  The
    # dated extra-comic lane has no QQ event to trust: it is bound to the
    # exact host capability and the task that owns it instead.  Keeping the
    # lane tag on the scope lets the common provider/transport guards use the
    # existing ``owner_scope_active`` boundary without accepting an owner id
    # or a copied string as authorization.
    kind: str = "owner"
    purpose: str | None = None
    host: Any | None = None
    capability: Any | None = None
    task: Any | None = None


@dataclass
class _OwnerExtraScope(OwnerScope):
    """Type marker that cannot be edited into an ordinary owner scope."""


_OWNER_SCOPE: ContextVar[OwnerScope | None] = ContextVar(
    "feiyu_peak_owner_scope", default=None
)
_MAINTENANCE_SCOPE: ContextVar[str | None] = ContextVar(
    "feiyu_peak_maintenance_scope", default=None
)
_OWNER_LINEAGE: ContextVar[Any | None] = ContextVar(
    "feiyu_peak_owner_lineage", default=None
)
_OWNER_GRANT_DENIED: ContextVar[bool] = ContextVar(
    "feiyu_peak_owner_grant_denied", default=False
)
_INGRESS_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar(
    "feiyu_peak_owner_ingress_scope", default=None
)
_OWNER_EVENT_GRANT = "_feiyu_peak_owner_direct_grant_v1"
_EXTRA_OWNER_SCOPE_KIND = "owner_extra_comic"
_EXTRA_OWNER_PURPOSE = "owner_extra_ai_friends"


def _cron_type():
    try:
        from astrbot.core.cron.events import CronMessageEvent

        return CronMessageEvent
    except Exception:  # pragma: no cover - optional during offline linting
        return ()


def automatic_peak_paused(at: float | None = None) -> bool:
    """Return whether automatic business work must pause now.

    The calendar is fail-closed for automatic work.  Only a verified off-peak
    value (``False``) permits an automatic call; peak and unknown both pause.
    """

    try:
        value = _peak(time.time() if at is None else at)
    except (TypeError, ValueError, OverflowError):
        return True
    return value is not False


def peak_status(at: float | None = None) -> dict[str, Any]:
    """Return safe tariff metadata without exposing configuration or secrets."""

    timestamp = time.time() if at is None else at
    try:
        value = _peak(timestamp)
    except (TypeError, ValueError, OverflowError):
        value = None
    return {
        "now": datetime.fromtimestamp(timestamp, TZ).isoformat(timespec="seconds"),
        "tariff": "peak" if value is True else "off_peak" if value is False else "unknown",
        "calendar": "verified_2026" if value is not None else "unverified",
        "timezone": "Asia/Shanghai",
        "automatic_paused": value is not False,
    }


def owner_private_direct_event(event: Any) -> bool:
    """Verify a current human owner QQ private message.

    The check intentionally requires the transport-origin fields that are
    present on SnowLuma's production friend-message events.  Cron events,
    synthetic wake events, and owner-looking text without raw transport proof
    never receive the exception.
    """

    if event is None or isinstance(event, _cron_type()):
        return False
    try:
        if str(getattr(event, "unified_msg_origin", "")) != OWNER_UMO:
            return False
        if str(event.get_sender_id()) != OWNER:
            return False
        if str(getattr(event, "get_group_id", lambda: "")() or ""):
            return False
        message = getattr(event, "message_obj", None)
        raw = getattr(message, "raw_message", None)
        if not isinstance(raw, dict) or not raw:
            return False
        if raw.get("post_type") != "message" or raw.get("message_type") != "private":
            return False
        if raw.get("sub_type", "friend") not in {"friend", ""}:
            return False
        if str(raw.get("self_id", "")) != SELF:
            return False
        if str(raw.get("user_id", "")) != OWNER:
            return False
        sender = raw.get("sender")
        if not isinstance(sender, dict) or str(sender.get("user_id", "")) != OWNER:
            return False
        if raw.get("group_id") or str(getattr(message, "group_id", "") or ""):
            return False
        message_id = raw.get("message_id")
        if isinstance(message_id, bool) or not _MESSAGE_ID.fullmatch(str(message_id)):
            return False
        event_message_id = getattr(message, "message_id", None)
        if event_message_id is not None and str(event_message_id) != str(message_id):
            return False
        target = raw.get("target_id")
        if target is not None and str(target) != SELF:
            return False
        stamp = raw.get("time")
        if stamp is None or isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return False
        now = time.time()
        # A copied payload must still be a current transport event.  The
        # short grace period handles clock skew without allowing an old
        # owner-shaped event to become a durable high-peak grant.
        if not now - 600 <= float(stamp) <= now + 300:
            return False
        sequence = raw.get("message_seq")
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0
        ):
            return False
        # A user-supplied cron marker must never upgrade a real-looking event.
        extras = getattr(event, "get_extra", lambda *_: None)
        if isinstance(extras("cron_job"), dict) or isinstance(extras("cron_payload"), dict):
            return False
        return True
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def owner_raw_private_event(raw: Any) -> bool:
    """Verify a OneBot payload before AstrBot builds its message event."""

    try:
        if not isinstance(raw, dict):
            raw = dict(raw)
        if raw.get("post_type") != "message" or raw.get("message_type") != "private":
            return False
        if raw.get("sub_type", "friend") not in {"friend", ""}:
            return False
        if str(raw.get("self_id", "")) != SELF or str(raw.get("user_id", "")) != OWNER:
            return False
        sender = raw.get("sender")
        if not isinstance(sender, dict) or str(sender.get("user_id", "")) != OWNER:
            return False
        if raw.get("group_id"):
            return False
        message_id = raw.get("message_id")
        if isinstance(message_id, bool) or not _MESSAGE_ID.fullmatch(str(message_id)):
            return False
        stamp = raw.get("time")
        if stamp is None or isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return False
        now = time.time()
        return now - 600 <= float(stamp) <= now + 300
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


_INGRESS_READ_ACTIONS = frozenset({"get_private_file_url", "get_msg"})


def _owner_ingress_scope(raw: Any) -> dict[str, Any] | None:
    """Build a narrow pre-pipeline read scope from one verified raw event."""

    if not owner_raw_private_event(raw):
        return None
    try:
        payload = raw if isinstance(raw, dict) else dict(raw)
        message = payload.get("message")
        if not isinstance(message, list):
            message = []
        file_ids = {
            str(segment.get("data", {}).get("file_id"))
            for segment in message
            if isinstance(segment, dict)
            and segment.get("type") == "file"
            and isinstance(segment.get("data"), dict)
            and segment.get("data", {}).get("file_id") is not None
        }
        reply_ids = {
            str(segment.get("data", {}).get("id"))
            for segment in message
            if isinstance(segment, dict)
            and segment.get("type") == "reply"
            and isinstance(segment.get("data"), dict)
            and segment.get("data", {}).get("id") is not None
        }
        return {
            "raw": raw,
            "actions": _INGRESS_READ_ACTIONS,
            "file_ids": frozenset(file_ids),
            "reply_ids": frozenset(reply_ids),
        }
    except (AttributeError, TypeError, ValueError):
        return None


def _ingress_read_allowed(action: Any, params: dict[str, Any] | None = None) -> bool:
    """Allow only reads named by the current owner raw message."""

    scope = _INGRESS_SCOPE.get()
    if not isinstance(scope, dict):
        return False
    name = _normal_action(action)
    params = params or {}
    if name == "get_private_file_url":
        return str(params.get("file_id", "")) in scope.get("file_ids", ())
    if name == "get_msg":
        return str(params.get("message_id", "")) in scope.get("reply_ids", ())
    return False


def _current_async_task() -> asyncio.Task | None:
    """Return the current task, or ``None`` outside a running event loop."""

    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _bound_extra_validator(host: Any):
    """Return only the host's bound extra-capability validator.

    An instance attribute containing a lambda, a free function, or a caller
    supplied identity string is not an authorization boundary.  The stage
    host exposes a real bound method named ``_validate_owner_extra_comic_capability``;
    requiring that binding keeps this lane tied to that host instance.
    """

    try:
        validator = getattr(host, "_validate_owner_extra_comic_capability", None)
    except Exception:
        return None
    if not inspect.ismethod(validator):
        return None
    if getattr(validator, "__self__", None) is not host:
        return None
    function = getattr(validator, "__func__", None)
    if getattr(function, "__name__", "") == "<lambda>":
        return None
    return validator


def _extra_capability_valid(host: Any, capability: Any, task: asyncio.Task | None = None) -> bool:
    """Revalidate one dated extra-comic capability in the current task.

    The host validator rereads the local, dated approval file.  This helper
    adds the runtime boundary that the validator cannot establish on its own:
    capability identity, purpose, current task identity, and a bounded
    monotonic TTL.  It intentionally rejects awaitable validators so a caller
    cannot smuggle an arbitrary coroutine or deferred decision into the
    synchronous context manager.
    """

    if not isinstance(capability, dict) or capability.get("purpose") != _EXTRA_OWNER_PURPOSE:
        return False
    current_task = _current_async_task()
    if current_task is None or capability.get("task") is not current_task:
        return False
    if task is not None and task is not current_task:
        return False
    expires_at = capability.get("expires_at")
    if isinstance(expires_at, bool) or type(expires_at) not in (int, float):
        return False
    try:
        remaining = float(expires_at) - time.monotonic()
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(remaining) or not 0 < remaining <= 3600:
        return False
    try:
        # The main host's validator also checks ``is current``.  Keeping this
        # identity check here prevents a permissive test double or future
        # host implementation from widening the lane to another capability.
        if getattr(host, "_owner_extra_comic_capability", None) is not capability:
            return False
    except Exception:
        return False
    validator = _bound_extra_validator(host)
    if validator is None:
        return False
    try:
        result = validator(capability)
    except Exception:
        return False
    if inspect.isawaitable(result):
        # Do not leave a coroutine created by a bad validator unclosed.  An
        # async validator cannot participate in this synchronous scope.
        close = getattr(result, "close", None)
        if callable(close):
            close()
        return False
    return result is True


def _extra_scope_valid(scope: OwnerScope) -> bool:
    """Revalidate a local extra scope without mutating it.

    ContextVars copy the scope reference into child tasks.  Checking the
    stored task on every read therefore makes an inherited child scope fail
    closed while leaving the parent's scope usable.
    """

    if (
        not isinstance(scope, _OwnerExtraScope)
        or scope.kind != _EXTRA_OWNER_SCOPE_KIND
        or scope.active is not True
    ):
        return False
    current_task = _current_async_task()
    if current_task is None or scope.task is not current_task:
        return False
    return _extra_capability_valid(scope.host, scope.capability, task=scope.task)


def _is_extra_scope(scope: OwnerScope) -> bool:
    """Identify the extra lane even if a caller edits its display tag."""

    return bool(
        isinstance(scope, _OwnerExtraScope)
        or scope.kind == _EXTRA_OWNER_SCOPE_KIND
        or scope.purpose == _EXTRA_OWNER_PURPOSE
        or scope.host is not None
        or scope.capability is not None
        or scope.task is not None
    )


def owner_scope_active(event: Any | None = None) -> bool:
    """Return whether the current task carries the exact verified owner grant."""

    scope = _OWNER_SCOPE.get()
    if scope is None or scope.active is not True:
        return False
    if _is_extra_scope(scope):
        if _OWNER_GRANT_DENIED.get() or event is not None:
            return False
        return _extra_scope_valid(scope)
    if event is not None and scope.event is not event:
        return False
    return True


def current_owner_scope() -> OwnerScope | None:
    """Return the task-local grant for hook consumers."""

    scope = _OWNER_SCOPE.get()
    if scope is None or scope.active is not True:
        return None
    if _is_extra_scope(scope):
        if _OWNER_GRANT_DENIED.get() or not _extra_scope_valid(scope):
            return None
    return scope


def owner_event_granted(event: Any | None) -> bool:
    """Return whether ``event`` was bound by a verified owner scope.

    The marker is deliberately attached only by :func:`owner_tool_scope`.
    It is used by the native reply route after a requested asynchronous task
    outlives the ContextVar scope; the mutable scope itself is still revoked.
    """

    marker = getattr(event, _OWNER_EVENT_GRANT, None)
    if (
        _OWNER_GRANT_DENIED.get()
        or not isinstance(marker, dict)
        or marker.get("event") is not event
        or _OWNER_LINEAGE.get() is not marker
    ):
        return False
    message = getattr(event, "message_obj", None)
    raw = getattr(message, "raw_message", {}) or {}
    return marker.get("source_id") == str(raw.get("message_id"))


@contextlib.contextmanager
def owner_tool_scope(event: Any):
    """Bind one verified event for its synchronous/asynchronous tool lifetime."""

    if not owner_private_direct_event(event):
        raise OwnerScopeRequired("verified_owner_private_event_required")
    message = getattr(event, "message_obj", None)
    raw = getattr(message, "raw_message", {}) or {}
    scope = OwnerScope(event=event, source_id=str(raw.get("message_id")))
    # The in-memory event marker survives an explicitly requested async
    # result, while ``scope.active`` below still revokes the inherited grant.
    try:
        setattr(
            event,
            _OWNER_EVENT_GRANT,
            {"event": event, "source_id": scope.source_id},
        )
    except Exception:
        raise OwnerScopeRequired("owner_event_marker_unavailable")
    token = _OWNER_SCOPE.set(scope)
    lineage_token = _OWNER_LINEAGE.set(getattr(event, _OWNER_EVENT_GRANT))
    try:
        yield scope
    finally:
        scope.active = False
        _OWNER_LINEAGE.reset(lineage_token)
        _OWNER_SCOPE.reset(token)


@contextlib.contextmanager
def owner_extra_comic_scope(host: Any, capability: Any):
    """Bind one dated owner extra-comic capability to this async task.

    ``host`` must expose the stage host's bound
    ``_validate_owner_extra_comic_capability`` method.  Validation is run on
    entry and again by every ``owner_scope_active``/``current_owner_scope``
    read, so a changed local approval, revoked capability, expired TTL, or
    task switch closes the lane immediately.  ContextVars are intentionally
    not treated as inheritance: a child task sees the copied object but fails
    the stored-task check.
    """

    inherited = _OWNER_SCOPE.get()
    if (
        _OWNER_GRANT_DENIED.get()
        or (
            inherited is not None
            and _is_extra_scope(inherited)
            and inherited.capability is capability
            and inherited.task is not _current_async_task()
        )
        or not _extra_capability_valid(host, capability)
    ):
        raise OwnerScopeRequired("verified_owner_extra_comic_capability_required")
    task = _current_async_task()
    # _extra_capability_valid above guarantees a running task, but keep this
    # guard local to the constructor so a future refactor cannot bind None.
    if task is None:
        raise OwnerScopeRequired("owner_extra_scope_requires_async_task")
    scope = _OwnerExtraScope(
        event=None,
        source_id=str(capability.get("authorization_id", "")),
        origin=OWNER_UMO,
        kind=_EXTRA_OWNER_SCOPE_KIND,
        purpose=_EXTRA_OWNER_PURPOSE,
        host=host,
        capability=capability,
        task=task,
    )
    token = _OWNER_SCOPE.set(scope)
    # An extra scope is not a message event and must never reuse an event
    # marker from an outer owner-tool scope.
    lineage_token = _OWNER_LINEAGE.set(None)
    try:
        yield scope
    finally:
        scope.active = False
        _OWNER_LINEAGE.reset(lineage_token)
        _OWNER_SCOPE.reset(token)


@dataclass
class _OwnerProactiveScope:
    host: Any
    capability: dict
    phase: str
    task: Any
    active: bool = True


_OWNER_PROACTIVE_SCOPE: ContextVar[_OwnerProactiveScope | None] = ContextVar(
    "feiyu_peak_owner_proactive_scope", default=None
)


def _owner_proactive_valid(scope: Any, host: Any, phase: str) -> bool:
    """Recheck the independent background capability at every boundary."""
    try:
        task = _current_async_task()
        if (not isinstance(scope, _OwnerProactiveScope) or not scope.active
                or scope.host is not host or scope.phase != phase
                or scope.task is not task or task is None or task.done()
                or task.cancelling() or _peak(time.time()) is not True):
            return False
        cap = scope.capability
        validator = getattr(host, "_validate_owner_proactive_capability", None)
        return bool(isinstance(cap, dict) and cap.get("active") is True
                    and cap.get("task") is task and cap.get("phase") == phase
                    and cap.get("purpose") == "peak_owner_proactive_chat_v1"
                    and cap.get("origin") == OWNER_UMO
                    and getattr(host, "_owner_peak_capability", None) is cap
                    and getattr(host, "_owner_peak_loop_task", None) is task
                    and inspect.ismethod(validator) and validator.__self__ is host
                    and validator(cap, phase) is True)
    except Exception:
        return False


@contextlib.contextmanager
def owner_proactive_scope(host: Any, capability: Any, phase: str):
    """Grant one narrow model or owner-text-send phase, never owner tools."""
    task = _current_async_task()
    if phase not in {"model", "send"} or not isinstance(capability, dict):
        raise OwnerScopeRequired("verified_owner_proactive_capability_required")
    scope = _OwnerProactiveScope(host, capability, phase, task)
    if not _owner_proactive_valid(scope, host, phase):
        raise OwnerScopeRequired("verified_owner_proactive_capability_required")
    token = _OWNER_PROACTIVE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        scope.active = False
        _OWNER_PROACTIVE_SCOPE.reset(token)


def _owner_proactive_claim(scope, counter, claimed):
    """Spend a single invocation, while allowing the same await to recheck."""
    cap = scope.capability
    if claimed[0] is None:
        if type(cap.get(counter)) is not int or cap[counter] != 0:
            return False
        cap[counter] = 1
        claimed[0] = scope
    return claimed[0] is scope and type(cap.get(counter)) is int and cap[counter] == 1


def _owner_proactive_provider_allowed(host, provider, name, args, kwargs, claimed):
    scope = _OWNER_PROACTIVE_SCOPE.get()
    if not _owner_proactive_valid(scope, host, "model"):
        return False
    cap = scope.capability
    try:
        keys = {"prompt", "contexts", "image_urls", "audio_urls", "func_tool",
                "model", "system_prompt", "request_max_retries"}
        if (args or set(kwargs) != keys or name != "text_chat"
                or cap.get("provider") is not provider
                or provider.get_model() != "deepseek-flash"
                or kwargs.get("model") != "deepseek-flash"
                or kwargs.get("request_max_retries") != 1
                or kwargs.get("contexts") != [] or kwargs.get("image_urls") != []
                or kwargs.get("audio_urls") != [] or kwargs.get("func_tool") is not None
                or not isinstance(cap.get("prompt"), str) or not cap["prompt"]
                or len(cap["prompt"]) > 20000 or kwargs.get("prompt") != cap["prompt"]
                or not isinstance(cap.get("system_prompt"), str) or not cap["system_prompt"]
                or len(cap["system_prompt"]) > 16000
                or kwargs.get("system_prompt") != cap["system_prompt"]
                or type(provider.client.max_retries) is not int
                or provider.client.max_retries != 0):
            return False
        return _owner_proactive_claim(scope, "model_calls", claimed)
    except Exception:
        return False


def _owner_proactive_sdk_allowed(host, provider, parent, name, args, kwargs, claimed):
    scope = _OWNER_PROACTIVE_SCOPE.get()
    if not _owner_proactive_valid(scope, host, "model"):
        return False
    cap = scope.capability
    try:
        # Check the exact serialized conversation and reject any tool,
        # multimodal, alternate model, or extra-body injection.
        allowed = {"model", "messages", "stream", "extra_body", "extra_headers", "max_tokens"}
        if (args or set(kwargs) - allowed or parent is not cap.get("sdk_parent")
                or cap.get("provider") is not provider or name != "create"
                or parent is not provider.client.chat.completions
                or type(provider.client.max_retries) is not int
                or provider.client.max_retries != 0 or provider.get_model() != "deepseek-flash"
                or kwargs.get("model") != "deepseek-flash" or kwargs.get("stream") is not False
                or kwargs.get("messages") != [
                    {"role": "system", "content": cap.get("system_prompt")},
                    {"role": "user", "content": cap.get("prompt")},
                ] or kwargs.get("extra_body") not in (None, {})
                or kwargs.get("extra_headers") not in (None, {})
                or cap.get("model_calls") != 1):
            return False
        maximum = kwargs.get("max_tokens", 1024)
        if type(maximum) is not int or not 0 < maximum <= 1024:
            return False
        kwargs["max_tokens"] = maximum
        return _owner_proactive_claim(scope, "sdk_calls", claimed)
    except Exception:
        return False


def _owner_proactive_send_allowed(host, client, action, args, params):
    scope = _OWNER_PROACTIVE_SCOPE.get()
    if not _owner_proactive_valid(scope, host, "send"):
        return False
    cap = scope.capability
    try:
        message = params.get("message")
        text = cap.get("expected_text")
        if (args or action != "send_private_msg" or client is not cap.get("client")
                or set(params) - {"user_id", "message", "self_id"}
                or str(params.get("user_id", "")) != OWNER
                or ("self_id" in params and str(params["self_id"]) != SELF)
                or not isinstance(text, str) or not text.strip() or len(text) > 4096
                or message != [{"type": "text", "data": {"text": text}}]
                or cap.get("model_calls") != 1 or cap.get("sdk_calls") != 1
                or type(cap.get("send_calls")) is not int or cap["send_calls"] != 0):
            return False
        cap["send_calls"] = 1
        return True
    except Exception:
        return False


@contextlib.contextmanager
def automatic_scope():
    """Clear any inherited owner grant around scheduler/background work."""

    token = _OWNER_SCOPE.set(None)
    lineage_token = _OWNER_LINEAGE.set(None)
    denied_token = _OWNER_GRANT_DENIED.set(True)
    try:
        yield
    finally:
        _OWNER_GRANT_DENIED.reset(denied_token)
        _OWNER_LINEAGE.reset(lineage_token)
        _OWNER_SCOPE.reset(token)


@contextlib.asynccontextmanager
async def same_task_deadline(seconds: Any):
    """Apply a cancellable deadline without creating a child task.

    ``asyncio.wait_for`` wraps its awaitable in another task in common
    versions of Python, which would deliberately lose an extra-comic scope's
    current-task binding.  This helper schedules cancellation of the current
    task instead.  Only the cancellation issued by this helper becomes a
    ``TimeoutError``; an external cancellation remains ``CancelledError``.
    """

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError("seconds_must_be_number")
    if not math.isfinite(float(seconds)) or float(seconds) < 0:
        raise ValueError("seconds_must_be_finite_nonnegative")
    task = _current_async_task()
    if task is None:
        raise RuntimeError("same_task_deadline_requires_async_task")
    loop = asyncio.get_running_loop()
    baseline_cancelling = getattr(task, "cancelling", lambda: 0)()
    state: dict[str, Any] = {
        "done": False,
        "timed_out": False,
        "cancel_before": None,
        "cancel_after": None,
        "cancel_token": object(),
    }

    def cancel_for_deadline() -> None:
        if state["done"] or state["timed_out"]:
            return
        state["timed_out"] = True
        state["cancel_before"] = getattr(task, "cancelling", lambda: 0)()
        # A private cancellation message lets Python 3.10 distinguish an
        # ordinary external CancelledError even though that version lacks
        # Task.cancelling()/uncancel().
        task.cancel(state["cancel_token"])
        state["cancel_after"] = getattr(task, "cancelling", lambda: 0)()

    handle = loop.call_later(float(seconds), cancel_for_deadline)

    def own_timeout_cancellation(error: BaseException | None = None) -> bool:
        if not state["timed_out"]:
            return False
        if error is not None:
            args = getattr(error, "args", ())
            if not args or args[0] is not state["cancel_token"]:
                return False
        # A cancellation that was already pending, or one added after this
        # helper's cancellation, belongs to the caller and must propagate.
        before = state["cancel_before"]
        after = state["cancel_after"]
        current = getattr(task, "cancelling", lambda: 0)()
        return baseline_cancelling == 0 and before == 0 and current == after

    def clear_own_cancellation() -> None:
        uncancel = getattr(task, "uncancel", None)
        if callable(uncancel):
            # Remove exactly one cancellation, the one issued above.  Never
            # clear a caller's cancellation count.
            uncancel()

    try:
        yield
        if own_timeout_cancellation():
            clear_own_cancellation()
            raise asyncio.TimeoutError("same_task_deadline_expired")
    except asyncio.CancelledError as error:
        if own_timeout_cancellation(error):
            clear_own_cancellation()
            raise asyncio.TimeoutError("same_task_deadline_expired") from None
        raise
    finally:
        state["done"] = True
        handle.cancel()


@contextlib.contextmanager
def maintenance_scope(kind: Any):
    """Bind a narrow non-business maintenance lane for this task.

    Maintenance is explicit and task-local.  Merely naming a read-only OneBot
    action cannot bypass the peak gate.
    """

    normalized = str(kind or "").lower()
    if not maintenance_allowed(normalized):
        raise OwnerScopeRequired("maintenance_scope_not_allowed")
    token = _MAINTENANCE_SCOPE.set(normalized)
    try:
        yield normalized
    finally:
        _MAINTENANCE_SCOPE.reset(token)


def maintenance_scope_active() -> str | None:
    return _MAINTENANCE_SCOPE.get()


def business_paused(
    event: Any | None = None, *, automatic: bool = False, at: float | None = None
) -> bool:
    """Return whether a business call is paused in the current scope."""

    if automatic:
        return automatic_peak_paused(at)
    if owner_scope_active(event):
        return False
    # A non-automatic call without a bound owner event is still business work;
    # at peak it must fail closed rather than trusting a caller-supplied owner id.
    return automatic_peak_paused(at)


def provider_allowed(
    session_id: Any = None,
    event: Any | None = None,
    *,
    automatic: bool = False,
    at: float | None = None,
) -> bool:
    """Gate every provider attempt, including fallbacks and retry rounds."""

    if not automatic_peak_paused(at):
        return True
    if automatic or not owner_scope_active(event):
        return False
    session = str(session_id or "")
    return not session or session == OWNER_UMO


# Actions which are safe to use from an explicitly bound maintenance lane.
# This is intentionally an exact set: broad ``get_*``/``list_*`` prefixes
# include business reads which can trigger a provider or an external API.
_MAINTENANCE_ACTIONS = frozenset(
    {
        "get_status",
        "get_login_info",
    }
)
# The extra comic must not become a general owner QQ-message grant.  Its
# process-wide client hook may perform only the account/session reads needed
# by the already-bound external publication route.  Provider and SDK calls
# remain separately gated by ``provider_allowed`` and are not widened here.
_EXTRA_OWNER_OUTBOUND_ACTIONS = frozenset(
    {
        "get_status",
        "get_login_info",
        "get_cookies",
        "get_uin",
        "get_csrf_token",
    }
)


def _normal_action(action: Any) -> str:
    value = str(action or "").lower().lstrip("./_")
    for suffix in ("_async", "_rate_limited"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value


def outbound_allowed(
    action: Any,
    params: dict[str, Any] | None = None,
    event: Any | None = None,
    *,
    automatic: bool = False,
    at: float | None = None,
) -> bool:
    """Gate all outbound actions, including read-only checks.

    During a peak window only the live owner scope or an explicit, narrow
    maintenance scope may reach the platform client.
    """

    name = _normal_action(action)
    if not automatic_peak_paused(at):
        return True
    # These two probes are required to keep the bridge/reconnect health state
    # accurate and never dispatch business content.
    if name in _MAINTENANCE_ACTIONS:
        return True
    if not automatic and owner_scope_active(event):
        scope = current_owner_scope()
        if getattr(scope, "kind", None) == _EXTRA_OWNER_SCOPE_KIND:
            return name in _EXTRA_OWNER_OUTBOUND_ACTIONS
        return True
    maintenance = maintenance_scope_active()
    return (
        maintenance is not None
        and maintenance_allowed(maintenance)
        and name in _MAINTENANCE_ACTIONS
    )


def cron_delivery_allowed(event: Any, producer: str | None = None, *, at: float | None = None) -> bool:
    """Block AstrBot Cron delivery throughout a peak window.

    CodexBrief completion is delivered by its independent process and does not
    pass through ``CronMessageEvent.send``; trusting metadata here would let a
    synthetic Cron event self-label as an allowed completion notice.
    """

    if not isinstance(event, _cron_type()):
        return True
    return not automatic_peak_paused(at)


def maintenance_allowed(kind: Any) -> bool:
    """Identify non-business upkeep that remains allowed during a peak window."""

    return str(kind or "").lower() in {
        "health", "monitor", "reconnect", "login_recovery", "ledger", "receipt",
        "audit", "status", "codex_brief",
    }


def _raise_peak(reason: str = "automatic_business_paused_during_peak"):
    raise PeakRuntimePaused(reason)


def _owned_wrapper(value: Any, owner: Any) -> bool:
    """Check wrapper identity without trusting copied ``wraps`` markers."""

    state = getattr(value, "_peak_runtime_state", None)
    return (
        isinstance(state, dict)
        and state.get("wrapped") is value
        and state.get("owner") is owner
        and state.get("active", True) is not False
    )


class _ProviderMapProxy(MutableMapping):
    """Proxy a provider manager map and guard providers returned from it.

    AstrBot's model selection can read ``provider_manager.inst_map`` directly,
    bypassing the public context getter.  Intercepting reads closes that late
    provider route while preserving writes and the original mapping object.
    """

    _peak_runtime_provider_map = True

    def __init__(self, original, on_provider):
        self._original = original
        self._on_provider = on_provider
        self._peak_runtime_state = None

    def _touch(self, value):
        if value is not None:
            self._on_provider(value)
        return value

    def __getitem__(self, key):
        return self._touch(self._original[key])

    def __setitem__(self, key, value):
        self._original[key] = value

    def __delitem__(self, key):
        del self._original[key]

    def __iter__(self):
        return iter(self._original)

    def __len__(self):
        return len(self._original)

    def get(self, key, default=None):
        return self._touch(self._original.get(key, default))

    def values(self):
        for value in self._original.values():
            yield self._touch(value)

    def items(self):
        for key, value in self._original.items():
            yield key, self._touch(value)

    def copy(self):
        return self._original.copy()


class PeakRuntimeHandle:
    """Reversible set of runtime hooks installed by ``install_peak_runtime``."""

    def __init__(self, host):
        self.host = host
        self._active = True
        self._adapter_bindings: list[tuple[Any, str, Any, Any]] = []
        self._provider_bindings: list[tuple[Any, str, Any, Any]] = []
        self._sdk_bindings: list[tuple[Any, str, Any, Any]] = []
        self._client_bindings: list[tuple[Any, Any, Any]] = []
        self._context_bindings: list[tuple[Any, str, Any, Any]] = []
        self._manager_bindings: list[tuple[Any, str, Any, Any]] = []
        self._class_bindings: list[tuple[Any, str, Any, Any]] = []
        self.refresh()
        self._install_pipeline_hook()
        self._install_tool_hooks()
        self._install_cron_hook()

    def _wrap_provider(self, provider, name):
        if not self._active:
            return
        method = getattr(provider, name, None)
        if not callable(method) or _owned_wrapper(method, self):
            return
        prior_state = getattr(method, "_peak_runtime_state", None)
        if isinstance(prior_state, dict) and callable(prior_state.get("restore")):
            prior_state["restore"]()
            method = getattr(provider, name, None)
            if not callable(method):
                return
        original = method
        state = {"active": True}

        @functools.wraps(original)
        def guarded(*args, **kwargs):
            # A retired handle can remain below a later observer/group
            # wrapper after a hot reload.  It must become transparent even
            # when its restore callback could not reach the object top.
            if not self._active or state["active"] is not True:
                return original(*args, **kwargs)
            session = kwargs.get("session_id")
            if session is None and len(args) > 1:
                session = args[1]
            claimed = [None]
            def check():
                if _OWNER_PROACTIVE_SCOPE.get() is not None:
                    if not _owner_proactive_provider_allowed(
                            self.host, provider, name, args, kwargs, claimed):
                        _raise_peak("owner_proactive_provider_not_allowed")
                    return
                scope = current_owner_scope()
                event = scope.event if scope else None
                if (not provider_allowed(session, event, automatic=scope is None)
                        and not heybox_peak.provider_allowed(
                            session, provider=provider, method=name, args=args, kwargs=kwargs)):
                    _raise_peak("provider_call_paused_during_peak")

            # Run an early check for synchronous providers and fail before
            # creating an external request.  Awaitables and async generators
            # are checked again when actually consumed, because callers may
            # create them off-peak and await/iterate them later at peak.
            check()
            result = original(*args, **kwargs)
            if inspect.isasyncgen(result):
                async def guarded_stream():
                    iterator = result.__aiter__()
                    try:
                        while True:
                            if self._active and state["active"] is True:
                                check()
                            try:
                                item = await anext(iterator)
                            except StopAsyncIteration:
                                return
                            yield item
                    finally:
                        close = getattr(result, "aclose", None)
                        if callable(close):
                            await close()
                return guarded_stream()
            if inspect.isawaitable(result):
                async def guarded_awaitable():
                    try:
                        if self._active and state["active"] is True:
                            check()
                    except BaseException:
                        close = getattr(result, "close", None)
                        if callable(close):
                            close()
                        raise
                    return await result
                return guarded_awaitable()
            return result

        def restore():
            state["active"] = False
            if getattr(provider, name, None) is guarded:
                setattr(provider, name, original)

        guarded._peak_runtime_owner = True
        guarded._peak_runtime_owner_handle = self
        guarded._peak_runtime_state = {
            "restore": restore, "active": state, "original": original,
            "owner": self, "wrapped": guarded,
        }
        setattr(provider, name, guarded)
        self._provider_bindings.append((provider, name, original, guarded))
        self._wrap_provider_sdk_boundaries(provider)

    def _wrap_provider_sdk_method(self, parent, name, provider, *, comment_model=False):
        if not self._active:
            return
        try:
            original = getattr(parent, name, None)
        except Exception:
            return
        if not callable(original) or _owned_wrapper(original, self):
            return
        prior_state = getattr(original, "_peak_runtime_state", None)
        if isinstance(prior_state, dict) and callable(prior_state.get("restore")):
            prior_state["restore"]()
            original = getattr(parent, name, None)
            if not callable(original):
                return
        lifetime = {"active": True}

        @functools.wraps(original)
        def guarded(*args, **kwargs):
            if not self._active or lifetime["active"] is not True:
                return original(*args, **kwargs)
            claimed = [None]
            def check():
                if _OWNER_PROACTIVE_SCOPE.get() is not None:
                    if not _owner_proactive_sdk_allowed(
                            self.host, provider, parent, name, args, kwargs, claimed):
                        _raise_peak("owner_proactive_sdk_not_allowed")
                    return
                scope = current_owner_scope()
                event = scope.event if scope else None
                if (not provider_allowed(None, event, automatic=scope is None)
                        and not (comment_model and heybox_peak.provider_allowed(
                            None, provider=provider, sdk=True, args=args, kwargs=kwargs))):
                    _raise_peak("provider_request_paused_during_peak")

            check()
            result = original(*args, **kwargs)
            if inspect.isasyncgen(result):
                async def guarded_stream():
                    iterator = result.__aiter__()
                    try:
                        while True:
                            if self._active and lifetime["active"] is True:
                                check()
                            try:
                                item = await anext(iterator)
                            except StopAsyncIteration:
                                return
                            yield item
                    finally:
                        close = getattr(result, "aclose", None)
                        if callable(close):
                            await close()
                return guarded_stream()
            if inspect.isawaitable(result):
                async def guarded_awaitable():
                    try:
                        if self._active and lifetime["active"] is True:
                            check()
                    except BaseException:
                        close = getattr(result, "close", None)
                        if callable(close):
                            close()
                        raise
                    return await result
                return guarded_awaitable()
            return result

        def restore(obj=parent, attr=name, wrapped=guarded, prior=original):
            lifetime["active"] = False
            if getattr(obj, attr, None) is wrapped:
                setattr(obj, attr, prior)

        guarded._peak_runtime_state = {
            "restore": restore, "original": original,
            "owner": self, "wrapped": guarded, "active": lifetime,
        }
        guarded._peak_runtime_owner_handle = self
        try:
            setattr(parent, name, guarded)
        except Exception:
            return
        self._sdk_bindings.append((parent, name, original, guarded))

    def _wrap_provider_sdk_boundaries(self, provider):
        """Guard SDK request methods so in-call retry rounds re-check peak."""

        roots = []
        for root_name in ("client", "_client", "api_client", "llm_client"):
            try:
                root = getattr(provider, root_name, None)
            except Exception:
                root = None
            if root is not None:
                roots.append(root)
        paths = (
            ("chat", "completions", "create"),
            ("responses", "create"),
            ("messages", "create"),
            ("embeddings", "create"),
            ("audio", "speech", "with_streaming_response", "create"),
            ("audio", "transcriptions", "create"),
            ("models", "list"),
            ("generate_content",),
            ("generate_content_async",),
        )
        for root in roots:
            for path in paths:
                parent = root
                try:
                    for part in path[:-1]:
                        parent = getattr(parent, part)
                    self._wrap_provider_sdk_method(
                        parent, path[-1], provider,
                        comment_model=path in {
                            ("chat", "completions", "create"), ("responses", "create"),
                            ("messages", "create"), ("generate_content",),
                            ("generate_content_async",),
                        },
                    )
                except Exception:
                    continue

    def _refresh_providers(self):
        context = getattr(self.host, "context", None)
        providers = []
        getter = getattr(context, "get_all_providers", None)
        if callable(getter):
            try:
                providers.extend(getter() or [])
            except Exception:
                pass
        manager = getattr(context, "provider_manager", None)
        for attr in (
            "provider_insts", "tts_provider_insts", "stt_provider_insts",
            "embedding_provider_insts", "rerank_provider_insts",
        ):
            values = getattr(manager, attr, None)
            if values:
                try:
                    providers.extend(values)
                except TypeError:
                    pass
        for provider in providers:
            for name in ("text_chat", "text_chat_stream"):
                self._wrap_provider(provider, name)

    def _wrap_provider_manager(self):
        context = getattr(self.host, "context", None)
        manager = getattr(context, "provider_manager", None)
        mapping = getattr(manager, "inst_map", None)
        if manager is None or mapping is None:
            return
        if isinstance(mapping, _ProviderMapProxy):
            owner = getattr(mapping, "_peak_runtime_owner_handle", None)
            if owner is self:
                return
            prior_state = getattr(mapping, "_peak_runtime_state", None)
            if isinstance(prior_state, dict) and callable(prior_state.get("restore")):
                prior_state["restore"]()
                mapping = getattr(manager, "inst_map", None)
        if isinstance(mapping, _ProviderMapProxy):
            mapping = mapping._original
        if not hasattr(mapping, "get") or not hasattr(mapping, "__getitem__"):
            return
        proxy = _ProviderMapProxy(mapping, lambda value: self._wrap_provider_instance(value))
        proxy._peak_runtime_owner_handle = self

        def restore(obj=manager, attr="inst_map", wrapped=proxy, prior=mapping):
            if getattr(obj, attr, None) is wrapped:
                setattr(obj, attr, prior)

        proxy._peak_runtime_state = {
            "restore": restore, "owner": self, "wrapped": proxy,
        }
        setattr(manager, "inst_map", proxy)
        self._manager_bindings.append((manager, "inst_map", mapping, proxy))

    def _wrap_provider_instance(self, provider):
        for name in ("text_chat", "text_chat_stream"):
            self._wrap_provider(provider, name)

    def _wrap_context_getters(self):
        context = getattr(self.host, "context", None)
        for name in (
            "get_provider_by_id", "get_all_providers", "get_using_provider",
            "get_using_provider_async",
        ):
            method = getattr(context, name, None)
            if not callable(method) or _owned_wrapper(method, self):
                continue
            prior_state = getattr(method, "_peak_runtime_state", None)
            if isinstance(prior_state, dict) and callable(prior_state.get("restore")):
                prior_state["restore"]()
                method = getattr(context, name, None)
                if not callable(method):
                    continue
            original = method

            @functools.wraps(original)
            def getter(*args, __original=original, __name=name, **kwargs):
                result = __original(*args, **kwargs)
                return self._guard_provider_result(result, all_values=__name == "get_all_providers")

            getter._peak_runtime_owner = True
            getter._peak_runtime_owner_handle = self

            def restore(obj=context, attr=name, wrapped=getter, prior=original):
                if getattr(obj, attr, None) is wrapped:
                    setattr(obj, attr, prior)

            getter._peak_runtime_state = {
                "restore": restore, "original": original,
                "owner": self, "wrapped": getter,
            }
            setattr(context, name, getter)
            self._context_bindings.append((context, name, original, getter))
        self._wrap_provider_manager()

    def _guard_provider_result(self, result, *, all_values=False):
        def touch(value):
            if value is None:
                return value
            if all_values and isinstance(value, (list, tuple, set, frozenset)):
                for provider in value:
                    self._wrap_provider_instance(provider)
            else:
                self._wrap_provider_instance(value)
            return value

        if inspect.isawaitable(result):
            async def wait_for_provider():
                return touch(await result)
            return wait_for_provider()
        return touch(result)

    def _wrap_client(self, client):
        if not self._active:
            return
        method = getattr(client, "call_action", None)
        if not callable(method) or _owned_wrapper(method, self):
            return
        prior_state = getattr(method, "_peak_runtime_state", None)
        if isinstance(prior_state, dict) and callable(prior_state.get("restore")):
            prior_state["restore"]()
            method = getattr(client, "call_action", None)
            if not callable(method):
                return
        original = method
        lifetime = {"active": True}

        @functools.wraps(original)
        async def guarded(action, *args, **kwargs):
            if not self._active or lifetime["active"] is not True:
                return await original(action, *args, **kwargs)
            params = dict(kwargs)
            if notice_scope_present():
                if args or not notice_action_allowed(action, params, client=client, claim=True):
                    _raise_peak("friend_time_notice_outbound_not_allowed")
                return await original(action, *args, **kwargs)
            if _OWNER_PROACTIVE_SCOPE.get() is not None:
                if not _owner_proactive_send_allowed(self.host, client, action, args, params):
                    _raise_peak("owner_proactive_outbound_not_allowed")
                return await original(action, *args, **kwargs)
            scope = current_owner_scope()
            event = scope.event if scope else None
            allowed = outbound_allowed(action, params, event, automatic=scope is None)
            if not allowed and automatic_peak_paused() and _ingress_read_allowed(action, params):
                # AstrBot resolves owner attachments and quoted messages before
                # the pipeline can establish an OwnerScope.  The adapter hook
                # supplies only the exact reads named by that raw event.
                allowed = True
            if not allowed and automatic_peak_paused():
                allowed = await self._native_peak_route_allowed(client, action, params)
            if not allowed:
                _raise_peak("outbound_business_paused_during_peak")
            return await original(action, *args, **kwargs)

        guarded._peak_runtime_owner = True
        guarded._peak_runtime_owner_handle = self

        def restore():
            lifetime["active"] = False
            if getattr(client, "call_action", None) is guarded:
                setattr(client, "call_action", original)

        guarded._peak_runtime_state = {
            "restore": restore, "original": original, "owner": self,
            "wrapped": guarded, "active": lifetime,
        }
        setattr(client, "call_action", guarded)
        self._client_bindings.append((client, original, guarded))

    def _wrap_adapter_method(self, adapter, name):
        method = getattr(adapter, name, None)
        if not callable(method) or _owned_wrapper(method, self):
            return
        prior_state = getattr(method, "_peak_runtime_state", None)
        if isinstance(prior_state, dict) and callable(prior_state.get("restore")):
            prior_state["restore"]()
            method = getattr(adapter, name, None)
            if not callable(method):
                return
        original = method

        @functools.wraps(original)
        async def guarded(*args, **kwargs):
            event = args[0] if args else kwargs.get("event")
            # A nested _convert_handle_message_event call belongs to the same
            # raw ingress.  Keep its narrow scope instead of deriving a new
            # grant from the quoted payload returned by get_msg.
            if _INGRESS_SCOPE.get() is not None:
                return await original(*args, **kwargs)
            ingress = _owner_ingress_scope(event)
            with automatic_scope():
                token = _INGRESS_SCOPE.set(ingress)
                try:
                    return await original(*args, **kwargs)
                finally:
                    _INGRESS_SCOPE.reset(token)

        guarded._peak_runtime_owner = True
        guarded._peak_runtime_owner_handle = self

        def restore(obj=adapter, attr=name, wrapped=guarded, prior=original):
            if getattr(obj, attr, None) is wrapped:
                setattr(obj, attr, prior)

        guarded._peak_runtime_state = {
            "restore": restore, "original": original,
            "owner": self, "wrapped": guarded,
        }
        setattr(adapter, name, guarded)
        self._adapter_bindings.append((adapter, name, original, guarded))

    def _wrap_adapter(self, adapter):
        if adapter is None:
            return
        # convert_message is the public ingress; keep the private conversion
        # hook covered as well for adapters/tests that call it directly.
        for name in ("convert_message", "_convert_handle_message_event"):
            self._wrap_adapter_method(adapter, name)

    def _refresh_adapters(self):
        context = getattr(self.host, "context", None)
        getter = getattr(context, "get_platform_inst", None)
        if not callable(getter):
            return
        seen = set()
        for platform_id in ("snowluma-qq", "aiocqhttp", "qq_official", "qq_official_webhook"):
            try:
                adapter = getter(platform_id)
            except Exception:
                adapter = None
            if adapter is not None and id(adapter) not in seen:
                seen.add(id(adapter))
                self._wrap_adapter(adapter)

    async def _native_peak_route_allowed(self, client, action, params):
        """Allow only a current, already-authorized native operation through.

        Token report continuations intentionally run after the owner scope has
        ended.  They are still safe when the durable manual intent and native
        operation authorizer both approve; destination checks alone never
        grant this exception.
        """

        handle = getattr(self.host, "_native_private_handle", None)
        if handle is None or getattr(handle, "client", None) is not client:
            return False
        try:
            if not handle.is_current():
                return False
            from .native_private_transport import _OPERATION

            operation = _OPERATION.get()
            if operation is None:
                return False
            allowed = handle.authorize(action, dict(params), operation)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            return allowed is True
        except Exception:
            return False

    def _refresh_clients(self):
        context = getattr(self.host, "context", None)
        getter = getattr(context, "get_platform_inst", None)
        if not callable(getter):
            return
        seen = set()
        # The QQ client is the required route.  Other adapters are included
        # when available so automatic public sends cannot switch platforms.
        for platform_id in ("snowluma-qq", "aiocqhttp", "qq_official", "qq_official_webhook"):
            try:
                adapter = getter(platform_id)
                client = adapter.get_client() if adapter else None
            except Exception:
                client = None
            if client is not None and id(client) not in seen:
                seen.add(id(client))
                self._wrap_client(client)

    def refresh(self):
        """Rebind late-loaded/replaced providers and platform clients."""

        if not self._active:
            return self.status()
        self._wrap_context_getters()
        self._refresh_adapters()
        self._refresh_providers()
        self._refresh_clients()
        return self.status()

    def _install_pipeline_hook(self):
        try:
            from astrbot.core.pipeline.scheduler import PipelineScheduler
        except Exception:
            return
        current = getattr(PipelineScheduler, "execute", None)
        old_state = getattr(current, "_peak_runtime_state", None)
        if (
            isinstance(old_state, dict)
            and callable(old_state.get("restore"))
            and not _owned_wrapper(current, self)
        ):
            old_state["restore"]()
            current = getattr(PipelineScheduler, "execute", None)
        if not callable(current) or _owned_wrapper(current, self):
            return
        original = current

        @functools.wraps(original)
        async def execute(scheduler, event):
            if owner_private_direct_event(event):
                with owner_tool_scope(event):
                    return await original(scheduler, event)
            with automatic_scope():
                # Revalidate only a registered native feed event against the
                # durable reservation in its actual scheduler task.  Ordinary
                # events receive no new grant.
                async with heybox_peak.pipeline_scope(event):
                    return await original(scheduler, event)

        def restore():
            if getattr(PipelineScheduler, "execute", None) is execute:
                PipelineScheduler.execute = original

        execute._peak_runtime_owner = True
        execute._peak_runtime_owner_handle = self
        execute._peak_runtime_state = {
            "restore": restore, "original": original, "owner": self,
            "wrapped": execute,
        }
        PipelineScheduler.execute = execute
        self._class_bindings.append((PipelineScheduler, "execute", original, execute))

    def _install_tool_hooks(self):
        try:
            from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
            from astrbot.core.tools.message_tools import SendMessageToUserTool
        except Exception:
            return
        descriptor = FunctionToolExecutor.__dict__.get("execute")
        if isinstance(descriptor, classmethod):
            old_state = getattr(descriptor, "_peak_runtime_state", None)
            if isinstance(old_state, dict) and callable(old_state.get("restore")):
                old_state["restore"]()
                descriptor = FunctionToolExecutor.__dict__.get("execute")
            if isinstance(descriptor, classmethod):
                original_func = descriptor.__func__

                async def guarded_execute(cls, tool, run_context, **kwargs):
                    if _OWNER_PROACTIVE_SCOPE.get() is not None:
                        _raise_peak("owner_proactive_tool_not_allowed")
                    event = getattr(getattr(run_context, "context", None), "event", None)
                    scope = current_owner_scope()
                    bound = event is not None and owner_scope_active(event)
                    if not bound:
                        with automatic_scope():
                            if business_paused(event, automatic=True):
                                _raise_peak("tool_execution_paused_during_peak")
                            async for item in original_func(cls, tool, run_context, **kwargs):
                                yield item
                        return
                    # The pipeline owns the scope.  Do not derive a new grant
                    # from an owner-shaped event passed to the executor.
                    async for item in original_func(cls, tool, run_context, **kwargs):
                        yield item

                guarded_execute._peak_runtime_owner = True
                new_descriptor = classmethod(guarded_execute)

                def restore_executor(
                    obj=FunctionToolExecutor,
                    attr="execute",
                    wrapped=new_descriptor,
                    prior=descriptor,
                ):
                    if obj.__dict__.get(attr) is wrapped:
                        setattr(obj, attr, prior)

                new_descriptor._peak_runtime_state = {
                    "restore": restore_executor, "original": descriptor,
                    "owner": self, "wrapped": new_descriptor,
                }
                FunctionToolExecutor.execute = new_descriptor
                self._class_bindings.append(
                    (FunctionToolExecutor, "execute", descriptor, new_descriptor)
                )

        original_call = SendMessageToUserTool.__dict__.get("call")
        if original_call is not None and callable(original_call):
            old_state = getattr(original_call, "_peak_runtime_state", None)
            if isinstance(old_state, dict) and callable(old_state.get("restore")):
                old_state["restore"]()
                original_call = SendMessageToUserTool.__dict__.get("call")

            @functools.wraps(original_call)
            async def guarded_call(tool, context, **kwargs):
                # A capture can retain this exact closure after a later reload.
                # Only the active generation may enforce its ContextVar scope.
                if not self._active:
                    return await original_call(tool, context, **kwargs)
                if _OWNER_PROACTIVE_SCOPE.get() is not None:
                    _raise_peak("owner_proactive_tool_not_allowed")
                event = getattr(getattr(context, "context", None), "event", None)
                scope = current_owner_scope()
                bound = event is not None and owner_scope_active(event)
                with (contextlib.nullcontext() if bound else automatic_scope()):
                    if not outbound_allowed(
                        "send_message_to_user",
                        {"session": kwargs.get("session")},
                        event if bound else None,
                        automatic=not bound,
                    ):
                        _raise_peak("tool_message_send_paused_during_peak")
                    return await original_call(tool, context, **kwargs)

            guarded_call._peak_runtime_owner = True
            guarded_call._peak_runtime_owner_handle = self

            def restore_call(
                obj=SendMessageToUserTool,
                attr="call",
                wrapped=guarded_call,
                prior=original_call,
            ):
                if obj.__dict__.get(attr) is wrapped:
                    setattr(obj, attr, prior)

            guarded_call._peak_runtime_state = {
                "restore": restore_call, "original": original_call,
                "owner": self, "wrapped": guarded_call,
            }
            SendMessageToUserTool.call = guarded_call
            self._class_bindings.append((SendMessageToUserTool, "call", original_call, guarded_call))

    def _install_cron_hook(self):
        try:
            from astrbot.core.cron.events import CronMessageEvent
        except Exception:
            return
        original = getattr(CronMessageEvent, "send", None)
        if not callable(original) or _owned_wrapper(original, self):
            return
        old_state = getattr(original, "_peak_runtime_state", None)
        if isinstance(old_state, dict) and callable(old_state.get("restore")):
            old_state["restore"]()
            original = getattr(CronMessageEvent, "send", None)
            if not callable(original):
                return

        @functools.wraps(original)
        async def send(event, chain):
            if not cron_delivery_allowed(event):
                _raise_peak("cron_business_delivery_paused_during_peak")
            return await original(event, chain)

        send._peak_runtime_owner = True
        send._peak_runtime_owner_handle = self

        def restore_send(
            obj=CronMessageEvent, attr="send", wrapped=send, prior=original
        ):
            if obj.__dict__.get(attr) is wrapped:
                setattr(obj, attr, prior)

        send._peak_runtime_state = {
            "restore": restore_send, "original": original,
            "owner": self, "wrapped": send,
        }
        CronMessageEvent.send = send
        self._class_bindings.append((CronMessageEvent, "send", original, send))

    def status(self) -> dict[str, Any]:
        """Return non-sensitive hook state for local status pages."""

        def binding_active(obj, name, wrapped, *, class_attr=False):
            state = getattr(wrapped, "_peak_runtime_state", None)
            if isinstance(state, dict) and state.get("active") is not None:
                active = state.get("active")
                if isinstance(active, dict) and active.get("active") is False:
                    return True
            current = obj.__dict__.get(name) if class_attr else getattr(obj, name, None)
            target = getattr(wrapped, "__func__", wrapped)
            seen = set()
            for _ in range(32):
                current = getattr(current, "__func__", current)
                if current is target:
                    return True
                if current is None or id(current) in seen:
                    break
                seen.add(id(current))
                prior = (_audited_capture_original(current)
                         if class_attr and name == "call" else None)
                current = prior if prior is not None else getattr(current, "__wrapped__", None)
            return False

        missing = []
        for kind, bindings, class_attr in (
            ("adapter", self._adapter_bindings, False),
            ("class", self._class_bindings, True),
            ("context", self._context_bindings, False),
            ("manager", self._manager_bindings, False),
            ("provider", self._provider_bindings, False),
            ("sdk", self._sdk_bindings, False),
        ):
            for obj, name, _, wrapped in bindings:
                if not binding_active(obj, name, wrapped, class_attr=class_attr):
                    missing.append(f"{kind}:{name}")
        for obj, _, wrapped in self._client_bindings:
            if not binding_active(obj, "call_action", wrapped):
                missing.append("client:call_action")
        current_bindings = not missing
        native = getattr(self.host, "_native_private_handle", None)
        native_current = (
            True if native is None else bool(getattr(native, "is_current", lambda: False)())
        )
        return {
            **peak_status(),
            "installed": self._active,
            "ready": bool(self._active and current_bindings and native_current),
            "not_current_bindings": missing,
            "native_current": native_current,
            "adapter_bindings": len(self._adapter_bindings),
            "provider_bindings": len(self._provider_bindings),
            "sdk_bindings": len(self._sdk_bindings),
            "client_bindings": len(self._client_bindings),
            "context_bindings": len(self._context_bindings),
            "manager_bindings": len(self._manager_bindings),
            "class_bindings": len(self._class_bindings),
        }

    def restore(self):
        """Restore only wrappers still owned by this handle."""

        if not self._active:
            return
        self._active = False
        for obj, name, original, wrapped in reversed(self._adapter_bindings):
            if getattr(obj, name, None) is wrapped:
                setattr(obj, name, original)
        for obj, name, original, wrapped in reversed(self._class_bindings):
            if obj.__dict__.get(name) is wrapped:
                setattr(obj, name, original)
        for obj, name, original, wrapped in reversed(self._context_bindings):
            if getattr(obj, name, None) is wrapped:
                setattr(obj, name, original)
        for obj, name, original, wrapped in reversed(self._manager_bindings):
            if getattr(obj, name, None) is wrapped:
                setattr(obj, name, original)
        for obj, original, wrapped in reversed(self._client_bindings):
            if getattr(obj, "call_action", None) is wrapped:
                obj.call_action = original
        for obj, name, original, wrapped in reversed(self._sdk_bindings):
            if getattr(obj, name, None) is wrapped:
                setattr(obj, name, original)
        for obj, name, original, wrapped in reversed(self._provider_bindings):
            if getattr(obj, name, None) is wrapped:
                setattr(obj, name, original)


def install_peak_runtime(host) -> PeakRuntimeHandle:
    """Install the reversible process-wide peak hooks for one steward host."""

    return PeakRuntimeHandle(host)


__all__ = [
    "PeakRuntimePaused", "OwnerScopeRequired", "OwnerScope", "automatic_peak_paused",
    "peak_status", "owner_private_direct_event", "owner_scope_active",
    "current_owner_scope", "owner_event_granted", "owner_tool_scope",
    "owner_extra_comic_scope", "owner_proactive_scope", "automatic_scope", "same_task_deadline",
    "maintenance_scope", "maintenance_scope_active",
    "business_paused",
    "provider_allowed", "outbound_allowed", "cron_delivery_allowed",
    "maintenance_allowed", "PeakRuntimeHandle", "install_peak_runtime",
]
