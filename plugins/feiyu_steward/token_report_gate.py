"""Pure, fail-closed readiness checks for one frozen combined usage report.

This module reads no files and sends nothing.  Its public result contains only
fixed reason codes and a validated UTC+8 interval, never supplied prose.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .official_billing import (
    primary_billing_for_summary,
    primary_token_usage_for_summary,
)

TZ = timezone(timedelta(hours=8))
MAX_COUNT = 10**12
PROJECT_KEYS = frozenset({"feiyu_bot", "academic_poc", "other"})
TOTAL_FIELDS = (
    "calls", "success", "failed", "known_usage_calls", "unknown_usage_calls",
    "input_tokens", "cached_tokens", "uncached_input_tokens", "output_tokens",
    "reasoning_tokens", "total_tokens",
)
REASON_CODES = frozenset({
    "invalid_report_window", "feiyu_window_invalid", "feiyu_window_mismatch",
    "deepseek_billing_unconfirmed", "deepseek_tokens_unofficial",
    "deepseek_window_granularity_unsupported", "deepseek_history_incomplete",
    "deepseek_usage_incomplete",
    "codex_summary_unavailable", "codex_window_invalid", "codex_window_mismatch",
    "codex_coverage_incomplete", "codex_usage_incomplete", "codex_usage_invalid",
    "codex_pricing_incomplete", "codex_pricing_invalid", "gate_validation_failed",
    "codex_classification_incomplete", "codex_project_buckets_invalid",
    "codex_project_totals_mismatch", "codex_project_pricing_mismatch",
    "codex_empty_window_unverified",
})


def _stamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 48:
        raise ValueError("invalid_report_window")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("invalid_report_window")
    return stamp.astimezone(TZ)


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid_report_window")
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)


def validate_report_window(window):
    """Validate a persisted interval and return a fresh, safe canonical dict."""
    try:
        if not isinstance(window, dict):
            raise TypeError
        kind = window.get("kind")
        fields = {"kind", "start", "end", "day"} if kind == "daily" else {"kind", "start", "end"}
        if kind not in ("daily", "rolling") or set(window) != fields:
            raise ValueError
        begin, finish = _stamp(window.get("start")), _stamp(window.get("end"))
        duration = (finish - begin).total_seconds()
        if not 0 < duration <= 720 * 3600:
            raise ValueError
        result = {"start": begin.isoformat(), "end": finish.isoformat(), "kind": kind}
        if kind == "daily":
            midnight = _day(window.get("day"))
            if begin != midnight or finish != midnight + timedelta(days=1):
                raise ValueError
            result["day"] = midnight.date().isoformat()
        elif duration < 1800:
            raise ValueError
        return result
    except Exception:  # noqa: BLE001 - invalid evidence is always one fixed safe error
        raise ValueError("invalid_report_window") from None


def freeze_report_window(summary):
    """Freeze structured dates, without inferring anything from report labels.

    Daily summaries represent the whole UTC+8 day.  Rolling summaries require
    explicit aware endpoints and bounded hour metadata.  Endpoints are the
    identity; subsecond hour rounding from the existing readers is tolerated.
    """
    try:
        if not isinstance(summary, dict):
            raise TypeError
        if "window_hours" in summary:
            hours = summary.get("window_hours")
            if type(hours) not in (int, float) or not 0.5 <= hours <= 720 or not math.isfinite(hours):
                raise ValueError
            begin, finish = _stamp(summary.get("from")), _stamp(summary.get("until"))
            if abs((finish - begin).total_seconds() - hours * 3600) >= 1:
                raise ValueError
            value = {"kind": "rolling", "start": begin.isoformat(), "end": finish.isoformat()}
        else:
            begin = _day(summary.get("day"))
            finish = begin + timedelta(days=1)
            if ("from" in summary or "until" in summary) and (
                    _stamp(summary.get("from")) != begin or _stamp(summary.get("until")) != finish):
                raise ValueError
            value = {"kind": "daily", "start": begin.isoformat(), "end": finish.isoformat(),
                     "day": begin.date().isoformat()}
        return validate_report_window(value)
    except Exception:  # noqa: BLE001 - invalid evidence is always one fixed safe error
        raise ValueError("invalid_report_window") from None


def _count(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def _money(value):
    return (type(value) in (int, float) and 0 <= value <= MAX_COUNT
            and math.isfinite(value))


def _valid_totals(totals):
    if not isinstance(totals, dict) or not all(_count(totals.get(key)) for key in TOTAL_FIELDS):
        return False
    return (totals["success"] + totals["failed"] == totals["calls"]
            and totals["known_usage_calls"] + totals["unknown_usage_calls"] == totals["calls"]
            and totals["input_tokens"] == totals["uncached_input_tokens"] + totals["cached_tokens"]
            and totals["total_tokens"] == totals["input_tokens"] + totals["output_tokens"]
            and totals["reasoning_tokens"] <= totals["output_tokens"]
            and (totals["calls"] != 0 or totals["total_tokens"] == 0))


def _complete_pricing(pricing, calls):
    return (isinstance(pricing, dict) and pricing.get("complete") is True
            and _count(pricing.get("unpriced_calls")) and pricing["unpriced_calls"] == 0
            and _count(pricing.get("priced_calls")) and pricing["priced_calls"] == calls
            and _money(pricing.get("known_cny")) and _money(pricing.get("total_cny"))
            and pricing["known_cny"] == pricing["total_cny"]
            and (calls != 0 or pricing["total_cny"] == 0))


def _classification_complete(classification, calls):
    if not isinstance(classification, dict):
        return False
    fields = ("known_calls", "unclassified_calls", "conflicting_calls", "subagent_calls")
    return (all(_count(classification.get(key)) for key in fields)
            and classification.get("method") == "captured_thread_id_and_codex_parent_edges"
            and classification["known_calls"] == calls
            and classification["unclassified_calls"] == 0
            and classification["conflicting_calls"] == 0
            and classification["subagent_calls"] <= classification["known_calls"]
            and _money(classification.get("coverage_percent"))
            and classification["coverage_percent"] == 100)


def _project_reasons(summary, totals, pricing):
    projects = summary.get("by_project")
    if not isinstance(projects, list) or len(projects) not in (len(PROJECT_KEYS), len(PROJECT_KEYS) + 1):
        return ["codex_project_buckets_invalid"]
    keys = [project.get("key") for project in projects if isinstance(project, dict)]
    if (len(keys) != len(projects) or any(not isinstance(key, str) for key in keys)
            or len(set(keys)) != len(keys)
            or set(keys) not in (PROJECT_KEYS, PROJECT_KEYS | {"unclassified"})):
        return ["codex_project_buckets_invalid"]
    for project in projects:
        counts, cost = project.get("totals"), project.get("pricing")
        if (not _valid_totals(counts) or counts["unknown_usage_calls"] != 0
                or counts["known_usage_calls"] != counts["calls"]
                or (project.get("key") == "unclassified" and counts["calls"] != 0)
                or not _complete_pricing(cost, counts["calls"])):
            return ["codex_project_buckets_invalid"]
    reasons = []
    if _valid_totals(totals) and any(
            sum(project["totals"][key] for project in projects) != totals[key] for key in TOTAL_FIELDS):
        reasons.append("codex_project_totals_mismatch")
    if _valid_totals(totals) and _complete_pricing(pricing, totals["calls"]):
        # The reader rounds each bucket and the overall amount independently
        # to six decimals.  Bound only that absolute rounding discrepancy.
        maximum_rounding = Decimal("0.000002")
        for field in ("known_cny", "total_cny"):
            bucket_sum = sum(Decimal(str(project["pricing"][field])) for project in projects)
            if abs(bucket_sum - Decimal(str(pricing[field]))) > maximum_rounding:
                reasons.append("codex_project_pricing_mismatch")
                break
        if any(sum(project["pricing"][field] for project in projects) != pricing[field]
               for field in ("priced_calls", "unpriced_calls")):
            reasons.append("codex_project_pricing_mismatch")
    return reasons


def _diagnostic_interval(summary, window):
    """Read dates for safe refusal details without constructing a ready window."""
    try:
        if window is not None:
            return _stamp(window["start"]), _stamp(window["end"])
        begin, finish = _stamp(summary.get("from")), _stamp(summary.get("until"))
        if not 0 < (finish - begin).total_seconds() <= 720 * 3600:
            return None
        return begin, finish
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def _deepseek_pending_reasons(summary, window):
    """Explain unconfirmed evidence; local gaps cannot override official proof."""
    if not isinstance(summary, dict):
        return []
    reasons, interval = [], _diagnostic_interval(summary, window)
    official = summary.get("official_billing")
    daily_source = (isinstance(official, dict) and (
        official.get("source_granularity") == "daily_aggregate"
        or official.get("source") == "official_daily_csv"))
    if (daily_source and official.get("exact_window") is None and interval is not None
            and any(stamp.time() != datetime.min.time() for stamp in interval)):
        reasons.append("deepseek_window_granularity_unsupported")
    # partial_day also marks an unfinished current day; it alone does not
    # establish that recording began after this interval's start.
    history_incomplete = summary.get("not_observed") is True
    if not history_incomplete and interval is not None:
        try:
            history_incomplete = _stamp(summary.get("observed_since")) > interval[0]
        except (ValueError, TypeError, OverflowError):
            pass
    if history_incomplete:
        reasons.append("deepseek_history_incomplete")
    totals = summary.get("totals")
    unknown = totals.get("unknown_usage_calls") if isinstance(totals, dict) else None
    if type(unknown) is int and unknown > 0:
        reasons.append("deepseek_usage_incomplete")
    return reasons


def _codex_reasons(summary, window):
    if not isinstance(summary, dict) or summary.get("ok") is not True or summary.get("not_observed") is not False:
        return ["codex_summary_unavailable"]
    reasons = []
    try:
        actual = freeze_report_window(summary)
        if window is not None and actual != window:
            reasons.append("codex_window_mismatch")
    except ValueError:
        reasons.append("codex_window_invalid")
    try:
        retained = _stamp(summary.get("retained_since"))
        if summary.get("partial_day") is not False or window is None or retained > _stamp(window["start"]):
            raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError):
        reasons.append("codex_coverage_incomplete")
    totals = summary.get("totals")
    valid_totals = _valid_totals(totals)
    if not valid_totals:
        reasons.append("codex_usage_invalid")
    else:
        if totals["unknown_usage_calls"] != 0 or totals["known_usage_calls"] != totals["calls"]:
            reasons.append("codex_usage_incomplete")
        if totals["calls"] == 0:
            # MIN(retained timestamp) says nothing about recording continuity.
            # The current reader has no persisted coverage watermark that can
            # prove an entirely empty interval was observed rather than lost.
            reasons.append("codex_empty_window_unverified")
    if not valid_totals or not _classification_complete(summary.get("classification"), totals["calls"]):
        reasons.append("codex_classification_incomplete")
    pricing = summary.get("pricing")
    if (not isinstance(pricing, dict) or pricing.get("complete") is not True
            or not _count(pricing.get("unpriced_calls")) or pricing.get("unpriced_calls") != 0
            or not _count(pricing.get("priced_calls")) or not valid_totals
            or pricing.get("priced_calls") != totals["calls"]):
        reasons.append("codex_pricing_incomplete")
    if (not isinstance(pricing, dict) or not _money(pricing.get("known_cny"))
            or not _money(pricing.get("total_cny")) or pricing["known_cny"] != pricing["total_cny"]):
        reasons.append("codex_pricing_invalid")
    reasons.extend(_project_reasons(summary, totals, pricing))
    return reasons


def report_readiness(summary, codex_summary, *, window=None):
    """Permit rendering only when both complete sources match a frozen window."""
    frozen, reasons = None, []
    try:
        if window is not None:
            try:
                frozen = validate_report_window(window)
            except ValueError:
                reasons.append("invalid_report_window")
        try:
            actual = freeze_report_window(summary)
            if window is None:
                frozen = actual
            elif frozen is not None and actual != frozen:
                reasons.append("feiyu_window_mismatch")
        except ValueError:
            reasons.append("feiyu_window_invalid")
        if primary_billing_for_summary(summary).get("status") != "confirmed":
            reasons.append("deepseek_billing_unconfirmed")
            reasons.extend(_deepseek_pending_reasons(summary, frozen))
        if primary_token_usage_for_summary(summary).get("source") != "official":
            reasons.append("deepseek_tokens_unofficial")
        reasons.extend(_codex_reasons(codex_summary, frozen))
    except Exception:  # noqa: BLE001 - every unexpected shape must fail closed
        reasons.append("gate_validation_failed")
    return {"ready": not reasons, "reason_codes": list(dict.fromkeys(reasons)), "window": frozen}


evaluate_report_readiness = report_readiness
