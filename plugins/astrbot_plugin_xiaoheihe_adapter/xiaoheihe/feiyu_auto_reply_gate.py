"""Durable limits for automatic Xiaoheihe reply generation and sending.

This stores event IDs, days and hashed post IDs only.  It never reads message
content, credentials or error strings, and it never creates external requests.
The adapter calls it before building automatic event context; manual owner
comments do not enter this gate.
"""
from __future__ import annotations

from . import feiyu_recovery
from . import feiyu_unseen_feed

import hashlib
import math
import re
import time
import weakref
from contextlib import asynccontextmanager
from datetime import date, datetime, time as day_time, timedelta, timezone

from .feiyu_peak_bridge import gate_allowed, revoke_event, tracked_event

DAILY_LIMIT = 10
LOCAL_TZ = timezone(timedelta(hours=8))
AUTO_TYPES = frozenset({"reply", "mention", "proactive_feed"})
# A proactive-feed event is an autonomous top-level comment.  The owner's
# daily ten-opportunity budget applies to replies/mentions only; comments keep
# their post-level de-duplication and all other gates, but do not consume it.
COUNTED_TYPES = frozenset({"reply", "mention"})
MAX_LEGACY_POSTS = 200_000
_peak_binding = None
_history_ready_repositories = weakref.WeakSet()


def register_peak_calendar(calendar):
    """Bind the calendar and return a unique owner token for this registration."""
    if not callable(calendar):
        raise ValueError("invalid_peak_calendar")
    global _peak_binding
    token = object()
    _peak_binding = (token, calendar)
    return token


def unregister_peak_calendar(token):
    """Only the current registration owner can remove its calendar."""
    global _peak_binding
    if _peak_binding is not None and _peak_binding[0] is token:
        _peak_binding = None
        return True
    return False


def peak_calendar_registered(token):
    """Check whether a steward instance still owns this module's binding."""
    return _peak_binding is not None and _peak_binding[0] is token


def _period_allowed(kind, stamp, *, repository=None, event_id=None, route=None):
    binding = _peak_binding
    if binding is None:
        return False
    try:
        peak = binding[1](stamp)
    except Exception:
        return False
    if binding is not _peak_binding or (peak is not True and peak is not False and peak is not None):
        return False
    if peak is False:
        if kind == "proactive_feed" and tracked_event(repository, event_id):
            return gate_allowed(repository, event_id, route)
        return True
    # Only the active, exact top-level feed candidate may use the peak lane.
    # Replies and mentions, missing calendar information, and unrelated work
    # still require verified off-peak time.
    return (peak is True and kind == "proactive_feed"
            and gate_allowed(repository, event_id, route))


def _post_key(post_id):
    if not isinstance(post_id, str) or not re.fullmatch(r"[1-9][0-9]{0,29}", post_id):
        return None
    return hashlib.sha256(("feiyu-automatic-post-v1:" + post_id).encode("ascii")).hexdigest()


def _clock(now=None):
    value = time.time() if now is None else now
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid_clock")
    local = datetime.fromtimestamp(value, LOCAL_TZ)
    start = datetime.combine(local.date(), day_time.min, tzinfo=LOCAL_TZ).timestamp()
    return local.date().isoformat(), start, start + 86400, value


def _day_window(day):
    """Return the UTC timestamps for one Hong Kong calendar day."""
    parsed = date.fromisoformat(day)
    start = datetime.combine(parsed, day_time.min, tzinfo=LOCAL_TZ).timestamp()
    return start, start + 86400


def _current_period_allowed(kind, day, *, now=None, repository=None, event_id=None, route=None):
    """Finish asynchronous checks with a synchronous day and period check."""
    try:
        current_day, _, _, stamp = _clock(now)
        return current_day == day and _period_allowed(
            kind, stamp, repository=repository, event_id=event_id, route=route,
        )
    except (ValueError, OverflowError, OSError):
        return False


async def _all(connection, sql, params=()):
    async with connection.execute(sql, params) as cursor:
        return await cursor.fetchall()


