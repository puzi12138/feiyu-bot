"""One bounded, explicit recovery campaign for confirmed unsent feed events.

The local authority is disabled by default. Every selected original remains
unchanged; a separate durable attempt is consumed before the source is read.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .endpoints import EndpointName
from .feiyu_peak_bridge import automatic_peak_paused, feed_paused
from .models import Notification, NotificationType
from .parsers import _feed_section_names, parse_thread_context
from .security import clean_untrusted_text, redact_data

CAMPAIGN_ID = "8c734dd47c5248f3814d253abeec4151"
SEED_SHA256 = "ada7a213e618bb80a5cf25358b0e2ace19ffae1eee3a49b10059b2542945f755"
ISSUED_AT = "2026-11-05T02:49:42.153133+08:00"
EXPIRES_AT = "2026-11-06T02:49:42.153133+08:00"
SEED_IDS = (977, 1176, 1236, 1279, 1288, 1294, 1299, 1385, 1419, 1421, 1548, 1554, 1775, 1823)
AUTHORITY_FILE = "feiyu-comment-recovery-authority.json"
INITIALIZED_FILE = "feiyu-comment-recovery-initialized.json"
REVISION_ID = "permission-contract-v1"
REVISION_IDS = (977, 1176, 1236, 1279, 1288, 1294, 1299, 1385, 1419, 1421, 1548, 1554, 1775)
OLD_CLAIMS_SHA256 = "debac6a0a6ac25c9ed3aa4b12f92cdbd55dc08bda68687b91792f6d4b7636645"
OLD_MANIFEST_SHA256 = "e167f96e5f5f6b6bdfacc1b93cf00e72198bbb16f866e375f31f0494ae6bd574"
REVISION_AUTHORITY_FILE = "feiyu-comment-recovery-permission-revision1-authority.json"
REVISION_INITIALIZED_FILE = "feiyu-comment-recovery-permission-revision1-initialized.json"
LEGACY_TABLE = "feiyu_recovery_attempts"
REVISION_TABLE = "feiyu_recovery_permission_revision1_attempts"
RENEWAL_CAMPAIGN_ID = "owner-renewal1-20261106"
RENEWAL_ID = "owner-renewal1"
RENEWAL_AUTHORITY_FILE = "feiyu-comment-recovery-owner-renewal1-authority.json"
RENEWAL_INITIALIZED_FILE = "feiyu-comment-recovery-owner-renewal1-initialized.json"
RENEWAL_TABLE = "feiyu_recovery_owner_renewal1_attempts"
RENEWAL_BASE_SHA256 = "70e8fad1540781294cdea835c8b427736e448131e7ef6d6e20f85a3cf430df58"
FEATURES = ("heybox_comment", "heybox_vision", "heybox_feed")
_INCIDENT_MODULE = "data.plugins.feiyu_steward.incident_hooks"
_HK = timezone(timedelta(hours=8))
_issued = {}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authority(repository):
    """Read only the fixed local campaign; absent or altered files fail closed."""
    try:
        path = Path(repository.db.path).parent / AUTHORITY_FILE
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        if len(raw) > 64_000:
            return None
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, dict) or value.get("schema") != 1
                or value.get("authorized") is not True
                or value.get("campaign_id") != CAMPAIGN_ID
                or value.get("issued_at") != ISSUED_AT or value.get("expires_at") != EXPIRES_AT
                or not isinstance(value.get("seed"), list)
                or _hash(_canonical(value["seed"])) != SEED_SHA256
                or tuple(item.get("event_id") for item in value["seed"]) != SEED_IDS):
            return None
        issued = datetime.fromisoformat(ISSUED_AT).timestamp()
        expires = datetime.fromisoformat(EXPIRES_AT).timestamp()
        if not issued <= time.time() < expires:
            return None
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def _snapshot(repository, feed=None):
    """Obtain the current live incident generation without opening a gate."""
    try:
        module = sys.modules.get(_INCIDENT_MODULE)
        owner = getattr(module, "_ACTIVE_INCIDENT_HOOKS", None)
        if owner is None or owner._active is not True or owner._blocked(*FEATURES):
            return None
        plugin = owner.host.plugin("astrbot_plugin_xiaoheihe_adapter")
        if plugin is None or plugin.runtime.repository is not repository:
            return None
        if owner.status().get("ready") is not True:
            return None
        if automatic_peak_paused() is not False:
            return None
        if feed is not None and (
            module.automatic_feed_ready(feed) is not True
            or feed.repository is not repository
            or automatic_peak_paused() is not False or feed_paused(feed)
        ):
            return None
        snapshot = owner._resume_snapshot(FEATURES)
        return snapshot if isinstance(snapshot, tuple) and len(snapshot) == 2 else None
    except Exception:
        return None


async def _one(connection, sql, parameters=()):
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


def _source_time(raw):
    try:
        post = json.loads(raw)["post"]
        value = post["created_at"]
        if isinstance(value, bool):
            return None
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return stamp if math.isfinite(stamp) and 0 < stamp <= time.time() else None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _row_hash(row):
    return _hash(_canonical(dict(row)))


async def _gate_intact(connection, repository, day):
    # Run the deployed gate's complete state validator, never its initializer.
    from . import feiyu_auto_reply_gate as gate

    if repository not in gate._history_ready_repositories:
        return False
    row = await _one(connection, "SELECT legacy_day,legacy_count,initialized_at,legacy_reply_count "
                     "FROM feiyu_auto_reply_meta WHERE id=1")
    if row is None:
        return False
    await gate._validate_state(connection, row["legacy_day"], row["legacy_count"],
                               row["initialized_at"], row["legacy_reply_count"], day)
    return True


async def _original_valid(connection, repository, entry, *, new_event_id=None):
    """Recheck the immutable original, old gate, account, and all post attempts."""
    try:
        original_id = entry["event_id"]
        if type(original_id) is not int or original_id not in SEED_IDS:
            return False
        original = await _one(connection, "SELECT * FROM incoming_events WHERE id=?", (original_id,))
        reservation = await _one(connection, "SELECT * FROM feiyu_auto_reply_reservations WHERE event_id=?",
                                 (original_id,))
        if original is None or reservation is None:
            return False
        profile, post = entry["profile_id"], entry["post_id"]
        if not _manual_clear(repository, post):
            return False
        raw = original["raw_json"]
        if (not isinstance(profile, str) or not isinstance(post, str)
                or not re.fullmatch(r"[1-9][0-9]{0,29}", post)
                or original["profile_id"] != profile or original["post_id"] != post
                or original["event_type"] != "proactive_feed" or original["status"] != "dead_letter"
                or original["external_event_id"] != "feed:" + post
                or original["external_comment_id"] != "feed:" + post
                or original["root_comment_id"] or original["parent_comment_id"]
                or not isinstance(raw, str) or _row_hash(original) != entry["event_row_sha256"]
                or _hash(raw) != entry["raw_sha256"]
                or _row_hash(reservation) != entry["reservation_row_sha256"]
                or reservation["event_type"] != "proactive_feed" or reservation["profile_id"] != profile
                or reservation["send_started_at"] is not None or reservation["send_blocked"] != 0
                or reservation["revoked"] != 0
                or _source_time(raw) != entry["source_created_at"]
                or not str(original["sender_uid"] or "").strip()):
            return False
        post_hash = _hash("feiyu-automatic-post-v1:" + post)
        if reservation["post_hash"] != post_hash:
            return False
        gate_post = await _one(connection, "SELECT source FROM feiyu_auto_reply_posts WHERE post_hash=?", (post_hash,))
        if gate_post is None or gate_post["source"] != "reservation":
            return False
        account = await _one(connection, "SELECT status,circuit_open_until FROM account_state WHERE profile_id=?",
                             (profile,))
        if (account is None or account["status"] != "success"
                or float(account["circuit_open_until"] or 0) > time.time()):
            return False
        if await _one(connection, "SELECT 1 FROM outgoing_replies WHERE profile_id=? AND post_id=? "
                      "AND (? IS NULL OR incoming_event_id IS NULL OR incoming_event_id<>?) LIMIT 1",
                      (profile, post, new_event_id, new_event_id)):
            return False
        if await _one(connection, "SELECT 1 FROM self_comment_ids WHERE profile_id=? AND post_id=? LIMIT 1",
                      (profile, post)):
            return False
        if await _one(connection, "SELECT 1 FROM feed_candidates WHERE profile_id=? AND post_id=? LIMIT 1",
                      (profile, post)):
            return False
        return await _gate_intact(connection, repository, datetime.now(_HK).date().isoformat())
    except (KeyError, TypeError, ValueError, OverflowError):
        return False




def _is_renewal(entry):
    return isinstance(entry, Mapping) and entry.get("_owner_renewal1") is True


def _entry_campaign(entry):
    return RENEWAL_CAMPAIGN_ID if _is_renewal(entry) else CAMPAIGN_ID


def _entry_revision(entry):
    return RENEWAL_ID if _is_renewal(entry) else REVISION_ID


def _sentinel(entry):
    if _is_renewal(entry):
        return RENEWAL_INITIALIZED_FILE, RENEWAL_CAMPAIGN_ID + ":" + RENEWAL_ID + "\n"
    if _is_revision(entry):
        return REVISION_INITIALIZED_FILE, CAMPAIGN_ID + ":" + REVISION_ID + "\n"
    return INITIALIZED_FILE, CAMPAIGN_ID + "\n"


def _renewal_authority(repository):
    """A separate six-hour authority never changes or extends the old one."""
    try:
        parent = Path(repository.db.path).parent
        base_path, path = parent / AUTHORITY_FILE, parent / RENEWAL_AUTHORITY_FILE
        if any(p.is_symlink() or not p.is_file() for p in (base_path, path)):
            return None
        base_raw, raw = base_path.read_bytes(), path.read_bytes()
        if (len(raw) > 64000 or hashlib.sha256(base_raw).hexdigest() != RENEWAL_BASE_SHA256):
            return None
        base, value = json.loads(base_raw), json.loads(raw)
        if (base.get("campaign_id") != CAMPAIGN_ID or base.get("issued_at") != ISSUED_AT
                or base.get("expires_at") != EXPIRES_AT or base.get("authorized") is not True
                or _hash(_canonical(base.get("seed"))) != SEED_SHA256
                or tuple(row.get("event_id") for row in base["seed"]) != SEED_IDS
                or type(value.get("schema")) is not int or value["schema"] != 1
                or value.get("authorized") is not True or value.get("campaign_id") != RENEWAL_CAMPAIGN_ID
                or value.get("revision_id") != RENEWAL_ID or value.get("source_campaign_id") != CAMPAIGN_ID
                or value.get("base_authority_sha256") != RENEWAL_BASE_SHA256
                or not isinstance(value.get("seed"), list) or not value["seed"]
                or not isinstance(value.get("predecessors"), dict)):
            return None
        begin, end = datetime.fromisoformat(value["issued_at"]), datetime.fromisoformat(value["expires_at"])
        if (begin.utcoffset() != timedelta(hours=8) or end.utcoffset() != timedelta(hours=8)
                or not 0 < end.timestamp() - begin.timestamp() <= 21600
                or not begin.timestamp() <= time.time() < end.timestamp()):
            return None
        ids = tuple(row.get("event_id") for row in value["seed"])
        if (len(set(ids)) != len(ids) or ids != tuple(i for i in SEED_IDS if i in ids)
                or set(value["predecessors"]) != {str(i) for i in ids}):
            return None
        base_seed = {row["event_id"]: row for row in base["seed"]}
        for entry in value["seed"]:
            expected = dict(base_seed[entry["event_id"]])
            expected["predecessor_sha256"] = _hash(_canonical(value["predecessors"][str(entry["event_id"])]))
            if entry != expected:
                return None
        manifest = dict(value, seed=[dict(row, _owner_renewal1=True) for row in value["seed"]])
        return manifest, hashlib.sha256(raw).hexdigest()
    except Exception:
        return None


async def _renewal_predecessors_intact(connection, repository, entry):
    try:
        authority = _renewal_authority(repository)
        if authority is None or not await _old_claims_intact(connection):
            return False
        expected = authority[0]["predecessors"][str(entry["event_id"])]
        if _hash(_canonical(expected)) != entry["predecessor_sha256"]:
            return False
        actual = {"legacy_sha256": None, "revision1_sha256": None, "events": {}}
        for table, key in ((LEGACY_TABLE, "legacy_sha256"), (REVISION_TABLE, "revision1_sha256")):
            if not await _table_exists(connection, table):
                continue
            row = await _one(connection, f"SELECT * FROM {table} WHERE original_event_id=?", (entry["event_id"],))
            if row is None:
                continue
            actual[key] = _row_hash(row)
            if any(row[k] is not None for k in ("dispatched_at", "send_started_at", "http_started_at")):
                return False
            new_id = row["new_event_id"]
            if new_id is not None:
                event = await _one(connection, "SELECT * FROM incoming_events WHERE id=?", (new_id,))
                if (entry["event_id"] != 1823 or new_id != 1850 or row["state"] != "reserved"
                        or event is None or event["status"] != "ignored"
                        or event["error"] != "feiyu_auto_reply_gate:period_not_allowed"
                        or event["reply_text"] is not None or event["generated_ms"] is not None):
                    return False
                actual["events"][str(new_id)] = _row_hash(event)
            elif (row["state"] not in {"claimed", "account_permission_rejected", "source_unavailable"}
                  or row["marker_json"] is not None or row["new_raw_sha256"] is not None):
                return False
        return actual == expected and actual["legacy_sha256"] is not None
    except Exception:
        return False


async def _create_renewal_table(connection, repository, entry):
    if not await _renewal_predecessors_intact(connection, repository, entry):
        raise RuntimeError("owner_renewal_predecessor_changed")
    path = Path(repository.db.path).parent / RENEWAL_INITIALIZED_FILE
    content = RENEWAL_CAMPAIGN_ID + ":" + RENEWAL_ID + "\n"
    exists = await _table_exists(connection, RENEWAL_TABLE)
    if path.exists():
        if not exists or path.is_symlink() or path.read_text(encoding="utf-8") != content:
            raise RuntimeError("owner_renewal_storage_unavailable")
        return
    if exists or path.is_symlink():
        raise RuntimeError("owner_renewal_sentinel_missing")
    await connection.execute("""CREATE TABLE feiyu_recovery_owner_renewal1_attempts (
        original_event_id INTEGER PRIMARY KEY, new_event_id INTEGER UNIQUE,
        campaign_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
        profile_id TEXT NOT NULL, post_id TEXT NOT NULL UNIQUE, source_created_at REAL NOT NULL,
        snapshot_json TEXT NOT NULL, claimed_at REAL NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL,
        marker_json TEXT, new_raw_sha256 TEXT, dispatched_at REAL, send_started_at REAL, http_started_at REAL,
        revision_id TEXT NOT NULL, predecessor_sha256 TEXT NOT NULL, claim_nonce TEXT NOT NULL UNIQUE
    )""")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def _is_revision(entry):
    return (not _is_renewal(entry) and isinstance(entry, Mapping)
            and type(entry.get("event_id")) is int and entry["event_id"] in REVISION_IDS)


def _attempt_table(entry):
    return RENEWAL_TABLE if _is_renewal(entry) else (REVISION_TABLE if _is_revision(entry) else LEGACY_TABLE)


def _revision_authority(repository, base_digest):
    """A new explicit authority never renews or resets the consumed campaign."""
    try:
        path = Path(repository.db.path).parent / REVISION_AUTHORITY_FILE
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        if len(raw) > 16000:
            return None
        value = json.loads(raw.decode("utf-8"))
        expected = {"schema": 1, "authorized": True, "campaign_id": CAMPAIGN_ID,
                    "revision_id": REVISION_ID, "issued_at": ISSUED_AT, "expires_at": EXPIRES_AT,
                    "original_ids": list(REVISION_IDS), "old_claims_sha256": OLD_CLAIMS_SHA256,
                    "base_authority_sha256": base_digest}
        if (type(value.get("schema")) is not int or value.get("authorized") is not True
                or value != expected):
            return None
        return hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return None


def _revision_digest(base_digest, revision_digest):
    return _hash(_canonical({"base_authority_sha256": base_digest,
                             "revision_authority_sha256": revision_digest, "revision_id": REVISION_ID}))


def _effective_digest(repository, entry, base_digest):
    if _is_renewal(entry):
        authority = _renewal_authority(repository)
        return authority[1] if authority is not None else None
    if not _is_revision(entry):
        return base_digest
    revision = _revision_authority(repository, base_digest)
    return _revision_digest(base_digest, revision) if revision is not None else None


async def _old_claims_intact(connection):
    if not await _table_exists(connection):
        return False
    async with connection.execute("SELECT * FROM feiyu_recovery_attempts WHERE original_event_id IN ("
                                  + ",".join("?" for _ in REVISION_IDS) + ") ORDER BY original_event_id",
                                  REVISION_IDS) as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]
    return bool(tuple(row["original_event_id"] for row in rows) == REVISION_IDS
                and all(row["campaign_id"] == CAMPAIGN_ID and row["state"] == "claimed"
                        and row["manifest_sha256"] == OLD_MANIFEST_SHA256
                        and all(row[key] is None for key in ("new_event_id", "marker_json", "new_raw_sha256",
                                                            "dispatched_at", "send_started_at", "http_started_at"))
                        for row in rows)
                and _hash(_canonical(rows)) == OLD_CLAIMS_SHA256)


async def _storage_intact(connection, repository):
    """Both ledgers remain permanent fences even after authority withdrawal."""
    try:
        for table, name, content in ((LEGACY_TABLE, INITIALIZED_FILE, CAMPAIGN_ID + "\n"),
                                     (REVISION_TABLE, REVISION_INITIALIZED_FILE, CAMPAIGN_ID + ":" + REVISION_ID + "\n"),
                                     (RENEWAL_TABLE, RENEWAL_INITIALIZED_FILE, RENEWAL_CAMPAIGN_ID + ":" + RENEWAL_ID + "\n")):
            path = Path(repository.db.path).parent / name
            exists = await _table_exists(connection, table)
            if path.is_symlink() or path.exists() != exists:
                return False
            if exists and (not path.is_file() or path.read_text(encoding="utf-8") != content):
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False



def batch_authorized(repository):
    """Only the explicit owner's eleven-item batch can skip interval waiting."""
    try:
        path = Path(repository.db.path).parent / "feiyu-comment-recovery-owner-renewal1-batch.json"
        if path.is_symlink() or not path.is_file():
            return False
        value = json.loads(path.read_bytes())
        authority = _renewal_authority(repository)
        return (value == {"schema": 1, "authorized": True, "campaign_id": RENEWAL_CAMPAIGN_ID,
                         "authority_sha256": "191f8776748994bd10d8d0f03db106c63a9b387e2817e4cfd848c635422c2a5e",
                         "maximum_items": 11, "sequential_confirmed_only": True}
                and authority is not None and authority[1] == value["authority_sha256"])
    except Exception:
        return False


