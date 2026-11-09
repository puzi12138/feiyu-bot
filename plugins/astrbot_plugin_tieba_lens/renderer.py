"""Bounded, source-faithful Tieba cards rendered through AstrBot's HTML service.

Only public Baidu CDN images are fetched. Long images are captured from one HTML
document first, and only actual screenshot limits trigger a continuation image.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import ipaddress
import math
import re
import socket
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .bundled_fonts import BUNDLED_FONT_FILENAME, BundledFontCache
from .fonts import FontStore

_CDN_HOSTS = {
    "tiebapic.baidu.com", "imgsa.baidu.com", "imgsrc.baidu.com",
    "tb.himg.baidu.com", "himg.baidu.com", "himg.bdimg.com", "hiphotos.baidu.com",
    "tb1.bdstatic.com", "tb2.bdstatic.com", "tb3.bdstatic.com",
    "tb4.bdstatic.com", "tiebac.baidu.com", "gss0.baidu.com",
    "gss3.baidu.com", "gss1.bdstatic.com", "gss2.bdstatic.com",
    "ss0.bdstatic.com", "ss1.bdstatic.com", "ss2.bdstatic.com",
    "ss3.bdstatic.com", "b.bdstatic.com", "emoji.cdn.bcebos.com",
}
_MAX_ASSET_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_PIXELS = 24_000_000
_MAX_ASSETS = 128
_ASSET_SECONDS = 25
_SHANGHAI = timezone(timedelta(hours=8))


class _CaptureSizeLimit(RuntimeError):
    """A confirmed screenshot size limit, safe to retry with fewer blocks."""


def _is_capture_size_error(error: Exception) -> bool:
    if isinstance(error, (_CaptureSizeLimit, Image.DecompressionBombError)):
        return True
    # Do not turn service failures (timeouts, authentication, transport, etc.)
    # into apparently successful but incomplete exports. Chromium's generic
    # "Unable to capture screenshot" is intentionally not a size diagnosis.
    message = str(error).lower()
    explicit = (
        "cannot take screenshot larger than", "screenshot is too large",
        "screenshot too large", "image is too large", "image too large",
        "bitmap size exceeds", "image dimensions exceed",
        "截图尺寸超限", "截图过大", "图片过大", "像素超过上限",
    )
    if any(part in message for part in explicit):
        return True
    return bool(re.search(
        r"(?:screenshot|image|bitmap)\s+(?:size|dimensions?|height|width|pixel count)"
        r"[^\n]{0,80}(?:exceeds?|over|larger than|maximum|too large|limit)",
        message,
    ))

# Every theme uses the bundled WenKai font unless a custom font is selected.
_THEME_FONTS = {
    "paper": "lxgw",
    "midnight": "lxgw",
    "mist": "lxgw",
    "denia": "lxgw",
    "miku": "lxgw",
    "bilibili": "lxgw",
}

# 完整离线字体源；HTML 按本次卡片全部文字生成临时子集。
_FONT_FILES = {
    "lxgw": BUNDLED_FONT_FILENAME,
}
_FONT_FAMILIES = {
    "lxgw": "LXGW WenKai Screen",
    "custom": "Tieba Custom Font",
}

_THEMES = {
    "paper": {
        "bg": "#fbf7ee", "ink": "#2b2118", "muted": "#7a6a58", "accent": "#a44a2e",
        "soft": "#f3e3cd", "line": "#e5cdb0", "chip": "#eed9b8", "chip_ink": "#7a4520",
        "highlight": "#f9e5c8", "brand": "#b5593f",
        "font": "lxgw",
    },
    "midnight": {
        "bg": "#0e1524", "ink": "#e7edf7", "muted": "#8b99b3", "accent": "#7fa8e8",
        "soft": "#1a2338", "line": "#2a3752", "chip": "#1f2c46", "chip_ink": "#a3b8d8",
        "highlight": "#1e2c48", "brand": "#5a8bd8",
        "font": "lxgw",
    },
    "mist": {
        "bg": "#f2f7f4", "ink": "#243636", "muted": "#5b706d", "accent": "#3f7a70",
        "soft": "#d5e5df", "line": "#bcd2cb", "chip": "#c5dbd4", "chip_ink": "#2f5f56",
        "highlight": "#cfe3da", "brand": "#3f867d",
        "font": "lxgw",
    },
    "denia": {
        "bg": "#fbf4f5", "ink": "#3a1f2c", "muted": "#8a5a6a", "accent": "#a33d68",
        "soft": "#ecd2cf", "line": "#dab5bd", "chip": "#e5c3cc", "chip_ink": "#7c2d52",
        "highlight": "#eecfd6", "brand": "#b8557a",
        "font": "lxgw",
    },
    "miku": {
        "bg": "#f0f8f6", "ink": "#0f2e2e", "muted": "#3f6866", "accent": "#0ea89b",
        "soft": "#c3e8e0", "line": "#a3d5cb", "chip": "#b2e0d6", "chip_ink": "#0a6e64",
        "highlight": "#bae6dc", "brand": "#0eb5a8",
        "font": "lxgw",
    },
    "bilibili": {
        "bg": "#fff6f9", "ink": "#331220", "muted": "#8a5468", "accent": "#f4407e",
        "soft": "#ffd0e0", "line": "#f5b3cb", "chip": "#ffc0d8", "chip_ink": "#b32558",
        "highlight": "#ffc9dd", "brand": "#fb5a8e",
        "font": "lxgw",
    },
}


def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def safe_asset_url(value: Any) -> str | None:
    """Strict host allowlist; content cannot point the renderer at arbitrary URLs."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return None
        host = parsed.hostname.lower().rstrip(".")
        if host not in _CDN_HOSTS:
            return None
        # Normalize http to TLS and reject alternate authorities / encoded hosts.
        if parsed.netloc.lower() not in {host, f"{host}:80", f"{host}:443"}:
            return None
        if host == "tb.himg.baidu.com" and parsed.path.startswith("/sys/portrait/item/"):
            host = "himg.bdimg.com"
        return f"https://{host}{parsed.path or '/'}" + (f"?{parsed.query}" if parsed.query else "")
    except (ValueError, UnicodeError):
        return None


