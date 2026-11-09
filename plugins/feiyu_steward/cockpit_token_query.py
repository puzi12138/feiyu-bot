"""Fixed calendar-period reports from the retained local Codex request ledger.

This adapter projects numeric usage only. It does not call QQ, a model, a
browser or the network, and it never reads the legacy report queues.
"""

import re
import math
from datetime import datetime, timedelta

from .cockpit_token_report import (
    MODE,
    build_cockpit_only_report,
    project_cockpit_summary,
    report_readiness,
)
from .codex_token_usage import _cockpit_usage_between
from .token_usage_store import LOCAL_TIMEZONE
from .bill_display_policy import PROJECT_SCOPE_LABEL

PERIODS = ("today", "yesterday", "last7", "day", "window", "range")
SCOPE_LABEL = PROJECT_SCOPE_LABEL


def unsupported_period():
    return {
        "ok": False,
        "sent": False,
        "queued": False,
        "status": "unsupported_period",
        "report_mode": MODE,
        "source": "cockpit_request_ledger",
        "supported_periods": list(PERIODS),
        "note": "可以查询今天、昨天、近7天、指定自然日，或最近1分钟至30天；也可以指定带日期的起止时刻。",
    }


def unavailable(status="cockpit_usage_unavailable"):
    notes = {
        "window_too_short": "当前统计时段不足1分钟，请稍后再查。",
        "no_records": "这个时段没有可用的本机请求记录。",
    }
    return {
        "ok": False,
        "sent": False,
        "queued": False,
        "status": status,
        "report_mode": MODE,
        "source": "cockpit_request_ledger",
        "note": notes.get(status, "本机用量记录暂时不可用，请稍后再查。"),
    }


def _period_bounds(period, day, now):
    """Return aware UTC+8 boundaries; the end is always exclusive."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_required")
    current = now.astimezone(LOCAL_TIMEZONE).replace(microsecond=0)
    midnight = current.replace(hour=0, minute=0, second=0)
    if period == "today":
        begin, end = midnight, current
    elif period == "yesterday":
        begin, end = midnight - timedelta(days=1), midnight
    elif period == "last7":
        begin, end = midnight - timedelta(days=6), current
    elif period == "day":
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("unsupported_period")
        begin = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TIMEZONE)
        if begin > midnight:
            raise ValueError("unsupported_period")
        end = current if begin == midnight else begin + timedelta(days=1)
    else:
        raise ValueError("unsupported_period")
    return begin, end, current


def query_period(period="today", day="", *, now=None, source=None, hours=0, minutes=0, start="", end=""):
    """Read one fixed period from the local .codex ledger.

    ``source`` is an internal injection point for a local reader; normal
    callers leave it unset. ``now`` must be timezone-aware when supplied.
    A request for today or the last seven days ends at the captured second;
    a completed natural day ends at the next midnight.
    """
    try:
        current = now or datetime.now(LOCAL_TIMEZONE)
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            return unsupported_period()
        current = current.astimezone(LOCAL_TIMEZONE).replace(microsecond=0)
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (hours, minutes)):
            return unsupported_period()
        if period == "window":
            if day or start or end or (hours > 0) == (minutes > 0) or hours < 0 or minutes < 0:
                return unsupported_period()
            seconds = hours * 3600 if hours > 0 else minutes * 60
            if not 60 <= seconds <= 30 * 86400 or seconds != int(seconds):
                return unsupported_period()
            finish = current
            begin = finish - timedelta(seconds=seconds)
        elif period == "range":
            if day or hours or minutes or not isinstance(start, str) or not isinstance(end, str) or max(len(start), len(end)) > 48:
                return unsupported_period()
            begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if any(value.tzinfo is None or value.utcoffset() is None for value in (begin, finish)):
                return unsupported_period()
            if begin.microsecond or finish.microsecond:
                return unsupported_period()
            begin, finish = begin.astimezone(LOCAL_TIMEZONE), finish.astimezone(LOCAL_TIMEZONE)
            if begin.microsecond or finish.microsecond:
                return unsupported_period()
            if finish > current or not 60 <= (finish - begin).total_seconds() <= 30 * 86400:
                return unsupported_period()
        else:
            if hours or minutes or start or end:
                return unsupported_period()
            if period != "day" and day:
                return unsupported_period()
            begin, finish, current = _period_bounds(period, day, current)
        seconds = (finish - begin).total_seconds()
        if seconds < 60:
            return unavailable("window_too_short")
        interval = {
            "window_hours": seconds / 3600,
            "from": begin.isoformat(timespec="seconds"),
            "until": finish.isoformat(timespec="seconds"),
        }
        raw = _cockpit_usage_between(
            int(begin.timestamp() * 1000),
            int(finish.timestamp() * 1000),
            interval,
            source=source,
            now=current.timestamp(),
        )
        if not isinstance(raw, dict) or raw.get("ok") is not True:
            return unavailable()
        if raw.get("not_observed") is True:
            return unavailable("no_records")
        summary = project_cockpit_summary(raw)
        if not report_readiness(summary)["ready"]:
            return unavailable()
        if summary.get("totals", {}).get("calls") == 0:
            return unavailable("no_records")
        summary["observed_at"] = current.isoformat(timespec="seconds")
        summary["period"] = period
        summary["scope_label"] = SCOPE_LABEL
        return {
            "ok": True,
            "sent": False,
            "queued": False,
            "report_mode": MODE,
            "source": "cockpit_request_ledger",
            "summary": summary,
            "text": build_cockpit_only_report(summary),
            "timezone": "Asia/Shanghai",
            "observed_at": summary["observed_at"],
            "from": interval["from"],
            "until": interval["until"],
            "scope_label": SCOPE_LABEL,
        }
    except (ValueError, TypeError, OverflowError, OSError):
        return unsupported_period()
