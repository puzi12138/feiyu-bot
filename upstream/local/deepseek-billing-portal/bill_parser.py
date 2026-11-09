"""Read official DeepSeek CSV exports without exposing account identities.

The two observed exports have different units: cost is CNY, amount is either
tokens, requests, or tool calls.  Reconciliation never modifies the local ledger.
Each call represents one official snapshot; overlapping snapshots are not added.
Hourly exports normally require all 24 explicit, validated cost/amount buckets
for each day. Trusted visible single-day scope can also establish a completed
official daily export after the daily cutoff and publication lag, even if its
CSV omits hours. Those rows are summed as exported, without fabricating hourly
zero usage. Today's verified selection retains partial, unknown-hour coverage.
"""
from __future__ import annotations

import csv
import hashlib
import io
import importlib.util
import json
import math
import re
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = "deepseek-daily-20261026-v1"
COST_COLUMNS = frozenset(("user_id", "start_time_iso", "end_time_iso", "model", "name", "wallet_type", "cost", "currency"))
AMOUNT_COLUMNS = frozenset(("user_id", "start_time_iso", "end_time_iso", "model", "name", "api_key_name", "api_key", "type", "price", "amount"))
TOKEN_TYPES = {"input_cache_miss_tokens": "input_other", "input_cache_hit_tokens": "input_cached", "output_tokens": "output"}
MAX_UPLOAD = 20 * 1024 * 1024
MAX_EXPANDED = 50 * 1024 * 1024
MAX_COUNT = 10**12
ZERO = Decimal(0)


class BillError(ValueError):
    """A deliberately fixed message; never include an offending cell or path."""


def _error():
    return BillError("Official billing schema or values are unsupported")


def _decimal(value, *, integer=False):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise _error()
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise _error() from None
    if not number.is_finite() or number < 0 or number > MAX_COUNT or (integer and number != number.to_integral_value()):
        raise _error()
    return int(number) if integer else number


