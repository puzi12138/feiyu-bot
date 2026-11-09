"""Illustrated charts for validated official website headline totals only."""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .token_report_art import (ART_VERSION, ASSET_DIRECTORY, draw_card,
                               draw_title_strip, paint_background, paste_scene, select_art)

MODE = "official_website_v1"
DESIGN = "feiyu_official_headlines_v2"
ART_ATTRIBUTION = (
    "角色：上善无形  ·  女仆设计：ZipZipPipe  ·  参考整理：Small-tailqwq",
    "AI 辅助二创  ·  CC-BY-NC-SA-4.0",
)
TZ = timezone(timedelta(hours=8))
MAXIMUM = 10 ** 12


def validated_image_summary(summary):
    """Return numeric display fields; supplied labels and prose never render."""
    try:
        if not isinstance(summary, dict) or summary.get("report_mode") != MODE:
            raise ValueError
        period = summary.get("period")
        if period not in {"today", "yesterday", "last7", "day"}:
            raise ValueError
        if type(summary.get("days")) is not int or summary["days"] != (7 if period == "last7" else 1):
            raise ValueError
        stamp = summary.get("observed_at")
        if not isinstance(stamp, str) or len(stamp) > 48:
            raise ValueError
        observed = datetime.fromisoformat(stamp)
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError
        observed = observed.astimezone(TZ)
        counts = {key: summary.get(key) for key in ("total_tokens", "request_count")}
        if not all(type(value) is int and 0 <= value <= MAXIMUM for value in counts.values()):
            raise ValueError
        raw_cost = summary.get("cost_cny")
        if not isinstance(raw_cost, str) or len(raw_cost) > 64 or not re.fullmatch(r"\d+(?:\.\d+)?", raw_cost):
            raise ValueError
        cost = Decimal(raw_cost)
        if not cost.is_finite() or not 0 <= cost <= MAXIMUM:
            raise ValueError
        if not counts["request_count"] and (counts["total_tokens"] or cost):
            raise ValueError
        if period == "last7":
            label = "官网近7天 · 含今天（UTC+8）"
        elif period == "day":
            day = summary.get("period_label")
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError
            label = datetime.strptime(day, "%Y-%m-%d").strftime("%Y年%m月%d日") + "（UTC+8）"
            if datetime.strptime(day, "%Y-%m-%d").date() > observed.date():
                raise ValueError
        else:
            day = observed.date() - timedelta(days=period == "yesterday")
            label = day.strftime("%Y年%m月%d日") + (" · 今天" if period == "today" else " · 昨天") + "（UTC+8）"
        return {"report_mode": MODE, "period": period, "period_label": label,
                "observed_at": observed.isoformat(), "days": summary["days"],
                **counts, "cost_cny": format(cost, "f")}
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        raise ValueError("official_usage_image_invalid") from None


def _compact(value):
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if value >= 10_000:
        return f"{value / 10_000:.2f} 万"
    return f"{value:,.0f}"


def _scale(value):
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    return math.ceil(value / power) * power


