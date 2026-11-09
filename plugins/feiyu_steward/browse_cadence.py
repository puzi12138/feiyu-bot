"""Cadence for cron platform discovery, independent of native and manual reads.

Only trusted automatic discovery should call ``claim``. Native feed work has its
own scheduler and timestamp; direct owner requests bypass this helper.
It updates the supplied state synchronously, before the caller performs async
work. The caller must save when ``state_changed`` is true, including a denied
claim that repaired a future timestamp after the system clock moved backward.
"""

import math
import time
from datetime import datetime, timedelta, timezone

INTERVAL_SECONDS = {"heybox": 0, "bilibili": 27000}
HEYBOX_DAYTIME_INTERVAL_SECONDS = 0
HEYBOX_DAYTIME_START_HOUR = 8
HEYBOX_DAYTIME_END_HOUR = 23
CADENCE_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
POLICY_VERSION = 3
_ALIASES = {
    "heybox": "heybox",
    "xiaoheihe": "heybox",
    "小黑盒": "heybox",
    "bilibili": "bilibili",
    "bili": "bilibili",
    "b站": "bilibili",
}


def canonical_source(source: str) -> str | None:
    """Return the supported platform name, without guessing unknown sources."""
    return _ALIASES.get(source.strip().lower()) if isinstance(source, str) else None


def _timestamp(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _now(now) -> float:
    value = _timestamp(time.time() if now is None else now)
    if value is None:
        raise ValueError("now must be a finite positive Unix timestamp")
    return value


def browse_interval_seconds(source: str, now=None) -> int:
    """Current local-time cadence; owner requests never use this automatic gate.

    Xiaoheihe has no owner-imposed browsing cooldown. Its native scheduler and
    network pacing still prevent idle spinning; ordinary tool reads are free.
    Bilibili and the caller's availability rules keep their existing policy.
    """
    canonical = canonical_source(source)
    if canonical is None:
        raise ValueError("unsupported automatic browsing source")
    current = _now(now)
    hour = datetime.fromtimestamp(current, CADENCE_TZ).hour
    if (canonical == "heybox"
            and HEYBOX_DAYTIME_START_HOUR <= hour < HEYBOX_DAYTIME_END_HOUR):
        return HEYBOX_DAYTIME_INTERVAL_SECONDS
    return INTERVAL_SECONDS[canonical]


def _last_at(state: dict, source: str) -> float | None:
    policy = state.get("automatic_browsing")
    sources = policy.get("sources") if isinstance(policy, dict) else None
    record = sources.get(source) if isinstance(sources, dict) else None
    last_at = _timestamp(record.get("last_at")) if isinstance(record, dict) else None
    if last_at is None and source == "heybox":
        last_at = _timestamp(state.get("heybox_last_comment_check"))
    return last_at


def _status(state: dict, source: str, now: float) -> dict:
    interval = browse_interval_seconds(source, now)
    previous = _last_at(state, source)
    # After a clock rollback, wait at most one fresh interval. A claim persists
    # this clamp, so later calls count down instead of extending the wait.
    last_at = min(previous, now) if previous is not None else None
    next_at = last_at + interval if last_at is not None else now
    remaining = max(0.0, next_at - now)
    return {
        "interval_seconds": interval,
        "due": remaining == 0,
        "last_at": last_at,
        "next_at": next_at,
        "remaining_seconds": remaining,
    }


def automatic_browsing_status(state: dict, now=None) -> dict:
    """Read-only due status for each canonical source; manual caches are ignored."""
    current = _now(now)
    return {source: _status(state, source, current) for source in INTERVAL_SECONDS}


def _record(state: dict, source: str, last_at: float, interval: int) -> bool:
    previous = state.get("automatic_browsing")
    policy = dict(previous) if isinstance(previous, dict) else {}
    old_sources = policy.get("sources")
    sources = dict(old_sources) if isinstance(old_sources, dict) else {}
    old_record = sources.get(source)
    record = dict(old_record) if isinstance(old_record, dict) else {}
    record.update(interval_seconds=interval, last_at=last_at)
    sources[source] = record
    policy.update(policy_version=POLICY_VERSION, sources=sources)
    if policy == previous:
        return False
    state["automatic_browsing"] = policy
    return True


def claim_automatic_browse(state: dict, source: str, now=None) -> dict:
    """Reserve one discovery attempt; no await occurs between check and stamp.

    Cron discovery entry points share this state in the same event loop.
    Failed network work still consumes the attempt, preventing rapid retries.
    This helper does not implement a lock across processes or independent states.
    """
    source = canonical_source(source)
    if source is None:
        return {"ok": False, "reason": "unsupported_source", "state_changed": False}
    current = _now(now)
    status = _status(state, source, current)
    if not status["due"]:
        # Seed the legacy native-feed timestamp and repair future values once.
        changed = _record(state, source, status["last_at"], status["interval_seconds"])
        return {
            "ok": False,
            "reason": "automatic_browse_cooldown",
            "source": source,
            "state_changed": changed,
            **status,
        }
    changed = _record(state, source, current, status["interval_seconds"])
    return {
        "ok": True,
        "reason": "claimed",
        "source": source,
        "state_changed": changed,
        **_status(state, source, current),
    }


def claim_native_heybox_browse(state: dict, now=None) -> dict:
    """Keep the native feed on its own cadence, including across reloads."""
    current = _now(now)
    interval = browse_interval_seconds("heybox", current)
    previous = _timestamp(state.get("heybox_last_comment_check"))
    last_at = min(previous, current) if previous is not None else None
    remaining = max(0.0, last_at + interval - current) if last_at is not None else 0
    if remaining:
        changed = last_at != previous
        if changed:
            state["heybox_last_comment_check"] = last_at
        return {"ok": False, "state_changed": changed, "remaining_seconds": remaining,
                "interval_seconds": interval}
    state["heybox_last_comment_check"] = current
    return {"ok": True, "state_changed": True, "remaining_seconds": 0,
            "interval_seconds": interval}