def _iso(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise _error() from None
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(hours=8):
        raise _error()
    return stamp


def _period(row):
    start, end = _iso(row["start_time_iso"]), _iso(row["end_time_iso"])
    delta = end - start
    daily = start.time() == datetime.min.time() and delta == timedelta(days=1)
    hourly = start.minute == start.second == start.microsecond == 0 and delta == timedelta(hours=1)
    if not (daily or hourly):
        raise _error()
    return start.isoformat(), end.isoformat()


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _money(value):
    return format(value, "f")


def _empty_totals():
    return {"input_other": 0, "input_cached": 0, "input_total": 0, "output": 0, "total": 0, "request_count": 0, "function_calls": 0}


@dataclass
class ParsedBill:
    # Private cells only live in memory and are intentionally omitted by repr.
    costs: dict = field(default_factory=dict, repr=False)
    amounts: dict = field(default_factory=dict, repr=False)
    key_masks: set = field(default_factory=set, repr=False)
    accounts: set = field(default_factory=set, repr=False)
    csv_kinds: set = field(default_factory=set)
    unique_csv_count: int = 0
    duplicate_csv_count: int = 0
    period_seconds: int | None = None
    hourly_current_cutoff: datetime | None = field(default=None, repr=False)
    hourly_missing_starts: list = field(default_factory=list)
    hourly_completed_range: tuple | None = field(default=None, repr=False)
    hourly_unreported_starts: list = field(default_factory=list)


def _verified_official_day(metadata, now):
    """Read the fixed visible single-day selection, without assuming coverage."""
    if (not isinstance(metadata, dict) or metadata.get("source") != "official_visible_export"
            or metadata.get("timezone") != "UTC+08:00"):
        return None
    raw_range = metadata.get("date_range")
    if (not isinstance(raw_range, dict) or raw_range.get("status") != "verified"
            or type(raw_range.get("inclusive")) is not bool):
        return None
    try:
        exported = _iso(metadata.get("observed_at"))
        current = datetime.now(TZ) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None:
            return None
        current = current.astimezone(TZ)
        if any(not isinstance(raw_range.get(k), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_range[k])
               for k in ("start", "end")):
            return None
        start = datetime.strptime(raw_range.get("start"), "%Y-%m-%d").replace(tzinfo=TZ)
        end = datetime.strptime(raw_range.get("end"), "%Y-%m-%d").replace(tzinfo=TZ)
        if raw_range["inclusive"]:
            end += timedelta(days=1)
        if exported > current or end - start != timedelta(days=1):
            return None
        return start, end, exported, current
    except (BillError, ValueError, TypeError, OverflowError):
        return None


def _partial_hourly_exported_at(metadata, now):
    """Allow partial current hours only with a verified visible today filter."""
    verified = _verified_official_day(metadata, now)
    if verified is None:
        return None
    start, _, exported, current = verified
    if start.date() == exported.date() == current.date():
        return exported
    return None


def _completed_hourly_range(metadata, now):
    """Trust the whole selected official day, never absent per-hour zeros."""
    verified = _verified_official_day(metadata, now)
    if verified is None:
        return None
    start, end, exported, _ = verified
    if exported >= end + timedelta(minutes=5):
        return start, end
    return None


def _csv_sources(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_UPLOAD:
        raise _error()
    data = path.read_bytes()
    if path.suffix.lower() == ".csv":
        return [data]
    if path.suffix.lower() != ".zip":
        raise _error()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= 32 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise _error()
            result = []
            for info in infos:
                # Nothing is extracted, executed, or saved under member names.
                if info.is_dir():
                    continue
                if info.flag_bits & 1 or info.file_size > MAX_UPLOAD or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise _error()
                if info.filename.lower().endswith(".csv"):
                    member = archive.read(info)
                    if len(member) != info.file_size:
                        raise _error()
                    result.append(member)
                elif info.filename.rsplit("/", 1)[-1].lower() not in ("readme", "readme.txt", "readme.md"):
                    raise _error()
            return result
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        raise _error() from None


def parse_receipts(paths, *, metadata=None, now=None):
    """Parse one snapshot, deduplicating identical ZIP/CSV and semantic rows.

    If callers supply overlapping *different* snapshots, conflicting business
    keys are rejected. Scheduled callers should reconcile each newest receipt
    independently, keeping earlier reports as historical snapshots.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    bill, seen = ParsedBill(), set()
    try:
        for path in paths:
            for data in _csv_sources(path):
                if not data or b"\x00" in data:
                    raise _error()
                text = data.decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
                columns = reader.fieldnames
                if not columns or len(columns) != len(set(columns)):
                    raise _error()
                schema = frozenset(columns)
                kind = "cost" if schema == COST_COLUMNS else "amount" if schema == AMOUNT_COLUMNS else None
                if kind is None:
                    raise _error()
                rows = list(reader)
                if any(None in row or any(value is None for value in row.values()) for row in rows):
                    raise _error()
                # Canonicalization handles byte-identical and reordered copies.
                canonical = json.dumps([sorted(columns), sorted(tuple(row[k] for k in sorted(columns)) for row in rows)], separators=(",", ":"))
                digest = _hash(canonical)
                if digest in seen:
                    bill.duplicate_csv_count += 1
                    continue
                seen.add(digest)
                bill.unique_csv_count += 1
                bill.csv_kinds.add(kind)
                for row in rows:
                    if not row["user_id"] or len(row["user_id"]) > 256:
                        raise _error()
                    account = _hash(row["user_id"])
                    bill.accounts.add(account)
                    period = _period(row)
                    seconds = int((_iso(period[1]) - _iso(period[0])).total_seconds())
                    if bill.period_seconds is None:
                        bill.period_seconds = seconds
                    elif bill.period_seconds != seconds:
                        # A daily bucket and its hourly components overlap.
                        # They are separate snapshots, never additive evidence.
                        raise _error()
                    model, name = row["model"], row["name"]
                    if (model, name) not in (("deepseek-flash", "deepseek-flash"), ("", "tool-search"), ("", "tool-fetch")):
                        raise _error()
                    common = (account, *period, model, name)
                    if kind == "cost":
                        if row["currency"] != "CNY" or row["wallet_type"] not in ("Paid", "Granted"):
                            raise _error()
                        key = (*common, row["wallet_type"])
                        value, target = _decimal(row["cost"]), bill.costs
                    else:
                        if not row["api_key"] or len(row["api_key"]) > 256:
                            raise _error()
                        bill.key_masks.add(row["api_key"])
                        kind_type = row["type"]
                        if kind_type not in (*TOKEN_TYPES, "request_count", "function_call"):
                            raise _error()
                        if (model == "deepseek-flash") != (kind_type != "function_call"):
                            raise _error()
                        if kind_type == "request_count":
                            if row["price"]:
                                raise _error()
                            price = None
                        else:
                            price = _decimal(row["price"])
                        key = (*common, _hash(row["api_key"]), kind_type, price)
                        value, target = _decimal(row["amount"], integer=True), bill.amounts
                    if key in target and target[key] != value:
                        raise _error()
                    # Identical business keys never multiply billing totals.
                    target[key] = value
        if not bill.csv_kinds or len(bill.accounts) != 1:
            raise _error()
        if bill.period_seconds == 3600:
            current = datetime.now(TZ) if now is None else now
            if not isinstance(current, datetime) or current.tzinfo is None:
                raise _error()
            if any(_iso(bucket["start"]) > current for bucket in _bucket_data(bill)):
                raise _error()
            bill.hourly_current_cutoff = _partial_hourly_exported_at(metadata, now)
            bill.hourly_completed_range = _completed_hourly_range(metadata, now)
            # Fail closed before returning any partial natural-day estimate
            # without a verified current-day observation.
            _day_data(bill)
        return bill
    except (UnicodeError, csv.Error, KeyError, TypeError, OSError):
        raise _error() from None


def _bucket_data(bill):
    days = {}
    for key, amount in bill.amounts.items():
        _, start, end, model, name, _, kind, price = key
        day = days.setdefault(start, {"start": start, "end": end, "tokens": _empty_totals(), "cost": ZERO, "model_cost": ZERO, "tool_cost": ZERO, "detail_cost": ZERO, "rates": {}, "has_amount": False, "has_cost": False})
        day["has_amount"] = True
        target = TOKEN_TYPES.get(kind, "request_count" if kind == "request_count" else "function_calls")
        day["tokens"][target] += amount
        if price is not None:
            day["detail_cost"] += amount * price
        if kind in TOKEN_TYPES:
            day["rates"].setdefault(TOKEN_TYPES[kind], set()).add(price)
    for key, cost in bill.costs.items():
        _, start, end, model, _, _ = key
        day = days.setdefault(start, {"start": start, "end": end, "tokens": _empty_totals(), "cost": ZERO, "model_cost": ZERO, "tool_cost": ZERO, "detail_cost": ZERO, "rates": {}, "has_amount": False, "has_cost": False})
        day["has_cost"] = True
        day["cost"] += cost
        day["model_cost" if model else "tool_cost"] += cost
    for day in days.values():
        t = day["tokens"]
        t["input_total"] = t["input_other"] + t["input_cached"]
        t["total"] = t["input_total"] + t["output"]
    return [days[key] for key in sorted(days)]


def _day_data(bill):
    buckets = _bucket_data(bill)
    if bill.period_seconds != 3600:
        return buckets
    grouped = {}
    for bucket in buckets:
        start = _iso(bucket["start"])
        grouped.setdefault(start.date(), []).append(bucket)
    days = []
    bill.hourly_missing_starts = []
    bill.hourly_unreported_starts = []
    for date, hours in sorted(grouped.items()):
        midnight = datetime.combine(date, datetime.min.time(), TZ)
        cutoff = bill.hourly_current_cutoff
        if cutoff is not None:
            # The visible current-day selection must describe these exact
            # source rows; future buckets cannot be a partial snapshot.
            if len(grouped) != 1 or date != cutoff.date() or any(_iso(h["start"]) > cutoff for h in hours):
                raise _error()
            expected_starts = {midnight + timedelta(hours=i) for i in range(cutoff.hour + 1)}
            actual_starts = {_iso(h["start"]) for h in hours}
            bill.hourly_missing_starts = [s.isoformat() for s in sorted(expected_starts - actual_starts)]
        elif bill.hourly_completed_range is not None:
            selected_start, selected_end = bill.hourly_completed_range
            if (len(grouped) != 1 or midnight != selected_start
                    or any(not selected_start <= _iso(h["start"]) < _iso(h["end"]) <= selected_end for h in hours)):
                raise _error()
            expected_starts = {midnight + timedelta(hours=i) for i in range(24)}
            actual_starts = {_iso(h["start"]) for h in hours}
            bill.hourly_unreported_starts = [s.isoformat() for s in sorted(expected_starts - actual_starts)]
        elif len(hours) != 24:
            raise _error()
        day = {"start": midnight.isoformat(), "end": (midnight + timedelta(days=1)).isoformat(),
               "tokens": _empty_totals(), "cost": ZERO, "model_cost": ZERO, "tool_cost": ZERO,
               "detail_cost": ZERO, "rates": {}, "has_amount": True, "has_cost": True}
        for index, hour in enumerate(hours):
            expected = (_iso(hour["start"]) if cutoff is not None or bill.hourly_completed_range is not None
                        else midnight + timedelta(hours=index))
            if (_iso(hour["start"]) != expected or _iso(hour["end"]) != expected + timedelta(hours=1)
                    or not hour["has_amount"] or not hour["has_cost"]
                    or hour["cost"] != hour["detail_cost"]):
                raise _error()
            for field in day["tokens"]:
                day["tokens"][field] += hour["tokens"][field]
            for field in ("cost", "model_cost", "tool_cost", "detail_cost"):
                day[field] += hour[field]
            for field, rates in hour["rates"].items():
                day["rates"].setdefault(field, set()).update(rates)
        days.append(day)
    return days


def bill_summary(bill):
    days, totals = _day_data(bill), _empty_totals()
    for day in days:
        for name in totals:
            totals[name] += day["tokens"][name]
    return {
        "schema_version": SCHEMA_VERSION, "timezone": "UTC+08:00", "currency": "CNY",
        "source_granularity": "hourly" if bill.period_seconds == 3600 else "daily",
        "source_period_count": len(_bucket_data(bill)),
        "coverage": ("partial_current_day_hourly" if bill.hourly_current_cutoff is not None
                     else "daily_export" if bill.hourly_completed_range is not None
                     else "complete_hourly_days" if bill.period_seconds == 3600 else "daily_export"),
        "missing_hour_starts": list(bill.hourly_missing_starts),
        "unreported_hour_starts": list(bill.hourly_unreported_starts),
        "start": days[0]["start"] if days else None, "end": days[-1]["end"] if days else None,
        "day_count": len(days), "key_count": len(bill.key_masks),
        "contiguous_days": all(_iso(a["end"]) == _iso(b["start"]) for a, b in zip(days, days[1:])),
        "unique_csv_count": bill.unique_csv_count, "duplicate_csv_count": bill.duplicate_csv_count,
        "totals": totals, "cost_cny": _money(sum((d["cost"] for d in days), ZERO)) if "cost" in bill.csv_kinds else None,
        "model_cost_cny": _money(sum((d["model_cost"] for d in days), ZERO)) if "cost" in bill.csv_kinds else None,
        "tool_cost_cny": _money(sum((d["tool_cost"] for d in days), ZERO)) if "cost" in bill.csv_kinds else None,
        "detail_cost_cny": _money(sum((d["detail_cost"] for d in days), ZERO)) if "amount" in bill.csv_kinds else None,
        "cost_crosscheck": "consistent" if {"cost", "amount"} <= bill.csv_kinds and all(d["has_amount"] and d["has_cost"] and d["cost"] == d["detail_cost"] for d in days) else "unverified",
    }


def match_configured_keys(bill, config_path):
    """Compare in memory only. A masked prefix/suffix is never exact evidence."""
    result = {"status": "unknown", "official_keys": len(bill.key_masks), "configured_keys": 0, "exact_matches": 0, "partial_matches": 0, "unmatched_keys": len(bill.key_masks)}
    if config_path is None:
        return result
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        sources = {p.get("provider_source_id") for p in config.get("provider", []) if isinstance(p, dict) and p.get("enable", True) and p.get("model") == "deepseek-flash"}
        keys = set()
        for source in config.get("provider_sources", []):
            if not isinstance(source, dict) or not source.get("enable", True) or source.get("id") not in sources:
                continue
            values = source.get("key", [])
            if isinstance(values, str):
                values = [values]
            keys.update(v for v in values if isinstance(v, str) and v)
        result["configured_keys"] = len(keys)
        for mask in bill.key_masks:
            parts = re.split(r"(?:\*+|\.{3,}|…+|•+)", mask)
            if len(parts) == 1 and mask in keys:
                result["exact_matches"] += 1
            elif len(parts) == 2 and len(parts[0]) >= 4 and len(parts[1]) >= 4 and sum(k.startswith(parts[0]) and k.endswith(parts[1]) for k in keys) == 1:
                result["partial_matches"] += 1
        result["unmatched_keys"] -= result["exact_matches"] + result["partial_matches"]
        if bill.key_masks and result["exact_matches"] == len(bill.key_masks) == len(keys):
            result["status"] = "exact"
        elif not result["unmatched_keys"] and (result["partial_matches"] or result["exact_matches"]):
            result["status"] = "partial"
        elif result["unmatched_keys"]:
            result["status"] = "unmatched"
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return result


def _exported_at(metadata):
    raw = metadata.get("observed_at") if isinstance(metadata, dict) else None
    try:
        value = datetime.fromisoformat(raw)
        return value if value.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _ledger_rows(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise _error()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        columns = {r[1] for r in db.execute("PRAGMA table_info(requests)")}
        if not {"at", "model", "usage_known", "input_other", "input_cached", "output"} <= columns:
            raise _error()
        started = db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()
        rows = [dict(r) for r in db.execute("SELECT at,model,usage_known,input_other,input_cached,output FROM requests WHERE model='deepseek-flash'")]
    started_value = Decimal(started[0]) if started else None
    if started_value is not None and (not started_value.is_finite() or started_value < 0):
        raise _error()
    for row in rows:
        if type(row["at"]) not in (int, float) or not math.isfinite(row["at"]) or row["at"] < 0 or row["usage_known"] not in (0, 1):
            raise _error()
        if any(type(row[k]) is not int or not 0 <= row[k] <= MAX_COUNT for k in TOKEN_TYPES.values()):
            raise _error()
    return started_value, rows


def _pricing_helper():
    """Load only our fixed offline tariff module, never anything from a CSV."""
    candidates = (
        Path(__file__).resolve().parent.parent / "feiyu_steward" / "token_pricing.py",
        Path("D:/QQBot/data/astrbot/plugins/feiyu_steward/token_pricing.py"),
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            try:
                spec = importlib.util.spec_from_file_location("feiyu_billing_offline_pricing", candidate)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
            except (OSError, ImportError, AttributeError):
                pass
    return None


def _local_cost(rows, rates):
    """Use official-export rates, checking timestamp tariffs for split days."""
    known = [r for r in rows if r["usage_known"] == 1]
    if all(k in rates and len(rates[k]) == 1 for k in TOKEN_TYPES.values()):
        return sum((Decimal(r[k]) * next(iter(rates[k])) for r in known for k in TOKEN_TYPES.values()), ZERO)
    helper = _pricing_helper()
    if helper is None:
        return None
    total = ZERO
    for row in known:
        peak = helper._peak(row["at"])
        if peak is None:
            return None
        multiplier = 2 if peak else 1
        expected = {"input_other": Decimal("0.000001") * multiplier,
                    "input_cached": Decimal("0.00000002") * multiplier,
                    "output": Decimal("0.000004") * multiplier}
        for name, rate in expected.items():
            if row[name] and rate not in rates.get(name, set()):
                return None
            total += row[name] * rate
    # The product helper rounds at six decimal places for display. Check its
    # outcome, while retaining exact per-token Decimal precision for the bill.
    priced = helper.price_feiyu_rows(known)
    if not priced.get("complete") or Decimal(str(priced["total_cny"])) != total.quantize(Decimal(".000001")):
        return None
    return total


def reconcile(paths, *, ledger_path, config_path=None, metadata=None, now=None):
    """Return only safe numeric summaries; no credentials or raw rows escape.

    ``status`` is one of matched/difference/history_incomplete/unsupported_schema.
    Daily deltas remain visible when comparison evidence is insufficient. They
    are diagnostic observations, never synthetic additions to local usage.
    """
    try:
        bill = parse_receipts(paths, metadata=metadata, now=now)
        official = bill_summary(bill)
    except BillError:
        return {"schema_version": SCHEMA_VERSION, "status": "unsupported_schema", "limitations": ["unsupported_official_schema"], "days": []}
    key_match = match_configured_keys(bill, config_path)
    exported = _exported_at(metadata)
    now = now if isinstance(now, datetime) and now.tzinfo is not None else datetime.now(TZ)
    if exported is not None and exported > now + timedelta(minutes=5):
        exported = None
    scope = {"timezone": "UTC+08:00", "key_match": key_match, "exported_at": exported.isoformat() if exported else None, "key_filter": metadata.get("key_filter", "unknown") if isinstance(metadata, dict) and metadata.get("key_filter") in ("all", "filtered") else "unknown", "official_interval_semantics": "daily_start_inclusive_end_exclusive", "local_history_start": None}
    limitations = []
    try:
        started, rows = _ledger_rows(ledger_path)
        if started is not None:
            scope["local_history_start"] = datetime.fromtimestamp(float(started), TZ).isoformat()
    except (BillError, sqlite3.Error, OSError, ValueError, OverflowError, InvalidOperation):
        return {"schema_version": SCHEMA_VERSION, "status": "history_incomplete", "scope": scope, "official": official, "limitations": ["local_ledger_unavailable"], "days": []}
    days, local_total = [], _empty_totals()
    range_reasons = []
    if not official["contiguous_days"]:
        range_reasons.append("official_day_missing")
    if official["missing_hour_starts"]:
        range_reasons.append("official_hour_missing")
    raw_range = metadata.get("date_range") if isinstance(metadata, dict) else None
    if isinstance(raw_range, dict) and raw_range.get("status") == "verified" and type(raw_range.get("inclusive")) is bool:
        try:
            range_start = datetime.strptime(raw_range.get("start"), "%Y-%m-%d").replace(tzinfo=TZ)
            range_end = datetime.strptime(raw_range.get("end"), "%Y-%m-%d").replace(tzinfo=TZ) + timedelta(days=int(raw_range["inclusive"]))
            if not (official["start"] and _iso(official["start"]) == range_start and _iso(official["end"]) == range_end):
                range_reasons.append("official_selected_range_mismatch")
        except (ValueError, TypeError, OverflowError):
            range_reasons.append("official_selected_range_mismatch")
    for day in _day_data(bill):
        start, end = _iso(day["start"]), _iso(day["end"])
        cutoff = min(end, exported if exported else now)
        selected = [r for r in rows if start.timestamp() <= r["at"] < cutoff.timestamp()]
        local, unknown = _empty_totals(), 0
        for row in selected:
            if row["usage_known"] != 1:
                unknown += 1
                continue
            for k in TOKEN_TYPES.values():
                value = row[k]
                if type(value) is not int or not 0 <= value <= MAX_COUNT:
                    raise _error()
                local[k] += value
        local["input_total"] = local["input_other"] + local["input_cached"]
        local["total"] = local["input_total"] + local["output"]
        local["request_count"] = len(selected)
        for k in local_total:
            local_total[k] += local[k]
        reasons = list(range_reasons)
        if started is None or started > Decimal(str(start.timestamp())):
            reasons.append("local_history_starts_after_day_start")
        if exported is None:
            reasons.append("export_time_unverified")
        elif exported < end or now < end:
            reasons.append("official_current_day_unfinished")
        if key_match["status"] != "exact":
            reasons.append("key_match_" + key_match["status"])
        if scope["key_filter"] == "unknown":
            reasons.append("key_filter_unverified")
        if not day["has_amount"] or not day["has_cost"]:
            reasons.append("official_companion_csv_missing")
        if day["cost"] != day["detail_cost"]:
            reasons.append("official_cost_crosscheck_failed")
        if unknown:
            reasons.append("local_unknown_usage")
        if day["tokens"]["function_calls"] or day["tool_cost"]:
            reasons.append("local_tool_cost_unverified")
        rates = day["rates"]
        local_cost = _local_cost(selected, rates)
        if local_cost is None:
            reasons.append("local_price_split_unverified")
        deltas = {k: local[k] - day["tokens"][k] for k in ("input_other", "input_cached", "input_total", "output", "total", "request_count")}
        cost_delta = local_cost - day["model_cost"] if local_cost is not None else None
        differs = any(deltas.values()) or (cost_delta is not None and cost_delta != 0)
        status = "history_incomplete" if reasons else "difference" if differs else "matched"
        limitations.extend(reasons)
        days.append({"start": day["start"], "end": day["end"], "local_cutoff": cutoff.isoformat(), "status": status, "limitations": reasons, "official": {"totals": day["tokens"], "cost_cny": _money(day["cost"]) if day["has_cost"] else None, "model_cost_cny": _money(day["model_cost"]) if day["has_cost"] else None, "tool_cost_cny": _money(day["tool_cost"]) if day["has_cost"] else None, "detail_cost_cny": _money(day["detail_cost"]) if day["has_amount"] else None}, "local": {"totals": local, "known_usage_calls": len(selected) - unknown, "unknown_usage_calls": unknown, "model_cost_cny": _money(local_cost) if local_cost is not None else None, "cost_basis": "official_export_unit_prices_applied" if local_cost is not None else "unknown"}, "deltas": {"tokens": deltas, "model_cost_cny": _money(cost_delta) if cost_delta is not None else None}})
    status = "history_incomplete" if any(d["status"] == "history_incomplete" for d in days) else "difference" if any(d["status"] == "difference" for d in days) else "matched"
    limitations.extend(("tool_calls_not_recorded_in_local_token_ledger", "official_current_day_may_lag_export_time"))
    return {"schema_version": SCHEMA_VERSION, "status": status, "scope": scope, "official": official, "local": {"totals": local_total}, "days": days, "limitations": sorted(set(limitations))}
