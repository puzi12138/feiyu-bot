"""Read an existing DeepSeek observation as a separate diagnostic annotation.

The official account and the retained Feiyu records have different scopes.
This module never changes feature totals or contacts an external service.
"""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from .deepseek_reconcile_2h import INTERVAL_SECONDS, MAX_COUNT, last_observed_record

TZ = timezone(timedelta(hours=8))
OFFICIAL_SCOPE = "all_keys"
LOCAL_SCOPE = "feiyu_recorded_deepseek_flash"
STATES = frozenset({"available", "observed_difference", "feature_filtered",
                    "period_not_daily", "date_mismatch", "observation_expired",
                    "observation_unavailable"})


def _datetime(value):
    if not isinstance(value, str) or len(value) > 48:
        raise ValueError("invalid_time")
    value = datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(TZ)


def _day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("invalid_day")
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("invalid_day")
    return parsed


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ValueError("invalid_count")
    return value


def _empty(state):
    return {"state": state, "official_scope": OFFICIAL_SCOPE, "local_scope": LOCAL_SCOPE,
            "official_observed_at": None, "official_day": None, "token_difference": None,
            "official_tokens": None, "local_snapshot_known_tokens": None}


def project_reconciliation(root, summary, *, record_loader=None):
    """Project a safe diagnostic; missing or unsuitable observations stay unknown."""
    if summary.get("selected_features") is not None:
        return _empty("feature_filtered")
    if summary.get("period") not in ("today", "yesterday", "day"):
        return _empty("period_not_daily")
    try:
        current = _datetime(summary["observed_at"])
        begin = _datetime(summary["from"])
        finish = _datetime(summary["until"])
        midnight = datetime.combine(begin.date(), time.min, TZ)
        if begin != midnight or finish <= begin or finish > current:
            return _empty("period_not_daily")
        is_today = begin.date() == current.date()
        if (is_today and finish != current) or (not is_today and finish != midnight + timedelta(days=1)):
            return _empty("period_not_daily")
        loader = record_loader or last_observed_record
        record = loader(root)
        if (not isinstance(record, dict) or record.get("schema") != "deepseek_reconciliation_2h_v1"
                or record.get("state") != "observed_incomplete" or not isinstance(record.get("periods"), dict)):
            return _empty("observation_unavailable")
        name = "today" if is_today else "yesterday"
        entry = record["periods"].get(name)
        official = entry.get("official") if isinstance(entry, dict) else None
        if (not isinstance(entry, dict) or entry.get("period") != name
                or not isinstance(official, dict) or official.get("key_filter") != "all"
                or official.get("data_lag_minutes") != 5):
            return _empty("observation_unavailable")
        # Keep full precision for freshness and future-time checks. The saved
        # local interval itself is serialized to whole seconds by the producer.
        observed = _datetime(official["observed_at"])
        official_day = _day(official["day"])
        if official_day != observed.date() - timedelta(days=name == "yesterday"):
            return _empty("observation_unavailable")
        result = _empty("observation_unavailable")
        result.update(official_observed_at=observed.isoformat(),
                      official_day=official_day.isoformat())
        if official_day != begin.date():
            result["state"] = "date_mismatch"
            return result
        age = (current - observed).total_seconds()
        if age < 0 or age >= INTERVAL_SECONDS:
            result["state"] = "observation_expired"
            return result
        local = entry.get("local_feiyu")
        delta = entry.get("diagnostic_differences")
        # Today can show a difference between these two snapshots, but the
        # advertised delay is not an exact matching cutoff.
        if (entry.get("state") != ("observed_incomplete" if is_today else "diagnostic_incomplete")
                or not isinstance(local, dict) or local.get("model_filter") != "deepseek-flash"
                or local.get("coverage_complete") is not False
                or _datetime(local["from"]) != begin
                or _datetime(local["until"]) != (observed.replace(microsecond=0) if is_today else finish)
                or (not is_today and observed < finish + timedelta(minutes=5))):
            return result
        try:
            official_tokens = _count(official["total_tokens"])
            local_tokens = _count(local["known_tokens"])
        except (KeyError, TypeError, ValueError):
            # A missing local value is unknown, but an independently checked
            # account date and observation time are still useful.
            return result
        difference = official_tokens - local_tokens
        if not is_today and (not isinstance(delta, dict)
                             or type(delta.get("official_minus_local_known_tokens")) is not int
                             or delta["official_minus_local_known_tokens"] != difference):
            return result
        result.update(state="observed_difference" if is_today else "available", official_tokens=official_tokens,
                      local_snapshot_known_tokens=local_tokens, token_difference=difference)
        return result
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return _empty("observation_unavailable")


