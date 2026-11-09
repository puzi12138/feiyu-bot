"""Bounded, current-event-only QQ image input for the friend exception."""

# Exact JSON integer and known exception types deliberately exclude subclasses.
# ruff: noqa: E721

import asyncio
import base64
import io
import ipaddress
import json
import re
import socket
import ssl
import warnings
import zlib
from typing import NamedTuple
from urllib.parse import urlsplit

from PIL import Image, ImageOps

MAX_IMAGES = 3
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_JPEG_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
# Permit narrow long screenshots within the pixel budget, while bounding an
# extreme one-pixel-wide input independently of the total decoded area.
MAX_SIDE = 32768
IMAGE_TIMEOUT = 15
_HOSTS = frozenset({"multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn"})
IMAGE_DIAGNOSTIC_VERSION = "friend_image_compat_v2"
_FAKE_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_DNS_IP = "223.5.5.5"
_DNS_HOST = "dns.alidns.com"
_DNS_TIMEOUT = 5
_DNS_MAX_BYTES = 32768
_IMAGE_CONNECT_TIMEOUT = 3
_IMAGE_CONNECT_ADDRESSES = 3
_DIAGNOSTIC_STAGES = frozenset(
    {"persona", "load", "model", "system_dns", "secure_dns", "download", "decode"}
)
_REASONS = frozenset(
    {
        "image_reference",
        "image_dns",
        "image_dns_fake",
        "image_dns_answer",
        "image_dns_question",
        "image_dns_response",
        "image_dns_size",
        "image_dns_header",
        "image_dns_status",
        "image_dns_encoding",
        "image_header",
        "image_status",
        "image_encoding",
        "image_size",
        "image_chunk",
        "image_empty",
        "image_dimensions",
        "image_format",
        "image_animation",
        "image_side",
        "image_pixels",
        "image_decode",
    }
)
_diagnostics = {
    stage: {"success": 0, "failure": 0, "last_outcome": "never", "last_reason": "none"}
    for stage in _DIAGNOSTIC_STAGES
}


class NormalizedImage(NamedTuple):
    """One still input and a conservative indication of frame-only meaning."""

    data_uri: str
    first_frame_only: bool


def record_image_diagnostic(stage, outcome, error=None):
    """Record only fixed classifications and bounded in-memory counts.

    Args:
        stage: One fixed image processing stage.
        outcome: Either success or failure.
        error: Optional exception, never rendered or persisted here.
    """
    if stage not in _DIAGNOSTIC_STAGES or outcome not in {"success", "failure"}:
        return
    reason = "none" if outcome == "success" else "other_error"
    if outcome == "failure":
        if (
            type(error) is ValueError
            and len(error.args) == 1
            and type(error.args[0]) is str
            and error.args[0] in _REASONS
        ):
            reason = error.args[0]
        elif isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            reason = "timeout"
        elif isinstance(error, ssl.SSLError):
            reason = "tls_error"
        elif isinstance(error, PermissionError):
            reason = "permission_error"
        elif isinstance(
            error, (asyncio.IncompleteReadError, asyncio.LimitOverrunError)
        ):
            reason = "response_error"
        elif isinstance(error, (OSError, ConnectionError)):
            reason = "network_error"
        elif isinstance(error, ValueError):
            reason = "value_error"
    current = _diagnostics[stage]
    current[outcome] = min(current[outcome] + 1, 1_000_000_000)
    current["last_outcome"], current["last_reason"] = outcome, reason


def image_diagnostic_status():
    """Return a detached safe snapshot without URLs, identities or addresses.

    Returns:
        The local diagnostic version and bounded fixed stage counters.
    """
    return {
        "version": IMAGE_DIAGNOSTIC_VERSION,
        "stages": {stage: dict(_diagnostics[stage]) for stage in sorted(_diagnostics)},
    }