async def batch_confirmed(repository, event_id):
    """Continue only after one exact attempt has a platform success receipt."""
    if not batch_authorized(repository) or type(event_id) is not int:
        return False
    try:
        row = await _one(repository.db.connection,
            "SELECT o.status,o.external_comment_id,i.status AS incoming_status "
            "FROM " + RENEWAL_TABLE + " a JOIN incoming_events i ON i.id=a.new_event_id "
            "JOIN outgoing_replies o ON o.incoming_event_id=i.id "
            "WHERE a.new_event_id=? AND a.http_started_at IS NOT NULL "
            "ORDER BY o.id DESC LIMIT 1", (event_id,))
        return bool(row and row["status"] == "sent" and row["incoming_status"] == "sent"
                    and str(row["external_comment_id"] or "").strip())
    except Exception:
        return False


async def _interval_elapsed(connection, repository=None, entry=None):
    if repository is not None and _is_renewal(entry) and batch_authorized(repository):
        return True
    stamps = []
    for table in (LEGACY_TABLE, REVISION_TABLE, RENEWAL_TABLE):
        if await _table_exists(connection, table):
            row = await _one(connection, f"SELECT MAX(claimed_at) AS stamp FROM {table}")
            if row is not None and row["stamp"] is not None:
                stamps.append(float(row["stamp"]))
    row = await _one(connection, "SELECT MAX(claimed_at) AS stamp FROM incoming_events WHERE event_type='proactive_feed'")
    if row is not None and row["stamp"] is not None:
        stamps.append(float(row["stamp"]))
    return not stamps or time.time() - max(stamps) >= 900


