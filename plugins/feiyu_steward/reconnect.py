"""Durable owner-only reconnect checks; no replay of external write operations."""

import asyncio
import functools
import hashlib
import json
import time
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType

from .group_policy import (
    SOCIAL_GROUP_UMO,
    group_input_rejection,
    group_risk_assessment,
    install_group_outbound_gate,
)
from .policy import OWNER_UMO
from .peak_policy import PeakTariffPaused, QQChatPaused, blocked_qq_event, peak_active
from .peak_runtime import business_paused
from .qq_chat_pause import qq_chat_paused, qq_chat_pending_before_pause, qq_chat_was_paused
from .qq_friend_policy import (
    FRIEND,
    FRIEND_UMO,
    friend_event_allowed,
    friend_raw_allowed,
    friend_message_chain_allowed,
    friend_send_scope,
    remember_friend_turn,
    reserve_friend_event,
)
from .token_pricing import _peak

OWNER = "0"
SELF = "0"
GROUP = "0"
SESSIONS = (OWNER_UMO, f"snowluma-qq:GroupMessage:{GROUP}", SOCIAL_GROUP_UMO)
GRACE = 180  # Let an already running model/tool request finish first.
LOOKBACK = 7 * 86400
HISTORY_PAGE_SIZE = 100  # SnowLuma/QQ silently returns [] for count=200.
HISTORY_MAX_PAGES = 10
LEGACY_BACKGROUND_USER = "Output your last task result below."
BACKGROUND_HISTORY_LABEL = "[后台任务记录；不是用户发言]"


def strip_prompt_template(value, template):
    """Undo exactly one configured wrapper, never a similarity-based match."""
    if (not isinstance(value, str) or not isinstance(template, str)
            or not 0 < len(template) <= 20000 or template.count("{{prompt}}") != 1):
        return value, False
    before, after = template.split("{{prompt}}")
    if not before and not after:
        return value, False
    if (len(value) < len(before) + len(after) or not value.startswith(before)
            or after and not value.endswith(after)):
        return value, False
    return value[len(before):len(value) - len(after) if after else None], True


def strip_prompt_template_history(history, template):
    """Strip wrappers only from the first text block of actual user rows."""
    if not isinstance(history, list):
        return history, 0
    result, changed = [], 0
    for row in history:
        if not isinstance(row, dict) or row.get("role") != "user":
            result.append(row)
            continue
        value = row.get("content")
        if isinstance(value, str):
            text, matched = strip_prompt_template(value, template)
            result.append({**row, "content": text} if matched else row)
        elif isinstance(value, list) and value and isinstance(value[0], dict) and value[0].get("type") == "text":
            text, matched = strip_prompt_template(value[0].get("text"), template)
            result.append({**row, "content": [{**value[0], "text": text}, *value[1:]]} if matched else row)
        else:
            matched = False
            result.append(row)
        changed += int(matched)
    return result, changed


def _source_value(source, key, default=None):
    return source.get(key, default) if isinstance(source, dict) else getattr(source, key, default)


def _verified_cron_note(note, jobs, session, now):
    """Recognize a legacy core-generated note using local scheduler evidence.

    Text resemblance is insufficient: the current task's exact name and
    description, target session, and scheduled run time must also agree.
    A removed/edited task cannot establish provenance for an old entry.
    """
    if not isinstance(note, str) or len(note) > 100000:
        return False
    for job in jobs[:1000]:
        payload = _source_value(job, "payload", {})
        if (_source_value(job, "job_type") != "active_agent" or not isinstance(payload, dict)
                or payload.get("session") != session):
            continue
        name = _source_value(job, "name") or _source_value(job, "job_id")
        description = _source_value(job, "description") or ""
        expression = _source_value(job, "cron_expression")
        zone = _source_value(job, "timezone") or "UTC"
        if (not isinstance(name, str) or not isinstance(description, str)
                or len(name) > 255 or len(description) > 20000
                or not isinstance(expression, str) or len(expression) > 255):
            continue
        prefix = f"[CronJob] {name}: {description}  triggered at "
        if not note.startswith(prefix):
            continue
        marker = ", I finished this job, here is the result: "
        stamp, separator, _ = note[len(prefix):].partition(marker)
        if not separator or len(stamp) > 48:
            continue
        try:
            started = datetime.fromisoformat(stamp)
            if started.tzinfo is None or not 0 < started.timestamp() <= now + 2:
                continue
            trigger = CronTrigger.from_crontab(expression, timezone=zone)
            scheduled = trigger.get_next_fire_time(None, started - timedelta(seconds=2))
            if scheduled is not None and 0 <= (started - scheduled).total_seconds() < 2:
                return True
        except (TypeError, ValueError, OverflowError, KeyError):
            continue
    return False


