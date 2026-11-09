"""Owner-approved NetEase listen-together presence, without audio or playback.

Only verified inbound QQ messages or the owner's explicit sender allowlist can
approve an invitation. The local service holds login cookies; this module never
exposes a model-facing join tool.
An uncertain join/leave is reserved durably and is never retried automatically.
"""

# External transports can include credentials in exceptions. Never retain them.
# ruff: noqa: BLE001

import asyncio

from .peak_policy import automatic_peak_paused
from .peak_policy import PeakTariffPaused
from .peak_runtime import business_paused
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import aiohttp

from .group_policy import GROUP, OWNER, SOCIAL_GROUP
from .policy import OWNER_UMO

SELF = "0"
TRUSTED_FRIEND = "0"
SERVICE = "http://127.0.0.1:16300"
SERVICE_TOKEN = Path("D:/QQBot/config/netease-listen/service-token")
MAX_INVITATIONS = 256
APPROVAL_WINDOW = 600
MESSAGE_WINDOW = 300
HEARTBEAT_INTERVAL = 15
JOIN_VERIFY_GRACE = 120
_ID = re.compile(r"-?[1-9][0-9]{0,19}\Z")
_ROOM = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_USER = re.compile(r"[1-9][0-9]{0,19}\Z")
_COMMAND = re.compile(r"^/?一起听(?:\s+(.*))?$", re.DOTALL)
_URL = re.compile(r"(?:https?://|orpheus://|cloudmusic://)[^\s<>\"'，。！；]+", re.IGNORECASE)
_HOSTS = frozenset({"music.163.com", "y.music.163.com", "st.music.163.com"})
_AWAITING = frozenset({"awaiting_owner"})


def _message_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value) if _ID.fullmatch(str(value)) else ""


def _receipt(response):
    if not isinstance(response, dict):
        return ""
    if "retcode" in response:
        if response.get("retcode") != 0 or response.get("status") not in {None, "ok"}:
            return ""
        response = response.get("data")
        if not isinstance(response, dict):
            return ""
    return _message_id(response.get("message_id"))


def _json_strings(value, depth=0):
    """Read bounded QQ JSON card values, without fetching any URL."""
    if depth > 8:
        return []
    if isinstance(value, str):
        return [value] if len(value) <= 8192 else []
    if isinstance(value, dict) and len(value) <= 100:
        return [text for child in value.values() for text in _json_strings(child, depth + 1)]
    if isinstance(value, list) and len(value) <= 100:
        return [text for child in value for text in _json_strings(child, depth + 1)]
    return []