def _event_key(row, entry):
    key = f"recovery:{_entry_campaign(entry)}:{entry['event_id']}"
    return key + f":revision:{_entry_revision(entry)}:{row['claim_nonce']}" if (_is_revision(entry) or _is_renewal(entry)) else key


async def _create_table(connection, entry=None):
    if not _is_revision(entry):
        return await _create_legacy_table(connection)
    if not await _old_claims_intact(connection):
        raise RuntimeError("recovery_predecessor_changed")
    database = await _one(connection, "PRAGMA database_list")
    initialized = Path(database["file"]).parent / REVISION_INITIALIZED_FILE
    exists = await _table_exists(connection, REVISION_TABLE)
    if initialized.exists():
        if (not exists or initialized.is_symlink()
                or initialized.read_text(encoding="utf-8") != CAMPAIGN_ID + ":" + REVISION_ID + "\n"):
            raise RuntimeError("recovery_revision_storage_unavailable")
        return
    if exists or initialized.is_symlink():
        raise RuntimeError("recovery_revision_sentinel_missing")
    await connection.execute("""CREATE TABLE feiyu_recovery_permission_revision1_attempts (
        original_event_id INTEGER PRIMARY KEY, new_event_id INTEGER UNIQUE,
        campaign_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
        profile_id TEXT NOT NULL, post_id TEXT NOT NULL UNIQUE, source_created_at REAL NOT NULL,
        snapshot_json TEXT NOT NULL, claimed_at REAL NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL,
        marker_json TEXT, new_raw_sha256 TEXT, dispatched_at REAL, send_started_at REAL, http_started_at REAL,
        revision_id TEXT NOT NULL, predecessor_sha256 TEXT NOT NULL, claim_nonce TEXT NOT NULL UNIQUE
    )""")
    with initialized.open("x", encoding="utf-8", newline="\n") as target:
        target.write(CAMPAIGN_ID + ":" + REVISION_ID + "\n")


