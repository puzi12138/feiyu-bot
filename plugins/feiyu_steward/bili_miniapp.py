"""Request and validate a fresh, official Bilibili QQ mini-app card.

This module only invokes the read-only AdaptShareInfo factory. It never sends a
message, fetches a URL, modifies a server-signed card, or caches card tokens.
Callers must supply the title/image from the real video's official metadata.

Protocol and field semantics are based on NapNeko/NapCatQQ's
packages/napcat-core/packet/transformer/proto/action/miniAppAdaptShareInfo.ts,
packet/utils/helper/miniAppHelper.ts and issues 995 / 828. The same complete
schema and documented pages/video/video path were verified locally on
2026-10-25. The native SnowLuma factory currently uses the wrong body fields
5/6/7 for pic/jump/icon; their correct fields are 9/11/12.
"""

import asyncio
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit

_APPID = "1109937557"
_VIEW = "view_8C8E89B49BE609866298ADDFF2DBABA4"
_VERSION = "1.0.0.19"
_SDK = "V1_PC_MINISDK_99.99.99_1_APP_A"
_VERSION_ID = "cfc5f7b05b44b5956502edaecf9d2240"
_ICON = "https://miniapp.gtimg.cn/public/appicon/51f90239b78a2e4994c11215f4c4ba15_200.jpg"
_FACTORY = "LightAppSvc.mini_app_share.AdaptShareInfo"
_MAX_PACKET = 131072
_MAX_JSON = 65536
_VIDEO = re.compile(r"https://www\.bilibili\.com/video/(BV[A-Za-z0-9]{10})/(?:\?p=([1-9][0-9]{0,5}))?\Z")


def _utf8_size(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        return None
    try:
        size = len(value.encode("utf-8"))
        return size if size <= maximum else None
    except UnicodeError:
        return None


def _public_https(value):
    """Syntax check only; no DNS or HTTP. Local/numeric destinations fail."""
    if (not isinstance(value, str) or not value or len(value) > 4096
            or re.search(r"[\x00-\x20\x7f\\]", value)):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443}
                or host.endswith(".") or "%" in host):
            return False
    except ValueError:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return bool(
            re.fullmatch(r"[A-Za-z0-9.-]+", host)
            and "." in host
            and not host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".invalid", ".onion", ".test", ".arpa"))
            and re.match(r"[A-Za-z]", host.rsplit(".", 1)[-1])
            and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.split("."))
        )


