"""Continue interesting public discussions under the bot's own Bili comments."""

import asyncio
import re
import time
from contextvars import ContextVar
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from .availability import refresh_availability
from .policy import (
    PUBLIC_COMMENT_SENSITIVE,
    TZ,
    activity_block,
    interest_driven,
    quiet_hours,
)

_OWNER_FOLLOWUP_MENTION = re.compile(r"[@＠][ \t\u3000]*(?:一团普子|0(?![0-9]))")


def bili_share_source(value, source_platform=""):
    """Return one stable Bili source key without resolving or reading a URL."""
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return ""
        bili_host = host == "bilibili.com" or host.endswith(".bilibili.com")
        short_host = host in {"b23.tv", "www.b23.tv"}
        if not (bili_host or short_host or source_platform == "bilibili"):
            return ""
        if parsed.scheme not in {"http", "https"} or not host:
            return value if source_platform == "bilibili" else ""
        canonical_host = ("www.bilibili.com" if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
                          else "b23.tv" if short_host else host)
        return urlunsplit(("https", canonical_host, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return ""


def owner_mention_usage(state, now):
    """Count owner mentions and Bili QQ shares against one local-day budget.

    A pending or uncertain public write may already have reached Bili. Keep it
    in today's budget until there is a definite rejection, without modifying
    or discarding the publication ledger when a new day begins.
    """
    local_day = now.astimezone(TZ).date()
    try:
        raw_limit = state.get("bili_owner_daily_limit", 10)
        limit = int(raw_limit) if not isinstance(raw_limit, bool) else 10
    except (TypeError, ValueError, OverflowError):
        limit = 10
    if limit <= 0:
        limit = 10
    limit = min(limit, 10)
    def reserved_today(row):
        if not isinstance(row, dict) or row.get("status") not in (None, "sent", "sending", "pending", "unknown"):
            return False
        stamp = None
        for value in (row.get("created"), row.get("confirmed_at")):
            try:
                if isinstance(value, bool) or value is None:
                    continue
                parsed = (datetime.fromtimestamp(value, TZ) if isinstance(value, (int, float))
                          else datetime.fromisoformat(str(value)))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=TZ)
                stamp = parsed.astimezone(TZ)
                break
            except (ValueError, TypeError, OverflowError, OSError):
                continue
        return stamp is not None and stamp.date() == local_day

    ledger = state.get("bili_owner_comments", {})
    used = sum(reserved_today(row) for row in ledger.values()) if isinstance(ledger, dict) else 0
    private_sources = set()
    private_ledger = state.get("bili_private_shares", {})
    for key, row in private_ledger.items() if isinstance(private_ledger, dict) else ():
        if reserved_today(row):
            source = bili_share_source(row.get("source") or key, row.get("source_platform", "bilibili"))
            private_sources.add(source or f"private:{key}")
    legacy = state.get("private_items", [])
    for index, row in enumerate(legacy if isinstance(legacy, list) else ()):
        if reserved_today(row):
            source = bili_share_source(row.get("source"), row.get("source_platform", ""))
            if source:
                private_sources.add(source)
            elif row.get("source_platform") == "bilibili":
                private_sources.add(f"legacy:{index}")
    used += len(private_sources)
    return {"date": local_day.isoformat(), "daily_limit": limit,
            "used": used, "remaining": max(0, limit - used)}


def _shared_bili_cooldown(state, now):
    """Count the latest sent or reserved Bili share across the shared ledgers.

    Uncertain results remain in their original ledgers. A malformed or future
    timestamp cannot establish that the thirty-minute interval has passed.
    """
    comments = state.get("bili_owner_comments", {})
    private = state.get("bili_private_shares", {})
    legacy = state.get("private_items", [])
    if not isinstance(comments, dict) or not isinstance(private, dict) or not isinstance(legacy, list):
        return "bili_share_time_unavailable"
    rows = list(comments.values()) + list(private.values())
    for row in legacy:
        if not isinstance(row, dict):
            continue
        if (row.get("source_platform") == "bilibili"
                or bili_share_source(row.get("source"), row.get("source_platform", ""))):
            rows.append(row)
    for row in rows:
        if not isinstance(row, dict):
            return "bili_share_time_unavailable"
        if row.get("status") not in (None, "sent", "sending", "pending", "unknown"):
            continue
        stamps = []
        for value in (row.get("created"), row.get("confirmed_at")):
            if value is None:
                continue
            try:
                if isinstance(value, bool):
                    raise ValueError
                parsed = (datetime.fromtimestamp(value, TZ) if isinstance(value, (int, float))
                          else datetime.fromisoformat(str(value)))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=TZ)
                stamps.append(parsed.timestamp())
            except (ValueError, TypeError, OverflowError, OSError):
                return "bili_share_time_unavailable"
        if not stamps or any(stamp > now.timestamp() for stamp in stamps):
            return "bili_share_time_unavailable"
        if now.timestamp() - max(stamps) < 1800:
            return "cooldown"
    return ""