def _parse_secure_dns(host, body):
    """Validate a bounded DNS JSON answer and its complete CNAME chain.

    Args:
        host: A fixed allowlisted QQ CDN hostname.
        body: The bounded HTTPS response bytes.

    Returns:
        Public IPv4 addresses belonging to the queried DNS chain.

    Raises:
        ValueError: If any question, record, association or address is unsafe.
    """
    if host not in _HOSTS or len(body) > _DNS_MAX_BYTES:
        raise ValueError("image_dns_response")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("image_dns_response") from None
    if (
        not isinstance(payload, dict)
        or type(payload.get("Status")) is not int
        or payload["Status"] != 0
        or payload.get("TC") is not False
    ):
        raise ValueError("image_dns_response")
    question = payload.get("Question")
    if isinstance(question, dict):
        question = [question]
    if (
        not isinstance(question, list)
        or len(question) != 1
        or not isinstance(question[0], dict)
        or type(question[0].get("type")) is not int
        or question[0]["type"] != 1
        or not isinstance(question[0].get("name"), str)
        or question[0]["name"].lower().removesuffix(".") != host
    ):
        raise ValueError("image_dns_question")
    answers = payload.get("Answer")
    if not isinstance(answers, list) or not 1 <= len(answers) <= 64:
        raise ValueError("image_dns_answer")
    aliases, address_records = {}, {}
    for record in answers:
        if (
            not isinstance(record, dict)
            or type(record.get("type")) is not int
            or record["type"] not in {1, 5}
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("data"), str)
        ):
            raise ValueError("image_dns_answer")
        name = record["name"].lower()
        if name.endswith("."):
            name = name[:-1]
        names = [name]
        if record["type"] == 5:
            target = record["data"].lower()
            if target.endswith("."):
                target = target[:-1]
            names.append(target)
        for item in names:
            if (
                not item.isascii()
                or len(item) > 253
                or not all(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in item.split(".")
                )
            ):
                raise ValueError("image_dns_answer")
        if record["type"] == 5:
            if name in aliases:
                raise ValueError("image_dns_answer")
            aliases[name] = target
        else:
            try:
                ip = ipaddress.ip_address(record["data"])
            except ValueError:
                raise ValueError("image_dns_answer") from None
            if ip.version != 4 or not ip.is_global or ip.is_multicast:
                raise ValueError("image_dns_answer")
            address_records.setdefault(name, []).append((socket.AF_INET, str(ip)))
    reached = set()
    current = host
    while current in aliases:
        if current in reached or current in address_records or len(reached) >= 8:
            raise ValueError("image_dns_answer")
        reached.add(current)
        current = aliases[current]
    if (
        current in reached
        or current not in address_records
        or set(aliases) != reached
        or set(address_records) != {current}
    ):
        raise ValueError("image_dns_answer")
    return list(dict.fromkeys(address_records[current]))


