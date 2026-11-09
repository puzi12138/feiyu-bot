"""Limit automatic Heybox-to-owner sharing to a verified off-peak period."""

import re
import time
from types import SimpleNamespace

from .bili_journal_gate import _explicit_share_text
from .heybox import heybox_post_url
from .peak_policy import manual_owner_event
from .token_pricing import _peak


def heybox_owner_off_peak():
    """Permit automatic sharing only when the verified tariff says off-peak.

    Returns:
        True only for a verified off-peak instant; unknown calendars fail closed.
    """
    try:
        return _peak(time.time()) is False
    except Exception:  # noqa: BLE001 - An unavailable calendar must fail closed.
        return False


def manual_heybox_share_request(event, source_url):
    """Recognize a current, verified owner's explicit request to share a post.

    Args:
        event: The current inbound event, including the transport identity.
        source_url: The already selected public Heybox URL.

    Returns:
        True only for a matching owner request without deferred-sharing wording.
    """
    canonical = heybox_post_url(source_url)
    if not canonical:
        return False
    try:
        original = getattr(event, "get_extra", lambda _key: None)("feiyu_owner_original_text")
        text = str(original or getattr(event, "message_str", "") or "").strip()
        if (not manual_owner_event(event)
                or not _explicit_share_text(SimpleNamespace(message_str=text))
                or re.search(r"以后|下次|每当|每天|经常|闲时|空闲时|有空时|低峰时|非高峰时|"
                             r"价格低时|便宜时|高峰过后|峰时过后|等(?:到|价格|低峰|闲|空)", text)):
            return False
        urls = re.findall(r"https://[^\s<>，。！？；、“”‘’]+", text)
        if urls:
            mentioned = [heybox_post_url(url.rstrip(",.!?;:)]}")) for url in urls]
            return bool(mentioned and all(value == canonical for value in mentioned))
        return True
    except Exception:  # noqa: BLE001 - Unverified event metadata never grants an exception.
        return False