async def _create_legacy_table(connection):
    # A separate sentinel prevents an accidentally missing table from turning
    # consumed attempts into fresh work. A crash between files and the commit
    # deliberately leaves recovery closed for an operator to inspect.
    database = await _one(connection, "PRAGMA database_list")
    initialized = Path(database["file"]).parent / INITIALIZED_FILE
    exists = await _table_exists(connection)
    if initialized.exists():
        if (not exists or initialized.is_symlink()
                or initialized.read_text(encoding="utf-8") != CAMPAIGN_ID + "\n"):
            raise RuntimeError("recovery_storage_unavailable")
        return
    if exists:
        raise RuntimeError("recovery_sentinel_missing")
    await connection.execute("""CREATE TABLE IF NOT EXISTS feiyu_recovery_attempts (
        original_event_id INTEGER PRIMARY KEY,
        new_event_id INTEGER UNIQUE,
        campaign_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        post_id TEXT NOT NULL UNIQUE,
        source_created_at REAL NOT NULL,
        snapshot_json TEXT NOT NULL,
        claimed_at REAL NOT NULL,
        day TEXT NOT NULL,
        state TEXT NOT NULL,
        marker_json TEXT,
        new_raw_sha256 TEXT,
        dispatched_at REAL,
        send_started_at REAL,
        http_started_at REAL
    )""")
    with initialized.open("x", encoding="utf-8", newline="\n") as target:
        target.write(CAMPAIGN_ID + "\n")


async def _table_exists(connection, table=LEGACY_TABLE):
    if table not in (LEGACY_TABLE, REVISION_TABLE, RENEWAL_TABLE):
        return False
    return await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)) is not None