async def _one(connection, sql, params=()):
    async with connection.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def _legacy_reply_count(
    connection, current_id, boundary_id, start, end, initialized_at,
):
    """Count only baseline reply/mention opportunities.

    The original gate's ``legacy_count`` intentionally remains the count of
    all legacy rows so its integrity check and post de-duplication are not
    rewritten.  This parallel count classifies only rows whose event type is
    known to be a reply/mention.  Standalone outgoing rows without an incoming
    event are not guessed to be replies.
    """
    boundary = min(end, initialized_at)
    if boundary <= start:
        return 0
    rows = await _all(connection, """
        SELECT 'e:' || e.id AS key FROM incoming_events e
         WHERE e.id <= ? AND e.id <> ? AND e.event_type IN ('reply','mention')
           AND e.claimed_at >= ? AND e.claimed_at < ?
        UNION
        SELECT CASE WHEN e.id IS NULL THEN 'o:' || o.id ELSE 'e:' || e.id END AS key
          FROM outgoing_replies o LEFT JOIN incoming_events e ON e.id=o.incoming_event_id
         WHERE e.id IS NOT NULL AND e.id <= ? AND e.id <> ?
           AND e.event_type IN ('reply','mention')
           AND ((o.attempted_at >= ? AND o.attempted_at < ?)
                OR (o.confirmed_at >= ? AND o.confirmed_at < ?)
                OR o.status='sending'
                OR (o.status='send_unknown' AND o.attempted_at >= ? AND o.attempted_at < ?))
           AND o.status IN ('sent','sending','send_unknown','retry_wait','dry_run')
    """, (boundary_id, current_id, start, boundary,
           boundary_id, current_id, start, boundary, start, boundary,
           start - 86400, boundary))
    return len(rows)


@asynccontextmanager
async def _read_snapshot(database):
    """Read a consistent snapshot without creating or modifying gate state."""
    async with database._write_lock:
        connection = database.connection
        await connection.execute("BEGIN")
        try:
            yield connection
        finally:
            await connection.rollback()


