"""Render private numeric usage summaries without a model call or account data."""

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .official_billing import primary_billing_for_summary, primary_token_usage_for_summary
from .token_usage_store import FEATURE_LABELS, LOCAL_TIMEZONE

ROOT = Path(__file__).resolve().parents[4]
WIDTH = 1400
MAX_COUNT = 10**18
_FONT_PATHS = (
    ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
    ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
)
BG = "#EDF5FF"
INK = "#172A43"
MUTED = "#64748B"
BLUE = "#4978DE"
TEAL = "#219B93"
GOLD = "#D99037"
PURPLE = "#9365CE"
MAX_CNY = 10**12
ART_PATH = Path(__file__).with_name("assets") / "token-report-feiyu.png"
STICKER_PATHS = {
    "peek": ART_PATH.with_name("token-report-feiyu-peek.png"),
    "rest": ART_PATH.with_name("token-report-feiyu-rest.png"),
}
# Unknown model/account labels are never rendered, even if they look like IDs.
SAFE_MODEL_IDS = frozenset({
    "deepseek-flash", "gpt-6.1-sol", "gpt-6-luna",
})
PROJECT_LABELS = {
    "feiyu_bot": "肥鱼相关",
    "academic_poc": "论文",
    "other": "其他",
}


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNT else 0


def _money(value):
    return float(value) if (type(value) in (int, float)
                            and 0 <= value <= MAX_CNY and math.isfinite(value)) else None


def _pricing(raw):
    """Keep only bounded amounts, fixed labels and a validated exchange date."""
    if not isinstance(raw, dict):
        return None
    known = _money(raw.get("known_cny"))
    if known is None:
        return None
    total = _money(raw.get("total_cny"))
    unpriced = _count(raw.get("unpriced_calls"))
    valid_unpriced = type(raw.get("unpriced_calls")) is int and 0 <= raw["unpriced_calls"] <= MAX_COUNT
    complete = (raw.get("complete") is True and total is not None and valid_unpriced and not unpriced
                and math.isclose(total, known, rel_tol=1e-8, abs_tol=1e-8))
    rate = _money(raw.get("usd_cny"))
    rate = rate if rate and rate <= 1000 else None
    day = raw.get("rate_date")
    try:
        day = datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") if isinstance(day, str) and len(day) == 10 else ""
    except (ValueError, OverflowError):
        day = ""
    features, models, model_calls = {}, {}, 0
    for rows, key, allowed, target in (
        (raw.get("by_feature"), "feature", FEATURE_LABELS, features),
        (raw.get("by_model"), "model", SAFE_MODEL_IDS, models),
    ):
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows[:200]:
            if not isinstance(row, dict):
                continue
            label, cost = row.get(key), _money(row.get("cost_cny"))
            if isinstance(label, str) and label in allowed and cost is not None:
                target[label] = min(MAX_CNY, target.get(label, 0) + cost)
                if key == "model":
                    model_calls = min(MAX_COUNT, model_calls + _count(row.get("calls")))
    priced_calls = raw.get("priced_calls")
    priced_calls = priced_calls if type(priced_calls) is int and 0 <= priced_calls <= MAX_COUNT else model_calls
    return {"known_cny": known, "total_cny": total if complete else None,
            "complete": complete, "unpriced_calls": unpriced, "priced_calls": priced_calls,
            "usd_cny": rate, "rate_date": day, "features": features, "models": models}


def _cny(value, *, detail=False):
    if value is None:
        return "费用待核实"
    if 0 < value < 0.0001:
        return "¥<0.0001"
    decimals = 4 if detail and 0 < value < 1 else 2
    return f"¥{value:,.{decimals}f}"


def _font(size, bold=False):
    for regular, heavy in _FONT_PATHS:
        path = heavy if bold else regular
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError("Chinese usage-report font is unavailable")


def _date(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            return None
        return result.astimezone(LOCAL_TIMEZONE)
    except (ValueError, OverflowError):
        return None


def _report_window(raw):
    if not isinstance(raw, dict):
        return None
    start, end = _date(raw.get("from")), _date(raw.get("until"))
    if start and end and start < end and end - start <= timedelta(days=31):
        return start, end
    day = raw.get("day")
    if not isinstance(day, str) or len(day) != 10:
        return None
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TIMEZONE)
        return start, start + timedelta(days=1)
    except (ValueError, OverflowError):
        return None


