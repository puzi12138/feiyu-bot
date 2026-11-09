"""Durable, owner-only capability questions without chat-frequency gates.

This module asks a question; it never changes a permission or stores a secret.
Call ``observe_owner_permission_reply`` only from the actual inbound transport
hook. The reply reference is context for later owner conversation, not a grant.
"""

import asyncio
import hashlib
import ipaddress
import json
import re
import time
from urllib.parse import unquote, urlsplit, urlunsplit

OWNER = "0"
GROUP = "0"
OWNER_SESSIONS = {f"snowluma-qq:FriendMessage:{OWNER}", f"snowluma-qq:GroupMessage:{GROUP}"}
MAX_REQUESTS = 256
_SECRET = re.compile(
    r"\bsk-[A-Za-z0-9_-]{12,}|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/-]{8,}"
    r"|(?:password|passwd|cookie|sessdata|bili_jct|p_skey|skey|authorization|"
    r"(?:access[_ -]?|refresh[_ -]?)?token|api[_ -]?key|密码|口令|验证码)"
    r"[\"']?\s*[:=：]\s*[^\s,，;；]+"
    r"|\b1[3-9]\d{9}\b|\b\d{17}[\dXx]\b",
    re.IGNORECASE,
)
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _owner_event(event):
    """Trust the transport sender and session, never message text or model IDs."""
    try:
        return str(event.get_sender_id()) == OWNER and str(event.unified_msg_origin) in OWNER_SESSIONS
    except (AttributeError, TypeError):
        return False


def _text(value, limit):
    if not isinstance(value, str):
        raise TypeError("invalid_request_text")
    result = re.sub(r"\s+", " ", value).strip()
    if not result or len(result) > limit or _CONTROLS.search(value):
        raise ValueError("invalid_request_text")
    if _SECRET.search(unquote(result)):
        raise ValueError("credentials_not_accepted")
    return result


def _source(value):
    """Retain only a public HTTPS origin/path, without signed query parameters."""
    if not value:
        return ""
    if not isinstance(value, str) or len(value) > 1800 or _SECRET.search(unquote(value)) or _CONTROLS.search(value):
        raise ValueError("invalid_or_sensitive_source_url")
    try:
        parsed = urlsplit(value)
        host = str(parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or not host or "." not in host
                or parsed.username or parsed.password or parsed.port not in {None, 443}
                or host.endswith((".local", ".localhost", ".internal", ".lan"))):
            raise ValueError("invalid_or_sensitive_source_url")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("invalid_or_sensitive_source_url")
        safe = urlunsplit(("https", host, parsed.path or "/", "", ""))
        if len(safe) > 800:
            raise ValueError("invalid_or_sensitive_source_url")
        return safe
    except ValueError:
        raise ValueError("invalid_or_sensitive_source_url") from None


def _request(action, reason, needed_permission, source_url):
    values = {
        "action": _text(action, 180),
        "reason": _text(reason, 600),
        "needed_permission": _text(needed_permission, 360),
        "source_url": _source(source_url),
    }
    canonical = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    body = ("我有件好奇的事想和你商量：\n"
            f"想做：{values['action']}\n"
            f"有趣在哪：{values['reason']}\n"
            f"还缺：{values['needed_permission']}\n"
            "你可以直接回复我；需要登录或配置时在本机/官方页面完成即可。")
    if values["source_url"]:
        body += "\n线索：" + values["source_url"]
    return key, values, body


def _receipt(row, *, duplicate=False):
    status = row["status"]
    return {
        "ok": status == "sent",
        "status": status,
        "reason": "already_requested" if duplicate else ("owner_notified" if status == "sent" else "delivery_needs_verification"),
        "request_id": row["key"],
        "notified": status == "sent",
        "permission_granted": False,
        **({"message_id": row["message_id"]} if row.get("message_id") else {}),
        "message": "已私聊主人，等待主人回复。" if status == "sent" else "通知结果待核实，未重复发送，也未授予权限。",
    }