async def _initialize(connection, current_id, day, start, end, initialized_at):
    # Executed in the adapter's existing BEGIN IMMEDIATE transaction.  If any
    # required history table is unavailable, the entire transaction rolls back.
    table_names = {row["name"] for row in await _all(connection, """
        SELECT name FROM sqlite_master WHERE type='table' AND name IN
        ('feiyu_auto_reply_meta','feiyu_auto_reply_posts','feiyu_auto_reply_reservations',
         'feiyu_auto_reply_legacy')
    """)}
    required = {"feiyu_auto_reply_meta", "feiyu_auto_reply_posts", "feiyu_auto_reply_reservations", "feiyu_auto_reply_legacy"}
    if table_names and table_names != required:
        raise ValueError("partial_gate_state")
    first_use = not table_names
    await connection.execute("""CREATE TABLE IF NOT EXISTS feiyu_auto_reply_meta (
        id INTEGER PRIMARY KEY CHECK (id=1), legacy_day TEXT NOT NULL,
        legacy_count INTEGER NOT NULL, initialized_at REAL NOT NULL,
        legacy_reply_count INTEGER)""")
    # Existing installations predate the reply-only budget.  Add a nullable
    # column in place and populate it from the preserved event ledger below;
    # do not rewrite or discard the old all-opportunity count.
    meta_columns = {row["name"] for row in await _all(
        connection, "PRAGMA table_info(feiyu_auto_reply_meta)"
    )}
    if "legacy_reply_count" not in meta_columns:
        await connection.execute(
            "ALTER TABLE feiyu_auto_reply_meta ADD COLUMN legacy_reply_count INTEGER"
        )
    await connection.execute("""CREATE TABLE IF NOT EXISTS feiyu_auto_reply_posts (
        post_hash TEXT PRIMARY KEY, source TEXT NOT NULL)""")
    await connection.execute("""CREATE TABLE IF NOT EXISTS feiyu_auto_reply_legacy (
        entry_hash TEXT PRIMARY KEY)""")
    await connection.execute("""CREATE TABLE IF NOT EXISTS feiyu_auto_reply_reservations (
        event_id INTEGER PRIMARY KEY, post_hash TEXT NOT NULL UNIQUE, profile_id TEXT NOT NULL,
        event_type TEXT NOT NULL, day TEXT NOT NULL, reserved_at REAL NOT NULL,
        dispatched_at REAL, send_started_at REAL, send_blocked INTEGER NOT NULL DEFAULT 0,
        revoked INTEGER NOT NULL DEFAULT 0)""")
    existing = await _one(connection, """SELECT legacy_day,legacy_count,initialized_at,
        legacy_reply_count FROM feiyu_auto_reply_meta WHERE id=1""")
    if existing is not None:
        if first_use:
            raise ValueError("invalid_gate_state")
        reply_count = existing["legacy_reply_count"]
        if reply_count is None:
            try:
                legacy_start, legacy_end = _day_window(str(existing["legacy_day"]))
                watermark = await _one(connection, """SELECT entry_hash
                    FROM feiyu_auto_reply_legacy
                    WHERE entry_hash GLOB 'legacy-max-event:*'""")
                if watermark is None:
                    raise ValueError("missing_gate_watermark")
                raw_boundary = str(watermark["entry_hash"]).split(":", 1)[1]
                if not raw_boundary.isdigit():
                    raise ValueError("invalid_gate_watermark")
                reply_count = await _legacy_reply_count(
                    connection, current_id, int(raw_boundary),
                    legacy_start, legacy_end,
                    float(existing["initialized_at"]),
                )
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                raise ValueError("invalid_gate_state")
            await connection.execute(
                "UPDATE feiyu_auto_reply_meta SET legacy_reply_count=? WHERE id=1",
                (reply_count,),
            )
        return (existing["legacy_day"], existing["legacy_count"],
                existing["initialized_at"], reply_count)
    if not first_use:
        raise ValueError("missing_gate_metadata")
    # Freeze an event watermark inside the same transaction as the migration.
    # This is stronger than timestamps when the wall clock is adjusted.
    last_event = await _one(connection, "SELECT COALESCE(MAX(id), 0) AS n FROM incoming_events")
    boundary = f"legacy-max-event:{int(last_event['n'])}"
    await connection.execute("INSERT INTO feiyu_auto_reply_legacy VALUES (?)", (boundary,))
    # Read only identifiers/state/timestamps.  Count sources without double
    # counting a candidate or outgoing record linked to the same event.
    history = await _all(connection, """
        SELECT post_id FROM incoming_events
         WHERE id <> ? AND event_type IN ('reply','mention','proactive_feed')
        UNION
        SELECT o.post_id FROM outgoing_replies o LEFT JOIN incoming_events e ON e.id=o.incoming_event_id
         WHERE (o.incoming_event_id IS NULL OR o.incoming_event_id <> ?)
           AND (e.id IS NULL OR e.event_type IN ('reply','mention','proactive_feed'))
           AND o.status IN ('sent','sending','send_unknown','retry_wait','dry_run')
        UNION
        SELECT post_id FROM feed_candidates WHERE incoming_event_id IS NULL OR incoming_event_id <> ?
        LIMIT ?
    """, (current_id, current_id, current_id, MAX_LEGACY_POSTS + 1))
    if len(history) > MAX_LEGACY_POSTS:
        raise ValueError("legacy_history_too_large")
    hashes = set()
    for row in history:
        key = _post_key(str(row["post_id"] or ""))
        if key is not None:
            hashes.add(key)
    if hashes:
        await connection.executemany("INSERT OR IGNORE INTO feiyu_auto_reply_posts VALUES (?, 'legacy')",
                                     [(key,) for key in hashes])
    today = await _all(connection, """
        SELECT 'e:' || e.id AS key FROM incoming_events e
         WHERE e.id <> ? AND e.event_type IN ('reply','mention','proactive_feed')
           AND e.claimed_at >= ? AND e.claimed_at < ?
        UNION
        SELECT CASE WHEN e.id IS NULL THEN 'o:' || o.id ELSE 'e:' || e.id END AS key
          FROM outgoing_replies o LEFT JOIN incoming_events e ON e.id=o.incoming_event_id
         WHERE (o.incoming_event_id IS NULL OR o.incoming_event_id <> ?)
           AND (e.id IS NULL OR e.event_type IN ('reply','mention','proactive_feed'))
           AND ((o.attempted_at >= ? AND o.attempted_at < ?)
                OR (o.confirmed_at >= ? AND o.confirmed_at < ?)
                OR o.status='sending'
                OR (o.status='send_unknown' AND o.attempted_at >= ?))
           AND o.status IN ('sent','sending','send_unknown','retry_wait','dry_run')
        UNION
        SELECT CASE WHEN f.incoming_event_id IS NULL THEN 'c:' || f.id ELSE 'e:' || f.incoming_event_id END AS key
          FROM feed_candidates f WHERE (f.incoming_event_id IS NULL OR f.incoming_event_id <> ?)
           AND f.created_at >= ? AND f.created_at < ?
    """, (current_id, start, end, current_id, start, end, start, end, start-86400, current_id, start, end))
    count = len(today)
    if today:
        await connection.executemany("INSERT INTO feiyu_auto_reply_legacy VALUES (?)", [
            (hashlib.sha256(("feiyu-legacy-opportunity-v1:" + str(row["key"])).encode("utf-8")).hexdigest(),)
            for row in today
        ])
    reply_count = await _legacy_reply_count(
        connection, current_id, int(last_event["n"]), start, end, initialized_at,
    )
    await connection.execute(
        """INSERT INTO feiyu_auto_reply_meta
           (id,legacy_day,legacy_count,initialized_at,legacy_reply_count)
           VALUES (1, ?, ?, ?, ?)""",
        (day, count, initialized_at, reply_count),
    )
    return day, count, initialized_at, reply_count


