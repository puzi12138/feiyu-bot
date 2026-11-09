"""Classify confirmed upload failures; retry only transient transport errors."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping


def _failure_wording(exc: Exception) -> str:
    # aiocqhttp versions expose the OneBot envelope in .result or .info.
    # A generic API timeout is ambiguous: the message may already be delivered.
    info = getattr(exc, "result", None)
    if not isinstance(info, Mapping):
        info = getattr(exc, "info", None)
    if not isinstance(info, Mapping) or info.get("retcode") != 100:
        return ""
    return str(info.get("wording") or info.get("message") or "").lower()


def is_confirmed_upload_failure(exc: Exception) -> bool:
    """Confirm failure before message submission, including permanent errors.

    This classifies delivery outcome, not retry safety. Unstructured errors and
    API/WebSocket timeouts do not confirm that a message remained unsent.
    """
    wording = _failure_wording(exc)
    return bool(re.search(r"(?:highway[\]\s:]*upload(?: transport)?|(?:image|media|richmedia) upload) failed|图片上传失败|富媒体上传失败", wording))


def is_upload_failure(exc: Exception) -> bool:
    """Identify confirmed upload transport errors eligible for bounded retries."""
    if not is_confirmed_upload_failure(exc):
        return False
    wording = _failure_wording(exc)
    if any(word in wording for word in ("禁言", "权限", "风控", "permission", "muted", "risk control")):
        return False
    transport = re.search(r"connect(?:ion)?[ :\w-]*time(?:d ?out|out)|etimedout|econnreset|econnrefused|ehostunreach|enetunreach|socket hang up", wording)
    return bool(transport)


async def send_with_retry(send_callable, *, sleep=asyncio.sleep):
    """Rebuild and send the current message at most three times."""
    for attempt in range(3):
        try:
            return await send_callable()
        except Exception as exc:
            if attempt == 2 or not is_upload_failure(exc):
                raise
            await sleep((1, 3)[attempt])