class PermissionRequests:
    """A private conversation channel for missing capabilities, not a grant API."""

    def init_permission_requests(self):
        self.permission_request_lock = asyncio.Lock()
        ledger = self.state.setdefault("permission_requests", [])
        changed = False
        for row in ledger:
            if row.get("status") == "pending":
                row["status"] = "unknown"
                changed = True
        if changed:
            self.save()

    async def ask_owner_permission(self, event, action, reason, needed_permission, source_url="", dry_run=False):
        """Only actual owner private messages or the configured group may ask."""
        if not _owner_event(event):
            return {"ok": False, "reason": "owner_only", "permission_granted": False}
        return await self.notify_capability_gap(action, reason, needed_permission, source_url, dry_run=dry_run)

    async def notify_capability_gap(self, action, reason, needed_permission, source_url="", *, dry_run=False):
        """Trusted background entry point. Never expose it as an unauthenticated API."""
        try:
            key, summary, body = _request(action, reason, needed_permission, source_url)
        except (TypeError, ValueError) as error:
            return {"ok": False, "reason": str(error), "permission_granted": False}
        if dry_run:
            return {"ok": True, "status": "preview", "dry_run": True, "notified": False,
                    "request_id": key, "summary": summary, "text": body, "permission_granted": False}
        if not hasattr(self, "permission_request_lock"):
            self.init_permission_requests()
        async with self.permission_request_lock:
            ledger = self.state.setdefault("permission_requests", [])
            existing = next((row for row in ledger if row.get("key") == key), None)
            if existing is not None:
                return _receipt(existing, duplicate=True)
            # Never evict an uncertain receipt then unknowingly repeat its send.
            if len(ledger) >= MAX_REQUESTS:
                return {"ok": False, "reason": "permission_request_ledger_full", "permission_granted": False,
                        "message": "已有较多权限问题记录，请先整理；本次未发送，也未授予权限。"}
            adapter = self.context.get_platform_inst("snowluma-qq")
            if adapter is None:
                return {"ok": False, "reason": "qq_unavailable", "permission_granted": False}
            try:
                client = adapter.get_client()
            except Exception:  # noqa: BLE001 - external adapter errors can include credentials
                return {"ok": False, "reason": "qq_unavailable", "permission_granted": False}
            row = {"key": key, "at": time.time(), "status": "pending", "summary": summary}
            ledger.append(row)
            self.save()
            try:
                result = await asyncio.wait_for(client.call_action(
                    "send_private_msg", user_id=int(OWNER),
                    message=[{"type": "text", "data": {"text": body}}]), 40)
                message_id = result.get("message_id") if isinstance(result, dict) else None
                valid_id = (isinstance(message_id, (int, str)) and not isinstance(message_id, bool)
                            and re.fullmatch(r"-?[1-9]\d{0,19}", str(message_id)) is not None)
                row["status"] = "sent" if valid_id else "unknown"
                if valid_id:
                    row["message_id"] = str(message_id)
            except BaseException as error:
                row["status"] = "unknown"
                self.save()
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return _receipt(row)
            self.save()
            return _receipt(row)

    def observe_owner_permission_reply(self, event):
        """Record a real new owner reply reference; never infer or grant access.

        This is called by the inbound hook, not a model tool. It deliberately
        stores no message text and does not retire duplicate-request barriers.
        """
        if not _owner_event(event):
            return False
        message = getattr(event, "message_obj", None)
        message_id = str(getattr(message, "message_id", "") or "")
        try:
            stamp = float(getattr(message, "timestamp", 0))
        except (TypeError, ValueError):
            return False
        if not re.fullmatch(r"-?[1-9]\d{0,19}", message_id) or not 0 < stamp <= time.time() + 30:
            return False
        ledger = self.state.get("permission_requests", [])
        unanswered = [row for row in ledger if row.get("status") in {"sent", "unknown"}
                      and not row.get("owner_reply_id") and stamp >= int(row.get("at", 0))]
        if not unanswered or any(row.get("owner_reply_id") == message_id for row in ledger):
            return False
        # The actual owner can answer normally. This marker is not consent and
        # cannot change plugin configuration or authorize the requested action.
        row = max(unanswered, key=lambda item: item.get("at", 0))
        row.update(owner_reply_id=message_id, owner_reply_at=time.time())
        self.save()
        return True