async def _validate_state(connection, legacy_day, legacy_count, initialized_at,
                          legacy_reply_count, day):
    if (not isinstance(legacy_day, str) or type(legacy_count) is not int
            or legacy_count < 0 or type(legacy_reply_count) is not int
            or legacy_reply_count < 0 or legacy_reply_count > legacy_count):
        raise ValueError("invalid_gate_state")
    parsed = date.fromisoformat(legacy_day)
    if parsed.isoformat() != legacy_day or legacy_day > day:
        raise ValueError("invalid_gate_state")
    if (type(initialized_at) not in (int, float) or not math.isfinite(initialized_at)
            or initialized_at <= 0 or _clock(initialized_at)[0] != legacy_day):
        raise ValueError("invalid_gate_state")
    if await _one(connection, "SELECT 1 FROM feiyu_auto_reply_meta WHERE id<>1 LIMIT 1"):
        raise ValueError("invalid_gate_state")
    legacy = await _one(connection, """SELECT COUNT(*) AS n FROM feiyu_auto_reply_legacy
        WHERE length(entry_hash)=64 AND entry_hash NOT GLOB '*[^0-9a-f]*'""")
    malformed_legacy = await _one(connection, """SELECT 1 FROM feiyu_auto_reply_legacy
        WHERE NOT (length(entry_hash)=64 AND entry_hash NOT GLOB '*[^0-9a-f]*')
          AND NOT (entry_hash GLOB 'legacy-max-event:*'
                   AND length(substr(entry_hash,18))>0
                   AND substr(entry_hash,18) NOT GLOB '*[^0-9]*'
                   AND (substr(entry_hash,18)='0' OR substr(entry_hash,18,1)<>'0')
                   AND length(substr(entry_hash,18))<=19) LIMIT 1""")
    watermarks = await _all(connection, """SELECT entry_hash FROM feiyu_auto_reply_legacy
        WHERE entry_hash GLOB 'legacy-max-event:*'""")
    if int(legacy["n"]) != legacy_count or malformed_legacy or len(watermarks) != 1:
        raise ValueError("invalid_gate_state")
    bad_post = await _one(connection, """
        SELECT 1 FROM feiyu_auto_reply_posts p
        LEFT JOIN feiyu_auto_reply_reservations r ON r.post_hash=p.post_hash
        WHERE length(p.post_hash)<>64 OR p.post_hash GLOB '*[^0-9a-f]*'
           OR p.source NOT IN ('legacy','reservation')
           OR (p.source='reservation' AND r.event_id IS NULL)
           OR (p.source='legacy' AND r.event_id IS NOT NULL) LIMIT 1
    """)
    bad_reservation = await _one(connection, """
        SELECT 1 FROM feiyu_auto_reply_reservations r
        LEFT JOIN feiyu_auto_reply_posts p ON p.post_hash=r.post_hash
        WHERE p.post_hash IS NULL OR p.source<>'reservation'
           OR typeof(r.event_id)<>'integer' OR r.event_id<=0
           OR r.event_type NOT IN ('reply','mention','proactive_feed')
           OR typeof(r.profile_id)<>'text' OR length(r.profile_id) NOT BETWEEN 1 AND 100
           OR length(r.day)<>10 OR strftime('%Y-%m-%d', julianday(r.day)) IS NOT r.day
           OR r.day>? OR r.day<?
           OR typeof(r.reserved_at) NOT IN ('integer','real') OR r.reserved_at<=0
           OR strftime('%Y-%m-%d',r.reserved_at,'unixepoch','+08:00') IS NOT r.day
           OR (r.dispatched_at IS NOT NULL AND (typeof(r.dispatched_at) NOT IN ('integer','real') OR r.dispatched_at<r.reserved_at))
           OR (r.send_started_at IS NOT NULL AND (r.dispatched_at IS NULL OR typeof(r.send_started_at) NOT IN ('integer','real') OR r.send_started_at<r.dispatched_at))
           OR typeof(r.send_blocked)<>'integer' OR r.send_blocked NOT IN (0,1)
           OR (r.send_blocked=1 AND r.send_started_at IS NULL)
           OR typeof(r.revoked)<>'integer' OR r.revoked NOT IN (0,1)
        LIMIT 1
    """, (day, legacy_day))
    if bad_post or bad_reservation:
        raise ValueError("invalid_gate_state")


