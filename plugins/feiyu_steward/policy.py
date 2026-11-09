"""Small, deterministic delivery policies independent of AstrBot."""

import re
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .availability import refresh_availability

TZ = ZoneInfo("Asia/Shanghai")
OWNER_UMO = "snowluma-qq:FriendMessage:0"
TOPICS = {"编程", "人工智能", "猫猫", "科学趣闻", "抽象日常", "游戏趣闻"}
PUBLIC_DOMAINS = {
    "github.com",
    "www.python.org",
    "blog.python.org",
    "www.nasa.gov",
    "science.nasa.gov",
    "www.esa.int",
    "www.microsoft.com",
    "blogs.microsoft.com",
    "devblogs.microsoft.com",
    "research.google",
    "blog.google",
    "api-docs.deepseek.com",
    "www.bilibili.com",
    "www.nature.com",
    "www.science.org",
}
SENSITIVE = re.compile(
    r"密码|口令|验证码|身份证|银行卡|银行账号|病历|诊断|病情|疾病|治疗|抑郁|药物|手机号|家庭住址|"
    r"cookie|token|api.?key|authorization|sessdata|skey|私聊记录|群聊记录|"
    r"\bsk-[\w-]+|\b1[3-9]\d{9}\b|\b\d{8,}\b|[\w.+-]+@[\w.-]+\.[a-z]{2,}",
    re.IGNORECASE,
)

# Public discussion of token usage is ordinary content. Credential assignments
# and every other private-data check remain blocked; private-memory rules above
# intentionally retain their broader matching.
PUBLIC_COMMENT_SENSITIVE = re.compile(
    SENSITIVE.pattern.replace("cookie|token|", "cookie|")
    + r"|(?<![a-z])(?:access[_ -]?|refresh[_ -]?)?tokens?[\"']?\s*[:=：]\s*\S+"
    + r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)


def public_url(value: str, unrestricted: bool = False) -> bool:
    """Validate a bounded primary/public source URL.

    Args:
        value: Candidate URL supplied by an agent.

    Returns:
        Whether it is an allowed HTTPS source without private parameters.
    """
    try:
        p = urlsplit(value)
        return (
            p.scheme == "https"
            and (p.hostname in PUBLIC_DOMAINS or (unrestricted and _public_source_host(p.hostname)))
            and not p.username
            and not p.password
            and p.port in (None, 443)
            and not p.query
            and not p.fragment
            and len(value) < 800
        )
    except ValueError:
        return False


def _public_source_host(host: str | None) -> bool:
    """Generic public sources may be fetched remotely without local credentials."""
    import ipaddress

    host = (host or "").lower().rstrip(".")
    if "." not in host or host.endswith((".local", ".localhost", ".internal", ".lan")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def quiet_hours(now: datetime) -> bool:
    """Automatic activity is quiet from 02:00 inclusive to 08:00 exclusive."""
    return 2 <= now.astimezone(TZ).hour < 8


def interest_driven(state: dict) -> bool:
    """Whether the owner selected interest-led autonomous activity."""
    return state.get("autonomy_mode") == "interest"


def activity_block(state: dict, now: datetime) -> str:
    """Respect current explicit rest controls without inventing new ones.

    Legacy modes retain the original quiet-hours behavior. Interest mode only
    pauses for a manual DND set after its activation; availability refresh
    clears inferred and superseded DND metadata.
    """
    refresh_availability(state, now)
    if not interest_driven(state) and quiet_hours(now):
        return "quiet_hours"
    if state.get("dnd", False):
        return "user_dnd"
    return ""


def _publication_time(value):
    """Read persisted timestamps without letting an old malformed row break checks."""
    try:
        if isinstance(value, bool) or value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, TZ)
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def qzone_publication_usage(state: dict, now: datetime) -> dict:
    """Count real Qzone publication attempts, shared by all publishing paths.

    Draft creation does not reserve a slot. Uncertain attempts keep theirs after
    a restart, and deleting a published post does not reclaim a slot. Candidate
    rows and comment receipts identify the same attempts by native draft ID,
    comment key or platform post ID. Anonymous legacy delivery stamps are an
    aggregate fallback: taking the larger count avoids counting their mirrored
    candidate records twice. Native guard attempts have their own reservations
    and never append legacy delivery stamps, so they add to that aggregate.
    """
    day = now.astimezone(TZ).date().isoformat()
    try:
        configured = int(state.get("qzone_daily_limit", 10))
    except (TypeError, ValueError, OverflowError):
        configured = 10
    limit = min(configured, 10) if configured > 0 else 10
    reserved = {"publishing", "sending", "sent", "unknown", "sent_unverified",
                "verified", "failed_or_uncertain", "deleted_verified"}
    events, aliases = {}, {}
    next_identifier = 0

    def remember(row, receipt=False, native=False):
        nonlocal next_identifier
        explicit = _publication_time(row.get("publication_attempt_at"))
        moment, priority = explicit, 3
        if moment is None and receipt:
            moment, priority = _publication_time(row.get("at")), 2
        if moment is None:
            moment, priority = _publication_time(row.get("published_day")), 1
        if moment is None and row.get("status") in reserved:
            moment, priority = _publication_time(row.get("created")), 0
        # An explicit reservation remains real even if a later status changed.
        if moment is None or (explicit is None and row.get("status") not in reserved):
            return
        draft_id = row.get("draft_id" if receipt else "id")
        keys = [f"draft:{draft_id}"] if draft_id is not None else []
        keys += [f"{field}:{row[field]}" for field in ("comment_key", "tid") if row.get(field)]
        matched = {aliases[key] for key in keys if key in aliases}
        identifier = min(matched) if matched else next_identifier
        if not matched:
            next_identifier += 1
        previous = [events.pop(key) for key in matched]
        choices = previous + [(priority, moment, native)]
        best = max(choices, key=lambda value: (value[0], -value[1].timestamp()))
        events[identifier] = (best[0], best[1], any(value[2] for value in choices))
        for key, value in list(aliases.items()):
            if value in matched:
                aliases[key] = identifier
        for key in keys:
            aliases[key] = identifier

    for row in state.get("candidates", []):
        if isinstance(row, dict):
            remember(row)
    receipts = state.get("comment_share_receipts", {})
    for row in receipts.values() if isinstance(receipts, dict) else []:
        if isinstance(row, dict) and row.get("destination") == "qzone":
            remember(row, receipt=True)
    native_attempts = state.get("qzone_publication_attempts", {})
    for row in native_attempts.values() if isinstance(native_attempts, dict) else []:
        if isinstance(row, dict):
            remember(row, receipt=True, native=True)
    identified = sum(moment.date().isoformat() == day and not native for _, moment, native in events.values())
    native_count = sum(moment.date().isoformat() == day and native for _, moment, native in events.values())
    legacy = sum(
        parsed is not None and parsed.date().isoformat() == day
        for parsed in (_publication_time(value) for value in state.get("deliveries", {}).get("draft", []))
    )
    used = max(identified, legacy) + native_count
    return {"date": day, "daily_limit": limit, "used": used, "remaining": max(0, limit - used)}


