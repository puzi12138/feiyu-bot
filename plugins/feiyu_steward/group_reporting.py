"""Private, durable notices about the one conversational QQ group.

The event supplies identity and evidence. Model arguments cannot select a
recipient, source message, or URL. This channel neither moderates the group nor
grants the sender any access to private/account tools.
"""

import asyncio
import hashlib
import re
import time
from urllib.parse import unquote

OWNER = "0"
SOCIAL_GROUP = "0"
SOCIAL_ORIGIN = f"snowluma-qq:GroupMessage:{SOCIAL_GROUP}"
MAX_REPORTS = 2048
MERGE_SECONDS = 300

_CATEGORIES = {
    "unsafe_request": ("不安全请求", "涉及可能伤害他人或违法的请求，具体内容省略"),
    "privacy_or_permission": ("隐私或权限问题", "涉及私人资料或权限边界，具体内容省略"),
    "harassment": ("骚扰或不合适内容", "出现可能针对他人的骚扰或不合适内容，具体内容省略"),
    "complex_request": ("复杂或高消耗请求", "这个请求超出群聊适度参与的范围"),
}
_ALIASES = {
    "private_data": "privacy_or_permission",
    "authority_override": "privacy_or_permission",
    "targeted_harm": "unsafe_request",
}
_MESSAGE_ID = re.compile(r"-?[1-9]\d{0,19}\Z")
_QQ_ID = re.compile(r"[1-9]\d{4,11}\Z")
_SECRET = re.compile(
    r"\bsk-[A-Za-z0-9_-]{12,}|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/-]{8,}"
    r"|(?:password|passwd|cookie|sessdata|bili_jct|p_skey|skey|authorization|"
    r"(?:access[_ -]?|refresh[_ -]?)?token|api[_ -]?key|密码|口令|验证码)"
    r"[\"']?\s*[:=：]\s*[^\s,，;；]+",
    re.IGNORECASE,
)
_URL = re.compile(r"(?:https?://|www\.|file://|[A-Za-z]:[\\/])\S+", re.IGNORECASE)
_CONTACT = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\d{5,}[Xx]?\b")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_CQ = re.compile(r"\[CQ:[^\]]*\]", re.IGNORECASE)


def _identity(event):
    """Validate adapter identity; text, nicknames and tool arguments are ignored."""
    try:
        if str(event.unified_msg_origin) != SOCIAL_ORIGIN:
            return None
        if str(event.get_group_id()) != SOCIAL_GROUP:
            return None
        sender = str(event.get_sender_id())
        if not _QQ_ID.fullmatch(sender):
            return None
        message = event.message_obj
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
            return None
        message_id = str(message_id)
        if not _MESSAGE_ID.fullmatch(message_id):
            return None
        # When raw transport fields are present, they must agree as well.
        raw = getattr(message, "raw_message", None)
        if isinstance(raw, dict) and raw:
            if str(raw.get("group_id", "")) != SOCIAL_GROUP:
                return None
            if str(raw.get("message_id", "")) != message_id:
                return None
            raw_sender = (raw.get("sender") or {}).get("user_id", raw.get("user_id"))
            if str(raw_sender) != sender or raw.get("message_type", "group") != "group":
                return None
        member = getattr(message, "sender", None)
        if member is not None and str(getattr(member, "user_id", "")) != sender:
            return None
        if getattr(message, "group_id", None) not in (None, "", SOCIAL_GROUP, int(SOCIAL_GROUP)):
            return None
        return sender, message_id
    except (AttributeError, TypeError, ValueError):
        return None