async def _secure_dns_once(host):
    """Query one fixed certificate-verified HTTPS resolver without retries.

    Args:
        host: One fixed allowlisted QQ CDN hostname, never an image URL.

    Returns:
        Validated public IPv4 addresses from one associated A response.

    Raises:
        ValueError: If the destination or HTTP/DNS response is unsafe.
    """
    if host not in _HOSTS:
        raise ValueError("image_dns")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DNS_TIMEOUT
    writer = None
    cancelled = False
    try:
        reader, writer = await asyncio.open_connection(
            _DNS_IP,
            443,
            family=socket.AF_INET,
            flags=socket.AI_NUMERICHOST,
            ssl=ssl.create_default_context(),
            server_hostname=_DNS_HOST,
            limit=16384,
        )
        request = (
            f"GET /resolve?name={host}&type=A HTTP/1.1\r\nHost: {_DNS_HOST}\r\n"
            "Accept: application/dns-json\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        raw_header = await reader.readuntil(b"\r\n\r\n")
        if len(raw_header) > 16384:
            raise ValueError("image_dns_header")
        lines = raw_header[:-4].split(b"\r\n")
        if not re.fullmatch(rb"HTTP/1\.[01] 200(?: [\x20-\x7e]*)?", lines[0]):
            raise ValueError("image_dns_status")
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            if (
                not sep
                or not re.fullmatch(rb"[A-Za-z0-9-]+", name)
                or name.lower() in headers
            ):
                raise ValueError("image_dns_header")
            headers[name.lower()] = value.strip().lower()
        if headers.get(b"content-encoding", b"identity") != b"identity" or headers.get(
            b"content-type", b""
        ).split(b";", 1)[0] not in {b"application/json", b"application/dns-json"}:
            raise ValueError("image_dns_encoding")
        length, transfer = (
            headers.get(b"content-length"),
            headers.get(b"transfer-encoding"),
        )
        if transfer is not None and (transfer != b"chunked" or length is not None):
            raise ValueError("image_dns_encoding")
        chunks, total = [], 0
        if length is not None:
            if (
                not re.fullmatch(rb"[0-9]{1,8}", length)
                or not 1 <= int(length) <= _DNS_MAX_BYTES
            ):
                raise ValueError("image_dns_size")
            body = await reader.readexactly(int(length))
        elif transfer == b"chunked":
            while True:
                line = await reader.readuntil(b"\r\n")
                if not re.fullmatch(rb"[0-9A-Fa-f]{1,8}\r\n", line):
                    raise ValueError("image_dns_encoding")
                size = int(line[:-2], 16)
                if size == 0:
                    if await reader.readexactly(2) != b"\r\n":
                        raise ValueError("image_dns_encoding")
                    break
                total += size
                if total > _DNS_MAX_BYTES:
                    raise ValueError("image_dns_size")
                part = await reader.readexactly(size + 2)
                if part[-2:] != b"\r\n":
                    raise ValueError("image_dns_encoding")
                chunks.append(part[:-2])
            body = b"".join(chunks)
        else:
            while True:
                part = await reader.read(min(4096, _DNS_MAX_BYTES + 1 - total))
                if not part:
                    break
                total += len(part)
                if total > _DNS_MAX_BYTES:
                    raise ValueError("image_dns_size")
                chunks.append(part)
            body = b"".join(chunks)
        return _parse_secure_dns(host, body)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if writer is not None:
            writer.close()
            remaining = deadline - loop.time()
            if remaining > 0 and not cancelled:
                try:
                    await asyncio.wait_for(
                        writer.wait_closed(), timeout=min(0.2, remaining)
                    )
                except asyncio.CancelledError:
                    writer.transport.abort()
                    raise
                except Exception:
                    writer.transport.abort()
            else:
                writer.transport.abort()


async def _resolve_image_addresses(host):
    """Permit secure DNS only when every system answer is a fake IPv4 address.

    Args:
        host: A fixed allowlisted QQ CDN hostname.

    Returns:
        Public fixed address tuples for the original QQ TLS hostname.

    Raises:
        ValueError: If system results are empty, private, mixed or malformed.
    """
    try:
        if host not in _HOSTS:
            raise ValueError("image_dns")
        loop = asyncio.get_running_loop()
        resolved = await loop.getaddrinfo(
            host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
        if not resolved or len(resolved) > 64:
            raise ValueError("image_dns")
        addresses, fake_count = [], 0
        for family, socktype, proto, _, address in resolved:
            if (
                family not in (socket.AF_INET, socket.AF_INET6)
                or socktype != socket.SOCK_STREAM
                or proto not in (0, socket.IPPROTO_TCP)
                or not isinstance(address, tuple)
                or not isinstance(address[0], str)
                or "%" in address[0]
                or address[1] != 443
                or (
                    family == socket.AF_INET6 and (len(address) != 4 or address[3] != 0)
                )
            ):
                raise ValueError("image_dns")
            ip = ipaddress.ip_address(address[0])
            if (family == socket.AF_INET) != (ip.version == 4):
                raise ValueError("image_dns")
            if ip.version == 4 and ip in _FAKE_NETWORK:
                fake_count += 1
            elif (
                not ip.is_global
                or ip.is_multicast
                or (ip.version == 6 and ip.is_reserved and not ip.ipv4_mapped)
                or (
                    ip.version == 6
                    and (ip.sixtofour is not None or ip.teredo is not None)
                )
                or (
                    ip.version == 6
                    and ip.ipv4_mapped
                    and (not ip.ipv4_mapped.is_global or ip.ipv4_mapped.is_multicast)
                )
            ):
                raise ValueError("image_dns")
            else:
                addresses.append((family, str(ip)))
        if fake_count and (addresses or fake_count != len(resolved)):
            raise ValueError("image_dns")
        if fake_count:
            record_image_diagnostic(
                "system_dns", "failure", ValueError("image_dns_fake")
            )
        else:
            record_image_diagnostic("system_dns", "success")
            return list(dict.fromkeys(addresses))
    except Exception as error:
        record_image_diagnostic("system_dns", "failure", error)
        raise
    try:
        addresses = await asyncio.wait_for(_secure_dns_once(host), timeout=_DNS_TIMEOUT)
        record_image_diagnostic("secure_dns", "success")
        return addresses
    except Exception as error:
        record_image_diagnostic("secure_dns", "failure", error)
        raise


def valid_image_data(data):
    """Validate the limited SnowLuma image fields without fetching them.

    Args:
        data: One incoming image segment's data.

    Returns:
        Whether it contains an eligible QQ CDN HTTPS reference.
    """
    try:
        if (
            not isinstance(data, dict)
            or not {"file", "url"} <= set(data)
            or set(data) - {"file", "url", "sub_type", "summary", "file_size"}
        ):
            return False
        file_id = data["file"]
        url = data["url"]
        if (
            not isinstance(file_id, str)
            or len(file_id) > 4096
            or not isinstance(url, str)
            or not 1 <= len(url) <= 4096
            or not url.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url)
        ):
            return False
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _HOSTS
            or parsed.netloc not in {parsed.hostname, parsed.hostname + ":443"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            return False
        # The adapter's file field is opaque metadata; only url is ever read.
        if "[CQ:" in file_id.upper() or any(
            ord(c) < 32 or ord(c) == 127 for c in file_id
        ):
            return False
        if "sub_type" in data:
            subtype = data["sub_type"]
            if not (
                (type(subtype) is int and 0 <= subtype <= 999)
                or (
                    isinstance(subtype, str)
                    and subtype.isascii()
                    and subtype.isdigit()
                    and len(subtype) <= 3
                )
            ):
                return False
        if "summary" in data:
            summary = data["summary"]
            if (
                not isinstance(summary, str)
                or len(summary) > 256
                or "[CQ:" in summary.upper()
                or any(ord(c) < 32 or ord(c) == 127 for c in summary)
            ):
                return False
        if "file_size" in data:
            size = data["file_size"]
            if not (
                (type(size) is int and 0 <= size <= MAX_SOURCE_BYTES)
                or (
                    isinstance(size, str)
                    and size.isascii()
                    and size.isdigit()
                    and len(size) <= 8
                    and int(size) <= MAX_SOURCE_BYTES
                )
            ):
                return False
        return True
    except (ValueError, TypeError, UnicodeError):
        return False


async def _connect_image(addresses, host, deadline):
    """Try at most three validated addresses before any HTTP request is sent.

    Only connection errors may move to another address. TLS verification errors
    and cancellation fail closed. The caller sends exactly one GET after this
    returns, and never re-enters this helper after a write or read failure.
    """
    loop = asyncio.get_running_loop()
    context = ssl.create_default_context()
    for family, ip in addresses[:_IMAGE_CONNECT_ADDRESSES]:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError()
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    ip,
                    443,
                    family=family,
                    flags=socket.AI_NUMERICHOST,
                    ssl=context,
                    server_hostname=host,
                    limit=16384,
                ),
                timeout=min(_IMAGE_CONNECT_TIMEOUT, remaining),
            )
        except ssl.SSLError:
            raise
        except (OSError, TimeoutError):
            # No writer was returned, so no application GET has been sent.
            continue
    raise ConnectionError("image_connect_failed")