def delivery_block(state: dict, channel: str, now: datetime) -> str:
    """Apply quiet hours, independent daily budgets, and activity checks.

    Args:
        state: Persisted delivery metadata, containing no chat transcript.
        channel: Either private or draft (Qzone candidates).
        now: Timezone-aware current time.

    Returns:
        An empty string when delivery is allowed, otherwise a reason.
    """
    local = now.astimezone(TZ)
    reason = activity_block(state, now)
    if reason:
        return reason
    if not state.get("proactive" if channel == "private" else "drafts", True):
        return "disabled"
    if channel == "draft" and not qzone_publication_usage(state, now)["remaining"]:
        return "daily_limit"
    if interest_driven(state):
        # Per-item delivery reservations and duplicate protection remain in the
        # senders. An uncertain result must not impose a global time budget on
        # unrelated discoveries or permission questions.
        return ""
    # A missing receipt can still mean that QQ delivered the message. Share
    # this reservation across all proactive sources without claiming success.
    if channel == "private":
        attempts = list(state.get("hltv", {}).get("receipts", []))
        attempts += [row for row in state.get("publication_notices", {}).values()
                     if isinstance(row, dict)]
        gap = max(1800, int(state.get("private_cooldown_seconds", 1800)))
        if any(
            item.get("status") in {"sending", "unknown"}
            and 0 <= now.timestamp() - float(item.get("at", 0)) < gap
            for item in attempts
        ):
            return "unknown_dispatch_cooldown"
    last_chat = float(state.get("last_chat", 0))
    if last_chat and now.timestamp() - last_chat < 30 * 60:
        return "active_conversation"
    stamps = state.get("deliveries", {}).get(channel, [])
    daily_limit = (
        int(state.get("private_daily_limit", 1))
        if channel == "private"
        else int(state.get("qzone_daily_limit", 1))
    )
    if (
        daily_limit > 0
        and sum(
            datetime.fromisoformat(x).astimezone(TZ).date() == local.date()
            for x in stamps
        )
        >= daily_limit
    ):
        return "daily_limit"
    if stamps and now.timestamp() - datetime.fromisoformat(stamps[-1]).timestamp() < (
        int(state.get("private_cooldown_seconds", 12 * 3600))
        if channel == "private"
        else int(state.get("qzone_cooldown_seconds", 12 * 3600))
    ):
        return "cooldown"
    return ""


def similar(text: str, previous: list[str]) -> bool:
    """Reject exact and close duplicates.

    Args:
        text: Proposed public text.
        previous: Previously generated public texts.

    Returns:
        Whether the normalized text repeats an earlier item.
    """
    clean = re.sub(r"\W+", "", text.lower())
    return any(
        SequenceMatcher(None, clean, re.sub(r"\W+", "", x.lower())).ratio() >= 0.72
        for x in previous
    )


def public_post_text(value):
    """Qzone collapses blank lines; keep all words, punctuation and URLs exact."""
    return "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())


def voice_suitable(text: str) -> bool:
    """Exclude links, code, structured reports, and long explanations.

    Args:
        text: Complete final reply.

    Returns:
        Whether a short voice reply can replace its text.
    """
    return (
        4 <= len(text) <= 150
        and not re.search(
            r"https?://|www\.|```|`|\n[-*#]|\n\d+[.、]|[{}<>]|付款|账单|Traceback", text
        )
        and text.count("\n") <= 2
    )