def owner_mention_block(state, now):
    """Bili first comments and QQ recommendations share the owner's daily cap."""
    refresh_availability(state, now)
    usage = owner_mention_usage(state, now)
    if usage["used"] >= usage["daily_limit"]:
        return "daily_limit"
    cooldown = _shared_bili_cooldown(state, now)
    if cooldown:
        return cooldown
    if interest_driven(state):
        return activity_block(state, now) or ("" if state.get("proactive", True) else "disabled")
    if state.get("dnd") or quiet_hours(now):
        return "quiet"
    if not state.get("proactive", True):
        return "disabled"
    rows = list(state.get("bili_owner_comments", {}).values())
    if sum(str(r.get("created", "")).startswith(now.date().isoformat()) for r in rows) >= 3:
        return "daily_limit"
    for row in rows:
        try:
            if now.timestamp() - datetime.fromisoformat(row["created"]).timestamp() < 1800:
                return "cooldown"
        except (ValueError, KeyError, TypeError):
            continue
    return ""


def conversation_block(state, root, now):
    """Bound public followups independently of QQ notifications."""
    refresh_availability(state, now)
    if not state.get("bili_conversations"):
        return "disabled"
    if interest_driven(state):
        return activity_block(state, now) or None
    if state.get("dnd") or quiet_hours(now):
        return "quiet"
    rows = state.get("bili_conversation_replies", {}).values()
    today = [
        r for r in rows if str(r.get("created", "")).startswith(now.date().isoformat())
    ]
    if len(today) >= 8:
        return "daily_limit"
    if sum(str(r.get("root")) == str(root) for r in today) >= 3:
        return "thread_limit"
    if any(0 <= now.timestamp() - r.get("at", 0) < 300 for r in today):
        return "cooldown"
    return None


