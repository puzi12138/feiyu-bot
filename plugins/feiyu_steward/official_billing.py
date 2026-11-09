"""Read verified official daily snapshots without exposing keys or inventing hours.

The local ledger is an observation log, not the account's official bill.  This
module keeps those sources separate.  The public reconciliation reports expose
natural-day aggregates (including verified full days from hourly CSVs); those
aggregates cannot resolve a rolling-hour window that cuts through a day.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path("D:/QQBot")
REPORTS = ROOT / "data/token-usage/reconciliation-reports"
IMPORTS = ROOT / "data/token-usage/official-imports"
TZ = timezone(timedelta(hours=8))
SCHEMA = "deepseek-daily-20261026-v1"
NAME = re.compile(r"([a-f0-9]{64})\.json\Z")
MONEY = re.compile(r"\d+(?:\.\d+)?\Z")
MAX_REPORT = 2 * 1024 * 1024
MAX_SOURCE = 20 * 1024 * 1024
MAX_COUNT = 10**12


def _stamp(value):
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and len(value) <= 48:
        stamp = datetime.fromisoformat(value)
    elif type(value) in (int, float) and math.isfinite(value):
        stamp = datetime.fromtimestamp(value, TZ)
    else:
        raise ValueError("Invalid billing time")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Billing time must have a timezone")
    return stamp.astimezone(TZ)


def _linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _money(value):
    if not isinstance(value, str) or len(value) > 64 or not MONEY.fullmatch(value):
        raise ValueError("Invalid billing amount")
    number = Decimal(value)
    if not number.is_finite() or not 0 <= number <= MAX_COUNT:
        raise ValueError("Invalid billing amount")
    return number


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ValueError("Invalid billing count")
    return value


def _source_matches(directory, digest):
    # Bind the public report to the original validated file.  The bytes are
    # hashed only; no API key, account or CSV cell is decoded here.
    if _linked(directory) or not directory.is_dir():
        return False
    for suffix in (".zip", ".csv"):
        source = directory / (digest + suffix)
        if _linked(source) or not source.is_file() or not 0 < source.stat().st_size <= MAX_SOURCE:
            continue
        if hashlib.sha256(source.read_bytes()).hexdigest() == digest:
            return True
    return False


def _day(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("official"), dict):
        raise ValueError("Missing official day")
    start, end = _stamp(raw.get("start")), _stamp(raw.get("end"))
    if start.time() != datetime.min.time() or end - start != timedelta(days=1):
        raise ValueError("Unsupported official period")
    official = raw["official"]
    totals = official.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("Missing official totals")
    counts = {field: _count(totals.get(field)) for field in (
        "input_other", "input_cached", "input_total", "output", "total", "request_count", "function_calls")}
    if counts["input_other"] + counts["input_cached"] != counts["input_total"] or counts["input_total"] + counts["output"] != counts["total"]:
        raise ValueError("Inconsistent official token total")
    costs = {field: _money(official.get(field)) for field in ("cost_cny", "model_cost_cny", "tool_cost_cny", "detail_cost_cny")}
    if costs["cost_cny"] != costs["model_cost_cny"] + costs["tool_cost_cny"] or costs["cost_cny"] != costs["detail_cost_cny"]:
        raise ValueError("Inconsistent official cost")
    return {"start": start.isoformat(), "end": end.isoformat(),
            "total_tokens": counts["total"], "request_count": counts["request_count"],
            "tool_calls": counts["function_calls"],
            **{field: counts[field] for field in ("input_other", "input_cached", "input_total", "output")},
            **{field: format(costs[field], "f") for field in ("cost_cny", "model_cost_cny", "tool_cost_cny")}}


def _snapshot(path, imports, now):
    match = NAME.fullmatch(path.name)
    if not match or _linked(path) or not path.is_file() or not 0 < path.stat().st_size <= MAX_REPORT:
        raise ValueError("Invalid official report")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    digest = match.group(1)
    if not isinstance(envelope, dict) or envelope.get("report_id") != digest or not _source_matches(imports, digest):
        raise ValueError("Official source is not verified")
    raw = envelope.get("report")
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA or raw.get("status") not in ("matched", "difference", "history_incomplete"):
        raise ValueError("Unsupported official report")
    scope, official = raw.get("scope"), raw.get("official")
    if not isinstance(scope, dict) or not isinstance(official, dict):
        raise ValueError("Missing official scope")
    # Incomplete current-day hourly exports are useful for diagnostics only.
    # They cannot displace a complete source when choosing a report total.
    if official.get("coverage", "daily_export") == "partial_current_day_hourly":
        raise ValueError("Incomplete hourly coverage")
    if (scope.get("timezone") != "UTC+08:00" or official.get("timezone") != "UTC+08:00"
            or official.get("currency") != "CNY" or official.get("cost_crosscheck") != "consistent"
            or scope.get("official_interval_semantics") != "daily_start_inclusive_end_exclusive"):
        raise ValueError("Unverified official scope")
    exported = _stamp(scope.get("exported_at"))
    # The export is a historical observation. Future timestamps are not trusted
    # for selection; a generated_at rerun never makes an old snapshot newer.
    if exported > now:
        raise ValueError("Future official snapshot")
    match_scope = scope.get("key_match")
    if not isinstance(match_scope, dict) or match_scope.get("status") not in ("exact", "partial", "unknown", "unmatched"):
        raise ValueError("Missing key scope")
    key_filter = scope.get("key_filter")
    if key_filter not in ("all", "filtered", "unknown"):
        raise ValueError("Missing key filter")
    days_raw = raw.get("days")
    if not isinstance(days_raw, list) or not 0 < len(days_raw) <= 3660:
        raise ValueError("Invalid official days")
    days = sorted((_day(day) for day in days_raw), key=lambda day: day["start"])
    for index, day in enumerate(days):
        if _stamp(day["start"]) > exported:
            raise ValueError("Future official day")
        if index and _stamp(days[index - 1]["end"]) > _stamp(day["start"]):
            raise ValueError("Overlapping official days")
    history = _stamp(scope["local_history_start"]) if scope.get("local_history_start") else None
    return {"exported": exported, "days": days, "key_match": match_scope["status"],
            "key_filter": key_filter, "history": history}


def _sum_days(days, start, end):
    return {"start": start.isoformat(), "end": end.isoformat(), "complete": True,
            **{field: sum(day[field] for day in days) for field in ("total_tokens", "request_count", "tool_calls")},
            **{field: sum(day[field] for day in days) for field in ("input_other", "input_cached", "input_total", "output")},
            **{field: format(sum((Decimal(day[field]) for day in days), Decimal(0)), "f")
               for field in ("cost_cny", "model_cost_cny", "tool_cost_cny")}}


def official_summary_for_window(start, end, *, reports_dir=None, imports_dir=None, now=None):
    """Return safe official-account evidence, keeping daily and hourly scopes apart.

    ``exact_window`` exists only for complete contiguous natural-day buckets.
    It is still account-scope data; masked partial key matches never establish
    that one bot exclusively incurred the account's official cost.
    """
    try:
        begin, finish = _stamp(start), _stamp(end)
        current = _stamp(now) if now is not None else datetime.now(TZ)
        if begin >= finish or finish > current + timedelta(minutes=5):
            raise ValueError("Invalid requested period")
    except (ValueError, TypeError, OverflowError, OSError):
        return {"available": False, "reason": "invalid_requested_window", "exact_window": None}
    directory = Path(reports_dir) if reports_dir is not None else REPORTS
    imports = Path(imports_dir) if imports_dir is not None else IMPORTS
    candidates = []
    try:
        if _linked(directory) or not directory.is_dir():
            raise ValueError("Official reports unavailable")
        for path in directory.iterdir():
            if not NAME.fullmatch(path.name):
                continue
            try:
                candidates.append(_snapshot(path, imports, current))
            except (ValueError, TypeError, KeyError, OSError, OverflowError, InvalidOperation, RecursionError):
                continue
    except (OSError, ValueError):
        candidates = []
    if not candidates:
        return {"available": False, "reason": "official_snapshot_unavailable", "exact_window": None}
    # Prefer one snapshot that covers the actual request. A newer Today-only
    # export must not hide yesterday's verified complete bill. Never add exports.
    eligible = candidates
    if begin.time() == datetime.min.time() and finish.time() == datetime.min.time():
        covering = []
        for item in candidates:
            cursor = begin
            selected = [day for day in item["days"]
                        if begin <= _stamp(day["start"]) and _stamp(day["end"]) <= finish]
            for day in selected:
                if _stamp(day["start"]) != cursor:
                    break
                cursor = _stamp(day["end"])
            else:
                if selected and cursor == finish and finish <= item["exported"] - timedelta(minutes=5):
                    covering.append(item)
        if covering:
            eligible = covering
    latest = max(eligible, key=lambda item: item["exported"])
    if any(item["exported"] == latest["exported"] and item != latest for item in eligible):
        return {"available": False, "reason": "official_snapshot_ambiguous", "exact_window": None}
    exported, days = latest["exported"], latest["days"]
    complete_before = exported - timedelta(minutes=5)
    completed = [day for day in days if _stamp(day["end"]) <= complete_before]
    present = next((day for day in days if _stamp(day["start"]) <= exported < _stamp(day["end"])), None)
    exact, reason = None, "official_daily_granularity"
    if begin.time() == datetime.min.time() and finish.time() == datetime.min.time():
        selected = [day for day in days if begin <= _stamp(day["start"]) and _stamp(day["end"]) <= finish]
        cursor = begin
        for day in selected:
            if _stamp(day["start"]) != cursor:
                break
            cursor = _stamp(day["end"])
        else:
            if selected and cursor == finish and finish <= complete_before:
                exact, reason = _sum_days(selected, begin, finish), None
        if exact is None:
            reason = "official_window_not_fully_exported"
    history = latest["history"]
    return {"available": True, "source": "official_daily_csv", "source_granularity": "daily_aggregate", "snapshot_at": exported.isoformat(),
            "snapshot_age_seconds": max(0, int((current - exported).total_seconds())),
            "snapshot_stale": current - exported > timedelta(hours=24),
            "requested_window": {"start": begin.isoformat(), "end": finish.isoformat(),
                                 "exact": exact is not None, "reason": reason},
            "exact_window": exact,
            "last_complete_day": {**completed[-1], "complete": True} if completed else None,
            "current_day": {**present, "as_of": exported.isoformat(), "complete": False} if present else None,
            "scope": {"label": "官网账号用量", "key_match": latest["key_match"], "key_filter": latest["key_filter"]},
            "local_history_start": history.isoformat() if history else None,
            "local_history_incomplete": history is None or begin < history,
            "official_data_lag_minutes": 5,
            "limitations": ["daily_buckets_cannot_resolve_rolling_hours", "account_scope_is_not_exclusive_bot_usage"]}


def _display_day(raw, *, complete):
    """A fixed public formatter accepts counts, never supplied prose/labels."""
    if not isinstance(raw, dict) or raw.get("complete") is not complete:
        raise ValueError("Missing official completion evidence")
    start, end = _stamp(raw.get("start")), _stamp(raw.get("end"))
    if start >= end or start.time() != datetime.min.time() or end.time() != datetime.min.time():
        raise ValueError("Unsupported public daily interval")
    costs = {field: _money(raw.get(field)) for field in ("cost_cny", "model_cost_cny", "tool_cost_cny")}
    if costs["cost_cny"] != costs["model_cost_cny"] + costs["tool_cost_cny"]:
        raise ValueError("Inconsistent public official costs")
    return {"start": start, "end": end, "total_tokens": _count(raw.get("total_tokens")), **costs}


def _display_money(number):
    # Keep meaningful tiny tool fees while avoiding a wall of decimal zeros.
    return format(number.quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".") or "0"


def primary_billing_for_summary(summary):
    """Choose one cost for the requested period; a subtotal is never a total."""
    unknown = {"status": "unverified", "label": "人民币费用", "cost_cny": None}
    if not isinstance(summary, dict) or summary.get("ok") is not True:
        return unknown
    official = summary.get("official_billing")
    try:
        if not isinstance(official, dict) or official.get("available") is not True or official.get("source") != "official_daily_csv":
            raise ValueError("No official period")
        requested = official.get("requested_window")
        if not isinstance(requested, dict) or requested.get("exact") is not True:
            raise ValueError("Unconfirmed official period")
        if "window_hours" in summary:
            begin, finish = _stamp(summary.get("from")), _stamp(summary.get("until"))
        else:
            day = summary.get("day")
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError("Missing summary day")
            begin = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
            finish = begin + timedelta(days=1)
        exact = _display_day(official.get("exact_window"), complete=True)
        snapshot = _stamp(official.get("snapshot_at"))
        scope = official.get("scope")
        if (not isinstance(scope, dict) or scope.get("key_match") not in ("exact", "partial", "unknown", "unmatched")
                or scope.get("key_filter") not in ("all", "filtered", "unknown")
                or begin >= finish or exact["start"] != begin or exact["end"] != finish
                or _stamp(requested.get("start")) != begin or _stamp(requested.get("end")) != finish
                or finish > snapshot - timedelta(minutes=5)):
            raise ValueError("Official period does not match report")
        return {"status": "confirmed", "label": "官网核实费用",
                "cost_cny": format(exact["cost_cny"], "f"), "scope_label": "官网账号口径"}
    except (ValueError, TypeError, OverflowError, OSError, InvalidOperation):
        pass
    try:
        totals = summary.get("totals")
        pricing = summary.get("pricing")
        if (summary.get("not_observed") or summary.get("partial_day") is not False
                or not isinstance(totals, dict) or _count(totals.get("unknown_usage_calls")) != 0
                or not isinstance(pricing, dict) or pricing.get("complete") is not True):
            return unknown
        calls = _count(totals.get("calls"))
        if (_count(pricing.get("unpriced_calls")) != 0
                or _count(pricing.get("priced_calls")) != calls):
            return unknown
        if "window_hours" in summary:
            begin, finish = _stamp(summary.get("from")), _stamp(summary.get("until"))
        else:
            day = summary.get("day")
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return unknown
            begin = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
            finish = begin + timedelta(days=1)
        if (begin >= finish or _stamp(summary.get("observed_since")) > begin
                or finish > datetime.now(TZ)):
            return unknown
        amount = pricing.get("total_cny")
        known = pricing.get("known_cny")
        if type(amount) not in (int, float) or not math.isfinite(amount) or not 0 <= amount <= MAX_COUNT:
            return unknown
        if type(known) not in (int, float) or not math.isfinite(known) or known != amount:
            return unknown
        return {"status": "estimated", "label": "官方价估算", "cost_cny": format(Decimal(str(amount)), "f")}
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        return unknown


def primary_token_usage_for_summary(summary):
    """Select one headline token source, keeping local classifications local.

    The same exact-window validation as the monetary headline gates official
    tokens. Older verified summaries may contain only an official total; their
    local composition is retained with its own denominator and source label.
    """
    result = {"source": "unavailable", "total_tokens": None,
              "composition_source": "unavailable", "composition_total_tokens": None,
              "input_other": None, "input_cached": None, "output": None}
    if not isinstance(summary, dict) or summary.get("ok") is not True:
        return result
    local = summary.get("totals")
    try:
        if not isinstance(local, dict) or summary.get("not_observed"):
            raise ValueError("No local counts")
        parts = {field: _count(local.get(field)) for field in ("input_other", "input_cached", "output")}
        total = _count(local.get("total"))
        if sum(parts.values()) != total:
            raise ValueError("Local count arithmetic")
        result.update(source="local", total_tokens=total,
                      composition_source="local", composition_total_tokens=total, **parts)
    except (ValueError, TypeError):
        pass
    if primary_billing_for_summary(summary)["status"] != "confirmed":
        return result
    try:
        exact = summary["official_billing"]["exact_window"]
        total = _count(exact.get("total_tokens"))
        result.update(source="official", total_tokens=total)
        parts = {field: _count(exact.get(field)) for field in ("input_other", "input_cached", "output")}
        if sum(parts.values()) != total:
            raise ValueError("Official count arithmetic")
        if "input_total" in exact and _count(exact["input_total"]) != parts["input_other"] + parts["input_cached"]:
            raise ValueError("Official input count arithmetic")
        result.update(composition_source="official", composition_total_tokens=total, **parts)
    except (KeyError, ValueError, TypeError):
        # Valid official total remains usable; arbitrary or missing optional
        # components cannot replace a verified local composition.
        pass
    return result


def format_official_billing_summary(value):
    """Render the fixed safe source labels used by text-only usage replies."""
    unavailable = "官网账单：暂无可核验快照，完整费用待核实。"
    if not isinstance(value, dict) or value.get("available") is not True or value.get("source") != "official_daily_csv":
        return unavailable
    try:
        snapshot = _stamp(value.get("snapshot_at"))
        scope = value.get("scope")
        if not isinstance(scope, dict) or scope.get("key_match") not in ("exact", "partial", "unknown", "unmatched"):
            return unavailable
        lines = ["官网账号用量（UTC+8），快照：" + snapshot.strftime("%Y-%m-%d %H:%M")]
        exact = value.get("exact_window")
        confirmed_exact = False
        if exact is not None:
            day = _display_day(exact, complete=True)
            requested = value.get("requested_window")
            confirmed_exact = (isinstance(requested, dict) and requested.get("exact") is True
                               and _stamp(requested.get("start")) == day["start"]
                               and _stamp(requested.get("end")) == day["end"] <= snapshot - timedelta(minutes=5))
            if confirmed_exact:
                lines.append("官网同一自然日范围费用：¥" + _display_money(day["cost_cny"]) +
                             "；模型 ¥" + _display_money(day["model_cost_cny"]) +
                             "，工具 ¥" + _display_money(day["tool_cost_cny"]) +
                             f"；{day['total_tokens']:,} token。")
        if not confirmed_exact:
            lines.append("当前官网核对数据按自然日汇总，无法精确还原这段滚动时窗；本机已记录费用不等于完整账单。")
        raw_complete = value.get("last_complete_day")
        if raw_complete is not None:
            day = _display_day(raw_complete, complete=True)
            if day["end"] > snapshot - timedelta(minutes=5) or day["end"] - day["start"] != timedelta(days=1):
                raise ValueError("Day is not complete at this snapshot")
            lines.append("最近完整自然日 " + day["start"].strftime("%Y-%m-%d") +
                         "：官网费用 ¥" + _display_money(day["cost_cny"]) +
                         "（模型 ¥" + _display_money(day["model_cost_cny"]) +
                         "，工具 ¥" + _display_money(day["tool_cost_cny"]) +
                         f"），{day['total_tokens']:,} token。")
        raw_current = value.get("current_day")
        if raw_current is not None:
            day = _display_day(raw_current, complete=False)
            if not day["start"] <= snapshot < day["end"] or _stamp(raw_current.get("as_of")) != snapshot:
                raise ValueError("Unverified partial-day cutoff")
            lines.append("当日截至 " + snapshot.strftime("%H:%M") + " 的官网费用：¥" +
                         _display_money(day["cost_cny"]) + "，数据可能延迟约 5 分钟。")
        if scope["key_match"] != "exact":
            lines.append("官网 Key 身份未完全核验，以上为账号用量，不能精确归为肥鱼独用。")
        if value.get("local_history_incomplete") is True and value.get("local_history_start"):
            history = _stamp(value["local_history_start"])
            lines.append("本机完整观测从 " + history.strftime("%Y-%m-%d %H:%M") + " 开始，早期缺失不补算。")
        if value.get("snapshot_stale") is True:
            lines.append("该官网快照超过 24 小时，等待下一次成功导出更新。")
        return "\n".join(lines)
    except (ValueError, TypeError, OverflowError, OSError, InvalidOperation):
        return unavailable
