"""Keep actual provider token usage without saving prompts or message bodies."""

import math
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from datetime import time as day_time
from pathlib import Path
from uuid import UUID

from .token_pricing import price_feiyu_rows
from .official_billing import primary_billing_for_summary, primary_token_usage_for_summary

LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
FEATURE_LABELS = {
    "private_chat": "私聊对话",
    "group_chat": "群聊对话",
    "autonomous_plan": "自主决策（综合任务）",
    "bili_summary": "B站阅读与摘要",
    "bili_comment": "B站评论",
    "bili_other": "B站其他模型处理",
    "bili_vision": "B站图片识别",
    "heybox_comment": "小黑盒评论",
    "heybox_vision": "小黑盒图片识别",
    "journal_review": "手账筛选与学习",
    "memory": "记忆整理",
    "context_compression": "对话压缩",
    "video_media": "视频音画分析",
    "hltv": "HLTV新闻",
    "comment_sharing": "评论分享筛选",
    "qzone_draft": "QQ空间文案",
    "qzone_reply": "QQ空间评论与接话",
    "proactive_chat": "主动聊天文案",
    "qq_profile_vision": "QQ头像识图",
    "meme_analysis": "表情包识别",
    "voice_text": "语音文本处理",
    "other": "其他模型调用",
}
SCENE_LABELS = {
    "owner_private": "主人私聊",
    "group": "群聊",
    "cron": "定时主动任务",
    "heybox": "小黑盒后台",
    "background": "其他后台",
    "other": "其他场景",
}
FEATURES = frozenset(FEATURE_LABELS)
SCENES = frozenset(SCENE_LABELS)
FIELDS = frozenset(
    {
        "request_id",
        "at",
        "feature",
        "scene",
        "provider_id",
        "model",
        "status",
        "usage_known",
        "input_other",
        "input_cached",
        "output",
        "elapsed_ms",
    }
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}")
MAX_TOKENS = 1_000_000_000
MAX_TIMESTAMP = 4_102_444_800
COUNT_SQL = (
    "COUNT(*) AS calls,"
    "SUM(status='success') AS success,"
    "SUM(status='failed') AS failed,"
    "SUM(status='cancelled') AS cancelled,"
    "SUM(usage_known=1) AS known_usage_calls,"
    "SUM(usage_known=0) AS unknown_usage_calls,"
    "SUM(CASE WHEN usage_known=1 THEN input_other ELSE 0 END) AS input_other,"
    "SUM(CASE WHEN usage_known=1 THEN input_cached ELSE 0 END) AS input_cached,"
    "SUM(CASE WHEN usage_known=1 THEN output ELSE 0 END) AS output"
)
COUNT_FIELDS = (
    "calls",
    "success",
    "failed",
    "cancelled",
    "known_usage_calls",
    "unknown_usage_calls",
    "input_other",
    "input_cached",
    "output",
)


def _resolved_path(path):
    value = str(path.resolve())
    if os.name == "nt" and value.startswith("\\\\?\\"):
        value = "\\\\" + value[8:] if value.startswith("\\\\?\\UNC\\") else value[4:]
    return Path(value)