async def _used_today(connection, day, start, end, legacy_day,
                      legacy_reply_count, initialized_at):
    row = await _one(connection, """SELECT COUNT(*) AS n
        FROM feiyu_auto_reply_reservations
        WHERE day=? AND event_type IN ('reply','mention')""", (day,))
    used = int(row["n"]) + (legacy_reply_count if day == legacy_day else 0)
    # A send crossing midnight, including an unresolved result, uses a slot on
    # the new day as well. Never infer that an unknown request did not happen.
    carry = await _one(connection, """
        SELECT COUNT(*) AS n FROM feiyu_auto_reply_reservations r
         WHERE r.day<>? AND r.event_type IN ('reply','mention')
           AND r.send_started_at IS NOT NULL AND r.send_blocked=0
           AND (EXISTS (SELECT 1 FROM outgoing_replies o WHERE o.incoming_event_id=r.event_id
                       AND (o.status='sending' OR (o.confirmed_at>=? AND o.confirmed_at<?)
                            OR (o.attempted_at>=? AND o.attempted_at<?)
                            OR (o.status='send_unknown' AND r.send_started_at>=?)))
                OR (NOT EXISTS (SELECT 1 FROM outgoing_replies o WHERE o.incoming_event_id=r.event_id)
                    AND EXISTS (SELECT 1 FROM incoming_events e WHERE e.id=r.event_id
                                AND e.status IN ('context_ready','dispatched','generated'))))
    """, (day, start, end, start, end, start-86400))
    used += int(carry["n"])
    if day != legacy_day:
        # Legacy attempts have no reservation row. Keep prior in-flight
        # attempts in the new day without including newly created manual writes.
        legacy_carry = await _one(connection, """
            SELECT COUNT(DISTINCT CASE WHEN o.incoming_event_id IS NULL THEN 'o:' || o.id
                                       ELSE 'e:' || o.incoming_event_id END) AS n
            FROM outgoing_replies o LEFT JOIN incoming_events e ON e.id=o.incoming_event_id
            WHERE o.attempted_at < ? AND o.attempted_at < ?
              AND e.id IS NOT NULL AND e.event_type IN ('reply','mention')
              AND (o.status='sending' OR (o.confirmed_at>=? AND o.confirmed_at<?)
                   OR (o.status='send_unknown' AND o.attempted_at>=?))
        """, (initialized_at, start, start, end, start-86400))
        used += int(legacy_carry["n"])
    return used


async def initialize_history(repository, *, now=None):
    """Establish the conservative legacy baseline before starting new pollers.

    Caller must quiesce the old runtime first; a stale backup cannot prove
    history that was already deleted. Failure never enables the gate.
    """
    _history_ready_repositories.discard(repository)
    try:
        async with repository.db.transaction() as connection:
            day, start, end, stamp = _clock(now)
            legacy_day, count, initialized_at, reply_count = await _initialize(
                connection, -1, day, start, end, stamp,
            )
            await _validate_state(
                connection, legacy_day, count, initialized_at, reply_count, day,
            )
        if _clock(now)[0] != day:
            return False
        _history_ready_repositories.add(repository)
        return True
    except Exception:
        return False


