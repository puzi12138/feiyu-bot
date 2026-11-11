"""One guarded immediate Bili discovery for an already armed F060 trial.

The grant exists only in the current hook owner and process generation.  It
never contains credentials or travels in model-visible text/metadata.  A new
watch is evidenced by the existing action reservation, native watch/cache and
new queue row; no historical timestamp is rewritten.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .new_content_retry import content_hash
from .peak_runtime import automatic_scope

_BV = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_ATTEMPTS = {}
_REQUESTED = set()


def _owner_current(attempt, *, require_intake=True):
    """Recheck actual owner, policy generation, window and independent fault."""
    try:
        host, owner, bot, incident_id = (attempt[k] for k in ("host", "owner", "bot", "incident_id"))
        from .bili_journal_review import _allowed
        current = host.incidents._retry_current(incident_id)
        return bool(_ATTEMPTS.get(incident_id) is attempt and owner._active is True
            and getattr(host, "_incident_hooks", None) is owner
            and owner.retry_local_preflight("bili_journal") is True
            and owner._blocked("bili_browse") is False
            and host.plugin("astrbot_plugin_bilibili_ai_bot") is bot
            and bot._running is True and bot._bili_incident_browse_ready() is True
            and _allowed(host) and current is not None and current["scope"] == "bili_journal"
            and current["generation"] == host.incidents._retry_store.generation
            and (not require_intake or (current["phase"] == "ready"
                 and host.incidents.retry_intake_allowed(incident_id) is True)))
    except Exception:
        return False


def _other_browse_notice(attempt):
    try:
        status = attempt["host"].incidents.status("bili_browse")
        row = status.get("active", {}).get("bili_browse", {})
        return (row.get("state") in {"closing", "paused"}
                and row.get("notification") in {"pending", "sent", "unknown"})
    except Exception:
        return False


async def _abort(attempt):
    """Close a ready trial once; another notified browse fault is not duplicated."""
    try:
        host, owner, incident_id = attempt["host"], attempt["owner"], attempt["incident_id"]
        if (getattr(host, "_incident_hooks", None) is not owner or not owner._active
                or host.incidents._retry_current(incident_id) is None):
            return False
        return await host.incidents.retry_abort_ready(incident_id,
            other_fault_notified=_other_browse_notice(attempt))
    except Exception:
        return False


def request_immediate(host, owner, incident_id):
    """Register and schedule exactly one wake after verified terminal/notice."""
    if type(incident_id) is not str or _ID.fullmatch(incident_id) is None:
        return False
    try:
        if incident_id in _REQUESTED or getattr(host, "_incident_hooks", None) is not owner:
            return False
        slot = host.incidents.retry_intake("bili_journal")
        bot = host.plugin("astrbot_plugin_bilibili_ai_bot")
        if not isinstance(slot, dict) or slot.get("incident_id") != incident_id or bot is None:
            return False
        attempt = {"host": host, "owner": owner, "bot": bot, "incident_id": incident_id,
                   "failed_at": slot["failed_at"], "run_task": None, "coordinator": None,
                   "wake_event": asyncio.Event(), "started": False, "proof": None}
        _ATTEMPTS[incident_id] = attempt
        if not _owner_current(attempt):
            _ATTEMPTS.pop(incident_id, None)
            return False
        _REQUESTED.add(incident_id)
        existing = getattr(bot, "_proactive_task", None)
        if existing is not None and not existing.done():
            attempt["wake_event"].set()
            event = getattr(bot, "_feiyu_immediate_wake_event", None)
            if event is None:
                event = bot._feiyu_immediate_wake_event = asyncio.Event()
            event.set()
        with automatic_scope():
            task = asyncio.create_task(_drive(attempt))
        attempt["coordinator"] = task
        tasks = getattr(host, "tasks", None)
        if isinstance(tasks, set):
            tasks.add(task); task.add_done_callback(tasks.discard)
        return True
    except Exception:
        # No repeated launch; an unconfirmed registration is conservative.
        return False


def valid_coordinator(bot, attempt):
    try:
        return bool(attempt["bot"] is bot and attempt["coordinator"] is asyncio.current_task()
                    and _owner_current(attempt))
    except Exception:
        return False


def valid_in_transaction(bot, attempt):
    """The scheduling transaction runs in a worker thread, without task vars."""
    try:
        return bool(attempt["bot"] is bot and attempt["coordinator"] is not None
                    and not attempt["coordinator"].done() and _owner_current(attempt))
    except Exception:
        return False


def bind_run_task(bot, attempt, task):
    if not valid_coordinator(bot, attempt) or task is not bot._proactive_task or attempt["run_task"] is not None:
        return False
    attempt["run_task"] = task
    return True


def valid_run(bot, attempt):
    try:
        return bool(attempt["bot"] is bot and attempt["run_task"] is asyncio.current_task()
                    and attempt["run_task"] is bot._proactive_task and _owner_current(attempt))
    except Exception:
        return False


async def interruptible_wait(bot, seconds):
    """End an old run only after its current item completed normally."""
    matches = [item for item in _ATTEMPTS.values()
               if item["bot"] is bot and item["run_task"] is None and item["wake_event"].is_set()
               and _owner_current(item)]
    if matches:
        return True
    event = getattr(bot, "_feiyu_immediate_wake_event", None)
    if event is None:
        event = bot._feiyu_immediate_wake_event = asyncio.Event()
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
        return any(item["bot"] is bot and item["wake_event"].is_set() and _owner_current(item)
                   for item in _ATTEMPTS.values())
    except asyncio.TimeoutError:
        return False


async def eligible_before_watch(bot, attempt, bvid, *, after_reservation=False):
    """Reject any known old item before title/model/video work begins.

    ``False`` is a confirmed duplicate; ``None`` means evidence unavailable
    and must stop discovery instead of probing additional candidates.
    """
    if not valid_run(bot, attempt) or type(bvid) is not str or _BV.fullmatch(bvid) is None:
        return None
    try:
        from . import bili_journal_review as review
        slot = attempt["host"].incidents.retry_intake("bili_journal")
        if not slot or content_hash("bili_journal", bvid) == slot["failed_key_hash"]:
            return False
        root = review._root()
        queue = root / review._QUEUE
        humanism = root / review._HUMANISM
        if any(path.is_symlink() or not path.is_file() for path in (queue, humanism)):
            return None
        with closing(sqlite3.connect(queue.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            if db.execute("SELECT 1 FROM watched WHERE bvid=?", (bvid,)).fetchone():
                return False
            has_owner = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_recovery'").fetchone()
            if has_owner and db.execute("SELECT 1 FROM owner_recovery WHERE bvid=?", (bvid,)).fetchone():
                return False
        with closing(sqlite3.connect(humanism.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            if db.execute("SELECT 1 FROM readings WHERE bvid=? LIMIT 1", (bvid,)).fetchone():
                return False
        available, note = review._journal_lookup(root, bvid)
        if not available:
            return None
        if note:
            return False
        layered = bot.layered_runtime
        if not layered.is_open or await layered.seen_videos.contains(bvid):
            return False if layered.is_open else None
        if any(str(item.get("bvid") or "") == bvid for item in bot._legacy_seen_video_records()
               if isinstance(item, dict)):
            return False
        action = await layered.db.fetch_one("SELECT state,attempts FROM actions WHERE key=?",
                                             ("proactive_watch:" + bvid,))
        if not after_reservation and action is not None:
            return False
        if after_reservation and (action is None or action["state"] != "succeeded" or action["attempts"] != 1):
            return None
        return True if valid_run(bot, attempt) else None
    except Exception:
        return None


async def watch_started(bot, attempt, bvid, outcome):
    """Bind only the one fresh, successfully reserved native watch action."""
    if (not valid_run(bot, attempt) or attempt["started"] or type(bvid) is not str
            or _BV.fullmatch(bvid) is None or getattr(outcome, "success", None) is not True
            or getattr(outcome, "duplicate", None) is not False
            or getattr(outcome, "state", None) != "succeeded"):
        return False
    try:
        if await eligible_before_watch(bot, attempt, bvid, after_reservation=True) is not True:
            return False
        trial = attempt["host"].incidents.retry_intake("bili_journal")
        if not trial or content_hash("bili_journal", bvid) == trial["failed_key_hash"]:
            return False
        db = bot.layered_runtime.db
        row = await db.fetch_one("SELECT key,kind,target_id,state,attempts,created_at,finished_at "
                                 "FROM actions WHERE key=?", ("proactive_watch:" + bvid,))
        if (row is None or row["key"] != "proactive_watch:" + bvid or row["kind"] != "proactive_watch"
                or row["target_id"] != bvid or row["state"] != "succeeded" or row["attempts"] != 1
                or type(row["created_at"]) not in (int, float) or type(row["finished_at"]) not in (int, float)
                or not attempt["failed_at"] < row["created_at"] <= row["finished_at"] <= time.time() + 2
                or not valid_run(bot, attempt)):
            return False
        attempt.update(started=True, bvid=bvid, observed_at=float(row["created_at"]),
                       action_finished_at=float(row["finished_at"]))
        return True
    except Exception:
        return False


def _row_digest(row):
    return hashlib.sha256(json.dumps(list(row), ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def watch_completed(bot, attempt, bvid, watched_at, inserted):
    """Bind this task's just-inserted queue row to existing native evidence."""
    if (not valid_run(bot, attempt) or not attempt["started"] or inserted is not True
            or attempt.get("bvid") != bvid or type(watched_at) is not str):
        return False
    try:
        from . import bili_journal_review as review
        root = review._root()
        if review._native_evidence(root, bvid, watched_at) is None:
            return False
        path = root / review._QUEUE
        if path.is_symlink() or not path.is_file():
            return False
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            row = db.execute("SELECT * FROM watched WHERE bvid=?", (bvid,)).fetchone()
        if (row is None or len(row) != 10 or row[1] != watched_at or row[2:4] != ("pending", "queued")
                or row[4] is not None or row[5] is not None or row[7] != 0):
            return False
        created = datetime.fromisoformat(row[8])
        if (created.tzinfo is None or created.timestamp() <= attempt["failed_at"]
                or created.timestamp() < attempt["action_finished_at"] or not valid_run(bot, attempt)):
            return False
        attempt["proof"] = {"bvid": bvid, "watched_at": watched_at,
                            "row_sha256": _row_digest(row), "observed_at": attempt["observed_at"]}
        return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False