async def _fetch_image(url):
    """Fetch once through a certificate-verified connection to a fixed public IP.

    Args:
        url: A previously validated QQ CDN URL.

    Returns:
        The bounded response bytes.

    Raises:
        ValueError: If the response or destination is unsafe.
    """
    parsed = urlsplit(url)
    host = parsed.hostname
    loop = asyncio.get_running_loop()
    deadline = loop.time() + IMAGE_TIMEOUT
    addresses = await _resolve_image_addresses(host)
    cancelled = False
    reader, writer = await _connect_image(addresses, host, deadline)
    try:
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
            "Accept: image/*\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        raw_header = await reader.readuntil(b"\r\n\r\n")
        if len(raw_header) > 16384:
            raise ValueError("image_header")
        lines = raw_header[:-4].split(b"\r\n")
        if not re.fullmatch(rb"HTTP/1\.[01] 200(?: [\x20-\x7e]*)?", lines[0]):
            raise ValueError("image_status")
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            if not sep or not re.fullmatch(rb"[A-Za-z0-9-]+", name):
                raise ValueError("image_header")
            key = name.lower()
            if key in headers:
                raise ValueError("image_header")
            headers[key] = value.strip().lower()
        if headers.get(b"content-encoding", b"identity") != b"identity":
            raise ValueError("image_encoding")
        transfer = headers.get(b"transfer-encoding")
        length = headers.get(b"content-length")
        if transfer is not None and (transfer != b"chunked" or length is not None):
            raise ValueError("image_encoding")
        if length is not None:
            if (
                not re.fullmatch(rb"[0-9]{1,8}", length)
                or int(length) > MAX_SOURCE_BYTES
            ):
                raise ValueError("image_size")
            body = await reader.readexactly(int(length))
        elif transfer == b"chunked":
            chunks = []
            total = 0
            while True:
                line = await reader.readuntil(b"\r\n")
                if not re.fullmatch(rb"[0-9A-Fa-f]{1,8}\r\n", line):
                    raise ValueError("image_chunk")
                size = int(line[:-2], 16)
                if not size:
                    if await reader.readexactly(2) != b"\r\n":
                        raise ValueError("image_chunk")
                    break
                total += size
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("image_size")
                part = await reader.readexactly(size + 2)
                if part[-2:] != b"\r\n":
                    raise ValueError("image_chunk")
                chunks.append(part[:-2])
            body = b"".join(chunks)
        else:
            chunks = []
            total = 0
            while True:
                part = await reader.read(min(65536, MAX_SOURCE_BYTES + 1 - total))
                if not part:
                    break
                total += len(part)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("image_size")
                chunks.append(part)
            body = b"".join(chunks)
        if not body:
            raise ValueError("image_empty")
        return body
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        writer.close()
        remaining = deadline - loop.time()
        if remaining > 0 and not cancelled:
            try:
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=min(0.2, remaining)
                )
            except asyncio.CancelledError:
                writer.transport.abort()
                raise
            except Exception:
                writer.transport.abort()
        else:
            writer.transport.abort()


