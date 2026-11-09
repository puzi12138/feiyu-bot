"""Read and price retained Feiyu feature records without contacting any service."""
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .cockpit_token_query import _period_bounds
from .feature_reconciliation import project_reconciliation, reconciliation_text
from .token_pricing import price_feiyu_rows
from .token_usage_store import (COUNT_SQL, FEATURE_LABELS, FEATURES, LOCAL_TIMEZONE,
                                _counts, _linked, _resolved_path)

MODE = "feiyu_feature_ledger_v1"
PERIODS = ("today", "yesterday", "last7", "day", "window", "range")
SOURCE = "feiyu_local_feature_ledger"
GROUPS = {
    "bili": ("bili_summary", "bili_comment", "bili_other", "bili_vision"),
    "heybox": ("heybox_comment", "heybox_vision"),
    "qzone": ("qzone_draft", "qzone_reply"),
    "qq_chat": ("private_chat", "group_chat"),
}


def unsupported_period():
    return {"ok": False, "sent": False, "queued": False, "status": "unsupported_period",
            "report_mode": MODE, "source": SOURCE,
            "note": "本机功能明细可查今天、昨天、近7天、指定自然日、最近1分钟至30天，或带日期的起止时刻。"}


def _unavailable(status="local_feature_usage_unavailable"):
    return {"ok": False, "sent": False, "queued": False, "status": status,
            "report_mode": MODE, "source": SOURCE,
            "note": "本机功能记录暂时不可用，请稍后再查。"}


def _selected(feature):
    if not isinstance(feature, str) or len(feature) > 1000:
        raise ValueError("invalid_feature")
    if not feature:
        return None
    parts = feature.split(",")
    if len(parts) > len(FEATURES) or any(not part or part not in FEATURES | GROUPS.keys() for part in parts):
        raise ValueError("invalid_feature")
    selected = set()
    for part in parts:
        selected.update(GROUPS.get(part, (part,)))
    return selected


def _bounds(period, day, *, now, hours, minutes, start, end):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_required")
    current = now.astimezone(LOCAL_TIMEZONE).replace(microsecond=0)
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (hours, minutes)):
        raise ValueError("invalid_window")
    if period == "window":
        if day or start or end or (hours > 0) == (minutes > 0) or hours < 0 or minutes < 0:
            raise ValueError("invalid_window")
        seconds = hours * 3600 if hours > 0 else minutes * 60
        if not 60 <= seconds <= 30 * 86400 or seconds != int(seconds):
            raise ValueError("invalid_window")
        begin, finish = current - timedelta(seconds=seconds), current
    elif period == "range":
        if day or hours or minutes or not isinstance(start, str) or not isinstance(end, str) or max(len(start), len(end)) > 48:
            raise ValueError("invalid_window")
        begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if any(value.tzinfo is None or value.utcoffset() is None or value.microsecond for value in (begin, finish)):
            raise ValueError("invalid_window")
        begin, finish = begin.astimezone(LOCAL_TIMEZONE), finish.astimezone(LOCAL_TIMEZONE)
        if finish > current or not 60 <= (finish - begin).total_seconds() <= 30 * 86400:
            raise ValueError("invalid_window")
    else:
        if hours or minutes or start or end or (period != "day" and day):
            raise ValueError("invalid_window")
        begin, finish, current = _period_bounds(period, day, current)
    if finish <= begin:
        raise ValueError("invalid_window")
    return begin, finish, current


