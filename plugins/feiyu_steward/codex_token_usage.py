"""Read Cockpit's local request-usage ledger, never credentials or payloads.

This is a separate source from AstrBot provider accounting.  Account rotation
does not multiply a request: one complete usage row wins per request_id.
"""

import json
import math
import ntpath
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, time as day_time
from pathlib import Path

from .token_usage_store import LOCAL_TIMEZONE
from .token_pricing import price_codex_rows

SOURCE = Path.home() / ".cockpit_tools" / "codex_local_access_logs.sqlite"
CODEX_STATE = Path.home() / ".codex" / "state_5.sqlite"
CLIENT_INSTANCE = ".codex"
MAX_TOKENS = 1_000_000_000_000
PRICE_COLUMNS = (
    "model_id", "requested_model", "upstream_model", "service_tier",
    "requested_service_tier", "response_service_tier",
)
PROJECT_THREADS = {
    "01a158bc-e697-76c3-ae8d-f4122db09b1a": "feiyu_bot",
    "01a16428-9ae2-74b1-9877-12e97cee8a26": "academic_poc",
}
PROJECT_LABELS = {"feiyu_bot": "肥鱼相关", "academic_poc": "论文", "other": "其他", "unclassified": "未归类"}
# These are explicit project roots, not names inferred from a directory or a
# conversation.  Other project roots must also be explicitly established.
PROJECT_PATHS = {
    "feiyu_bot": str(Path.home() / "Documents" / "Codex" / "2026-10-20" / "github-astrbot-snowluma-qq"),
    "academic_poc": r"D:\academic",
}
OTHER_PROJECT_PATHS = ()
THREAD_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
# Model usage is independent of whether a dated tariff is available. Keep
# labels fixed so an unexpected upstream field cannot expose account data.
MODEL_USAGE_LABELS = {
    "gpt-6.1-sol": "gpt-6.1-sol",
    "gpt-6.1-terra": "gpt-6.1-terra",
    "gpt-6-astra": "gpt-6-astra",
    "gpt-6-luna": "gpt-6-luna",
    "unknown": "模型未记录",
    "other_models": "其他模型",
}


def _usage_model(row):
    # The returned upstream model is authoritative, including unknown values.
    model = next((row.get(key) for key in ("upstream_model", "requested_model", "model_id")
                  if row.get(key)), "unknown")
    return model if model in MODEL_USAGE_LABELS else "other_models"


def _table_columns(db, table):
    # All callers supply fixed internal table names, never caller input.
    return {row["name"] for row in db.execute("PRAGMA table_info(" + table + ")")}


def _identity_sql(db):
    """Return numeric-usage joins using only captured identity metadata."""
    joins, expressions = [], []
    display = {"log_id", "request_id", "client_instance_id", "client_thread_id", "client_identity_captured"}
    if display <= _table_columns(db, "request_display_summaries"):
        joins.append("LEFT JOIN request_display_summaries ids ON ids.log_id=r.id ")
        valid = "COALESCE(ids.request_id,'')=r.request_id AND COALESCE(ids.client_instance_id,'')='.codex'"
        expressions.extend([
            "CASE WHEN ids.client_identity_captured=1 AND " + valid +
            " THEN COALESCE(ids.client_thread_id,'') ELSE '' END AS display_thread_id",
            "CASE WHEN ids.log_id IS NOT NULL AND NOT(" + valid +
            ") THEN 1 ELSE 0 END AS identity_conflict",
        ])
    else:
        expressions.extend(["'' AS display_thread_id", "0 AS identity_conflict"])
    diagnostic = {"request_id", "client_thread_id", "client_identity_captured"}
    if diagnostic <= _table_columns(db, "request_diagnostics"):
        joins.append("LEFT JOIN request_diagnostics diag ON r.request_id<>'' AND diag.request_id=r.request_id ")
        expressions.append("CASE WHEN diag.client_identity_captured=1 THEN "
                           "COALESCE(diag.client_thread_id,'') ELSE '' END AS diagnostic_thread_id")
    else:
        expressions.append("'' AS diagnostic_thread_id")
    return "".join(joins), ",".join(expressions)


