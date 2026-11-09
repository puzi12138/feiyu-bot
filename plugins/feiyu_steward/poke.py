"""Turn verified inbound QQ pokes into ordinary, bounded conversation facts.

Only transport notices count; a quoted poke, nickname or chat text cannot
grant identity or start an action. No QQ write API is used by this module.
"""

import time

from .group_policy import ALLOWED_GROUPS, OWNER, group_id_from_event
from .policy import OWNER_UMO

SELF = "0"
PRIVATE_POKE_COOLDOWN = 30
FACT_LIFETIME = 600


def poke_notice(event):
    """Return a real OneBot poke payload, or None for ordinary messages."""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if (isinstance(raw, dict) and raw.get("post_type") == "notice"
            and raw.get("notice_type") == "notify" and raw.get("sub_type") == "poke"):
        return raw
    return None


def _extra(event, key):
    getter = getattr(event, "get_extra", None)
    return getter(key) if callable(getter) else None


class PokeAwareness:
    """Host must supply state/save and the existing group participation policy."""

    def _poke_now(self):
        return time.time()

    def prepare_poke(self, event):
        """Prepare a verified poke; return True only when it must stop.

        Call before the transport group gate, and again in the normal observer.
        The second call is idempotent. An allowed poke stays on the normal
        persona/LLM path; tools are disabled by ``apply_poke_context`` later.
        """
        raw = poke_notice(event)
        if raw is None:
            return False
        prepared = _extra(event, "feiyu_poke_prepared")
        if prepared is not None:
            return prepared == "blocked"

        def stop(reason):
            event.set_extra("feiyu_poke_prepared", "blocked")
            event.set_extra("feiyu_poke_block", reason)
            event.stop_event()
            return True

        origin = str(getattr(event, "unified_msg_origin", ""))
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        group = group_id_from_event(event)
        raw_group = str(raw.get("group_id") or "")
        if (self_id != SELF or str(raw.get("self_id")) != SELF
                or str(raw.get("target_id")) != SELF or sender == SELF
                or not sender.isdigit() or str(raw.get("user_id")) != sender
                or (raw.get("sender_id") is not None and str(raw["sender_id"]) != sender)):
            return stop("poke_not_addressed_to_bot_or_invalid_identity")
        if group:
            if group not in ALLOWED_GROUPS or raw_group != group:
                return stop("poke_unauthorized_group")
        elif group is None or raw_group or origin != OWNER_UMO or sender != OWNER:
            return stop("poke_unauthorized_private_session")
        # This reuses the owner's exact group/sender rules before persisting
        # anything. A non-owner in the owner-only group cannot enter memory.
        gate = self.group_message_block(event)
        if gate and gate != "group_cooldown":
            return stop(gate)

        now = self._poke_now()
        rows = self.state.setdefault("poke_facts", {})
        previous = rows.get(origin, {})
        rows[origin] = {"actor_id": sender, "target_id": SELF, "at": now,
                        "group_id": group or ""}
        self.save()
        if gate:
            return stop(gate)
        try:
            private_age = now - float(previous.get("at", 0) or 0)
        except (TypeError, ValueError):
            private_age = PRIVATE_POKE_COOLDOWN
        if not group and 0 <= private_age < PRIVATE_POKE_COOLDOWN:
            return stop("poke_private_cooldown")

        actor = "主人" if sender == OWNER else "群成员"
        fact = f"【QQ戳一戳事件】{actor}（QQ {sender}）刚刚戳了戳你。"
        event.message_str = fact
        event.message_obj.message_str = fact
        event.set_extra("feiyu_poke_prepared", "accepted")
        event.set_extra("feiyu_poke_fact", rows[origin].copy())
        event.is_at_or_wake_command = True
        event.is_wake = True
        event.should_call_llm(True)
        # A verified poke shares only the reactive lane with @/keyword replies.
        self.reserve_group_wakeup(event)
        return False

    def apply_poke_context(self, event, req):
        """Add only this session's recent verified poke fact to the request."""
        origin = str(getattr(event, "unified_msg_origin", ""))
        row = getattr(self, "state", {}).get("poke_facts", {}).get(origin)
        if not isinstance(row, dict):
            return False
        try:
            age = self._poke_now() - float(row.get("at", 0))
        except (TypeError, ValueError):
            return False
        if not 0 <= age <= FACT_LIFETIME:
            return False
        current = _extra(event, "feiyu_poke_prepared") == "accepted"
        actor = str(row.get("actor_id", ""))
        if not actor.isdigit() or row.get("target_id") != SELF:
            return False
        req.system_prompt = str(getattr(req, "system_prompt", "") or "") + (
            '请仅依据当前公开来源及本地明确配置的信息处理请求；不要推测个人身份或私人偏好。'
        )
        if current:
            req.func_tool = None
            req.system_prompt += (
                "本轮只有戳一戳，没有新文字指令。按肥鱼的语气自然回一两句，"
                "不用解释技术，不调用工具、不执行管理、不外发、不翻唱或连续反戳。"
                "只用当前会话已有上下文，群里不提主人私聊或私人记忆。"
            )
        else:
            req.system_prompt += "这是此前的动作，不是当前用户又戳了一次；以当前文字需求为主。"
        return True
