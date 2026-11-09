"""Illustrated chart of retained Feiyu feature records, with no live calls."""
import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal
from datetime import datetime
from pathlib import Path

from .token_usage_store import FEATURE_LABELS
from .cost_pie_chart import (COST_PIE_VERSION, cost_card_height, draw_cost_card,
                             make_cost_distribution)
from .feature_reconciliation import reconciliation_text, validate_reconciliation
from .token_report_art import (ART_VERSION, ASSET_DIRECTORY, draw_card,
                               draw_title_strip, paint_background, paste_scene, select_art)

WIDTH = 1500
ASSET_DIR = ASSET_DIRECTORY


def _font(size, bold=False):
    from PIL import ImageFont
    for name in (("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "simhei.ttf")):
        path = Path("C:/Windows/Fonts") / name
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise RuntimeError("report_font_unavailable")


def _price(value):
    return f"¥{value:,.6f}"


def _validate(summary):
    if not isinstance(summary, dict) or summary.get("local_records_only") is not True or summary.get("coverage_complete") is not False:
        raise ValueError("invalid_local_feature_summary")
    rows, totals, pricing = summary.get("by_feature"), summary.get("totals"), summary.get("pricing")
    if not isinstance(rows, list) or len(rows) > len(FEATURE_LABELS) or not isinstance(totals, dict) or not isinstance(pricing, dict):
        raise ValueError("invalid_local_feature_summary")
    fields = ("calls", "known_usage_calls", "unknown_usage_calls", "total")
    if any(type(totals.get(key)) is not int or totals[key] < 0 for key in fields):
        raise ValueError("invalid_local_feature_totals")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("feature") not in FEATURE_LABELS or row["feature"] in seen:
            raise ValueError("invalid_local_feature_row")
        seen.add(row["feature"])
        if row.get("label") != FEATURE_LABELS[row["feature"]] or any(type(row.get(key)) is not int or row[key] < 0 for key in fields):
            raise ValueError("invalid_local_feature_row")
        if not row["calls"] or row["known_usage_calls"] + row["unknown_usage_calls"] != row["calls"]:
            raise ValueError("invalid_local_feature_row")
        amount = row.get("pricing")
        if (not isinstance(amount, dict) or type(amount.get("known_cny")) not in (int, float)
                or not math.isfinite(amount["known_cny"]) or amount["known_cny"] < 0
                or type(amount.get("priced_calls")) is not int or type(amount.get("unpriced_calls")) is not int
                or amount["priced_calls"] < 0 or amount["unpriced_calls"] < 0
                or amount["priced_calls"] + amount["unpriced_calls"] != row["calls"]):
            raise ValueError("invalid_local_feature_price")
    if any(sum(row[key] for row in rows) != totals[key] for key in fields):
        raise ValueError("invalid_local_feature_totals")
    if (type(pricing.get("known_cny")) not in (int, float) or not math.isfinite(pricing["known_cny"])
            or pricing["known_cny"] < 0 or type(pricing.get("priced_calls")) is not int
            or type(pricing.get("unpriced_calls")) is not int or pricing["priced_calls"] < 0
            or pricing["unpriced_calls"] < 0 or pricing["priced_calls"] + pricing["unpriced_calls"] != totals["calls"]
            or sum(row["pricing"]["priced_calls"] for row in rows) != pricing["priced_calls"]
            or sum(Decimal(str(row["pricing"]["known_cny"])) for row in rows) != Decimal(str(pricing["known_cny"]))):
        raise ValueError("invalid_local_feature_price")
    if not all(isinstance(summary.get(k), str) for k in ("from", "until")):
        raise ValueError("invalid_local_feature_interval")
    validate_reconciliation(summary)


def render_feature_usage_image(summary, *, output_dir, asset_dir=None):
    """Return an absolute PNG path using only the already aggregated report."""
    from PIL import Image, ImageDraw

    _validate(summary)
    rows = sorted(summary["by_feature"], key=lambda row: (-row["total"], row["feature"]))
    totals, pricing = summary["totals"], summary["pricing"]
    reconciliation = summary.get("reconciliation")
    has_gap = (isinstance(reconciliation, dict)
               and reconciliation.get("state") in ("available", "observed_difference"))
    gap_tokens = reconciliation["token_difference"] if has_gap else None
    notes = reconciliation_text(reconciliation)
    if has_gap:
        # The first two lines are represented by the chart row below.
        notes = notes[2:]
    has_composite = any(row["feature"] == "autonomous_plan" for row in rows)
    row_height, chart_top = 112, 710
    card_bottom = chart_top + (max(1, len(rows)) + int(has_gap)) * row_height + 64
    height = card_bottom + (80 if has_composite else 0) + 36 * len(notes) + 112
    art = select_art({"from": summary["from"], "until": summary["until"]},
                     ASSET_DIR if asset_dir is None else asset_dir)
    theme = art["theme"]
    image = Image.new("RGB", (WIDTH, height), theme["bg"])
    paint_background(image, theme)
    draw = ImageDraw.Draw(image)
    ink, muted, accent = (theme[key] for key in ("ink", "muted", "accent"))
    font_cache = {}

    def face(size, heavy=False):
        key = (size, heavy)
        if key not in font_cache:
            font_cache[key] = _font(size, heavy)
        return font_cache[key]

    def text(x, y, value, size=26, color=ink, heavy=False, width=None, right=False):
        value = str(value)
        while width and face(size, heavy).getlength(value) > width and size > 16:
            size -= 1
        if right:
            x -= face(size, heavy).getlength(value)
        draw.text((int(x), y), value, fill=color, font=face(size, heavy))

    def card(box):
        draw_card(draw, box, theme)

    draw_title_strip(draw, (64, 46, 470, 91), theme)
    paste_scene(image, art["image"], (1096, 42, 1436, 339), theme)
    text(72, 55, "FEIYU  /  TOKEN JOURNAL", 22, accent)
    text(70, 108, "肥鱼功能用量", 56, ink, True, width=930)
    text(74, 198, "本机已记录用量", 27, muted)
    text(74, 252, summary["from"].replace("T", " ") + "  至", 23, muted, width=980)
    text(74, 288, summary["until"].replace("T", " ") + "（北京时间，结束不含）", 23, muted, width=980)
    try:
        if datetime.fromisoformat(summary["observed_since"]) > datetime.fromisoformat(summary["from"]):
            text(74, 324, "记录起点：" + summary["observed_since"].replace("T", " "), 21, muted, width=980)
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid_recording_start")

    for box in ((64, 365, 506, 594), (529, 365, 971, 594), (994, 365, 1436, 594)):
        card(box)
    text(92, 395, "已知 Token", 25, muted)
    no_usage = totals["calls"] > 0 and totals["known_usage_calls"] == 0
    text(92, 451, "待回传" if no_usage else f"{totals['total']:,}", 46, ink, True, width=380)
    text(94, 543, f"已回传 {totals['known_usage_calls']:,} 次", 24, muted, width=380)
    text(557, 395, "已记录调用", 25, muted)
    text(557, 451, f"{totals['calls']:,}", 46, ink, True, width=380)
    text(559, 543, f"未回传用量 {totals['unknown_usage_calls']:,} 次", 24, muted, width=380)
    text(1022, 395, "按官方单价", 25, muted)
    amount = _price(pricing["known_cny"]) if pricing["priced_calls"] else "—"
    text(1022, 451, amount, 42, accent, True, width=380)
    text(1024, 543, f"官方单价估算 {pricing['priced_calls']:,} / {totals['calls']:,} 次", 23, muted, width=380)

    card((64, 628, 1436, card_bottom))
    text(100, 652, "功能明细与对账诊断" if reconciliation else "功能明细", 34, ink, True)
    text(1400, 663, "金额 / Token（差额为诊断值）" if has_gap else "金额 / 已知 Token",
         23, muted, width=520, right=True)
    largest = max([row["total"] for row in rows] + ([abs(gap_tokens)] if has_gap else [0]))
    # On a negative difference, a shared central zero keeps left and right
    # distances on the same token scale. Other reports keep the full-width scale.
    negative_axis = has_gap and gap_tokens < 0
    origin, span = (750, 645) if negative_axis else (105, 1290)

    def bar(y, value, color):
        draw.rounded_rectangle((105, y, 1395, y + 8), radius=4, fill="#e9effa")
        if negative_axis:
            draw.line((origin, y - 5, origin, y + 13), fill="#9bacbf", width=2)
        if largest and value:
            length = max(1, round(span * abs(value) / largest))
            # Pillow rectangles include both end pixels.
            left, right = (origin - length, origin - 1) if value < 0 else (origin, origin + length - 1)
            draw.rounded_rectangle((left, y, right, y + 8), radius=4, fill=color)
    if not rows:
        text(100, chart_top + 22, "这个时段没有符合条件的本机记录。", 27, muted)
    for i, row in enumerate(rows):
        y = chart_top + i * row_height
        amount = row["pricing"]
        text(104, y, row["label"], 28, ink, True, width=580)
        price = _price(amount["known_cny"]) if amount["priced_calls"] else "—"
        tokens = "待回传" if row["calls"] > 0 and row["known_usage_calls"] == 0 else f"{row['total']:,}"
        text(1396, y + 1, f"{price}  ·  {tokens}", 26, ink, width=700, right=True)
        text(106, y + 39, f"已回传 {row['known_usage_calls']:,} 次  ·  未回传 {row['unknown_usage_calls']:,} 次", 21, muted, width=650)
        text(1396, y + 40, f"计价 {amount['priced_calls']:,} / {row['calls']:,} 次", 21, muted, width=360, right=True)
        bar(y + 81, row["total"], accent)
    if has_gap:
        y = chart_top + max(1, len(rows)) * row_height
        gap_color = "#df884b"
        label = ("官网与本机观测差额（未归因，截止未对齐）" if reconciliation["state"] == "observed_difference"
                 else "官网与本机差额（未归因）")
        text(104, y, label, 28, ink, True, width=690)
        text(1396, y + 1, f"—  ·  {gap_tokens:+,}", 26, gap_color, True, width=650, right=True)
        text(106, y + 39, reconciliation_text(reconciliation)[1], 21, muted, width=1000)
        if negative_axis:
            text(1396, y + 40, "← 负差（本机快照数较大）", 21, gap_color, right=True)
        bar(y + 81, gap_tokens, gap_color)
    bottom = card_bottom
    if has_composite:
        text(92, bottom + 20, "自主决策为综合任务，不能拆到各业务。", 23, muted, width=1200)
        bottom += 80
    for note in notes:
        text(92, bottom + 15, note, 21, muted, width=1300)
        bottom += 36
    text(80, bottom + 18, "按调用时段官方单价计算", 22, muted)
    text(80, bottom + 51, "角色：上善无形 · 女仆设计：ZipZipPipe · 参考整理：Small-tailqwq · AI 辅助二创 · CC-BY-NC-SA-4.0", 16, muted, width=1340)

    cost = make_cost_distribution(
        [{"key": row["feature"], "label": row["label"], "calls": row["calls"],
          "priced_calls": row["pricing"]["priced_calls"],
          "unpriced_calls": row["pricing"]["unpriced_calls"],
          "amount": row["pricing"]["known_cny"]} for row in rows],
        priced_calls=pricing["priced_calls"], unpriced_calls=pricing["unpriced_calls"],
        expected_amount=pricing["known_cny"])
    cost_top = 628
    cost_bottom = cost_top + cost_card_height(cost)
    offset = cost_bottom - cost_top + 30
    expanded = Image.new("RGB", (WIDTH, height + offset), theme["bg"])
    paint_background(expanded, theme)
    expanded.paste(image.crop((0, 0, WIDTH, cost_top)), (0, 0))
    expanded.paste(image.crop((0, cost_top, WIDTH, height)), (0, cost_top + offset))
    draw_cost_card(ImageDraw.Draw(expanded), (64, cost_top, 1436, cost_bottom), cost, theme, face,
                   title="金额分布 · 按功能")
    image = expanded
    directory = Path(output_dir)
    if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
        raise OSError("linked_report_directory")
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    identity = {"summary": summary, "design": ART_VERSION, "cost_pie": COST_PIE_VERSION, "theme": art["index"],
                "asset": art["asset_name"], "asset_sha256": art["asset_hash"]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:24]
    path = directory / f"feiyu-features-{digest}.png"
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise OSError("linked_report_image")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".feiyu-features-", suffix=".png", dir=directory, delete=False) as file:
            temporary = Path(file.name)
        image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path