def observed_same_minute(steward, row, trial):
    """Return a real seconds timestamp only for the exact in-process watch."""
    try:
        attempt = _ATTEMPTS.get(trial["incident_id"])
        proof = attempt.get("proof") if attempt else None
        if (not attempt or attempt["host"] is not steward or not _owner_current(attempt, require_intake=False)
                or attempt["run_task"] is not attempt["bot"]._proactive_task or not isinstance(proof, dict)
                or trial["failed_at"] != attempt["failed_at"] or proof["bvid"] != row[0]
                or proof["watched_at"] != row[1] or proof["row_sha256"] != _row_digest(row)):
            return None
        current = steward.incidents._retry_current(trial["incident_id"])
        if current["phase"] not in {"ready", "reserved"}:
            return None
        if current["phase"] == "reserved" and current["candidate_key_hash"] != content_hash("bili_journal", row[0]):
            return None
        from . import bili_journal_review as review
        expected = (review._root() / review._NATIVE / "bilibot.sqlite3").resolve()
        database = Path(attempt["bot"].layered_runtime.db._path)
        if database.is_symlink() or database.resolve() != expected or not database.is_file():
            return None
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            action = db.execute("SELECT kind,target_id,state,attempts,created_at,finished_at "
                                "FROM actions WHERE key=?", ("proactive_watch:" + row[0],)).fetchone()
        if (action is None or action[:4] != ("proactive_watch", row[0], "succeeded", 1)
                or action[4] != proof["observed_at"] or action[5] != attempt["action_finished_at"]):
            return None
        return proof["observed_at"] if trial["failed_at"] < proof["observed_at"] <= time.time() else None
    except Exception:
        return None