async def usage(repository, *, now=None):
    """Report durable usage without exposing message contents or credentials."""
    try:
        if repository not in _history_ready_repositories:
            return {"available": False, "limit": DAILY_LIMIT, "used": None}
        async with _read_snapshot(repository.db) as connection:
            day, start, end, stamp = _clock(now)
            if not _period_allowed("reply", stamp):
                return {"available": False, "limit": DAILY_LIMIT, "used": None}
            meta = await _one(connection, """SELECT legacy_day,legacy_count,
                initialized_at,legacy_reply_count FROM feiyu_auto_reply_meta
                WHERE id=1""")
            if meta is None:
                return {"available": False, "limit": DAILY_LIMIT, "used": None}
            legacy_day = meta["legacy_day"]
            legacy_count = meta["legacy_count"]
            initialized_at = meta["initialized_at"]
            reply_count = meta["legacy_reply_count"]
            await _validate_state(
                connection, legacy_day, legacy_count, initialized_at,
                reply_count, day,
            )
            used = await _used_today(
                connection, day, start, end, legacy_day, reply_count,
                initialized_at,
            )
        if not _current_period_allowed("reply", day, now=now):
            return {"available": False, "limit": DAILY_LIMIT, "used": None}
        return {"available": True, "day": day, "limit": DAILY_LIMIT, "used": used}
    except Exception:
        return {"available": False, "limit": DAILY_LIMIT, "used": None}


