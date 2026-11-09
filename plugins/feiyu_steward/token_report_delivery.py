"""Cockpit-only owner reports with durable send barriers and frozen windows."""

import asyncio
import copy
import hashlib
import re
from contextlib import nullcontext
from datetime import datetime

from .admin_info import owner_private
from .native_private_transport import native_delivery, producer_operation
from .policy import TZ
from .token_report_gate import freeze_report_window
from .bill_display_policy import display_bill_text
from .cockpit_token_report import report_readiness
from .token_report_queue import (
    due_report_keys,
    new_pending_report,
    pending_report_feedback,
    queue_key,
    validate_pending_report,
)
from .peak_runtime import owner_scope_active

OWNER = "0"
BOT_QQ = "0"
LEGACY_QUEUE_STATE = "token_usage_report_queue"
QUEUE_STATE = "cockpit_token_usage_report_queue_v1"
RETRY_SECONDS = 300
UNSUPPORTED_RETRY_SECONDS = 6 * 3600
MAX_QUEUE = 256


def _body(value):
    if not isinstance(value, dict):
        return {}
    if "retcode" in value:
        return value.get("data", {}) if value.get("retcode") == 0 and isinstance(value.get("data"), dict) else {}
    return value


def _receipt_response(row, *, duplicate=False):
    status = row.get("status", "unknown")
    return {"ok": status == "sent", "sent": status == "sent", "status": status,
            "duplicate": duplicate, "format": row.get("format", "image"),
            "message_id": row.get("message_id"), "native_message_id": row.get("native_message_id"),
            "native_confirmed": row.get("native_confirmed", False),
            "note": ("报告已发送，无需重复整份报告。" if status == "sent"
                     else "报告发送结果尚未确认，已保留记录，不会自动重发。")}


def _pending_response(*, queued=True, reason_codes=()):
    allowed = {"codex_summary_unavailable", "codex_window_mismatch", "codex_usage_invalid"}
    reasons = [code for code in reason_codes if code in allowed]
    return {"ok": False, "sent": False, "queued": queued, "status": "cockpit_source_unavailable",
            "reason_codes": reasons,
            "note": "用量记录暂时不可用。"
                    + ("本次请求已保留，只在原时段重新核对；历史缺失不保证能补齐。" if queued else "")}