def _check_image_dimensions(size):
    """Validate decoded canvas dimensions before allocating image pixels."""
    width, height = size
    if type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise ValueError("image_dimensions")
    if max(width, height) > MAX_SIDE:
        raise ValueError("image_side")
    if width * height > MAX_PIXELS:
        raise ValueError("image_pixels")


def _preflight_image(raw):
    """Check canvas headers before Pillow can allocate GIF/PNG/WebP pixels.

    Only the GIF first descriptor is needed: later frames are never decoded.
    Its offsets can enlarge a tiny logical screen, so check their union too.
    WebP's first VP8X/VP8/VP8L header defines its decoder canvas. JPEG open is
    lazy and retains the dimensions check before its first pixel load.
    """
    if raw.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(raw) < 33 or raw[8:16] != b"\x00\x00\x00\rIHDR":
            raise ValueError("image_decode")
        canvas = (
            int.from_bytes(raw[16:20], "big"),
            int.from_bytes(raw[20:24], "big"),
        )
        _check_image_dimensions(canvas)
        pos, seen_animation, seen_control = 8, False, False
        while pos + 12 <= len(raw):
            length = int.from_bytes(raw[pos : pos + 4], "big")
            kind = raw[pos + 4 : pos + 8]
            end = pos + 12 + length
            if end > len(raw):
                raise ValueError("image_decode")
            if kind == b"IHDR" and pos != 8:
                raise ValueError("image_decode")
            if kind in {b"IHDR", b"acTL", b"fcTL"}:
                expected_crc = int.from_bytes(raw[end - 4 : end], "big")
                if zlib.crc32(raw[pos + 4 : end - 4]) != expected_crc:
                    raise ValueError("image_decode")
            if kind == b"acTL":
                frames = int.from_bytes(raw[pos + 8 : pos + 12], "big")
                if seen_animation or length != 8 or not 1 <= frames <= 0x80000000:
                    raise ValueError("image_decode")
                seen_animation = True
            if kind == b"fcTL":
                if not seen_animation or seen_control or length != 26:
                    raise ValueError("image_decode")
                control = raw[pos + 8 : end - 4]
                if (
                    control[:4] != b"\x00\x00\x00\x00"
                    or control[24] > 2
                    or control[25] > 1
                ):
                    raise ValueError("image_decode")
                width = int.from_bytes(control[4:8], "big")
                height = int.from_bytes(control[8:12], "big")
                left = int.from_bytes(control[12:16], "big")
                top = int.from_bytes(control[16:20], "big")
                _check_image_dimensions((width, height))
                if left + width > canvas[0] or top + height > canvas[1]:
                    raise ValueError("image_decode")
                seen_control = True
            if kind == b"IDAT":
                return "PNG"
            if kind in {b"IEND", b"fdAT"}:
                raise ValueError("image_decode")
            pos = end
        raise ValueError("image_decode")
    if raw[:6] in {b"GIF87a", b"GIF89a"}:
        if len(raw) < 13:
            raise ValueError("image_decode")
        width = int.from_bytes(raw[6:8], "little")
        height = int.from_bytes(raw[8:10], "little")
        _check_image_dimensions((width, height))
        flags = raw[10]
        pos = 13 + (3 * (1 << ((flags & 7) + 1)) if flags & 128 else 0)
        while pos < len(raw):
            marker = raw[pos]
            pos += 1
            if marker == 0x2C:
                if pos + 9 > len(raw):
                    raise ValueError("image_decode")
                left = int.from_bytes(raw[pos : pos + 2], "little")
                top = int.from_bytes(raw[pos + 2 : pos + 4], "little")
                frame_width = int.from_bytes(raw[pos + 4 : pos + 6], "little")
                frame_height = int.from_bytes(raw[pos + 6 : pos + 8], "little")
                _check_image_dimensions((frame_width, frame_height))
                _check_image_dimensions(
                    (max(width, left + frame_width), max(height, top + frame_height))
                )
                return "GIF"
            if marker != 0x21 or pos >= len(raw):
                raise ValueError("image_decode")
            label = raw[pos]
            pos += 1
            # Do not reproduce Pillow's permissive handling of malformed GIF
            # extensions: it can consume a following descriptor as subblock
            # data, changing which frame is first. Accept strict known forms.
            expected = {0xF9: 4, 0xFF: 11, 0x01: 12}
            if label != 0xFE:
                if label not in expected or pos >= len(raw):
                    raise ValueError("image_decode")
                length = raw[pos]
                pos += 1
                if length != expected[label] or pos + length > len(raw):
                    raise ValueError("image_decode")
                header = raw[pos : pos + length]
                pos += length
                if label == 0xFF and header == b"NETSCAPE2.0":
                    # Pillow consumes this loop subblock separately before its
                    # ordinary loop. Require the canonical nonempty form.
                    if pos + 4 > len(raw) or raw[pos : pos + 2] != b"\x03\x01":
                        raise ValueError("image_decode")
                    pos += 4
                if label == 0xF9 or (label == 0xFF and header == b"NETSCAPE2.0"):
                    if pos >= len(raw) or raw[pos] != 0:
                        raise ValueError("image_decode")
                    pos += 1
                    continue
            # Comment/application/text data use zero-terminated subblocks.
            while True:
                if pos >= len(raw):
                    raise ValueError("image_decode")
                length = raw[pos]
                pos += 1
                if not length:
                    break
                pos += length
                if pos > len(raw):
                    raise ValueError("image_decode")
        raise ValueError("image_decode")
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        if len(raw) < 20:
            raise ValueError("image_decode")
        end = int.from_bytes(raw[4:8], "little") + 8
        length = int.from_bytes(raw[16:20], "little")
        if end > len(raw) or 20 + length > end:
            raise ValueError("image_decode")
        kind = raw[12:16]
        if kind == b"VP8X" and length == 10:
            width = int.from_bytes(raw[24:27], "little") + 1
            height = int.from_bytes(raw[27:30], "little") + 1
        elif kind == b"VP8 " and length >= 10 and raw[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(raw[26:28], "little") & 0x3FFF
            height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        elif kind == b"VP8L" and length >= 5 and raw[20] == 0x2F:
            bits = int.from_bytes(raw[21:25], "little")
            width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        else:
            raise ValueError("image_decode")
        _check_image_dimensions((width, height))
        return "WEBP"
    if raw[:2] == b"BM" or raw[:4] in {b"II*\x00", b"MM\x00*"}:
        raise ValueError("image_format")
    raise ValueError("image_decode")


def _frame_selection(source):
    """Identify animation-capable containers without counting all frames.

    GIF's n_frames property can scan the entire animation, so it is never read.
    GIF/WebP containers are conservatively marked frame-only even when still.
    APNG's optional default image is the static preview. Never seek beyond it.
    """
    if source.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
        raise ValueError("image_format")
    apng = source.format == "PNG" and (
        getattr(source, "custom_mimetype", None) == "image/apng"
    )
    return source.format in {"GIF", "WEBP"} or apng


def _normalize_image(raw, *, with_metadata=False):
    """Decode one bounded still frame and emit a metadata-free JPEG.

    Args:
        raw: Downloaded bytes.

    Returns:
        A JPEG data URI, or NormalizedImage when with_metadata is true.

    Raises:
        ValueError: If the image cannot be safely decoded or normalized.
    """
    try:
        if not raw or len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("image_size")
        image_format = _preflight_image(raw)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=[image_format]) as probe:
                first_frame_only = _frame_selection(probe)
                _check_image_dimensions(probe.size)
                # PNG verify scans through IEND, including every APNG frame.
                # Keep the original still-image check, but decode only the
                # selected frame for animation-capable inputs.
                if not first_frame_only:
                    probe.verify()
            with Image.open(io.BytesIO(raw), formats=[image_format]) as source:
                _check_image_dimensions(source.size)
                try:
                    source.load()
                except (Image.DecompressionBombWarning, Image.DecompressionBombError):
                    raise
                except Exception:
                    raise ValueError(
                        "image_animation" if first_frame_only else "image_decode"
                    ) from None
                _check_image_dimensions(source.size)
                image = ImageOps.exif_transpose(source)
                image.thumbnail((2048, 2048))
                if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                # Pillow can implicitly carry JPEG/GIF comments into JPEG.
                # Orientation has already been applied; retain no source info.
                image.info.clear()
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=85, optimize=True)
                jpeg = output.getvalue()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError("image_pixels") from None
    except Exception as error:
        if (
            type(error) is ValueError
            and len(error.args) == 1
            and type(error.args[0]) is str
            and error.args[0] in _REASONS
        ):
            raise
        raise ValueError("image_decode") from None
    if not jpeg or len(jpeg) > MAX_JPEG_BYTES:
        raise ValueError("image_size")
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    return NormalizedImage(uri, first_frame_only) if with_metadata else uri


async def load_qq_image(data, *, with_metadata=False):
    """Validate, fetch once and normalize an image without writing to disk.

    Args:
        data: The frozen current-event image segment.

    Returns:
        A JPEG data URI, or NormalizedImage when with_metadata is true.

    Raises:
        ValueError: If the image reference is invalid.
    """
    if not valid_image_data(data):
        raise ValueError("image_reference")
    try:
        raw = await asyncio.wait_for(_fetch_image(data["url"]), timeout=IMAGE_TIMEOUT)
        record_image_diagnostic("download", "success")
    except Exception as error:
        record_image_diagnostic("download", "failure", error)
        raise
    try:
        result = await asyncio.to_thread(
            _normalize_image, raw, with_metadata=with_metadata
        )
        record_image_diagnostic("decode", "success")
        return result
    except Exception as error:
        record_image_diagnostic("decode", "failure", error)
        raise
