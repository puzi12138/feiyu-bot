"""Validated local usage, official-tariff estimates and illustrated chart reports."""

import hashlib
import json
import math
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .token_report_gate import (
    TOTAL_FIELDS, _valid_totals,
)
from .cockpit_report_window import freeze_report_window, validate_report_window
from .bill_display_policy import PROJECT_SCOPE_LABEL, PROJECT_USAGE_LABEL
from .cost_pie_chart import (COST_PIE_VERSION, cost_card_height, draw_cost_card,
                             make_cost_distribution)
from .token_report_art import (
    ART_VERSION, ASSET_DIRECTORY as JOURNAL_ART_DIRECTORY, select_art,
    paint_background, draw_card, draw_title_strip, paste_scene,
)

MODE = "cockpit_only_v1"
DESIGN = "feiyu_token_charts_anime_v4_models"
PROJECT_LABELS = {"feiyu_bot": "肥鱼相关", "academic_poc": "论文", "other": "其他", "unclassified": "未归类"}
CLASSIFICATION_BASIS = "按当前项目和父子关系归类；线程变动可能影响历史归属。"
MODEL_USAGE_LABELS = {
    "gpt-6.1-sol": "gpt-6.1-sol", "gpt-6.1-terra": "gpt-6.1-terra",
    "gpt-6-astra": "gpt-6-astra", "gpt-6-luna": "gpt-6-luna",
    "unknown": "模型未记录", "other_models": "其他模型",
}
ASSET_DIRECTORY = Path(__file__).resolve().parent / "assets/token-chart-anime"
APPROVED_ANIME_ASSETS = frozenset({"feiyu-reading.png", "feiyu-games.png", "feiyu-stars.png"})
ART_ATTRIBUTION = (
    "角色：上善无形  ·  女仆设计：ZipZipPipe  ·  参考整理：Small-tailqwq",
    "AI 辅助二创  ·  CC-BY-NC-SA-4.0",
)
THEMES = (
    {"name": "樱花汽水", "bg": "#fff3f7", "ink": "#423954", "muted": "#8c7b95", "accent": "#db79a1", "pale": "#ffe4ef", "colors": ("#e78db1", "#8d9dea", "#79c9bb")},
    {"name": "薄荷星河", "bg": "#ecf8f5", "ink": "#294a52", "muted": "#708c91", "accent": "#4ba99c", "pale": "#d9f1e8", "colors": ("#65bfaa", "#8fa7e7", "#efbd80")},
    {"name": "月光蓝莓", "bg": "#f0f0fc", "ink": "#3b3e65", "muted": "#8485a6", "accent": "#8980ce", "pale": "#e3dff8", "colors": ("#a491d8", "#8bb7e8", "#e9aac2")},
    {"name": "奶油橘子", "bg": "#fff7eb", "ink": "#654c45", "muted": "#a18d82", "accent": "#d79763", "pale": "#ffe7cb", "colors": ("#e9b276", "#a6b4df", "#8fcbb2")},
    {"name": "海盐晴空", "bg": "#edf6ff", "ink": "#344e70", "muted": "#7e95ad", "accent": "#679bd1", "pale": "#dcecff", "colors": ("#83abe0", "#a6a0dc", "#85cbbb")},
    {"name": "蜜桃夜游", "bg": "#faf0f5", "ink": "#57405f", "muted": "#9b849f", "accent": "#b785ba", "pale": "#f0ddec", "colors": ("#c897cc", "#edada9", "#91bcd8")},
)
QUOTES = ("今天的灵感，也有认真记账。", "把努力攒成星星，一颗都不漏。", "数据要清楚，可爱也要在线。", "缓存省下的每一点，都是小小魔法。", "这页手账，装着今天的脑洞。", "灵感营业中，数字也排好队。")


def _stamp(value):
    try:
        value = datetime.fromisoformat(value)
        return value.isoformat(timespec="seconds") if value.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12


