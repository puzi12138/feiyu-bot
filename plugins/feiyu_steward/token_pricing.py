"""Reprice numeric usage with a dated official tariff; never an account bill.

Rates checked 2026-10-26 against official model documentation.  No network,
credentials, request bodies, account identities, or model calls are needed.
"""

import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

PRICE_DATE = "2026-10-26"
USD_CNY = Decimal("6.7180")
FX_DATE = "2026-10-26"
FX_SOURCE = "https://www.boc.cn/sourcedb/whpj/"
DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
TZ = timezone(timedelta(hours=8))
MILLION = Decimal(1_000_000)
MAX_TOKENS = 1_000_000_000_000
SAFE_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
# USD / million: uncached input, cache read, 30-minute cache write, output.
OPENAI_RATES = {
    "gpt-6.1-sol": tuple(map(Decimal, ("2", ".1", "2.5", "10"))),
    "gpt-6-luna": tuple(map(Decimal, (".1", ".01", ".125", ".5"))),
}
TIER_MULTIPLIERS = {
    "": Decimal(1), "auto": Decimal(1), "default": Decimal(1), "standard": Decimal(1),
    "priority": Decimal(2), "fast": Decimal(2), "flex": Decimal(".5"),
    "batch": Decimal(".5"), "ultrafast": Decimal(6),
}
# Populated only for calendars verified against the State Council notice.
HOLIDAY_SOURCE = "https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html"
HOLIDAY_RANGES_2026 = (
    ("2026-01-01", "2026-01-03"), ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"), ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"), ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
)
HOLIDAYS_2026 = frozenset(
    (datetime.fromisoformat(start) + timedelta(days=i)).date().isoformat()
    for start, end in HOLIDAY_RANGES_2026
    for i in range((datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1)
)


def _count(value):
    return type(value) is int and 0 <= value <= MAX_TOKENS


def _money(value):
    return float(value.quantize(Decimal(".000001")))


def _peak(at):
    if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
        return None
    try:
        dt = datetime.fromtimestamp(at, TZ)
    except (ValueError, OverflowError, OSError):
        return None
    if dt.year != 2026:
        return None  # An unverified future holiday calendar is not an estimate.
    return (dt.weekday() < 5 and dt.date().isoformat() not in HOLIDAYS_2026
            and (9 <= dt.hour < 12 or 14 <= dt.hour < 18))


def _result(entries):
    total, unknown, by_model, by_feature = Decimal(0), 0, {}, {}
    for model, feature, cost in entries:
        if cost is None:
            unknown += 1
            continue
        total += cost
        item = by_model.setdefault(model, {"cost": Decimal(0), "calls": 0})
        item["cost"] += cost
        item["calls"] += 1
        if isinstance(feature, str) and SAFE_LABEL.fullmatch(feature):
            by_feature[feature] = by_feature.get(feature, Decimal(0)) + cost
    return {
        "known_cny": _money(total), "total_cny": _money(total) if not unknown else None,
        "complete": not unknown, "unpriced_calls": unknown, "currency": "CNY",
        "priced_calls": sum(item["calls"] for item in by_model.values()),
        "basis_label": "官方标准价折算", "usd_cny": float(USD_CNY), "rate_date": FX_DATE,
        "price_date": PRICE_DATE, "fx_basis": "中国银行中行折算价",
        "sources": [DEEPSEEK_SOURCE, OPENAI_SOURCE, FX_SOURCE],
        "by_model": [{"model": model, "cost_cny": _money(item["cost"]), "calls": item["calls"]}
                     for model, item in sorted(by_model.items())],
        "by_feature": [{"feature": key, "cost_cny": _money(value)}
                       for key, value in sorted(by_feature.items())],
    }


def price_feiyu_rows(rows):
    """Use original timestamps for DeepSeek peak/off-peak, not report time."""
    entries = []
    for raw in rows:
        row = dict(raw)
        model, feature = row.get("model"), row.get("feature")
        cost = None
        values = [row.get(key) for key in ("input_other", "input_cached", "output")]
        peak = _peak(row.get("at"))
        if (model == "deepseek-flash" and row.get("usage_known") in (True, 1)
                and all(_count(value) for value in values) and peak is not None):
            uncached, cached, output = values
            cost = (Decimal(uncached) + Decimal(cached) * Decimal(".02")
                    + Decimal(output) * 4) / MILLION * (2 if peak else 1)
        entries.append((model, feature, cost))
    return _result(entries)


def _codex_price(row):
    # An explicitly returned upstream model is authoritative, including an
    # unknown model. Never quietly price it as the requested cheaper model.
    model = next((row.get(key) for key in ("upstream_model", "requested_model", "model_id")
                  if row.get(key)), "")
    rates = OPENAI_RATES.get(model)
    if rates is None:
        return model, None
    tier = next((row.get(key) for key in ("response_service_tier", "service_tier", "requested_service_tier")
                 if row.get(key)), "")
    multiplier = TIER_MULTIPLIERS.get(tier)
    if multiplier is None or (tier == "ultrafast" and model == "gpt-6-luna"):
        return model, None
    try:
        data = json.loads(row.get("token_breakdown_json"))
    except (TypeError, ValueError):
        return model, None
    if not isinstance(data, dict) or data.get("quality") != "complete":
        return model, None
    inp, out = data.get("input"), data.get("output")
    if not isinstance(inp, dict) or not isinstance(out, dict):
        return model, None
    values = [row.get(key) for key in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens")]
    if not all(_count(value) for value in values):
        return model, None
    total_input, cached, output, total = values
    # Missing write counts cannot be assumed to mean zero on newer models.
    write = inp.get("cache_write_tokens")
    uncached = inp.get("uncached_tokens")
    if (not _count(write) or not _count(uncached) or cached > total_input
            or uncached + cached + write != total_input
            or inp.get("cache_read_tokens") != cached or inp.get("total_tokens") != total_input
            or out.get("total_tokens") != output or data.get("total_tokens") != total
            or total_input + output != total):
        return model, None
    if write:
        # The local breakdown has no retention duration. Official 30-minute
        # and 12-hour write rates differ, so neither may be silently assumed.
        return model, None
    reasoning = row.get("reasoning_tokens")
    if not _count(reasoning) or reasoning > output:
        return model, None
    # This dated report is a Standard-region token estimate, not an invoice.
    # No regional uplift is guessed from account routing information.
    input_rate, read_rate, write_rate, output_rate = rates
    long = total_input > 272_000
    input_multiplier = Decimal(2) if long else Decimal(1)
    output_multiplier = Decimal("1.5") if long else Decimal(1)
    cost = ((uncached * input_rate + cached * read_rate + write * write_rate) * input_multiplier
            + output * output_rate * output_multiplier) * multiplier / MILLION * USD_CNY
    return model, cost


def price_codex_rows(rows):
    """Caller must pass one selected usage row per request, never raw replays."""
    entries = []
    for raw in rows:
        model, cost = _codex_price(dict(raw))
        entries.append((model, None, cost))
    return _result(entries)


def price_text(pricing):
    if not isinstance(pricing, dict):
        return "费用待核实"
    if not pricing.get("complete") and not pricing.get("by_model"):
        return "费用待核实"
    value = pricing.get("total_cny") if pricing.get("complete") else pricing.get("known_cny")
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return "费用待核实"
    label = "官方价估算" if pricing.get("complete") else "已定价部分"
    return f"{label} ¥{value:,.2f}"