def _linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _valid_record(value):
    """Reject arbitrary payloads before any values reach the accounting ledger."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        return False
    request_id = value["request_id"]
    if not isinstance(request_id, str):
        return False
    try:
        if str(UUID(request_id)) != request_id:
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    for field in ("feature", "scene", "status"):
        if not isinstance(value[field], str):
            return False
    if (
        value["feature"] not in FEATURES
        or value["scene"] not in SCENES
        or value["status"] not in {"success", "failed", "cancelled"}
        or type(value["usage_known"]) is not bool
    ):
        return False
    for field in ("provider_id", "model"):
        item = value[field]
        if (
            not isinstance(item, str)
            or (item and not IDENTIFIER.fullmatch(item))
            or "://" in item
        ):
            return False
    at = value["at"]
    if (
        type(at) not in (int, float)
        or not 0 <= at <= MAX_TIMESTAMP
        or not math.isfinite(at)
    ):
        return False
    for field in ("input_other", "input_cached", "output"):
        if type(value[field]) is not int or not 0 <= value[field] <= MAX_TOKENS:
            return False
    if not value["usage_known"] and any(
        value[key] for key in ("input_other", "input_cached", "output")
    ):
        return False
    return type(value["elapsed_ms"]) is int and 0 <= value["elapsed_ms"] <= 86_400_000


def _counts(row):
    value = {key: int(row[key] or 0) for key in COUNT_FIELDS}
    value["input_total"] = value["input_other"] + value["input_cached"]
    value["total"] = value["input_total"] + value["output"]
    return value


class TokenUsageStore:
    """Persist bounded usage metadata at a fixed private installation path.

    An instance owns no long-lived SQLite connection. Each mutation is atomic,
    and both multiple threads and independently reloaded instances can safely
    share the same ledger. The earliest creation time survives plugin reloads.
    """

    def __init__(self, root):
        self.root = _resolved_path(Path(root))
        self.directory = self.root / "data" / "token-usage"
        self.path = self.directory / "usage.sqlite"
        self._created_at = time.time()
        self._lock = threading.RLock()
        # Initialization failure stays local; later calls can recover when the
        # directory becomes available, without recording fabricated usage.
        try:
            with closing(self._connect(create=True)):
                pass
        except (OSError, ValueError, sqlite3.Error):
            pass

    def _connect(self, create=False):
        for path in (self.root / "data", self.directory):
            if _linked(path) or not _resolved_path(path).is_relative_to(self.root):
                raise OSError("Unsafe usage ledger path")
        # SQLite may access these fixed sidecars in addition to its main file.
        for path in (
            self.path,
            Path(str(self.path) + "-journal"),
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            if _linked(path) or (path.exists() and path.stat().st_nlink > 1):
                raise OSError("Unsafe usage ledger file")
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() and not create:
            raise FileNotFoundError("Usage ledger not initialized")
        database = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout=5000")
        try:
            if create:
                database.executescript(
                    "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                    "CREATE TABLE IF NOT EXISTS requests("
                    "request_id TEXT PRIMARY KEY,at REAL NOT NULL,feature TEXT NOT NULL,"
                    "scene TEXT NOT NULL,provider_id TEXT NOT NULL,model TEXT NOT NULL,"
                    "status TEXT NOT NULL,usage_known INTEGER NOT NULL,"
                    "input_other INTEGER NOT NULL,input_cached INTEGER NOT NULL,"
                    "output INTEGER NOT NULL,elapsed_ms INTEGER NOT NULL);"
                    "CREATE INDEX IF NOT EXISTS requests_at ON requests(at);"
                )
                database.execute(
                    "INSERT OR IGNORE INTO metadata(key,value) VALUES('started_at',?)",
                    (str(self._created_at),),
                )
            return database
        except sqlite3.Error:
            database.close()
            raise

    def record(self, value):
        """Record one actual provider invocation, excluding all message content."""
        if not _valid_record(value):
            return {"ok": False, "reason": "invalid_usage_metadata"}
        columns = (
            "request_id",
            "at",
            "feature",
            "scene",
            "provider_id",
            "model",
            "status",
            "usage_known",
            "input_other",
            "input_cached",
            "output",
            "elapsed_ms",
        )
        values = tuple(value[key] for key in columns)
        try:
            with self._lock, closing(self._connect(create=True)) as database:
                database.execute("BEGIN IMMEDIATE")
                existing = database.execute(
                    "SELECT * FROM requests WHERE request_id=?", (value["request_id"],)
                ).fetchone()
                if existing is not None:
                    database.commit()
                    if tuple(existing[key] for key in columns) != values:
                        return {"ok": False, "reason": "request_id_conflict"}
                    return {"ok": True, "duplicate": True}
                database.execute(
                    "INSERT INTO requests(request_id,at,feature,scene,provider_id,model,status,"
                    "usage_known,input_other,input_cached,output,elapsed_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                database.commit()
            return {"ok": True, "duplicate": False}
        except (OSError, ValueError, sqlite3.Error):
            return {"ok": False, "reason": "token_usage_storage_unavailable"}

    def summary(self, day):
        """Aggregate a Shanghai natural day; missing usage remains unknown."""
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return {"ok": False, "reason": "invalid_usage_day"}
        try:
            date = datetime.strptime(day, "%Y-%m-%d").date()
            start = datetime.combine(date, day_time(), LOCAL_TIMEZONE).timestamp()
            end = start + 86_400
        except (ValueError, OverflowError, OSError):
            return {"ok": False, "reason": "invalid_usage_day"}
        try:
            with self._lock, closing(self._connect(create=True)) as database:
                database.execute("BEGIN")
                raw = database.execute(
                    "SELECT value FROM metadata WHERE key='started_at'"
                ).fetchone()
                observed = float(raw["value"])
                if not math.isfinite(observed) or observed < 0:
                    raise ValueError("Invalid observation metadata")
                observed_since = datetime.fromtimestamp(
                    observed, LOCAL_TIMEZONE
                ).isoformat(timespec="seconds")
                now = time.time()
                common = {
                    "day": day,
                    "from": datetime.fromtimestamp(start, LOCAL_TIMEZONE).isoformat(timespec="seconds"),
                    "until": datetime.fromtimestamp(min(end, now), LOCAL_TIMEZONE).isoformat(timespec="seconds"),
                    "observed_since": observed_since,
                    "partial_day": start < observed < end or end > now,
                }
                if end <= observed or start > now:
                    return {
                        "ok": True,
                        "not_observed": True,
                        "reason": "not_observed",
                        **common,
                    }
                bounds = (start, end)
                totals = _counts(
                    database.execute(
                        "SELECT " + COUNT_SQL + " FROM requests WHERE at>=? AND at<?",
                        bounds,
                    ).fetchone()
                )
                grouped = {}
                for name, columns in (
                    ("by_feature", ("feature",)),
                    ("by_scene", ("scene",)),
                    ("by_model", ("provider_id", "model")),
                ):
                    # Column names are fixed here, never taken from a caller.
                    group = ",".join(columns)
                    rows = database.execute(
                        f"SELECT {group},{COUNT_SQL} FROM requests WHERE at>=? AND at<? "
                        f"GROUP BY {group} ORDER BY {group}",
                        bounds,
                    ).fetchall()
                    grouped[name] = [
                        {**{key: row[key] for key in columns}, **_counts(row)}
                        for row in rows
                    ]
                pricing_rows = database.execute(
                    "SELECT at,feature,model,usage_known,input_other,input_cached,output "
                    "FROM requests WHERE at>=? AND at<?", bounds,
                ).fetchall()
                database.commit()
            return {
                "ok": True,
                "not_observed": False,
                **common,
                "totals": totals,
                "pricing": price_feiyu_rows(pricing_rows),
                **grouped,
            }
        except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error):
            return {
                "ok": False,
                "reason": "token_usage_storage_unavailable",
                "day": day,
            }

    def window_summary(self, hours, *, now=None):
        """Aggregate a rolling duration; unlike daily reports this does not send."""
        current = time.time() if now is None else now
        if (type(hours) not in (int, float) or not math.isfinite(hours) or not 0.5 <= hours <= 720
                or type(current) not in (int, float) or not math.isfinite(current)):
            return {"ok": False, "reason": "invalid_usage_window"}
        start, end = current - hours * 3600, current
        common = {"window_hours": hours, "period_label": f"过去{hours:g}小时",
                  "from": datetime.fromtimestamp(start, LOCAL_TIMEZONE).isoformat(timespec="seconds"),
                  "until": datetime.fromtimestamp(end, LOCAL_TIMEZONE).isoformat(timespec="seconds")}
        try:
            with self._lock, closing(self._connect(create=True)) as database:
                database.execute("BEGIN")
                raw = database.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()
                observed = float(raw["value"])
                if not math.isfinite(observed) or observed < 0:
                    raise ValueError("Invalid observation metadata")
                common.update(observed_since=datetime.fromtimestamp(
                    observed, LOCAL_TIMEZONE).isoformat(timespec="seconds"), partial_day=start < observed < end)
                if end <= observed:
                    return {"ok": True, "not_observed": True, **common}
                bounds = (start, end)
                totals = _counts(database.execute(
                    "SELECT " + COUNT_SQL + " FROM requests WHERE at>=? AND at<?", bounds
                ).fetchone())
                grouped = {}
                for name, columns in (("by_feature", ("feature",)), ("by_scene", ("scene",)),
                                      ("by_model", ("provider_id", "model"))):
                    group = ",".join(columns)
                    rows = database.execute(
                        f"SELECT {group},{COUNT_SQL} FROM requests WHERE at>=? AND at<? "
                        f"GROUP BY {group} ORDER BY {group}", bounds).fetchall()
                    grouped[name] = [{**{key: row[key] for key in columns}, **_counts(row)} for row in rows]
                pricing_rows = database.execute(
                    "SELECT at,feature,model,usage_known,input_other,input_cached,output "
                    "FROM requests WHERE at>=? AND at<?", bounds,
                ).fetchall()
                database.commit()
            return {"ok": True, "not_observed": False, **common, "totals": totals,
                    "pricing": price_feiyu_rows(pricing_rows), **grouped}
        except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error):
            return {"ok": False, "reason": "token_usage_storage_unavailable", **common}


def build_usage_report(summary):
    """Produce a fixed Chinese daily report, without another model request."""
    if not isinstance(summary, dict):
        return "肥鱼用量日报：本机统计暂不可用。"
    day = summary.get("day", "")
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        day = "当日"
    period = summary.get("period_label")
    if isinstance(period, str) and re.fullmatch(r"过去\d+(?:\.\d+)?小时", period):
        day = period
    heading = "肥鱼用量统计" if "window_hours" in summary else "肥鱼用量日报"
    if summary.get("not_observed"):
        return f"{heading}（{day}）\n此时段尚未开始统计，没有可核实的历史用量。"
    if summary.get("ok") is not True:
        return f"{heading}（{day}）\n本机统计暂不可用。"
    totals = summary["totals"]
    billing = primary_billing_for_summary(summary)
    primary_tokens = primary_token_usage_for_summary(summary)
    cost = (f"{billing['label']}：¥{float(billing['cost_cny']):,.2f}。"
            if billing["status"] in {"confirmed", "estimated"} else "完整时段费用待核实。")
    official_tokens = primary_tokens["source"] == "official"
    headline = (f"官网核实 {primary_tokens['total_tokens']:,} token · 本机记录 {totals['calls']:,} 次模型调用。"
                if official_tokens else f"本机已记录 {totals['total']:,} token · {totals['calls']:,} 次模型调用。")
    parts = primary_tokens
    composition_label = ("官网用量构成" if parts["composition_source"] == "official"
                         else "本机已记录部分构成" if official_tokens else "本机用量构成")
    composition = (f"{composition_label}：非缓存输入 {parts['input_other']:,}，缓存输入 {parts['input_cached']:,}，输出 {parts['output']:,}。"
                   if parts["composition_source"] != "unavailable" else "用量构成待核实。")
    lines = [
        f"{heading}（{day}）",
        headline,
        cost,
        composition,
    ]
    if summary.get("partial_day"):
        lines.append("用量分类为本机已记录的部分时段。" if billing["status"] == "confirmed"
                     else "部分时段记录，完整时段费用待核实。")
        try:
            observed = datetime.fromisoformat(summary["observed_since"])
            begin = datetime.fromisoformat(summary["from"])
            if observed.tzinfo is None or begin.tzinfo is None:
                raise ValueError("timezone_required")
            actual = max(begin, observed).astimezone(LOCAL_TIMEZONE)
            lines.append("本机实际记录自 " + actual.strftime("%Y-%m-%d %H:%M") + " 开始（北京时间）。")
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    if "window_hours" in summary:
        lines.append(f"时段：{summary['from']} 至 {summary['until']}（北京时间）。")
    lines.append("功能明细（本机已记录部分）：" if official_tokens or summary.get("partial_day") or totals["unknown_usage_calls"] else "功能明细：")
    costs = {row['feature']: row['cost_cny'] for row in summary.get('pricing', {}).get('by_feature', [])}
    for row in sorted(
        summary.get("by_feature", []),
        key=lambda item: (-item["total"], item["feature"]),
    )[:20]:
        label = FEATURE_LABELS.get(row["feature"], FEATURE_LABELS["other"])
        if row["known_usage_calls"]:
            text = (
                f"• {label}：已知 {row['total']:,} token，{row['calls']:,} 次"
                f"（输入 {row['input_other']:,}+缓存 {row['input_cached']:,}，输出 {row['output']:,}）"
            )
        else:
            text = f"• {label}：{row['calls']:,} 次，尚无可核实 token 用量"
        if row["unknown_usage_calls"]:
            text += f"，其中 {row['unknown_usage_calls']:,} 次未回传用量"
        if row['feature'] in costs:
            text += f" · ¥{costs[row['feature']]:,.2f}"
        lines.append(text)
    if not summary.get("by_feature"):
        lines.append("• 统计时段内没有模型调用。")
    unknown = totals["unknown_usage_calls"]
    if unknown:
        lines.append(f"另有 {unknown:,} 次用量待核实。")
        if not summary.get("partial_day") and billing["status"] == "unverified":
            lines.append("完整时段费用待核实。")
    if any(row['feature'] == 'autonomous_plan' for row in summary.get('by_feature', [])):
        lines.append("自主决策包含聊天、刷帖评论、分享、空间、手账及头像签名调整。")
    return "\n".join(lines)
