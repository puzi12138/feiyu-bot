"""Build readable source links while QQ Ark cards are incompatible.

No native card factory, message send or external URL fetch is performed.
"""

import ipaddress
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

_BILI_ID = r"(?:BV[A-Za-z0-9]{10}|av[1-9][0-9]*)"
_BILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_HEYBOX_HOSTS = {"xiaoheihe.cn", "example.invalid"}


def _public_https(value):
    """Validate syntax without DNS/network I/O, excluding local destinations."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if value != value.strip() or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443}):
            return None
    except ValueError:
        return None
    if host.endswith(".") or "%" in host:
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        if (not re.fullmatch(r"[A-Za-z0-9.-]+", host) or "." not in host
                or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".invalid", ".onion", ".test", ".arpa"))
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in host.split("."))):
            return None
        # Numeric alternative forms such as 2130706433 or 127.1 are local-IP
        # ambiguities, not normal public DNS names.
        if re.fullmatch(r"[0-9.]+", host) or not re.match(r"[A-Za-z]", host.rsplit(".", 1)[-1]):
            return None
    return parsed


def canonical_share_url(value):
    """Return an approved canonical destination, or an empty string.

    Tracking queries are removed. Bilibili page and reply destinations are
    preserved; short links are intentionally not resolved or fetched here.
    """
    parsed = _public_https(value)
    if parsed is None:
        return ""
    host, path = parsed.hostname.lower(), parsed.path
    if host in _BILI_HOSTS and re.fullmatch(r"/video/" + _BILI_ID + r"/?", path):
        query = parse_qs(parsed.query)
        page = query.get("p", [])
        page_query = (urlencode({"p": page[0]}) if len(page) == 1
                      and re.fullmatch(r"[1-9][0-9]{0,5}", page[0]) else "")
        fragment = parsed.fragment if re.fullmatch(r"reply[1-9][0-9]*", parsed.fragment) else ""
        return urlunsplit(("https", "www.bilibili.com", path.rstrip("/") + "/", page_query, fragment))
    if host in _HEYBOX_HOSTS and re.fullmatch(r"/app/bbs/link/[A-Za-z0-9_-]{1,128}/?", path):
        return "https://example.invalid/configure-endpoint" + path.rstrip("/")
    if host == "user.qzone.qq.com" and re.fullmatch(r"/[1-9][0-9]{4,11}/mood/[A-Za-z0-9_-]{1,128}/?", path):
        return "https://user.qzone.qq.com" + path.rstrip("/")
    if host in {"hltv.org", "www.hltv.org"} and re.fullmatch(r"/news/[1-9][0-9]*/[A-Za-z0-9_-]+/?", path):
        return "https://www.hltv.org" + path.rstrip("/")
    return ""


async def build_share_segment(client, url, title="", summary="", image=""):
    """Return an ordinary text link without invoking a card/send API.

    The existing signature is retained for all consumers. QQ clients rejected
    the legacy share Ark as an old sender version, so no share/JSON card can
    be emitted even when a caller provides a native client or metadata.
    """
    canonical = canonical_share_url(url)
    if not canonical:
        return {"segment": None, "format": "none", "reason": "unsupported_or_invalid_url"}
    return {"segment": {"type": "text", "data": {"text": "\n" + canonical}},
            "format": "plain_link", "reason": "qq_card_rendering_incompatible"}
