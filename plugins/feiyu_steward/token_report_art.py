"""Shared, bounded artwork and decorative framing for local token reports.

The report's figures and text are always drawn by its renderer.  Artwork is
optional, chosen deterministically from two explicitly named local PNG files,
and never allowed to alter chart measurements or source data.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from pathlib import Path

ART_VERSION = "feiyu-token-anime-v3.1"
ASSET_DIRECTORY = Path(__file__).parent / "assets" / "token-chart-anime-v3"
APPROVED_ANIME_ASSETS = ("feiyu-ledger-magic.png", "feiyu-cloud-ticket.png")
MAX_ASSET_BYTES = 20_000_000
MAX_ASSET_DIMENSION = 8192
MAX_ASSET_PIXELS = 25_000_000

THEMES = (
    {"name": "海盐星光", "bg": "#edf6ff", "ink": "#324761", "muted": "#6d8199", "accent": "#668ed1", "pale": "#dbe9fb", "frame": "#bccfee", "shadow": "#d9e7f6", "colors": ("#77a4db", "#a391d4", "#6ebdb5")},
    {"name": "樱云汽水", "bg": "#fff1f6", "ink": "#51405b", "muted": "#8c7991", "accent": "#cf78a4", "pale": "#fbe1ee", "frame": "#edc5da", "shadow": "#f6dce9", "colors": ("#dd8db3", "#91a6df", "#78bfba")},
    {"name": "薄荷航海", "bg": "#edf9f6", "ink": "#315158", "muted": "#728e90", "accent": "#55a99f", "pale": "#d9f1e9", "frame": "#b8e0d6", "shadow": "#d7eee8", "colors": ("#67baa9", "#98a6dd", "#e3a98b")},
    {"name": "月夜紫晶", "bg": "#f3f1fc", "ink": "#484364", "muted": "#85829d", "accent": "#9683c9", "pale": "#e9e2f8", "frame": "#d3c7eb", "shadow": "#e6def5", "colors": ("#a18ed4", "#81b1db", "#e1a5bc")},
)
QUOTES = ("把今天的数字，装进海风手账。", "让小星星陪你一起看账单。", "一页一页，认真收藏每份记录。", "肥鱼把记录整理好啦。")


def _contains_link(path):
    """Check the path and every ancestor before resolving or opening it."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            return True
        try:
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return True
    return False