async def _attempt_valid(connection, repository, entry, manifest_sha, snapshot, *,
                         new_event_id=None, permit_claimed=False):
    table = _attempt_table(entry)
    if not await _storage_intact(connection, repository):
        return None
    if _is_renewal(entry):
        authority = _renewal_authority(repository)
        if (authority is None or authority[1] != manifest_sha
                or not await _renewal_predecessors_intact(connection, repository, entry)):
            return None
    if _is_revision(entry):
        authority = _authority(repository)
        if (authority is None or _effective_digest(repository, entry, authority[1]) != manifest_sha
                or not await _old_claims_intact(connection)):
            return None
    if not await _table_exists(connection, table):
        return None
    sentinel_name, sentinel_content = _sentinel(entry)
    initialized = Path(repository.db.path).parent / sentinel_name
    if (initialized.is_symlink() or not initialized.is_file()
            or initialized.read_text(encoding="utf-8") != sentinel_content):
        return None
    row = await _one(connection, f"SELECT * FROM {table} WHERE original_event_id=?",
                     (entry["event_id"],))
    if (row is None or row["campaign_id"] != _entry_campaign(entry) or row["manifest_sha256"] != manifest_sha
            or row["profile_id"] != entry["profile_id"] or row["post_id"] != entry["post_id"]
            or row["source_created_at"] != entry["source_created_at"]
            or row["snapshot_json"] != _canonical(snapshot)
            or row["day"] != datetime.now(_HK).date().isoformat()):
        return None
    if _is_revision(entry):
        predecessor = await _one(connection, "SELECT * FROM feiyu_recovery_attempts WHERE original_event_id=?", (entry["event_id"],))
        if (predecessor is None or row["revision_id"] != REVISION_ID
                or row["predecessor_sha256"] != _row_hash(predecessor)
                or not re.fullmatch(r"[a-f0-9]{32}", row["claim_nonce"])):
            return None
    if _is_renewal(entry):
        if (row["revision_id"] != RENEWAL_ID or row["predecessor_sha256"] != entry["predecessor_sha256"]
                or not re.fullmatch(r"[a-f0-9]{32}", row["claim_nonce"])):
            return None
    if new_event_id is None:
        if not permit_claimed or row["state"] != "claimed" or row["new_event_id"] is not None:
            return None
    else:
        if row["new_event_id"] != new_event_id or row["state"] not in {"prepared", "reserved", "dispatched", "sending"}:
            return None
        event = await _one(connection, "SELECT * FROM incoming_events WHERE id=?", (new_event_id,))
        issued = _issued.get((id(repository), new_event_id))
        key = _event_key(row, entry)
        if (event is None or event["profile_id"] != entry["profile_id"]
                or event["post_id"] != entry["post_id"] or event["event_type"] != "proactive_feed"
                or event["external_event_id"] != key or event["external_comment_id"] != key
                or event["notification_id"] != "feed:" + entry["post_id"]
                or issued is None or event["sender_uid"] != issued["notification"].sender_uid
                or event["sender_nickname"] != issued["notification"].sender_nickname
                or event["content"] != issued["notification"].content
                or event["root_comment_id"] or event["parent_comment_id"]
                or event["status"] not in {"claimed", "context_ready", "dispatched", "generated"}
                or _hash(str(event["raw_json"] or "")) != row["new_raw_sha256"]
                or _source_time(event["raw_json"]) != entry["source_created_at"]):
            return None
        try:
            if _canonical(json.loads(event["raw_json"])["_feiyu_recovery"]) != row["marker_json"]:
                return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        outgoing = await _one(connection, "SELECT COUNT(*) AS n, "
                              "SUM(CASE WHEN status='sending' THEN 1 ELSE 0 END) AS sending "
                              "FROM outgoing_replies WHERE incoming_event_id=?", (new_event_id,))
        if int(outgoing["n"]) > 1 or (int(outgoing["n"]) and int(outgoing["sending"] or 0) != 1):
            return None
    if not await _original_valid(connection, repository, entry, new_event_id=new_event_id):
        return None
    return row


def _live_authority(repository, digest, snapshot, feed=None):
    renewal = _renewal_authority(repository)
    if renewal is not None and renewal[1] == digest:
        return _snapshot(repository, feed) == snapshot
    authority = _authority(repository)
    if authority is None or _snapshot(repository, feed) != snapshot:
        return False
    if digest == authority[1]:
        return True
    revision = _revision_authority(repository, authority[1])
    return revision is not None and digest == _revision_digest(authority[1], revision)


def _manual_clear(repository, post):
    """Preserve manual intents that can precede the native outgoing row."""
    try:
        owner = sys.modules[_INCIDENT_MODULE]._ACTIVE_INCIDENT_HOOKS
        if owner.host.plugin("astrbot_plugin_xiaoheihe_adapter").runtime.repository is not repository:
            return False
        state = owner.host.state
        ledger = state.get("manual_heybox_comments", {}) if isinstance(state, dict) else None
        return isinstance(ledger, dict) and post not in ledger
    except Exception:
        return False


def _valid_feed(feed):
    try:
        config = feed.config["proactive_feed"]
        return bool(config.get("enabled") is True and config.get("dry_run") is False
                    and config.get("review_required") is False and feed.client.credentials is not None
                    and not feed.client.closed and str(feed.profile_id) == str(feed.client.profile_id))
    except (AttributeError, KeyError, TypeError):
        return False


def _credential_binding(feed):
    """Bind only this newly issued recovery to its current credential object."""
    try:
        client = feed.client
        credentials = client.credentials
        if (credentials is None or client.closed
                or str(credentials.profile_id) != str(feed.profile_id)
                or not str(credentials.uid).strip()):
            return None
        return {"client": client, "credentials": credentials,
                "profile_id": str(feed.profile_id),
                "fingerprint": _hash(_canonical(asdict(credentials)))}
    except Exception:
        return None


def _credentials_intact(binding):
    try:
        return bool(isinstance(binding, dict) and not binding["client"].closed
                    and binding["client"].credentials is binding["credentials"]
                    and str(binding["credentials"].profile_id) == binding["profile_id"]
                    and _hash(_canonical(asdict(binding["credentials"]))) == binding["fingerprint"])
    except Exception:
        return False


def _comment_permission_allowed(payload, uid):
    """Check the observed permission endpoint; it is not an identity readback.

    The current authenticated endpoint returns a top-level permission object,
    without result/data or a user object. Exact integer flags were verified by
    a bounded read-only GET. Missing, coerced or denied flags fail closed.
    """
    if not isinstance(payload, Mapping):
        return False
    for key, required in (("post_comment_permission", 1), ("visitor_enabled", 0),
                          ("bbs_forbid_forever_permission", 0)):
        if type(payload.get(key)) is not int or payload[key] != required:
            return False
    # Reject any explicitly supplied contradictory identity, but never invent
    # an identity from permission flags. Authenticated cookies stay client-bound.
    containers = [payload]
    for key in ("user", "user_info"):
        if key in payload:
            if not isinstance(payload[key], Mapping):
                return False
            containers.append(payload[key])
    for container in containers:
        for key in ("uid", "heybox_id", "heyboxid", "user_id"):
            if key in container and container[key] not in (None, "") and str(container[key]) != str(uid):
                return False
    return True