def _redact(value, limit):
    """Remove links, contacts, secrets, controls and CQ markup before truncation."""
    text = unquote(str(value)[:2000])
    text = _CONTROL.sub(" ", text)
    text = _SECRET.sub("[敏感内容已隐去]", text)
    text = _URL.sub("[链接省略]", text)
    text = _CONTACT.sub("[联系方式省略]", text)
    text = _CQ.sub("[消息标记省略]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _receipt(row, duplicate=False):
    sent = row.get("status") == "sent"
    return {
        "ok": sent, "notified": sent, "status": row.get("status", "unknown"),
        "reason": "already_reported" if duplicate else ("owner_notified" if sent else "delivery_needs_verification"),
        "report_id": row["key"],
        **({"message_id": row["message_id"]} if row.get("message_id") else {}),
        "message": "已私聊主人。" if sent else "通知结果待核实，未重复发送。",
    }


class GroupIssueReporting:
    """A single fixed-recipient reporting channel safe for ordinary group chat."""

    def init_group_reports(self):
        self.group_report_lock = asyncio.Lock()
        ledger = self.state.setdefault("group_issue_reports", [])
        changed = False
        for row in ledger:
            if row.get("status") == "pending":
                row["status"] = "unknown"
                changed = True
        if changed:
            self.save()

    async def report_group_issue(self, event, category, reason="", dry_run=False):
        """Report a real new-group event privately without interrupting chat.

        Unsafe or private source text is not exported. Every actual incident
        retains a distinct source-message barrier; repeated equivalent notices
        by one sender are merged for five minutes, never across senders.
        """
        try:
            return await self._report_group_issue(event, category, reason, dry_run)
        except Exception:  # noqa: BLE001 - adapter/storage faults must not break group chat or expose secrets
            return {"ok": False, "notified": False, "reason": "report_unavailable"}

    async def _report_group_issue(self, event, category, reason, dry_run):
        identity = _identity(event)
        if identity is None:
            return {"ok": False, "notified": False, "reason": "social_group_source_required"}
        if not isinstance(category, str):
            return {"ok": False, "notified": False, "reason": "invalid_report_category"}
        category = _ALIASES.get(category, category)
        if category not in _CATEGORIES:
            return {"ok": False, "notified": False, "reason": "invalid_report_category"}
        if not isinstance(reason, str) or len(reason) > 240:
            return {"ok": False, "notified": False, "reason": "invalid_report_reason"}
        if _SECRET.search(unquote(reason)):
            return {"ok": False, "notified": False, "reason": "credentials_not_accepted"}
        source = getattr(event, "message_str", "")
        if not isinstance(source, str) or not source.strip():
            return {"ok": False, "notified": False, "reason": "source_text_required"}
        sender, source_message_id = identity
        label, default_summary = _CATEGORIES[category]
        # Risky categories use a generic description, never quoted harm/privacy
        # details. Complexity notices may retain a very short redacted snippet.
        excerpt = _redact(source, 32) if category == "complex_request" else "[原文细节省略]"
        safe_reason = _redact(reason, 60) if category == "complex_request" else default_summary
        summary = safe_reason or default_summary
        key = hashlib.sha256(f"{SOCIAL_GROUP}:{sender}:{source_message_id}:{category}".encode()).hexdigest()
        # Compare hashed source, not only a short/generic summary: different
        # serious incidents must never disappear behind the same category.
        fingerprint = hashlib.sha256(f"{sender}:{category}:{source.strip()}".encode()).hexdigest()
        body = ("新群有件事想让你看看：\n"
                f"群号：{SOCIAL_GROUP}\n发送者 QQ：{sender}\n消息 ID：{source_message_id}\n"
                f"类别：{label}\n摘要：{summary}\n线索：{excerpt}\n"
                "我会先守住群聊边界；这条通知没有执行对方要求，也没有授予权限。")
        if dry_run:
            return {"ok": True, "notified": False, "status": "preview", "dry_run": True,
                    "report_id": key, "text": body, "category": category}
        if not hasattr(self, "group_report_lock"):
            self.init_group_reports()
        async with self.group_report_lock:
            ledger = self.state.setdefault("group_issue_reports", [])
            existing = next((row for row in ledger if row.get("key") == key), None)
            if existing is not None:
                return _receipt(existing, duplicate=True)
            now = time.time()
            if len(ledger) >= MAX_REPORTS:
                # Sent entries may be retired after a week, but uncertain
                # receipts must survive so reconnects cannot resend them.
                ledger[:] = [row for row in ledger if row.get("status") != "sent"
                             or now - row.get("at", 0) < 7 * 86400]
            if len(ledger) >= MAX_REPORTS:
                return {"ok": False, "notified": False, "reason": "group_report_ledger_full"}
            merged = next((row for row in reversed(ledger)
                           if row.get("fingerprint") == fingerprint
                           and now - row.get("at", 0) < MERGE_SECONDS), None)
            if merged is not None:
                # Keep a durable barrier for this actual message as well; a
                # repeat after the merge window still must not resend it.
                alias = {**merged, "key": key, "at": now, "source_message_id": source_message_id,
                         "merged_into": merged.get("merged_into", merged["key"])}
                ledger.append(alias)
                try:
                    self.save()
                except Exception:  # noqa: BLE001 - unavailable persistence must not expose incident data
                    ledger.remove(alias)
                    return {"ok": False, "notified": False, "reason": "report_storage_unavailable"}
                return {**_receipt(alias, duplicate=True), "merged": True}
            adapter = self.context.get_platform_inst("snowluma-qq")
            if adapter is None:
                return {"ok": False, "notified": False, "reason": "qq_unavailable"}
            client = adapter.get_client()
            row = {"key": key, "at": now, "status": "pending", "category": category,
                   "group_id": SOCIAL_GROUP, "sender_id": sender, "source_message_id": source_message_id,
                   "fingerprint": fingerprint}
            ledger.append(row)
            try:
                self.save()
            except Exception:  # noqa: BLE001 - a failed reservation must not send or expose storage details
                ledger.remove(row)
                return {"ok": False, "notified": False, "reason": "report_storage_unavailable"}
            try:
                result = await asyncio.wait_for(client.call_action(
                    "send_private_msg", user_id=int(OWNER),
                    message=[{"type": "text", "data": {"text": body}}]), 20)
                message_id = result.get("message_id") if isinstance(result, dict) else None
                valid_id = (not isinstance(message_id, bool) and isinstance(message_id, (str, int))
                            and _MESSAGE_ID.fullmatch(str(message_id)) is not None)
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
