"""Scheduled DeepSeek account observations compared with the local Feiyu ledger.

No QQ or model calls. Official all-key figures and local observed records have
different coverage. Persist diagnostic differences without altering either.
"""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .peak_policy import automatic_peak_paused

TZ = timezone(timedelta(hours=8))
INTERVAL_SECONDS = 2 * 60 * 60
MAX_COUNT = 10**12
DB_NAME = "deepseek-reconciliation-2h.sqlite"
SAFE_OFFICIAL_FAILURES = frozenset({
    "official_data_unavailable", "official_login_required", "official_browser_unavailable",
    "official_query_busy", "official_query_not_completed", "official_query_timeout",
    "official_refresh_disabled", "unsupported_period",
})


def _now(value=None):
    value = value or datetime.now(TZ)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(TZ)


def _database_path(root):
    root = Path(root).resolve()
    directory = root / "data" / "token-usage"
    if not directory.is_dir() or directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise OSError("invalid_reconciliation_directory")
    path = directory / DB_NAME
    for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise OSError("invalid_reconciliation_file")
        if candidate.exists() and candidate.stat().st_nlink != 1:
            raise OSError("invalid_reconciliation_file")
    return path


def _configuration(root):
    """Keep the new scheduler distinct from the legacy export reconciler."""
    try:
        root = Path(root).resolve()
        path = root / "config" / "token-query-cockpit-only.json"
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root) or path.stat().st_size > 32768:
            return "configuration_unavailable"
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            return "configuration_unavailable"
        if raw.get("deepseek_periodic_reconciliation_enabled") is not True:
            return "disabled"
        if (type(raw.get("deepseek_periodic_reconciliation_interval_seconds")) is not int
                or raw["deepseek_periodic_reconciliation_interval_seconds"] != INTERVAL_SECONDS
                or raw.get("deepseek_automatic_reconciliation_enabled") is not False
                or raw.get("deepseek_automatic_browser_enabled") is not True
                or raw.get("official_browser_mode") != "on_demand"):
            return "configuration_invalid"
        return "enabled"
    except (OSError, ValueError, TypeError, OverflowError):
        return "configuration_unavailable"


