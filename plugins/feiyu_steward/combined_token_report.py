"""Present two independent local request ledgers over the same interval."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .cockpit_token_report import report_readiness as cockpit_report_readiness
from .local_feature_query import report_ready as feature_report_ready
from .token_report_art import ART_VERSION, select_art, paint_background, draw_title_strip, paste_scene
from .bill_display_policy import PROJECT_USAGE_LABEL, display_bill_text
from .cost_pie_chart import COST_PIE_VERSION

MODE = "local_sources_combined_v1"
SOURCE = "feiyu_and_cockpit_local_ledgers"


def _child_ready(name, result):
    if (not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"]):
        return False
    if name == "feiyu":
        return result.get("ok") is True and feature_report_ready(result)
    return (result.get("ok") is True
            and result.get("source") == "cockpit_request_ledger"
            and cockpit_report_readiness(result.get("summary"))["ready"])


def make_combined_report(feiyu, cockpit):
    """Keep each ledger's totals separate; an unavailable side stays unknown."""
    children = {"feiyu": feiyu, "cockpit": cockpit}
    for result in children.values():
        if not isinstance(result, dict):
            return {"ok": False, "sent": False, "queued": False, "status": "local_report_unavailable",
                    "note": "本机用量记录暂时不可用，请稍后再查。"}
        if result.get("status") in ("unsupported_period", "unsupported_feature"):
            return result
    if any(item.get("ok") is True and not _child_ready(name, item) for name, item in children.items()):
        return {"ok": False, "sent": False, "queued": False, "status": "local_report_unavailable",
                "note": "本机用量记录暂时不可用，请稍后再查。"}
    available = [(name, item) for name, item in children.items() if _child_ready(name, item)]
    if not available:
        return {"ok": False, "sent": False, "queued": False, "status": "local_reports_unavailable",
                "note": "肥鱼和项目用量的本机记录都暂时不可用。"}
    first = available[0][1]["summary"]
    fields = ("period", "observed_at", "from", "until")
    if (any(not isinstance(first.get(key), str) or not first[key] for key in fields)
            or any(any(item["summary"].get(key) != first[key] for key in fields)
                   for _, item in available)):
        return {"ok": False, "sent": False, "queued": False, "status": "local_report_interval_mismatch",
                "note": "两个来源的查询时段不一致，请重新查询。"}
    summary = {key: first[key] for key in fields}
    summary.update(local_records_only=True, feiyu=feiyu, cockpit=cockpit)
    lines = ["Token 消耗汇总", f"时段：{first['from']} 至 {first['until']}（北京时间，结束不含）",
             "用途中的肥鱼相关是项目分类。", ""]
    for name, title in (("feiyu", "肥鱼 · 功能分项"), ("cockpit", f"{PROJECT_USAGE_LABEL} · 用途分项")):
        result = children[name]
        lines.append("【" + title + "】")
        lines.append(display_bill_text(result["text"] if _child_ready(name, result) else result.get("note", "本机记录暂时不可用。")))
        lines.append("")
    return {"ok": True, "sent": False, "queued": False, "report_mode": MODE, "source": SOURCE,
            "summary": summary, "text": "\n".join(lines).strip(), "timezone": "Asia/Shanghai"}


def report_ready(result):
    if not isinstance(result, dict) or result.get("report_mode") != MODE or result.get("source") != SOURCE:
        return False
    summary = result.get("summary")
    if (result.get("ok") is not True or not isinstance(summary, dict)
            or summary.get("local_records_only") is not True
            or not isinstance(result.get("text"), str) or not result["text"]):
        return False
    fields = ("period", "observed_at", "from", "until")
    if any(not isinstance(summary.get(key), str) or not summary[key] for key in fields):
        return False
    ready = 0
    for name in ("feiyu", "cockpit"):
        child = summary.get(name)
        if not isinstance(child, dict):
            return False
        if _child_ready(name, child):
            ready += 1
            if any(child["summary"].get(key) != summary[key] for key in fields):
                return False
        elif child.get("ok") is not False or not isinstance(child.get("note"), str):
            return False
    return bool(ready)


def render_combined_usage_image(result, *, output_dir):
    """Stack the source charts, retaining the independent totals and caveats."""
    if not report_ready(result):
        raise ValueError("combined_usage_report_unavailable")
    from PIL import Image, ImageDraw
    from .feature_token_image import _font, render_feature_usage_image
    from .cockpit_token_report import render_cockpit_usage_image

    directory = Path(output_dir)
    if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
        raise OSError("linked_report_directory")
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    summary = result["summary"]
    artwork = select_art({"from": summary["from"], "until": summary["until"]})
    theme = artwork["theme"]
    width, gap, header = 1500, 24, 245
    sections = []
    for name, title in (("feiyu", "肥鱼 · 功能分项"), ("cockpit", PROJECT_USAGE_LABEL + " · 用途分项")):
        child = summary[name]
        if _child_ready(name, child):
            render = render_feature_usage_image if name == "feiyu" else render_cockpit_usage_image
            path = Path(render(child["summary"], output_dir=directory)).resolve()
            if path.parent != directory or path.suffix.lower() != ".png" or not path.is_file():
                raise OSError("invalid_source_report_image")
            with Image.open(path) as original:
                if original.width < 1 or original.height < 1 or max(original.size) > 12000:
                    raise ValueError("invalid_source_report_dimensions")
                section = original.convert("RGB")
            if section.width != width:
                height = round(section.height * width / section.width)
                section = section.resize((width, height), Image.Resampling.LANCZOS)
        else:
            section = Image.new("RGB", (width, 210), "#fffaf8")
            draw = ImageDraw.Draw(section)
            draw.text((70, 36), title, font=_font(38, True), fill="#283c61")
            draw.text((70, 105), display_bill_text(child.get("note", "本机记录暂时不可用。"))[:70],
                      font=_font(25), fill="#687c9b")
        sections.append((title, section))
    height = header + sum(section.height + 70 for _, section in sections) + gap * (len(sections) + 1)
    image = Image.new("RGB", (width, height), theme["bg"])
    paint_background(image, theme)
    draw = ImageDraw.Draw(image)
    paste_scene(image, artwork["image"], (1235, 18, 1460, 235), theme)
    draw_title_strip(draw, (60, 25, 660, 93), theme)
    draw.text((78, 35), "肥鱼的星海账单", font=_font(42, True), fill=theme["ink"])
    draw.text((70, 105), "用途中的肥鱼相关是项目分类。", font=_font(24), fill="#687c9b")
    draw.text((70, 150), summary["from"].replace("T", " ") + "  至  " + summary["until"].replace("T", " "),
              font=_font(23), fill="#687c9b")
    draw.text((70, 190), "北京时间，结束不含；未回传的用量单独标示。", font=_font(22), fill="#687c9b")
    y = header + gap
    for title, section in sections:
        draw_title_strip(draw, (60, y, 700, y + 54), theme)
        draw.text((78, y + 8), title, font=_font(30, True), fill=theme["ink"])
        y += 70
        image.paste(section, (0, y))
        y += section.height + gap
    identity = {"summary": summary, "design": ART_VERSION, "cost_pie": COST_PIE_VERSION,
                "asset_sha256": artwork["asset_hash"]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:24]
    path = directory / f"combined-token-{digest}.png"
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise OSError("linked_report_image")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".combined-token-", suffix=".png", dir=directory, delete=False) as file:
            temporary = Path(file.name)
        image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path
