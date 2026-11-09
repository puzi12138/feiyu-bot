"""Local-only receipt portal for files exported from DeepSeek's official UI.

This service never signs in to DeepSeek and never reads a browser session. It
stores validated CSV/ZIP uploads under content-derived names for later review.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import queue
import re
import secrets
import socket
import stat
import struct
import threading
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_PORT = 16302
DEFAULT_STORAGE = Path("D:/QQBot/data/token-usage/official-imports")
DEFAULT_REPORTS = Path("D:/QQBot/data/token-usage/reconciliation-reports")
DEFAULT_LEDGER = Path("D:/QQBot/data/token-usage/usage.sqlite")
DEFAULT_CONFIG = Path("D:/QQBot/data/astrbot/cmd_config.json")
MAX_UPLOAD = 20 * 1024 * 1024
MAX_ZIP_ENTRIES = 32
MAX_MEMBER_SIZE = 25 * 1024 * 1024
MAX_EXPANDED_SIZE = 50 * 1024 * 1024
READ_TIMEOUT = 20
MAX_COMMAND = 1024
STORED_NAME = re.compile(r"([a-f0-9]{64})\.(csv|zip)\Z")
MAX_CSV_FIELD = 1024 * 1024
MAX_REPORT = 2 * 1024 * 1024
REPORT_SCHEMA = "deepseek-daily-20261026-v1"
REPORT_STATUSES = frozenset(("matched", "difference", "history_incomplete", "unsupported_schema"))
TOKEN_FIELDS = ("input_other", "input_cached", "input_total", "output", "total", "request_count", "function_calls")
DELTA_FIELDS = ("input_other", "input_cached", "input_total", "output", "total", "request_count")
MONEY_FIELDS = ("cost_cny", "model_cost_cny", "tool_cost_cny", "detail_cost_cny")
LIMITATION_CODES = frozenset((
    "unsupported_official_schema", "local_ledger_unavailable", "local_history_starts_after_day_start",
    "export_time_unverified", "official_current_day_unfinished", "key_match_unknown", "key_match_partial",
    "key_match_unmatched", "key_filter_unverified", "official_companion_csv_missing", "official_cost_crosscheck_failed",
    "local_unknown_usage", "local_price_split_unverified", "tool_calls_not_recorded_in_local_token_ledger",
    "official_current_day_may_lag_export_time", "official_day_missing", "official_selected_range_mismatch", "local_tool_cost_unverified",
    "official_hour_missing",
))
csv.field_size_limit(MAX_CSV_FIELD)

BROWSER_MESSAGES = {
    "unavailable": "独立浏览器尚未准备好，可先手动上传账单。",
    "closed": "点击打开独立浏览器，在 DeepSeek 官网自行登录一次。",
    "opening": "正在打开独立浏览器，请稍候。",
    "login_required": "请在独立浏览器的 DeepSeek 官方页面完成登录。",
    "ready": "官网页面已就绪，请确认日期范围后导出。",
    "export_available": "可申请导出官网当前选择的账单范围。",
    "exporting": "正在申请并接收官网账单，请稍候。",
    "received_pending_review": "官方账单已接收，等待核对。",
    "export_needs_range_review": "官网账单已保存，日期范围由文件核定；核对结果见下方，无需重复导出。",
    "error": "浏览器操作暂未完成，可重新打开独立浏览器后重试。",
}


def browser_snapshot(manager) -> dict:
    """Expose fixed UI text and an allowlisted state, never raw browser details."""
    try:
        raw = manager.status() if manager is not None else {}
        state = raw.get("state", "unavailable") if isinstance(raw, dict) else "error"
        if state not in BROWSER_MESSAGES:
            state = "error"
    except Exception:
        state = "error"
        raw = {}
    snapshot = {
        "state": state,
        "message": BROWSER_MESSAGES[state],
        "can_open": manager is not None and state not in ("opening", "exporting"),
        "can_export": manager is not None and state in ("ready", "export_available", "received_pending_review", "export_needs_range_review"),
    }
    if isinstance(raw, dict) and state == "closed" and raw.get("automatic_open_deferred") is True:
        snapshot["automatic_open_deferred"] = True
    if isinstance(raw, dict) and isinstance(raw.get("last_export_at"), str) and len(raw["last_export_at"]) <= 40:
        try:
            stamp = datetime.fromisoformat(raw["last_export_at"])
            if stamp.tzinfo is not None:
                snapshot["last_export_at"] = stamp.isoformat()
        except ValueError:
            pass
    return snapshot


def _safe_export_observation(metadata: dict | None) -> dict:
    """Keep only non-identifying range evidence from an official-page export."""
    raw = metadata if isinstance(metadata, dict) else {}
    raw_range = raw.get("date_range") if isinstance(raw.get("date_range"), dict) else {}
    date_range = {"status": "unknown", "start": None, "end": None, "inclusive": None}
    if raw_range.get("status") == "verified":
        try:
            start = raw_range.get("start")
            end = raw_range.get("end")
            if (
                isinstance(start, str)
                and isinstance(end, str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)
                and datetime.strptime(start, "%Y-%m-%d") <= datetime.strptime(end, "%Y-%m-%d")
            ):
                date_range = {
                    "status": "verified", "start": start, "end": end,
                    "inclusive": raw_range.get("inclusive") if isinstance(raw_range.get("inclusive"), bool) else None,
                }
        except ValueError:
            pass
    observed_at = None
    if isinstance(raw.get("observed_at"), str) and len(raw["observed_at"]) <= 40:
        try:
            observed = datetime.fromisoformat(raw["observed_at"])
            if observed.tzinfo is not None:
                observed_at = observed.isoformat()
        except ValueError:
            pass
    return {
        "source": "official_visible_export",
        "date_range": date_range,
        "timezone": "UTC+08:00" if raw.get("timezone") == "UTC+08:00" else "unknown",
        "key_filter": raw.get("key_filter") if raw.get("key_filter") in ("all", "filtered", "unknown") else "unknown",
        "trigger": raw.get("trigger") if raw.get("trigger") in ("manual", "scheduled") else "unknown",
        "observed_at": observed_at,
        # Receipt/range evidence alone never proves all billing lines reconcile.
        "automatic_compare_allowed": False,
    }


def _report_enum(value, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("Unsupported report field")
    return value


def _report_count(value, *, signed=False):
    if type(value) is not int or not (-10**18 if signed else 0) <= value <= 10**18:
        raise ValueError("Invalid report count")
    return value


def _report_money(value, *, signed=False):
    if value is None:
        return None
    pattern = r"-?\d+(?:\.\d+)?" if signed else r"\d+(?:\.\d+)?"
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(pattern, value):
        raise ValueError("Invalid report amount")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or abs(amount) > 10**18:
            raise ValueError("Invalid report amount")
    except InvalidOperation as exc:
        raise ValueError("Invalid report amount") from exc
    return format(amount, "f")


def _report_time(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid report date")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Invalid report date")
    return stamp.isoformat()


def _report_limitations(values):
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("Invalid report limitations")
    return sorted(set(_report_enum(value, LIMITATION_CODES) for value in values))


def _report_totals(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid report totals")
    return {field: _report_count(value.get(field)) for field in TOKEN_FIELDS}


def _report_coverage(official, scope, status):
    """Preserve verified partial-hour coverage without exposing source cells."""
    coverage = _report_enum(official.get("coverage", "daily_export"), (
        "daily_export", "complete_hourly_days", "partial_current_day_hourly"))
    missing = official.get("missing_hour_starts", [])
    if not isinstance(missing, list) or len(missing) > 24:
        raise ValueError("Invalid official hourly coverage")
    if coverage != "partial_current_day_hourly":
        if missing:
            raise ValueError("Unexpected missing official hours")
        return {"coverage": coverage, "missing_hour_starts": []}
    try:
        start = datetime.fromisoformat(official["start"])
        end = datetime.fromisoformat(official["end"])
        exported = datetime.fromisoformat(scope["exported_at"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid partial official day") from None
    if (status == "matched" or official.get("day_count") != 1
            or official.get("contiguous_days") is not True
            or any(stamp.utcoffset() != timedelta(hours=8) for stamp in (start, end, exported))
            or start.time() != datetime.min.time() or end != start + timedelta(days=1)
            or not start <= exported < end):
        raise ValueError("Invalid partial official day")
    current_hour = exported.replace(minute=0, second=0, microsecond=0)
    safe_missing = []
    for value in missing:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00\+08:00", value):
            raise ValueError("Invalid missing official hour")
        try:
            hour = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("Invalid missing official hour") from None
        if not start <= hour <= current_hour:
            raise ValueError("Missing official hour outside export")
        safe_missing.append(hour.isoformat())
    if safe_missing != sorted(set(safe_missing)):
        raise ValueError("Unordered missing official hours")
    return {"coverage": coverage, "missing_hour_starts": safe_missing}


def _report_unreported_hours(values, official, scope):
    """Allow diagnostics for a closed single-day export, never zero-fill hours."""
    if not isinstance(values, list) or len(values) > 24:
        raise ValueError("Invalid unreported official hours")
    if not values:
        return []
    try:
        start = datetime.fromisoformat(official["start"])
        end = datetime.fromisoformat(official["end"])
        exported = datetime.fromisoformat(scope["exported_at"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid closed official day") from None
    if (official.get("coverage") != "daily_export" or official.get("missing_hour_starts")
            or official.get("day_count") != 1 or official.get("contiguous_days") is not True
            or any(stamp.utcoffset() != timedelta(hours=8) for stamp in (start, end, exported))
            or start.time() != datetime.min.time() or end != start + timedelta(days=1)
            or exported < end + timedelta(minutes=5) or exported > datetime.now(timezone.utc)):
        raise ValueError("Invalid closed official day")
    safe = []
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00\+08:00", value):
            raise ValueError("Invalid unreported official hour")
        try:
            hour = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("Invalid unreported official hour") from None
        if not start <= hour < end:
            raise ValueError("Unreported official hour outside day")
        safe.append(hour.isoformat())
    if safe != sorted(set(safe)):
        raise ValueError("Unordered unreported official hours")
    return safe


def safe_reconciliation_report(raw) -> dict:
    """Strict public schema. Unknown fields cannot escape from parser output."""
    if not isinstance(raw, dict) or raw.get("schema_version") != REPORT_SCHEMA:
        raise ValueError("Unsupported report schema")
    status = _report_enum(raw.get("status"), REPORT_STATUSES)
    result = {"schema_version": REPORT_SCHEMA, "status": status, "limitations": _report_limitations(raw.get("limitations")), "days": []}
    days = raw.get("days")
    if not isinstance(days, list) or len(days) > 3660:
        raise ValueError("Invalid report days")
    if status == "unsupported_schema":
        if days:
            raise ValueError("Invalid unsupported report")
        return result
    source_scope = raw.get("scope")
    if not isinstance(source_scope, dict) or not isinstance(source_scope.get("key_match"), dict):
        raise ValueError("Missing report scope")
    match = source_scope["key_match"]
    safe_match = {"status": _report_enum(match.get("status"), ("unknown", "exact", "partial", "unmatched"))}
    for field in ("official_keys", "configured_keys", "exact_matches", "partial_matches", "unmatched_keys"):
        safe_match[field] = _report_count(match.get(field))
    result["scope"] = {
        "timezone": _report_enum(source_scope.get("timezone"), ("UTC+08:00",)),
        "key_match": safe_match,
        "exported_at": _report_time(source_scope.get("exported_at"), nullable=True),
        "key_filter": _report_enum(source_scope.get("key_filter"), ("all", "filtered", "unknown")),
        "official_interval_semantics": _report_enum(source_scope.get("official_interval_semantics"), ("daily_start_inclusive_end_exclusive",)),
        "local_history_start": _report_time(source_scope.get("local_history_start"), nullable=True),
    }
    official = raw.get("official")
    if not isinstance(official, dict) or official.get("schema_version") != REPORT_SCHEMA:
        raise ValueError("Invalid official summary")
    result["official"] = {
        "schema_version": REPORT_SCHEMA,
        "timezone": _report_enum(official.get("timezone"), ("UTC+08:00",)),
        "currency": _report_enum(official.get("currency"), ("CNY",)),
        "start": _report_time(official.get("start"), nullable=True),
        "end": _report_time(official.get("end"), nullable=True),
        "totals": _report_totals(official.get("totals")),
        "cost_crosscheck": _report_enum(official.get("cost_crosscheck"), ("consistent", "unverified")),
    }
    if type(official.get("contiguous_days")) is not bool:
        raise ValueError("Invalid official day coverage")
    result["official"]["contiguous_days"] = official["contiguous_days"]
    for field in ("day_count", "key_count", "unique_csv_count", "duplicate_csv_count"):
        result["official"][field] = _report_count(official.get(field))
    for field in MONEY_FIELDS:
        result["official"][field] = _report_money(official.get(field))
    result["official"].update(_report_coverage(result["official"] | {
        "coverage": official.get("coverage", "daily_export"),
        "missing_hour_starts": official.get("missing_hour_starts", []),
    }, result["scope"], status))
    result["official"]["unreported_hour_starts"] = _report_unreported_hours(
        official.get("unreported_hour_starts", []), result["official"], result["scope"])
    local = raw.get("local")
    if local is not None:
        if not isinstance(local, dict):
            raise ValueError("Invalid local summary")
        result["local"] = {"totals": _report_totals(local.get("totals"))}
    for day in days:
        if not isinstance(day, dict):
            raise ValueError("Invalid report day")
        item = {
            "start": _report_time(day.get("start")), "end": _report_time(day.get("end")),
            "local_cutoff": _report_time(day.get("local_cutoff")),
            "status": _report_enum(day.get("status"), ("matched", "difference", "history_incomplete")),
            "limitations": _report_limitations(day.get("limitations")),
        }
        day_official, day_local, deltas = day.get("official"), day.get("local"), day.get("deltas")
        if not all(isinstance(value, dict) for value in (day_official, day_local, deltas)):
            raise ValueError("Invalid report detail")
        item["official"] = {"totals": _report_totals(day_official.get("totals"))}
        for field in MONEY_FIELDS:
            item["official"][field] = _report_money(day_official.get(field))
        item["local"] = {
            "totals": _report_totals(day_local.get("totals")),
            "known_usage_calls": _report_count(day_local.get("known_usage_calls")),
            "unknown_usage_calls": _report_count(day_local.get("unknown_usage_calls")),
            "model_cost_cny": _report_money(day_local.get("model_cost_cny")),
            "cost_basis": _report_enum(day_local.get("cost_basis"), ("official_export_unit_prices_applied", "unknown")),
        }
        token_deltas = deltas.get("tokens")
        if not isinstance(token_deltas, dict):
            raise ValueError("Invalid report delta")
        item["deltas"] = {
            "tokens": {field: _report_count(token_deltas.get(field), signed=True) for field in DELTA_FIELDS},
            "model_cost_cny": _report_money(deltas.get("model_cost_cny"), signed=True),
        }
        if item["status"] == "matched" and item["limitations"]:
            raise ValueError("Unverified match")
        if item["status"] == "matched" and (
            any(item["deltas"]["tokens"].values())
            or item["deltas"]["model_cost_cny"] is None
            or Decimal(item["deltas"]["model_cost_cny"]) != 0
        ):
            raise ValueError("Nonzero matched difference")
        if result["official"]["coverage"] == "partial_current_day_hourly" and (
            item["status"] == "matched" or item["start"] != result["official"]["start"]
            or item["end"] != result["official"]["end"] or len(days) != 1
        ):
            raise ValueError("Invalid partial official detail")
        result["days"].append(item)
    if status == "matched" and (
        safe_match["status"] != "exact" or not days or any(day["status"] != "matched" for day in result["days"])
        or not result["official"]["contiguous_days"]
        or safe_match["official_keys"] == 0 or safe_match["exact_matches"] != safe_match["official_keys"]
        or safe_match["partial_matches"] != 0 or safe_match["unmatched_keys"] != 0
    ):
        raise ValueError("Unverified match")
    return result


def reconciliation_summary(report: dict) -> dict:
    official = report.get("official", {})
    local = report.get("local", {})
    scope = report.get("scope", {})
    summary = {
        "status": report["status"],
        "start": official.get("start"), "end": official.get("end"),
        "official_tokens": official.get("totals", {}).get("total"),
        "local_tokens": local.get("totals", {}).get("total"),
        "official_cost_cny": official.get("cost_cny"),
        "coverage": official.get("coverage", "daily_export"),
        "missing_hour_starts": list(official.get("missing_hour_starts", [])),
        "unreported_hour_starts": list(official.get("unreported_hour_starts", [])),
        "key_match": scope.get("key_match", {}).get("status", "unknown"),
        "local_history_start": scope.get("local_history_start"),
        "day_counts": {status: sum(day["status"] == status for day in report["days"]) for status in ("matched", "difference", "history_incomplete")},
        "focus_day": None,
    }
    exported_at = scope.get("exported_at")
    if exported_at is not None and summary["coverage"] != "partial_current_day_hourly":
        exported = datetime.fromisoformat(exported_at)
        complete_days = [day for day in report["days"] if datetime.fromisoformat(day["end"]) <= exported]
        if complete_days:
            day = max(complete_days, key=lambda item: datetime.fromisoformat(item["end"]))
            summary["focus_day"] = {
                "start": day["start"], "end": day["end"], "status": day["status"],
                "official_tokens": day["official"]["totals"]["total"],
                "local_known_tokens": day["local"]["totals"]["total"],
                "token_delta_local_minus_official": day["deltas"]["tokens"]["total"],
                "official_cost_cny": day["official"]["cost_cny"],
                "unknown_usage_calls": day["local"]["unknown_usage_calls"],
            }
    return summary


class UploadError(ValueError):
    """An invalid upload, described without echoing any file content."""


def validate_csv(data: bytes) -> None:
    if not data or b"\x00" in data:
        raise UploadError("CSV 为空或不是可读取的文本。")
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        raise UploadError("CSV 编码无法识别，请上传官网原始导出文件。")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise UploadError("CSV 含有非文本内容。")
    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(rows, None)
        if not header or len(header) < 2 or not all(cell.strip() for cell in header):
            raise UploadError("CSV 缺少有效表头，请上传官网原始导出文件。")
        for row in rows:
            if row and len(row) != len(header):
                raise UploadError("CSV 列数不一致，请上传官网原始导出文件。")
    except (csv.Error, UnicodeError) as exc:
        raise UploadError("CSV 格式无法识别，请上传官网原始导出文件。") from exc


def _member_name(info: zipfile.ZipInfo) -> str:
    name = info.orig_filename
    normalized = name.replace("\\", "/")
    parts = normalized.rstrip("/").split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or any(":" in part or part.rstrip(". ") != part for part in parts)
        or "\x00" in name
    ):
        raise UploadError("ZIP 包含不安全的文件路径。")
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise UploadError("ZIP 不支持链接或特殊文件。")
    if info.flag_bits & 1:
        raise UploadError("不支持加密 ZIP，请上传官网原始导出文件。")
    return normalized


def validate_zip(data: bytes) -> None:
    if not data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        raise UploadError("文件不是有效 ZIP。")
    # Reject large/ZIP64 directory tables before ZipFile allocates their entries.
    end_record = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end_record < 0 or len(data) - end_record < 22:
        raise UploadError("ZIP 中央目录无效。")
    disk, directory_disk, entries_on_disk, entries_total, _, _, comment_size = struct.unpack_from(
        "<4H2IH", data, end_record + 4
    )
    if (
        disk != 0
        or directory_disk != 0
        or entries_on_disk != entries_total
        or not 0 < entries_total <= MAX_ZIP_ENTRIES
        or end_record + 22 + comment_size != len(data)
    ):
        raise UploadError("ZIP 文件条目超过上限或目录格式无效。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_ZIP_ENTRIES:
                raise UploadError("ZIP 文件条目超过上限或压缩包为空。")
            names: set[str] = set()
            csv_count = 0
            declared_total = 0
            actual_total = 0
            for info in members:
                normalized = _member_name(info)
                canonical = normalized.rstrip("/").casefold()
                if canonical in names:
                    raise UploadError("ZIP 含有重复文件名。")
                names.add(canonical)
                if info.is_dir():
                    if info.file_size:
                        raise UploadError("ZIP 目录条目无效。")
                    continue
                if info.file_size < 0 or info.file_size > MAX_MEMBER_SIZE:
                    raise UploadError("ZIP 内单个文件超过 25 MB。")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise UploadError("ZIP 压缩方式不受支持，请上传官网原始导出文件。")
                declared_total += info.file_size
                if declared_total > MAX_EXPANDED_SIZE:
                    raise UploadError("ZIP 展开后超过 50 MB。")
                leaf = normalized.rsplit("/", 1)[-1].casefold()
                is_csv = leaf.endswith(".csv")
                is_readme = leaf in ("readme", "readme.txt", "readme.md")
                if not is_csv and not is_readme:
                    raise UploadError("ZIP 只接受 CSV 和可选的 README 说明文件。")
                chunks = []
                actual_size = 0
                with archive.open(info) as member:
                    while True:
                        chunk = member.read(64 * 1024)
                        if not chunk:
                            break
                        actual_size += len(chunk)
                        actual_total += len(chunk)
                        if actual_size > MAX_MEMBER_SIZE or actual_total > MAX_EXPANDED_SIZE:
                            raise UploadError("ZIP 实际展开大小超过上限。")
                        chunks.append(chunk)
                if actual_size != info.file_size:
                    raise UploadError("ZIP 文件大小校验失败。")
                contents = b"".join(chunks)
                if is_csv:
                    validate_csv(contents)
                    csv_count += 1
                elif b"\x00" in contents:
                    raise UploadError("README 说明文件不是文本。")
            if not csv_count:
                raise UploadError("ZIP 中没有 CSV 账单。")
    except UploadError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, EOFError, zlib.error) as exc:
        raise UploadError("ZIP 无法读取或校验失败，请上传官网原始导出文件。") from exc


def validate_upload(data: bytes, file_format: str) -> None:
    if not data or len(data) > MAX_UPLOAD:
        raise UploadError("请选择不超过 20 MB 的 ZIP 或 CSV。")
    if file_format == "csv":
        validate_csv(data)
    elif file_format == "zip":
        validate_zip(data)
    else:
        raise UploadError("只接受 ZIP 或 CSV 文件。")


class ReceiptStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _receipt(path: Path) -> dict:
        info = path.stat()
        return {
            "filename": path.name,
            "received_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
            "size": info.st_size,
            "sha256": path.stem,
        }

    def list_receipts(self) -> list[dict]:
        with self._lock:
            receipts = [
                self._receipt(path)
                for path in self.directory.iterdir()
                if STORED_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink()
            ]
        return sorted(receipts, key=lambda receipt: receipt["received_at"], reverse=True)

    def save(self, data: bytes, file_format: str) -> tuple[dict, bool]:
        digest = hashlib.sha256(data).hexdigest()
        path = self.directory / f"{digest}.{file_format}"
        with self._lock:
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file():
                    raise UploadError("本机保存路径不可用，请稍后再试。")
                existing_hash = hashlib.sha256()
                existing_size = 0
                with path.open("rb") as existing:
                    while chunk := existing.read(64 * 1024):
                        existing_size += len(chunk)
                        if existing_size > MAX_UPLOAD:
                            raise UploadError("本机已有文件校验失败，请保留原始账单并联系核对。")
                        existing_hash.update(chunk)
                if existing_hash.hexdigest() != digest:
                    raise UploadError("本机已有文件校验失败，请保留原始账单并联系核对。")
                return self._receipt(path), True
            temporary = self.directory / f".upload-{secrets.token_hex(16)}.tmp"
            try:
                with temporary.open("xb") as output:
                    output.write(data)
                    output.flush()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return self._receipt(path), False

    def save_export_observation(self, digest: str, metadata: dict | None) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise UploadError("账单摘要无效。")
        observation = _safe_export_observation(metadata)
        sidecar = self.directory / f"{digest}.metadata.json"
        with self._lock:
            observations = []
            if sidecar.exists() or sidecar.is_symlink():
                if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 1024 * 1024:
                    raise UploadError("账单范围记录校验失败，请保留原始账单并联系核对。")
                try:
                    saved = json.loads(sidecar.read_text(encoding="utf-8"))
                    if saved.get("schema_version") != 1 or saved.get("receipt_sha256") != digest:
                        raise ValueError
                    stored = saved.get("observations")
                    if not isinstance(stored, list) or len(stored) > 500:
                        raise ValueError
                    # Re-sanitize even internally saved data before writing it back.
                    observations = [_safe_export_observation(item) for item in stored]
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError, AttributeError, RecursionError) as exc:
                    raise UploadError("账单范围记录校验失败，请保留原始账单并联系核对。") from exc
            if observation not in observations:
                if len(observations) >= 500:
                    raise UploadError("这份账单的范围记录已满，请保留原始文件并联系核对。")
                observations.append(observation)
            payload = {"schema_version": 1, "receipt_sha256": digest, "observations": observations}
            temporary = self.directory / f".metadata-{secrets.token_hex(16)}.tmp"
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as output:
                    json.dump(payload, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                temporary.replace(sidecar)
            finally:
                temporary.unlink(missing_ok=True)

    def latest_export_observation(self, digest: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise UploadError("账单摘要无效。")
        sidecar = self.directory / f"{digest}.metadata.json"
        with self._lock:
            if not sidecar.exists():
                return _safe_export_observation(None)
            if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 1024 * 1024:
                raise UploadError("账单范围记录校验失败。")
            try:
                saved = json.loads(sidecar.read_text(encoding="utf-8"))
                observations = saved.get("observations")
                if saved.get("schema_version") != 1 or saved.get("receipt_sha256") != digest or not isinstance(observations, list) or len(observations) > 500:
                    raise ValueError
                return _safe_export_observation(observations[-1] if observations else None)
            except (OSError, UnicodeError, ValueError, AttributeError, RecursionError) as exc:
                raise UploadError("账单范围记录校验失败。") from exc


class ReconciliationManager:
    """Process one receipt snapshot at a time without blocking HTTP uploads."""

    def __init__(self, store: ReceiptStore, reconciler, *, reports_dir=DEFAULT_REPORTS, ledger_path=DEFAULT_LEDGER, config_path=DEFAULT_CONFIG):
        self.store = store
        self.reconciler = reconciler
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path, self.config_path = Path(ledger_path), Path(config_path) if config_path is not None else None
        self._queue = queue.Queue(maxsize=64)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pending: set[str] = set()
        self._records: dict[str, dict] = {}
        self._latest: str | None = None
        self._latest_at = ""
        self._scan_thread = None
        self._thread = threading.Thread(target=self._run, name="billing-reconciliation", daemon=False)
        self._thread.start()

    def submit(self, receipt: dict, metadata: dict | None = None, *, wait=False) -> dict:
        name = receipt.get("filename") if isinstance(receipt, dict) else None
        match = STORED_NAME.fullmatch(name) if isinstance(name, str) else None
        if not match or self._stop.is_set():
            return {"state": "error"}
        digest = match.group(1)
        with self._lock:
            received_at = receipt.get("received_at", "")
            if isinstance(received_at, str) and received_at >= self._latest_at:
                self._latest, self._latest_at = digest, received_at
            if digest in self._pending:
                return dict(self._records[digest])
            self._pending.add(digest)
            self._records[digest] = {"state": "queued", "report_id": digest}
        job = (name, _safe_export_observation(metadata) if metadata is not None else None)
        try:
            if wait:
                while not self._stop.is_set():
                    try:
                        self._queue.put(job, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                else:
                    raise queue.Full
            else:
                self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._pending.discard(digest)
                self._records[digest] = {"state": "error", "report_id": digest}
        return self.status()

    def scan_existing(self):
        def scan():
            try:
                for receipt in self.store.list_receipts():
                    if self._stop.is_set():
                        break
                    self.submit(receipt, wait=True)
            except Exception:
                # The HTTP state remains fixed and contains no filesystem errors.
                pass

        self._scan_thread = threading.Thread(target=scan, name="billing-receipt-scan", daemon=True)
        self._scan_thread.start()

    def status(self) -> dict:
        with self._lock:
            if self._latest is None:
                return {"state": "waiting_for_bill"}
            return dict(self._records.get(self._latest, {"state": "waiting_for_bill"}))

    def _run(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                name, metadata = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            digest, file_format = STORED_NAME.fullmatch(name).groups()
            with self._lock:
                self._records[digest] = {"state": "checking", "report_id": digest}
            try:
                source = self.store.directory / name
                if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= MAX_UPLOAD:
                    raise ValueError("Invalid receipt")
                with source.open("rb") as incoming:
                    contents = incoming.read(MAX_UPLOAD + 1)
                if hashlib.sha256(contents).hexdigest() != digest:
                    raise ValueError("Receipt digest mismatch")
                validate_upload(contents, file_format)
                observed = metadata if metadata is not None else self.store.latest_export_observation(digest)
                raw = self.reconciler(source, ledger_path=self.ledger_path, config_path=self.config_path, metadata=observed)
                report = safe_reconciliation_report(raw)
                generated_at = datetime.now(timezone.utc).isoformat()
                envelope = {"report_id": digest, "generated_at": generated_at, "report": report}
                encoded = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
                if len(encoded) > MAX_REPORT:
                    raise ValueError("Report too large")
                target = self.reports_dir / f"{digest}.json"
                if target.is_symlink():
                    raise ValueError("Invalid report path")
                temporary = self.reports_dir / f".report-{secrets.token_hex(16)}.tmp"
                try:
                    with temporary.open("xb") as outgoing:
                        outgoing.write(encoded)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                record = {"state": report["status"], "report_id": digest, "generated_at": generated_at, "summary": reconciliation_summary(report)}
            except Exception:
                record = {"state": "error", "report_id": digest}
            finally:
                with self._lock:
                    self._records[digest] = record
                    self._pending.discard(digest)
                self._queue.task_done()

    def read_report(self, digest: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid report identifier")
        path = self.reports_dir / f"{digest}.json"
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_REPORT:
            raise ValueError("Report unavailable")
        with path.open("rb") as incoming:
            contents = incoming.read(MAX_REPORT + 1)
        if len(contents) > MAX_REPORT:
            raise ValueError("Report too large")
        envelope = json.loads(contents)
        if not isinstance(envelope, dict) or envelope.get("report_id") != digest:
            raise ValueError("Invalid report envelope")
        return {
            "report_id": digest,
            "generated_at": _report_time(envelope.get("generated_at")),
            "report": safe_reconciliation_report(envelope.get("report")),
        }

    def shutdown(self, timeout=10):
        self._stop.set()
        if self._scan_thread is not None:
            self._scan_thread.join(min(timeout, 1))
        self._thread.join(timeout)


HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>大肥鱼 · DeepSeek 账单核对</title>
<style nonce="__NONCE__">
:root{color-scheme:light;font-family:"Microsoft YaHei",system-ui,sans-serif;color:#213b56;background:#eef6ff}*{box-sizing:border-box}body{margin:0;padding:32px 18px 48px}.page{max-width:820px;margin:auto}.eyebrow{font-size:13px;letter-spacing:2px;color:#448bc4}.title{font-size:30px;margin:8px 0 12px;line-height:1.3}.intro{margin:0;color:#58728b;line-height:1.8}.hero{padding:12px 3px 24px}.fish{float:right;font-size:48px;line-height:1.4}.card{background:white;border:1px solid #d7e9fb;border-radius:20px;padding:25px;margin:16px 0;box-shadow:0 7px 25px #5e95c40a}.row{display:flex;gap:14px;align-items:flex-start}.step{width:31px;height:31px;flex:0 0 31px;background:#e8f3ff;color:#277fbe;border-radius:50%;font-weight:bold;text-align:center;line-height:31px}h2{font-size:18px;margin:3px 0 10px}p{line-height:1.8;margin:8px 0}small,.muted{color:#698097;font-size:13px;line-height:1.8}.button{display:inline-block;border:0;border-radius:11px;background:#298ed1;color:#fff;padding:12px 20px;text-decoration:none;font:inherit;cursor:pointer;margin:12px 0 4px}.button:hover{background:#1b79ba}.button:disabled{opacity:.55;cursor:wait}.filebox{border:1px dashed #a6cbe9;background:#f8fbff;border-radius:13px;padding:18px;margin-top:14px}input[type=file]{max-width:100%;font:inherit;color:#416888}.status{display:flex;align-items:center;gap:9px;color:#277caa;font-weight:600}.dot{height:9px;width:9px;background:#5aa9d4;border-radius:50%}.result{background:#edf7ff;border-radius:12px;padding:12px 15px;overflow-wrap:anywhere;margin:14px 0 0;white-space:pre-line}.result.error{background:#fff1ef;color:#a04838}.receipt{padding:13px 0;border-bottom:1px solid #edf2f7;overflow-wrap:anywhere}.receipt:last-child{border:0}.receipt strong{font-size:14px;font-weight:500}.receipt code{font-family:Consolas,monospace;font-size:12px;color:#52708d}#receipts:empty{display:none}.footer{text-align:center;font-size:12px;color:#758da4;margin:22px 0 0}.note{padding-left:45px;margin-top:16px}@media(max-width:520px){body{padding:20px 12px}.card{padding:20px 16px}.title{font-size:25px}.fish{font-size:37px}.note{padding-left:0}}
</style>
</head>
<body><main class="page">
<header class="hero"><span class="fish" aria-hidden="true">🐟</span><div class="eyebrow">大肥鱼 · 本机用量核对</div><h1 class="title">DeepSeek 官方账单核对</h1><p class="intro">在独立浏览器的官网页面自行登录一次，每天 09:10 自动获取官网当前快照并核对，电脑需保持开机。<br>登录过期时需重新登录；也可以随时手动导出。</p></header>
<section class="card"><div class="row"><span class="step">1</span><div><h2>登录独立浏览器</h2><p>点击打开后，在 DeepSeek 官方页面自行输入登录信息。该浏览器使用 D 盘独立资料夹，网站允许时可保留登录；不会读取你日常 Chrome 的资料。</p><button class="button" id="browserOpen" type="button" disabled>打开独立浏览器登录</button><p class="muted">密码、验证码等仅在 DeepSeek 官网输入，本机页面不接收这些信息。网站可能要求重新登录，是否保留成功需以实际页面为准。</p></div></div><div class="result" id="browserMessage" role="status" aria-live="polite">正在读取独立浏览器状态…</div></section>
<section class="card"><div class="row"><span class="step">2</span><div><h2>申请导出官方账单</h2><p>先在独立浏览器确认官网日期范围和 API Key 筛选，然后点击导出。这里沿用官网当前选择，不会猜测核对日期。</p><button class="button" id="browserExport" type="button" disabled>导出官网当前范围</button><p class="muted">收到文件表示账单已保存，仍需核对日期、计费项目和本机历史记录。不会仅凭登录或下载成功，就宣称费用已对齐。</p></div></div></section>
<section class="card"><div class="row"><span class="step">3</span><div><h2>也可以手动上传账单</h2><p>如果自动导出暂不可用，可在官网手动导出，再上传 ZIP 或 CSV，单次不超过 20 MB。</p><a href="https://platform.deepseek.com/usage" target="_blank" rel="noopener noreferrer">在当前浏览器打开 DeepSeek 官方用量页 ↗</a><p class="muted">官网导出为 ZIP，内含两份 CSV；带有 amount 的明细用于核对分 API Key 费用。建议保留完整 ZIP 上传。</p></div></div><form id="uploadForm"><div class="filebox"><label for="billFile">选择官网导出的文件</label><p><input id="billFile" type="file" accept=".zip,.csv" required></p><small>文件只保存在此电脑的 D 盘，不会上传给其他网站。</small></div><button class="button" id="uploadButton" type="submit">上传账单</button></form><div id="result" role="status" aria-live="polite" hidden></div></section>
<section class="card"><div class="status"><span class="dot"></span><span id="state">正在读取本机状态…</span></div><p class="muted" id="stateNote">接收文件后，仍需核对日期范围、计费项目和历史记录是否完整。</p><div id="receipts"></div></section>
<section class="card"><h2 id="reconciliationState">正在读取核对结果…</h2><p id="reconciliationDetails" class="muted">官方文件和本机记录将按同一日桶分别核对。</p><a id="reportDownload" class="button" hidden>下载脱敏核对报告</a></section>
<p class="footer">仅供此电脑使用 · 登录操作由你完成 · 收到账单后再核对</p>
</main>
<script nonce="__NONCE__">
"use strict";
const csrf = "__CSRF__";
const resultBox = document.getElementById("result");
const button = document.getElementById("uploadButton");
const openButton = document.getElementById("browserOpen");
const exportButton = document.getElementById("browserExport");
let browserCommandPending = false;
function showResult(text, error=false) { resultBox.hidden=false; resultBox.className="result"+(error?" error":""); resultBox.textContent=text; }
function sizeLabel(bytes) {return bytes < 1024*1024 ? (bytes/1024).toFixed(1)+" KB" : (bytes/1024/1024).toFixed(2)+" MB";}
function moneyLabel(value) {return Number(value).toLocaleString("zh-CN",{minimumFractionDigits:4,maximumFractionDigits:6});}
function renderBrowser(browser) {
  document.getElementById("browserMessage").textContent=browser.message;
  openButton.disabled=browserCommandPending||!browser.can_open;
  exportButton.disabled=browserCommandPending||!browser.can_export;
}
function renderReconciliation(result) {
  const labels={disabled:"自动核对未启用",waiting_for_bill:"等待官方账单",queued:"账单已收到，等待核对",checking:"正在核对账单",matched:"核对完整，结果一致",difference:"核对完成，存在差异",history_incomplete:"历史或范围证据不足",unsupported_schema:"账单格式暂不支持",error:"核对暂未完成"};
  document.getElementById("reconciliationState").textContent=labels[result.state]||"核对暂未完成";
  const summary=result.summary;
  const lines=[];
  if(summary) {
    const partial=summary.coverage==="partial_current_day_hourly";
    const focus=summary.focus_day;
    if(focus) {
      lines.push("最近完整日桶（UTC+8）："+focus.start.slice(0,10));
      lines.push("官网 Token："+Number(focus.official_tokens).toLocaleString("zh-CN")+" · 本机已知 Token："+Number(focus.local_known_tokens).toLocaleString("zh-CN"));
      const delta=Number(focus.token_delta_local_minus_official);
      lines.push("差额（本机 − 官网）："+(delta>0?"+":"")+delta.toLocaleString("zh-CN"));
      if(focus.official_cost_cny!==null)lines.push("该日官网费用：¥"+moneyLabel(focus.official_cost_cny));
      if(focus.status==="history_incomplete")lines.push("该日历史或范围证据不足，差额只能作为排查线索。");
      if(focus.unknown_usage_calls>0)lines.push("另有 "+Number(focus.unknown_usage_calls).toLocaleString("zh-CN")+" 次请求缺少用量，不计为零。");
    }else lines.push("尚无带可靠导出时间的完整日桶，暂不挑选单日作对比。");
    if(summary.key_match==="partial")lines.push("官网隐藏了部分 API Key，目前仅部分匹配，不能视为已确认精确同一 Key。");
    if(summary.local_history_start)lines.push("本机历史开始："+summary.local_history_start.replace("T"," "));
    if(result.state==="history_incomplete")lines.push("差值保留在报告中作为线索；缺失历史和未知用量不会补成账本记录。");
    lines.push("");
    lines.push(partial?"当日已导出部分（不是完整当日账单）":"整份账单汇总");
    if(partial) {
      const missing=Array.isArray(summary.missing_hour_starts)?summary.missing_hour_starts:[];
      lines.push("当前导出的小时数据覆盖不完整，缺失小时不按零计费。");
      if(missing.length)lines.push("尚缺小时（UTC+8）："+missing.map(value=>value.slice(11,16)).join("、"));
    }
    const unreported=Array.isArray(summary.unreported_hour_starts)?summary.unreported_hour_starts:[];
    if(unreported.length) {
      lines.push("官方文件未列小时（UTC+8）："+unreported.map(value=>value.slice(11,16)).join("、"));
      lines.push("以上仅说明文件未列这些小时，不据此认定逐小时费用为零。");
    }
    if(summary.start&&summary.end)lines.push("官方范围（UTC+8，结束时间不含在内）："+summary.start.replace("T"," ")+" 至 "+summary.end.replace("T"," "));
    if(summary.official_tokens!==null&&summary.official_tokens!==undefined)lines.push((partial?"已导出部分 Token：":"官方 Token：")+Number(summary.official_tokens).toLocaleString("zh-CN"));
    if(summary.local_tokens!==null&&summary.local_tokens!==undefined)lines.push("本机已知 Token："+Number(summary.local_tokens).toLocaleString("zh-CN"));
    if(summary.official_cost_cny!==null&&summary.official_cost_cny!==undefined)lines.push((partial?"已导出部分费用：¥":"官方账单费用：¥")+moneyLabel(summary.official_cost_cny));
  }
  if(!lines.length)lines.push(result.state==="unsupported_schema"?"原始文件已保留，当前格式无法可靠核对。":"只有范围、Key 和完整性证据满足后，才会显示核对一致。");
  document.getElementById("reconciliationDetails").textContent=lines.join("\n");
  document.getElementById("reconciliationDetails").style.whiteSpace="pre-line";
  const link=document.getElementById("reportDownload");
  const ready=["matched","difference","history_incomplete","unsupported_schema"].includes(result.state)&&/^[a-f0-9]{64}$/.test(result.report_id||"");
  link.hidden=!ready;if(ready)link.href="/api/reconciliation/report/"+result.report_id+".json";
}
async function refreshStatus() {
  const response = await fetch("/api/status", {cache:"no-store", credentials:"omit"});
  if(!response.ok) throw new Error("本机状态暂时无法读取，请刷新页面。");
  const data = await response.json();
  renderBrowser(data.browser_state);
  renderReconciliation(data.reconciliation);
  const receiptLabels={matched:"已核对，结果一致",difference:"已核对，存在差异",history_incomplete:"已核对，存在历史或范围不足",unsupported_schema:"已收到账单，格式暂不支持核对",error:"已收到账单，核对未完成",queued:"已收到账单，等待核对",checking:"已收到账单，正在核对"};
  document.getElementById("state").textContent=receiptLabels[data.reconciliation.state]||(data.receipts.length?"已收到账单，待核对":"等待账单");
  const list=document.getElementById("receipts"); list.replaceChildren();
  for(const receipt of data.receipts) {
    const item=document.createElement("div"); item.className="receipt";
    const name=document.createElement("strong"); name.textContent="账单 "+receipt.sha256.slice(0,12)+" · "+receipt.filename.split(".").pop().toUpperCase();
    const details=document.createElement("div"); details.className="muted";
    details.textContent=new Date(receipt.received_at).toLocaleString("zh-CN",{hour12:false})+" · "+sizeLabel(receipt.size);
    const hash=document.createElement("code"); hash.textContent="SHA-256 "+receipt.sha256;
    item.append(name,details,hash);list.append(item);
  }
}
async function browserCommand(path) {
  browserCommandPending=true;openButton.disabled=true;exportButton.disabled=true;
  try {
    const response=await fetch(path,{method:"POST",credentials:"omit",headers:{"X-CSRF-Token":csrf,"Content-Type":"application/json"},body:"{}"});
    const data=await response.json();
    if(!response.ok) throw new Error(data.error||"操作暂未完成，请重试。");
    renderBrowser(data.browser_state);
  }catch(error){showResult(error.message||"操作暂未完成，请重试。",true);}
  finally{browserCommandPending=false;await refreshStatus().catch(()=>{});}
}
openButton.addEventListener("click",()=>browserCommand("/api/browser/open"));
exportButton.addEventListener("click",()=>browserCommand("/api/export"));
document.getElementById("uploadForm").addEventListener("submit",async(event)=>{
  event.preventDefault(); const file=document.getElementById("billFile").files[0];
  if(!file) {showResult("请先选择文件。",true);return;}
  const format=file.name.split(".").pop().toLowerCase();
  if(!["zip","csv"].includes(format)||file.size===0||file.size>20*1024*1024){showResult("请选择不超过 20 MB 的 ZIP 或 CSV。",true);return;}
  button.disabled=true;button.textContent="正在接收…";
  try {
    const response=await fetch("/api/import",{method:"POST",credentials:"omit",headers:{"X-CSRF-Token":csrf,"X-File-Format":format,"Content-Type":format==="zip"?"application/zip":"text/csv"},body:await file.arrayBuffer()});
    const data=await response.json(); if(!response.ok) throw new Error(data.error||"上传未完成，请重试。");
    showResult((data.duplicate?"这份账单已保存，无需重复上传。":"账单已保存。")+"\n当前状态：已收到账单，待核对。你可以回到聊天回复“已上传”。");
    await refreshStatus();
  }catch(error){showResult(error.message||"上传未完成，请重试。",true);}
  finally{button.disabled=false;button.textContent="上传账单";}
});
refreshStatus().catch(error=>{document.getElementById("state").textContent="状态暂时不可用";showResult(error.message,true);});
setInterval(()=>{if(!document.hidden)refreshStatus().catch(()=>{});},3000);
</script></body></html>'''


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int = DEFAULT_PORT, storage: Path = DEFAULT_STORAGE, browser_manager=None, reconciliation_manager=None):
        # Do not accept a bind-host option: the portal is intentionally loopback-only.
        super().__init__(("127.0.0.1", port), PortalHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.expected_host = f"127.0.0.1:{self.server_port}"
        self.csrf = secrets.token_urlsafe(32)
        self.store = ReceiptStore(Path(storage))
        self.browser_manager = browser_manager
        self.reconciliation_manager = reconciliation_manager

    def queue_reconciliation(self, receipt: dict, metadata: dict | None = None):
        if self.reconciliation_manager is not None:
            try:
                self.reconciliation_manager.submit(receipt, metadata)
            except Exception:
                # Receiving an original file remains distinct from reconciliation.
                pass

    def import_browser_export(self, path: Path, metadata: dict | None = None) -> dict:
        """Validate a worker download using the same receipt checks as an upload.

        Metadata is intentionally not reflected into the public HTTP status.
        The browser worker owns its internal date/filter evidence separately.
        """
        path = Path(path)
        file_format = path.suffix.lower().lstrip(".")
        if file_format not in ("csv", "zip") or path.is_symlink() or not path.is_file():
            raise UploadError("官网导出文件格式无效。")
        if not 0 < path.stat().st_size <= MAX_UPLOAD:
            raise UploadError("官网导出文件超过 20 MB 或为空。")
        with path.open("rb") as incoming:
            data = incoming.read(MAX_UPLOAD + 1)
        validate_upload(data, file_format)
        receipt, duplicate = self.store.save(data, file_format)
        self.store.save_export_observation(receipt["sha256"], metadata)
        self.queue_reconciliation(receipt, metadata)
        return {"state": "received_pending_review", "duplicate": duplicate, "receipt": receipt}


class PortalHandler(BaseHTTPRequestHandler):
    server: PortalServer
    protocol_version = "HTTP/1.1"
    server_version = "LocalBillingPortal"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(READ_TIMEOUT)

    def log_message(self, format, *args):
        # Request lines, uploaded filenames and request headers are never logged.
        return

    def _headers(self, status: int, content_type: str, length: int, nonce: str | None = None, download_name: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        policy = "default-src 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; connect-src 'self'"
        if nonce:
            policy += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Connection", "close")
        if download_name is not None:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.close_connection = True

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def _safe_request(self, write: bool = False) -> bool:
        hosts = self.headers.get_all("Host", [])
        if hosts != [self.server.expected_host]:
            self._error(HTTPStatus.BAD_REQUEST, "请通过本机入口地址打开页面。")
            return False
        origins = self.headers.get_all("Origin", [])
        if (write and origins != [self.server.origin]) or (origins and origins != [self.server.origin]):
            self._error(HTTPStatus.FORBIDDEN, "来源校验失败，请重新打开本机页面。")
            return False
        if write:
            tokens = self.headers.get_all("X-CSRF-Token", [])
            if (
                len(tokens) != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", tokens[0])
                or not secrets.compare_digest(tokens[0], self.server.csrf)
            ):
                self._error(HTTPStatus.FORBIDDEN, "页面校验已过期，请刷新后重试。")
                return False
        return True

    def do_GET(self):
        if not self._safe_request():
            return
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "入口地址无效。")
            return
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            self._error(HTTPStatus.NOT_FOUND, "入口不存在。")
            return
        if parsed.path in ("/", "/login"):
            nonce = secrets.token_urlsafe(24)
            body = HTML.replace("__NONCE__", nonce).replace("__CSRF__", self.server.csrf).encode("utf-8")
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body), nonce)
            if self.command != "HEAD":
                self.wfile.write(body)
        elif parsed.path == "/api/status":
            try:
                receipts = self.server.store.list_receipts()
                self._json(HTTPStatus.OK, {
                    "state": "received_pending_review" if receipts else "waiting_for_bill",
                    "receipts": receipts,
                    "browser_state": browser_snapshot(self.server.browser_manager),
                    "reconciliation": self.server.reconciliation_manager.status() if self.server.reconciliation_manager is not None else {"state": "disabled"},
                })
            except OSError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "本机账单状态暂时不可用。")
        elif re.fullmatch(r"/api/reconciliation/report/[a-f0-9]{64}\.json", parsed.path):
            manager = self.server.reconciliation_manager
            if manager is None:
                self._error(HTTPStatus.NOT_FOUND, "核对报告暂不可用。")
                return
            digest = parsed.path.rsplit("/", 1)[-1][:-5]
            try:
                envelope = manager.read_report(digest)
                encoded = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
                self._headers(HTTPStatus.OK, "application/json; charset=utf-8", len(encoded), download_name=f"deepseek-reconciliation-{digest[:12]}.json")
                if self.command != "HEAD":
                    self.wfile.write(encoded)
            except Exception:
                self._error(HTTPStatus.NOT_FOUND, "核对报告暂不可用。")
        else:
            self._error(HTTPStatus.NOT_FOUND, "入口不存在。")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._safe_request(write=True):
            return
        if self.path in ("/api/browser/open", "/api/export"):
            self._browser_command()
            return
        if self.path != "/api/import":
            self._error(HTTPStatus.NOT_FOUND, "入口不存在。")
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._error(HTTPStatus.BAD_REQUEST, "不支持此上传方式，请使用页面上传。")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._error(HTTPStatus.LENGTH_REQUIRED, "上传缺少文件大小。")
            return
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            self._error(HTTPStatus.BAD_REQUEST, "上传文件大小无效。")
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_UPLOAD:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请选择不超过 20 MB 的 ZIP 或 CSV。")
            return
        formats = self.headers.get_all("X-File-Format", [])
        types = self.headers.get_all("Content-Type", [])
        if len(formats) != 1 or formats[0] not in ("csv", "zip"):
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "只接受 ZIP 或 CSV 文件。")
            return
        file_format = formats[0]
        expected_type = "application/zip" if file_format == "zip" else "text/csv"
        if types != [expected_type]:
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "文件类型校验失败，请使用页面上传。")
            return
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                self._error(HTTPStatus.BAD_REQUEST, "文件接收不完整，请重新上传。")
                return
            validate_upload(data, file_format)
            receipt, duplicate = self.server.store.save(data, file_format)
            self.server.queue_reconciliation(receipt)
            self._json(HTTPStatus.OK, {"state": "received_pending_review", "duplicate": duplicate, "receipt": receipt})
        except (TimeoutError, socket.timeout):
            self._error(HTTPStatus.REQUEST_TIMEOUT, "上传等待超时，请重新上传。")
        except UploadError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (OSError, ValueError):
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "本机保存未完成，请稍后重试。")

    def _browser_command(self):
        if self.headers.get_all("Transfer-Encoding", []):
            self._error(HTTPStatus.BAD_REQUEST, "不支持此请求方式，请使用本机页面。")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._error(HTTPStatus.LENGTH_REQUIRED, "请求缺少大小。")
            return
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,4}", lengths[0]):
            self._error(HTTPStatus.BAD_REQUEST, "请求大小无效。")
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_COMMAND:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求大小无效。")
            return
        if self.headers.get_all("Content-Type", []) != ["application/json"]:
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "请使用本机页面发起操作。")
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length or json.loads(body) != {}:
                self._error(HTTPStatus.BAD_REQUEST, "操作不接受额外参数，请使用本机页面。")
                return
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            self._error(HTTPStatus.BAD_REQUEST, "请求格式无效。")
            return
        except (TimeoutError, socket.timeout):
            self._error(HTTPStatus.REQUEST_TIMEOUT, "请求等待超时，请重试。")
            return
        manager = self.server.browser_manager
        if manager is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, BROWSER_MESSAGES["unavailable"])
            return
        try:
            # Manager methods enqueue work; browser I/O runs on its own worker.
            if self.path == "/api/browser/open":
                manager.open()
            else:
                manager.export()
            self._json(HTTPStatus.ACCEPTED, {"browser_state": browser_snapshot(manager)})
        except Exception:
            # Never expose exception strings, URLs or browser authentication data.
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "浏览器操作暂未完成，请稍后重试。")

    def _method_not_allowed(self):
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "不支持此操作。")

    do_OPTIONS = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_TRACE = _method_not_allowed
    do_CONNECT = _method_not_allowed


def main():
    parser = argparse.ArgumentParser(description="Local DeepSeek billing receipt portal.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--storage", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--browser", action="store_true", help="Enable the separate official-site browser worker.")
    parser.add_argument("--open-browser", action="store_true", help="Open the separate official-site browser window on startup; requires --browser.")
    parser.add_argument("--scheduled-mode", choices=("verified_yesterday", "current_official_snapshot", "on_demand"), default="verified_yesterday", help="Browser schedule export strategy; current_official_snapshot requires later bill-range verification.")
    parser.add_argument("--reconcile", action="store_true", help="Reconcile imported receipts against the fixed local token ledger without modifying it.")
    args = parser.parse_args()
    if args.open_browser and not args.browser:
        parser.error("--open-browser requires --browser")
    with PortalServer(args.port, args.storage) as server:
        manager = None
        reconciliation = None
        if args.reconcile:
            from bill_parser import reconcile

            reconciliation = ReconciliationManager(server.store, reconcile)
            server.reconciliation_manager = reconciliation
            reconciliation.scan_existing()
        if args.browser:
            from browser_worker import BrowserManager

            manager = BrowserManager(import_callback=server.import_browser_export, inspect_ready_once=True, scheduled_mode=args.scheduled_mode)
            server.browser_manager = manager
            if args.open_browser:
                manager.open_automatically()
        print(f"账单导入入口：{server.origin}/login", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if manager is not None:
                manager.shutdown()
            if reconciliation is not None:
                reconciliation.shutdown()


if __name__ == "__main__":
    main()