def _priced_subset(raw, totals):
    """The amount covers priced requests only; missing usage is never priced as zero."""
    empty = {"valid": False, "complete": False, "basis": "dated_official_tariff_cny_estimate_not_bill"}
    if not isinstance(raw, dict):
        return empty
    priced, unpriced, amount = raw.get("priced_calls"), raw.get("unpriced_calls"), raw.get("known_cny")
    if (type(priced) is not int or type(unpriced) is not int or
            not 0 <= priced <= totals["known_usage_calls"] <= totals["calls"] or
            priced + unpriced != totals["calls"] or not _number(amount)):
        return empty
    models = raw.get("by_model")
    if not isinstance(models, list) or len(models) > 64:
        return empty
    clean = []
    for row in models:
        if (not isinstance(row, dict) or not isinstance(row.get("model"), str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", row["model"]) or
                type(row.get("calls")) is not int or not 0 < row["calls"] <= priced or
                not _number(row.get("cost_cny"))):
            return empty
        clean.append({"model": row["model"], "calls": row["calls"], "cost_cny": row["cost_cny"]})
    if len({row["model"] for row in clean}) != len(clean) or sum(row["calls"] for row in clean) != priced:
        return empty
    tolerance = Decimal("0.000001") * (len(clean) + 1)
    if abs(sum((Decimal(str(row["cost_cny"])) for row in clean), Decimal(0)) - Decimal(str(amount))) > tolerance:
        return empty
    if type(raw.get("complete")) is not bool or raw["complete"] != (unpriced == 0):
        return empty
    complete = raw["complete"] and _number(raw.get("total_cny")) and raw["total_cny"] == amount
    if raw["complete"] and not complete:
        return empty
    if priced == 0:
        if amount != 0 or clean:
            return empty
        return {**empty, "valid": True, "priced_calls": 0, "unpriced_calls": unpriced, "by_model": []}
    result = {**empty, "valid": True, "complete": complete, "known_cny": amount,
              "display_cny": amount, "priced_calls": priced, "unpriced_calls": unpriced, "by_model": clean}
    if complete:
        result["total_cny"] = amount
    for field in ("price_date", "rate_date"):
        value = raw.get(field)
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            result[field] = value
    if _number(raw.get("usd_cny")) and raw["usd_cny"] > 0:
        result["usd_cny"] = raw["usd_cny"]
    return result


def _classification_valid(classification, calls):
    if not isinstance(classification, dict):
        return False
    keys = ("known_calls", "unclassified_calls", "conflicting_calls", "subagent_calls")
    if (classification.get("method") != "captured_thread_id_parent_edges_and_project_metadata_v2"
            or any(type(classification.get(key)) is not int or not 0 <= classification[key] <= calls for key in keys)):
        return False
    known, unknown = classification["known_calls"], classification["unclassified_calls"]
    coverage = round(100 * known / calls, 4) if calls else 100.0
    return (known + unknown == calls
            and classification["conflicting_calls"] <= unknown
            and classification["subagent_calls"] <= known
            and _number(classification.get("coverage_percent"))
            and classification["coverage_percent"] == coverage)


def _model_usage(raw, totals):
    """Validate all model usage, without using priced models as a filter."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= len(MODEL_USAGE_LABELS):
        return []
    clean, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            return []
        model, part = row.get("model"), row.get("totals")
        if (not isinstance(model, str) or model not in MODEL_USAGE_LABELS or model in seen or not _valid_totals(part)
                or part["calls"] < 1):
            return []
        seen.add(model)
        clean.append({"model": model, "label": MODEL_USAGE_LABELS[model],
                      "totals": {key: part[key] for key in TOTAL_FIELDS}})
    if any(sum(row["totals"][key] for row in clean) != totals[key] for key in TOTAL_FIELDS):
        return []
    return sorted(clean, key=lambda row: (-row["totals"]["total_tokens"], row["model"]))


def _projects(raw, totals, classification_valid):
    if not classification_valid:
        return []
    rows = raw.get("by_project")
    if not isinstance(rows, list) or len(rows) != len(PROJECT_LABELS):
        return []
    if (any(not isinstance(row, dict) or row.get("key") not in PROJECT_LABELS or not _valid_totals(row.get("totals")) for row in rows) or
            {row["key"] for row in rows} != set(PROJECT_LABELS) or
            any(sum(row["totals"][key] for row in rows) != totals[key] for key in TOTAL_FIELDS)):
        return []
    classification = raw["classification"]
    if (sum(row["totals"]["calls"] for row in rows if row["key"] != "unclassified") != classification["known_calls"]
            or next(row["totals"]["calls"] for row in rows if row["key"] == "unclassified") != classification["unclassified_calls"]):
        return []
    return [{"key": row["key"], "label": PROJECT_LABELS[row["key"]],
             "totals": {key: row["totals"][key] for key in TOTAL_FIELDS},
             "pricing": _priced_subset(row.get("pricing"), row["totals"])} for row in rows]


def _display_projects(projects):
    """Merge verified unknown-purpose usage into Other for display only.

    Keep the source ledger and classification counters unchanged. Missing
    usage remains in unknown_usage_calls; a missing price remains unpriced.
    """
    indexed = {row["key"]: row for row in projects}
    other, unclassified = indexed["other"], indexed["unclassified"]
    totals = {key: other["totals"][key] + unclassified["totals"][key]
              for key in TOTAL_FIELDS}
    parts = (other["pricing"], unclassified["pricing"])
    pricing = {"valid": False, "complete": False}
    if all(part.get("valid") is True for part in parts):
        priced = sum(part["priced_calls"] for part in parts)
        unpriced = sum(part["unpriced_calls"] for part in parts)
        models = {}
        for part in parts:
            for row in part["by_model"]:
                item = models.setdefault(row["model"], {"calls": 0, "cost": Decimal(0)})
                item["calls"] += row["calls"]
                item["cost"] += Decimal(str(row["cost_cny"]))
        pricing = {"valid": True, "complete": False,
                   "basis": "dated_official_tariff_cny_estimate_not_bill",
                   "priced_calls": priced, "unpriced_calls": unpriced,
                   "by_model": [{"model": model, "calls": item["calls"],
                                 "cost_cny": float(item["cost"])}
                                for model, item in sorted(models.items())]}
        if priced:
            amount = float(sum((Decimal(str(part.get("known_cny", 0)))
                                for part in parts), Decimal(0)))
            pricing.update(known_cny=amount, display_cny=amount, complete=unpriced == 0)
            if pricing["complete"]:
                pricing["total_cny"] = amount
    merged = {"key": "other", "label": PROJECT_LABELS["other"],
              "totals": totals, "pricing": pricing}
    return [indexed["feiyu_bot"], indexed["academic_poc"], merged]


def project_cockpit_summary(raw):
    """Keep source identifiers internal; reports use numeric validated fields only."""
    if not isinstance(raw, dict):
        return {"ok": False, "report_mode": MODE, "reason": "cockpit_usage_unavailable"}
    base = {"ok": raw.get("ok") is True, "report_mode": MODE, "design_version": DESIGN,
            "source": "cockpit_request_ledger", "scope": "codex_client_all_upstream_accounts",
            "client_instance": ".codex", "local_records_only": True}
    try:
        window = freeze_report_window(raw)
    except ValueError:
        return {**base, "ok": False, "reason": "invalid_usage_window"}
    if window["kind"] == "daily":
        base["day"] = window["day"]
    else:
        base.update(window_hours=(datetime.fromisoformat(window["end"]) - datetime.fromisoformat(window["start"])).total_seconds() / 3600,
                    **{"from": window["start"], "until": window["end"]})
    if not base["ok"]:
        return {**base, "reason": "cockpit_usage_unavailable"}
    if raw.get("not_observed") is True:
        return {**base, "not_observed": True, "complete": False, "issues": ["no_records"],
                "pricing": {"valid": False, "complete": False}, "by_project": []}
    totals = raw.get("totals")
    if not _valid_totals(totals):
        return {**base, "ok": False, "reason": "cockpit_usage_invalid"}
    totals = {key: totals[key] for key in TOTAL_FIELDS}
    retained = _stamp(raw.get("retained_since"))
    issues = []
    if raw.get("partial_day") is True or retained is None or datetime.fromisoformat(retained) > datetime.fromisoformat(window["start"]):
        issues.append("coverage_incomplete")
    if totals["unknown_usage_calls"]:
        issues.append("usage_missing")
    classification = raw.get("classification")
    classification_valid = _classification_valid(classification, totals["calls"])
    classified = (classification_valid and classification["unclassified_calls"] == 0
                  and classification["conflicting_calls"] == 0)
    if not classified:
        issues.append("classification_missing")
    pricing = _priced_subset(raw.get("pricing"), totals)
    model_usage = _model_usage(raw.get("by_model_usage"), totals)
    if "by_model_usage" in raw and totals["calls"] and not model_usage:
        issues.append("model_usage_mismatch")
    if not pricing.get("valid") or pricing.get("priced_calls", 0) < totals["known_usage_calls"]:
        issues.append("pricing_missing")
    projects = _projects(raw, totals, classification_valid)
    if classification_valid and not projects:
        issues.append("project_mismatch")
    if projects:
        prices = [row["pricing"] for row in projects]
        reconciled = pricing.get("valid") and all(row.get("valid") for row in prices)
        if reconciled:
            reconciled = all(sum(row[field] for row in prices) == pricing[field] for field in ("priced_calls", "unpriced_calls"))
            amount_sum = sum((Decimal(str(row.get("known_cny", 0))) for row in prices), Decimal(0))
            reconciled = reconciled and abs(amount_sum - Decimal(str(pricing.get("known_cny", 0)))) <= Decimal("0.000001") * (len(prices) + 1)
        if not reconciled:
            issues.append("project_pricing_mismatch")
            for row in projects:
                row["pricing"] = {"valid": False, "complete": False}
        projects = _display_projects(projects)
    counters = {}
    for key in ("known_calls", "unclassified_calls", "conflicting_calls", "subagent_calls"):
        value = classification.get(key) if isinstance(classification, dict) else None
        if type(value) is int and 0 <= value <= totals["calls"]:
            counters[key] = value
    if classification_valid:
        counters["coverage_percent"] = classification["coverage_percent"]
    return {**base, "not_observed": False, "totals": totals, "complete": not issues and pricing.get("complete") is True,
            "issues": issues, "retained_since": retained,
            "classification": {"valid": classification_valid, "complete": classified,
                               "all_classified": classification_valid and classification["unclassified_calls"] == 0,
                               "basis": CLASSIFICATION_BASIS, **counters},
            "pricing": pricing, "by_project": projects, "by_model_usage": model_usage}


def report_readiness(summary, codex_summary=None, *, window=None):
    try:
        if not isinstance(summary, dict) or summary.get("report_mode") != MODE or summary.get("ok") is not True:
            raise ValueError
        frozen = freeze_report_window(summary)
        if window is not None and frozen != validate_report_window(window):
            return {"ready": False, "reason_codes": ["codex_window_mismatch"]}
        if summary.get("not_observed") is not True and not _valid_totals(summary.get("totals")):
            raise ValueError
        return {"ready": True, "complete": summary.get("complete") is True, "reason_codes": [], "window": frozen}
    except (ValueError, TypeError, OverflowError):
        return {"ready": False, "reason_codes": ["codex_summary_unavailable"]}


def _period(summary, compact=False):
    window = freeze_report_window(summary)
    begin, finish = (datetime.fromisoformat(window[key]) for key in ("start", "end"))
    if window["kind"] == "daily":
        return begin.strftime("%Y年%m月%d日") + " · 全天（UTC+8）"
    fmt = "%Y-%m-%d %H:%M:%S"
    return begin.strftime(fmt) + " — " + finish.strftime(fmt) + "（UTC+8）"


def build_cockpit_only_report(summary):
    if not report_readiness(summary)["ready"]:
        return "用量记录暂时不可用。"
    lines = [f"{PROJECT_USAGE_LABEL} · Token 手账", _period(summary)]
    if summary.get("not_observed"):
        return "\n".join(lines + ["这段时间没有找到本机用量记录。", PROJECT_SCOPE_LABEL + "。"])
    t, price = summary["totals"], summary["pricing"]
    lines += [f"已确认 {t['total_tokens']:,} token · 已记录 {t['calls']:,} 次请求。",
              f"用量记录 {t['known_usage_calls']:,} / {t['calls']:,} 次；成功 {t['success']:,}，失败 {t['failed']:,}。",
              f"非缓存读取输入 {t['uncached_input_tokens']:,}，缓存输入 {t['cached_tokens']:,}，输出 {t['output_tokens']:,}（含推理 {t['reasoning_tokens']:,}）。"]
    if t["unknown_usage_calls"]:
        lines.append(f"{t['unknown_usage_calls']:,} 次请求未回传完整用量，其 Token 未计入已确认总量。")
    if "display_cny" in price:
        lines.append(f"按官方单价折算 ¥{price['display_cny']:,.2f} · 计价覆盖 {price['priced_calls']:,} / {t['calls']:,} 次。")
    for row in summary.get("by_model_usage", []):
        part = row["totals"]
        lines.append(f"{row['label']}：已确认 {part['total_tokens']:,} token · {part['calls']:,} 次请求"
                     + (f"（{part['unknown_usage_calls']:,} 次未回传完整用量）" if part["unknown_usage_calls"] else "") + "。")
    if summary.get("by_model_usage"):
        lines.append("模型用量含未计价模型；缺失费率的金额仍待核实。")
    if "coverage_incomplete" in summary.get("issues", []):
        retained = summary.get("retained_since")
        if retained:
            lines.append("记录起点：" + datetime.fromisoformat(retained).strftime("%Y-%m-%d %H:%M") + "。")
    for row in summary.get("by_project", []):
        part = row["totals"]
        amount = f"已确认 {part['total_tokens']:,} token" if part["known_usage_calls"] or not part["calls"] else "用量待回传"
        text = f"{row['label']}：{amount} · {part['calls']:,} 次请求"
        if part["unknown_usage_calls"]:
            text += f"（{part['unknown_usage_calls']:,} 次未回传用量）"
        if row["pricing"].get("complete") is True and "display_cny" in row["pricing"]:
            text += f" · 官方单价折算 ¥{row['pricing']['display_cny']:,.2f}"
        elif row["pricing"].get("valid") is True and row["pricing"].get("priced_calls", 0) and "display_cny" in row["pricing"]:
            text += (f" · 已计价部分 ¥{row['pricing']['display_cny']:,.2f}"
                     f"（{row['pricing']['priced_calls']:,} / {part['calls']:,} 次）")
        lines.append(text + "。")
    if summary.get("by_project"):
        unclassified = summary.get("classification", {}).get("unclassified_calls", 0)
        if unclassified:
            lines.append(f"其他包含 {unclassified:,} 次尚未识别用途的请求。")
        lines.append(CLASSIFICATION_BASIS)
    return "\n".join(lines + [PROJECT_SCOPE_LABEL + "。"])


def report_theme(summary, *, asset_dir=None):
    frozen = freeze_report_window(summary)
    artwork = select_art({"from": frozen["start"], "until": frozen["end"]}, asset_dir)
    directory = Path(asset_dir) if asset_dir is not None else JOURNAL_ART_DIRECTORY
    asset = directory / artwork["asset_name"] if artwork["asset_name"] else None
    return {**artwork["theme"], "index": artwork["index"], "quote": artwork["quote"],
            "asset": asset, "artwork": artwork}


def _short(value):
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if value >= 10_000:
        return f"{value / 10_000:.2f} 万"
    return f"{value:,}"


def _project_cost_distribution(summary):
    """Project the current three display purposes; do not reclassify the ledger."""
    unavailable = {"state": "unavailable", "entries": []}
    projects, pricing, totals = summary.get("by_project"), summary.get("pricing"), summary.get("totals")
    classification = summary.get("classification")
    display_keys = {"feiyu_bot", "academic_poc", "other"}
    if (not isinstance(projects, list) or len(projects) != len(display_keys)
            or not isinstance(pricing, dict) or pricing.get("valid") is not True
            or not isinstance(classification, dict) or classification.get("valid") is not True
            or not _valid_totals(totals)):
        return unavailable
    if (any(not isinstance(row, dict) or not isinstance(row.get("key"), str)
            or row["key"] not in display_keys or row.get("label") != PROJECT_LABELS[row["key"]]
            or not _valid_totals(row.get("totals"))
            or not isinstance(row.get("pricing"), dict) or row["pricing"].get("valid") is not True
            for row in projects)
            or {row["key"] for row in projects} != display_keys
            or any(sum(row["totals"][field] for row in projects) != totals[field] for field in TOTAL_FIELDS)):
        return unavailable
    known, unknown = classification.get("known_calls"), classification.get("unclassified_calls")
    other = next(row for row in projects if row["key"] == "other")
    if (type(known) is not int or type(unknown) is not int or min(known, unknown) < 0
            or known + unknown != totals["calls"] or unknown > other["totals"]["calls"]
            or type(pricing.get("priced_calls")) is not int or pricing["priced_calls"] > totals["known_usage_calls"]
            or any(type(row["pricing"].get("priced_calls")) is not int
                   or row["pricing"]["priced_calls"] > row["totals"]["known_usage_calls"] for row in projects)):
        return unavailable
    entries = [{"key": row["key"], "label": row["label"], "calls": row["totals"]["calls"],
                "priced_calls": row["pricing"].get("priced_calls"),
                "unpriced_calls": row["pricing"].get("unpriced_calls"),
                "amount": row["pricing"].get("known_cny")} for row in projects]
    return make_cost_distribution(entries, priced_calls=pricing.get("priced_calls"),
                                  unpriced_calls=pricing.get("unpriced_calls"),
                                  expected_amount=pricing.get("known_cny"),
                                  # Upstream reconciliation uses four source purposes;
                                  # the current display then merges unclassified into Other.
                                  tolerance=Decimal("0.000001") * (len(PROJECT_LABELS) + 1))


def render_cockpit_usage_image(summary, *, output_dir, asset_dir=None):
    """Draw data with deterministic charting; illustrations never contain numbers."""
    if not report_readiness(summary)["ready"]:
        raise ValueError("usage_report_unavailable")
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    def font(size, bold=False):
        for name in (("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "simhei.ttf")):
            file = Path("C:/Windows/Fonts") / name
            if file.is_file():
                return ImageFont.truetype(str(file), size)
        raise RuntimeError("report_font_unavailable")

    theme = report_theme(summary, asset_dir=asset_dir)
    width, height = 1600, 1740
    image = Image.new("RGB", (width, height), theme["bg"])
    paint_background(image, theme)
    draw = ImageDraw.Draw(image)
    ink, muted, accent = theme["ink"], theme["muted"], theme["accent"]
    colors = theme["colors"]
    fonts = {size: font(size) for size in (22, 23, 24, 26, 28, 30, 34, 38)}
    bold = {size: font(size, True) for size in (30, 34, 38, 44, 50, 56, 66)}

    def text(x, y, value, size=28, color=None, heavy=False, max_width=None, fit=False):
        face = (bold if heavy else fonts).get(size) or font(size, heavy)
        value = str(value)
        if max_width is not None:
            if fit:
                while face.getlength(value) > max_width and size > 16:
                    size -= 1
                    face = font(size, heavy)
            else:
                while face.getlength(value) > max_width and len(value) > 1:
                    value = value[:-2] + "…"
        draw.text((x, y), value, font=face, fill=color or ink)

    def card(box, fill="#ffffff", radius=30):
        draw_card(draw, box, theme, radius=radius, fill=fill)

    def star(x, y, radius, color):
        points = [(x, y-radius), (x+radius*.25, y-radius*.25), (x+radius, y), (x+radius*.25, y+radius*.25),
                  (x, y+radius), (x-radius*.25, y+radius*.25), (x-radius, y), (x-radius*.25, y-radius*.25)]
        draw.polygon(points, fill=color)

    # A roomy illustration area stays outside all numeric/chart regions.
    draw.ellipse((1130, 22, 1670, 562), fill=theme["pale"])
    draw.ellipse((-180, 1450, 280, 1910), fill=theme["pale"])
    for x, y, radius in ((1060, 110, 12), (1510, 95, 18), (1120, 330, 18), (1495, 390, 11), (118, 387, 10)):
        star(x, y, radius, accent)
    art = theme["asset"]
    paste_scene(image, theme["artwork"]["image"], (1152, 34, 1545, 388), theme)
    draw_title_strip(draw, (72, 48, 700, 94), theme)
    text(80, 60, "FEIYU  /  TOKEN JOURNAL", 23, accent)
    text(80, 112, "肥鱼的 Token 手账", 56, heavy=True)
    text(84, 210, _period(summary, compact=True), 26, muted, max_width=1010)
    text(84, 272, theme["quote"], 30, accent)
    text(84, 337, PROJECT_USAGE_LABEL + " · " + theme["name"], 24, muted)

    if summary.get("not_observed"):
        card((60, 440, 1540, 1500))
        text(110, 550, "这段时间没有找到本机用量记录。", 38, heavy=True)
        text(110, 625, "有新的记录时，再来翻开这页手账。", 28, muted)
    else:
        t, pricing = summary["totals"], summary["pricing"]
        metric_boxes = ((60, 430, 540, 655), (560, 430, 1040, 655), (1060, 430, 1540, 655))
        for box in metric_boxes:
            card(box)
        text(92, 460, "已确认 Token", 26, muted)
        no_reported_usage = t["calls"] > 0 and t["known_usage_calls"] == 0
        text(92, 508, "待回传" if no_reported_usage else _short(t["total_tokens"]), 50, heavy=True)
        text(94, 590, "暂无完整用量" if no_reported_usage else f"{t['total_tokens']:,} token", 23, muted)
        text(592, 460, "已记录请求", 26, muted)
        text(592, 508, f"{t['calls']:,}", 50, heavy=True, max_width=410, fit=True)
        text(594, 590, f"成功 {t['success']:,}  /  失败 {t['failed']:,}", 23, muted, max_width=410)
        text(1092, 460, "按官方单价折算", 26, muted)
        amount = f"¥{pricing['display_cny']:,.2f}" if "display_cny" in pricing else "—"
        text(1092, 508, amount, 50, accent, heavy=True, max_width=410, fit=True)
        covered = pricing.get("priced_calls", 0)
        text(1094, 590, f"计价覆盖 {covered:,} / {t['calls']:,} 次", 23, muted, max_width=410)

        card((60, 680, 1540, 1130))
        text(98, 712, "已确认 Token 构成", 34, heavy=True)
        values = (t["uncached_input_tokens"], t["cached_tokens"], t["output_tokens"])
        labels = ("非缓存读取输入", "缓存输入", "输出")
        total = sum(values)
        bbox = (118, 788, 428, 1098)
        draw.ellipse(bbox, fill=theme["pale"])
        angle = -90.0
        if total:
            for value, color in zip(values, colors):
                sweep = 360 * value / total
                if value:
                    draw.pieslice(bbox, angle, angle+sweep, fill=color)
                angle += sweep
        draw.ellipse((174, 844, 372, 1042), fill="#ffffff")
        value_text = _short(total)
        text(200, 894, "TOTAL", 22, muted)
        text(186, 933, value_text, 30, heavy=True, max_width=180)
        for index, (label, value, color) in enumerate(zip(labels, values, colors)):
            y = 798 + index * 91
            draw.rounded_rectangle((535, y+10, 553, y+28), radius=5, fill=color)
            text(575, y, label, 28, heavy=True)
            text(835, y, f"{value:,}", 28, max_width=385)
            percent = 100 * value / total if total else 0
            text(1305, y, f"{percent:.2f}%", 28, color, max_width=150)
        text(575, 1070, f"推理 {t['reasoning_tokens']:,}，已包含在输出中", 24, muted)

        card((60, 1155, 785, 1515))
        card((810, 1155, 1540, 1515))
        projects = summary.get("by_project", [])
        if projects:
            text(98, 1188, "用途分布 · 已确认", 34, heavy=True)
            maximum = max((row["totals"]["total_tokens"] for row in projects), default=0)
            for index, row in enumerate(projects):
                y = 1244+index*56
                part = row["totals"]
                value = part["total_tokens"]
                text(98, y, row["label"], 24)
                amount = _short(value) if part["known_usage_calls"] or not part["calls"] else "待回传"
                text(560, y, amount, 24, muted, max_width=185)
                if part["unknown_usage_calls"]:
                    text(560, y+27, f"未回传 {part['unknown_usage_calls']:,} 次", 20, muted, max_width=185, fit=True)
                draw.rounded_rectangle((245, y+12, 530, y+30), radius=9, fill=theme["pale"])
                if value and maximum:
                    color = colors[index] if index < len(colors) else muted
                    draw.rounded_rectangle((245, y+12, 245+max(8, 285*value/maximum), y+30), radius=9, fill=color)
            unknown = summary.get("classification", {}).get("unclassified_calls", 0)
            text(98, 1467, f"其他含未归类 {unknown:,} 次请求", 20, muted, max_width=650)
            text(98, 1493, CLASSIFICATION_BASIS, 18, muted, max_width=650, fit=True)
        else:
            text(98, 1188, "请求概览", 34, heavy=True)
            rows = (("用量记录", t["known_usage_calls"]), ("未回传用量", t["unknown_usage_calls"]), ("已计价请求", covered))
            for index, (label, value) in enumerate(rows):
                y = 1257+index*70
                text(98, y, label, 24)
                text(520, y, f"{value:,} 次", 26, colors[index], heavy=True, max_width=220)

        model_usage = _model_usage(summary.get("by_model_usage"), t)
        if model_usage:
            text(848, 1188, "模型用量 · 包含未计价模型", 29, heavy=True)
            text(850, 1240, "模型", 21, muted)
            text(1118, 1240, "已确认 Token", 21, muted)
            text(1315, 1240, "请求 / 未回传", 21, muted)
            for index, row in enumerate(model_usage):
                y, part = 1282+index*38, row["totals"]
                text(850, y, row["label"], 23, max_width=250)
                text(1118, y, _short(part["total_tokens"]), 23, accent, max_width=180, fit=True)
                text(1315, y, f"{part['calls']:,} / {part['unknown_usage_calls']:,}", 22, muted,
                     max_width=190, fit=True)
            text(850, 1513, "缺失费率的金额仍待核实", 20, muted)
        else:
            # Retained reports created before model aggregation remain usable.
            text(848, 1188, "官方单价折算 · 模型", 30, heavy=True)
            models = sorted(pricing.get("by_model", []), key=lambda row: row["cost_cny"], reverse=True)
            if models:
                for index, row in enumerate(models[:3]):
                    y = 1255+index*72
                    text(850, y, row["model"], 24, max_width=420)
                    text(1280, y, f"¥{row['cost_cny']:,.2f}", 26, accent, heavy=True, max_width=220, fit=True)
                if len(models)>3:
                    text(850, 1470, f"另有 {len(models)-3} 个模型", 22, muted)
            else:
                text(850, 1280, "这页暂时没有计价记录。", 26, muted)
        info = [f"用量记录 {t['known_usage_calls']:,} / {t['calls']:,} 次",
                f"未回传完整用量 {t['unknown_usage_calls']:,} 次"]
        if pricing.get("price_date"):
            info.append("费率 " + pricing["price_date"])
        if pricing.get("usd_cny"):
            info.append(f"USD/CNY {pricing['usd_cny']:.4f}")
        text(82, 1560, "  ·  ".join(info), 23, muted, max_width=1430)
        if "coverage_incomplete" in summary.get("issues", []) and summary.get("retained_since"):
            text(82, 1603, "记录起点 " + datetime.fromisoformat(summary["retained_since"]).strftime("%Y-%m-%d %H:%M"), 22, muted)
    text(82, 1630, PROJECT_SCOPE_LABEL, 22, muted, max_width=1430)
    attributed_art = theme["artwork"]["image"] is not None
    if attributed_art:
        for index, line in enumerate(ART_ATTRIBUTION):
            text(82, 1660 + index * 34, line, 22, muted, max_width=1430, fit=True)
    if summary.get("not_observed") is not True:
        cost = _project_cost_distribution(summary)
        cost_top = 680
        cost_bottom = cost_top + cost_card_height(cost)
        offset = cost_bottom - cost_top + 30
        expanded = Image.new("RGB", (width, height + offset), theme["bg"])
        paint_background(expanded, theme)
        expanded.paste(image.crop((0, 0, width, cost_top)), (0, 0))
        expanded.paste(image.crop((0, cost_top, width, height)), (0, cost_top + offset))
        draw_cost_card(ImageDraw.Draw(expanded), (60, cost_top, 1540, cost_bottom), cost, theme, font,
                       title="金额分布 · 按用途")
        image = expanded
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    key = {"summary": summary, "design": DESIGN, "cost_pie": COST_PIE_VERSION, "theme": theme["name"],
           "art_version": ART_VERSION, "asset_sha256": theme["artwork"]["asset_hash"]}
    digest = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:24]
    path = output / f"feiyu-token-{digest}.png"
    image.save(path, format="PNG")
    return path