def _current_post(payload, entry, original, feed):
    """Accept only current detail with its own identity, source time and author."""
    if not isinstance(payload, Mapping):
        return None
    body = payload.get("result", payload.get("data", payload))
    if not isinstance(body, Mapping):
        return None
    post = body.get("post", body.get("link", body))
    if not isinstance(post, Mapping):
        return None
    ids = [str(post[key]) for key in ("linkid", "link_id", "post_id", "id")
           if key in post and post[key] not in (None, "")]
    if not ids or any(value != entry["post_id"] for value in ids):
        return None
    context = parse_thread_context(payload, entry["post_id"])
    if (not context.body.strip() or not context.author_uid.strip()
            or context.author_uid != str(original["sender_uid"])
            or not math.isfinite(context.post_created_at)
            or context.post_created_at != entry["source_created_at"]
            or context.post_created_at > time.time()):
        return None
    sections = _feed_section_names(post)
    if feed._fallback_aliases and not any(alias in name.casefold()
                                          for alias in feed._fallback_aliases for name in sections):
        return None
    if feed._topic_ids:
        # The detail contract must independently provide a currently matching topic.
        topic_values = post.get("topics", [])
        if not isinstance(topic_values, list):
            return None
        topic_ids = {str(item.get("topic_id", item.get("id", ""))) for item in topic_values
                     if isinstance(item, Mapping)}
        if not topic_ids.intersection(feed._topic_ids):
            return None
    current = {"post_id": entry["post_id"], "created_at": context.post_created_at,
               "title": context.title, "content": context.body,
               "author": {"uid": context.author_uid, "nickname": context.author_name},
               "image_urls": list(context.image_urls), "section_names": sections,
               "_proactive_feed_origin": "explicit_recovery"}
    if "content_type" in post:
        current["content_type"] = post["content_type"]
    elif "type" in post:
        current["type"] = post["type"]
    elif feed._allowed_types:
        return None
    return current if feed._eligible(current, context.author_uid) else None


def _feed_preflight(feed):
    """Require the current controlled feed and registered off-peak calendar."""
    try:
        from . import feiyu_auto_reply_gate as gate
        module = sys.modules.get("data.plugins.feiyu_steward.heybox_peak")
        grant = module._FEED.get()
        return bool(grant.feed is feed and grant.runtime.repository is feed.repository
                    and module._feed_alive(grant, require_control=True)
                    and gate._period_allowed("reply", time.time()))
    except Exception:
        return False


