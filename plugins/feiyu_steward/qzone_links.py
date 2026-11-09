"""Keep Qzone source links whole without rewriting the bot's public quote.

Qzone's plain-text link detector can stop before a ``#reply`` fragment. Use
the public video/post entry for this destination; QQ chat may still use the
original comment permalink. This module neither fetches URLs nor creates
QQ mini-app payloads or undocumented Qzone rich-text markup.
"""

import html
import ipaddress
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .heybox import heybox_post_url


def qzone_source_url(value: str) -> str:
    """Normalize known public source routes for a new Qzone publication.

    Args:
        value: A previously authorized public source URL, not an entire body.

    Returns:
        A canonical video, article, or post URL. Known routes lose tracking
        queries and comment fragments. A single valid positive Bilibili ``p``
        is retained because it identifies a video part. Unknown public URLs
        retain their query and fragment because those may identify content.
        Invalid or local URLs return an empty string. This is presentation
        normalization, not permission to fetch or publish an arbitrary URL.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        return ""
    value = html.unescape(value.strip())
    if not value or re.search(r"[\s\\<>\x00-\x1f\x7f]", value):
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443 if parsed.scheme == "https" else 80)
            or "." not in host
            or host.endswith((".local", ".localhost", ".internal", ".lan"))
        ):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
    except ValueError:
        return ""

    if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        video = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10}|av[1-9][0-9]*)/?", parsed.path)
        if video:
            try:
                parts = parse_qs(
                    parsed.query, keep_blank_values=True, max_num_fields=100
                ).get("p", [])
            except ValueError:
                parts = []
            part_query = ""
            if (
                len(parts) == 1
                and re.fullmatch(r"[0-9]+", parts[0])
                and int(parts[0]) > 0
            ):
                part_query = "?" + urlencode({"p": int(parts[0])})
            return "https://www.bilibili.com/video/" + video[1] + part_query
        article = re.fullmatch(r"/(read/cv[1-9][0-9]*|opus/[1-9][0-9]*)/?", parsed.path)
        if article:
            return "https://www.bilibili.com/" + article[1]
        if parsed.path.rstrip("/") == "/read/mobile":
            try:
                identifiers = parse_qs(
                    parsed.query, keep_blank_values=True, max_num_fields=100
                ).get("id", [])
            except ValueError:
                identifiers = []
            if len(identifiers) == 1 and re.fullmatch(r"[1-9][0-9]*", identifiers[0]):
                return "https://www.bilibili.com/read/cv" + identifiers[0]

    if host in {"b23.tv", "bili2233.cn"} and re.fullmatch(
        r"/[A-Za-z0-9_-]{1,80}/?", parsed.path
    ):
        return "https://" + host + parsed.path.rstrip("/")
    if host == "t.bilibili.com" and re.fullmatch(r"/[1-9][0-9]*/?", parsed.path):
        return "https://t.bilibili.com" + parsed.path.rstrip("/")

    # Reuse the installed reader's exact official-share/link-id rules. In
    # particular, an API share URL needs link_id before its query is removed.
    heybox = heybox_post_url(value)
    if heybox:
        return heybox
    if host in {"tieba.baidu.com", "www.tieba.baidu.com"} and re.fullmatch(
        r"/p/[1-9][0-9]*/?", parsed.path
    ):
        return "https://tieba.baidu.com" + parsed.path.rstrip("/")
    if host in {"www.hltv.org", "hltv.org"} and re.fullmatch(
        r"/(?:news|matches|team|player)/[1-9][0-9]*/[A-Za-z0-9_-]+/?", parsed.path
    ):
        return "https://www.hltv.org" + parsed.path.rstrip("/")
    if host == "user.qzone.qq.com" and re.fullmatch(
        r"/[1-9][0-9]*/mood/[A-Za-z0-9_-]{1,128}/?", parsed.path
    ):
        return "https://user.qzone.qq.com" + parsed.path.rstrip("/")
    return value


def qzone_comment_body(text: str, source_url: str) -> str:
    """Append a destination-specific source while preserving the exact quote.

    Args:
        text: The bot's already verified public comment, kept byte-for-byte.
        source_url: Its original public source/comment URL.

    Returns:
        The full comment followed by the Qzone source URL on a separate line.

    Raises:
        TypeError: If the comment is not text.
        ValueError: If the source is not a valid public URL.
    """
    if not isinstance(text, str):
        raise TypeError("comment_text_required")
    source = qzone_source_url(source_url)
    if not source:
        raise ValueError("invalid_qzone_source_url")
    return text + "\n\n" + source