def _connect_readonly(root):
    root = _resolved_path(Path(root))
    directory = root / "data" / "token-usage"
    path = directory / "usage.sqlite"
    for item in (root / "data", directory):
        if _linked(item) or not _resolved_path(item).is_relative_to(root):
            raise OSError("unsafe_usage_ledger_path")
    for item in (path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        if _linked(item) or (item.exists() and item.stat().st_nlink > 1):
            raise OSError("unsafe_usage_ledger_file")
    if not path.is_file():
        raise FileNotFoundError("usage_ledger_missing")
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only=ON")
    return database


def _price_projection(value):
    return {key: value[key] for key in ("known_cny", "priced_calls", "unpriced_calls", "price_date", "basis_label")}


def _money(value):
    return f"¥{value:,.6f}"


def _report(summary):
    totals = summary["totals"]
    lines = ["肥鱼功能用量（本机已记录）",
             f"时段：{summary['from']} 至 {summary['until']}（北京时间，结束时刻不含）"]
    if datetime.fromisoformat(summary['observed_since']) > datetime.fromisoformat(summary['from']):
        lines.append(f"本机记录起点：{summary['observed_since']}。")
    amount = (f"已知 {totals['total']:,} token" if totals['known_usage_calls'] or not totals['calls']
              else "用量待回传")
    lines.append(f"{amount}，{totals['known_usage_calls']:,} 次有用量；"
                 f"{totals['unknown_usage_calls']:,} 次未回传用量。")
    pricing = summary["pricing"]
    if pricing["priced_calls"]:
        lines.append(f"按官方单价 {_money(pricing['known_cny'])}"
                     f"（{pricing['priced_calls']:,} 次已计价，{pricing['unpriced_calls']:,} 次未计价）。")
    elif totals["calls"]:
        lines.append("已记录调用暂无法按现有价格计价。")
    lines.append("功能明细（只列有记录的功能）：")
    for row in sorted(summary["by_feature"], key=lambda x: (-x["total"], x["feature"])):
        price = row["pricing"]
        amount = (f"已知 {row['total']:,} token" if row['known_usage_calls'] or not row['calls']
                  else "用量待回传")
        line = (f"• {row['label']}：{amount}；"
                f"{row['known_usage_calls']:,} 次有用量，{row['unknown_usage_calls']:,} 次未回传用量")
        if price["priced_calls"]:
            line += (f"；已定价部分 {_money(price['known_cny'])}"
                     f"（{price['priced_calls']:,} 次已计价，{price['unpriced_calls']:,} 次未计价）")
        else:
            line += "；未能计价"
        lines.append(line + "。")
    if not summary["by_feature"]:
        lines.append("• 这个时段没有符合条件的本机记录。")
    lines.extend(reconciliation_text(summary.get("reconciliation")))
    if any(row["feature"] == "autonomous_plan" for row in summary["by_feature"]):
        lines.append("自主决策是综合任务，不能拆成各业务的用量。")
    return "\n".join(lines)


def query_period(root, period="today", day="", *, now=None, hours=0, minutes=0, start="", end="", feature=""):
    """Read an exclusive interval from the existing ledger using SQLite mode=ro."""
    try:
        selected = _selected(feature)
    except ValueError:
        return {**_unavailable("unsupported_feature"), "note": "无法识别该功能，请选择已有功能或查询全部功能明细。"}
    try:
        begin, finish, current = _bounds(period, day, now=now or datetime.now(LOCAL_TIMEZONE),
                                          hours=hours, minutes=minutes, start=start, end=end)
    except (ValueError, TypeError, OverflowError, OSError):
        return unsupported_period()
    bounds = (begin.timestamp(), finish.timestamp())
    try:
        with closing(_connect_readonly(root)) as database:
            database.execute("BEGIN")
            raw = database.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()
            observed = float(raw["value"])
            if not math.isfinite(observed) or observed < 0:
                raise ValueError("invalid_observed_since")
            groups = database.execute("SELECT feature," + COUNT_SQL + " FROM requests WHERE at>=? AND at<? "
                                      "GROUP BY feature ORDER BY feature", bounds).fetchall()
            source_rows = database.execute("SELECT at,feature,model,usage_known,input_other,input_cached,output "
                                           "FROM requests WHERE at>=? AND at<?", bounds).fetchall()
            database.commit()
        if any(row["feature"] not in FEATURES for row in groups):
            raise ValueError("invalid_feature_in_ledger")
        groups = [row for row in groups if selected is None or row["feature"] in selected]
        source_rows = [row for row in source_rows if selected is None or row["feature"] in selected]
        by_feature = []
        for row in groups:
            counts = _counts(row)
            if (counts["calls"] != counts["known_usage_calls"] + counts["unknown_usage_calls"]
                    or any(type(value) is not int or value < 0 for value in counts.values())):
                raise ValueError("invalid_usage_counts")
            key = row["feature"]
            pricing = price_feiyu_rows(item for item in source_rows if item["feature"] == key)
            by_feature.append({"feature": key, "label": FEATURE_LABELS[key], **counts,
                               "pricing": _price_projection(pricing)})
        total_fields = ("calls", "success", "failed", "cancelled", "known_usage_calls", "unknown_usage_calls",
                        "input_other", "input_cached", "output", "input_total", "total")
        totals = {key: sum(row[key] for row in by_feature) for key in total_fields}
        pricing = price_feiyu_rows(source_rows)
        if (pricing["priced_calls"] + pricing["unpriced_calls"] != totals["calls"] or
                any(row["pricing"]["priced_calls"] + row["pricing"]["unpriced_calls"] != row["calls"] for row in by_feature)):
            raise ValueError("invalid_pricing_counts")
        # Present the same six-decimal precision for every feature and total;
        # the displayed feature amounts therefore add up to the displayed sum.
        pricing["known_cny"] = float(sum((Decimal(str(row["pricing"]["known_cny"]))
                                          for row in by_feature), Decimal(0)))
        summary = {"period": period, "observed_at": current.isoformat(timespec="seconds"),
                   "from": begin.isoformat(timespec="seconds"), "until": finish.isoformat(timespec="seconds"),
                   "observed_since": datetime.fromtimestamp(observed, LOCAL_TIMEZONE).isoformat(timespec="seconds"),
                   "local_records_only": True, "coverage_complete": False,
                   "selected_features": sorted(selected) if selected is not None else None,
                   "totals": totals, "pricing": _price_projection(pricing), "by_feature": by_feature}
        summary["reconciliation"] = project_reconciliation(root, summary)
        return {"ok": True, "sent": False, "queued": False, "report_mode": MODE, "source": SOURCE,
                "summary": summary, "text": _report(summary), "timezone": "Asia/Shanghai"}
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, OverflowError):
        return _unavailable()


def report_ready(result):
    summary = result.get("summary") if isinstance(result, dict) else None
    return (isinstance(summary, dict) and result.get("report_mode") == MODE and result.get("source") == SOURCE
            and summary.get("period") in PERIODS and summary.get("local_records_only") is True
            and summary.get("coverage_complete") is False and isinstance(summary.get("observed_at"), str)
            and isinstance(result.get("text"), str) and bool(result["text"]))
