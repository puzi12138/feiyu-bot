"""Explicit steward wiring for durable native owner-private plain text."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import sys
import time
import types
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .bill_display_policy import display_bill_tool_messages

from .native_private_transport import (
    OWNER,
    OWNER_UMO,
    NativeDeliveryUnknown,
    NativeOperationStore,
    NativePrivateTransport,
    NativeTransportError,
    TrustedNativeClient,
    install_native_private_transport,
    native_delivery,
    owner_reply_operation,
    producer_operation,
)

# A copied async context in another Task must never bypass the lane lock.
_ACTIVE_DELIVERY = ContextVar("steward_native_active_delivery", default=None)

_IMAGE_PROGRESS_TEXT = {
    "preparing": "正在准备这次画图请求。",
    "queued": "正在等待当前图片流程的前一步处理完成。",
    "waiting_external": "正在准备提交或等待生图服务结果。",
    "validating": "正在检查生图流程返回的结果。",
    "saving": "正在整理图片结果。",
    "delivering": "正在准备发送已取得的图片。",
    "result_unknown": "生图结果尚未确认，不会自动重复提交。",
}


def _audited_capture_original(function):
    """Inspect the live capture or an identical retired reload generation.

    Retired captures are accepted only when their own old module/class still
    owns the exact nested code and every code structure field equals the
    current audited implementation. This only traces an existing delegation;
    it never replaces or executes a send wrapper.
    """
    module_name = "data.plugins.astrbot_plugin_private_companion.proactive_message"
    peer = sys.modules.get(module_name)
    if type(peer) is not types.ModuleType:
        return None
    capture = getattr(getattr(peer, "ProactiveMessageMixin", None),
                      "_capture_framework_send_message_calls", None)
    if type(capture) is not types.FunctionType:
        return None
    code = getattr(capture, "__code__", None)
    expected = next((item for item in getattr(code, "co_consts", ())
                     if isinstance(item, types.CodeType)
                     and item.co_name == "_intercept_call"), None)
    if type(function) is not types.FunctionType or expected is None:
        return None
    cells = dict(zip(function.__code__.co_freevars, function.__closure__ or ()))
    try:
        prior = cells["original_call"].cell_contents
        active = cells["capture_active"].cell_contents
    except (KeyError, ValueError):
        return None
    if type(active) is not bool or not callable(prior) or prior is function:
        return None
    live_globals = peer.__dict__
    if function.__code__ is expected and function.__globals__ is live_globals:
        return prior

    # An older active capture may still intercept or terminate a tool call.
    # Only a retired capture can be traced across a replaced plugin module.
    old_globals = function.__globals__
    if active is not False or old_globals is live_globals:
        return None
    if (old_globals.get("__name__") != module_name
            or old_globals.get("__package__") != live_globals.get("__package__")
            or old_globals.get("__file__") != live_globals.get("__file__")
            or old_globals.get("__builtins__") is not live_globals.get("__builtins__")):
        return None
    # Functions retain their builtins dictionary at creation even if globals'
    # __builtins__ entry is later replaced. Check the actual retained mapping.
    if (function.__builtins__ is not capture.__builtins__
            or function.__builtins__.get("getattr") is not getattr
            or function.__builtins__.get("str") is not str):
        return None
    # The inactive implementation still resolves getattr/str before delegation.
    # Reject a retained globals dictionary that shadows either built-in.
    if (old_globals.get("getattr", getattr) is not getattr
            or old_globals.get("str", str) is not str):
        return None
    old_class = old_globals.get("ProactiveMessageMixin")
    if type(old_class) is not type or old_class.__module__ != module_name:
        return None
    old_capture = old_class.__dict__.get("_capture_framework_send_message_calls")
    if (type(old_capture) is not types.FunctionType
            or old_capture.__globals__ is not old_globals):
        return None
    old_expected = next((item for item in old_capture.__code__.co_consts
                         if isinstance(item, types.CodeType)
                         and item.co_name == "_intercept_call"), None)
    if old_expected is not function.__code__:
        return None
    # CodeType equality omits some fields, while serialization reference flags
    # can vary with object retention. Compare immutable fields and literal
    # constants explicitly, including stack size and exception/source tables.
    def same_constant(left, right, depth=0):
        if depth > 32 or type(left) is not type(right):
            return False
        if type(left) is types.CodeType:
            fields = (
                "co_argcount", "co_posonlyargcount", "co_kwonlyargcount",
                "co_nlocals", "co_stacksize", "co_flags", "co_code",
                "co_names", "co_varnames", "co_freevars", "co_cellvars",
                "co_name", "co_qualname", "co_filename", "co_firstlineno",
                "co_linetable", "co_exceptiontable",
            )
            return (all(getattr(left, name) == getattr(right, name) for name in fields)
                    and same_constant(left.co_consts, right.co_consts, depth + 1))
        if type(left) is tuple:
            return (len(left) == len(right)
                    and all(same_constant(a, b, depth + 1) for a, b in zip(left, right)))
        if type(left) in (type(None), bool, int, float, complex, str, bytes):
            return left == right
        # The audited function has no other literal constant types.
        return False

    if not same_constant(old_expected, expected):
        return None
    return prior


class StewardNativeHandle:
    """Own reversible event/tool wrappers and the mandatory permission path."""

    def __init__(
        self, host, adapter, *, config_path, store_path, enabled, enabled_media=False
    ):
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.message.components import Image, Plain
        from astrbot.core.tools.message_tools import SendMessageToUserTool

        self.host = host
        self.adapter = adapter
        self.client = adapter.get_client()
        self._plain = Plain
        self._image = Image
        self._cron_class = CronMessageEvent
        self._tool_class = SendMessageToUserTool
        self._permit = ContextVar("steward_native_source_permit", default=None)
        self._bindings = []
        self._retired = False
        media_spool = None
        if enabled_media:
            from .native_media_spool import NativeMediaSpool

            config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
            media_spool = NativeMediaSpool(Path(config["receipt_dir"]) / "media-spool")
        self.transport = NativePrivateTransport(
            TrustedNativeClient(Path(config_path)),
            NativeOperationStore(Path(store_path)),
            enabled=enabled,
            enabled_media=enabled_media,
            media_spool=media_spool,
            authorize=self.authorize,
        )
        self._restore_transport = install_native_private_transport(
            self.client, self.transport
        )
        original_create = adapter.create_event

        @functools.wraps(original_create)
        def create_event(message):
            event = original_create(message)
            if self._retired:
                return event
            try:
                owner_reply_operation(event, slot="event/0")
            except NativeTransportError:
                return event
            original_send = event.send
            progress_lock = asyncio.Lock()
            progress_attempts = set()
            last_progress_at = None

            async def feiyu_send_image_progress(
                *, task_id, index, stage, elapsed_seconds
            ):
                """Send one bounded, owner-authorized progress update.

                Args:
                    task_id: Fixed lowercase hexadecimal display identifier.
                    index: Zero-based update index, limited to four per event.
                    stage: One of the predefined observable stages.
                    elapsed_seconds: Whole seconds elapsed in this image task.

                Returns:
                    True only after the native operation is confirmed.

                Raises:
                    NativeTransportError: Authorization, bounds or routing failed.
                """
                nonlocal last_progress_at
                if (
                    not isinstance(task_id, str)
                    or not re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{32}", task_id)
                    or isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < 4
                    or not isinstance(stage, str)
                    or stage not in _IMAGE_PROGRESS_TEXT
                    or isinstance(elapsed_seconds, bool)
                    or not isinstance(elapsed_seconds, int)
                    or not 0 <= elapsed_seconds <= 86400
                    or (index > 0 and elapsed_seconds < 25)
                ):
                    raise NativeTransportError("invalid_image_progress")
                async with progress_lock:
                    from .resource_guard import owner_photo_timeout_budget

                    if (
                        self._retired
                        or getattr(self.host, "_native_private_handle", None) is not self
                        or not self.is_current()
                        or not self.transport.enabled
                        or owner_photo_timeout_budget(event) is None
                    ):
                        raise NativeTransportError("image_progress_authorization_required")
                    operation = owner_reply_operation(event, slot=f"progress/{index}")
                    now = time.monotonic()
                    minimum_interval = 25 if index == 1 else 60
                    if (
                        index != len(progress_attempts)
                        or (
                            last_progress_at is not None
                            and now - last_progress_at < minimum_interval
                        )
                    ):
                        raise NativeTransportError("image_progress_rate_limited")
                    progress_attempts.add(index)
                    last_progress_at = now
                    text = (
                        f"开始准备画图，稍等一下。（任务 {task_id}）"
                        if index == 0
                        else f"{_IMAGE_PROGRESS_TEXT[stage]}\n"
                        f"已等待约 {elapsed_seconds} 秒，任务 {task_id}。"
                    )
                    token = self._permit.set((operation, event, asyncio.current_task()))
                    try:
                        with native_delivery(operation):
                            await self.client.call_action(
                                "send_private_msg", user_id=OWNER, message=text
                            )
                        if self.transport.store.state(operation) != "confirmed":
                            raise NativeDeliveryUnknown(
                                "native_delivery_unknown_do_not_resend"
                            )
                        return True
                    finally:
                        self._permit.reset(token)

            @functools.wraps(original_send)
            async def send(chain):
                return await self.deliver(
                    event, "event", chain, lambda: original_send(chain)
                )

            event.send = send
            event.feiyu_send_image_progress = feiyu_send_image_progress
            return event

        adapter.create_event = create_event
        self._bindings.append((adapter, "create_event", original_create, create_event))
        original_tool = SendMessageToUserTool.call

        @functools.wraps(original_tool)
        async def tool_call(tool, context, **kwargs):
            event = getattr(getattr(context, "context", None), "event", None)
            current = str(getattr(event, "unified_msg_origin", ""))
            target = str(kwargs.get("session") or current)
            messages = kwargs.get("messages")
            if (current == OWNER_UMO
                    and getattr(event, "get_extra", lambda *_: None)("feiyu_token_report_display")):
                messages = display_bill_tool_messages(messages)
                kwargs = {**kwargs, "messages": messages}
            if current != OWNER_UMO or target != OWNER_UMO:
                return await original_tool(tool, context, **kwargs)
            if (
                self.transport.enabled_media
                and isinstance(messages, list)
                and messages
                and all(
                    isinstance(part, dict)
                    and (
                        part.get("type") == "plain"
                        and isinstance(part.get("text"), str)
                        or part.get("type") == "image"
                        and isinstance(part.get("path"), str)
                        and not part.get("url")
                    )
                    for part in messages
                )
            ):
                chain = [
                    Plain(part["text"].strip())
                    if part["type"] == "plain"
                    else Image.fromFileSystem(part["path"])
                    for part in messages
                ]
                return await self.deliver(
                    event, "tool", chain, lambda: original_tool(tool, context, **kwargs)
                )
            if (
                not isinstance(messages, list)
                or not messages
                or any(
                    not isinstance(part, dict)
                    or part.get("type") != "plain"
                    or not isinstance(part.get("text"), str)
                    for part in messages
                )
            ):
                return await self.deliver(
                    event,
                    "tool",
                    None,
                    lambda: original_tool(tool, context, **kwargs),
                )
            # Match the core tool's stripping rules before candidate checks.
            chain = [Plain(part["text"].strip()) for part in messages]
            return await self.deliver(
                event,
                "tool",
                chain,
                lambda: original_tool(tool, context, **kwargs),
            )

        SendMessageToUserTool.call = tool_call
        self._bindings.append((SendMessageToUserTool, "call", original_tool, tool_call))
        original_cron_send = CronMessageEvent.send

        @functools.wraps(original_cron_send)
        async def cron_send(event, chain):
            return await self.deliver(
                event, "event", chain, lambda: original_cron_send(event, chain)
            )

        CronMessageEvent.send = cron_send
        self._bindings.append((CronMessageEvent, "send", original_cron_send, cron_send))

    async def authorize(self, action, params, operation):
        """Reuse the actual outbound policy and verify the sending producer.

        Args:
            action: The exact OneBot action requested by the protected client.
            params: Isolated parameters already checked for plain owner text.
            operation: The stable native operation identity.

        Returns:
            Whether the source is a current verified event or persisted intent.
        """
        from .group_policy import (
            _COMMENT_SHARE_ROUTE,
            _platform_group_share,
            admin_info_outbound_block,
            group_outbound_block,
        )
        from .peak_runtime import (
            automatic_peak_paused,
            owner_event_granted,
            owner_scope_active,
        )

        if (
            admin_info_outbound_block(action, params)
            or group_outbound_block(action, params)
            or (
                not _COMMENT_SHARE_ROUTE.get() and _platform_group_share(action, params)
            )
        ):
            return False
        peak = automatic_peak_paused()
        # NativePrivateTransport is a direct HTTP route and therefore has to
        # enforce the peak boundary independently of the client.call_action
        # wrapper.  AstrBot Cron, automatic discovery, and daily reports never
        # acquire a high-peak exemption.  A manual report may continue only
        # when its durable intent was created by the current owner scope.
        if peak and operation.producer in {
            "cron", "private-discovery", "token-report-daily"
        }:
            return False
        permit = self._permit.get()
        if operation.producer == "incident-notice":
            # A persisted incident alone is not authority to send. The policy
            # must hold the exact notice in the current report task, and this
            # current native handle must own the protected client chain. Peak
            # runtime asks this same authorizer before entering its inner route.
            policy = getattr(self.host, "incidents", None)
            check = getattr(policy, "notice_authorized", None)
            return bool(
                operation.slot == "notice"
                and getattr(self.host, "_native_private_handle", None) is self
                and self.transport.enabled and self.is_current()
                and callable(check)
                and action == "send_private_msg"
                and set(params) == {"user_id", "message"}
                and str(params.get("user_id", "")) == OWNER
                and isinstance(params.get("message"), str)
                and check(operation.source_id, params["message"], asyncio.current_task())
            )
        if operation.producer == "reply":
            if not permit or permit[0] != operation:
                return False
            if peak and (
                len(permit) < 2
                or not owner_event_granted(permit[1])
            ):
                return False
            if operation.slot.startswith("progress/"):
                from .resource_guard import owner_photo_timeout_budget

                if (
                    len(permit) != 3
                    or permit[2] is not asyncio.current_task()
                    or owner_photo_timeout_budget(permit[1]) is None
                ):
                    return False
            try:
                return (
                    owner_reply_operation(permit[1], slot=operation.slot) == operation
                )
            except NativeTransportError:
                return False
        if operation.producer == "private-discovery":
            return operation.slot == "message" and any(
                isinstance(row, dict)
                and row.get("status") == "sending"
                and row.get("delivery_id") == operation.source_id
                for row in self.host.state.get("private_items", [])
            )
        if operation.producer == "token-report-manual":
            row = self.host.state.get("token_usage_manual_images", {}).get(
                operation.source_id
            )
            return (
                operation.slot == "report"
                and isinstance(row, dict)
                and row.get("status") == "intent"
                and (
                    not peak
                    or row.get("owner_direct_peak_v1") is True
                    or owner_scope_active()
                )
            )
        if operation.producer == "token-report-daily":
            row = self.host.state.get("token_usage_reports", {}).get(
                operation.source_id
            )
            return (
                operation.slot == "report"
                and isinstance(row, dict)
                and row.get("status") == "intent"
            )
        if operation.producer == "cron":
            if not permit or permit[0] != operation:
                return False
            event = permit[1]
            if not isinstance(event, self._cron_class):
                return False
            meta = event.get_extra("cron_job", {})
            if not isinstance(meta, dict) or meta.get("session") != OWNER_UMO:
                return False
            manager = getattr(self.host.context, "cron_manager", None)
            db = getattr(manager, "db", None)
            if not callable(getattr(db, "get_cron_job", None)):
                return False
            job = await db.get_cron_job(meta.get("id"))
            started = getattr(job, "last_run_at", None)
            try:
                source_started = datetime.fromisoformat(meta["run_started_at"])
            except (ValueError, TypeError, KeyError):
                return False
            if not isinstance(started, datetime):
                return False
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            payload = getattr(job, "payload", None)
            return (
                str(getattr(job, "job_id", "")) == meta.get("id")
                and getattr(job, "job_type", None) == "active_agent"
                and getattr(job, "status", None) == "running"
                and isinstance(payload, dict)
                and payload.get("session") == OWNER_UMO
                and source_started.tzinfo is not None
                and abs((started - source_started).total_seconds()) < 0.001
            )
        return False

    async def send_incident_notice(self, incident_id, text):
        """Attempt a fixed pending incident notice using the native route once.

        The operation ledger independently reserves before transport. A missing
        handle, failure, or uncertain response is never retried via Snow.
        """
        policy = getattr(self.host, "incidents", None)
        check = getattr(policy, "notice_authorized", None)
        if (self._retired or getattr(self.host, "_native_private_handle", None) is not self
                or not self.transport.enabled or not self.is_current()
                or not callable(check)
                or not check(incident_id, text, asyncio.current_task())):
            raise NativeTransportError("incident_notice_not_authorized")
        operation = producer_operation("incident-notice", incident_id, slot="notice")
        with native_delivery(operation):
            result = await self.client.call_action(
                "send_private_msg", user_id=OWNER, message=text
            )
        if (not isinstance(result, dict) or result.get("native_delivery_confirmed") is not True
                or self.transport.store.state(operation) != "confirmed"):
            raise NativeDeliveryUnknown("incident_notice_unknown_do_not_resend")
        return {"confirmed": True, "message_id": result.get("native_message_id"), "transport": "native"}

    async def deliver(self, event, lane, chain, original):
        """Route one verified plain event/tool part and retain uncertain slots.

        Args:
            event: Real owner event or a persisted active cron occurrence.
            lane: Stable producer lane, separating normal and tool replies.
            chain: Core message chain or its component list.
            original: Awaitable factory preserving the original send behavior.

        Returns:
            The original event/tool result after native acknowledgement.
        """
        if self._retired:
            return await self._deliver_retired(event, lane, chain, original)
        if str(getattr(event, "unified_msg_origin", "")) != OWNER_UMO:
            return await original()
        slots = getattr(event, "_feiyu_native_slots", None)
        if slots is None:
            slots = {
                "event": 0,
                "tool": 0,
                "unknown": set(),
                "locks": {"event": asyncio.Lock(), "tool": asyncio.Lock()},
            }
            event._feiyu_native_slots = slots
        # Event producers can concurrently emit multiple intentional outputs.
        # Select/complete their deterministic lane indexes under the same lock.
        async with slots["locks"][lane]:
            if not self._retired:
                token = _ACTIVE_DELIVERY.set((asyncio.current_task(), event, lane, self))
                try:
                    return await self._deliver_locked(event, lane, chain, original, slots)
                finally:
                    _ACTIVE_DELIVERY.reset(token)
        # Retirement can happen while this task waits behind another send.
        # Release the old lane lock before asking its current owner to acquire it.
        return await self._deliver_retired(event, lane, chain, original)

    async def _deliver_retired(self, event, lane, chain, original):
        """Bypass only a nested stale wrapper; standalone old events stay gated."""
        active = _ACTIVE_DELIVERY.get()
        if (
            isinstance(active, tuple) and len(active) == 4
            and active[0] is asyncio.current_task() and active[1] is event
            and active[2] == lane
        ):
            if active[3]._retired or active[3] is self:
                raise NativeTransportError("native_inflight_handle_retired_do_not_resend")
            return await original()
        current = getattr(self.host, "_native_private_handle", None)
        if (
            isinstance(current, StewardNativeHandle) and current is not self
            and not current._retired and current.adapter is self.adapter
        ):
            return await current.deliver(event, lane, chain, original)
        raise NativeTransportError("native_retired_handle_requires_current_route")

    async def _deliver_locked(self, event, lane, chain, original, slots):
        """Select one stable output slot while its event lane is serialized.

        Args:
            event: Verified owner event or scheduler event.
            lane: The event or tool output lane.
            chain: Core output chain.
            original: Original awaitable send factory.
            slots: Per-event indexes and uncertainty markers.

        Returns:
            Original send or tool result.
        """
        index = slots[lane]
        slot = f"{lane}/{index}"
        if isinstance(event, self._cron_class):
            meta = event.get_extra("cron_job", {})
            if not isinstance(meta, dict) or not all(
                isinstance(meta.get(key), str) and meta[key]
                for key in ("id", "run_started_at")
            ):
                return await original()
            occurrence = hashlib.sha256(
                (meta["id"] + "\0" + meta["run_started_at"]).encode()
            ).hexdigest()
            operation = producer_operation("cron", occurrence, slot=slot)
        else:
            try:
                operation = owner_reply_operation(event, slot=slot)
            except NativeTransportError:
                return await original()
        saved_state = self.transport.store.state(operation)
        if saved_state == "excluded":
            raise NativeDeliveryUnknown("original_delivery_unknown_do_not_resend")
        parts = getattr(chain, "chain", chain)
        text = None
        media = False
        if (
            isinstance(parts, list)
            and parts
            and all(isinstance(part, self._plain) for part in parts)
        ):
            text = self.transport.text_candidate(
                "send_private_msg",
                {
                    "user_id": OWNER,
                    "message": [
                        {"type": "text", "data": {"text": part.text}} for part in parts
                    ],
                },
            )
        elif (
            self.transport.enabled_media
            and isinstance(parts, list)
            and parts
            and all(isinstance(part, (self._plain, self._image)) for part in parts)
        ):
            media = any(isinstance(part, self._image) for part in parts)
        if (text is None and not media) or not self.transport.enabled:
            # In-memory markers disappear after a rebuilt event or restart;
            # the durable ledger remains authoritative about its chosen route.
            if saved_state is not None or slot in slots["unknown"]:
                raise NativeDeliveryUnknown("native_delivery_unknown_do_not_resend")
            if not self.transport.store.exclude(operation):
                raise NativeDeliveryUnknown("original_delivery_unknown_do_not_resend")
            slots["unknown"].add(slot)
            result = await original()
            # Tool.call catches its own delivery exceptions and returns text.
            # Leave its slot unresolved unless its explicit success is returned.
            if (
                lane != "tool"
                or isinstance(result, str)
                and result.startswith("Message sent to session ")
            ):
                slots[lane] += 1
                slots["unknown"].discard(slot)
            return result
        slots["unknown"].add(slot)
        token = self._permit.set((operation, event))
        try:
            with native_delivery(operation):
                result = await original()
            # The built-in tool catches send errors. Read its durable outcome
            # before advancing; a retry must use the same uncertain operation.
            if self.transport.store.state(operation) == "confirmed":
                slots[lane] += 1
                slots["unknown"].discard(slot)
            return result
        finally:
            self._permit.reset(token)

    def is_current(self):
        """Return whether every wrapper still owns its current binding."""
        def binding_reaches(target, name, wrapped):
            current = target.__dict__.get(name)
            seen = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                if current is wrapped:
                    return True
                state = getattr(current, "_peak_runtime_state", None)
                prior = state.get("original") if isinstance(state, dict) else None
                if prior is None and target is self._tool_class and name == "call":
                    prior = _audited_capture_original(current)
                if prior is None:
                    return False
                current = prior
            return False

        call_action = getattr(self.client, "call_action", None)
        seen = set()
        reaches_native = False
        # PeakRuntime may sit outside this native wrapper.  Follow only the
        # explicit restoration chain; functools.wraps markers are copied and
        # cannot establish wrapper ownership.
        while callable(call_action) and id(call_action) not in seen:
            seen.add(id(call_action))
            if call_action is self._restore_transport.wrapped:
                reaches_native = True
                break
            state = getattr(call_action, "_peak_runtime_state", None)
            prior = state.get("original") if isinstance(state, dict) else None
            if prior is None:
                break
            call_action = prior
        return (
            not self._retired and all(
                binding_reaches(target, name, wrapped)
                for target, name, _, wrapped in self._bindings
            )
            and self.adapter.get_client() is self.client
            and reaches_native
        )

    def status(self):
        """Return non-secret scope and wrapper health for local diagnostics."""
        try:
            from .resource_guard import owner_photo_timeout_budget

            progress_guard_available = callable(owner_photo_timeout_budget)
        except ImportError:
            progress_guard_available = False
        current = self.is_current()
        return {
            "enabled": self.transport.enabled,
            "installed": current,
            "image_progress_ready": bool(
                self.transport.enabled
                and current
                and getattr(self.host, "_native_private_handle", None) is self
                and progress_guard_available
            ),
            "owner_private_plain_text": True,
            "media_supported": self.transport.enabled_media,
            "enabled_media": self.transport.enabled_media,
            "enabled_voice": False,
            "group_supported": False,
            "producers": [
                "reply",
                "private-discovery",
                "cron",
                "token-report-manual",
                "token-report-daily",
                "incident-notice",
            ],
        }

    def restore(self):
        """Restore only bindings still owned by this installation."""
        # Other plugins can retain our closures below temporary wrappers.
        # Tombstone them permanently before restoring any top-level binding.
        self._retired = True
        for target, name, original, wrapped in reversed(self._bindings):
            if getattr(target, name, None) is wrapped:
                setattr(target, name, original)
        self._restore_transport()


def install_steward_native_transport(
    host,
    adapter,
    *,
    config_path: Path,
    store_path: Path,
    enabled: bool = True,
    enabled_media: bool = False,
):
    """Install event/tool routes after steward's existing reconnect gates.

    Args:
        host: Steward instance with current state and AstrBot context.
        adapter: The existing Snow adapter after reconnect installation.
        config_path: Absolute protected bridge configuration path.
        store_path: Absolute persistent native operation ledger path.
        enabled: Whether producers may scope eligible owner-private text.
        enabled_media: Enable images only after the protocol capability gate.

    Returns:
        A reversible handle exposing transport, status and binding ownership.
    """
    return StewardNativeHandle(
        host,
        adapter,
        config_path=config_path,
        store_path=store_path,
        enabled=enabled,
        enabled_media=enabled_media,
    )