def _load_thread_lineage():
    """Read parent edges and thread IDs only; never titles or conversation text."""
    parents, known, conflicts, missing_parent = {}, set(), set(), set()
    try:
        path = Path(CODEX_STATE)
        if not path.is_file() or path.is_symlink():
            return parents, known, conflicts, missing_parent
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if {"id", "source"} <= _table_columns(db, "threads"):
                # JSON projection avoids loading agent descriptions or other
                # thread fields. Source-parent metadata also survives an edge
                # table being unavailable on older Codex versions.
                rows = db.execute(
                    "SELECT id,CASE WHEN json_valid(source) THEN "
                    "json_extract(source,'$.subagent.thread_spawn.parent_thread_id') ELSE NULL END parent_id,"
                    "CASE WHEN json_valid(source) THEN json_type(source,'$.subagent') IS NOT NULL "
                    "ELSE source='subagent' END is_child FROM threads"
                )
                for row in rows:
                    child, parent = row["id"], row["parent_id"]
                    if not isinstance(child, str) or not THREAD_ID.fullmatch(child):
                        continue
                    known.add(child)
                    if isinstance(parent, str) and THREAD_ID.fullmatch(parent):
                        parents[child] = parent
                    elif row["is_child"]:
                        missing_parent.add(child)
            if {"parent_thread_id", "child_thread_id"} <= _table_columns(db, "thread_spawn_edges"):
                for row in db.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                    child, parent = row["child_thread_id"], row["parent_thread_id"]
                    if not all(isinstance(x, str) and THREAD_ID.fullmatch(x) for x in (child, parent)):
                        continue
                    if child in parents and parents[child] != parent:
                        conflicts.add(child)
                    else:
                        parents[child] = parent
                        missing_parent.discard(child)
            db.commit()
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {}, set(), set(), set()
    return parents, known, conflicts, missing_parent


def _canonical_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    # Windows extended-length drive paths name the same path as normal ones.
    if value.startswith("\\\\?\\"):
        value = value[4:]
    if not ntpath.isabs(value) or not ntpath.splitdrive(value)[0]:
        return None
    return ntpath.normcase(ntpath.normpath(value))


def _purpose_for_path(value):
    path = _canonical_path(value)
    if path is None:
        return None
    anchors = list(PROJECT_PATHS.items()) + [("other", root) for root in OTHER_PROJECT_PATHS]
    for purpose, root in anchors:
        root = _canonical_path(root)
        if root and (path == root or path.startswith(root.rstrip("\\") + "\\")):
            return purpose
    return None


def _load_project_evidence():
    """Read only thread cwd, attached project ID and registered project roots."""
    try:
        path = Path(CODEX_STATE)
        if not path.is_file() or path.is_symlink():
            return {}
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if not {"id", "cwd", "project_id"} <= _table_columns(db, "threads"):
                return {}
            project_roots = {}
            if {"project_id", "path"} <= _table_columns(db, "project_roots"):
                for row in db.execute("SELECT project_id,path FROM project_roots"):
                    project_roots.setdefault(row["project_id"], []).append(row["path"])
            projects = {}
            for project_id, roots in project_roots.items():
                if not roots or any(_canonical_path(root) is None for root in roots):
                    projects[project_id] = (None, True)
                    continue
                candidates = {_purpose_for_path(root) for root in roots}
                projects[project_id] = (next(iter(candidates)), False) if None not in candidates and len(candidates) == 1 else (None, True)
            evidence = {}
            for row in db.execute("SELECT id,cwd,project_id FROM threads"):
                if not isinstance(row["id"], str) or not THREAD_ID.fullmatch(row["id"]):
                    continue
                values = set()
                mapped = PROJECT_THREADS.get(row["id"])
                cwd = _purpose_for_path(row["cwd"])
                project, ambiguous = projects.get(row["project_id"], (None, False))
                values.update(value for value in (mapped, cwd, project) if value)
                evidence[row["id"]] = (values, ambiguous or len(values) > 1)
            db.commit()
        return evidence
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {}


