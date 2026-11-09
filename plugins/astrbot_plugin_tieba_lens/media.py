"""Bounded downloads of original body images for merged-forward attachments.

Files retain their exact downloaded image bytes, including animated GIFs. This
module does not send messages, use account cookies, or modify card rendering.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import os
import socket
import tempfile
import time
import warnings
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

_CDN_HOSTS = frozenset({
    "tiebapic.baidu.com", "imgsa.baidu.com", "imgsrc.baidu.com",
    "tb.himg.baidu.com", "himg.baidu.com", "himg.bdimg.com", "hiphotos.baidu.com",
    "tb1.bdstatic.com", "tb2.bdstatic.com", "tb3.bdstatic.com", "tb4.bdstatic.com",
    "gss0.baidu.com", "gss3.baidu.com", "gss0.bdstatic.com", "gss1.bdstatic.com",
    "gss2.bdstatic.com", "gss3.bdstatic.com", "ss0.bdstatic.com", "ss1.bdstatic.com",
    "ss2.bdstatic.com", "ss3.bdstatic.com", "b.bdstatic.com", "emoji.cdn.bcebos.com",
})
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_TOTAL_BYTES = 80 * 1024 * 1024
_TOTAL_SECONDS = 40
_MAX_PIXELS = 40_000_000
_CONCURRENCY = 3
_MAX_IMAGES = 100


class _MediaFailure(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _safe_url(value, *, redirect=False):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or "\\" in value or any(ord(c) < 33 for c in value)):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ({"https"} if redirect else {"http", "https"})
                or parts.hostname not in _CDN_HOSTS or parts.username is not None
                or parts.password is not None or parts.netloc.endswith(":")
                or parts.port is not None and parts.port != (443 if parts.scheme == "https" else 80)):
            return None
        # Drop fragments; retain signatures/query parameters required by CDNs.
        return f"https://{parts.hostname}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
    except (ValueError, UnicodeError):
        return None


async def _public_address(host):
    if host not in _CDN_HOSTS:
        raise _MediaFailure("安全地址校验失败")
    try:
        rows = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM), 5)
    except (OSError, TimeoutError) as exc:
        raise _MediaFailure("网络连接失败") from exc
    addresses = []
    for row in rows:
        try:
            address = ipaddress.ip_address(row[4][0])
        except ValueError as exc:
            raise _MediaFailure("安全地址校验失败") from exc
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise _MediaFailure("安全地址校验失败")
        addresses.append(address)
    if not addresses:
        raise _MediaFailure("网络连接失败")
    addresses.sort(key=lambda item: item.version)
    return str(addresses[0])


class _PinnedMediaTransport(httpx.AsyncBaseTransport):
    """Pin a checked public IP, retaining original Host and verified TLS SNI."""
    def __init__(self):
        self.pools = {}

    async def handle_async_request(self, request):
        if _safe_url(str(request.url), redirect=True) is None:
            raise _MediaFailure("安全地址校验失败")
        host = request.url.host
        address = await _public_address(host)
        pool = self.pools.get(host)
        if pool is None:
            pool = self.pools[host] = httpx.AsyncHTTPTransport(
                retries=0, limits=httpx.Limits(max_connections=_CONCURRENCY, max_keepalive_connections=1))
        headers = request.headers.copy()
        headers["Host"] = host
        headers.pop("Cookie", None)
        headers.pop("Authorization", None)
        extensions = {**request.extensions, "sni_hostname": host}
        pinned = httpx.Request(request.method, request.url.copy_with(host=address),
                               headers=headers, stream=request.stream, extensions=extensions)
        return await pool.handle_async_request(pinned)

    async def aclose(self):
        for pool in self.pools.values():
            await pool.aclose()
        self.pools.clear()


def _collect(document, options):
    try:
        limit = min(_MAX_IMAGES, max(0, int(options.get("image_limit", 40))))
    except (TypeError, ValueError, OverflowError):
        limit = 40
    if not options.get("include_images", True) or not limit:
        return []
    result = []
    seen = set()
    slots = nodes = 0

    def add(value, label):
        nonlocal slots
        if slots >= limit:
            return
        slots += 1
        if not isinstance(value, str) or not value:
            return
        key = _safe_url(value) or value
        if key not in seen:
            seen.add(key)
            result.append({"url": value, "label": label})

    def visit(post, depth=0, parent=""):
        nonlocal nodes
        nodes += 1
        if depth > 8 or nodes > 6000 or not isinstance(post, dict) or slots >= limit:
            return
        floor = post.get("floor")
        label = f"{floor}楼" if floor else "正文"
        if depth:
            label = f"{parent} · 楼中楼"
        number = 0
        for segment in (post.get("segments") or [])[:10000]:
            if isinstance(segment, dict) and segment.get("type") == "image":
                number += 1
                add(segment.get("url"), f"{label} · 原图{number}")
        for comment in (post.get("comments") or [])[:6000]:
            visit(comment, depth + 1, label)

    for post in (document.get("posts") or [])[:6000]:
        visit(post)
    for index, thread in enumerate((document.get("threads") or [])[:1000]):
        if not isinstance(thread, dict):
            continue
        for number, value in enumerate((thread.get("images") or [])[:1000]):
            add(value, f"主页帖子{index + 1} · 原图{number + 1}")
    return result


def _validate_image(body):
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        expected, extension = "PNG", ".png"
        if len(body) < 20 or body[-8:-4] != b"IEND":
            raise _MediaFailure("图片数据不完整")
    elif body.startswith(b"\xff\xd8\xff"):
        expected, extension = "JPEG", ".jpg"
        if b"\xff\xd9" not in body[-32:]:
            raise _MediaFailure("图片数据不完整")
    elif body.startswith((b"GIF87a", b"GIF89a")):
        expected, extension = "GIF", ".gif"
        _verify_gif_structure(body)
    elif body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        expected, extension = "WEBP", ".webp"
        if len(body) < 12 or int.from_bytes(body[4:8], "little") + 8 != len(body):
            raise _MediaFailure("图片数据不完整")
    else:
        raise _MediaFailure("不是支持的原始图片格式")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as image:
                if image.format != expected or image.width < 1 or image.height < 1:
                    raise _MediaFailure("图片格式校验失败")
                if image.width * image.height > _MAX_PIXELS:
                    raise _MediaFailure("图片像素超过安全上限")
                image.verify()
    except _MediaFailure:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise _MediaFailure("图片格式校验失败") from exc
    return extension


def _verify_gif_structure(body):
    """Validate every GIF frame/block without expanding animation pixels."""
    if len(body) < 14:
        raise _MediaFailure("图片数据不完整")
    pos = 13
    if body[10] & 128:
        pos += 3 * (1 << ((body[10] & 7) + 1))
    frames = 0

    def blocks(position):
        while position < len(body):
            size = body[position]
            position += 1
            if not size:
                return position
            position += size
            if position > len(body):
                break
        raise _MediaFailure("图片数据不完整")

    while pos < len(body):
        tag = body[pos]
        pos += 1
        if tag == 0x3B:
            if pos != len(body) or not frames:
                raise _MediaFailure("图片格式校验失败")
            return
        if tag == 0x21:
            if pos >= len(body):
                break
            pos = blocks(pos + 1)  # extension label, then sized data blocks
        elif tag == 0x2C:
            if pos + 9 > len(body):
                break
            width = int.from_bytes(body[pos + 4:pos + 6], "little")
            height = int.from_bytes(body[pos + 6:pos + 8], "little")
            flags = body[pos + 8]
            if not width or not height or width * height > _MAX_PIXELS:
                raise _MediaFailure("图片像素超过安全上限")
            pos += 9
            if flags & 128:
                pos += 3 * (1 << ((flags & 7) + 1))
            if pos >= len(body):
                break
            pos = blocks(pos + 1)  # LZW minimum-code-size byte
            frames += 1
            if frames > 2000:
                raise _MediaFailure("动图帧数超过安全上限")
        else:
            raise _MediaFailure("图片格式校验失败")
    raise _MediaFailure("图片数据不完整")


def _atomic_write(folder, name, body):
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != folder.parent:
        raise _MediaFailure("原图保存目录不可用")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".original-", suffix=".part", dir=folder, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
        target = folder / name
        os.replace(temporary, target)
        return str(target.resolve())
    except OSError as exc:
        raise _MediaFailure("原图保存失败") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def download_originals(document: dict, options: dict, root: Path) -> tuple[list[dict], list[str]]:
    """Download displayed body images in source order, preserving original bytes.

    Returns ``([{path,label,url}, ...], warnings)``. Individual failures are
    reported and do not abort card delivery or the remaining downloads. Only an
    already-existing job directory and its ``originals/`` child are written.
    """
    selected = _collect(document, options)
    if not selected:
        return [], []
    try:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            raise OSError("invalid job root")
        root = root.resolve(strict=True)
        folder = root / "originals"
        if folder.is_symlink():
            raise OSError("unsafe originals directory")
        folder.mkdir(exist_ok=True)
        if folder.resolve().parent != root:
            raise OSError("unsafe originals directory")
    except (OSError, RuntimeError):
        return [], ["正文原图保存目录不可用，已保留卡片。"]

    deadline = time.monotonic() + _TOTAL_SECONDS
    results = [None] * len(selected)
    failures = Counter()
    total_bytes = 0
    byte_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def fetch(index, item, client):
        nonlocal total_bytes
        try:
            async with semaphore:
                async with asyncio.timeout(max(.001, deadline - time.monotonic())):
                    current = _safe_url(item["url"])
                    if current is None:
                        raise _MediaFailure("安全地址校验失败")
                    visited = set()
                    for _ in range(4):
                        if current in visited:
                            raise _MediaFailure("跳转无效")
                        visited.add(current)
                        request = client.build_request("GET", current)
                        request.headers.pop("Cookie", None)
                        request.headers.pop("Authorization", None)
                        response = await client.send(request, stream=True, follow_redirects=False)
                        try:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                current = _safe_url(urljoin(current, response.headers.get("location", "")), redirect=True)
                                if current is None:
                                    raise _MediaFailure("安全地址校验失败")
                                continue
                            if response.status_code != 200:
                                raise _MediaFailure("源站拒绝或图片不存在")
                            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            if not (mime.startswith("image/") or mime == "application/octet-stream"):
                                raise _MediaFailure("源站未返回图片")
                            declared = response.headers.get("content-length")
                            try:
                                declared = int(declared) if declared is not None else None
                            except ValueError:
                                raise _MediaFailure("图片长度无效") from None
                            if declared is not None and (declared < 0 or declared > _MAX_IMAGE_BYTES):
                                raise _MediaFailure("单张原图超过大小限制")
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                async with byte_lock:
                                    if total_bytes + len(chunk) > _MAX_TOTAL_BYTES:
                                        raise _MediaFailure("本次原图总大小达到上限")
                                    total_bytes += len(chunk)
                                if len(body) + len(chunk) > _MAX_IMAGE_BYTES:
                                    raise _MediaFailure("单张原图超过大小限制")
                                body.extend(chunk)
                            if (declared is not None and not response.headers.get("content-encoding")
                                    and len(body) != declared):
                                raise _MediaFailure("图片数据不完整")
                            raw = bytes(body)
                            extension = await asyncio.to_thread(_validate_image, raw)
                            if time.monotonic() >= deadline:
                                raise _MediaFailure("下载超时")
                            name = f"original-{index + 1:03d}-{hashlib.sha256(current.encode()).hexdigest()[:12]}{extension}"
                            path = _atomic_write(folder, name, raw)
                            results[index] = {"path": path, "label": item["label"], "url": item["url"]}
                            return
                        finally:
                            await response.aclose()
                            client.cookies.clear()
                    raise _MediaFailure("跳转次数超过限制")
        except _MediaFailure as exc:
            failures[exc.reason] += 1
        except (TimeoutError, httpx.TimeoutException):
            failures["下载超时"] += 1
        except httpx.HTTPError:
            failures["网络连接或证书校验失败"] += 1
        except (OSError, ValueError, RuntimeError):
            failures["原图处理失败"] += 1
        except Exception:
            failures["原图处理失败"] += 1

    async with httpx.AsyncClient(transport=_PinnedMediaTransport(), trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(12, connect=5),
                                 headers={"User-Agent": "TiebaLens/1.0", "Accept": "image/*, application/octet-stream"}) as client:
        tasks = [asyncio.create_task(fetch(index, item, client)) for index, item in enumerate(selected)]
        try:
            _, pending = await asyncio.wait(tasks, timeout=max(0, deadline - time.monotonic()))
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            failures["下载超时"] += len(pending)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    output = [item for item in results if item is not None]
    missing = len(selected) - len(output)
    warning_list = []
    if missing:
        detail = "；".join(f"{reason} {count} 张" for reason, count in failures.items() if count)
        warning_list.append(f"{missing} 张正文原图未能附带（{detail or '原图不可用'}），已保留卡片。")
    return output, warning_list
