"""Bounded HTTPS DNS fallback for one observed official Xiaoheihe image host."""

import asyncio
import ipaddress
import json
import re
import socket
import ssl

OFFICIAL_IMAGE_HOSTS = frozenset({"imgheybox1.max-c.com"})
_HOSTS = OFFICIAL_IMAGE_HOSTS
_DNS_IP = "223.5.5.5"
_DNS_HOST = "dns.alidns.com"
_DNS_TIMEOUT = 5
_DNS_MAX_BYTES = 32768


def _parse_secure_dns(host, body):
    """Validate a bounded DNS JSON answer and its complete CNAME chain.

    Args:
        host: A fixed allowlisted Xiaoheihe CDN hostname.
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
        name = record["name"].lower().removesuffix(".")
        names = [name]
        if record["type"] == 5:
            target = record["data"].lower().removesuffix(".")
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
        host: One fixed allowlisted Xiaoheihe CDN hostname, never an image URL.

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
                except Exception:  # noqa: BLE001 - cleanup must preserve the primary failure
                    writer.transport.abort()
            else:
                writer.transport.abort()