def parse_listen_invitation(value):
    """Parse a supported first-party invitation URL; never resolve remote URLs."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        return None
    try:
        parsed = urlsplit(value.rstrip("）)】]"))
        if (parsed.scheme not in {"https", "http", "orpheus", "cloudmusic"}
                or parsed.username or parsed.password):
            return None
        if parsed.scheme in {"http", "https"}:
            if parsed.hostname not in _HOSTS or parsed.port not in {None, 80, 443}:
                return None
        elif parsed.netloc.lower() not in {"listentogether", "listen-together"}:
            return None
        parts = [parsed.path, parsed.fragment]
        if not any(re.search(r"listen[-_]?together", unquote(part), re.IGNORECASE) for part in parts) and parsed.scheme in {"http", "https"}:
            return None
        queries = [parsed.query]
        if "?" in parsed.fragment:
            queries.append(parsed.fragment.partition("?")[2])
        params = {}
        for query in queries:
            for key, values in parse_qs(query, max_num_fields=40).items():
                if len(values) != 1 or (key in params and params[key] != values[0]):
                    return None
                params[key] = values[0]
        if (("roomId" in params and "roomid" in params and params["roomId"] != params["roomid"])
                or ("inviterId" in params and "inviterid" in params and params["inviterId"] != params["inviterid"])):
            return None
        room = params.get("roomId", params.get("roomid", ""))
        inviter = params.get("inviterId", params.get("inviterid", ""))
        if not _ROOM.fullmatch(room) or not _USER.fullmatch(inviter):
            return None
        return {"roomId": room, "inviterId": inviter}
    except (ValueError, TypeError):
        return None


def _wire_message(event, now):
    """Require matching transport session, bot, actor and original message ID."""
    try:
        message = event.message_obj
        raw = message.raw_message
        origin, sender = str(event.unified_msg_origin), str(event.get_sender_id())
        if not isinstance(raw, dict) or raw.get("post_type") != "message":
            return None
        if (str(raw.get("self_id")) != SELF or str(raw.get("user_id")) != sender
                or not _USER.fullmatch(sender) or sender == SELF):
            return None
        raw_sender = raw.get("sender")
        if isinstance(raw_sender, dict) and str(raw_sender.get("user_id", sender)) != sender:
            return None
        self_getter = getattr(event, "get_self_id", None)
        if callable(self_getter) and str(self_getter()) != SELF:
            return None
        identifier = _message_id(raw.get("message_id"))
        message_identifier = _message_id(getattr(message, "message_id", identifier))
        if not identifier or identifier != message_identifier:
            return None
        stamp = float(raw.get("time", 0))
        if not now - MESSAGE_WINDOW <= stamp <= now + 30:
            return None
        group = ""
        if raw.get("message_type") == "private":
            if (sender not in {OWNER, TRUSTED_FRIEND}
                    or origin != "snowluma-qq:FriendMessage:" + sender or raw.get("group_id")):
                return None
            group_getter = getattr(event, "get_group_id", None)
            if callable(group_getter) and str(group_getter() or ""):
                return None
        elif raw.get("message_type") == "group":
            group = str(raw.get("group_id", ""))
            if (group not in {GROUP, SOCIAL_GROUP}
                    or origin != "snowluma-qq:GroupMessage:" + group
                    or (group == GROUP and sender not in {OWNER, TRUSTED_FRIEND})):
                return None
            group_getter = getattr(event, "get_group_id", None)
            if callable(group_getter) and str(group_getter() or "") != group:
                return None
        else:
            return None
        text, strings, replies = [], [], []
        segments = raw.get("message")
        if isinstance(segments, str) and len(segments) <= 16000:
            text.append(segments)
            strings.append(segments)
        elif isinstance(segments, list) and len(segments) <= 100:
            for part in segments:
                if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
                    continue
                kind, data = part.get("type"), part["data"]
                if kind == "text" and isinstance(data.get("text"), str):
                    text.append(data["text"][:16000])
                    strings.append(data["text"][:16000])
                elif kind == "json" and isinstance(data.get("data"), str) and len(data["data"]) <= 30000:
                    try:
                        strings.extend(_json_strings(json.loads(data["data"])))
                    except (ValueError, RecursionError):
                        pass
                elif kind == "reply":
                    identifier_reply = _message_id(data.get("id"))
                    if identifier_reply:
                        replies.append(identifier_reply)
        return {"origin": origin, "sender": sender, "group": group,
                "message_id": identifier, "at": stamp,
                "text": "".join(text).strip(), "strings": strings,
                "reply_id": replies[0] if len(set(replies)) == 1 else ""}
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _invitations(wire):
    invitations = {}
    for value in wire["strings"]:
        for candidate in _URL.findall(value):
            invitation = parse_listen_invitation(candidate)
            if invitation:
                key = hashlib.sha256(json.dumps(invitation, sort_keys=True).encode()).hexdigest()[:12]
                invitations[key] = invitation
    return invitations


class ListenTogether:
    """Mixin requiring ``state``, atomic ``save`` and the QQ platform context."""

    def _listen_now(self):
        return time.time()

    def _listen_save(self):
        try:
            self.save()
            self._listen_storage_ready = True
            return True
        except Exception:
            self._listen_storage_ready = False
            return False

    def init_listen_together(self):
        self.listen_together_lock = asyncio.Lock()
        self._listen_storage_ready = True
        self.state.setdefault("listen_together_auto_accept_qq", [])
        state = self.state.setdefault("listen_together", {})
        state.setdefault("invitations", {})
        state.setdefault("active", None)
        state.setdefault("service", {"ready": False, "logged_in": False})
        state.setdefault("owner_controls", {})
        notices = state.setdefault("departure_notices", {})
        for notice in notices.values():
            if isinstance(notice, dict) and notice.get("status") == "sending":
                notice["status"] = "unknown"
        for row in state["invitations"].values():
            if not isinstance(row, dict):
                continue
            for notices in (row.get("peer_notices", {}), row.get("owner_notices", {})):
                if isinstance(notices, dict):
                    for notice in notices.values():
                        if isinstance(notice, dict) and notice.get("status") == "sending":
                            notice["status"] = "unknown"
            if row.get("notification_status") == "sending":
                row["notification_status"] = "unknown"
            if row.get("status") == "accepting":
                row.update(status="accept_unknown", reason="interrupted_join")
            if (row.get("status") == "accept_unknown" and not state.get("active")
                    and row.get("approved_by") in {OWNER, TRUSTED_FRIEND} and row.get("approval_message_id")
                    and isinstance(row.get("invitation"), dict)):
                state["active"] = {"roomId": row["invitation"]["roomId"],
                                   "request_id": row["id"], "status": "join_unknown",
                                   "joined_at": row.get("approved_at", 0), "last_heartbeat": 0}
        active = state.get("active")
        if isinstance(active, dict) and active.get("status") == "ending":
            active["status"] = "end_unknown"
        if isinstance(active, dict):
            row = state["invitations"].get(active.get("request_id"))
            if (self._listen_authorized_row(row) and row.get("status") == "joined"
                    and active.get("status") in {"active", "heartbeat_unconfirmed"}):
                active.setdefault("verified_joined", True)
        self._listen_save()

    def listen_together_status(self):
        state = self.state.get("listen_together", {})
        rows = [row for row in state.get("invitations", {}).values() if isinstance(row, dict)]
        active = state.get("active") or {}
        configured = [sender for sender in (OWNER, TRUSTED_FRIEND) if self._listen_trusted_sender(sender)]
        return {"enabled": True, "approval_required": True, "audio_playback": False,
                "approval_policy": "owner_or_trusted_sender", "auto_accept_qq": configured,
                "lifecycle_notification_policy": "owner_and_verified_inviter",
                "others_require_owner_approval": True,
                "storage_ready": getattr(self, "_listen_storage_ready", False),
                "service_ready": state.get("service", {}).get("ready") is True,
                "logged_in": state.get("service", {}).get("logged_in") is True,
                "pending_count": sum(row.get("status") in _AWAITING for row in rows),
                "awaiting_login_count": sum(row.get("status") == "awaiting_login" for row in rows),
                "uncertain_count": sum(row.get("status") in {"accept_unknown", "switch_unknown"} for row in rows),
                "presence": active.get("status", "idle"),
                "departure_notices": {status: sum(row.get("status") == status for row in
                    state.get("departure_notices", {}).values() if isinstance(row, dict))
                    for status in ("sent", "unknown")},
                "login_url": SERVICE + "/login"}

    def _listen_trusted_sender(self, sender):
        configured = self.state.get("listen_together_auto_accept_qq", [])
        return isinstance(configured, list) and sender in {OWNER, TRUSTED_FRIEND} and sender in configured

    def listen_together_prompt(self, event):
        """Describe the real capability to a verified QQ speaker, without I/O.

        Conversation context does not authorize room actions. Only the inbound
        invitation handler can join, and cached login evidence expires quickly.
        Never copy identifiers, login URLs or other people's room state here.
        """
        wire = _wire_message(event, self._listen_now())
        if not wire:
            return ""
        state = self.state.get("listen_together", {})
        service = state.get("service", {})
        now = self._listen_now()
        try:
            age = now - float(service.get("checked_at", 0))
        except (TypeError, ValueError, OverflowError):
            age = -1
        fresh = 0 <= age <= 150
        if fresh and service.get("ready") is True and service.get("logged_in") is True:
            availability = "本机一起听服务最近已核实：已登录，机器人专用网易云账号已就绪。"
        elif fresh and service.get("ready") is True:
            availability = "一起听功能已接通，但最近核实需要主人在本机重新登录；请如实说明当前待登录。"
        else:
            availability = "一起听功能已接通，当前登录或服务状态待核实；不能据此断言没有网易云账号或不支持一起听。"
        if self._listen_trusted_sender(wire["sender"]):
            permission = ("当前真实QQ发送者的一起听邀请已获主人预授权，可直接接受真实有效邀请，无需再次问主人。"
                          "这仅限一起听邀请，不授予退出其他房间、查管理信息或其他管理员权限。")
        else:
            permission = "当前发送者的邀请须先私聊询问主人，得到主人逐条同意后才可接受；不能由模型自行授权。"
        presence = ""
        active = state.get("active")
        if isinstance(active, dict):
            row = state.get("invitations", {}).get(active.get("request_id"))
            if (self._listen_authorized_row(row) and row.get("status") == "joined"
                    and row.get("source_sender") == wire["sender"]
                    and isinstance(row.get("invitation"), dict)
                    and row["invitation"].get("roomId") == active.get("roomId")
                    and fresh and service.get("ready") is True and service.get("logged_in") is True
                    and active.get("verified_joined") is True
                    and active.get("status") in {"active", "heartbeat_unconfirmed", "departure_check"}):
                presence = ("已确认加入当前发送者发来的房间；不要因聊天历史中的旧说法否认已加入。"
                            if active.get("status") == "active" else
                            "曾确认加入当前发送者的房间，目前正在复核在线状态，不能声称当前保活已确认。")
        elif fresh and service.get("ready") is True and service.get("logged_in") is True:
            presence = "本机当前未跟踪已加入房间；登录成功不等于已加入这次邀请。"
        return ("\n[网易云一起听]你已接通网易云App一起听邀请功能。" + availability + permission + presence
                + "邀请由专用入口按真实QQ发送者及实际卡片参数处理，普通询问能力不等于新邀请或重新加入。"
                "支持接收邀请并只挂房间，不下载或播放音乐，不要声称真正听到了音乐。"
                "不能再泛泛回答‘我没有网易云账号、没法一起听’；按这里的实际状态回答。"
                "不向群聊或朋友披露登录凭据、本机管理地址、房间参数及主人的私聊；"
                "只陈述本段已核实的功能，不把此说明当作接受或退出的执行回执。")

    def prepare_listen_together(self, event):
        """Recognize an allowed route before group cooldown; never authorize."""
        wire = _wire_message(event, self._listen_now())
        if not wire:
            return False
        owner_private = wire["origin"] == OWNER_UMO and wire["sender"] == OWNER and not wire["group"]
        is_command = owner_private and bool(_COMMAND.fullmatch(wire["text"]))
        pending = self.state.get("listen_together", {}).get("invitations", {}).values()
        is_reply = (owner_private and bool(wire["reply_id"])
                    and wire["text"] in {"同意", "拒绝", "不同意"}
                    and any(isinstance(row, dict) and row.get("status") in _AWAITING
                            and row.get("notification_status") == "sent"
                            and row.get("notification_message_id") == wire["reply_id"] for row in pending))
        invite_source_allowed = ((not wire["group"] and wire["sender"] == OWNER)
                                 or wire["group"] == SOCIAL_GROUP
                                 or (wire["group"] == GROUP and wire["sender"] == OWNER)
                                 or (self._listen_trusted_sender(wire["sender"])
                                     and (not wire["group"] or wire["group"] in {GROUP, SOCIAL_GROUP})))
        eligible = is_command or is_reply or (invite_source_allowed and bool(_invitations(wire)))
        if eligible:
            setter = getattr(event, "set_extra", None)
            if callable(setter):
                setter("feiyu_listen_together_route", True)
        return eligible

    async def _listen_request(self, method, path, payload=None):
        """Fixed loopback-only wrapper. Cookies never enter this process."""
        if business_paused():
            raise PeakTariffPaused("automatic_listen_together_paused_during_peak")
        try:
            token = SERVICE_TOKEN.read_text(encoding="utf-8").strip()
            if not re.fullmatch(r"[A-Za-z0-9._~-]{24,256}", token):
                return {"ok": False, "code": "service_unavailable"}
            async with (
                aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=25)) as session,
                session.request(method, SERVICE + path,
                    headers={"Authorization": "Bearer " + token},
                    json=payload, allow_redirects=False) as response,
            ):
                value = await response.json()
                if not isinstance(value, dict):
                    return {"ok": False, "code": "invalid_response"}
                if response.status == 200:
                    return value
                if (value.get("ok") is False and value.get("attempted") is False
                        and value.get("code") in {"login_required", "service_not_ready", "invalid_invitation", "invite_expired"}):
                    return {"ok": False, "code": value["code"], "attempted": False}
                return {"ok": False, "code": "service_unavailable"}
        except Exception:
            return {"ok": False, "code": "service_unavailable"}

    async def _listen_private(self, body, recipient=OWNER):
        try:
            if not _USER.fullmatch(str(recipient)) or str(recipient) == SELF:
                return ""
            adapter = self.context.get_platform_inst("snowluma-qq")
            if adapter is None:
                return ""
            response = await asyncio.wait_for(adapter.get_client().call_action(
                "send_private_msg", user_id=int(recipient),
                message=[{"type": "text", "data": {"text": body}}]), 40)
            return _receipt(response)
        except Exception:
            return ""

    async def _listen_lifecycle_send(self, row, kind, recipient, ledger_name, body):
        """Reserve each fixed lifecycle notice durably before private delivery."""
        if kind not in {"joined", "ended"} or not self._listen_approval_valid(row):
            return
        if kind == "joined" and row.get("status") != "joined":
            return
        ledger = row.setdefault(ledger_name, {})
        if not isinstance(ledger, dict) or kind in ledger:
            return
        notice = {"status": "sending", "at": self._listen_now()}
        ledger[kind] = notice
        if not self._listen_save():
            return
        try:
            message_id = await self._listen_private(body, recipient=recipient)
        except BaseException:
            notice["status"] = "unknown"
            self._listen_save()
            raise
        notice["status"] = "sent" if message_id else "unknown"
        if message_id:
            notice["message_id"] = message_id
        self._listen_save()

    async def _listen_peer_notice(self, row, kind):
        """Notify only the original verified QQ inviter; grant no controls."""
        if not isinstance(row, dict):
            return
        recipient = str(row.get("source_sender", ""))
        if recipient in {OWNER, SELF} or not _USER.fullmatch(recipient):
            return
        if not _message_id(row.get("source_message_id")):
            return
        body = {"joined": "已经加入你发来的网易云一起听啦，我会安静挂在房间里，不播放音乐。",
                "ended": "这次网易云一起听已经结束啦。"}.get(kind)
        if body:
            await self._listen_lifecycle_send(row, kind, recipient, "peer_notices", body)

    async def _listen_join_notice(self, row):
        if not self._listen_approval_valid(row) or row.get("status") != "joined":
            return
        if ("joined" in row.get("owner_notices", {})
                and (str(row.get("source_sender")) == OWNER or "joined" in row.get("peer_notices", {}))):
            return
        row["lifecycle_notifications_started"] = True
        if not self._listen_save():
            return
        await self._listen_lifecycle_send(
            row, "joined", OWNER, "owner_notices",
            "已经加入网易云一起听，会安静挂着，不播放音乐。想退出时私聊我：/一起听 退出")
        await self._listen_peer_notice(row, "joined")

    async def _listen_ask(self, identifier, invitation, wire):
        state = self.state["listen_together"]
        rows = state["invitations"]
        trusted = self._listen_trusted_sender(wire["sender"])
        existing = rows.get(identifier)
        if existing:
            # A later real trusted sender can independently authorize the same
            # pending room. The untrusted earlier sender never grants authority.
            if trusted and existing.get("status") == "awaiting_owner":
                existing.update(source_group=wire["group"], source_sender=wire["sender"],
                                source_message_id=wire["message_id"], notification_status="not_required")
                if self._listen_save():
                    await self._listen_join(existing, wire, trusted=True)
            return
        if len(rows) >= MAX_INVITATIONS:
            return
        row = {"id": identifier, "invitation": invitation, "status": "awaiting_owner",
               "created_at": self._listen_now(), "source_group": wire["group"],
               "source_sender": wire["sender"], "source_message_id": wire["message_id"],
               "notification_status": "sending", "notification_at": self._listen_now()}
        rows[identifier] = row
        if trusted:
            row.update(notification_status="not_required", approval_kind="trusted_sender")
        if not self._listen_save():
            return
        if trusted:
            await self._listen_join(row, wire, trusted=True)
            return
        location = f"群 {wire['group']} 的 QQ {wire['sender']}" if wire["group"] else "你"
        switching = "这会先退出当前一起听房间，再加入新房间。\n" if state.get("active") else ""
        body = (f"收到{location}发来的网易云一起听邀请。\n{switching}"
                "我只挂在房间里，不播放或下载音乐。是否加入？\n"
                f"同意请发：/一起听 同意 {identifier}\n"
                f"拒绝请发：/一起听 拒绝 {identifier}\n"
                "也可以引用这条消息回复“同意”或“拒绝”。我会等你明确答复。")
        try:
            message_id = await self._listen_private(body)
        except BaseException:
            row["notification_status"] = "unknown"
            self._listen_save()
            raise
        row["notification_status"] = "sent" if message_id else "unknown"
        if message_id:
            row["notification_message_id"] = message_id
        self._listen_save()

    def _listen_pending(self):
        return [row for row in self.state["listen_together"]["invitations"].values()
                if isinstance(row, dict) and row.get("status") in _AWAITING]

    def _listen_approval(self, wire):
        """Return a decision from original owner-private text, never model extras."""
        if wire["group"] or wire["origin"] != OWNER_UMO or wire["sender"] != OWNER:
            return None
        rows = self.state["listen_together"]["invitations"]
        match = _COMMAND.fullmatch(wire["text"])

        def current_request(row):
            return (row.get("status") in _AWAITING
                    and wire["at"] >= int(max(row.get("notification_at", 0), row.get("created_at", 0)))
                    and row.get("approval_message_id") != wire["message_id"])

        if match:
            command = str(match[1] or "").strip()
            explicit = re.fullmatch(r"(同意|拒绝|不同意)\s+([a-f0-9]{12})", command)
            if explicit:
                row = rows.get(explicit[2])
                if (row and current_request(row)
                        and row.get("notification_status") in {"sent", "unknown"}):
                    return explicit[1] == "同意", row
            if command in {"同意", "拒绝", "不同意"}:
                pending = self._listen_pending()
                if (len(pending) == 1 and pending[0].get("notification_status") == "sent"
                        and current_request(pending[0])
                        and 0 <= self._listen_now() - pending[0].get("notification_at", 0) <= APPROVAL_WINDOW):
                    return command == "同意", pending[0]
        elif wire["reply_id"] and wire["text"] in {"同意", "拒绝", "不同意"}:
            matches = [row for row in self._listen_pending()
                       if row.get("notification_status") == "sent"
                       and current_request(row)
                       and row.get("notification_message_id") == wire["reply_id"]]
            if len(matches) == 1:
                return wire["text"] == "同意", matches[0]
        return None

    @staticmethod
    def _listen_confirmed(result, room, active):
        return (isinstance(result, dict) and result.get("ok") is True
                and str(result.get("roomId", "")) == room
                and isinstance(result.get("state"), dict)
                and result["state"].get("active") is active)

    @staticmethod
    def _listen_authorized_row(row):
        if (not isinstance(row, dict) or row.get("status") not in {
                "joined", "accept_unknown", "accepting", "confirmed_not_joined"}
                or row.get("departure_suppressed") or row.get("owner_ended")
                or row.get("accept_attempted") is not True
                or not _message_id(row.get("approval_message_id"))):
            return False
        return ListenTogether._listen_approval_valid(row)

    @staticmethod
    def _listen_approval_valid(row):
        """Check durable QQ authorization, independent of room lifetime."""
        if (not isinstance(row, dict) or row.get("accept_attempted") is not True
                or not _message_id(row.get("approval_message_id"))):
            return False
        if row.get("approval_kind") == "explicit_owner":
            return row.get("approved_by") == OWNER and row.get("authorization_mode") == "explicit_owner"
        return (row.get("approval_kind") == "trusted_sender"
                and row.get("authorization_mode") == "owner_allowlist"
                and row.get("approved_by") in {OWNER, TRUSTED_FRIEND}
                and row.get("approved_by") == row.get("source_sender")
                and row.get("approval_message_id") == row.get("source_message_id"))

    def _listen_partner_observation(self, active, result):
        state = result.get("state", {})
        if state.get("partner_evidence") == "room_users" and state.get("partner_present") is True:
            active["partner_seen"] = True
            active["partner_seen_at"] = self._listen_now()
            row = self.state["listen_together"]["invitations"].get(active.get("request_id"))
            if isinstance(row, dict):
                row["partner_seen"] = True

    def _listen_departure_recheck(self, active, evidence):
        """Require two independent explicit observations, at least 15s apart."""
        candidate = active.get("departure_candidate")
        if not isinstance(candidate, dict) or candidate.get("evidence") != evidence:
            active["departure_candidate"] = {"evidence": evidence, "at": self._listen_now()}
            active["status"] = "departure_check"
            self._listen_save()
            return False
        active["status"] = "departure_check"
        return self._listen_now() - float(candidate.get("at", self._listen_now())) >= 15

    async def _listen_sync_current(self, *, allow_unmanaged_exit=False):
        """Restore only an actually live room with durable local authorization."""
        state = self.state["listen_together"]
        result = await self._listen_request("GET", "/together/current")
        if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("state"), dict):
            return "unknown"
        if result["state"].get("active") is False:
            return "inactive"
        room = str(result.get("roomId", ""))
        if result["state"].get("active") is not True or not _ROOM.fullmatch(room):
            return "unknown"
        candidates = [row for row in state["invitations"].values()
                      if self._listen_authorized_row(row)
                      and isinstance(row.get("invitation"), dict)
                      and row["invitation"].get("roomId") == room]
        if not candidates and not allow_unmanaged_exit:
            state["service"]["current_room_unmanaged"] = True
            self._listen_save()
            return "unmanaged"
        row = max(candidates, key=lambda item: item.get("approved_at", 0)) if candidates else None
        active = {"roomId": room, "request_id": row["id"] if row else "",
                  "status": "active", "verified_joined": True,
                  "joined_at": row.get("approved_at", self._listen_now()) if row else self._listen_now(),
                  "last_heartbeat": 0, "last_check": self._listen_now(), "recovered_from_service": True}
        if isinstance(result.get("requestId"), str) and len(result["requestId"]) <= 128:
            active["service_request_id"] = result["requestId"]
        if row:
            active["inviterId"] = row["invitation"]["inviterId"]
            active["partner_seen"] = row.get("partner_seen") is True
            row.setdefault("reconciled_from", row.get("status"))
            became_joined = row.get("status") != "joined"
            row["status"] = "joined"
        else:
            active["departure_suppressed"] = True
        self._listen_partner_observation(active, result)
        state["active"] = active
        state["service"].pop("current_room_unmanaged", None)
        if not self._listen_save():
            return "unknown"
        if row and became_joined:
            await self._listen_join_notice(row)
        return "active"

    async def _listen_departure_notice(self, active, evidence):
        """Reserve one private notice before I/O, independent of active lifetime."""
        if (active.get("verified_joined") is not True or active.get("departure_suppressed")
                or active.get("status") in {"ending", "end_unknown", "leave_unconfirmed_active"}
                or (evidence == "partner_absent" and active.get("partner_seen") is not True)):
            return
        state = self.state["listen_together"]
        identifier = hashlib.sha256((str(active.get("roomId")) + "\n" + str(active.get("request_id"))).encode()).hexdigest()
        ledger = state["departure_notices"]
        if identifier in ledger:
            await self._listen_peer_notice(state["invitations"].get(active.get("request_id")), "ended")
            return
        notice = {"status": "sending", "at": self._listen_now(), "evidence": evidence,
                  "request_id": active.get("request_id", "")}
        ledger[identifier] = notice
        if not self._listen_save():
            return
        body = ("一起听的对方已经离开了。" if evidence == "partner_absent"
                else "这次一起听已经结束了。")
        try:
            message_id = await self._listen_private(body)
        except BaseException:
            notice["status"] = "unknown"
            self._listen_save()
            raise
        notice["status"] = "sent" if message_id else "unknown"
        if message_id:
            notice["message_id"] = message_id
        self._listen_save()
        row = state["invitations"].get(active.get("request_id"))
        await self._listen_peer_notice(row, "ended")

    async def _listen_end(self, *, discover=False):
        state = self.state["listen_together"]
        active = state.get("active")
        original_active = active
        if isinstance(active, dict):
            # The owner intent exists even when a fresh read is unavailable.
            # Persist it before discovery so later natural room closure cannot
            # be mistaken for a partner departure notification.
            active["departure_suppressed"] = True
            intent_row = state["invitations"].get(active.get("request_id"))
            if intent_row:
                intent_row.update(departure_suppressed=True, owner_ended=True)
            if not self._listen_save():
                return False
        if discover and isinstance(active, dict) and active.get("status") not in {"active", "leave_unconfirmed_active"}:
            # An explicit owner leave may resolve an uncertain pointer. This is
            # a fresh read, never permission for a retry without verification.
            previous = active
            discovered = await self._listen_sync_current(allow_unmanaged_exit=True)
            if discovered == "inactive":
                previous["departure_suppressed"] = True
                row = state["invitations"].get(previous.get("request_id"))
                if row:
                    row.update(departure_suppressed=True, owner_ended=True, status="ended")
                state["active"] = None
                if not self._listen_save():
                    return False
                await self._listen_peer_notice(row, "ended")
                return True
            if discovered != "active":
                return False
            active = state.get("active")
        if not isinstance(active, dict) and discover:
            discovered = await self._listen_sync_current(allow_unmanaged_exit=True)
            if discovered == "inactive":
                return True
            if discovered != "active":
                return False
            active = state.get("active")
        if not isinstance(active, dict):
            return True
        if active.get("status") not in {"active", "leave_unconfirmed_active"}:
            return False
        active.update(status="ending", departure_suppressed=True)
        row = state["invitations"].get(active.get("request_id"))
        if (row is None and isinstance(original_active, dict)
                and original_active.get("roomId") == active.get("roomId")):
            row = state["invitations"].get(original_active.get("request_id"))
            if row:
                active["request_id"] = row["id"]
                active["verified_joined"] = original_active.get("verified_joined") is True
        if row:
            row.update(departure_suppressed=True, owner_ended=True)
        if not self._listen_save():
            return False
        try:
            result = await self._listen_request("POST", "/together/end", {"roomId": active["roomId"]})
        except BaseException:
            active["status"] = "end_unknown"
            self._listen_save()
            raise
        if self._listen_confirmed(result, active["roomId"], False):
            if row:
                row["status"] = "ended"
            state["active"] = None
            if not self._listen_save():
                return False
            await self._listen_peer_notice(row, "ended")
            return True
        active["status"] = "end_unknown"
        self._listen_save()
        return False

    async def _listen_join(self, row, wire, *, trusted=False):
        state = self.state["listen_together"]
        if trusted and not self._listen_trusted_sender(wire["sender"]):
            return
        row.update(status="accepting", approved_by=wire["sender"] if trusted else OWNER,
                   approval_kind="trusted_sender" if trusted else "explicit_owner",
                   authorization_mode="owner_allowlist" if trusted else "explicit_owner",
                   approval_message_id=wire["message_id"], approved_at=self._listen_now())
        if not self._listen_save():
            return
        active = state.get("active")
        room = row["invitation"]["roomId"]
        if not active:
            discovered = await self._listen_sync_current()
            if discovered in {"unknown", "unmanaged"}:
                row.update(status="awaiting_login" if trusted else "awaiting_owner",
                           reason="current_room_unverified", accept_attempted=False,
                           deferred_retry_at=self._listen_now() + 60)
                self._listen_save()
                await self._listen_private("当前一起听房间状态还没有核实，暂不重复加入或切换；核实后再处理这次邀请。")
                return
            active = state.get("active")
        if active:
            if active.get("roomId") == room and active.get("status") == "active":
                row["status"] = "joined"
                self._listen_save()
                await self._listen_private("已经挂在这个一起听房间里，没有重复加入。")
                await self._listen_peer_notice(row, "joined")
                return
            if not await self._listen_end():
                row.update(status="switch_unknown", reason="old_room_leave_unconfirmed")
                self._listen_save()
                await self._listen_private("当前房间的退出结果未确认，暂未加入新房间；我会先核实状态，不会重复尝试加入。")
                return
        # Persist the possibly joined room before I/O as well as the row. A
        # process interruption must leave enough context for read-only checks.
        state["active"] = {"roomId": room, "request_id": row["id"], "status": "join_unknown",
                           "joined_at": self._listen_now(), "last_heartbeat": 0}
        row["accept_attempted"] = True
        if not self._listen_save():
            return
        try:
            result = await self._listen_request("POST", "/together/accept",
                                                {**row["invitation"], "requestId": row["id"]})
        except BaseException:
            row.update(status="accept_unknown", reason="join_unconfirmed")
            self._listen_save()
            raise
        if (result.get("ok") is False and result.get("attempted") is False
                and result.get("code") in {"login_required", "service_not_ready", "invalid_invitation", "invite_expired"}):
            code = result["code"]
            state["active"] = None
            previous_reason = row.get("reason")
            row.update(status=("awaiting_login" if trusted else "awaiting_owner")
                       if code in {"login_required", "service_not_ready"} else "expired",
                       reason=code, accept_attempted=False, deferred_retry_at=self._listen_now() + 60)
            self._listen_save()
            body = ("网易云还没有登录，刚才没有尝试加入。请在本机登录：\n"
                    + SERVICE + "/login\n"
                    + ("这个邀请来自你已授权的 QQ，登录后会继续首次加入。" if trusted
                       else "/一起听 同意 " + row["id"] + "（登录后再次确认）")
                    if code == "login_required" else
                    "本机一起听服务尚未就绪，没有尝试加入；恢复后需要你再次明确同意。"
                    if code == "service_not_ready" else "一起听邀请无效或已过期，没有尝试加入；请发新的邀请。")
            if not trusted or previous_reason != code:
                await self._listen_private(body)
            return
        if self._listen_confirmed(result, room, True):
            row["status"] = "joined"
            state["active"] = {"roomId": room, "request_id": row["id"], "status": "active",
                               "joined_at": self._listen_now(), "last_heartbeat": 0,
                               "verified_joined": True, "inviterId": row["invitation"]["inviterId"]}
            self._listen_partner_observation(state["active"], result)
            body = "已经加入网易云一起听，会安静挂着，不播放音乐。想退出时私聊我：/一起听 退出"
        else:
            row.update(status="accept_unknown", reason="join_unconfirmed")
            # Remember only this approved possible join for read-only recovery.
            state["active"] = {"roomId": room, "request_id": row["id"], "status": "join_unknown",
                               "joined_at": self._listen_now(), "last_heartbeat": 0}
            body = "这次一起听的加入结果没有确认，我不会重复加入；会先检查房间状态。"
        if not self._listen_save():
            return
        if row.get("status") == "joined":
            await self._listen_join_notice(row)
        else:
            await self._listen_private(body)

    async def handle_listen_together(self, event):
        """Consume invitations/owner controls before normal chat and its lanes."""
        wire = _wire_message(event, self._listen_now())
        if not wire or not self.prepare_listen_together(event):
            return False
        event.stop_event()
        getter = getattr(event, "get_extra", None)
        if callable(getter) and getter("feiyu_listen_together_handled"):
            return True
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("feiyu_listen_together_handled", True)
        if not hasattr(self, "listen_together_lock"):
            self.init_listen_together()
        async with self.listen_together_lock:
            if not self._listen_storage_ready:
                return True
            owner_control = (wire["origin"] == OWNER_UMO and wire["sender"] == OWNER
                             and (bool(_COMMAND.fullmatch(wire["text"]))
                                  or (wire["reply_id"] and wire["text"] in {"同意", "拒绝", "不同意"})))
            if owner_control:
                controls = self.state["listen_together"]["owner_controls"]
                if wire["message_id"] in controls:
                    return True
                controls[wire["message_id"]] = wire["at"]
                if not self._listen_save():
                    return True
            approval = self._listen_approval(wire)
            if approval:
                agreed, row = approval
                if agreed:
                    await self._listen_join(row, wire)
                else:
                    row.update(status="declined", approval_message_id=wire["message_id"], decided_at=self._listen_now())
                    if self._listen_save():
                        await self._listen_private("这次一起听邀请已拒绝，没有加入。")
                return True
            match = _COMMAND.fullmatch(wire["text"]) if wire["origin"] == OWNER_UMO and not wire["group"] else None
            if match:
                command = str(match[1] or "").strip()
                if command in {"退出", "离开", "结束"}:
                    success = await self._listen_end(discover=True)
                    await self._listen_private("已经退出一起听。" if success else "退出结果还未确认，我会检查状态，不会重复发送退出或加入。")
                elif command in {"", "状态", "待办", "帮助"}:
                    status = self.listen_together_status()
                    pending = self._listen_pending()
                    body = (f"网易云一起听：{'已登录' if status['logged_in'] else '待登录'}；"
                            f"房间状态：{status['presence']}；待确认邀请 {len(pending)} 个。\n"
                            "你授权的 QQ 发来邀请可直接加入；其他邀请先征求你同意。只挂房间，不播放音乐。")
                    if pending:
                        body += "\n待办：" + "、".join(row["id"] for row in pending[:20])
                    if not status["logged_in"]:
                        body += "\n本机登录页：" + SERVICE + "/login"
                    await self._listen_private(body)
                else:
                    await self._listen_private("请使用：/一起听 同意 邀请编号、/一起听 拒绝 邀请编号、/一起听 状态、/一起听 退出。没有匹配的待确认邀请时不会加入。")
                return True
            for identifier, invitation in _invitations(wire).items():
                await self._listen_ask(identifier, invitation, wire)
        return True

    async def _listen_together_monitor_once(self):
        """Maintain presence; defer only never-attempted owner-allowlisted joins."""
        async with self.listen_together_lock:
            if not self._listen_storage_ready:
                return
            state = self.state["listen_together"]
            now = self._listen_now()
            service = state["service"]
            waiting_login = [row for row in state["invitations"].values() if isinstance(row, dict)
                             and row.get("status") == "awaiting_login" and row.get("accept_attempted") is False
                             and row.get("approval_kind") == "trusted_sender"
                             and self._listen_trusted_sender(row.get("source_sender"))]
            login_interval = 15 if waiting_login else 60
            if now - float(service.get("checked_at", 0)) >= login_interval:
                previously_logged_in = service.get("logged_in") is True
                result = await self._listen_request("GET", "/login/status")
                service.update(ready=result.get("ok") is True,
                               logged_in=result.get("ok") is True and result.get("logged_in") is True,
                               checked_at=now)
                if not previously_logged_in and service["logged_in"]:
                    for row in waiting_login:
                        row["deferred_retry_at"] = now
                self._listen_save()
            if service.get("logged_in") and not state.get("active"):
                await self._listen_sync_current()
            if service.get("logged_in") and not state.get("active"):
                trusted_waiting = [row for row in state["invitations"].values()
                                   if isinstance(row, dict) and row.get("status") == "awaiting_login"
                                   and row.get("approval_kind") == "trusted_sender"
                                   and row.get("authorization_mode") == "owner_allowlist"
                                   and row.get("accept_attempted") is False
                                   and now >= float(row.get("deferred_retry_at", 0))
                                   and row.get("approved_by") == row.get("source_sender")
                                   and row.get("approval_message_id") == row.get("source_message_id")
                                   and self._listen_trusted_sender(row.get("source_sender"))]
                if trusted_waiting:
                    # One explicitly delegated first join per pass. Unknown
                    # outcomes can never re-enter this awaiting-login branch.
                    row = min(trusted_waiting, key=lambda item: item.get("created_at", 0))
                    await self._listen_join(row, {"sender": row["source_sender"],
                                                 "message_id": row["source_message_id"]}, trusted=True)
            active = state.get("active")
            if not active or not service.get("logged_in"):
                return
            check_interval = 15 if active.get("departure_candidate") or active.get("status") == "join_unknown" else 60
            if now - float(active.get("last_check", 0)) >= check_interval:
                payload = {"roomId": active["roomId"]}
                row = state["invitations"].get(active.get("request_id"))
                if (self._listen_authorized_row(row) and row["invitation"]["roomId"] == active["roomId"]
                        and _USER.fullmatch(str(row["invitation"].get("inviterId", "")))):
                    payload["inviterId"] = row["invitation"]["inviterId"]
                result = await self._listen_request("POST", "/together/status", payload)
                active["last_check"] = now
                if self._listen_confirmed(result, active["roomId"], False):
                    if (active.get("verified_joined") is not True
                            and now - float(active.get("joined_at", now)) < JOIN_VERIFY_GRACE
                            and not active.get("departure_suppressed")):
                        active["status"] = "join_unknown"
                        self._listen_save()
                        return
                    if (active.get("verified_joined") is True and not active.get("departure_suppressed")
                            and active.get("status") not in {"ending", "end_unknown", "leave_unconfirmed_active"}
                            and not self._listen_departure_recheck(active, "room_inactive")):
                        return
                    await self._listen_departure_notice(active, "room_inactive")
                    row = state["invitations"].get(active.get("request_id"))
                    if row and active.get("departure_suppressed") and active.get("verified_joined") is True:
                        row["status"] = "ended"
                        if self._listen_save():
                            await self._listen_lifecycle_send(
                                row, "ended", OWNER, "owner_notices", "已经确认退出网易云一起听。")
                            await self._listen_peer_notice(row, "ended")
                    if row and row.get("status") == "accept_unknown":
                        row.update(status="confirmed_not_joined", reason="room_status_confirmed_inactive")
                    state["active"] = None
                    self._listen_save()
                    return
                if self._listen_confirmed(result, active["roomId"], True):
                    if active.get("status") == "end_unknown":
                        # Confirm the failed leave without resuming heartbeats.
                        active["status"] = "leave_unconfirmed_active"
                        active["last_verified_active"] = now
                    elif active.get("status") == "leave_unconfirmed_active":
                        active["last_verified_active"] = now
                    else:
                        partner = result["state"]
                        candidate = active.get("departure_candidate")
                        returned = partner.get("partner_evidence") == "room_users" and partner.get("partner_present") is True
                        room_restored = isinstance(candidate, dict) and candidate.get("evidence") == "room_inactive"
                        if returned or room_restored:
                            active.pop("departure_candidate", None)
                        active["status"] = "departure_check" if active.get("departure_candidate") else "active"
                        active["verified_joined"] = True
                        self._listen_partner_observation(active, result)
                        row = state["invitations"].get(active.get("request_id"))
                        if row and row.get("status") == "accept_unknown":
                            row["status"] = "joined"
                            if self._listen_save():
                                await self._listen_join_notice(row)
                        if row and active.get("partner_seen"):
                            row["partner_seen"] = True
                        if (result["state"].get("partner_evidence") == "room_users"
                                and result["state"].get("partner_present") is False
                                and active.get("partner_seen") is True
                                and self._listen_departure_recheck(active, "partner_absent")):
                            await self._listen_departure_notice(active, "partner_absent")
                            active["status"] = "partner_departed"
                self._listen_save()
            row = state["invitations"].get(active.get("request_id"))
            if (isinstance(row, dict) and row.get("status") == "joined"
                    and row.get("lifecycle_notifications_started") is True
                    and active.get("status") == "active" and active.get("verified_joined") is True):
                # Resume only recipients whose notice was never attempted.
                # A sending/unknown receipt is retained and never resent.
                await self._listen_join_notice(row)
            if (active.get("status") == "active"
                    and now - float(active.get("last_heartbeat", 0)) >= HEARTBEAT_INTERVAL):
                active["last_heartbeat"] = now
                if not self._listen_save():
                    return
                result = await self._listen_request("POST", "/together/heartbeat", {"roomId": active["roomId"]})
                if not self._listen_confirmed(result, active["roomId"], True):
                    active["status"] = "heartbeat_unconfirmed"
                self._listen_save()

    async def listen_together_monitor(self):
        while True:
            try:
                if not automatic_peak_paused():
                    await self._listen_together_monitor_once()
            except Exception:
                # Service restarts do not affect ordinary QQ conversation.
                self._listen_monitor_error = "service_check_unavailable"
            await asyncio.sleep(15)