def _varint(value):
    out = bytearray()
    while value > 127:
        out.append(value & 127 | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _bytes(field, value):
    value = value.encode("utf-8") if isinstance(value, str) else value
    return _varint(field << 3 | 2) + _varint(len(value)) + value


def _integer(field, value):
    return _varint(field << 3) + _varint(value)


def _build_request(url, bvid, page, title, image, description):
    # The trailing slash in the bvid parameter matches the documented, locally
    # verified factory input. webURL carries the exact canonical web target.
    jump = "pages/video/video?bvid=" + bvid + "/"
    if page:
        raise ValueError("unsupported_multipart_miniapp")
    body = b"".join((
        _bytes(1, _bytes(2, b"")), _bytes(2, _APPID),
        _bytes(3, title), _bytes(4, description),
        _integer(5, int(time.time() * 1000)), _integer(6, 1),
        _integer(7, 1), _integer(8, 0), _bytes(9, image),
        _bytes(10, ""), _bytes(11, jump), _bytes(12, _ICON),
        _integer(13, 3), _integer(14, 0), _bytes(15, _VERSION_ID),
        _integer(16, 0), _bytes(17, url), _bytes(18, b""),
        _bytes(19, _bytes(1, "") + _bytes(2, "")), _bytes(20, ""),
    ))
    return _bytes(2, _SDK) + _bytes(4, body)


def _decode_fields(data):
    """Bounded flat protobuf decoder; nesting is explicit at the two callers."""
    if not isinstance(data, bytes) or len(data) > _MAX_PACKET:
        raise ValueError("packet_size")
    offset = 0
    fields = {}

    def read_varint(start):
        value = 0
        for index in range(10):
            if start >= len(data):
                raise ValueError("truncated_varint")
            byte = data[start]
            start += 1
            if index == 9 and byte > 1:
                raise ValueError("oversize_varint")
            value |= (byte & 127) << (7 * index)
            if byte < 128:
                return value, start
        raise ValueError("oversize_varint")

    while offset < len(data):
        tag, offset = read_varint(offset)
        field, wire = tag >> 3, tag & 7
        if not 0 < field <= 536870911 or field in fields or len(fields) >= 128:
            raise ValueError("invalid_field")
        if wire == 0:
            value, offset = read_varint(offset)
        elif wire == 2:
            length, offset = read_varint(offset)
            if length > len(data) - offset:
                raise ValueError("truncated_field")
            value = data[offset:offset + length]
            offset += length
        elif wire in {1, 5}:
            length = 8 if wire == 1 else 4
            if length > len(data) - offset:
                raise ValueError("truncated_fixed_field")
            value = data[offset:offset + length]
            offset += length
        else:
            raise ValueError("unsupported_wire")
        fields[field] = value
    return fields


def _unwrap_packet(response):
    for _ in range(3):
        if isinstance(response, str):
            if (not response or len(response) > _MAX_PACKET * 2
                    or len(response) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", response)):
                raise ValueError("invalid_hex")
            return bytes.fromhex(response)
        if not isinstance(response, dict):
            break
        if "retcode" in response and (type(response["retcode"]) is not int or response["retcode"] != 0):
            raise ValueError("factory_failed")
        if "status" in response and response["status"] != "ok":
            raise ValueError("factory_failed")
        response = response.get("data")
    raise ValueError("invalid_envelope")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("invalid_json_constant")


def _bounded_json(value):
    if not isinstance(value, bytes) or len(value) > _MAX_JSON:
        raise ValueError("json_size")
    result = json.loads(value.decode("utf-8"), object_pairs_hook=_unique_object,
                        parse_constant=_reject_json_constant)
    pending = [(result, 0)]
    seen = 0
    while pending:
        item, depth = pending.pop()
        seen += 1
        if depth > 12 or seen > 2048:
            raise ValueError("json_structure")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise TypeError("json_type")
    if not isinstance(result, dict):
        raise TypeError("json_object")
    return result


def _validated_card(raw, url, title, image):
    if (raw.get("appName") != "com.tencent.miniapp_01"
            or raw.get("appView") != _VIEW or raw.get("ver") != _VERSION):
        raise ValueError("card_type_mismatch")
    meta, config = raw.get("metaData"), raw.get("config")
    detail = meta.get("detail_1") if isinstance(meta, dict) else None
    if not isinstance(detail, dict) or not isinstance(config, dict):
        raise TypeError("card_structure_mismatch")
    if (detail.get("appid") != _APPID or type(detail.get("appType")) is not int
            or detail["appType"] != 0 or config.get("type") != "normal"):
        raise ValueError("card_app_mismatch")
    if detail.get("qqdocurl") != url:
        raise ValueError("card_target_mismatch")
    if detail.get("desc") != title or raw.get("prompt") != "[QQ小程序]" + title:
        raise ValueError("card_title_mismatch")
    # The factory's title is the application name, while desc is the video title.
    if detail.get("title") != "哔哩哔哩":
        raise ValueError("card_app_name_mismatch")
    if not isinstance(config.get("token"), str) or not re.fullmatch(r"[A-Fa-f0-9]{32}", config["token"]):
        raise ValueError("card_token_missing")
    if not isinstance(detail.get("url"), str) or not re.fullmatch(
            r"(?:https://)?m\.q\.qq\.com/a/s/[A-Fa-f0-9]{32}", detail["url"]):
        raise ValueError("card_short_url_mismatch")
    # QQ legitimately uploads the supplied cover to qq.ugcimg.cn. Literal URL
    # equality would reject the genuine result; keep the server value intact.
    preview = detail.get("preview")
    if (image and not _public_https(preview)) or (preview not in {None, "", "-"} and not _public_https(preview)):
        raise ValueError("card_preview_mismatch")
    # This is NapCat MiniAppInfoHelper.RawToSend's wire-name conversion. Every
    # nested meta/config field and token is passed through without modification.
    return {
        "ver": raw["ver"], "prompt": raw["prompt"], "config": config,
        "app": raw["appName"], "view": raw["appView"], "meta": meta,
        "miniappShareOrigin": 3, "miniappOpenRefer": "10002",
    }


async def build_bili_miniapp(client, canonical_url, title, image="", description=""):
    """Return a fresh verified JSON segment, or None with a non-sensitive reason.

    No sending or plain-link fallback occurs here. In particular, a failed or
    ambiguous send must never cause callers to generate and resend a new card.
    Multi-part p is deliberately unsupported until the official mini-app path
    has been verified. Callers' ordinary-link fallback must preserve that p.
    """
    match = _VIDEO.fullmatch(canonical_url) if isinstance(canonical_url, str) else None
    if not match:
        return {"segment": None, "reason": "unsupported_video_url"}
    if match[2]:
        return {"segment": None, "reason": "unsupported_multipart_miniapp"}
    if (_utf8_size(title, 2048) is None or not title.strip()
            or re.search(r"[\x00-\x1f\x7f]", title)):
        return {"segment": None, "reason": "missing_or_invalid_official_title"}
    if image and not _public_https(image):
        return {"segment": None, "reason": "invalid_public_image"}
    if not isinstance(image, str) or _utf8_size(description, 8192) is None:
        return {"segment": None, "reason": "invalid_metadata"}
    if client is None or not callable(getattr(client, "call_action", None)):
        return {"segment": None, "reason": "miniapp_factory_unavailable"}
    try:
        packet = _build_request(canonical_url, match[1], match[2], title, image, description)
        response = await asyncio.wait_for(client.call_action(
            "send_packet", cmd=_FACTORY, data=packet.hex(), rsp=True), timeout=12)
        fields = _decode_fields(_unwrap_packet(response))
        if type(fields.get(2)) is not int or fields[2] != 0:
            return {"segment": None, "reason": "miniapp_factory_rejected"}
        content = _decode_fields(fields.get(4))
        raw = _bounded_json(content.get(2))
        card = _validated_card(raw, canonical_url, title, image)
        return {"segment": {"type": "json", "data": {"data": json.dumps(
            card, ensure_ascii=False, separators=(",", ":"))}}, "reason": "verified_official_bili_miniapp"}
    except TimeoutError:
        return {"segment": None, "reason": "miniapp_factory_timeout"}
    except Exception:  # noqa: BLE001 - External packet errors may contain private signed payloads.
        # The exception may contain private signed card contents. Never return
        # its text, the raw packet or a token to logs or the chatbot's context.
        return {"segment": None, "reason": "miniapp_factory_invalid_or_unavailable"}