async def next_notification(feed, daily_limit):
    """Consume at most one frozen item in the existing off-peak feed round."""
    repository = feed.repository
    authority = _renewal_authority(repository) or _authority(repository)
    credential_binding = _credential_binding(feed)
    if (authority is None or not _valid_feed(feed) or credential_binding is None
            or not _feed_preflight(feed)):
        return None
    snapshot = _snapshot(repository, feed)
    if snapshot is None:
        return None
    manifest, base_digest = authority
    selected = None
    connection = repository.db.connection
    try:
        if not await _storage_intact(connection, repository):
            return None
        # Fresh unclaimed 1823 remains first; consumed originals require the
        # independent revision authority and can never reset the old rows.
        for entry in sorted(manifest["seed"], key=lambda entry: entry["event_id"] != 1823 if _is_renewal(entry) else _is_revision(entry)):
            if entry.get("profile_id") != str(feed.profile_id):
                continue
            table = _attempt_table(entry)
            effective = _effective_digest(repository, entry, base_digest)
            if effective is None or (_is_revision(entry) and not await _old_claims_intact(connection)):
                continue
            if (await _table_exists(connection, table) and await _one(connection,
                    f"SELECT 1 FROM {table} WHERE original_event_id=? OR post_id=?",
                    (entry["event_id"], entry["post_id"]))):
                continue
            if (_is_renewal(entry) and not await _renewal_predecessors_intact(connection, repository, entry)):
                continue
            if await _original_valid(connection, repository, entry):
                selected, digest = entry, effective
                break
        if (selected is None or not _live_authority(repository, digest, snapshot, feed)
                or not await _interval_elapsed(connection, repository, selected)):
            return None
    except Exception:
        return None
    table = _attempt_table(selected)
    # Use the same UTC daily budget method as an ordinary feed candidate.
    if not _feed_preflight(feed) or not await repository.reserve_proactive_request(feed.profile_id, daily_limit):
        return None
    consumed = (None, {"_feiyu_recovery_consumed": True})
    try:
        async with repository.db.transaction() as connection:
            if (not _feed_preflight(feed) or not _live_authority(repository, digest, snapshot, feed)
                    or not await _storage_intact(connection, repository)
                    or not await _original_valid(connection, repository, selected)
                    or (_is_revision(selected) and not await _old_claims_intact(connection))):
                return consumed
            if _is_renewal(selected):
                await _create_renewal_table(connection, repository, selected)
            else:
                await _create_table(connection, selected)
            if not await _interval_elapsed(connection, repository, selected):
                return consumed
            if await _one(connection, f"SELECT 1 FROM {table} WHERE original_event_id=? OR post_id=?",
                          (selected["event_id"], selected["post_id"])):
                return consumed
            if not _feed_preflight(feed):
                return consumed
            values = (selected["event_id"], _entry_campaign(selected), digest, selected["profile_id"], selected["post_id"],
                      selected["source_created_at"], _canonical(snapshot), time.time(),
                      datetime.now(_HK).date().isoformat())
            if _is_revision(selected) or _is_renewal(selected):
                predecessor = await _one(connection, "SELECT * FROM feiyu_recovery_attempts WHERE original_event_id=?",
                                         (selected["event_id"],))
                if not _feed_preflight(feed):
                    return consumed
                predecessor_digest = selected["predecessor_sha256"] if _is_renewal(selected) else _row_hash(predecessor)
                await connection.execute(f"""INSERT INTO {table}
                    (original_event_id,campaign_id,manifest_sha256,profile_id,post_id,source_created_at,
                     snapshot_json,claimed_at,day,state,revision_id,predecessor_sha256,claim_nonce)
                    VALUES (?,?,?,?,?,?,?,?,?,'claimed',?,?,?)""",
                    values + (_entry_revision(selected), predecessor_digest, secrets.token_hex(16)))
            else:
                await connection.execute(f"""INSERT INTO {table}
                    (original_event_id,campaign_id,manifest_sha256,profile_id,post_id,source_created_at,
                     snapshot_json,claimed_at,day,state) VALUES (?,?,?,?,?,?,?,?,?,'claimed')""", values)
        if not _live_authority(repository, digest, snapshot, feed):
            return consumed

        async def read_authorized():
            if (not _valid_feed(feed) or not _credentials_intact(credential_binding)
                    or not _manual_clear(repository, selected["post_id"])
                    or not _live_authority(repository, digest, snapshot, feed)):
                return False
            try:
                row = await _attempt_valid(repository.db.connection, repository, selected, digest, snapshot,
                                           permit_claimed=True)
                return bool(row is not None and _live_authority(repository, digest, snapshot, feed))
            except Exception:
                return False

        account_response = await feed.client._request(EndpointName.CURRENT_USER,
            before_request=read_authorized, after_response=read_authorized)
        if not await read_authorized():
            return consumed
        if not _comment_permission_allowed(account_response.payload, credential_binding["credentials"].uid):
            # Record a fixed terminal reason for this new attempt; never alter
            # any previously consumed claim. The existing feed exception hook
            # reports and pauses this genuine permission/contract failure.
            async with repository.db.transaction() as connection:
                row = await _attempt_valid(connection, repository, selected, digest, snapshot,
                                           permit_claimed=True)
                if (row is not None and _credentials_intact(credential_binding)
                        and _live_authority(repository, digest, snapshot, feed)):
                    await connection.execute(f"UPDATE {table} SET state='account_permission_rejected' "
                                             "WHERE original_event_id=? AND state='claimed' AND new_event_id IS NULL",
                                             (selected["event_id"],))
            raise RuntimeError("recovery_account_permission_rejected")
        response = await feed.client._request(EndpointName.POST_TREE,
            params={"link_id": selected["post_id"], "h_src": ""},
            before_request=read_authorized, after_response=read_authorized)
        if not await read_authorized():
            return consumed
        original = await repository.db.fetchone("SELECT * FROM incoming_events WHERE id=?", (selected["event_id"],))
        current = _current_post(response.payload, selected, original, feed) if original is not None else None
        if current is None:
            await repository.db.execute(f"UPDATE {table} SET state='source_unavailable' "
                                        "WHERE original_event_id=? AND state='claimed'", (selected["event_id"],))
            return consumed
        raw = {"event_type": "proactive_feed", "post": current}
        notification = Notification(
            profile_id=selected["profile_id"], external_event_id="feed:" + selected["post_id"],
            external_comment_id="feed:" + selected["post_id"], notification_id="feed:" + selected["post_id"],
            event_type=NotificationType.PROACTIVE_FEED, sender_uid=current["author"]["uid"],
            sender_nickname=current["author"]["nickname"], post_id=selected["post_id"],
            root_comment_id="", parent_comment_id="", content=clean_untrusted_text(current["content"], max_chars=4000),
            created_at=selected["source_created_at"], observed_at=time.time(),
            post_author_uid=current["author"]["uid"], explicit_wake=True,
            image_urls=list(current["image_urls"]), raw=raw)
        async with repository.db.transaction() as connection:
            row = await _attempt_valid(connection, repository, selected, digest, snapshot, permit_claimed=True)
            if row is None or not _live_authority(repository, digest, snapshot, feed):
                return consumed
            stamp = time.time()
            key = _event_key(row, selected)
            cursor = await connection.execute("""INSERT INTO incoming_events
                (profile_id,external_event_id,external_comment_id,notification_id,event_type,status,
                 sender_uid,sender_nickname,post_id,root_comment_id,parent_comment_id,content,raw_json,
                 discovered_at,claimed_at,updated_at) VALUES (?,?,?,?,?,'claimed',?,?,?,?,?,?,?,?,?,?)""",
                (selected["profile_id"], key, key, "feed:" + selected["post_id"], "proactive_feed",
                 notification.sender_uid, notification.sender_nickname, selected["post_id"], "", "",
                 notification.content, "{}", stamp, stamp, stamp))
            new_id = int(cursor.lastrowid)
            marker = {"campaign_id": _entry_campaign(selected), "original_event_id": selected["event_id"],
                      "new_event_id": new_id, "manifest_sha256": digest, "nonce": secrets.token_hex(16)}
            if _is_revision(selected) or _is_renewal(selected):
                marker.update(revision_id=_entry_revision(selected), predecessor_sha256=row["predecessor_sha256"],
                              claim_nonce=row["claim_nonce"])
            raw["_feiyu_recovery"] = marker
            raw["_adapter_timing"] = {"source_created_at": notification.created_at, "observed_at": notification.observed_at}
            raw["_adapter_identity"] = {"sender_uid_verified": notification.sender_uid_verified,
                                        "sender_identity_key": notification.sender_identity_key}
            raw_json = json.dumps(redact_data(raw), ensure_ascii=False, separators=(",", ":"))
            await connection.execute("UPDATE incoming_events SET raw_json=? WHERE id=?", (raw_json, new_id))
            await connection.execute("INSERT OR IGNORE INTO processed_event_keys "
                                     "(profile_id,event_key,key_type,created_at) VALUES (?,?,'notification',?)",
                                     (selected["profile_id"], key, stamp))
            await connection.execute(f"UPDATE {table} SET new_event_id=?,state='prepared',"
                                     "marker_json=?,new_raw_sha256=? WHERE original_event_id=? AND state='claimed'",
                                     (new_id, _canonical(marker), _hash(raw_json), selected["event_id"]))
        if not _live_authority(repository, digest, snapshot, feed):
            return consumed
        _issued[(id(repository), new_id)] = {"repository": repository, "notification": notification,
            "marker": marker, "task": asyncio.current_task(), "event": None, "entry": selected,
            "digest": digest, "snapshot": snapshot, "notification_hash": _hash(_canonical(asdict(notification))),
            "credential_binding": credential_binding}
        return notification, {"_feiyu_recovery_event_id": new_id,
                              "candidate_reason": "explicit_recovery",
                              "post_title": current["title"], "post_author_uid": notification.sender_uid}
    except asyncio.CancelledError:
        raise


def identity_allowed(repository, event_id, notification=None, event=None):
    """Bind the candidate and real scheduler event to the one issued object."""
    issued = _issued.get((id(repository), event_id))
    raw = getattr(notification, "raw", None) if notification is not None else None
    event_raw = getattr(getattr(event, "message_obj", None), "raw_message", None) if event is not None else None
    looks_recovery = (isinstance(raw, Mapping) and "_feiyu_recovery" in raw
                      or isinstance(event_raw, Mapping) and "feiyu_recovery" in event_raw)
    if issued is None:
        return False if looks_recovery else None
    if (issued["repository"] is not repository or not _credentials_intact(issued.get("credential_binding"))
            or not _live_authority(repository, issued["digest"], issued["snapshot"])):
        return False
    task = issued["task"]
    if task is None or task.done() or task.cancelling():
        return False
    marker = issued["marker"]
    if notification is not None:
        return bool(asyncio.current_task() is task and notification is issued["notification"]
                    and _hash(_canonical(asdict(notification))) == issued["notification_hash"]
                    and isinstance(raw, Mapping) and raw.get("_feiyu_recovery") == marker
                    and notification.profile_id == issued["entry"]["profile_id"]
                    and notification.post_id == issued["entry"]["post_id"])
    if event is not None:
        if not isinstance(event_raw, Mapping) or event_raw.get("feiyu_recovery") != marker:
            return False
        if issued["event"] is None and asyncio.current_task() is task:
            issued["event"] = event
        return bool(issued["event"] is event and event.event_id == event_id
                    and event.route.profile_id == issued["entry"]["profile_id"]
                    and event.route.post_id == issued["entry"]["post_id"])
    return False


