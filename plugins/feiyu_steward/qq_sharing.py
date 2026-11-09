"""Prepare QQ source links and strictly verified official Bilibili miniapps.

This helper never sends messages. Its only optional HTTP request reads the
fixed official Bilibili metadata endpoint through the existing Bili plugin.
"""

import asyncio
import json
import re
from urllib.parse import urlsplit, urlunsplit

from . import share_cards

_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_BVID = re.compile(r"BV[A-Za-z0-9]{10}\Z")


def verified_miniapp_segment(card):
    """Accept only the verified factory result at a message composition boundary.

    The generator already validates the signed target and official metadata.
    This extra provenance/shape gate keeps legacy share/JSON results out.
    """
    if (not isinstance(card, dict) or card.get("verified_miniapp") is not True
            or card.get("format") != "bili_miniapp"):
        return None
    segment = card.get("segment")
    if not isinstance(segment, dict) or segment.get("type") != "json":
        return None
    data = segment.get("data")
    value = data.get("data") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value or len(value) > 65536:
        return None
    try:
        raw = json.loads(value)
        if (raw.get("app") != "com.tencent.miniapp_01"
                or raw.get("meta", {}).get("detail_1", {}).get("appid") != "1109937557"):
            return None
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None
    return segment


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _official_cover(value):
    """Use a public HTTPS cover; upgrade only standard HDslb HTTP URLs."""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "http" and parsed.hostname
                and (parsed.hostname == "hdslb.com" or parsed.hostname.endswith(".hdslb.com"))
                and parsed.port is None and parsed.username is None and parsed.password is None):
            value = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return ""
    return value if share_cards._public_https(value) is not None else ""


async def build_qq_share(host, url, title="", summary="", image=""):
    """Return a verified Bili miniapp or the existing compatible text link.

    Caller-provided metadata is never treated as official Bilibili metadata.
    The original canonical source keeps its comment anchor; the miniapp opens
    the video itself. The caller retains control over sending and receipts.
    """
    fallback = await share_cards.build_share_segment(None, url, title, summary, image)
    canonical = share_cards.canonical_share_url(url)

    def fail(reason):
        return {**fallback, "miniapp_reason": reason, "source_url": canonical}

    if not canonical:
        return fail("unsupported_or_invalid_url")
    parsed = urlsplit(canonical)
    if parsed.hostname != "www.bilibili.com":
        return fail("not_bilibili")
    video_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    is_bvid = _BVID.fullmatch(video_id) is not None
    params = {"bvid": video_id} if is_bvid else {"aid": int(video_id[2:])}
    try:
        bili = host.plugin("astrbot_plugin_bilibili_ai_bot")
        if bili is None:
            return fail("bili_plugin_unavailable")
        response = await asyncio.wait_for(bili._http_get(_VIEW_API, params=params), timeout=12)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - Native metadata errors must not expose response bodies.
        return fail("metadata_error_" + type(exc).__name__)
    if not isinstance(response, tuple) or len(response) != 2:
        return fail("metadata_response_invalid")
    payload = response[0]
    if (not isinstance(payload, dict) or isinstance(payload.get("code"), bool)
            or payload.get("code") != 0 or not isinstance(payload.get("data"), dict)):
        return fail("metadata_response_invalid")
    metadata = payload["data"]
    bvid, aid = metadata.get("bvid"), metadata.get("aid")
    if (not isinstance(bvid, str) or _BVID.fullmatch(bvid) is None
            or not _positive_integer(aid)
            or (is_bvid and bvid != video_id)
            or (not is_bvid and aid != params["aid"])):
        return fail("metadata_target_mismatch")
    official_title = metadata.get("title")
    cover = _official_cover(metadata.get("pic"))
    if not isinstance(official_title, str) or not official_title.strip() or not cover:
        return fail("metadata_title_or_cover_invalid")
    target = urlunsplit(("https", "www.bilibili.com", "/video/" + bvid + "/", parsed.query, ""))
    try:
        from .bili_miniapp import build_bili_miniapp

        platform = host.context.get_platform_inst("snowluma-qq")
        if platform is None:
            return fail("qq_platform_unavailable")
        client = platform.get_client()
        if client is None:
            return fail("qq_client_unavailable")
        built = await build_bili_miniapp(
            client, target, title=official_title.strip(), image=cover,
            description=summary if isinstance(summary, str) else "")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - Signed factory errors must not expose card tokens.
        return fail("miniapp_error_" + type(exc).__name__)
    if not isinstance(built, dict):
        return fail("miniapp_response_invalid")
    segment = built.get("segment")
    if (not isinstance(segment, dict) or segment.get("type") != "json"
            or not isinstance(segment.get("data"), dict)):
        # Factory diagnostics are local constants, but constrain their syntax
        # before allowing a failed result into persisted receipt metadata.
        reason = built.get("reason")
        return fail(reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,100}", reason)
                    else "miniapp_not_verified")
    return {"segment": segment, "format": "bili_miniapp",
            "reason": "official_bili_miniapp_verified", "verified_miniapp": True,
            "source_url": canonical}