def label_verified_background_history(history, jobs, session, *, now=None):
    """Relabel proven internal pairs in a request copy; never edit the DB."""
    if not isinstance(history, list) or not isinstance(jobs, list):
        return history, 0
    current = time.time() if now is None else now
    result, index, changed = [], 0, 0
    while index < len(history):
        row = history[index]
        following = history[index + 1] if index + 1 < len(history) else None
        if (isinstance(row, dict) and set(row) == {"role", "content"}
                and row.get("role") == "user" and row.get("content") == LEGACY_BACKGROUND_USER
                and isinstance(following, dict) and set(following) == {"role", "content"}
                and following.get("role") == "assistant"
                and _verified_cron_note(following.get("content"), jobs, session, current)):
            result.append({"role": "assistant", "content": BACKGROUND_HISTORY_LABEL + "\n" + following["content"]})
            changed += 1
            index += 2
        else:
            result.append(row)
            index += 1
    return result, changed


def sender(message):
    return str((message.get("sender") or {}).get("user_id") or message.get("user_id", ""))


def segments(message):
    value = message.get("message", [])
    return value if isinstance(value, list) else []


def message_text(message):
    parts = []
    for seg in segments(message):
        kind, data = seg.get("type"), seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind not in {"reply", "at"}:
            parts.append(f"[{kind}附件，尚未读取内容]")
    return "".join(parts).strip()