def _project_for_thread(thread, lineage, evidence=None):
    parents, known, conflicts, missing_parent = lineage
    evidence = evidence or {}
    seen, depth = set(), 0
    values = set()
    while isinstance(thread, str) and THREAD_ID.fullmatch(thread):
        if thread in seen or thread in conflicts:
            return "unclassified", "conflict", depth
        seen.add(thread)
        node_values, ambiguous = evidence.get(thread, (set(), False))
        values.update(node_values)
        if thread in PROJECT_THREADS:
            values.add(PROJECT_THREADS[thread])
        if ambiguous or len(values) > 1:
            return "unclassified", "conflict", depth
        if thread in parents:
            thread = parents[thread]
            depth += 1
            continue
        if thread in missing_parent:
            break
        if thread not in known and thread not in PROJECT_THREADS:
            break
        if len(values) == 1:
            return next(iter(values)), "known", depth
        break
    return "unclassified", "missing", depth


def _usage_totals(rows):
    totals = {key: 0 for key in (
        "calls", "success", "failed", "known_usage_calls", "unknown_usage_calls",
        "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens"
    )}
    for row in rows:
        totals["calls"] += 1
        totals["success" if row["success"] == 1 else "failed"] += 1
        counts = _counts(row)
        totals["known_usage_calls" if counts is not None else "unknown_usage_calls"] += 1
        if counts is not None:
            for key, value in counts.items():
                totals[key] += value
    totals["uncached_input_tokens"] = totals["input_tokens"] - totals["cached_tokens"]
    return totals


def _valid_count(value):
    return type(value) is int and 0 <= value <= MAX_TOKENS


