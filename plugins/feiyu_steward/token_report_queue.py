"""Pure, minimal pending-report rows for the parent's durable state ledger.

The owner of ``state`` and ``save`` controls persistence and dispatch.  This
module reads no files, reads no clock, and never keeps a source message or any
usage totals.  Only ``waiting`` rows may become due; an uncertain send requires
separate reconciliation rather than another automatic send.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date

from .token_report_gate import REASON_CODES, validate_report_window

_ERROR = "invalid_pending_report"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_SOURCES = frozenset({"manual", "daily"})
_FORMATS = frozenset({"image", "text"})
_STATUSES = frozenset({"waiting", "sent", "intent", "unknown"})
_FIELDS = frozenset({
    "version", "status", "source", "format", "window", "receipt_key",
    "created_at", "next_check", "attempts", "last_reason_codes",
})
_NOTE = "这份报告尚未通过完整核验，原请求已保留，暂不发送报告。"
_UNSUPPORTED_NOTE = (
    "当前官方来源仅提供完整自然日精度，无法核实原请求时段；"
    "原请求已保留，暂不发送报告。"
)
_GAP_GUIDANCE = (
    "缺失用量或历史覆盖目前没有自动补数来源，不保证能够补齐；"
    "合并报告仍需 Codex 用量、用途分类、历史覆盖和价格均完整，并通过全部核验。"
)
_UNSUPPORTED_GUIDANCE = (
    "肥鱼来源可按完整自然日核对；"
    "合并报告仍需 Codex 用量、用途分类、历史覆盖和价格均完整，并通过全部核验。"
    "缺失用量或历史覆盖目前没有自动补数来源，不保证能够补齐。"
)
_REASON_TEXT = {
    "invalid_report_window": "请求的报告时段尚未通过核验。",
    "feiyu_window_invalid": "肥鱼用量的时段信息尚未通过核验。",
    "feiyu_window_mismatch": "肥鱼用量与原请求时段不一致。",
    "deepseek_billing_unconfirmed": "DeepSeek 官方费用尚未核实。",
    "deepseek_tokens_unofficial": "DeepSeek 用量尚无可核实的官方依据。",
    "deepseek_window_granularity_unsupported": "DeepSeek 官方来源仅提供完整自然日精度，无法核实原请求时段。",
    "deepseek_history_incomplete": "DeepSeek 历史记录未完整覆盖原请求时段。",
    "deepseek_usage_incomplete": "DeepSeek 部分调用未回传完整用量。",
    "codex_summary_unavailable": "Codex 用量来源暂不可用。",
    "codex_window_invalid": "Codex 用量的时段信息尚未通过核验。",
    "codex_window_mismatch": "Codex 用量与原请求时段不一致。",
    "codex_coverage_incomplete": "Codex 历史记录未完整覆盖原请求时段。",
    "codex_usage_incomplete": "Codex 部分调用未回传完整用量。",
    "codex_usage_invalid": "Codex 用量数据尚未通过一致性核验。",
    "codex_pricing_incomplete": "Codex 部分调用缺少可核实的价格。",
    "codex_pricing_invalid": "Codex 价格数据尚未通过一致性核验。",
    "codex_classification_incomplete": "Codex 部分调用的用途分类尚未核实。",
    "codex_project_buckets_invalid": "Codex 各用途分类的数据尚不完整。",
    "codex_project_totals_mismatch": "Codex 各用途分类的用量与合计不一致。",
    "codex_project_pricing_mismatch": "Codex 各用途分类的费用与合计不一致。",
    "codex_empty_window_unverified": "Codex 尚无完整历史覆盖依据，无法确认原请求时段没有调用。",
    "gate_validation_failed": "报告的完整性核验尚未完成。",
}
_REASON_CATEGORY = {
    "deepseek_window_granularity_unsupported": "source_precision_unsupported",
    "deepseek_history_incomplete": "historical_coverage_incomplete",
    "codex_coverage_incomplete": "historical_coverage_incomplete",
    "codex_empty_window_unverified": "historical_coverage_incomplete",
    "deepseek_usage_incomplete": "usage_not_returned",
    "codex_usage_incomplete": "usage_not_returned",
}


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(_ERROR)
    return float(value)


def _choice(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(_ERROR)
    return value


def _receipt(value):
    if not isinstance(value, str):
        raise TypeError(_ERROR)
    if _HASH.fullmatch(value):
        return value
    if _DAY.fullmatch(value):
        date.fromisoformat(value)
        return value
    raise ValueError(_ERROR)


def queue_key(source, identity, format):
    """Hash an identity; changing format cannot bypass its delivery barrier."""
    try:
        source = _choice(source, _SOURCES)
        _choice(format, _FORMATS)
        if type(identity) not in (str, int):
            raise ValueError
        identity = str(identity)
        if not 1 <= len(identity) <= 1024:
            raise ValueError
        value = json.dumps(["token-report-queue-v1", source, identity],
                           ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 - never leak caller-provided identifiers
        raise ValueError(_ERROR) from None


def validate_pending_report(row):
    """Project a row to fresh safe fields, or raise one fixed validation error.

    Extra top-level fields are intentionally discarded so migrated state cannot
    retain a raw summary, source text, transport response, or message identity.
    The nested window is validated by the same contract used by the report gate.
    """
    try:
        if not isinstance(row, dict) or not _FIELDS.issubset(row):
            raise ValueError
        if type(row["version"]) is not int or row["version"] != 1:
            raise ValueError
        status = _choice(row["status"], _STATUSES)
        source = _choice(row["source"], _SOURCES)
        format = _choice(row["format"], _FORMATS)
        window = validate_report_window(row["window"])
        receipt_key = _receipt(row["receipt_key"])
        if source == "daily" and (window["kind"] != "daily" or receipt_key != window["day"]):
            raise ValueError
        attempts = row["attempts"]
        if type(attempts) is not int or attempts < 0:
            raise ValueError
        reasons = row["last_reason_codes"]
        if (not isinstance(reasons, (list, tuple)) or len(reasons) > len(REASON_CODES)
                or any(not isinstance(reason, str) or reason not in REASON_CODES for reason in reasons)):
            raise ValueError
        return {
            "version": 1, "status": status, "source": source, "format": format,
            "window": window, "receipt_key": receipt_key,
            "created_at": _stamp(row["created_at"]), "next_check": _stamp(row["next_check"]),
            "attempts": attempts, "last_reason_codes": list(dict.fromkeys(reasons)),
        }
    except Exception:  # noqa: BLE001 - malformed durable state has one fixed safe error
        raise ValueError(_ERROR) from None


def new_pending_report(window, source, format, now, receipt_key):
    """Create a waiting row for a previously frozen window, without side effects."""
    return validate_pending_report({
        "version": 1, "status": "waiting", "source": source, "format": format,
        "window": window, "receipt_key": receipt_key,
        "created_at": now, "next_check": now, "attempts": 0, "last_reason_codes": [],
    })


def pending_report_note(row=None):
    """Return fixed copy; even an unsafe caller-provided row cannot reach prose."""
    return _NOTE


def pending_report_feedback(reason_codes=()):
    """Explain only allowlisted gate codes with fixed copy and no report data.

    Invalid elements and unknown codes are ignored.  No supplied text, object
    representation, amount, or exception is interpolated into the result.
    """
    codes = []
    if type(reason_codes) in (list, tuple, set, frozenset):
        codes = list(dict.fromkeys(
            code for code in reason_codes if type(code) is str and code in REASON_CODES
        ))
        if type(reason_codes) in (set, frozenset):
            codes.sort()
    unsupported = "deepseek_window_granularity_unsupported" in codes
    result = {
        "status": "source_unavailable" if unsupported else "pending_verification",
        "reason_codes": codes,
        "reason_categories": list(dict.fromkeys(
            _REASON_CATEGORY.get(code, "verification_incomplete") for code in codes
        )),
        "reasons": [_REASON_TEXT.get(code, "报告的完整性核验尚未完成。") for code in codes],
        "note": _UNSUPPORTED_NOTE if unsupported else _NOTE,
    }
    if unsupported:
        result["guidance"] = _UNSUPPORTED_GUIDANCE
    elif any(category in result["reason_categories"]
             for category in ("historical_coverage_incomplete", "usage_not_returned")):
        result["guidance"] = _GAP_GUIDANCE
    return result


def due_report_keys(ledger, now, limit=4):
    """Return deterministic safe keys for due waiting rows, without mutation."""
    try:
        current = _stamp(now)
        if type(limit) is not int or limit < 0:
            raise ValueError
    except Exception:  # noqa: BLE001 - malformed scheduling input must fail closed
        raise ValueError(_ERROR) from None
    if not isinstance(ledger, dict) or limit == 0:
        return []
    due = []
    for key, raw in ledger.items():
        if not isinstance(key, str) or not _HASH.fullmatch(key):
            continue
        try:
            row = validate_pending_report(raw)
        except ValueError:
            continue
        if row["status"] == "waiting" and row["next_check"] <= current:
            due.append((row["next_check"], row["created_at"], key))
    due.sort()
    return [item[2] for item in due[:limit]]
