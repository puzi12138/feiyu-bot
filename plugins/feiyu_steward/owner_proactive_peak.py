"""A bounded high-tariff opportunity for ordinary owner text conversation."""

import asyncio
import math
import time
from datetime import datetime
from urllib.parse import urlsplit

from .availability import refresh_availability
from .policy import OWNER_UMO, TZ, delivery_block, interest_driven
from .proactive import chat_block
from .token_pricing import _peak


_PURPOSE = "peak_owner_proactive_chat_v1"
_SLOT_SECONDS = 15 * 60
_CAPABILITY_SECONDS = 90


def _contains_wrapper(current, target):
    """Check an installed wrapper chain without assuming a fixed depth."""
    seen = set()
    while callable(current) and id(current) not in seen and len(seen) < 32:
        if current is target:
            return True
        seen.add(id(current))
        current = getattr(current, "__wrapped__", None)
    return False


class OwnerProactivePeak:
    def _owner_peak_bindings(self):
        """Return only the currently protected provider and QQ client."""
        try:
            runtime = self._peak_runtime_handle
            if runtime.status().get("ready") is not True:
                return None
            provider = self.context.get_provider_by_id("deepseek-flash")
            if (provider is None or provider is not self._group_budget_provider
                    or provider.get_model() != "deepseek-flash"):
                return None
            sdk = provider.client
            if (type(sdk.max_retries) is not int or sdk.max_retries != 0
                    or urlsplit(str(sdk.base_url)).hostname != "api.deepseek.com"
                    or urlsplit(str(provider.provider_config.get("api_base") or "")).hostname != "api.deepseek.com"):
                return None
            completions = sdk.chat.completions
            adapter = self.context.get_platform_inst("snowluma-qq")
            client = adapter.get_client() if adapter is not None else None
            reconnect = self._reconnect_guard
            outbound = self._group_outbound_restore
            group_budget = self._group_provider_restore
            if (not reconnect or reconnect[0] is not adapter or reconnect[3] is not client
                    or not client or not outbound or not group_budget
                    or not _contains_wrapper(client.call_action, outbound.wrapped)
                    or not _contains_wrapper(provider.text_chat, group_budget.wrapped["text_chat"])):
                return None
            if not any(obj is provider and name == "text_chat"
                       and _contains_wrapper(provider.text_chat, wrapped)
                       for obj, name, _, wrapped in runtime._provider_bindings):
                return None
            if not any(obj is completions and name == "create"
                       and _contains_wrapper(completions.create, wrapped)
                       for obj, name, _, wrapped in runtime._sdk_bindings):
                return None
            if not any(obj is client and _contains_wrapper(client.call_action, wrapped)
                       for obj, _, wrapped in runtime._client_bindings):
                return None
            return provider, completions, adapter, client
        except Exception:
            return None

    def _validate_owner_proactive_capability(self, capability, phase):
        """Recheck one task-bound capability without spending its budgets."""
        try:
            task = asyncio.current_task()
            now = time.time()
            monotonic = time.monotonic()
            if (phase not in {"between", "model", "send"}
                    or not isinstance(capability, dict)
                    or self._owner_peak_capability is not capability
                    or task is None or task is not self._owner_peak_loop_task
                    or capability.get("task") is not task
                    or capability.get("purpose") != _PURPOSE
                    or capability.get("origin") != OWNER_UMO
                    or capability.get("active") is not True
                    or capability.get("phase") != phase):
                return False
            issued, expires = capability.get("issued_at"), capability.get("expires_at")
            issued_mono, deadline = capability.get("issued_mono"), capability.get("deadline_mono")
            if (any(type(x) not in (int, float) or not math.isfinite(x)
                    for x in (issued, expires, issued_mono, deadline))
                    or not issued <= now < expires <= issued + _CAPABILITY_SECONDS
                    or not issued_mono <= monotonic < deadline <= issued_mono + _CAPABILITY_SECONDS
                    or _peak(now) is not True):
                return False
            if self.state.get("proactive") is not True or self.state.get("dnd", False):
                return False
            if (not interest_driven(self.state)
                    and self.state.get("last_chat", 0) != capability.get("last_chat")):
                return False
            counters = tuple(capability.get(key) for key in ("model_calls", "sdk_calls", "send_calls"))
            if any(type(value) is not int or value not in (0, 1) for value in counters):
                return False
            if phase == "model" and counters not in {(0, 0, 0), (1, 0, 0), (1, 1, 0)}:
                return False
            if phase == "send" and counters not in {(1, 1, 0), (1, 1, 1)}:
                return False
            bindings = self._owner_peak_bindings()
            return bool(bindings and all(bindings[index] is capability.get(key) for index, key in enumerate(
                ("provider", "sdk_parent", "adapter", "client")
            )))
        except Exception:
            return False

    async def peak_owner_chat_opportunity(self):
        """Run at most one ordinary chat in the monitor's current task."""
        task = asyncio.current_task()
        if (task is None or task is not getattr(self, "_owner_peak_loop_task", None)
                or getattr(self, "_owner_peak_capability", None) is not None):
            return {"ok": False, "reason": "peak_owner_task_required"}
        now = datetime.now(TZ)
        if _peak(now.timestamp()) is not True:
            return {"ok": False, "reason": "verified_peak_required"}
        if refresh_availability(self.state, now):
            self.save()
        reason = delivery_block(self.state, "private", now) or chat_block(self.state, now)
        if reason:
            return {"ok": False, "reason": reason}
        bindings = self._owner_peak_bindings()
        if bindings is None:
            return {"ok": False, "reason": "peak_owner_guards_not_ready"}
        wall, monotonic = time.time(), time.monotonic()
        capability = {
            "purpose": _PURPOSE, "origin": OWNER_UMO, "task": task, "active": True,
            "issued_at": wall, "expires_at": wall + _CAPABILITY_SECONDS,
            "issued_mono": monotonic, "deadline_mono": monotonic + _CAPABILITY_SECONDS,
            "phase": "between", "provider": bindings[0], "sdk_parent": bindings[1],
            "adapter": bindings[2], "client": bindings[3], "last_chat": self.state.get("last_chat", 0),
            "model_calls": 0, "sdk_calls": 0, "send_calls": 0,
            "prompt": "", "system_prompt": "", "expected_text": "",
        }
        self._owner_peak_capability = capability
        try:
            if not self._validate_owner_proactive_capability(capability, "between"):
                return {"ok": False, "reason": "peak_owner_capability_invalid"}
            return await self.send_private_opportunity("chat", peak_capability=capability)
        finally:
            capability["active"] = False
            capability["prompt"] = capability["system_prompt"] = capability["expected_text"] = ""
            if self._owner_peak_capability is capability:
                self._owner_peak_capability = None

    async def peak_owner_chat_monitor(self):
        """Give verified high-peak slots one chance, without catch-up."""
        task = asyncio.current_task()
        previous = getattr(self, "_owner_peak_loop_task", None)
        if task is None or (previous is not None and not previous.done()):
            return
        self._owner_peak_loop_task = task
        self._owner_peak_capability = None
        self._owner_peak_monitor_status = {
            "running": True, "guard_ready": False, "last_decision": "waiting",
            "opportunities": 0, "sent": 0,
        }
        try:
            while True:
                try:
                    now = time.time()
                    status = self._owner_peak_monitor_status
                    status["guard_ready"] = self._owner_peak_bindings() is not None
                    if _peak(now) is True:
                        slot = int(now // _SLOT_SECONDS)
                        run = False
                        async with self.lock:
                            saved = self.state.get("peak_owner_proactive_slot")
                            if saved is None:
                                self.state["peak_owner_proactive_slot"] = slot
                                self.save()
                                status["last_decision"] = "initialized"
                            elif type(saved) is not int or saved < 0 or saved > slot:
                                status["last_decision"] = "invalid_slot"
                            elif saved < slot:
                                self.state["peak_owner_proactive_slot"] = slot
                                self.save()
                                run = True
                        if run:
                            status["opportunities"] += 1
                            result = await self.peak_owner_chat_opportunity()
                            reason = result.get("reason") if isinstance(result, dict) else None
                            allowed = {
                                "sent", "adapter_unavailable", "unknown", "disabled", "user_dnd",
                                "quiet_hours", "active_conversation", "daily_limit", "cooldown",
                                "ordinary_chat_awaiting_reply", "ordinary_chat_cooldown",
                                "quality_or_privacy_gate", "duplicate", "unfounded_experience",
                                "peak_platform_content_blocked", "peak_owner_guards_not_ready",
                                "peak_owner_capability_invalid", "peak_owner_text_boundary",
                                "verified_peak_required", "model_response_not_json",
                                "invalid_model_response_schema", "ambiguous_model_json",
                            }
                            status["last_decision"] = reason if reason in allowed else "skipped_or_failed"
                            if isinstance(result, dict) and result.get("ok") is True:
                                status["sent"] += 1
                except Exception:
                    self._owner_peak_monitor_status["last_decision"] = "skipped_or_failed"
                await asyncio.sleep(30)
        finally:
            cap = getattr(self, "_owner_peak_capability", None)
            if isinstance(cap, dict):
                cap["active"] = False
            self._owner_peak_capability = None
            self._owner_peak_monitor_status["running"] = False
            if self._owner_peak_loop_task is task:
                self._owner_peak_loop_task = None

    def peak_owner_chat_status(self):
        """Expose only loop and decision metadata, never message contents."""
        status = getattr(self, "_owner_peak_monitor_status", None)
        if not isinstance(status, dict):
            return {"running": False, "guard_ready": False, "last_decision": "not_started",
                    "opportunities": 0, "sent": 0}
        return {key: status[key] for key in
                ("running", "guard_ready", "last_decision", "opportunities", "sent")}