def _connect(path, *, read_only=False):
    if read_only:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(path, timeout=3, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
    conn.row_factory = sqlite3.Row
    return conn


def _claim(root, now):
    """Persist a single attempt and the next eligible time before browser I/O."""
    timestamp = now.timestamp()
    with closing(_connect(_database_path(root))) as db:
        db.execute("CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY CHECK(id=1), next_epoch REAL NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY, started_epoch REAL NOT NULL,
            finished_epoch REAL, state TEXT NOT NULL, record_json TEXT NOT NULL)""")
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT next_epoch FROM schedule WHERE id=1").fetchone()
            if row is not None and timestamp < row["next_epoch"]:
                db.commit()
                return None
            # An interrupted attempt stays in history, but cannot block forever.
            db.execute("UPDATE attempts SET state='interrupted' WHERE state='running' AND started_epoch<=?",
                       (timestamp - INTERVAL_SECONDS,))
            attempt_id = uuid.uuid4().hex
            db.execute("INSERT INTO attempts VALUES (?, ?, NULL, 'running', ?)",
                       (attempt_id, timestamp, json.dumps({"state": "running"})))
            db.execute("INSERT INTO schedule VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET next_epoch=excluded.next_epoch",
                       (timestamp + INTERVAL_SECONDS,))
            db.commit()
            return attempt_id
        except BaseException:
            db.rollback()
            raise


def _finish(root, attempt_id, record, now):
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with closing(_connect(_database_path(root))) as db:
        db.execute("UPDATE attempts SET finished_epoch=?, state=?, record_json=? WHERE attempt_id=? AND state='running'",
                   (now.timestamp(), record["state"], encoded, attempt_id))


def _iso_epoch(value):
    return datetime.fromtimestamp(value, TZ).isoformat(timespec="seconds")


def reconciliation_status(root, *, now=None):
    """Read scheduler state and observed timestamps; never acquire new data."""
    config = _configuration(root)
    view = {"enabled": config == "enabled", "state": config if config != "enabled" else "not_started",
            "next_due_at": None}
    try:
        current = _now(now)
        path = _database_path(root)
        if not path.is_file():
            return view
        with closing(_connect(path, read_only=True)) as db:
            schedule = db.execute("SELECT next_epoch FROM schedule WHERE id=1").fetchone()
            last = db.execute("SELECT started_epoch, finished_epoch, state FROM attempts ORDER BY started_epoch DESC LIMIT 1").fetchone()
        view.update(last_started_at=_iso_epoch(last["started_epoch"]) if last else None,
                    last_finished_at=_iso_epoch(last["finished_epoch"]) if last and last["finished_epoch"] else None,
                    next_due_at=_iso_epoch(schedule["next_epoch"]) if schedule else None)
        if config == "enabled" and last:
            # Persistent 'running' cannot prove a live task after a crash.
            view["state"] = "running_or_interrupted" if last["state"] == "running" else last["state"]
        latest = latest_record(root)
        if latest is not None:
            view["latest_attempt"] = _timed_view(latest, current)
        observed = last_observed_record(root)
        if observed is not None and (latest is None or observed != latest):
            view["last_observation"] = _timed_view(observed, current)
        return view
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return {"enabled": config == "enabled", "state": "status_unavailable", "next_due_at": None}


def _latest_record(root, *, observed_only):
    try:
        path = _database_path(root)
        if not path.is_file():
            return None
        with closing(_connect(path, read_only=True)) as db:
            statement = "SELECT record_json FROM attempts WHERE finished_epoch IS NOT NULL"
            if observed_only:
                statement += " AND state='observed_incomplete'"
            row = db.execute(statement + " ORDER BY started_epoch DESC LIMIT 1").fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        return record if isinstance(record, dict) and record.get("schema") == "deepseek_reconciliation_2h_v1" else None
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def latest_record(root):
    """Read the most recent finished attempt; it may contain an outage."""
    return _latest_record(root, observed_only=False)


def last_observed_record(root):
    """Read the last attempt with an official observation, including its time."""
    return _latest_record(root, observed_only=True)


def _timed_view(record, current):
    """Copy period observations and calculate age relative to this read."""
    view = json.loads(json.dumps(record))
    for name, period in view.get("periods", {}).items():
        official = period.get("official") if isinstance(period, dict) else None
        if not isinstance(official, dict):
            continue
        try:
            observed = _now(datetime.fromisoformat(official["observed_at"]))
            seconds = (current - observed).total_seconds()
            expected_day = (current.date() - timedelta(days=name == "yesterday")).isoformat()
            range_current = name in ("today", "yesterday") and official.get("day") == expected_day
            official["age_seconds"] = int(seconds) if seconds >= 0 else None
            official["range_current"] = range_current
            official["snapshot_cache_stale"] = seconds < 0 or seconds > 15 * 60 or not range_current
            official["scheduled_refresh_overdue"] = seconds < 0 or seconds >= INTERVAL_SECONDS or not range_current
        except (ValueError, TypeError, KeyError, OverflowError):
            official["age_seconds"] = None
            official["range_current"] = False
            official["snapshot_cache_stale"] = True
            official["scheduled_refresh_overdue"] = True
    return view


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ValueError("invalid_local_usage")
    return value


def _local(root, start, end):
    """Read only Feiyu's recorded DeepSeek rows for the specified interval."""
    from .local_feature_query import _connect_readonly
    from .token_pricing import price_feiyu_rows

    with closing(_connect_readonly(root)) as db:
        db.execute("BEGIN")
        start_row = db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()
        if start_row is None:
            raise ValueError("local_start_unavailable")
        observed_since = float(start_row[0])
        if not math.isfinite(observed_since) or observed_since < 0:
            raise ValueError("local_start_unavailable")
        rows = [dict(row) for row in db.execute(
            "SELECT at,feature,model,usage_known,input_other,input_cached,output FROM requests "
            "WHERE model='deepseek-flash' AND at>=? AND at<? ORDER BY at",
            (start.timestamp(), end.timestamp()))]
        db.commit()
    known = []
    for row in rows:
        if row["usage_known"] not in (0, 1) or type(row["at"]) not in (int, float) or not math.isfinite(row["at"]):
            raise ValueError("invalid_local_usage")
        if row["usage_known"] == 1:
            known.append(sum(_count(row[key]) for key in ("input_other", "input_cached", "output")))
    price = price_feiyu_rows(rows)
    priced = _count(price["priced_calls"])
    unpriced = _count(price["unpriced_calls"])
    if priced + unpriced != len(rows):
        raise ValueError("invalid_local_pricing")
    # An empty ledger is not proof that account-wide usage was zero.
    return {"from": start.isoformat(timespec="seconds"), "until": end.isoformat(timespec="seconds"),
            "observed_since": _iso_epoch(observed_since), "coverage_complete": False,
            "model_filter": "deepseek-flash",
            "recorded_calls": len(rows), "known_usage_calls": len(known),
            "unknown_usage_calls": len(rows) - len(known),
            "known_tokens": sum(known) if known else None,
            "priced_calls": priced, "unpriced_calls": unpriced,
            "known_priced_cny": str(Decimal(str(price["known_cny"]))) if priced else None,
            "price_basis": "offline_dated_standard_rates_estimate"}


def _official(result, period):
    """Project safe numeric fields from the validated official query response."""
    if not isinstance(result, dict) or result.get("ok") is not True:
        status = result.get("status") if isinstance(result, dict) else None
        return None, status if status in SAFE_OFFICIAL_FAILURES else "official_data_unavailable"
    s = result.get("summary")
    if not isinstance(s, dict) or s.get("period") != period or s.get("fresh") is not True or s.get("key_filter") != "all":
        return None, "official_data_unavailable"
    observed = _now(datetime.fromisoformat(s["observed_at"]))
    expected = (observed.date() - timedelta(days=period == "yesterday")).isoformat()
    if s.get("day") != expected or s.get("data_lag_minutes") != 5:
        return None, "official_data_unavailable"
    amount = Decimal(s["cost_cny"])
    if not amount.is_finite() or not 0 <= amount <= MAX_COUNT:
        return None, "official_data_unavailable"
    return {"day": expected, "observed_at": observed.isoformat(), "key_filter": "all",
            "data_lag_minutes": 5, "request_count": _count(s["request_count"]),
            "total_tokens": _count(s["total_tokens"]), "cost_cny": str(amount)}, None


def _period_record(root, period, result):
    try:
        official, error = _official(result, period)
        if error:
            return {"period": period, "state": "official_unavailable", "reason": error}
        observed = _now(datetime.fromisoformat(official["observed_at"]))
        day = datetime.fromisoformat(official["day"]).date()
        start = datetime.combine(day, time.min, TZ)
        end = start + timedelta(days=1) if period == "yesterday" else observed
    except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
        return {"period": period, "state": "official_unavailable",
                "reason": "official_data_unavailable"}
    try:
        local = _local(root, start, end)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
        # Retain the independently validated account observation even if the
        # local ledger cannot be read. Never replace its absence with zeros.
        return {"period": period, "state": "observed_incomplete", "official": official,
                "local_feiyu": None, "diagnostic_differences": None,
                "limitations": ["local_ledger_unavailable"]}
    try:
        reasons = ["official_all_keys_vs_feiyu_recorded_scope",
                   "same_account_and_subset_relationship_unverified",
                   "official_model_scope_vs_local_deepseek_flash", "local_scope_not_proven_complete",
                   "official_tools_not_recorded_in_local_token_ledger", "request_count_semantics_not_proven_equal",
                   "official_amount_is_display_value_rounding_unverified",
                   "local_amount_is_known_standard_price_estimate",
                   "official_data_lag_is_approximate_not_settlement_finality"]
        if datetime.fromisoformat(local["observed_since"]) > start:
            reasons.append("local_history_starts_after_period_start")
        if local["unknown_usage_calls"]:
            reasons.append("local_usage_unknown")
        if local["unpriced_calls"]:
            reasons.append("local_price_unknown")
        delta = {"official_minus_local_recorded_calls": None,
                 "official_minus_local_known_tokens": None,
                 "official_minus_local_known_estimate_cny": None}
        if period == "today":
            # Five minutes is an advertised delay, not an exact cutoff. Do not
            # invent a same-window difference for an in-progress day.
            reasons.append("official_current_day_cutoff_not_exact")
            state = "observed_incomplete"
        elif observed < end + timedelta(minutes=5):
            reasons.append("official_daily_publication_lag")
            state = "observed_incomplete"
        else:
            # These are diagnostic observations across explicitly different
            # coverage, not a proof of a discrepancy or corrected invoice.
            if local["recorded_calls"]:
                delta["official_minus_local_recorded_calls"] = official["request_count"] - local["recorded_calls"]
            if local["known_tokens"] is not None:
                delta["official_minus_local_known_tokens"] = official["total_tokens"] - local["known_tokens"]
            if local["known_priced_cny"] is not None:
                delta["official_minus_local_known_estimate_cny"] = str(
                    Decimal(official["cost_cny"]) - Decimal(local["known_priced_cny"]))
            state = "diagnostic_incomplete"
        return {"period": period, "state": state, "official": official, "local_feiyu": local,
                "diagnostic_differences": delta, "limitations": reasons}
    except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
        return {"period": period, "state": "local_or_official_validation_unavailable",
                "reason": "observation_unavailable"}


async def _execute(owner, root, attempt_id, query, now_fn):
    periods = {}
    for period in ("today", "yesterday"):
        try:
            if automatic_peak_paused(_now(now_fn()).timestamp()):
                periods[period] = {"period": period, "state": "not_requested",
                                   "reason": "peak_tariff"}
                continue
            if (getattr(owner, "_token_usage_stopped", False)
                    or await asyncio.to_thread(_configuration, root) != "enabled"):
                periods[period] = {"period": period, "state": "not_requested",
                                   "reason": "periodic_reconciliation_disabled"}
                continue
            if automatic_peak_paused(_now(now_fn()).timestamp()):
                periods[period] = {"period": period, "state": "not_requested",
                                   "reason": "peak_tariff"}
                continue
            # Always acquire a new, validated browser snapshot per run.
            result = await query(period, refresh=True, force_refresh=True)
            periods[period] = await asyncio.to_thread(_period_record, root, period, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            periods[period] = {"period": period, "state": "official_unavailable",
                               "reason": "official_query_failed"}
    state = "observed_incomplete" if any(
        p["state"] in ("diagnostic_incomplete", "observed_incomplete") for p in periods.values()
    ) else "unavailable"
    record = {"schema": "deepseek_reconciliation_2h_v1", "state": state,
              "finished_at": _now(now_fn()).isoformat(), "periods": periods}
    today_official = periods["today"].get("official")
    yesterday_official = periods["yesterday"].get("official")
    record["periods_crossed_midnight"] = (
        datetime.fromisoformat(today_official["observed_at"]).date()
        != datetime.fromisoformat(yesterday_official["observed_at"]).date()
        if today_official and yesterday_official else None)
    try:
        await asyncio.to_thread(_finish, root, attempt_id, record, _now(now_fn()))
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        # The existing persisted running attempt remains due only in two hours.
        return


async def maybe_reconcile_deepseek(owner, root, *, now=None, query=None, now_fn=None):
    """Schedule at most one due job; return immediately after durable claim.

    Integrate once in the existing non-model monitor loop. A persisted two-hour
    eligibility window prevents restart, failures or 30-second ticks from
    causing retries. The returned task is retained by the plugin owner.
    """
    active = getattr(owner, "_deepseek_reconcile_task", None)
    config = await asyncio.to_thread(_configuration, root)
    if config != "enabled":
        if active is not None and not active.done():
            active.cancel()
        return {"state": config}
    if active is not None and not active.done():
        return {"state": "running"}
    if automatic_peak_paused(_now(now).timestamp()):
        return {"state": "peak_tariff"}
    try:
        attempt = await asyncio.to_thread(_claim, root, _now(now))
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return {"state": "scheduler_unavailable"}
    if attempt is None:
        return {"state": "not_due"}
    config = await asyncio.to_thread(_configuration, root)
    if config != "enabled" or getattr(owner, "_token_usage_stopped", False):
        record = {"schema": "deepseek_reconciliation_2h_v1", "state": "disabled",
                  "finished_at": _now(now).isoformat(), "periods": {}}
        try:
            await asyncio.to_thread(_finish, root, attempt, record, _now(now))
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            pass
        return {"state": "disabled"}
    if query is None:
        from .official_token_query import query_period

        async def query(period, *, refresh, force_refresh):
            return await query_period(period, refresh=refresh, force_refresh=force_refresh, automatic=True)
    task = asyncio.create_task(
        _execute(owner, root, attempt, query, now_fn or (lambda: datetime.now(TZ))),
        name="feiyu-deepseek-reconciliation-2h")
    owner._deepseek_reconcile_task = task
    tasks = getattr(owner, "tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return {"state": "scheduled"}