def _official_money(value):
    """Official CSV amounts are decimal strings, never arbitrary display text."""
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return None
    try:
        number = Decimal(value)
        canonical = format(number, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        fraction = canonical.partition(".")[2]
        return canonical if number.is_finite() and 0 <= number <= MAX_CNY and len(fraction) <= 12 else None
    except InvalidOperation:
        return None


def _primary_billing(raw):
    """Validate the shared fee selector's bounded output before drawing it."""
    fallback = {"status": "unverified", "label": "人民币费用", "cost_cny": None}
    if not isinstance(raw, dict) or raw.get("status") not in ("confirmed", "estimated", "unverified"):
        return fallback
    status = raw["status"]
    if status == "unverified":
        return fallback
    cost = _official_money(raw.get("cost_cny"))
    if cost is None:
        return fallback
    labels = {"confirmed": ("官网核实费用", "官网所选Key费用", "官网筛选范围费用", "官网已核实费用"),
              "estimated": ("官方价估算",)}
    supplied = raw.get("label")
    label = supplied if isinstance(supplied, str) and supplied in labels[status] else labels[status][0]
    return {"status": status, "label": label, "cost_cny": cost}


def _primary_token_usage(raw, local, primary):
    """Keep headline/composition sources distinct from local classifications."""
    result = {"source": "unavailable", "total_tokens": None,
              "composition_source": "unavailable", "composition_total_tokens": None,
              "input_other": None, "input_cached": None, "output": None}
    if local["state"] == "available":
        result.update(source="local", total_tokens=local["total"],
                      composition_source="local", composition_total_tokens=local["total"],
                      input_other=local["uncached"], input_cached=local["cached"], output=local["output"])
    if not isinstance(raw, dict) or primary["status"] != "confirmed" or raw.get("source") != "official":
        return result
    total = raw.get("total_tokens")
    if type(total) is not int or not 0 <= total <= MAX_COUNT:
        return result
    result.update(source="official", total_tokens=total)
    parts = {key: raw.get(key) for key in ("input_other", "input_cached", "output")}
    if (raw.get("composition_source") == "official"
            and type(raw.get("composition_total_tokens")) is int
            and raw["composition_total_tokens"] == total
            and all(type(value) is int and 0 <= value <= MAX_COUNT for value in parts.values())
            and sum(parts.values()) == total):
        result.update(composition_source="official", composition_total_tokens=total, **parts)
    return result


def _period(feiyu, codex):
    """Use validated dates/numbers; caller-supplied labels never reach pixels."""
    for source in (feiyu, codex):
        if not isinstance(source, dict):
            continue
        hours = source.get("window_hours")
        start, end = _date(source.get("from")), _date(source.get("until"))
        if (type(hours) in (int, float) and 0.5 <= hours <= 720
                and math.isfinite(hours) and start and end and start < end):
            return (f"过去 {hours:g} 小时", start.strftime("%Y-%m-%d %H:%M"),
                    end.strftime("%Y-%m-%d %H:%M"))
        day = source.get("day")
        if not isinstance(day, str) or len(day) != 10:
            continue
        try:
            start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TIMEZONE)
            return (start.strftime("%Y年%m月%d日"), start.strftime("%Y-%m-%d %H:%M"),
                    (start + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"))
        except (ValueError, OverflowError):
            pass
    return "用量概览", "", ""


def _groups(rows, key, labels):
    combined = {}
    if not isinstance(rows, (list, tuple)):
        return []
    for row in rows[:200]:
        if not isinstance(row, dict):
            continue
        category = row.get(key)
        category = category if isinstance(category, str) and category in labels else "other"
        item = combined.setdefault(category, {"key": category, "label": labels[category],
                                               "total": 0, "calls": 0, "known": 0, "unknown": 0})
        item["total"] += _count(row.get("total"))
        item["calls"] += _count(row.get("calls"))
        item["known"] += _count(row.get("known_usage_calls"))
        item["unknown"] += _count(row.get("unknown_usage_calls"))
    return sorted(combined.values(), key=lambda item: (-item["total"], item["key"]))


def _normalise_summary(raw, *, codex=False):
    """Allow only fixed labels and numeric metadata into the report model."""
    result = {"state": "unavailable", "total": 0, "calls": 0, "success": 0,
              "failed": 0, "cancelled": 0, "known": 0, "unknown": 0,
              "uncached": 0, "cached": 0, "output": 0, "reasoning": 0,
              "accounts": 0, "partial": False, "coverage_start": None,
              "features": [], "pricing": None, "projects": []}
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        return result
    if raw.get("not_observed"):
        result["state"] = "not_observed"
        return result
    t = raw.get("totals")
    if not isinstance(t, dict):
        return result
    fields = (("input_tokens", "cached_tokens", "output_tokens", "total_tokens")
              if codex else ("input_other", "input_cached", "output", "total"))
    if any(type(t.get(k)) is not int or not 0 <= t[k] <= MAX_COUNT for k in fields):
        return result
    uncached = t["input_tokens"] - t["cached_tokens"] if codex else t["input_other"]
    cached, output = t[fields[1]], t[fields[2]]
    if uncached < 0 or uncached + cached + output != t[fields[3]]:
        return result
    result.update(state="available", total=uncached + cached + output,
                  uncached=uncached, cached=cached, output=output,
                  calls=_count(t.get("calls")), success=_count(t.get("success")),
                  failed=_count(t.get("failed")), cancelled=_count(t.get("cancelled")),
                  known=_count(t.get("known_usage_calls")), unknown=_count(t.get("unknown_usage_calls")),
                  partial=raw.get("partial_day") is True, pricing=_pricing(raw.get("pricing")))
    if codex:
        result.update(reasoning=min(_count(t.get("reasoning_tokens")), output),
                      accounts=_count(raw.get("upstream_account_count")),
                      projects=_project_rows(raw.get("by_project")))
    else:
        result.update(features=_groups(raw.get("by_feature"), "feature", FEATURE_LABELS))
        observed, window = _date(raw.get("observed_since")), _report_window(raw)
        if observed and window and observed <= window[1]:
            result["coverage_start"] = max(observed, window[0]).isoformat()
            result["partial"] = result["partial"] or observed > window[0]
    return result


def _project_rows(raw):
    """Read three fixed project groups, never labels, threads or account IDs."""
    if not isinstance(raw, (list, tuple)):
        return []
    rows = {}
    for row in raw[:200]:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if not isinstance(key, str) or key not in PROJECT_LABELS:
            continue
        if key in rows:
            # Duplicate category totals cannot be reconciled safely here.
            rows[key] = _normalise_summary({}, codex=True)
            continue
        rows[key] = _normalise_summary({"ok": True, "totals": row.get("totals"),
                                        "pricing": row.get("pricing")}, codex=True)
    if not rows:
        return []
    return [{"key": key, "label": label,
             **{field: rows.get(key, {}).get(field, default) for field, default in
                (("state", "unavailable"), ("total", 0), ("calls", 0),
                 ("known", 0), ("unknown", 0), ("pricing", None))}}
            for key, label in PROJECT_LABELS.items()]


def _feature_rows(summary):
    prices = summary["pricing"]["features"] if summary.get("pricing") else {}
    source = [{**row, "cost_cny": prices.get(row["key"])} for row in summary["features"]]
    rows = source[:8]
    remaining = source[8:]
    if remaining:
        rows = [*rows, {"key": "remaining", "label": "其余功能合计",
                       "cost_cny": sum(row["cost_cny"] for row in remaining) if all(row["cost_cny"] is not None for row in remaining) else None,
                       **{k: sum(row[k] for row in remaining) for k in ("total", "calls", "known", "unknown")}}]
    return rows


class _Canvas:
    def __init__(self, height):
        self.image = Image.new("RGB", (WIDTH, height), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.fonts = {}

    def font(self, size, bold=False):
        if (size, bold) not in self.fonts:
            self.fonts[size, bold] = _font(size, bold)
        return self.fonts[size, bold]

    def text(self, xy, value, size=26, color=INK, bold=False):
        self.draw.text(xy, str(value), font=self.font(size, bold), fill=color, stroke_width=0)

    def right(self, right, y, value, size=26, color=INK, bold=False):
        value = str(value)
        width = self.draw.textlength(value, font=self.font(size, bold))
        self.text((right - width, y), value, size, color, bold)

    def wrap(self, value, x, y, max_width, *, size=24, color=MUTED, spacing=12):
        line = ""
        for char in value:
            if char == "\n" or (line and self.draw.textlength(line + char, font=self.font(size)) > max_width):
                self.text((x, y), line, size, color)
                y += size + spacing
                line = "" if char == "\n" else char
            else:
                line += char
        if line:
            self.text((x, y), line, size, color)
            y += size + spacing
        return y

    def card(self, box):
        x0, y0, x1, y1 = box
        self.draw.rounded_rectangle((x0, y0 + 5, x1, y1 + 5), radius=26, fill="#E4EAF4")
        self.draw.rounded_rectangle(box, radius=26, fill="white")


def _state_message(summary):
    if summary["state"] == "not_observed":
        return "该时段没有可核实的历史记录", "未倒算历史用量，也没有按零消耗计。"
    return "本机统计暂不可用", "暂时无法核实用量，没有按零消耗计。"


def _sticker(canvas, name, box):
    """Lay out fixed local character art away from the financial figures."""
    source_path = STICKER_PATHS.get(name)
    if source_path is None:
        return False
    try:
        with Image.open(source_path) as source:
            if source.width > 2000 or source.height > 2000:
                return False
            art = source.convert("RGB")
            if name == "rest":
                # Show the sleepy girl and whale, leaving the source's greeting
                # outside the frame so the sticker works at any report time.
                art = art.crop((0, round(art.height * 0.28), art.width, art.height))
            size = (box[2] - box[0], box[3] - box[1])
            art = ImageOps.fit(art, size, method=Image.Resampling.LANCZOS)
        mask = Image.new("L", size)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=18, fill=255)
        canvas.image.paste(art, (box[0], box[1]), mask)
        return True
    except (OSError, ValueError):
        return False


def _overview(canvas, summary, *, x, y, width, codex=False, primary=None, token_usage=None):
    canvas.card((x, y, x + width, y + 344))
    accent = TEAL if codex else BLUE
    canvas.draw.rounded_rectangle((x + 28, y + 32, x + 35, y + 66), radius=3, fill=accent)
    canvas.text((x + 52, y + 27), "Codex" if codex else "DeepSeek 大肥鱼", 34, bold=True)
    if codex:
        canvas.right(x + width - 30, y + 36, "多账号合计", 22, MUTED)
    else:
        _sticker(canvas, "peek", (x + width - 143, y + 18, x + width - 35, y + 118))
    if codex and summary["state"] != "available":
        first, second = _state_message(summary)
        canvas.text((x + 30, y + 112), "—", 60, color=accent, bold=True)
        canvas.text((x + 30, y + 201), first, 26)
        canvas.text((x + 30, y + 248), "费用待核实", 25, MUTED)
        return
    pricing = summary["pricing"]
    if codex:
        pending = (not pricing or not pricing["complete"] and not pricing["known_cny"]
                   and not pricing["priced_calls"])
        title = "人民币费用" if pending else "官方价估算" if pricing["complete"] else "已定价部分"
        amount = "费用待核实" if pending else _cny(pricing["known_cny"])
    else:
        primary = _primary_billing(primary)
        pending = primary["status"] == "unverified"
        title = primary["label"]
        amount = ("待核实" if pending else _official_cny(primary["cost_cny"])
                  if primary["status"] == "confirmed" else _cny(float(primary["cost_cny"])))
    canvas.text((x + 30, y + 90), title, 24, MUTED)
    size = 54 if len(amount) < 15 else 41
    if pending:
        size = 44
    while size > 30 and canvas.draw.textlength(amount, font=canvas.font(size, True)) > width - 60:
        size -= 2
    canvas.text((x + 28, y + 118), amount, size, accent, True)
    incomplete = summary["partial"] or summary["unknown"] or summary["state"] != "available"
    if not codex and pending:
        canvas.text((x + 30, y + 181), "完整时段费用待核实", 23, GOLD, True)
    elif not codex and primary["status"] == "confirmed":
        note = "官网账号口径 · 明细为已记录部分" if incomplete else "官网账号口径"
        canvas.text((x + 30, y + 181), note, 21, MUTED)
    coverage = _date(summary.get("coverage_start"))
    if not codex and coverage:
        canvas.text((x + 30, y + 213), "本机记录自 " + coverage.strftime("%m-%d %H:%M"), 19, MUTED)
    official_tokens = not codex and token_usage and token_usage["source"] == "official"
    if summary["state"] != "available" and not official_tokens:
        canvas.text((x + 30, y + 243), "—", 34, bold=True)
        canvas.text((x + 30, y + 289), _state_message(summary)[0], 24, MUTED)
        return
    total = token_usage["total_tokens"] if official_tokens else summary["total"]
    number = f"{total:,}"
    canvas.text((x + 30, y + 243), number, 32 if len(number) < 19 else 27, bold=True)
    token_label = "已核实 token" if codex else "官网核实 token" if official_tokens else "本机已记录 token"
    canvas.right(x + width - 30, y + 250, token_label, 23, MUTED)
    if summary["state"] != "available":
        canvas.text((x + 30, y + 289), "本机请求记录不可用", 23, bold=True)
        canvas.text((x + 30, y + 318), _state_message(summary)[0], 19, MUTED)
        return
    calls = f"{summary['calls']:,} 次请求" if codex else f"本机 {summary['calls']:,} 次请求"
    canvas.text((x + 30, y + 289), calls, 23, bold=True)
    detail = (f"涉及 {summary['accounts']:,} 个上游账号" if codex
              else f"成功 {summary['success']:,}  ·  失败 {summary['failed']:,}  ·  取消 {summary['cancelled']:,}")
    if codex and summary["unknown"]:
        detail = f"{summary['accounts']:,} 个账号 · {summary['unknown']:,} 次用量未回传"
    elif summary["unknown"]:
        detail = f"成功 {summary['success']:,} · {summary['unknown']:,} 次用量未回传"
    canvas.text((x + 30, y + 318), detail, 19, MUTED)


def _official_cny(value):
    number = Decimal(value)
    text = format(number, ",f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if "." not in text:
        text += ".00"
    elif len(text.rsplit(".", 1)[1]) == 1:
        text += "0"
    return "¥" + text


def _composition(canvas, summary, y, *, codex=False, token_usage=None):
    """Each bar is normalized within its own service, never across services."""
    models = summary["pricing"]["models"] if codex and summary.get("pricing") else {}
    canvas.card((50, y, 1350, y + (302 if models else 244)))
    title = "Codex 用量构成" if codex else "肥鱼用量构成"
    if not codex:
        title += " · 官网核实" if token_usage["composition_source"] == "official" else " · 本机已记录部分"
    canvas.text((80, y + 25), title, 31, bold=True)
    composition = summary if codex else {
        "total": token_usage["composition_total_tokens"],
        "uncached": token_usage["input_other"], "cached": token_usage["input_cached"],
        "output": token_usage["output"],
    }
    total = composition["total"]
    parts = [("非缓存输入", "uncached", BLUE), ("缓存输入", "cached", TEAL), ("输出", "output", GOLD)]
    if (codex and summary["state"] != "available") or (not codex and token_usage["composition_source"] == "unavailable"):
        canvas.text((80, y + 103), _state_message(summary)[1], 26, MUTED)
        return
    bar_y, cursor, remaining = y + 91, 80, 1240
    canvas.draw.rounded_rectangle((80, bar_y, 1320, bar_y + 23), radius=10, fill="#EDF1F7")
    for i, (_, key, color) in enumerate(parts):
        width = remaining if i == len(parts) - 1 and total else round(composition[key] / total * 1240) if total else 0
        width = min(width, remaining)
        if width:
            canvas.draw.rectangle((cursor, bar_y, cursor + width, bar_y + 23), fill=color)
        cursor += width
        remaining -= width
    for i, (label, key, color) in enumerate(parts):
        x = 80 + i * 416
        canvas.draw.ellipse((x, y + 146, x + 12, y + 158), fill=color)
        canvas.text((x + 24, y + 136), label, 24, MUTED)
        canvas.text((x + 24, y + 176), f"{composition[key]:,}", 30, bold=True)
    if models:
        detail = "  ·  ".join(f"{model} {_cny(amount, detail=True)}"
                             for model, amount in sorted(models.items())[:3])
        canvas.text((80, y + 244), detail, 23, MUTED)


def _project_card(canvas, summary, y):
    rows = summary["projects"]
    height = 105 + 76 * len(rows)
    canvas.card((50, y, 1350, y + height))
    canvas.text((80, y + 26), "Codex · 三类任务开销", 31, bold=True)
    canvas.right(1320, y + 36, "人民币 / 已核实 token / 占比", 22, MUTED)
    for index, row in enumerate(rows):
        yy = y + 94 + index * 76
        canvas.draw.ellipse((82, yy + 11, 94, yy + 23), fill=(BLUE, TEAL, PURPLE)[index])
        canvas.text((112, yy), row["label"], 26, bold=True)
        known_tokens = row["state"] == "available" and (not row["unknown"] or row["known"])
        ratio = min(1.0, row["total"] / summary["total"]) if known_tokens and summary["total"] else 0
        tokens = f"{row['total']:,}  ·  {ratio:.1%}" if known_tokens else "用量待核实"
        price = row["pricing"]
        if row["state"] != "available" or not price or (
                not price["complete"] and not price["known_cny"] and not price["priced_calls"]):
            cost = "费用待核实"
        else:
            cost = ("" if price["complete"] else "已定价 ") + _cny(price["known_cny"], detail=True)
        canvas.right(1318, yy, f"{cost}  ·  {tokens}", 24)
        if row["unknown"]:
            canvas.text((112, yy + 35), f"{row['unknown']:,} 次用量未回传", 19, MUTED)
        canvas.draw.line((112, yy + 65, 1318, yy + 65), fill="#EDF1F7", width=2)


def _header_art(canvas):
    """Place an unchanged first frame from the installed DeepSeek-girl pack."""
    canvas.draw.ellipse((880, 25, 925, 70), fill="#D7E7FC")
    canvas.draw.ellipse((1330, 220, 1360, 250), fill="#D7E7FC")
    canvas.draw.ellipse((950, 210, 970, 230), fill="#C5E7E9")
    box = (990, 40, 1350, 243)
    canvas.draw.rounded_rectangle((984, 34, 1356, 249), radius=29, fill="white")
    try:
        with Image.open(ART_PATH) as source:
            if source.width > 2000 or source.height > 2000:
                raise ValueError("Report character asset is oversized")
            art = ImageOps.fit(source.convert("RGB"), (360, 203), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", art.size)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 359, 202), radius=23, fill=255)
        canvas.image.paste(art, (box[0], box[1]), mask)
    except (OSError, ValueError):
        # A missing optional sticker never stops a financial report.
        canvas.draw.ellipse((1060, 100, 1272, 195), fill=BLUE)
        canvas.draw.polygon(((1260, 140), (1320, 100), (1300, 160), (1320, 197)), fill=BLUE)
        canvas.draw.ellipse((1110, 128, 1122, 140), fill=INK)
        canvas.draw.arc((1130, 126, 1175, 163), 5, 160, fill=INK, width=3)


def _price_basis(feiyu, codex):
    rates = []
    for summary in (feiyu, codex):
        pricing = summary.get("pricing")
        if not pricing or not pricing["usd_cny"]:
            continue
        pair = (pricing["usd_cny"], pricing["rate_date"])
        if pair not in rates:
            rates.append(pair)
    if not rates:
        return "功能明细及 Codex 按官方价折算 · 非实际扣款账单"
    rate, day = rates[0]
    suffix = f"（{day}）" if day else ""
    return f"功能明细及 Codex 按官方价折算 · 美元兑人民币 {rate:.4f}{suffix} · 非实际扣款账单"


def render_usage_image(feiyu_summary, codex_summary, output_dir=None):
    """Return an absolute PNG Path; consumes only aggregate metadata.

    Default storage is D:/QQBot/data/token-usage/reports when deployed. File
    names are hashes of a privacy-filtered report model, never user text.
    Unsupported data remains explicitly unknown rather than a fabricated zero.
    """
    feiyu = _normalise_summary(feiyu_summary)
    codex = _normalise_summary(codex_summary, codex=True)
    period = _period(feiyu_summary, codex_summary)
    primary = _primary_billing(primary_billing_for_summary(feiyu_summary))
    token_usage = _primary_token_usage(primary_token_usage_for_summary(feiyu_summary), feiyu, primary)
    features = _feature_rows(feiyu)
    autonomous = any(row["key"] == "autonomous_plan" for row in feiyu["features"])
    feature_height = max(220, 150 + 82 * len(features)) + (32 if autonomous else 0)
    feature_y = 956
    codex_y = feature_y + feature_height + 24
    has_models = bool(codex["pricing"] and codex["pricing"]["models"])
    project_y = codex_y + (302 if has_models else 244) + 24
    project_height = 105 + 76 * len(codex["projects"]) + 24 if codex["projects"] else 0
    height = project_y - 24 + project_height + 244
    model = {"v": 10, "period": period, "feiyu": feiyu, "codex": codex,
             "primary_billing": primary, "primary_token_usage": token_usage}
    digest = hashlib.sha256(json.dumps(model, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:24]
    directory = Path(output_dir) if output_dir is not None else ROOT / "data" / "token-usage" / "reports"
    if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
        raise OSError("Usage report directory cannot be linked")
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    path = directory / f"usage-{digest}.png"
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise OSError("Usage report file cannot be linked")
    if path.is_file():
        try:
            with Image.open(path) as cached:
                if cached.format == "PNG" and cached.size == (WIDTH, height):
                    return path
        except (OSError, ValueError):
            pass
    c = _Canvas(height)
    c.draw.rectangle((0, 0, WIDTH, 12), fill=BLUE)
    _header_art(c)
    c.text((55, 46), "大肥鱼的开销手账", 54, bold=True)
    c.text((58, 125), period[0], 32, BLUE, True)
    subtitle = f"{period[1]}  至  {period[2]}  ·  UTC+8" if period[1] else "统计时段暂不可用 · UTC+8"
    c.text((58, 182), subtitle, 24, MUTED)
    c.text((58, 225), "DeepSeek 大肥鱼  /  TOKEN & COST", 19, BLUE)
    _overview(c, feiyu, x=50, y=286, width=638, primary=primary, token_usage=token_usage)
    _overview(c, codex, x=712, y=286, width=638, codex=True)
    c.text((60, 654), _price_basis(feiyu, codex), 22, MUTED)
    _composition(c, feiyu, 688, token_usage=token_usage)

    c.card((50, feature_y, 1350, feature_y + feature_height))
    title = "大肥鱼 · 功能开销明细"
    if token_usage["source"] == "official":
        title += "（本机已记录部分）"
    elif feiyu["partial"] or feiyu["unknown"]:
        title += "（已记录部分）"
    c.text((80, feature_y + 26), title, 31, bold=True)
    c.right(1320, feature_y + 35,
            "人民币 / 本机 token / 本机占比" if token_usage["source"] == "official" else "人民币 / token / 占比", 22, MUTED)
    if autonomous:
        c.text((80, feature_y + 76), "自主决策包含：聊天、刷帖评论、分享、空间、手账、头像签名", 20, MUTED)
    if not features:
        message = ("暂无功能分类记录。" if feiyu["calls"] else "该时段没有模型调用。") if feiyu["state"] == "available" else _state_message(feiyu)[1]
        c.text((80, feature_y + 111), message, 26, MUTED)
    for index, row in enumerate(features):
        y = feature_y + 94 + (32 if autonomous else 0) + index * 82
        ratio = min(1.0, row["total"] / feiyu["total"]) if feiyu["total"] else 0
        c.text((80, y), row["label"], 25, bold=True)
        amount = "用量未知" if row["unknown"] and not row["known"] else f"{row['total']:,}  ·  {ratio:.1%}"
        if row["cost_cny"] is not None:
            amount = f"{_cny(row['cost_cny'], detail=True)}  ·  {amount}"
        c.right(1318, y, amount, 23)
        c.draw.rounded_rectangle((80, y + 42, 1150, y + 54), radius=6, fill="#EDF1F7")
        if ratio:
            c.draw.rounded_rectangle((80, y + 42, 80 + max(2, int(1070 * ratio)), y + 54), radius=6, fill=BLUE)
        c.right(1318, y + 39, f"{row['calls']:,} 次调用", 19, MUTED)

    _composition(c, codex, codex_y, codex=True)
    if codex["projects"]:
        _project_card(c, codex, project_y)
    banner_y = height - 220
    c.draw.rounded_rectangle((50, banner_y, 1350, height - 58), radius=26, fill="#E0EDFF")
    c.draw.ellipse((965, banner_y + 26, 982, banner_y + 43), fill="#C7DEF9")
    c.draw.ellipse((1003, banner_y + 74, 1034, banner_y + 105), fill="#C5E4EF")
    c.text((80, banner_y + 38), "大肥鱼帮你认真记账～", 31, BLUE, True)
    c.text((80, banner_y + 92), "看完账本，继续找点有趣的事。", 23, MUTED)
    _sticker(c, "rest", (1114, banner_y + 12, 1330, banner_y + 150))
    c.text((60, height - 36), "DEEPSEEK · 大肥鱼的开销手账", 18, BLUE)
    c.right(1340, height - 36, "本机生成 · 无额外模型调用", 18, MUTED)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".usage-", suffix=".png", dir=directory, delete=False) as file:
            temporary = Path(file.name)
        c.image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path