async def claimed_event(repository, notification, metadata):
    if not isinstance(metadata, dict) or "_feiyu_recovery_event_id" not in metadata:
        return None
    event_id = metadata["_feiyu_recovery_event_id"]
    if type(event_id) is not int or identity_allowed(repository, event_id, notification=notification) is not True:
        return None
    issued = _issued[(id(repository), event_id)]
    try:
        row = await _attempt_valid(repository.db.connection, repository, issued["entry"], issued["digest"],
                                   issued["snapshot"], new_event_id=event_id)
        return event_id if row is not None and row["state"] == "prepared" else None
    except Exception:
        return None


async def _issued_attempt(repository, event_id, connection):
    issued = _issued.get((id(repository), event_id))
    if (issued is None or issued["repository"] is not repository
            or not _credentials_intact(issued.get("credential_binding"))):
        return None
    if not _live_authority(repository, issued["digest"], issued["snapshot"]):
        return None
    if not _manual_clear(repository, issued["entry"]["post_id"]):
        return None
    row = await _attempt_valid(connection, repository, issued["entry"], issued["digest"],
                               issued["snapshot"], new_event_id=event_id)
    return (issued, row) if row is not None and _live_authority(repository, issued["digest"], issued["snapshot"]) else None


async def reserve(repository, event_id, notification):
    marker = isinstance(getattr(notification, "raw", None), Mapping) and "_feiyu_recovery" in notification.raw
    if not marker and (id(repository), event_id) not in _issued:
        return None
    if identity_allowed(repository, event_id, notification=notification) is not True:
        return False, "recovery_identity_invalid", True
    try:
        async with repository.db.transaction() as connection:
            result = await _issued_attempt(repository, event_id, connection)
            if result is None or result[1]["state"] != "prepared":
                return False, "recovery_claim_invalid", True
            await connection.execute(f"UPDATE {_attempt_table(result[0]['entry'])} SET state='reserved' "
                                     "WHERE new_event_id=? AND state='prepared'", (event_id,))
        return True, "recovery_reserved", False
    except Exception:
        return False, "recovery_guard_unavailable", True


async def authorize(repository, event_id, route, *, phase, now=None):
    issued = _issued.get((id(repository), event_id))
    if issued is None:
        try:
            connection = repository.db.connection
            if not await _storage_intact(connection, repository):
                return False
            for table in (LEGACY_TABLE, REVISION_TABLE, RENEWAL_TABLE):
                if await _table_exists(connection, table) and await _one(connection,
                        f"SELECT 1 FROM {table} WHERE new_event_id=?", (event_id,)):
                    return False
            return None
        except Exception:
            return False
    table = _attempt_table(issued["entry"])
    if phase not in {"context", "dispatch", "check", "send", "http"}:
        return False
    if (str(route.profile_id) != issued["entry"]["profile_id"]
            or str(route.post_id) != issued["entry"]["post_id"]
            or str(route.root_comment_id or "") or str(route.parent_comment_id or "")
            or str(route.notification_id or "") != "feed:" + issued["entry"]["post_id"]):
        return False
    # The native candidate or scheduler scope must still own this exact route.
    from .feiyu_peak_bridge import gate_allowed
    if gate_allowed(repository, event_id, route) is not True:
        return False
    try:
        allowed = False
        async with repository.db.transaction() as connection:
            result = await _issued_attempt(repository, event_id, connection)
            if result is None:
                return False
            row = result[1]
            if phase == "context":
                allowed = row["state"] == "reserved" and row["dispatched_at"] is None
            elif phase == "dispatch":
                if row["state"] != "reserved" or row["dispatched_at"] is not None:
                    return False
                await connection.execute(f"UPDATE {table} SET state='dispatched',dispatched_at=? "
                                         "WHERE new_event_id=? AND state='reserved'", (time.time(), event_id))
                allowed = True
            elif phase == "check":
                allowed = row["state"] in {"dispatched", "sending"} and row["dispatched_at"] is not None
            elif phase == "send":
                if row["state"] != "dispatched" or row["dispatched_at"] is None or row["send_started_at"] is not None:
                    return False
                await connection.execute(f"UPDATE {table} SET state='sending',send_started_at=? "
                                         "WHERE new_event_id=? AND state='dispatched'", (time.time(), event_id))
                allowed = True
            else:
                if row["state"] != "sending" or row["send_started_at"] is None or row["http_started_at"] is not None:
                    return False
                outgoing = await _one(connection, "SELECT COUNT(*) AS n FROM outgoing_replies "
                                      "WHERE incoming_event_id=? AND status='sending'", (event_id,))
                if outgoing is None or outgoing["n"] != 1:
                    return False
                await connection.execute(f"UPDATE {table} SET http_started_at=? "
                                         "WHERE new_event_id=? AND http_started_at IS NULL", (time.time(), event_id))
                allowed = True
        # The transaction exit awaits its commit. Recheck the local authority
        # and incident generation synchronously after that final yield.
        return bool(allowed and _credentials_intact(issued.get("credential_binding"))
                    and _manual_clear(repository, issued["entry"]["post_id"])
                    and _live_authority(repository, issued["digest"], issued["snapshot"]))
    except Exception:
        return False


async def source_exception(repository, event_id, snapshot):
    issued = _issued.get((id(repository), event_id))
    if issued is None or issued["snapshot"] != snapshot:
        return False
    try:
        result = await _issued_attempt(repository, event_id, repository.db.connection)
        return bool(result is not None and result[1]["state"] in {"dispatched", "sending"}
                    and _live_authority(repository, issued["digest"], snapshot))
    except Exception:
        return False


async def normal_send_allowed(repository, event_id, route):
    """Both permanent ledgers fence every ordinary and manual send."""
    try:
        connection = repository.db.connection
        if not await _storage_intact(connection, repository):
            return False
        for table in (RENEWAL_TABLE, REVISION_TABLE, LEGACY_TABLE):
            if not await _table_exists(connection, table):
                continue
            row = await _one(connection, f"SELECT new_event_id FROM {table} WHERE profile_id=? AND post_id=?",
                             (str(route.profile_id), str(route.post_id)))
            if row is not None:
                if event_id is None or row["new_event_id"] != event_id:
                    return False
                return await authorize(repository, event_id, route, phase="check") is True
        return True
    except Exception:
        return False