def _counts(row):
    """Only complete, internally consistent upstream usage is a known amount."""
    try:
        data = json.loads(row["token_breakdown_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("quality") != "complete":
        return None
    values = {key: row[key] for key in (
        "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens"
    )}
    if not all(_valid_count(value) for value in values.values()):
        return None
    if (values["cached_tokens"] > values["input_tokens"]
            or values["reasoning_tokens"] > values["output_tokens"]
            or values["total_tokens"] != values["input_tokens"] + values["output_tokens"]):
        return None
    # Reconcile stored totals with the upstream breakdown without reading any
    # other JSON fields. Cached input and reasoning output are subsets.
    source_input = data.get("input")
    source_output = data.get("output")
    if (not isinstance(source_input, dict) or not isinstance(source_output, dict)
            or type(data.get("total_tokens")) is not int
            or data["total_tokens"] != values["total_tokens"]
            or type(source_input.get("total_tokens")) is not int
            or source_input["total_tokens"] != values["input_tokens"]
            or type(source_output.get("total_tokens")) is not int
            or source_output["total_tokens"] != values["output_tokens"]):
        return None
    return values


def cockpit_usage_summary(day, *, source=None, now=None):
    """Return numeric totals for one UTC+8 day across all rotated accounts.

    The fixed .codex client instance matches this computer's Codex provider
    configuration. Other clients routed through Cockpit are excluded. A
    read-only snapshot and numeric projection exclude diagnostic payloads,
    errors, email addresses, API keys and account identities from the reader.
    """
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return {"day": day, "ok": False, "reason": "invalid_usage_day"}
    try:
        target = datetime.strptime(day, "%Y-%m-%d").date()
        start = int(datetime.combine(target, day_time(), LOCAL_TIMEZONE).timestamp() * 1000)
        end = start + 86_400_000
    except (ValueError, OverflowError, OSError):
        return {"day": day, "ok": False, "reason": "invalid_usage_day"}
    return _cockpit_usage_between(start, end, {"day": day}, source=source, now=now)


def cockpit_usage_window(hours, *, source=None, now=None):
    current = time.time() if now is None else now
    if type(hours) not in (int, float) or not 0.5 <= hours <= 720:
        return {"ok": False, "reason": "invalid_usage_window"}
    start, end = int((current - hours * 3600) * 1000), int(current * 1000)
    return _cockpit_usage_between(start, end, {
        "window_hours": hours, "period_label": f"过去{hours:g}小时",
        "from": datetime.fromtimestamp(start / 1000, LOCAL_TIMEZONE).isoformat(timespec="seconds"),
        "until": datetime.fromtimestamp(end / 1000, LOCAL_TIMEZONE).isoformat(timespec="seconds"),
    }, source=source, now=current)


def _cockpit_usage_between(start, end, period, *, source=None, now=None):
    base = {**period, "source": "cockpit_request_ledger", "client_instance": CLIENT_INSTANCE,
            "scope": "codex_client_all_upstream_accounts", "not_observed": False}
    try:
        now_ms = int((time.time() if now is None else now) * 1000)
        path = Path(source) if source is not None else SOURCE
        if not path.is_file() or path.is_symlink():
            return {**base, "ok": False, "reason": "cockpit_usage_unavailable"}
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            first = db.execute(
                "SELECT MIN(timestamp) FROM request_logs WHERE client_instance_id=?", (CLIENT_INSTANCE,)
            ).fetchone()[0]
            if first is None or end <= first or start > now_ms:
                return {**base, "ok": True, "not_observed": True}
            # The request log itself has a unique event_key. Guard against
            # replayed/fan-out copies sharing a request_id across accounts.
            # Prefer a complete row, then the most recent event for that
            # request. Incomplete retry rows do not erase complete usage.
            # Pin a replayed request to its first recorded timestamp. Updating
            # its final usage must not move yesterday's spend into today.
            # Older Cockpit ledgers have no pricing metadata. Project only
            # these fixed safe names and leave absent values explicitly empty.
            available = _table_columns(db, "request_logs")
            price_projection = ",".join(
                column if column in available else "'' AS " + column
                for column in PRICE_COLUMNS
            )
            ranked = (
                "WITH ranked AS (SELECT id,timestamp,request_id,account_id,success,input_tokens,cached_tokens,"
                "output_tokens,reasoning_tokens,total_tokens,token_breakdown_json,"
                + price_projection + ","
                "MIN(timestamp) OVER(PARTITION BY CASE WHEN request_id='' THEN 'log:'||id "
                "ELSE 'req:'||request_id END) request_timestamp,"
                "ROW_NUMBER() OVER(PARTITION BY CASE WHEN request_id='' THEN 'log:'||id "
                "ELSE 'req:'||request_id END ORDER BY "
                "CASE WHEN json_valid(token_breakdown_json) THEN "
                "CASE WHEN json_extract(token_breakdown_json,'$.quality')='complete' THEN 1 ELSE 0 END "
                "ELSE 0 END DESC,timestamp DESC,id DESC) rn "
                "FROM request_logs WHERE client_instance_id=? AND timestamp<=?) "
            )
            identity_joins, identity_projection = _identity_sql(db)
            usage_columns = ("timestamp", "success", "input_tokens", "cached_tokens", "output_tokens",
                             "reasoning_tokens", "total_tokens", "token_breakdown_json") + PRICE_COLUMNS
            rows = db.execute(ranked +
                "SELECT " + ",".join("r." + column for column in usage_columns) + "," + identity_projection +
                " FROM ranked r " + identity_joins +
                "WHERE r.rn=1 AND r.request_timestamp>=? AND r.request_timestamp<?",
                (CLIENT_INSTANCE, now_ms, start, end),
            ).fetchall()
            # Identity is counted within SQLite, never loaded or returned.
            accounts = db.execute(ranked +
                "SELECT COUNT(DISTINCT NULLIF(account_id,'')) FROM ranked "
                "WHERE request_timestamp>=? AND request_timestamp<?",
                (CLIENT_INSTANCE, now_ms, start, end),
            ).fetchone()[0]
            db.commit()
        lineage = _load_thread_lineage()
        evidence = _load_project_evidence()
        project_rows = {key: [] for key in PROJECT_LABELS}
        classification = {"method": "captured_thread_id_parent_edges_and_project_metadata_v2", "known_calls": 0,
                          "unclassified_calls": 0, "conflicting_calls": 0, "subagent_calls": 0}
        clean_rows = []
        model_rows = {key: [] for key in MODEL_USAGE_LABELS}
        for row in rows:
            display, diagnostic = row["display_thread_id"], row["diagnostic_thread_id"]
            if row["identity_conflict"] or (display and diagnostic and display != diagnostic):
                project, status, depth = "unclassified", "conflict", 0
            else:
                project, status, depth = _project_for_thread(display or diagnostic, lineage, evidence)
            classification["known_calls" if status == "known" else "unclassified_calls"] += 1
            classification["conflicting_calls"] += status == "conflict"
            classification["subagent_calls"] += status == "known" and depth > 0
            # Never pass thread, request or account identities into pricing or
            # the public report. Only fixed project labels leave this reader.
            clean = {column: row[column] for column in usage_columns}
            clean_rows.append(clean)
            project_rows[project].append(clean)
            model_rows[_usage_model(clean)].append(clean)
        totals = _usage_totals(clean_rows)
        classification["coverage_percent"] = round(100 * classification["known_calls"] / len(rows), 4) if rows else 100.0
        by_project = [{"key": key, "label": label, "totals": _usage_totals(project_rows[key]),
                       "pricing": price_codex_rows(project_rows[key])}
                      for key, label in PROJECT_LABELS.items()]
        return {**base, "ok": True, "totals": totals, "upstream_account_count": accounts,
                "pricing": price_codex_rows(clean_rows), "by_project": by_project,
                "by_model_usage": [{"model": key, "label": MODEL_USAGE_LABELS[key],
                                    "totals": _usage_totals(items)}
                                   for key, items in model_rows.items() if items],
                "model_usage_basis": "upstream_then_requested_then_model_id",
                "classification": classification,
                "partial_day": start < first < end or end > now_ms,
                "retained_since": datetime.fromtimestamp(first / 1000, LOCAL_TIMEZONE).isoformat(timespec="seconds"),
                "deduplication": "one_complete_usage_per_request_id_first_timestamp_window",
                "limitations": "retained_local_cockpit_logs_only_no_hidden_retry_usage_or_other_clients"}
    except (OSError, ValueError, TypeError, OverflowError, sqlite3.Error):
        return {**base, "ok": False, "reason": "cockpit_usage_unavailable"}


def build_cockpit_usage_report(summary):
    """Compact local Codex totals and official-price CNY estimate."""
    if not isinstance(summary, dict) or not summary.get("ok"):
        return "Codex：用量暂不可用，费用待核实。"
    if summary.get("not_observed"):
        return "Codex：该时段暂无用量记录，费用待核实。"
    t = summary["totals"]
    pricing = summary.get("pricing") or {}
    amount = pricing.get("total_cny") if pricing.get("complete") else pricing.get("known_cny")
    if (type(amount) in (int, float) and math.isfinite(amount) and amount >= 0
            and (pricing.get("complete") or pricing.get("by_model"))):
        cost = f"约 ¥{amount:,.2f}" if pricing.get("complete") else f"已核实 ¥{amount:,.2f}，其余费用待核实"
    else:
        cost = "费用待核实"
    lines = [
        "Codex：",
        f"{t['total_tokens']:,} token · {t['calls']:,}次请求 · 官方价折合人民币{cost}。",
        f"非缓存输入{t['uncached_input_tokens']:,}，缓存输入{t['cached_tokens']:,}，输出{t['output_tokens']:,}。",
    ]
    models = []
    for row in pricing.get("by_model", []):
        model = row.get("model") if isinstance(row, dict) else None
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", model):
            if model not in models:
                models.append(model)
    if models:
        lines.append("模型：" + "、".join(models) + "。")
    for project in summary.get("by_project", []):
        if not isinstance(project, dict) or project.get("key") not in PROJECT_LABELS:
            continue
        project_totals, project_pricing = project.get("totals") or {}, project.get("pricing") or {}
        amount = project_pricing.get("total_cny") if project_pricing.get("complete") else project_pricing.get("known_cny")
        if (type(amount) in (int, float) and math.isfinite(amount) and amount >= 0
                and (project_pricing.get("complete") or project_pricing.get("by_model"))):
            project_cost = f"约 ¥{amount:,.2f}" if project_pricing.get("complete") else f"已定价 ¥{amount:,.2f}"
        else:
            project_cost = "费用待核实"
        label = PROJECT_LABELS[project["key"]]
        lines.append(f"{label}：{project_totals.get('total_tokens', 0):,} token · {project_cost}。")
    return "\n".join(lines)
