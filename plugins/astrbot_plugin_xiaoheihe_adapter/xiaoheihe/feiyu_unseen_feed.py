"""Fresh feed admissions; never turn a prior task into fresh work.

The seven-day source window is deliberately shorter than the audited minimum
30-day task retention.  Historical evidence is not inferred from row-id gaps.
Only a current native fetch can issue an admission; durable rows alone cannot
resume after a restart, replacement or incident-generation change.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import math
import re
import time
from weakref import WeakKeyDictionary

from . import feiyu_recovery
from .feiyu_peak_bridge import feed_snapshot_current, feed_scope_issuer


MAX_SOURCE_AGE = 7 * 86400
MIN_RETENTION_DAYS = 30
# Historical configuration audit: work/heybox-unseen-old-20261108/
# retention-backup-audit.json. Earlier sources remain outside this exception.
AUDITED_SOURCE_FLOOR = 1793462400.0  # 2026-11-01 00:00:00 +08:00
_SCOPE = ContextVar("feiyu_fresh_native_feed", default=None)
_issued = WeakKeyDictionary()


def _key(post):
    return hashlib.sha256(("feiyu-automatic-post-v1:" + str(post)).encode("ascii")).hexdigest()


def _snapshot_json(snapshot):
    return json.dumps(snapshot, ensure_ascii=True, separators=(",", ":"))


def _source(notification):
    try:
        value = notification.raw["post"]["created_at"]
        if isinstance(value, bool):
            return None
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


@contextmanager
def candidate_scope(feed, notification, snapshot, fetch_started_at):
    """Issue no authority unless the exact native fetch remains current."""
    token = _SCOPE.set((feed, notification, snapshot, fetch_started_at, feed_scope_issuer(feed, snapshot),
                       feiyu_recovery._credential_binding(feed)))
    try:
        yield
    finally:
        _SCOPE.reset(token)


async def _one(connection, sql, parameters=()):
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


async def _schema(connection):
    tables = {name: await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None
              for name in ("feiyu_feed_admissions", "feiyu_feed_coverage")}
    if any(tables.values()):
        if not all(tables.values()) or not await _one(connection, "SELECT 1 FROM feiyu_feed_coverage WHERE id=1"):
            raise ValueError("fresh_feed_coverage_incomplete")
        return
    await connection.execute("""CREATE TABLE IF NOT EXISTS feiyu_feed_admissions (
        event_id INTEGER PRIMARY KEY, post_hash TEXT NOT NULL UNIQUE,
        profile_id TEXT NOT NULL, source_created_at REAL NOT NULL,
        fetch_started_at REAL NOT NULL, observed_at REAL NOT NULL,
        claimed_at REAL NOT NULL, resume_snapshot TEXT NOT NULL)""")
    await connection.execute("""CREATE TABLE IF NOT EXISTS feiyu_feed_coverage (
        id INTEGER PRIMARY KEY CHECK(id=1), valid INTEGER NOT NULL CHECK(valid IN (0,1)))""")
    await connection.execute("INSERT OR IGNORE INTO feiyu_feed_coverage VALUES (1,1)")


async def record_retention(connection, retention):
    """A shorter future cleanup poisons the audited coverage, never resets it."""
    await _schema(connection)
    if (type(retention.get("failed_days")) is not int
            or type(retention.get("dedup_days")) is not int
            or min(retention["failed_days"], retention["dedup_days"]) < MIN_RETENTION_DAYS):
        await connection.execute("UPDATE feiyu_feed_coverage SET valid=0 WHERE id=1")


async def _prior(connection, post, *, event_id=None):
    # No status whitelist: a rejected/ignored/failed/manual/unknown task is old.
    for table in ("incoming_events", "outgoing_replies", "feed_candidates",
                  "self_comment_ids", "session_mappings", "visual_contexts"):
        if table == "incoming_events" and event_id is not None:
            sql, args = "SELECT 1 FROM incoming_events WHERE post_id=? AND id<>? LIMIT 1", (post, event_id)
        elif table in {"outgoing_replies", "feed_candidates"} and event_id is not None:
            sql = f"SELECT 1 FROM {table} WHERE post_id=? AND (incoming_event_id IS NULL OR incoming_event_id<>?) LIMIT 1"
            args = (post, event_id)
        elif event_id is not None and table in {"session_mappings", "visual_contexts", "self_comment_ids"}:
            # Context created by this admitted task is expected after claim.
            continue
        else:
            sql, args = f"SELECT 1 FROM {table} WHERE post_id=? LIMIT 1", (post,)
        if await _one(connection, sql, args):
            return True
    if event_id is None:
        if await _one(connection, "SELECT 1 FROM processed_event_keys WHERE event_key=? LIMIT 1", ("feed:" + post,)):
            return True
        if await _one(connection, "SELECT 1 FROM feiyu_feed_admissions WHERE post_hash=?", (_key(post),)):
            return True
        if await _one(connection, "SELECT 1 FROM feiyu_auto_reply_posts WHERE post_hash=?", (_key(post),)):
            return True
        if await _one(connection, "SELECT 1 FROM feiyu_auto_reply_reservations WHERE post_hash=? LIMIT 1", (_key(post),)):
            return True
    for table in ("feiyu_recovery_attempts", "feiyu_recovery_permission_revision1_attempts",
                  "feiyu_recovery_owner_renewal1_attempts"):
        if await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
            if await _one(connection, f"SELECT 1 FROM {table} WHERE post_id=? LIMIT 1", (post,)):
                return True
    return False


def _scope_current(repository, notification, scope):
    try:
        feed, exact, snapshot, fetched, issuer, credentials = scope
        now, source, observed = time.time(), _source(notification), notification.observed_at
        retention = feed.config["retention"]
        return bool(feed.repository is repository and notification is exact
                    and re.fullmatch(r"[1-9][0-9]{0,29}", str(notification.post_id))
                    and notification.external_event_id == notification.external_comment_id == notification.notification_id == "feed:" + str(notification.post_id)
                    and notification.event_type.value == "proactive_feed"
                    and notification.profile_id == feed.profile_id
                    and isinstance(snapshot, tuple) and len(snapshot) == 2
                    and type(fetched) in (int, float) and math.isfinite(fetched)
                    and type(observed) in (int, float) and math.isfinite(observed)
                    and snapshot[0] < fetched <= observed <= now
                    and source is not None and source <= now
                    and source >= max(AUDITED_SOURCE_FLOOR, now - MAX_SOURCE_AGE)
                    and min(retention["failed_days"], retention["dedup_days"]) >= MIN_RETENTION_DAYS
                    and feiyu_recovery._credentials_intact(credentials)
                    and feed.client is credentials["client"]
                    and feed_snapshot_current(feed, snapshot, issuer)
                    and feiyu_recovery._manual_clear(repository, str(notification.post_id)))
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


async def before_claim(connection, repository, notification):
    """Run inside the native BEGIN IMMEDIATE claim transaction."""
    scope = _SCOPE.get()
    if not _scope_current(repository, notification, scope):
        return None
    await _schema(connection)
    coverage = await _one(connection, "SELECT valid FROM feiyu_feed_coverage WHERE id=1")
    if (coverage is None or coverage["valid"] != 1
            or not await feiyu_recovery._storage_intact(connection, repository)
            or await _prior(connection, str(notification.post_id))):
        return None
    return scope if _scope_current(repository, notification, scope) else None


async def bind_claim(connection, repository, notification, event_id, claim, scope):
    if not _scope_current(repository, notification, scope):
        raise ValueError("fresh_feed_generation_changed")
    feed, _, snapshot, fetched, _, _ = scope
    if notification.observed_at > claim:
        raise ValueError("fresh_feed_claim_boundary")
    record = (event_id, _key(notification.post_id), notification.profile_id, _source(notification),
              fetched, notification.observed_at, claim, _snapshot_json(snapshot))
    await connection.execute("INSERT INTO feiyu_feed_admissions VALUES (?,?,?,?,?,?,?,?)", record)
    # A rolled-back transaction leaves no usable capability: validation always
    # requires the durable row, exact incoming identity and matching timestamps.
    _issued.setdefault(repository, {})[event_id] = (scope, record)


async def valid(repository, event_id, route, *, snapshot=None, connection=None):
    """Check every native authorization phase; no restart or replay capability."""
    try:
        issued = _issued.get(repository, {}).get(event_id)
        if issued is None:
            return False
        scope, record = issued
        notification = scope[1]
        if (not _scope_current(repository, notification, scope)
                or (snapshot is not None and snapshot != scope[2])
                or route.profile_id != notification.profile_id or route.post_id != notification.post_id
                or route.notification_id != notification.notification_id
                or route.root_comment_id or route.parent_comment_id):
            return False
        if connection is None:
            async with repository.db.transaction() as current:
                allowed = await valid(repository, event_id, route, snapshot=snapshot, connection=current)
            return bool(allowed and _scope_current(repository, notification, scope))
        row = await _one(connection, "SELECT * FROM feiyu_feed_admissions WHERE event_id=?", (event_id,))
        event = await _one(connection, "SELECT profile_id,post_id,event_type,claimed_at,discovered_at,raw_json FROM incoming_events WHERE id=?", (event_id,))
        coverage = await _one(connection, "SELECT valid FROM feiyu_feed_coverage WHERE id=1")
        if (row is None or tuple(row) != record or event is None
                or coverage is None or coverage["valid"] != 1
                or event["profile_id"] != route.profile_id or event["post_id"] != route.post_id
                or event["event_type"] != "proactive_feed" or event["claimed_at"] != record[6]
                or event["discovered_at"] != record[5]
                or _source(type("Source", (), {"raw": json.loads(event["raw_json"])})()) != record[3]
                or await _prior(connection, str(route.post_id), event_id=event_id)):
            return False
        return _scope_current(repository, notification, scope)
    except Exception:
        return False


async def source_exception(repository, event_id, snapshot):
    issued = _issued.get(repository, {}).get(event_id)
    return bool(issued and await valid(repository, event_id, issued[0][1].route, snapshot=snapshot))


class ManualPostPreserved(RuntimeError):
    """A persistent prior attempt prevents an ordinary manual write."""


async def manual_outgoing_blocked(connection, profile_id, post):
    # This exact query also runs in the ordinary automatic reserve transaction.
    # A known pre-HTTP failure is retained too: no ordinary command retries it.
    return bool(await _one(connection, """SELECT 1 FROM outgoing_replies
        WHERE profile_id=? AND post_id=? AND incoming_event_id IS NULL
          AND status<>'dry_run' LIMIT 1""", (profile_id, post)))


async def manual_post_blocked(repository, post, *, profile_id="default",
                              outgoing_id=None, connection=None):
    """One durable, account-scoped fence for entry, claim and HTTP checks.

    Do not inspect the steward's manual-intent dictionary here: the caller has
    already registered its own pending intent. Only this call's verified native
    sending row may be excluded immediately before its first HTTP request.
    """
    try:
        if (not isinstance(profile_id, str) or not 1 <= len(profile_id) <= 100
                or not re.fullmatch(r"[1-9][0-9]{0,29}", str(post))):
            return True
        post = str(post)
        if connection is None:
            async with repository.db.transaction() as current:
                return await manual_post_blocked(repository, post, profile_id=profile_id,
                    outgoing_id=outgoing_id, connection=current)
        if not await feiyu_recovery._storage_intact(connection, repository):
            return True
        if outgoing_id is not None:
            current = await _one(connection, """SELECT profile_id,post_id,
                incoming_event_id,status FROM outgoing_replies WHERE id=?""", (outgoing_id,))
            if (type(outgoing_id) is not int or current is None
                    or tuple(current) != (profile_id, post, None, "sending")):
                return True
        if await _one(connection, """SELECT 1 FROM outgoing_replies
                WHERE profile_id=? AND post_id=? AND status<>'dry_run'
                  AND (? IS NULL OR id<>?) LIMIT 1""",
                (profile_id, post, outgoing_id, outgoing_id)):
            return True
        if await _one(connection, "SELECT 1 FROM self_comment_ids WHERE profile_id=? AND post_id=? LIMIT 1",
                      (profile_id, post)):
            return True
        # Runtime startup always initializes these four permanent tables. A
        # missing/partial gate must not turn old history into a fresh attempt.
        required = ("feiyu_auto_reply_meta", "feiyu_auto_reply_posts",
                    "feiyu_auto_reply_reservations", "feiyu_auto_reply_legacy")
        if not all([await _one(connection,
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) for name in required]):
            return True
        meta = await _one(connection, "SELECT * FROM feiyu_auto_reply_meta WHERE id=1")
        if meta is None:
            return True
        # Existing validation is read-only: no migration or authority changes.
        from . import feiyu_auto_reply_gate as automatic
        await automatic._validate_state(connection, meta["legacy_day"], meta["legacy_count"],
            meta["initialized_at"], meta["legacy_reply_count"], automatic._clock()[0])
        key = _key(post)
        reservation = await _one(connection, "SELECT * FROM feiyu_auto_reply_reservations WHERE post_hash=?", (key,))
        if reservation is not None:
            event = await _one(connection, "SELECT profile_id,post_id,event_type FROM incoming_events WHERE id=?",
                               (reservation["event_id"],))
            if (event is None or tuple(event) != (reservation["profile_id"], post, reservation["event_type"])
                    or reservation["profile_id"] == profile_id):
                return True
        permanent = await _one(connection, "SELECT source FROM feiyu_auto_reply_posts WHERE post_hash=?", (key,))
        if permanent is not None:
            if permanent["source"] == "reservation":
                if reservation is None:
                    return True
            elif permanent["source"] == "legacy":
                # The old permanent hash has no account identity. Surviving
                # other-account rows cannot prove this account never existed;
                # preserve the unscoped fence rather than inventing ownership.
                return True
            else:
                return True
        tables = [await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
                  for name in ("feiyu_feed_admissions", "feiyu_feed_coverage")]
        if any(tables):
            if not all(tables) or not await _one(connection, "SELECT 1 FROM feiyu_feed_coverage WHERE id=1 AND valid=1"):
                return True
            admission = await _one(connection, "SELECT event_id,profile_id FROM feiyu_feed_admissions WHERE post_hash=?", (key,))
            if admission is not None:
                event = await _one(connection, "SELECT profile_id,post_id,event_type FROM incoming_events WHERE id=?",
                                   (admission["event_id"],))
                if (event is None or tuple(event) != (admission["profile_id"], post, "proactive_feed")
                        or admission["profile_id"] == profile_id):
                    return True
        for table in (feiyu_recovery.LEGACY_TABLE, feiyu_recovery.REVISION_TABLE, feiyu_recovery.RENEWAL_TABLE):
            if await feiyu_recovery._table_exists(connection, table):
                if await _one(connection, f"SELECT 1 FROM {table} WHERE profile_id=? AND post_id=? LIMIT 1",
                              (profile_id, post)):
                    return True
        return False
    except Exception:
        return True