def validate_reconciliation(summary):
    """Reject inconsistent annotations before rendering; old summaries remain valid."""
    value = summary.get("reconciliation")
    if value is None:
        return
    if (not isinstance(value, dict) or value.get("state") not in STATES
            or value.get("official_scope") != OFFICIAL_SCOPE or value.get("local_scope") != LOCAL_SCOPE):
        raise ValueError("invalid_reconciliation")
    observed, day = value.get("official_observed_at"), value.get("official_day")
    if (observed is None) != (day is None):
        raise ValueError("invalid_reconciliation")
    if observed is not None:
        _datetime(observed)
        _day(day)
    numbers = (value.get("official_tokens"), value.get("local_snapshot_known_tokens"), value.get("token_difference"))
    if value["state"] not in ("available", "observed_difference"):
        if any(number is not None for number in numbers):
            raise ValueError("invalid_reconciliation")
        return
    official, local, difference = numbers
    if (summary.get("selected_features") is not None or summary.get("period") not in ("today", "yesterday", "day")
            or type(difference) is not int or difference != _count(official) - _count(local)
            or observed is None):
        raise ValueError("invalid_reconciliation")
    begin, finish, current = (_datetime(summary[key]) for key in ("from", "until", "observed_at"))
    observation = _datetime(observed)
    if (begin != datetime.combine(begin.date(), time.min, TZ) or finish > current
            or begin.date() != _day(day) or not 0 <= (current - observation).total_seconds() < INTERVAL_SECONDS):
        raise ValueError("invalid_reconciliation")
    if value["state"] == "observed_difference":
        if (summary["period"] not in ("today", "day") or finish != current
                or begin.date() != current.date() or observation.date() != begin.date()):
            raise ValueError("invalid_reconciliation")
    elif (summary["period"] not in ("yesterday", "day") or finish != begin + timedelta(days=1)
          or observation.date() - timedelta(days=1) != begin.date()
          or observation < finish + timedelta(minutes=5)):
        raise ValueError("invalid_reconciliation")


def snapshot_caption(value):
    """Show the two source values and their ratio, without asserting a subset."""
    official = value["official_tokens"]
    local = value["local_snapshot_known_tokens"]
    ratio = f"{Decimal(local) * 100 / Decimal(official):,.2f}%" if official else "不可计算"
    clock = _datetime(value["official_observed_at"]).strftime("%H:%M")
    return f"对账快照 {clock} · 本机 {local:,} / 官网 {official:,}（两值之比 {ratio}）"


def reconciliation_text(value):
    """Fixed user-facing text; no raw errors or source content are displayed."""
    if not isinstance(value, dict):
        return []
    # Keep the reason local and deterministic.  A missing or mismatched
    # snapshot is a scope/freshness problem, not a missing amount that can be
    # assigned to a feature.  These lines deliberately avoid raw upstream
    # responses and never turn unknown usage into zero.
    messages = {
        "feature_filtered": [
            "对账差额不可对应：查询带了功能筛选，而官网快照覆盖全部密钥，没有同范围基准。",
            "官网全部密钥与肥鱼本机 DeepSeek-flash 记录的范围不同；差额不计入功能合计。",
        ],
        "period_not_daily": [
            "对账差额不可对应：当前查询不是一个完整自然日，官网日账单无法与该时段对齐。",
            "范围或时段不一致时不计算差额，也不把差额分摊到功能。",
        ],
        "date_mismatch": [
            "对账差额不可对应：官网快照的数据日与本报告日期不同，尚未取得对应完整日快照。",
            "官网统计全部密钥，本机仅统计肥鱼 DeepSeek-flash；差额不计入功能合计。",
        ],
        "observation_expired": [
            "对账差额不可对应：官网快照已超过两小时新鲜窗口，或观测时间异常。",
            "官网全部密钥与肥鱼本机 DeepSeek-flash 的范围仍不同；等新快照后再判断，不分摊差额。",
        ],
        "observation_unavailable": [
            "对账差额不可对应：没有可用的官网完整日快照，可能仍在等待约五分钟数据延迟或快照完成。",
            "本机未回传用量保持未知，不当作零；差额不计入功能合计。",
        ],
    }
    if value.get("state") in ("available", "observed_difference"):
        today = value["state"] == "observed_difference"
        label = "官网与本机观测差额（截止未对齐，未归因）" if today else "官网与本机差额（未归因）"
        caveat = "官网全部密钥与肥鱼本机 DeepSeek-flash 快照范围不同；差额不计入功能合计。"
        if today:
            caveat += "今日官网截止未与本机对齐。"
        lines = [f"{label}：{value['token_difference']:+,} token；不计入功能合计。",
                 snapshot_caption(value), caveat]
    else:
        lines = list(messages.get(value.get("state"), ["对账差额不可对应：没有满足范围和新鲜度要求的快照。",
                                                          "差额保持未归因，不计入功能合计。"]))
    if value.get("official_observed_at"):
        lines.append(f"官网数据日：{value['official_day']}；观测：{value['official_observed_at'].replace('T', ' ')}（北京时间；全部密钥）。")
    return lines