def immediate_candidate(steward, row, trial):
    """An active immediate run may only review the exact video it watched."""
    attempt = _ATTEMPTS.get(trial.get("incident_id")) if isinstance(trial, dict) else None
    if attempt is None:
        return False, None
    return True, observed_same_minute(steward, row, trial)


async def _drive(attempt):
    """Finish any active watch, then run one bounded fresh discovery."""
    bot, host = attempt["bot"], attempt["host"]
    try:
        existing = getattr(bot, "_proactive_task", None)
        if existing is not None and not existing.done():
            try:
                await asyncio.wait_for(asyncio.shield(existing), timeout=300)
            except asyncio.TimeoutError:
                await _abort(attempt)
                return
        if not valid_coordinator(bot, attempt):
            if _other_browse_notice(attempt):
                await _abort(attempt)
            return
        started = await bot._maybe_run_interest_proactive(_retry_attempt=attempt)
        if started is not True:
            await _abort(attempt)
            return
        running = attempt["run_task"]
        if running is None:
            await _abort(attempt)
            return
        await asyncio.shield(running)
        if attempt.get("proof") is None:
            await _abort(attempt)
            return
        if not _owner_current(attempt, require_intake=False):
            return
        binding = attempt["owner"]._bindings.get("bili_journal")
        if (not isinstance(binding, dict)
                or getattr(binding.get("object"), binding.get("name", ""), None) is not binding.get("wrapped")):
            await _abort(attempt)
            return
        # The actual hook is installed on steward.main's imported alias, not
        # on the originating module. Never bypass it with a raw import.
        await binding["wrapped"](host)
        if host.incidents.retry_intake_allowed(attempt["incident_id"]):
            # Busy, invalidated evidence or an empty selection is never a loop.
            await _abort(attempt)
    except asyncio.CancelledError:
        raise
    except Exception:
        await _abort(attempt)
    finally:
        event = getattr(bot, "_feiyu_immediate_wake_event", None)
        if event is not None:
            event.clear()