class TokenReportDelivery:
    """Mixin; the owning plugin supplies its existing durable state/save/lock."""

    def _report_receipts(self, row, *, create=False):
        name = "token_usage_reports" if row["source"] == "daily" else "token_usage_manual_images"
        if create:
            return self.state.setdefault(name, {})
        receipts = self.state.get(name, {})
        return receipts if isinstance(receipts, dict) else {}

    def _report_queue_snapshot(self, key):
        try:
            queue = self.state.get(QUEUE_STATE, {})
            return validate_pending_report(queue.get(key) if isinstance(queue, dict) else None)
        except ValueError:
            return None

    async def request_verified_token_report(self, event, result, *, format="image", source="manual",
                                            daily_day=None, now=None, dry_run=False):
        if source == "manual" and not owner_private(event):
            return {"ok": False, "reason": "owner_private_only"}
        current = now or datetime.now(TZ)
        try:
            if current.tzinfo is None or format not in {"image", "text"} or source not in {"manual", "daily"}:
                raise ValueError
            snapshot = copy.deepcopy(result)
            frozen = freeze_report_window(snapshot.get("summary"))
            if source == "daily":
                if frozen.get("day") != daily_day:
                    raise ValueError
                identity, receipt_key = daily_day, daily_day
            else:
                identity = getattr(event, "get_message_id", lambda: "")()
                identity = identity or getattr(getattr(event, "message_obj", None), "message_id", "")
                identity = str(identity or id(event))
                # Reuse the old image receipt key even for text requests, so
                # changing presentation cannot bypass a preexisting send.
                receipt_key = hashlib.sha256(f"{identity}:token_report_image".encode()).hexdigest()
            key = queue_key(source, identity, format)
            row = new_pending_report(frozen, source, format, current.timestamp(), receipt_key)
        except (ValueError, TypeError, OverflowError, AttributeError):
            return {"ok": False, "sent": False, "reason": "invalid_report_window",
                    "note": "查询时段无法核实，暂不发送报告。"}
        async with self.token_usage_lock:
            receipts = self._report_receipts(row)
            previous = receipts.get(receipt_key)
            if isinstance(previous, dict):
                queue = self.state.get(QUEUE_STATE, {})
                if not dry_run and isinstance(queue, dict) and key in queue:
                    queue.pop(key)
                    self.save()
                return _receipt_response(previous, duplicate=True)
            legacy = self.state.get(LEGACY_QUEUE_STATE, {})
            if isinstance(legacy, dict) and key in legacy:
                return {"ok": True, "sent": False, "queued": False, "status": "legacy_combined_paused",
                        "note": "旧报告请求已暂停并保留，请发起新的查询。"}
            queue = self.state.get(QUEUE_STATE, {})
            if not isinstance(queue, dict):
                return {"ok": False, "sent": False, "reason": "pending_report_invalid"}
            if key in queue:
                old = self._report_queue_snapshot(key)
                if old is None:
                    return {"ok": False, "sent": False, "reason": "pending_report_invalid"}
                if old["status"] != "waiting":
                    return _receipt_response(old, duplicate=True)
                if not dry_run and old["next_check"] > current.timestamp():
                    return _pending_response(reason_codes=old["last_reason_codes"])
                # An identical owner event always keeps its first window.
                row = old
                snapshot = copy.deepcopy(self.token_usage_frozen_result(row["window"], now=current))
            readiness = report_readiness(snapshot.get("summary"), snapshot.get("codex_summary"), window=row["window"])
            if dry_run:
                if not readiness["ready"]:
                    return _pending_response(queued=False, reason_codes=readiness["reason_codes"])
                path = await asyncio.to_thread(self.token_usage_report_image, snapshot) if format == "image" else None
                return {"ok": True, "status": "preview", "sent": False,
                        "format": "image" if path else "text", "image_path": str(path) if path else None,
                        "text": display_bill_text(snapshot.get("text", ""))}
            if key not in queue:
                if len(queue) >= MAX_QUEUE:
                    return {"ok": False, "sent": False, "reason": "pending_report_capacity",
                            "note": "待核实报告已保留，暂无法登记新的报告。"}
                self.state.setdefault(QUEUE_STATE, {})[key] = row
                try:
                    self.save()
                except Exception:  # noqa: BLE001 - storage failure must prevent dispatch
                    self.state[QUEUE_STATE].pop(key, None)
                    return {"ok": False, "sent": False, "reason": "report_intent_persist_failed"}
            return await self._dispatch_pending_token_report_locked(key, row, snapshot, readiness, current)

    async def _dispatch_pending_token_report_locked(self, key, row, result, readiness, current):
        queue = self.state[QUEUE_STATE]
        receipts = self._report_receipts(row)
        previous = receipts.get(row["receipt_key"])
        if isinstance(previous, dict):
            queue.pop(key, None)
            self.save()
            return _receipt_response(previous, duplicate=True)
        if not readiness["ready"]:
            delay = RETRY_SECONDS
            row.update(attempts=row["attempts"] + 1, next_check=current.timestamp() + delay,
                       last_reason_codes=readiness["reason_codes"])
            queue[key] = row
            try:
                self.save()
            except Exception:  # noqa: BLE001 - storage failure must prevent dispatch
                return {"ok": False, "sent": False, "reason": "report_intent_persist_failed"}
            return _pending_response(reason_codes=readiness["reason_codes"])
        # The same copied snapshot which passed the gate is the only payload
        # given to the renderer/text path. No live re-read can shift its window.
        path = await asyncio.to_thread(self.token_usage_report_image, result) if row["format"] == "image" else None
        text = display_bill_text(result.get("text"))
        if not path and (not isinstance(text, str) or not text):
            row["next_check"] = current.timestamp() + RETRY_SECONDS
            self.save()
            return {"ok": False, "sent": False, "queued": True, "reason": "verified_render_unavailable"}
        unavailable = "qq_unavailable"
        try:
            adapter = self.context.get_platform_inst("snowluma-qq")
            client = adapter.get_client()
            login = _body(await asyncio.wait_for(client.call_action("get_login_info"), 20))
            if str(login.get("user_id")) != BOT_QQ:
                unavailable = "qq_identity_unavailable"
                raise ValueError("qq_identity_unavailable")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any failed QQ preflight keeps the report pending
            row["next_check"] = current.timestamp() + RETRY_SECONDS
            self.save()
            return {"ok": False, "sent": False, "queued": True, "reason": unavailable,
                    "note": "报告已核实并保留，QQ恢复后再发送。"}
        receipt = {"status": "intent", "at": current.timestamp(), "format": "image" if path else "text"}
        if row["source"] == "manual" and owner_scope_active():
            receipt["owner_direct_peak_v1"] = True
        receipts = self._report_receipts(row, create=True)
        receipts[row["receipt_key"]] = receipt
        row["status"] = "intent"
        queue[key] = row
        try:
            self.save()
        except Exception:  # noqa: BLE001 - storage failure must prevent dispatch
            receipts.pop(row["receipt_key"], None)
            row["status"] = "waiting"
            return {"ok": False, "sent": False, "reason": "report_intent_persist_failed"}
        try:
            transport = getattr(self, "_native_private_transport", None)
            use_native = bool(transport is not None and transport.enabled
                              and (path is None or getattr(transport, "enabled_media", False)))
            producer = "token-report-daily" if row["source"] == "daily" else "token-report-manual"
            scope = (native_delivery(producer_operation(producer, row["receipt_key"], slot="report"))
                     if use_native else nullcontext())
            message = ([{"type": "image", "data": {"file": path.as_uri()}}] if path
                       else [{"type": "text", "data": {"text": text}}])
            with scope:
                response = _body(await asyncio.wait_for(client.call_action(
                    "send_private_msg", user_id=int(OWNER), message=message), 40))
            message_id = response.get("message_id")
            valid = (type(message_id) in (str, int) and re.fullmatch(r"-?[1-9]\d{0,19}", str(message_id)))
            native_id = response.get("native_message_id") if use_native else None
            native_confirmed = bool(use_native and response.get("native_delivery_confirmed") is True
                                    and isinstance(native_id, str) and re.fullmatch(r"[1-9]\d{0,23}", native_id))
            receipt["status"] = "sent" if valid or native_confirmed else "unknown"
            if valid:
                receipt["message_id"] = str(message_id)
            if native_confirmed:
                receipt.update(native_message_id=native_id, native_confirmed=True)
        except BaseException as error:
            receipt["status"] = "unknown"
            row["status"] = "unknown"
            self.save()
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
        row["status"] = receipt["status"]
        if receipt["status"] == "sent":
            # The permanent legacy receipt remains the duplicate barrier;
            # completed work must not consume the bounded waiting capacity.
            queue.pop(key, None)
        self.save()
        return _receipt_response(receipt)

    async def retry_pending_token_reports(self, *, now=None, limit=4):
        """Retry verification for old frozen windows, never an uncertain send."""
        current = now or datetime.now(TZ)
        if current.tzinfo is None:
            raise ValueError("timezone_required")
        results = []
        async with self.token_usage_lock:
            queue = self.state.get(QUEUE_STATE, {})
            for key in due_report_keys(queue, current.timestamp(), limit):
                row = self._report_queue_snapshot(key)
                if row is None or row["status"] != "waiting":
                    continue
                previous = self._report_receipts(row).get(row["receipt_key"])
                if isinstance(previous, dict):
                    queue.pop(key, None)
                    self.save()
                    results.append(_receipt_response(previous, duplicate=True))
                    continue
                result = copy.deepcopy(self.token_usage_frozen_result(row["window"], now=current))
                ready = report_readiness(result.get("summary"), result.get("codex_summary"), window=row["window"])
                results.append(await self._dispatch_pending_token_report_locked(key, row, result, ready, current))
        return {"ok": True, "checked": len(results),
                "sent": sum(r.get("sent") is True and not r.get("duplicate") for r in results)}