def _text_parts(value: Any, columns: int, max_lines: int = 24) -> list[str]:
    """Split without losing even one character; use a conservative CJK measure."""
    text = str(value or "")
    if not text:
        return []
    parts: list[str] = []
    start = 0
    line_units = 0.0
    lines = 1
    for i, character in enumerate(text):
        if character == "\n":
            lines += 1
            line_units = 0
        elif not unicodedata.combining(character):
            line_units += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1.15
            if line_units > columns * 2:
                lines += 1
                line_units = 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1.15
        if lines > max_lines:
            parts.append(text[start:i + 1])
            start = i + 1
            line_units = 0
            lines = 1
    if start < len(text):
        parts.append(text[start:])
    return parts


def _line_count(text: str, columns: int) -> int:
    return sum(max(1, math.ceil(sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1.15
                                    for c in line) / (columns * 2)))
               for line in text.split("\n"))


def _display_time(value: Any) -> str:
    """Presentation-only timezone conversion; normalized source data stays ISO."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_SHANGHAI)
        return parsed.astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OverflowError):
        return str(value)


class CardRenderer:
    def __init__(self, render_html: Callable[[str, dict, dict], Awaitable[str]], data_root: Path):
        self.render_html = render_html
        self.data_root = Path(data_root)
        self.template = (Path(__file__).parent / "templates" / "card.html").read_text(encoding="utf-8")
        self.fonts = FontStore(self.data_root)
        self._bundled_font = BundledFontCache(self.data_root)
        self._theme_art_assets = self._load_theme_art_assets()

    @staticmethod
    def _load_theme_art_assets() -> dict[str, str]:
        """Embed only the two bundled, fixed-path PNGs; never source URLs."""
        art_dir = Path(__file__).parent / "templates" / "art"
        result: dict[str, str] = {}
        for theme in ("miku", "denia"):
            try:
                body = (art_dir / f"{theme}.png").read_bytes()
                with Image.open(io.BytesIO(body)) as asset:
                    if asset.format != "PNG" or asset.width * asset.height > _MAX_PIXELS:
                        continue
                    asset.verify()
                result[theme] = "data:image/png;base64," + base64.b64encode(body).decode("ascii")
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
        return result

    async def render(self, document: dict, options: dict, job_id: str) -> list[str]:
        if not isinstance(job_id, str) or not job_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in job_id):
            raise ValueError("无效的渲染任务编号")
        opts = dict(options or {})
        opts.setdefault("show_source", True)
        width = _integer(opts.get("image_width"), 960, 640, 1200)
        page_height = _integer(opts.get("page_height"), 3000, 1600, 5000)
        max_pages = _integer(opts.get("max_pages"), 12, 1, 30)
        theme = str(opts.get("theme", "denia"))
        theme = {"daniya": "denia", "danyia": "denia"}.get(theme, theme)
        if theme not in _THEMES:
            theme = "denia"
        opts["theme"] = theme
        # 决定本次渲染使用的字体（auto → 主题默认；否则覆盖）
        font_override = str(opts.get("font_override", "lxgw"))
        font_key = "custom" if font_override == "custom" else "lxgw"
        font_data = None
        font_format = "woff2"
        custom = await asyncio.to_thread(self.fonts.load) if font_key == "custom" else None
        if font_key == "custom" and not custom:
            font_key = "lxgw"
        font_face_src = ""
        fallback_font_face_src = ""
        if custom:
            metadata, font_data = custom
            font_format = metadata["format"]
            font_face_src = "data:font/otf;base64," + base64.b64encode(font_data).decode("ascii")
        font_family = _FONT_FAMILIES.get(font_key, "")
        root = self.data_root / "artifacts" / job_id
        root.mkdir(parents=True, exist_ok=True)
        doc = copy.deepcopy(document)
        if font_override == "custom" and not custom:
            doc.setdefault("warnings", []).append("尚未上传自定义字体，已回退使用霞鹜文楷。")
        warnings = list(doc.get("warnings") or [])
        assets, asset_warnings = await self._load_assets(doc, opts, width)
        warnings.extend(asset_warnings)
        blocks = self._build_blocks(doc, opts, width, assets)
        single_image = bool(opts.get("single_image", True))
        captured: list[Path] = []
        limited = False

        def make_context(chunk: list[dict], index: int, total: int) -> dict:
            return {
                "document": doc, "blocks": chunk, "sections": self._as_sections(chunk), "theme": theme, "colors": _THEMES[theme],
                "width": width, "chunk_index": index + 1, "chunk_total": total,
                "warnings": warnings if opts.get("show_source") and index == 0 else [], "options": opts,
                "avatar": assets.get(str(doc.get("avatar") or doc.get("author", {}).get("avatar") or "")),
                "kind_name": {"thread": "帖子存档", "forum": "吧内速览", "user": "用户主页"}.get(doc.get("kind"), "贴吧内容"),
                "has_more": bool(doc.get("pagination", {}).get("has_more")),
                "fetched_at_display": _display_time(doc.get("fetched_at")),
                "font_face_src": font_face_src,
                "font_family": font_family,
                "font_key": font_key,
                "font_format": font_format,
                "fallback_font_face_src": fallback_font_face_src,
                "theme_art_src": self._theme_art_assets.get(theme, ""),
            }

        # One complete character set covers the whole job, including template
        # labels, all pages, generated placeholders, names, and diagnostics.
        # Keep font parsing/compression and its bounded LRU off the event loop.
        subset = await asyncio.to_thread(self._bundled_font.subset_for_context,
                                        self.template, make_context(blocks, 0, 1))
        default_font_src = "data:font/woff2;base64," + base64.b64encode(subset).decode("ascii")
        if custom:
            fallback_font_face_src = default_font_src
        else:
            font_face_src = default_font_src

        async def capture_chunk(chunk: list[dict], index: int, total: int) -> Image.Image:
            rendered_path = await self.render_html(self.template, make_context(chunk, index, total), {
                "viewport_width": width, "device_scale_factor_level": "normal", "full_page": True,
                "type": "png", "timeout": 60000, "animations": "disabled", "scale": "css",
            })
            path = Path(str(rendered_path))
            if not path.is_file():
                raise RuntimeError("HTML 渲染服务未返回可读取的本地图片")
            try:
                with Image.open(path) as source:
                    target_height = max(1, round(source.height * width / source.width))
                    if source.width * source.height > _MAX_PIXELS or width * target_height > _MAX_PIXELS:
                        raise _CaptureSizeLimit("渲染截图实际像素超过安全上限")
                    image = source.convert("RGB")
                    if image.width != width:
                        try:
                            resized = image.resize((width, target_height), Image.Resampling.LANCZOS)
                        finally:
                            image.close()
                        image = resized
                    return image
            finally:
                if path.resolve().parent == (self.data_root / "render_tmp").resolve():
                    path.unlink(missing_ok=True)

        async def capture_remaining(remaining: list[dict], index: int) -> tuple[int, Image.Image]:
            # Always try the complete remaining HTML before splitting. Font
            # metrics and CSS, not the old block height estimates, decide fit.
            try:
                return len(remaining), await capture_chunk(remaining, index, 1)
            except Exception as error:
                if not _is_capture_size_error(error):
                    raise
                size_error = error
            low, high = 1, len(remaining) - 1
            best: Image.Image | None = None
            best_count = 0
            try:
                while low <= high:
                    count = (low + high) // 2
                    try:
                        candidate = await capture_chunk(remaining[:count], index, 1)
                    except Exception as error:
                        if not _is_capture_size_error(error):
                            raise
                        size_error = error
                        high = count - 1
                    else:
                        if best is not None:
                            best.close()
                        best, best_count = candidate, count
                        low = count + 1
                if best is None:
                    raise RuntimeError("单个内容块仍超过渲染安全限额，无法完整导出；请查看原帖。") from size_error
                return best_count, best
            except BaseException:
                if best is not None:
                    best.close()
                raise

        if single_image:
            remaining = list(blocks)
            chunk_index = 0
            while remaining and len(captured) < max_pages:
                count, image = await capture_remaining(remaining, chunk_index)
                raw_path = root / f".capture-{len(captured) + 1:03d}.png"
                try:
                    image.save(raw_path, "PNG")
                finally:
                    image.close()
                captured.append(raw_path)
                remaining = remaining[count:]
                chunk_index += 1
            if remaining:
                limited = True
        else:
            chunks = self._paginate(blocks, page_height)
            # At most one extra bounded chunk is captured to discover a page limit.
            for index, chunk in enumerate(chunks):
                if len(captured) >= max_pages:
                    limited = True
                    break
                image = await capture_chunk(chunk, index, len(chunks))
                try:
                    parts = self._crop_pages(image, page_height - 112, _THEMES[theme]["bg"])
                finally:
                    image.close()
                try:
                    for part in parts:
                        if len(captured) == max_pages:
                            limited = True
                            continue
                        raw_path = root / f".capture-{len(captured) + 1:03d}.png"
                        part.save(raw_path, "PNG")
                        captured.append(raw_path)
                finally:
                    for part in parts:
                        part.close()
                if limited:
                    break
        if not captured:
            raise RuntimeError("没有可导出的渲染内容")
        if limited:
            message = f"已达到 {max_pages} 页上限，后续内容未收录；请打开原帖，或提高页数上限后重新解析。"
            warnings.append(message)
        # Keep omissions in diagnostics even when source notes are hidden.
        original_warnings = document.setdefault("warnings", [])
        for warning in warnings:
            if warning not in original_warnings:
                original_warnings.append(warning)
        # Pillow does not decode WOFF2. Keep its complete SFNT outside the
        # plugin checkout and reuse it across jobs and plugin restarts.
        font_path = await asyncio.to_thread(self._bundled_font.sfnt_path)
        output: list[str] = []
        for index, raw_path in enumerate(captured):
            with Image.open(raw_path) as page:
                final = self._footer(page, width, _THEMES[theme], index + 1, len(captured),
                                     str(doc.get("url") or "") if opts.get("show_source") and index + 1 == len(captured) else None,
                                     bool(opts.get("show_source")) and limited and index + 1 == len(captured), max_pages,
                                     font_key=font_key, font_data=font_data, font_path=font_path)
            target = root / f"page-{index + 1:03d}.png"
            try:
                final.save(target, "PNG", optimize=True)
            finally:
                final.close()
            raw_path.unlink(missing_ok=True)
            output.append(str(target.resolve()))
        return output

    async def _load_assets(self, document: dict, options: dict, width: int) -> tuple[dict, list[str]]:
        image_limit = _integer(options.get("image_limit"), 40, 0, 100)
        content_urls: dict[str, str] = {}
        avatar_urls: dict[str, str] = {}
        image_count = 0

        def add_avatar(author: Any) -> None:
            if isinstance(author, dict) and author.get("avatar"):
                avatar_urls.setdefault(str(author["avatar"]), "avatar")

        def visit(post: dict) -> None:
            nonlocal image_count
            add_avatar(post.get("author"))
            for seg in post.get("segments") or []:
                kind = seg.get("type")
                if kind not in {"image", "emoji"}:
                    continue
                if kind == "image" and not options.get("include_images", True):
                    continue
                if kind == "emoji" and not options.get("include_emoji", True):
                    continue
                within_limit = kind == "emoji" or image_count < image_limit
                if within_limit and seg.get("url"):
                    content_urls.setdefault(str(seg["url"]), kind)
                    if kind == "image":
                        image_count += 1
            for comment in post.get("comments") or []:
                visit(comment)

        add_avatar(document.get("author"))
        if document.get("avatar"):
            avatar_urls.setdefault(str(document["avatar"]), "avatar")
        for post in document.get("posts") or []:
            visit(post)
        for thread in document.get("threads") or []:
            add_avatar(thread.get("author"))
            if options.get("include_images", True):
                for value in thread.get("images") or []:
                    if image_count < image_limit:
                        content_urls.setdefault(str(value), "image")
                        image_count += 1
        # Body images and inline emoji retain the first claim on both the
        # resource count and the shared download budget. Reply avatars can use
        # what remains, without silently bypassing the global safety limits.
        candidates = dict(content_urls)
        for url, kind in avatar_urls.items():
            candidates.setdefault(url, kind)
        urls = dict(list(candidates.items())[:_MAX_ASSETS])
        if not urls:
            return {}, []
        result: dict[str, dict] = {}
        start = time.monotonic()
        total_bytes = 0
        downloaded = 0
        semaphore = asyncio.Semaphore(4)
        lock = asyncio.Lock()
        normalize_lock = asyncio.Lock()

        async def download(original: str, kind: str) -> None:
            nonlocal total_bytes, downloaded
            current = safe_asset_url(original)
            if current is None:
                return
            async with semaphore:
                if time.monotonic() - start > _ASSET_SECONDS:
                    return
                try:
                    for _ in range(3):
                        # A compromised / locally overridden CDN record cannot target LAN.
                        host = urlsplit(current).hostname
                        addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM), 3)
                        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                            return
                        async with client.stream("GET", current) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                current = safe_asset_url(urljoin(current, response.headers.get("location", "")))
                                if not current:
                                    return
                                continue
                            if response.status_code != 200:
                                return
                            if not response.headers.get("content-type", "").lower().startswith("image/"):
                                return
                            length = response.headers.get("content-length")
                            if length and int(length) > _MAX_ASSET_BYTES:
                                return
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                async with lock:
                                    if total_bytes + len(chunk) > _MAX_TOTAL_BYTES:
                                        return
                                    total_bytes += len(chunk)
                                body.extend(chunk)
                                if len(body) > _MAX_ASSET_BYTES or time.monotonic() - start > _ASSET_SECONDS:
                                    return
                            # Large RGBA decodes can occupy several copies of
                            # the original image. Download in parallel, but keep
                            # only one Pillow normalization active per job.
                            async with normalize_lock:
                                normalized = await asyncio.to_thread(self._normalize_asset, bytes(body), kind, width)
                            if normalized:
                                result[original] = normalized
                                downloaded += 1
                            return
                except (httpx.HTTPError, OSError, ValueError, asyncio.TimeoutError):
                    return

        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), follow_redirects=False,
                                     trust_env=False, headers={"User-Agent": "TiebaLens/1.0", "Accept": "image/*"}) as client:
            async def download_in_priority_order() -> None:
                await asyncio.gather(*(download(url, kind) for url, kind in urls.items() if url in content_urls))
                await asyncio.gather(*(download(url, kind) for url, kind in urls.items() if url not in content_urls))

            try:
                await asyncio.wait_for(download_in_priority_order(), _ASSET_SECONDS + 1)
            except asyncio.TimeoutError:
                pass
        warnings = []
        missing = len(candidates) - downloaded
        if missing:
            warnings.append(f"{missing} 张图片或头像不可用，已保留文字占位（受安全白名单、下载预算或源站限制）。")
        return result, warnings

    @staticmethod
    def _normalize_asset(body: bytes, kind: str, width: int) -> dict | None:
        try:
            with Image.open(io.BytesIO(body)) as opened:
                if opened.width * opened.height > _MAX_PIXELS or opened.width < 1 or opened.height < 1:
                    return None
                opened.seek(0)
                with ImageOps.exif_transpose(opened) as oriented, oriented.convert("RGBA") as image:
                    maximum = (128, 128) if kind == "avatar" else ((80, 80) if kind == "emoji" else (width - 128, 980))
                    image.thumbnail(maximum, Image.Resampling.LANCZOS)
                    with io.BytesIO() as target:
                        image.save(target, "PNG", optimize=True)
                        return {"src": "data:image/png;base64," + base64.b64encode(target.getvalue()).decode("ascii"),
                                "width": image.width, "height": image.height}
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    @staticmethod
    def _build_blocks(document: dict, options: dict, width: int, assets: dict) -> list[dict]:
        blocks: list[dict] = []
        columns = max(12, (width - 72) // 34)
        image_seen = 0
        image_limit = _integer(options.get("image_limit"), 40, 0, 100)

        def segment_blocks(segments: list, meta: dict) -> None:
            nonlocal image_seen
            first = True
            runs: list[dict] = []
            plain = ""
            primary = not meta.get("depth") and meta.get("floor") == 1
            font_size = 36 if primary else (30 if meta.get("depth") else 35)
            text_width = width - 72 - (0 if primary else 112) - (48 if meta.get("depth") else 0)
            text_columns = max(8, text_width // font_size)
            line_height = 58 if primary else (50 if meta.get("depth") else 56)
            overhead = 128 if primary else (25 if meta.get("depth") else 132)

            def flush() -> None:
                nonlocal runs, plain, first
                if not runs:
                    return
                blocks.append({**meta, "type": "richtext", "runs": runs, "text": plain,
                               "header": first, "continuation": False,
                               "height": (overhead if first else 12) + _line_count(plain, text_columns) * line_height})
                first = False
                runs, plain = [], ""

            # Protocols often split one sentence into many text spans (hashtags
            # included). Merge those spans before line budgeting and typography.
            merged: list[dict] = []
            for segment in segments:
                if segment.get("type", "text") == "text" and merged and merged[-1].get("type") == "text":
                    merged[-1]["text"] = str(merged[-1].get("text") or "") + str(segment.get("text") or "")
                else:
                    merged.append(dict(segment))
            for segment in merged:
                kind = segment.get("type", "text")
                if kind == "text":
                    for part in _text_parts(segment.get("text", ""), text_columns, 20):
                        if runs and _line_count(plain + part, text_columns) > 23:
                            flush()
                        runs.append({"type": "text", "text": part})
                        plain += part
                elif kind == "emoji":
                    enabled = options.get("include_emoji", True)
                    url = str(segment.get("url") or "")
                    asset = assets.get(url) if enabled else None
                    label = str(segment.get("text") or f"[{segment.get('alt') or '贴吧表情'}]")
                    if runs and _line_count(plain + label, text_columns) > 23:
                        flush()
                    runs.append({"type": "emoji", "asset": asset, "alt": label, "text": label})
                    plain += label
                elif kind == "image":
                    flush()
                    enabled = options.get("include_images", True)
                    url = str(segment.get("url") or "")
                    asset = assets.get(url) if enabled and image_seen < image_limit else None
                    if enabled:
                        image_seen += 1
                    alt = str(segment.get("alt") or "帖子配图")
                    reason = "图片未启用" if not enabled else ("图片数量达到设置上限" if image_seen > image_limit else "图片暂不可用")
                    blocks.append({**meta, "type": kind, "asset": asset, "alt": alt,
                                   "placeholder": f"{reason} · {alt}", "header": first, "continuation": False,
                                   "height": (overhead if first else 20) + (asset["height"] if asset else 65)})
                    first = False
                else:
                    text = str(segment.get("text") or segment.get("alt") or ("视频内容，请打开原帖查看" if kind == "video" else "原帖链接"))
                    url = str(segment.get("url") or "")
                    visible = text + (f"（{url}）" if url and url != text else "")
                    # Preserve full URLs and texts, including unusually long ones.
                    for part in _text_parts(visible, text_columns, 20):
                        if runs and _line_count(plain + part, text_columns) > 23:
                            flush()
                        runs.append({"type": "link", "text": part})
                        plain += part
            flush()
            if first:
                text = "（此楼没有可解析的正文）"
                blocks.append({**meta, "type": "richtext", "runs": [{"type":"text","text":text}], "text": text,
                               "height": 150, "header": True, "continuation": False})

        post_sequence = 0

        def post_blocks(post: dict, depth: int = 0, root_floor: Any = None) -> None:
            nonlocal post_sequence
            post_sequence += 1
            # A missing source ID is valid for some nested replies. Keep each
            # author distinct while joining the same reply's mixed body blocks.
            post_key = f"post-{post_sequence}"
            author = dict(post.get("author") or {})
            meta = {"author": author, "avatar": assets.get(str(author.get("avatar") or "")),
                    "floor": post.get("floor"), "post_id": str(post.get("id") or ""), "post_key": post_key,
                    "floor_inferred": bool(post.get("floor_inferred")),
                    "time": _display_time(post.get("time")), "likes": post.get("likes") if post.get("likes_available", True) else None,
                    "is_op": bool(post.get("is_op")), "depth": depth, "parent_floor": root_floor}
            segment_blocks(post.get("segments") or [], meta)
            comments = post.get("comments") or []
            for comment in comments:
                post_blocks(comment, min(3, depth + 1), post.get("floor") if depth == 0 else root_floor)
            count = post.get("comment_total")
            if options.get("show_source", True) and isinstance(count, int) and count > len(comments):
                blocks.append({"type": "notice", "text": f"其余 {count - len(comments)} 条回复请查看原帖", "height": 48,
                               "parent_id": str(post.get("id") or ""), "parent_key": post_key})

        if document.get("description"):
            for part in _text_parts(document["description"], columns):
                blocks.append({"type": "description", "text": part, "height": 72 + _line_count(part, columns) * 43})
        for post in document.get("posts") or []:
            post_blocks(post)
        for thread in document.get("threads") or []:
            author = dict(thread.get("author") or {})
            previews = []
            for url in thread.get("images") or []:
                if options.get("include_images", True) and image_seen < image_limit:
                    image_seen += 1
                    previews.append(assets.get(str(url)))
            excerpts = _text_parts(thread.get("excerpt", ""), columns, 16) or [""]
            for part_index, excerpt in enumerate(excerpts):
                portion = dict(thread)
                portion["excerpt"] = excerpt
                portion["time"] = _display_time(thread.get("time"))
                if part_index:
                    portion["title"] = f"{thread.get('title') or '帖子'} · 续"
                shown_images = previews if part_index == len(excerpts) - 1 else []
                blocks.append({"type": "thread", "thread": portion, "author": author,
                               "avatar": assets.get(str(author.get("avatar") or "")), "images": shown_images,
                               "height": 260 + _line_count(excerpt, columns) * 36 + (240 if shown_images else 0)})
        if not blocks:
            blocks.append({"type": "notice", "text": "当前页面没有可读取的公开正文。页面可能需要登录，或源站暂未返回内容。", "height": 160})
        if options.get("show_source", True) and document.get("pagination", {}).get("has_more"):
            pagination = document["pagination"]
            if document.get("kind") == "thread" and pagination.get("total"):
                text = f"已收录 {pagination.get('fetched')} 楼 / 共 {pagination.get('total')} 楼"
            else:
                text = "其余内容请查看原帖" if document.get("kind") == "thread" else "更多内容请查看来源页面"
            blocks.append({"type": "notice", "text": text, "height": 82})
        return blocks

    @staticmethod
    def _as_sections(blocks: list[dict]) -> list[dict]:
        sections: list[dict] = []
        def identity(block):
            return block.get("post_key") or block.get("post_id")

        for block in blocks:
            if block["type"] in {"richtext", "image"}:
                if identity(block) and sections and sections[-1]["kind"] == "post" and identity(sections[-1]["meta"]) == identity(block) and sections[-1]["meta"].get("depth") == block.get("depth"):
                    sections[-1]["blocks"].append(block)
                else:
                    sections.append({"kind": "post", "meta": block, "blocks": [block]})
            else:
                sections.append({"kind": block["type"], "block": block})
        roots: list[dict] = []
        stack: dict[int, dict] = {}
        by_id: dict[str, dict] = {}
        for section in sections:
            if section["kind"] == "post":
                depth = int(section["meta"].get("depth") or 0)
                section["comments"] = []
                section["more"] = []
                if depth and depth - 1 in stack:
                    stack[depth - 1]["comments"].append(section)
                else:
                    roots.append(section)
                stack[depth] = section
                for level in list(stack):
                    if level > depth:
                        del stack[level]
                if identity(section["meta"]):
                    by_id[identity(section["meta"])] = section
            elif section["kind"] == "notice" and (section["block"].get("parent_key") or section["block"].get("parent_id")) in by_id:
                by_id[section["block"].get("parent_key") or section["block"].get("parent_id")]["more"].append(section["block"])
            else:
                roots.append(section)
        return roots

    @staticmethod
    def _paginate(blocks: list[dict], page_height: int) -> list[list[dict]]:
        # Reserve a generous header/footer allowance; actual captures are checked.
        budget = max(700, page_height - 360)
        pages: list[list[dict]] = []
        current: list[dict] = []
        height = 0
        for block in blocks:
            amount = block.get("height", 180) + (14 if block.get("floor") == 1 else 4)
            if current and height + amount > budget:
                pages.append(current)
                current, height = [], 0
                budget = max(700, page_height - 230)
            if not current and block.get("post_id") and not block.get("header", False):
                block = {**block, "continuation": True}
            current.append(block)
            height += amount
        if current:
            pages.append(current)
        return pages

    @staticmethod
    def _crop_pages(image: Image.Image, height: int, background: str) -> list[Image.Image]:
        if image.height <= height:
            return [image.copy()]
        result = []
        top = 0
        # Low-resolution row scan finds real whitespace between Chinese text lines.
        resized = image.resize((120, image.height))
        try:
            scan = resized.convert("RGB")
        finally:
            resized.close()
        background_rgb = tuple(int(background[i:i + 2], 16) for i in (1, 3, 5))
        try:
            while top < image.height:
                end = min(top + height, image.height)
                if end < image.height:
                    lower = max(top + height // 2, end - 180)
                    candidates = []
                    pixels = scan.load()
                    for y in range(lower, end):
                        ink = sum(1 for x in range(5, 115) if sum(abs(pixels[x, y][channel] - background_rgb[channel]) for channel in range(3)) > 65)
                        candidates.append((ink, abs(end - 18 - y), y))
                    # Whitespace first, closeness second. If an image occupies every
                    # row, contiguous crops preserve it without dropping pixels.
                    empty = [item for item in candidates if item[0] <= 2]
                    if empty:
                        end = min(empty, key=lambda item: item[1])[2]
                if end <= top:
                    end = min(top + height, image.height)
                result.append(image.crop((0, top, image.width, end)))
                top = end
        except BaseException:
            for part in result:
                part.close()
            raise
        finally:
            scan.close()
        return result

    @staticmethod
    def stitch_pages(page_paths: list[str], width: int, background: str, output_path: Path) -> str:
        """Vertically stitch rendered page images into one long image.

        The input pages should have identical width (they all come from the same
        renderer with the same width setting). Any page whose width differs will
        be resized proportionally. Returns the absolute output path.
        """
        if not page_paths:
            raise ValueError("没有可拼接的页面")
        images: list[Image.Image] = []
        try:
            for raw in page_paths:
                with Image.open(raw) as src:
                    frame = src.convert("RGB")
                    if frame.width != width:
                        new_height = round(frame.height * width / frame.width)
                        frame = frame.resize((width, new_height), Image.Resampling.LANCZOS)
                    images.append(frame.copy())
            total_height = sum(image.height for image in images)
            if width * total_height > 120_000_000:
                raise RuntimeError(f"拼接后的长图像素超过上限（{width}×{total_height}）")
            stitched = Image.new("RGB", (width, total_height), background)
            y = 0
            for image in images:
                stitched.paste(image, (0, y))
                y += image.height
            stitched.save(output_path, "PNG", optimize=True)
            return str(output_path.resolve())
        finally:
            for image in images:
                try:
                    image.close()
                except Exception:
                    pass

    @staticmethod
    def _font(size: int, font_key: str = "lxgw", font_data: bytes | None = None,
              font_path: Path | None = None):
        # The footer uses the same font snapshot as the HTML body.
        if font_key == "custom" and font_data:
            try:
                return ImageFont.truetype(io.BytesIO(font_data), size)
            except OSError:
                pass
        if font_path is not None:
            try:
                return ImageFont.truetype(str(font_path), size)
            except OSError:
                pass
        return ImageFont.load_default()

    @classmethod
    def _footer(cls, page: Image.Image, width: int, colors: dict, number: int, total: int,
                source: str | None, limited: bool, max_pages: int, font_key: str = "lxgw",
                font_data: bytes | None = None, font_path: Path | None = None) -> Image.Image:
        url_font = cls._font(13, font_key, font_data, font_path)
        url_lines: list[str] = []
        line = ""
        for character in ((source or "来源未提供") if source is not None else ""):
            if line and url_font.getlength(line + character) > width - 88:
                url_lines.append(line)
                line = ""
            line += character
        if line:
            url_lines.append(line)
        warning_lines: list[str] = []
        warning_font = cls._font(17, font_key, font_data, font_path)
        if limited:
            line = ""
            for character in f"已达 {max_pages} 页上限，后续内容未收录。请打开原帖或提高页数上限。":
                if line and warning_font.getlength(line + character) > width - 88:
                    warning_lines.append(line)
                    line = ""
                line += character
            if line:
                warning_lines.append(line)
        page_label = f"{number} / {total}" if total > 1 else ""
        if not (page_label or url_lines or warning_lines):
            return page.copy()
        height = 24 + (24 if page_label else 0) + len(url_lines) * 17 + (12 + len(warning_lines) * 24 if limited else 0)
        final = Image.new("RGB", (width, page.height + height), colors["bg"])
        final.paste(page, (0, 0))
        draw = ImageDraw.Draw(final)
        y = page.height + 12
        if page_label:
            label_font = cls._font(15, font_key, font_data, font_path)
            draw.text((width - 35 - draw.textlength(page_label,font=label_font), y), page_label, font=label_font, fill=colors["muted"])
            y += 24
        # Retain the complete source URL; wrap instead of ellipsizing a source.
        for line in url_lines:
            draw.text((35, y), line, font=url_font, fill=colors["muted"])
            y += 17
        if limited:
            y += 12
            for line in warning_lines:
                draw.text((35, y), line, font=warning_font, fill=colors["ink"])
                y += 24
        return final