def _load_scene(directory, name):
    """Load only a bounded, single-frame transparent RGBA PNG, or return None."""
    if name not in APPROVED_ANIME_ASSETS:
        return None
    from PIL import Image

    path = Path(directory) / name
    try:
        if _contains_link(path) or not path.is_file():
            return None
        before = path.stat()
        if before.st_size > MAX_ASSET_BYTES or before.st_nlink > 1:
            return None
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return None
            data = stream.read(MAX_ASSET_BYTES + 1)
            ended = os.fstat(stream.fileno())
        after = path.stat()
        if (_contains_link(path) or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                or (ended.st_size, ended.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns)):
            return None
        if not data or len(data) > MAX_ASSET_BYTES:
            return None
        with Image.open(io.BytesIO(data)) as original:
            width, height = original.size
            if (original.format != "PNG" or original.mode != "RGBA"
                    or getattr(original, "n_frames", 1) != 1
                    or not (0 < width <= MAX_ASSET_DIMENSION and 0 < height <= MAX_ASSET_DIMENSION)
                    or width * height > MAX_ASSET_PIXELS):
                return None
            original.load()
            alpha_min, alpha_max = original.getchannel("A").getextrema()
            if alpha_min == 255 or alpha_max == 0:
                return None
            artwork = original.copy()
        return artwork, hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def select_art(seed, asset_dir=None):
    """Pick a theme and valid scene from a stable report-window seed.

    Invalid or missing art is skipped; no art is a supported result.  The
    returned SHA-256 belongs to the bytes actually decoded by Pillow.
    """
    identity = json.dumps(seed, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(THEMES)
    first = int(digest[8:16], 16) % len(APPROVED_ANIME_ASSETS)
    directory = Path(asset_dir) if asset_dir is not None else ASSET_DIRECTORY
    artwork = asset_name = asset_hash = None
    for offset in range(len(APPROVED_ANIME_ASSETS)):
        name = APPROVED_ANIME_ASSETS[(first + offset) % len(APPROVED_ANIME_ASSETS)]
        loaded = _load_scene(directory, name)
        if loaded is not None:
            artwork, asset_hash = loaded
            asset_name = name
            break
    return {"theme": THEMES[index], "index": index, "quote": QUOTES[index],
            "image": artwork, "asset_name": asset_name, "asset_hash": asset_hash}


def _star(draw, x, y, radius, fill):
    points = [(x, y-radius), (x+radius*.25, y-radius*.25), (x+radius, y),
              (x+radius*.25, y+radius*.25), (x, y+radius), (x-radius*.25, y+radius*.25),
              (x-radius, y), (x-radius*.25, y-radius*.25)]
    draw.polygon(points, fill=fill)


def paint_background(image, theme):
    """Draw ocean, bubbles and stars in the outer margins behind report cards."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    width, height = image.size
    draw.ellipse((width-425, -170, width+130, 385), fill=theme["pale"])
    draw.ellipse((-140, height-360, 240, height+20), fill=theme["pale"])
    for y in range(370, max(371, height-120), 275):
        draw.ellipse((13, y, 26, y+13), outline=theme["frame"], width=2)
        draw.ellipse((width-29, y+75, width-14, y+90), outline=theme["frame"], width=2)
        _star(draw, 39, y+66, 9, theme["frame"])
        _star(draw, width-43, y+150, 8, theme["frame"])
    for side in (0, width-78):
        draw.arc((side-20, height-116, side+80, height-32), 190, 340, fill=theme["frame"], width=4)
        draw.arc((side-20, height-98, side+80, height-14), 190, 340, fill=theme["frame"], width=3)


def draw_card(draw, box, theme, radius=26, fill="#ffffff"):
    """A paper card with a subtle shadow and fine colored border."""
    x1, y1, x2, y2 = box
    draw.rounded_rectangle((x1, y1+7, x2, y2+7), radius=radius, fill=theme["shadow"])
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=theme["frame"], width=2)


def draw_title_strip(draw, box, theme):
    """Draw a washi-tape heading strip; caller draws the words over it."""
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=12, fill=theme["pale"], outline=theme["frame"], width=1)
    for index in range(4):
        x = x2-32-index*11
        draw.line((x, y1+7, x-10, y2-7), fill=theme["frame"], width=2)


def paste_scene(image, artwork, box, theme):
    """Paste scene art inside a separate header panel, never over report data."""
    from PIL import Image, ImageDraw

    x1, y1, x2, y2 = (int(value) for value in box)
    if x2 <= x1 or y2 <= y1:
        return False
    draw = ImageDraw.Draw(image)
    draw_card(draw, (x1, y1, x2, y2), theme, radius=24)
    draw.rounded_rectangle((x1+9, y1+9, x2-9, y2-9), radius=18, fill=theme["bg"])
    if artwork is None:
        # A simple ocean postcard keeps the fallback pleasant without requiring
        # external artwork. It contains no words, numbers or data marks.
        cx, cy = (x1+x2)//2, (y1+y2)//2
        size = min(x2-x1, y2-y1)
        rx, ry = int(size*.28), int(size*.17)
        draw.ellipse((cx-rx, cy-ry, cx+rx, cy+ry), fill=theme["pale"], outline=theme["frame"], width=3)
        draw.polygon(((cx+rx-3, cy), (cx+rx+int(size*.14), cy-int(size*.14)),
                      (cx+rx+int(size*.12), cy+int(size*.13))), fill=theme["pale"])
        draw.ellipse((cx-int(size*.13), cy-int(size*.025), cx-int(size*.10), cy+int(size*.005)), fill=theme["accent"])
        draw.arc((cx-int(size*.12), cy, cx+int(size*.08), cy+int(size*.12)), 0, 130, fill=theme["accent"], width=2)
        for dx, dy, radius in ((-0.28, -0.27, 8), (0.24, -0.25, 11), (-0.22, 0.27, 6)):
            _star(draw, cx+int(size*dx), cy+int(size*dy), radius, theme["accent"])
        return False
    picture = artwork.copy()
    bounds = picture.getchannel("A").getbbox()
    if bounds:
        picture = picture.crop(bounds)
    picture.thumbnail((max(1, x2-x1-24), max(1, y2-y1-24)), Image.Resampling.LANCZOS)
    position = (x1+(x2-x1-picture.width)//2, y1+(y2-y1-picture.height)//2)
    image.paste(picture, position, picture)
    _star(draw, x1+19, y1+20, 7, theme["accent"])
    _star(draw, x2-21, y2-21, 7, theme["accent"])
    return True