class BiliConversations:
    """Reuse native interest filtering, event dedup and reply memory."""

    def install_bili_conversations(self, bili):
        """Install scoped instance hooks without editing the native plugin."""
        names = ("_generate_reply", "_apply_reply_result", "_send_reply")
        if not all(hasattr(bili, name) for name in names):
            return None
        originals = {name: getattr(bili, name) for name in names}
        target = ContextVar("feiyu_bili_reply_target", default=None)
        lock = asyncio.Lock()

        def own_root(oid):
            row = self.state.get("bili_owner_comments", {}).get(str(oid), {})
            return str(row.get("rpid") or "") if row.get("status") == "sent" else ""

        async def resolve_root(oid, thread):
            root = own_root(oid)
            if not root:
                return ""
            if str(thread) == root:
                return root
            # Some notification variants omit root_id. Verify membership in our
            # real floor instead of assuming every @ on the same video is ours.
            data, _ = await bili._http_get(
                "https://api.bilibili.com/x/v2/reply/reply",
                params={"oid": oid, "type": 1, "root": root, "pn": 1, "ps": 20},
            )
            if data.get("code") != 0:
                return ""
            rows = (data.get("data") or {}).get("replies") or []
            for row in rows[:20]:
                if (
                    str(row.get("rpid")) == str(thread)
                    and str(row.get("root") or root) == root
                ):
                    return root
            return ""

        async def generate(
            content,
            mid,
            username,
            thread_id,
            oid,
            comment_type,
            image_desc="",
            channel="comment",
            **kwargs,
        ):
            if channel != "comment":
                return await originals["_generate_reply"](
                    content,
                    mid,
                    username,
                    thread_id,
                    oid,
                    comment_type,
                    image_desc=image_desc,
                    channel=channel,
                    **kwargs,
                )
            ignored = {"decision": "ignore", "reply": ""}
            if str(comment_type) != "1" or str(mid) == str(
                bili.config.get("DEDE_USER_ID")
            ):
                return ignored
            root = await resolve_root(oid, thread_id)
            if not root or conversation_block(self.state, root, datetime.now(TZ)):
                return ignored
            if bili._interaction_filter_reason(content, "comment"):
                return ignored
            interested, _ = await bili._should_reply_by_interest(
                content, username, "comment"
            )
            if not interested:
                return ignored
            result = await originals["_generate_reply"](
                content,
                mid,
                username,
                root,
                oid,
                comment_type,
                image_desc=image_desc,
                channel=channel,
                **kwargs,
            )
            if isinstance(result, dict):
                result["_feiyu_root"] = root
            return result

        async def apply(**kwargs):
            root = str(
                kwargs.get("result", {}).get("_feiyu_root")
                or kwargs.get("thread_id")
                or ""
            )
            if root != own_root(kwargs.get("oid")):
                return False
            token = target.set((str(kwargs.get("oid")), root, str(kwargs.get("rpid"))))
            try:
                return await originals["_apply_reply_result"](
                    **{**kwargs, "thread_id": root}
                )
            finally:
                target.reset(token)

        async def send(oid, rpid, reply_type, content):
            selected = target.get()
            if (
                not selected
                or selected != (str(oid), own_root(oid), str(rpid))
                or str(reply_type) != "1"
            ):
                return False
            # A reply can mention other readers naturally. Convert only the
            # owner's mention to a plain name so generated followups cannot
            # become repeated notifications outside the recommendation cap.
            text = _OWNER_FOLLOWUP_MENTION.sub("一团普子", str(content or "")).strip()
            if not 1 <= len(text) <= 1000 or PUBLIC_COMMENT_SENSITIVE.search(text):
                return False
            async with lock:
                now = datetime.now(TZ)
                root = selected[1]
                rows = self.state.setdefault("bili_conversation_replies", {})
                key = f"{oid}:{rpid}"
                if key in rows or conversation_block(self.state, root, now):
                    return False
                rows[key] = {
                    "status": "pending",
                    "root": root,
                    "parent": str(rpid),
                    "oid": str(oid),
                    "created": now.isoformat(),
                    "at": time.time(),
                }
                self.save()
                try:
                    data, _ = await bili._http_post(
                        "https://api.bilibili.com/x/v2/reply/add",
                        data={
                            "oid": oid,
                            "type": 1,
                            "root": root,
                            "parent": rpid,
                            "message": text,
                            "plat": 1,
                            "csrf": bili.config.get("BILI_JCT", ""),
                        },
                    )
                    result = data.get("data") or {}
                    new_id = result.get("rpid") or (result.get("reply") or {}).get(
                        "rpid"
                    )
                    ok = data.get("code") == 0 and bool(new_id)
                    rows[key].update(
                        status="sent"
                        if ok
                        else "unknown"
                        if data.get("code") == 0
                        else "rejected",
                        rpid=new_id,
                        code=data.get("code"),
                        text=text,
                        confirmed_at=time.time() if ok else None,
                    )
                    self.save()
                    return ok
                except Exception:  # noqa: BLE001 - a dispatched public write must never be retried blindly
                    rows[key]["status"] = "unknown"
                    self.save()
                    return False

        hooks = dict(zip(names, (generate, apply, send), strict=True))
        for name, handler in hooks.items():
            setattr(bili, name, handler)

        def restore():
            for name, handler in hooks.items():
                if getattr(bili, name) is handler:
                    setattr(bili, name, originals[name])

        return restore
