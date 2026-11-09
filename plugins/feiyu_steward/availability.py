"""Bounded owner availability, independent of AstrBot and its event objects.

Call ``update_availability`` only for a newly received owner private message.
Use ``explicit=True/False`` for the /勿扰 and /继续聊 controls. Refresh before
checking delivery gates and persist state whenever either function returns True.
Only timestamps and a reason label are stored; message text is never retained.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
NATURAL_REASONS = {"natural_sleep", "natural_busy"}
_FIELDS = ("dnd", "dnd_reason", "dnd_until", "dnd_changed_at")
_QUOTED = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"
    r'"[^"\n]*"|‘[^’]*’|\'[^\'\n]*\''
)
_REPORTED = re.compile(
    r"(?:机器人|bot|肥鱼|大肥鱼|你|他|她|它|有人|某人|对方).{0,4}"
    r"(?:说|表示|告诉)|我说过|听说|转述|引用",
    re.IGNORECASE,
)
_PREFIX = r"(?:(?:好(?:了|的)?|嗯|那|行|现在|先这样)[呀啊吧啦呢]?\s*)?"
_TAIL = r"[了啦咯哦呀哈吧呢～~\s]*"
_RESTORE = re.compile(
    rf"^{_PREFIX}(?:我(?:已经)?(?:醒了|回来了)|继续聊|恢复主动(?:消息|分享)?|取消勿扰|关闭勿扰){_TAIL}$"
)
_MANUAL = re.compile(
    rf"^{_PREFIX}(?:(?:你)?(?:先)?(?:别|不要|不用|不许)(?:再)?打扰(?:我)?|"
    rf"(?:开启|打开|进入)(?:勿扰|免打扰)(?:模式)?|勿扰){_TAIL}$"
)
_SLEEP = re.compile(
    rf"^{_PREFIX}(?:晚安(?:肥鱼|大肥鱼)?|"
    rf"我(?:(?:现在)?(?:先|要|去|准备|打算|先去|要去|准备去|准备要|想去))?"
    rf"(?:睡觉|睡了|睡啦|睡咯|睡会儿|睡一会儿|睡一会|休息|休息一下|休息一会儿|休息一会)){_TAIL}$"
)
_BUSY = re.compile(
    rf"^{_PREFIX}(?:我(?:现在|这会儿)?(?:在忙|很忙|有点忙|不想聊(?:天)?|没空(?:聊(?:天)?)?)|"
    rf"我(?:先|要|去|先去|要去)(?:忙(?:一会儿|一会)?|工作|开会)){_TAIL}$"
)


def _local(now: datetime) -> datetime:
    return now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)


def _set(state: dict, enabled: bool, reason: str, until: float, now: datetime) -> bool:
    before = tuple(state.get(key) for key in _FIELDS)
    desired = (bool(enabled), reason, float(until))
    if before[:3] == desired:
        return False
    state.update(
        dnd=bool(enabled),
        dnd_reason=reason,
        dnd_until=float(until),
        dnd_changed_at=_local(now).timestamp(),
    )
    return True


def refresh_availability(state: dict, now: datetime) -> bool:
    """Expire inferred rest; preserve explicit and ambiguous legacy settings."""
    if not state.get("dnd", False):
        # Avoid churning every previously unannotated available state at startup.
        if state.get("dnd_reason") or state.get("dnd_until"):
            return _set(state, False, "", 0, now)
        return False
    reason = state.get("dnd_reason")
    if state.get("autonomy_mode") == "interest":
        try:
            activated = float(state.get("autonomy_enabled_at", 0))
            changed = float(state.get("dnd_changed_at", 0))
        except (TypeError, ValueError):
            activated, changed = 0, 0
        # The owner's new mode replaces previously inferred/legacy rest. A
        # subsequent explicit pause still wins and is never cleared by chat.
        if reason != "manual" or (activated > 0 and changed < activated):
            return _set(state, False, "", 0, now)
        return False
    if not reason:
        return _set(state, True, "legacy_manual", 0, now)
    if reason not in NATURAL_REASONS:
        return False
    try:
        until = float(state.get("dnd_until", 0))
    except (TypeError, ValueError):
        until = 0
    # Missing, non-finite and invalid natural deadlines must not last forever.
    if not (until > _local(now).timestamp()):
        return _set(state, False, "", 0, now)
    if until == float("inf"):
        return _set(state, False, "", 0, now)
    return False


def _intents(text: str):
    """Yield direct, unquoted owner intent in clause order."""
    clean = re.sub(r"(?m)^\s*>.*$", "", str(text or ""))
    # A separator prevents quoted text removal from joining otherwise unrelated
    # words into a new instruction (for example 我“不是”在忙).
    clean = _QUOTED.sub(" ", clean)
    clean = re.sub(r"https?://\S+", " ", clean)
    for sentence in re.split(r"[。.!！?？；;\n]+", clean):
        if _REPORTED.search(sentence):
            continue
        for clause in re.split(r"[，,]+", sentence):
            clause = clause.strip()
            if _RESTORE.fullmatch(clause):
                yield "restore"
            elif _MANUAL.fullmatch(clause):
                yield "manual"
            elif _SLEEP.fullmatch(clause):
                yield "natural_sleep"
            elif _BUSY.fullmatch(clause):
                yield "natural_busy"


def update_availability(
    state: dict, text: str, now: datetime, explicit: bool | None = None
) -> bool:
    """Apply a new owner message, or an explicit manual on/off control.

    Ordinary owner interaction resumes an inferred rest period. It never
    overrides manual /勿扰, or legacy DND whose origin cannot be established.
    A direct resume instruction can always disable DND.
    """
    if explicit is not None:
        return _set(state, bool(explicit), "manual" if explicit else "", 0, now)
    changed = refresh_availability(state, now)
    intent = next(reversed(list(_intents(text))), None)
    if intent == "restore":
        return _set(state, False, "", 0, now) or changed
    if intent == "manual":
        return _set(state, True, "manual", 0, now) or changed
    if state.get("dnd") and state.get("dnd_reason") not in NATURAL_REASONS:
        return changed
    if state.get("autonomy_mode") == "interest":
        # Saying good night or being busy supplies conversational context, not
        # permission for the program to impose an unrequested activity ban.
        return changed
    if intent in NATURAL_REASONS:
        local = _local(now)
        until = local + timedelta(hours=2)
        if intent == "natural_sleep" and (local.hour >= 20 or local.hour < 8):
            until = local.replace(hour=8, minute=0, second=0, microsecond=0)
            if until <= local:
                until += timedelta(days=1)
        return _set(state, True, intent, until.timestamp(), now) or changed
    if str(text or "").strip() and state.get("dnd_reason") in NATURAL_REASONS:
        return _set(state, False, "", 0, now) or changed
    return changed