async def reserve(repository, event_id, notification, *, now=None, daily_limit=DAILY_LIMIT):
    """Reserve one opportunity before context/vision/compression/main models.

    A completed or unknown native outgoing is left intact for reconciliation.
    All other denials become ignored and are never scheduled for a later day.
    """
    try:
        if repository not in _history_ready_repositories:
            return False, "history_unavailable", True
        if type(event_id) is not int or event_id <= 0 or type(daily_limit) is not int or not 0 <= daily_limit <= 10:
            return False, "invalid_gate_input", False
        post = str(notification.post_id or "")
        key = _post_key(post)
        profile = str(notification.profile_id or "")
        kind = str(notification.event_type.value)
        if key is None or not profile or len(profile) > 100 or kind not in AUTO_TYPES:
            return False, "invalid_gate_input", False
        # Check the current period and exact candidate before recovery reserve
        # can irreversibly advance its independent ledger to reserved.
        if feiyu_recovery.identity_allowed(repository, event_id, notification=notification) is not None:
            day, _, _, stamp = _clock(now)
            if (not _period_allowed(kind, stamp, repository=repository, event_id=event_id,
                                    route=notification.route)
                    or gate_allowed(repository, event_id, notification.route) is not True
                    or not _current_period_allowed(kind, day, now=now, repository=repository,
                                                   event_id=event_id, route=notification.route)):
                return False, "period_not_allowed", False
        recovery = await feiyu_recovery.reserve(repository, event_id, notification)
        if recovery is not None:
            day, _, _, stamp = _clock(now)
            if (not _period_allowed(kind, stamp, repository=repository, event_id=event_id,
                                    route=notification.route)
                    or not _current_period_allowed(kind, day, now=now, repository=repository,
                                                   event_id=event_id, route=notification.route)):
                return False, "period_not_allowed", False
            return recovery
        async with repository.db.transaction() as connection:
            day, start, end, stamp = _clock(now)
            if kind == "proactive_feed" and not await feiyu_unseen_feed.valid(
                    repository, event_id, notification.route, connection=connection):
                return False, "fresh_feed_admission_invalid", True
            row = await _one(connection, "SELECT profile_id,post_id,event_type,status FROM incoming_events WHERE id=?", (event_id,))
            if row is None or (str(row["profile_id"]), str(row["post_id"]), str(row["event_type"])) != (profile, post, kind):
                return False, "event_identity_mismatch", False
            previous = await _one(connection, "SELECT status FROM outgoing_replies WHERE incoming_event_id=? ORDER BY id DESC LIMIT 1", (event_id,))
            if previous is not None:
                return False, "prior_outgoing_preserved", True
            if row["status"] != "claimed":
                # Old queued, ignored or completed records never become new work.
                return False, "event_not_fresh", True
            if not _period_allowed(kind, stamp, repository=repository,
                                   event_id=event_id, route=notification.route):
                return False, "period_not_allowed", False
            legacy_day, legacy_count, initialized_at, reply_count = await _initialize(
                connection, event_id, day, start, end, stamp,
            )
            await _validate_state(
                connection, legacy_day, legacy_count, initialized_at,
                reply_count, day,
            )
            watermark = await _one(connection, "SELECT entry_hash FROM feiyu_auto_reply_legacy WHERE entry_hash GLOB 'legacy-max-event:*'")
            if event_id <= int(str(watermark["entry_hash"]).split(":", 1)[1]):
                return False, "event_predates_gate", True
            # Run after the dedicated recovery branch, within the same
            # BEGIN IMMEDIATE transaction that establishes ordinary authority.
            if await feiyu_unseen_feed.manual_outgoing_blocked(connection, profile, post):
                return False, "manual_post_preserved", False
            if await _one(connection, "SELECT 1 FROM feiyu_auto_reply_posts WHERE post_hash=?", (key,)):
                return False, "post_already_processed", False
            # Preserve detection of preexisting writes that may have been
            # created by a pipeline already in flight during initialization.
            previous_post = await _one(connection, """
                SELECT 1 FROM outgoing_replies o LEFT JOIN incoming_events e ON e.id=o.incoming_event_id
                 WHERE o.post_id=? AND o.incoming_event_id IS NOT NULL AND o.incoming_event_id<>?
                   AND (e.id IS NULL OR e.event_type IN ('reply','mention','proactive_feed'))
                   AND o.status IN ('sent','sending','send_unknown','retry_wait') LIMIT 1
            """, (post, event_id))
            if previous_post is not None:
                await connection.execute("INSERT OR IGNORE INTO feiyu_auto_reply_posts VALUES (?, 'legacy')", (key,))
                return False, "post_already_processed", False
            if kind in COUNTED_TYPES:
                used = await _used_today(
                    connection, day, start, end, legacy_day, reply_count,
                    initialized_at,
                )
                if used >= daily_limit:
                    return False, "daily_limit", False
            final_day, _, _, final_stamp = _clock(now)
            if final_day != day or not _period_allowed(
                kind, final_stamp, repository=repository, event_id=event_id,
                route=notification.route,
            ):
                return False, "period_not_allowed", False
            await connection.execute("INSERT INTO feiyu_auto_reply_posts VALUES (?, 'reservation')", (key,))
            await connection.execute("""INSERT INTO feiyu_auto_reply_reservations
                (event_id,post_hash,profile_id,event_type,day,reserved_at) VALUES (?,?,?,?,?,?)""",
                (event_id, key, profile, kind, day, final_stamp))
        # The reservation remains consumed if commit crosses the boundary.
        if kind == "proactive_feed" and not await feiyu_unseen_feed.valid(repository, event_id, notification.route):
            return False, "fresh_feed_admission_invalid", True
        if not _current_period_allowed(kind, day, now=now, repository=repository,
                                       event_id=event_id, route=notification.route):
            return False, "period_not_allowed", False
        return True, "reserved", False
    except Exception:
        return False, "gate_unavailable", True


async def block_event(repository, event_id, reason):
    """Irrevocably stop active work without altering a send or old queue."""
    revoke_event(repository, event_id)
    try:
        async with repository.db.transaction() as connection:
            table = await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feiyu_auto_reply_reservations'")
            if table is not None:
                await connection.execute("""UPDATE feiyu_auto_reply_reservations SET revoked=1
                    WHERE event_id=?
                      AND EXISTS (SELECT 1 FROM incoming_events e WHERE e.id=?
                                  AND e.status IN ('claimed','context_ready','dispatched','generated','ignored','dead_letter'))
                      AND NOT EXISTS (SELECT 1 FROM outgoing_replies o WHERE o.incoming_event_id=?)""",
                    (event_id, event_id, event_id))
            stamp = time.time()
            await connection.execute("""UPDATE incoming_events SET status='ignored',error=?,should_filter=1,
                completed_at=COALESCE(completed_at,?),updated_at=? WHERE id=?
                AND status IN ('claimed','context_ready','dispatched','generated')
                AND NOT EXISTS (SELECT 1 FROM outgoing_replies o WHERE o.incoming_event_id=?)""",
                ("feiyu_auto_reply_gate:" + str(reason), stamp, stamp, event_id, event_id))
    except Exception:
        pass