def fingerprint(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def addressed_owner_message(message, session):
    if sender(message) != OWNER or message.get("message_id") is None:
        return False
    if session == OWNER_UMO:
        return (message.get("message_type") == "private"
                and str(message.get("target_id") or SELF) == SELF)
    if (session not in SESSIONS[1:] or message.get("message_type") != "group"
            or str(message.get("group_id")) != session.rsplit(":", 1)[-1]):
        return False
    mentions = {str((part.get("data") or {}).get("qq") or "")
                for part in segments(message) if part.get("type") in {"at", "atall"}}
    if SELF in mentions:
        return True
    # Match the social wake rule: a name call can address the bot, while a
    # message explicitly addressed to someone else cannot do so by accident.
    return (session == SOCIAL_GROUP_UMO and not mentions
            and message_text(message).startswith(("大肥鱼", "肥鱼")))


class ReconnectRecovery:
    def configured_prompt_template(self, session):
        """Read only the active fixed wrapper, without editing configuration."""
        try:
            cfg = self.context.get_config(umo=session)
            value = cfg.get("provider_settings", {}).get("prompt_prefix", "")
            if isinstance(value, str) and 0 < len(value) <= 20000 and value.count("{{prompt}}") == 1:
                return value
        except Exception:  # noqa: BLE001 - absent config leaves original text intact
            pass
        return ""

    async def request_background_sources(self):
        """Obtain scheduler definitions as local provenance, not model claims."""
        try:
            cached = getattr(self, "_background_history_source_cache", {})
            if time.time() - cached.get("at", 0) > 60:
                manager = getattr(self.context, "cron_manager", None)
                values = await manager.list_jobs("active_agent") if manager else []
                cached = {"at": time.time(), "jobs": values if isinstance(values, list) else []}
                self._background_history_source_cache = cached
            return cached.get("jobs", [])
        except Exception:  # noqa: BLE001 - absent provenance never permits history rewriting
            return []

    async def apply_inbound_identity_context(self, event, req):
        """Separate one real QQ input from quotes and background placeholders."""
        session = str(getattr(event, "unified_msg_origin", ""))
        if session != OWNER_UMO:
            return {"verified_background_pairs": 0, "current_qq_input": False}
        jobs = await self.request_background_sources()
        changed = 0
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            req.contexts, changed = label_verified_background_history(contexts, jobs, session)
        migrated = getattr(req, "_feiyu_prompt_template_migrated", False)
        if not migrated:
            template = self.configured_prompt_template(session)
            prompt, matched = strip_prompt_template(getattr(req, "prompt", None), template)
            contexts, history_matches = strip_prompt_template_history(getattr(req, "contexts", None), template)
            if matched or history_matches:
                if matched:
                    req.prompt = prompt
                req.contexts = contexts
                before, after = template.split("{{prompt}}")
                static = "\n[本机固定提示词；不是主人本次发言]\n" + before + after
                req.system_prompt = (getattr(req, "system_prompt", "") or "") + static
                req._feiyu_prompt_template_migrated = True
        raw = getattr(getattr(event, "message_obj", None), "raw_message", {}) or {}
        verified = isinstance(raw, dict) and addressed_owner_message(raw, session)
        note = (
            "\n[QQ收件与后台记录的区别]历史里的定时任务结果、后台任务结果及其兼容占位不是主人再次发消息。"
            "不能按相同提示词或引用文字出现的次数断言主人连续发送；也不能凭模型记忆猜测QQ回执、重传、同步或实际消息总数。"
        )
        if verified:
            source_id = str(raw.get("message_id"))
            if len(source_id) <= 24 and source_id.lstrip("-").isdigit():
                references = [str((part.get("data") or {}).get("id")) for part in segments(raw)
                              if part.get("type") == "reply"]
                references = [value for value in references[:8]
                              if len(value) <= 24 and value.lstrip("-").isdigit()]
                note += ("当前QQ事件对应一条已核实的主人入站消息，message_id=" + source_id + "。")
                if references:
                    note += "它引用的旧消息ID为" + ",".join(references) + "；引用块是上下文材料，不是另一条新发送。"
                note += "若主人说只发了一次，不能拿内部后台记录反驳；需要查记录才解释接收是否重复。"
        if "[QQ收件与后台记录的区别]" not in (getattr(req, "system_prompt", "") or ""):
            req.system_prompt = (getattr(req, "system_prompt", "") or "") + note
        return {"verified_background_pairs": changed, "current_qq_input": verified}

    def init_reconnect(self):
        self.reconnect_path = self.root / "reconnect-receipts.json"
        self.reconnect_state = {"installed_at": int(time.time()), "rows": {}, "attempts": [], "checkpoints": {}}
        if self.reconnect_path.exists():
            # A damaged ledger must not silently reset and replay old requests.
            self.reconnect_state.update(json.loads(self.reconnect_path.read_text(encoding="utf-8")))
        self._reconnect_guard = None
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_requested = True
        self._reconnect_retry_at = 0
        self._reconnect_last_activity = {}
        self._reconnect_status = {"enabled": True, "connected": False}
        for attempt in self.reconnect_state["attempts"]:
            if attempt["status"] == "sending":
                attempt["status"] = "unknown"
                for key in attempt["keys"]:
                    row = self.reconnect_state["rows"].get(key)
                    if row and row["status"] == "pending":
                        row["status"] = "unknown"
        self.save_reconnect()

    def save_reconnect(self):
        # Store only transport metadata, statuses and hashes, never private text.
        if getattr(self, "_reconnect_terminated", False):
            return
        cutoff = time.time() - LOOKBACK
        rows = self.reconnect_state["rows"]
        self.reconnect_state["rows"] = {k: v for k, v in rows.items() if v["at"] >= cutoff}
        self.reconnect_state["attempts"] = [a for a in self.reconnect_state["attempts"]
                                             if a["at"] >= cutoff][-500:]
        tmp = self.reconnect_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.reconnect_state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.reconnect_path)

    def reconnect_status(self):
        if not hasattr(self, "reconnect_state"):
            return {"enabled": False}
        counts = {}
        for row in self.reconnect_state["rows"].values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {**self._reconnect_status, "counts": counts,
                "last_scan": self.reconnect_state.get("last_scan", 0),
                "installed_at": self.reconnect_state["installed_at"],
                "quiet_hours_apply": False}

    def recovery_key(self, session, message_id):
        return session + ":" + str(message_id)

    def record_reconnect_inbound(self, session, message, live=False):
        if not addressed_owner_message(message, session):
            return None
        at = float(message.get("time") or time.time())
        if at < max(self.reconnect_state["installed_at"], time.time() - LOOKBACK):
            return None
        key = self.recovery_key(session, message["message_id"])
        if key not in self.reconnect_state["rows"]:
            self.reconnect_state["rows"][key] = {
                "session": session, "id": str(message["message_id"]), "at": at,
                "status": "pending", "observed_at": time.time(), "live": live}
            self.save_reconnect()
        if live:
            self._reconnect_last_activity[session] = time.time()
        return key

    def reconnect_reply_sent(self, key):
        row = self.reconnect_state["rows"].get(key)
        if row:
            row.update(status="replied", replied_at=time.time())
            self.save_reconnect()

    def covered_reconnect_keys(self, event, chain=None):
        """Resolve verified image sources and normally completed text follow-ups."""
        session = str(getattr(event, "unified_msg_origin", ""))
        raw = getattr(getattr(event, "message_obj", None), "raw_message", {}) or {}
        if session not in SESSIONS or not addressed_owner_message(raw, session):
            return []
        events = [event]
        batch = getattr(event, "private_companion_claimed_image_semantic_buffer", None)
        if session == OWNER_UMO and isinstance(batch, dict):
            sources = batch.get("source_events")
            if isinstance(sources, list):
                events.extend(sources[:32])
            else:
                events.append(batch.get("original_event"))
        follow_up_sources = []
        runner = getattr(event, "_astrbot_follow_up_runner", None)
        result = getattr(event, "get_result", lambda: None)()
        result_type = getattr(getattr(result, "result_content_type", None), "name", None)
        final = runner.get_final_llm_resp() if runner is not None else None
        if (runner is not None
                and getattr(getattr(runner.run_context, "context", None), "event", None) is event
                and getattr(getattr(runner, "state", None), "name", None) == "DONE"
                and not runner.was_aborted() and not runner._is_stop_requested()
                and not event.get_extra("agent_stop_requested") and not event.is_stopped()
                and final is not None and final.role == "assistant"
                and final is getattr(runner, "_follow_up_last_response", None)
                and final.completion_text == getattr(runner, "_follow_up_response_text", None)
                and all(part == "Plain" for part in runner._follow_up_response_parts)
                and final.completion_text and result_type == "LLM_RESULT"
                and (not final.result_chain or all(
                    getattr(getattr(part, "type", None), "name", None) == "Plain"
                    for part in final.result_chain.chain))
                and chain is not None and getattr(chain, "type", None) is None
                and chain.get_plain_text().strip() == final.completion_text.strip()
                and all(getattr(getattr(part, "type", None), "name", None)
                        in {"Plain", "Reply", "At"} for part in chain.chain)):
            # Only one complete plain final reply can cover the text sources;
            # a stream chunk, split reply or delegated send cannot prove this.
            # Captured or merely resolved tickets are not proof of coverage.
            for ticket in getattr(runner, "_consumed_follow_up_tickets", []):
                if (ticket.consumed and ticket.receipt_source_valid
                        and ticket.included_in_final_request
                        and ticket.source_event is not None
                        and not ticket.source_event.is_stopped()
                        and not ticket.source_event.get_extra("agent_stop_requested")):
                    follow_up_sources.append(ticket.source_event)
        keys = []
        for source in events + follow_up_sources:
            if str(getattr(source, "unified_msg_origin", "")) != session:
                continue
            source_raw = getattr(getattr(source, "message_obj", None), "raw_message", {}) or {}
            if not isinstance(source_raw, dict) or not addressed_owner_message(source_raw, session):
                continue
            key = self.recovery_key(session, source_raw["message_id"])
            row = self.reconnect_state["rows"].get(key)
            if (row and row.get("session") == session and key not in keys
                    and (source in events or row.get("status") == "pending")):
                keys.append(key)
        return keys

    def mark_peak_group_skipped(self, event, manual_paused=False):
        """Prevent a locally dropped group input from becoming a catch-up."""
        session = str(getattr(event, "unified_msg_origin", ""))
        raw = getattr(getattr(event, "message_obj", None), "raw_message", {}) or {}
        if (session not in SESSIONS[1:] or not isinstance(raw, dict)
                or not addressed_owner_message(raw, session)):
            return
        key = self.recovery_key(session, raw["message_id"])
        row = self.reconnect_state["rows"].get(key)
        if row and (manual_paused or row.get("status") != "pending"):
            return
        if row is None:
            try:
                at = float(raw.get("time") or time.time())
            except (TypeError, ValueError, OverflowError):
                at = time.time()
            row = {"session": session, "id": str(raw["message_id"]), "at": at,
                   "observed_at": time.time(), "live": True}
            self.reconnect_state["rows"][key] = row
        row["status"] = "qq_pause_skipped" if manual_paused else "peak_skipped"
        self.save_reconnect()

    def mark_reconnect_event(self, event):
        if not hasattr(self, "reconnect_state"):
            return
        for key in self.covered_reconnect_keys(event):
            self.reconnect_reply_sent(key)

    def reconnect_busy(self, session):
        # Also wait for long running/background agents, not only the grace timer.
        from astrbot.core.utils.active_event_registry import active_event_registry
        events = [e for values in getattr(active_event_registry, "_events", {}).values() for e in values]
        events += [e for values in getattr(active_event_registry, "_background_tasks", {}).values()
                   for task, e in values.items() if not task.done()]
        return any(addressed_owner_message(
            getattr(getattr(e, "message_obj", None), "raw_message", {}) or {}, session) for e in events)

    def install_reconnect_adapter(self):
        adapter = self.context.get_platform_inst("snowluma-qq")
        if adapter is None:
            return None
        if self._reconnect_guard and self._reconnect_guard[0] is adapter:
            return adapter
        self.restore_reconnect_adapter()
        original = adapter.create_event
        original_convert = getattr(adapter, "_convert_handle_message_event", None)
        original_commit = adapter.commit_event
        bot = adapter.get_client()
        if not callable(original_convert):
            raise TypeError("reconnect_converter_unsupported")
        self._group_outbound_restore = install_group_outbound_gate(bot)
        lifetime = {"active": True}

        async def on_connect(event):
            if str(event.get("self_id")) == SELF:
                self._reconnect_requested = True

        def rejected_friend_message(message):
            """Build a side-effect-free event for a rejected raw friend message."""
            result = AstrBotMessage()
            user_id = str(message.get("user_id", "")) if isinstance(message, dict) else ""
            result.self_id = str(message.get("self_id", "")) if isinstance(message, dict) else ""
            result.sender = MessageMember(user_id, user_id)
            result.type = MessageType.FRIEND_MESSAGE
            result.session_id = user_id
            result.message_id = str(message.get("message_id", "")) if isinstance(message, dict) else ""
            result.message = []
            result.message_str = ""
            result.raw_message = message
            return result

        @functools.wraps(original_convert)
        async def guarded_convert(message, get_reply=True):
            """Reject target-friend media/replies before aiocqhttp resolves them."""
            if not lifetime["active"]:
                return await original_convert(message, get_reply=get_reply)
            try:
                is_target_friend = (
                    isinstance(message, dict)
                    and message.get("post_type") == "message"
                    and message.get("message_type") == "private"
                    and str(message.get("user_id", "")) == FRIEND
                )
            except (AttributeError, TypeError, ValueError):
                is_target_friend = False
            if is_target_friend and not friend_raw_allowed(message):
                return rejected_friend_message(message)
            return await original_convert(message, get_reply=get_reply)

        adapter._convert_handle_message_event = guarded_convert

        def create_event(message):
            if not lifetime["active"]:
                return original(message)
            event = original(message)
            manual_paused = qq_chat_paused()
            if blocked_qq_event(event, manual_paused=manual_paused):
                self.mark_peak_group_skipped(event, manual_paused=manual_paused)
                if not manual_paused:
                    self.observe_peak_deferred(event, paused=True)
                event.set_extra("feiyu_peak_blocked", True)
                event.stop_event()
                return event
            if str(getattr(event, "unified_msg_origin", "")) == FRIEND_UMO:
                if not reserve_friend_event(event):
                    event.set_extra("feiyu_peak_blocked", True)
                    event.stop_event()
                    return event
                send = event.send
                send_state = {"attempted": False}

                async def friend_send(chain):
                    if (not lifetime["active"] or send_state["attempted"] or not friend_event_allowed(event)
                            or not friend_message_chain_allowed(chain)):
                        raise QQChatPaused("Friend chat window closed before send")
                    send_state["attempted"] = True
                    with friend_send_scope(event):
                        result = await send(chain)
                    remember_friend_turn(event, chain)
                    return result

                event.send = friend_send
            self.observe_peak_deferred(event, paused=False)
            prepare_cooldown = getattr(self, "prepare_group_cooldown", None)
            if prepare_cooldown:
                prepare_cooldown(event)
            if str(getattr(event, "unified_msg_origin", "")) != FRIEND_UMO:
                prepare_listen = getattr(self, "prepare_listen_together", None)
                if prepare_listen and prepare_listen(event):
                    # Invitation handling has its own source checks and explicit
                    # private owner approval, independent of ordinary chat lanes.
                    return event
            ensure_resource = getattr(self, "ensure_native_resource_guard", None)
            if ensure_resource:
                ensure_resource()
            prepare_admin = getattr(self, "prepare_admin_info", None)
            if prepare_admin and prepare_admin(event):
                return event
            prepare_resource = getattr(self, "prepare_resource_request", None)
            if prepare_resource and prepare_resource(event):
                return event
            prepare_poke = getattr(self, "prepare_poke", None)
            if prepare_poke and prepare_poke(event):
                return event
            risk = {}
            if str(event.unified_msg_origin) == SOCIAL_GROUP_UMO:
                risk = group_risk_assessment(event)
                report = getattr(self, "report_group_issue", None)
                if risk.get("notify_owner") and callable(report):
                    self.spawn(report(event, risk["category"], risk["reason"]))
                event.set_extra("feiyu_group_risk_checked", True)
            rejection = group_input_rejection(event)
            if rejection:
                # Native follow-up capture runs before OnLLMRequest. Stop at
                # transport creation even when an older run's cooldown is over.
                event.set_extra("feiyu_group_early_rejection", rejection["reason"])
                if str(event.unified_msg_origin) == SOCIAL_GROUP_UMO and not risk.get("notify_owner"):
                    report = getattr(self, "report_group_issue", None)
                    if callable(report):
                        self.spawn(report(event, "complex_request", "请求过长或要求持续大量执行，群里先不展开。"))
                event.stop_event()
                return event
            group_gate = getattr(self, "group_message_block", None)
            if group_gate and group_gate(event):
                # Stop before AstrBot command argument parsing can emit an
                # error reply to an unauthorized group or non-owner sender.
                event.stop_event()
                return event
            raw = getattr(message, "raw_message", {}) or {}
            session = str(event.unified_msg_origin)
            key = self.record_reconnect_inbound(session, raw, live=True)
            if key:
                row = self.reconnect_state["rows"][key]
                live_ids = getattr(self, "_reconnect_live_ids", {})
                if row["status"] != "pending" or key in live_ids:
                    # History/transport duplicates must not run the normal tools again.
                    event.stop_event()
                self._reconnect_live_ids = {k: at for k, at in live_ids.items() if time.time() - at < LOOKBACK}
                self._reconnect_live_ids[key] = time.time()
                send = event.send

                async def tracked_send(chain):
                    if not chain.chain:
                        return await send(chain)
                    keys = self.covered_reconnect_keys(event, chain)
                    attempt = {"session": session, "keys": keys, "at": time.time(),
                               "hash": fingerprint(chain.get_plain_text()), "status": "sending",
                               "readback_requires_reference": True}
                    self.reconnect_state["attempts"].append(attempt)
                    for covered_key in keys:
                        covered_row = self.reconnect_state["rows"][covered_key]
                        if covered_row["status"] in {"pending", "media_unavailable"}:
                            covered_row["status"] = "unknown"
                    self.save_reconnect()
                    try:
                        result = await send(chain)
                    except BaseException:
                        attempt["status"] = "unknown"
                        for covered_key in keys:
                            covered_row = self.reconnect_state["rows"][covered_key]
                            if covered_row["status"] != "replied":
                                covered_row["status"] = "unknown"
                        self.save_reconnect()
                        raise
                    attempt["status"] = "sent"
                    for covered_key in keys:
                        self.reconnect_reply_sent(covered_key)
                    return result

                event.send = tracked_send
            return event

        def commit_event(event):
            if not lifetime["active"]:
                return original_commit(event)
            if event.get_extra("feiyu_peak_blocked"):
                return
            manual_paused = qq_chat_paused()
            if blocked_qq_event(event, manual_paused=manual_paused):
                self.mark_peak_group_skipped(event, manual_paused=manual_paused)
                if not manual_paused:
                    self.observe_peak_deferred(event, paused=True)
                return
            return original_commit(event)

        adapter.create_event = create_event
        adapter.commit_event = commit_event
        bot.subscribe("meta_event.lifecycle.connect", on_connect)
        self._reconnect_guard = (adapter, original, create_event, bot, on_connect,
                                 original_commit, commit_event, original_convert,
                                 guarded_convert, lifetime)
        return adapter

    def restore_reconnect_adapter(self):
        restore_outbound = getattr(self, "_group_outbound_restore", None)
        if restore_outbound:
            restore_outbound()
            self._group_outbound_restore = None
        guard = getattr(self, "_reconnect_guard", None)
        if guard:
            adapter, original, wrapped, bot, handler, original_commit, commit_event = guard[:7]
            original_convert = guard[7] if len(guard) > 7 else None
            guarded_convert = guard[8] if len(guard) > 8 else None
            if len(guard) > 9:
                guard[9]["active"] = False
            if adapter.create_event is wrapped:
                adapter.create_event = original
            if adapter.commit_event is commit_event:
                adapter.commit_event = original_commit
            if guarded_convert is not None and getattr(adapter, "_convert_handle_message_event", None) is guarded_convert:
                adapter._convert_handle_message_event = original_convert
            bot.unsubscribe("meta_event.lifecycle.connect", handler)
            self._reconnect_guard = None

    async def reconnect_action(self, adapter, action, **params):
        return await asyncio.wait_for(adapter.get_client().call_action(
            action, self_id=int(SELF), **params), 45)

    async def reconnect_history(self, adapter, session):
        private = session == OWNER_UMO
        params = {"user_id": int(OWNER)} if private else {"group_id": int(session.rsplit(":", 1)[-1])}
        floor = self.reconnect_state["checkpoints"].get(session, self.reconnect_state["installed_at"]) - 2
        pending_times = [r["at"] for r in self.reconnect_state["rows"].values()
                         if r["session"] == session and r["status"] in {"pending", "unknown"}]
        floor = max(time.time() - LOOKBACK, min([floor] + pending_times))
        collected, anchor = {}, None
        for _ in range(HISTORY_MAX_PAGES):
            page_params = {**params, "count": HISTORY_PAGE_SIZE}
            if anchor is not None:
                page_params.update(message_id=int(anchor), reverse_order=True)
            data = await self.reconnect_action(adapter,
                "get_friend_msg_history" if private else "get_group_msg_history", **page_params)
            if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                raise TypeError("history_unavailable")
            page = data["messages"]
            old_count = len(collected)
            for msg in page:
                if msg.get("message_id") is not None:
                    collected[str(msg["message_id"])] = msg
            if not page:
                break
            oldest = min(page, key=lambda m: (float(m.get("time") or 0), int(m.get("message_seq") or 0)))
            if float(oldest.get("time") or 0) <= floor or len(page) < HISTORY_PAGE_SIZE:
                break
            if len(collected) == old_count or oldest.get("message_id") == anchor:
                raise ValueError("history_cursor_did_not_advance")
            anchor = oldest["message_id"]
        else:
            raise ValueError("history_window_exceeded_1000_messages")
        # QQ history can omit an older message that is still directly readable.
        # Only recover already recorded, unanswered source messages; ambiguous
        # sends remain unknown and must never be made eligible again here.
        missing = sorted(
            (row for row in self.reconnect_state["rows"].values()
             if row["session"] == session and row["status"] == "pending"
             and row["id"] not in collected),
            key=lambda row: (row["at"], row["id"]))
        for row in missing[:20]:
            message = await self.reconnect_action(adapter, "get_msg", message_id=int(row["id"]))
            if (not isinstance(message, dict)
                    or str(message.get("message_id")) != row["id"]
                    or not addressed_owner_message(message, session)):
                # Let check_reconnect mark this session retryable without
                # advancing its checkpoint or treating a missing read as zero.
                raise ValueError("pending_message_identity_unavailable")
            collected[row["id"]] = message
        return sorted(collected.values(), key=lambda m: (float(m.get("time") or 0),
                                                        int(m.get("message_seq") or 0)))

    def reconcile_reconnect(self, session, messages):
        """Only explicit reply references/exact receipts count; notices do not."""
        rows = self.reconnect_state["rows"]
        for msg in messages:
            if sender(msg) != SELF:
                continue
            quoted = {str((x.get("data") or {}).get("id")) for x in segments(msg) if x.get("type") == "reply"}
            for quote in quoted:
                key = self.recovery_key(session, quote)
                if key in rows:
                    self.reconnect_reply_sent(key)
            for attempt in self.reconnect_state["attempts"]:
                if (attempt["session"] == session and attempt["status"] in {"unknown", "sending"}
                        and float(msg.get("time") or 0) >= attempt["at"] - 2
                        and attempt["hash"] == fingerprint(message_text(msg))
                        and (not attempt.get("readback_requires_reference")
                             or any(self.recovery_key(session, quote) in attempt["keys"] for quote in quoted))
                        and (not attempt.get("anchor") or attempt["anchor"] in quoted)):
                    attempt["status"] = "sent_readback"
                    for key in attempt["keys"]:
                        self.reconnect_reply_sent(key)
        self.save_reconnect()

    async def recover_reconnect_session(self, adapter, session, messages, dry_run=False):
        if business_paused():
            return {"pending": 0, "sent": False, "reason": "peak_tariff", "retry": True}
        all_messages = messages
        if session != OWNER_UMO:
            manual_paused = qq_chat_paused()
            if manual_paused or peak_active():
                return {"pending": 0, "sent": False,
                        "reason": "qq_chat_paused" if manual_paused else "peak_tariff", "retry": True}
            filtered = []
            for message in messages:
                stamp = message.get("time")
                if _peak(stamp) is True:
                    continue
                if qq_chat_was_paused(stamp):
                    message_id = str(message.get("message_id") or "")
                    key = self.recovery_key(session, message_id) if message_id else ""
                    row = self.reconnect_state["rows"].get(key)
                    if (not isinstance(row, dict) or row.get("status") != "pending"
                            or not qq_chat_pending_before_pause(stamp, row.get("observed_at"))):
                        continue
                filtered.append(message)
            messages = filtered
        for msg in messages:
            self.record_reconnect_inbound(session, msg)
        self.reconcile_reconnect(session, all_messages)
        indexed = {str(m.get("message_id")): m for m in messages if addressed_owner_message(m, session)}
        now = time.time()
        candidates = [(key, row) for key, row in self.reconnect_state["rows"].items()
                      if row["session"] == session and row["status"] == "pending" and row["id"] in indexed]
        if not candidates:
            return {"pending": 0, "sent": False}
        if self.reconnect_busy(session):
            return {"pending": len(candidates), "sent": False, "reason": "normal_reply_in_progress"}
        if any(now - row["observed_at"] < GRACE and row.get("live") for _, row in candidates):
            return {"pending": len(candidates), "sent": False, "reason": "normal_reply_in_progress"}
        if now - self._reconnect_last_activity.get(session, 0) < GRACE:
            return {"pending": len(candidates), "sent": False, "reason": "owner_active"}
        candidates.sort(key=lambda pair: pair[1]["at"])
        candidates = candidates[:20]
        # Recovery is text-only. Keep unread media recorded instead of asking a
        # text model to reply to attachment placeholders or pretend a disconnect.
        media = [(key, row) for key, row in candidates
                 if any(part.get("type") not in {"text", "reply", "at", "face"}
                        for part in segments(indexed[row["id"]]))]
        if media:
            media_keys = {key for key, _ in media}
            if not dry_run:
                for _, row in media:
                    row.update(status="media_unavailable", reason="recovery_has_no_media_input")
                self.save_reconnect()
            candidates = [(key, row) for key, row in candidates if key not in media_keys]
        if not candidates:
            remaining = sum(row["session"] == session and row["status"] == "pending"
                            and row["id"] in indexed and key not in media_keys
                            for key, row in self.reconnect_state["rows"].items())
            return {"pending": remaining, "sent": False, "reason": "media_input_unavailable",
                    "media_deferred": len(media), "retry": bool(remaining)}
        if dry_run:
            return {"pending": len(candidates), "sent": False, "eligible": True}
        manual_paused = session != OWNER_UMO and qq_chat_paused()
        if session != OWNER_UMO and (manual_paused or peak_active()):
            return {"pending": len(candidates), "sent": False,
                    "reason": "qq_chat_paused" if manual_paused else "peak_tariff"}
        # Do not replay old commands through handlers: the old turn might already
        # have posted a comment, paid for generation, or started a background job.
        private = session == OWNER_UMO
        if private:
            persona, context = await self.private_chat_context()
        else:
            selected = self.context.persona_manager.get_persona_v3_by_id("肥鱼")
            persona, context = (selected or {}).get("prompt", "你是肥鱼。"), []
        pending = [{"time": row["at"], "text": message_text(indexed[row["id"]])[:4000]}
                   for _, row in candidates[-20:]]
        instructions = (
            '请仅依据当前公开来源及本地明确配置的信息处理请求；不要推测个人身份或私人偏好。'
        )
        if not private:
            instructions += "本次在群里只回复主人，不能引用私聊记录或任何主人隐私；只使用本次群消息。"
        if business_paused():
            return {"pending": len(candidates), "sent": False, "reason": "peak_tariff", "retry": True}
        answer = await asyncio.wait_for(self.context.get_provider_by_id("deepseek-flash").text_chat(
            prompt=json.dumps({"pending": pending, "recent_private_context": context}, ensure_ascii=False),
            contexts=[], system_prompt=persona + instructions, session_id=session,
            request_max_retries=1), 75)
        text = answer.completion_text.strip()
        if business_paused():
            return {"pending": len(candidates), "sent": False, "reason": "peak_tariff", "retry": True}
        if not text or len(text) > 2000:
            return {"pending": len(candidates), "sent": False, "reason": "empty_or_oversize_response"}
        # Generation may overlap a successful normal reply or a new owner turn.
        if (any(row["status"] != "pending" for _, row in candidates)
                or time.time() - self._reconnect_last_activity.get(session, 0) < GRACE
                or self.reconnect_busy(session)):
            return {"pending": len(candidates), "sent": False, "reason": "conversation_changed"}
        manual_paused = not private and qq_chat_paused()
        if not private and (manual_paused or peak_active()):
            return {"pending": len(candidates), "sent": False,
                    "reason": "qq_chat_paused" if manual_paused else "peak_tariff"}
        anchor = candidates[-1][1]["id"]
        attempt = {"session": session, "keys": [key for key, _ in candidates], "at": time.time(),
                   "hash": fingerprint(text), "anchor": anchor, "status": "sending"}
        self.reconnect_state["attempts"].append(attempt)
        for _, row in candidates:
            row["status"] = "unknown"  # Durable reservation before the send.
        self.save_reconnect()
        try:
            result = await self.reconnect_action(adapter, "send_private_msg" if private else "send_group_msg",
                **({"user_id": int(OWNER)} if private else {"group_id": int(session.rsplit(":", 1)[-1])}),
                message=[{"type": "reply", "data": {"id": anchor}}, {"type": "text", "data": {"text": text}}])
            if not isinstance(result, dict) or not result.get("message_id"):
                raise ValueError("missing_send_receipt")
        except QQChatPaused:
            attempt.update(status="blocked", reason="qq_chat_paused")
            for _, row in candidates:
                if row["status"] == "unknown":
                    row["status"] = "pending"
            self.save_reconnect()
            return {"pending": len(candidates), "sent": False, "reason": "qq_chat_paused"}
        except PeakTariffPaused:
            attempt.update(status="blocked", reason="peak_tariff")
            for _, row in candidates:
                if row["status"] == "unknown":
                    row["status"] = "pending"
            self.save_reconnect()
            return {"pending": len(candidates), "sent": False, "reason": "peak_tariff"}
        except BaseException:
            attempt["status"] = "unknown"
            self.save_reconnect()
            raise
        attempt.update(status="sent", message_id=str(result["message_id"]))
        for key, _ in candidates:
            self.reconnect_reply_sent(key)
        # Preserve the actual catch-up in the normal private conversation too.
        if private:
            try:
                mgr = self.context.conversation_manager
                cid = await mgr.get_curr_conversation_id(session)
                if cid:
                    await mgr.add_message_pair(cid,
                        {"role": "user", "content": "[此前未回复消息]\n" + "\n".join(x["text"] for x in pending)},
                        {"role": "assistant", "content": text})
            except Exception as exc:  # noqa: BLE001 - External history storage must not undo delivery.
                logger.warning("[肥鱼重连] 补回已送达，历史追加失败：%s", type(exc).__name__)
        return {"pending": len(candidates), "sent": True, "replied": len(candidates)}

    async def check_reconnect(self, adapter, dry_run=False):
        if business_paused():
            return {OWNER_UMO: {"pending": 0, "sent": False, "reason": "peak_tariff", "retry": True}}
        async with self._reconnect_lock:
            results = {}
            for session in SESSIONS:
                try:
                    if business_paused():
                        results[session] = {"sent": False, "reason": "peak_tariff", "retry": True}
                        continue
                    manual_paused = session != OWNER_UMO and qq_chat_paused()
                    if session != OWNER_UMO and (manual_paused or peak_active()):
                        results[session] = {"sent": False,
                                            "reason": "qq_chat_paused" if manual_paused else "peak_tariff",
                                            "retry": True}
                        continue
                    scan_started = time.time()
                    messages = await self.reconnect_history(adapter, session)
                    results[session] = await self.recover_reconnect_session(adapter, session, messages, dry_run)
                    if not dry_run and not results[session].get("retry"):
                        self.reconnect_state["checkpoints"][session] = scan_started
                except Exception as exc:  # noqa: BLE001 - Isolate each external history scan.
                    results[session] = {"sent": False, "reason": type(exc).__name__, "retry": True}
                    logger.warning("[肥鱼重连] 历史补查未完成：%s", type(exc).__name__)
            self.reconnect_state["last_scan"] = time.time()
            self._reconnect_status["last_result"] = results
            self.save_reconnect()
            return results

    async def reconnect_monitor(self):
        """Startup, OneBot reconnect, QQ offline->online, and bounded retries."""
        previous = False
        failures = 0
        while True:
            try:
                adapter = self.install_reconnect_adapter()
                status = await self.reconnect_action(adapter, "get_status") if adapter else {}
                online = bool(status.get("online") and status.get("good", True))
                self._reconnect_status["connected"] = online
                if online and (not previous or self._reconnect_requested or
                               (self._reconnect_retry_at and time.time() >= self._reconnect_retry_at)):
                    self._reconnect_requested = False
                    await asyncio.sleep(10)  # Let native QQ history sync finish.
                    results = await self.check_reconnect(adapter)
                    retry = any(r.get("pending") or r.get("retry") for r in results.values())
                    failures = min(failures + 1, 5) if retry else 0
                    self._reconnect_retry_at = time.time() + min(900, 30 * 2**failures) if retry else 0
                previous = online
            except Exception as exc:  # noqa: BLE001 - Keep the external transport monitor alive.
                previous = False
                self._reconnect_status.update(connected=False, error=type(exc).__name__)
            await asyncio.sleep(15)