def render_official_usage_image(summary, *, output_dir, asset_dir=None):
    """Render exact source totals and labelled arithmetic; never infer a trend."""
    source = validated_image_summary(summary)
    from PIL import Image, ImageDraw, ImageFont

    art = select_art({"period": source["period"], "day": source["observed_at"][:10],
                      "label": source["period_label"]},
                     ASSET_DIRECTORY if asset_dir is None else asset_dir)
    index, theme = art["index"], art["theme"]
    accent, ink, muted = (theme[key] for key in ("accent", "ink", "muted"))
    image = Image.new("RGB", (1600, 1740), theme["bg"])
    paint_background(image, theme)
    draw = ImageDraw.Draw(image)

    def font(size, bold=False):
        names = ("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "simhei.ttf")
        for name in names:
            path = Path("C:/Windows/Fonts") / name
            if path.is_file():
                return ImageFont.truetype(str(path), size)
        raise ValueError("report_font_unavailable")

    def text(x, y, value, size=28, color=None, bold=False, width=None):
        value = str(value)
        selected = font(size, bold)
        while width and draw.textlength(value, font=selected) > width and size > 16:
            size -= 1
            selected = font(size, bold)
        draw.text((x, y), value, font=selected, fill=color or ink)

    def card(box):
        draw_card(draw, box, theme, radius=30)

    draw_title_strip(draw, (70, 46, 475, 91), theme)
    loaded_art = paste_scene(image, art["image"], (1160, 35, 1544, 385), theme)
    text(80, 55, "FEIYU  /  TOKEN JOURNAL", 23, accent)
    text(80, 108, "肥鱼的 Token 手账", 56, bold=True)
    text(84, 205, source["period_label"], 28, muted, width=1020)
    text(84, 264, art["quote"], 30, accent, width=1020)
    stamp = datetime.fromisoformat(source["observed_at"])
    text(84, 326, "DeepSeek 官网 · 更新 " + stamp.strftime("%m-%d %H:%M"), 24, muted)

    cost = Decimal(source["cost_cny"])
    fields = (("Token 总量", _compact(source["total_tokens"]), f"{source['total_tokens']:,} token"),
              ("API 请求", f"{source['request_count']:,}", "官网统计请求次数"),
              ("官网消费金额", f"¥{cost:,.2f}", "人民币 CNY"))
    for column, (label, value, note) in enumerate(fields):
        left = 60 + 500 * column
        card((left, 420, left + 480, 650))
        text(left + 32, 450, label, 27, muted)
        text(left + 32, 498, value, 50, accent if column == 2 else ink, True, 414)
        text(left + 34, 590, note, 23, muted, width=414)

    seven = source["days"] == 7
    metrics = (("Token", float(source["total_tokens"]), "token"),
               ("API 请求", float(source["request_count"]), "次"),
               ("官网消费", float(cost), "元"))
    for row, (label, total, unit) in enumerate(metrics):
        top = 680 + row * 282
        card((60, top, 1540, top + 258))
        text(98, top + 23, label + (" · 总量与日均" if seven else " · 当日总量"), 32, bold=True)
        maximum = _scale(total)
        bar_left, bar_right = 340, 1245
        chart_width = bar_right - bar_left
        items = (("近7天", total), ("日均 ÷7", total / 7)) if seven else (("总量", total),)
        for item, (name, amount) in enumerate(items):
            y = top + 92 + item * 66
            text(100, y - 9, name, 25, muted)
            draw.rounded_rectangle((bar_left, y, bar_right, y + 26), radius=13, fill=theme["pale"])
            length = chart_width * amount / maximum
            if amount > 0:
                draw.rounded_rectangle((bar_left, y, bar_left + max(4, length), y + 26), radius=13,
                                       fill=theme["colors"][row])
            number = (f"{amount:,.2f}" if unit == "元" or (seven and item) else f"{amount:,.0f}")
            text(1270, y - 12, number + " " + unit, 25, width=230)
        if seven:
            text(340, top + 207, "同组共用刻度；日均由官网总量除以7计算。", 21, muted)
        else:
            text(340, top + 162, f"该组刻度 0 — {maximum:,.0f} {unit}", 22, muted)
            if source["request_count"]:
                note = (f"每次请求平均 {source['total_tokens']/source['request_count']:,.2f} token"
                        if row == 0 else f"每次请求平均 ¥{cost/source['request_count']:.6f}" if row == 2
                        else "官网统计范围内的 API 请求次数")
                text(340, top + 202, note, 22, muted)

    scope = "官网账号全部 Key" if summary.get("key_filter") == "all" else "官网账号用量"
    text(82, 1560, scope + " · 数据可能延迟约5分钟", 23, muted, width=1430)
    text(82, 1602, "七天日均为数学平均值，不代表逐日走势。" if seven else "账单范围与更新时刻见页首。", 22, muted, width=1430)
    if loaded_art:
        for row, line in enumerate(ART_ATTRIBUTION):
            text(82, 1660 + row * 34, line, 22, muted, width=1430)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    identity = {"summary": source, "design": DESIGN, "art_version": ART_VERSION, "theme": index,
                "asset": art["asset_name"], "asset_sha256": art["asset_hash"], "scope": scope}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    path = output / f"feiyu-official-token-{digest}.png"
    image.save(path, format="PNG")
    return path