async def authorize(repository, event_id, route, *, phase, now=None):
    """Check bound same-day reservation; dispatch and send can each begin once."""
    try:
        if repository not in _history_ready_repositories:
            return False
        if type(event_id) is not int or event_id <= 0 or phase not in {"context", "check", "dispatch", "send", "http"}:
            return False
        key = _post_key(str(route.post_id or ""))
        if key is None:
            return False
        day, _, _, stamp = _clock(now)
        if _period_allowed("proactive_feed", stamp, repository=repository,
                            event_id=event_id, route=route):
            recovery = await feiyu_recovery.authorize(repository, event_id, route, phase=phase, now=now)
            if recovery is not None:
                return bool(recovery and _current_period_allowed(
                    "proactive_feed", day, now=now, repository=repository, event_id=event_id, route=route))
        async with repository.db.transaction() as connection:
            day, _, _, stamp = _clock(now)
            meta = await _one(connection, """SELECT legacy_day,legacy_count,
                initialized_at,legacy_reply_count FROM feiyu_auto_reply_meta
                WHERE id=1""")
            if meta is None:
                return False
            await _validate_state(
                connection, meta["legacy_day"], meta["legacy_count"],
                meta["initialized_at"], meta["legacy_reply_count"], day,
            )
            row = await _one(connection, """SELECT r.post_hash,r.profile_id,r.event_type,r.day,
                r.dispatched_at,r.send_started_at,r.send_blocked,r.revoked,e.profile_id AS event_profile,
                e.post_id AS event_post,e.event_type AS actual_type,e.status AS event_status,p.source AS post_source
                FROM feiyu_auto_reply_reservations r
                LEFT JOIN incoming_events e ON e.id=r.event_id
                LEFT JOIN feiyu_auto_reply_posts p ON p.post_hash=r.post_hash
                WHERE r.event_id=?""", (event_id,))
            if (row is None or row["post_hash"] != key or row["profile_id"] != str(route.profile_id)
                    or row["event_profile"] != row["profile_id"] or str(row["event_post"]) != str(route.post_id)
                    or row["event_type"] != row["actual_type"] or row["event_type"] not in AUTO_TYPES
                    or row["post_source"] != "reservation" or row["day"] != day
                    or row["event_status"] not in {'claimed','context_ready','dispatched','generated'}
                    or row["send_blocked"] or row["revoked"]
                    or not _period_allowed(row["event_type"], stamp, repository=repository,
                                           event_id=event_id, route=route)):
                return False
            if row["event_type"] == "proactive_feed" and not await feiyu_unseen_feed.valid(
                    repository, event_id, route, connection=connection):
                return False
            final_day, _, _, final_stamp = _clock(now)
            if final_day != day or not _period_allowed(
                row["event_type"], final_stamp, repository=repository,
                event_id=event_id, route=route,
            ):
                return False
            stamp = final_stamp
            if phase == "context" and row["dispatched_at"] is not None:
                return False
            if phase == "check" and row["dispatched_at"] is None:
                return False
            if phase == "http" and (row["dispatched_at"] is None or row["send_started_at"] is None):
                return False
            if phase == "dispatch":
                if row["dispatched_at"] is not None:
                    return False
                await connection.execute("UPDATE feiyu_auto_reply_reservations SET dispatched_at=? WHERE event_id=?", (stamp, event_id))
            if phase == "send":
                if row["dispatched_at"] is None or row["send_started_at"] is not None:
                    return False
                await connection.execute("UPDATE feiyu_auto_reply_reservations SET send_started_at=? WHERE event_id=?", (stamp, event_id))
            kind = row["event_type"]
        # Exiting the transaction commits asynchronously. Check again after it
        # completes, with no further await on the successful path.
        if kind == "proactive_feed" and not await feiyu_unseen_feed.valid(repository, event_id, route):
            if phase == "send":
                await mark_not_sent(repository, event_id)
            return False
        if not _current_period_allowed(kind, day, now=now, repository=repository,
                                       event_id=event_id, route=route):
            if phase == "send":
                await mark_not_sent(repository, event_id)
            return False
        return True
    except Exception:
        return False


async def mark_not_sent(repository, event_id):
    try:
        await repository.db.execute("UPDATE feiyu_auto_reply_reservations SET send_blocked=1 WHERE event_id=?", (event_id,))
    except Exception:
        pass
