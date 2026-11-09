"""Read confirmed bot comment receipts for the shared private notice queue.

Browsing/evaluation logs are deliberately not accepted as publication evidence.
This module has no public-write or QQ-send path and never changes the ledger.
"""

import asyncio
import re
from datetime import datetime, timezone


BVID = re.compile(r"BV[0-9A-Za-z]{10}\Z")


def _identifier(value):
    """Accept only positive integer platform IDs, never paths or URLs."""
    text = str(value or "")
    return text if text.isascii() and text.isdigit() and int(text) > 0 else ""


def _timestamp(row):
    value = row.get("confirmed_at") or row.get("created")
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        parsed = datetime.fromisoformat(str(value))
        # Old ledgers use ISO timestamps with +08:00. Reject ambiguous records.
        if parsed.tzinfo is None:
            return 0.0
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def confirmed_bili_receipts(state, *, since=0, limit=100):
    """Return bounded metadata from API-confirmed first comments and followups.

    Both ledgers are written by the bot's scoped Bili adapter only after
    reply/add returned code=0 and a real rpid. ``unknown`` is never retried.
    Followup records from older versions without their sent text are omitted.
    """
    rows = []
    owners = state.get("bili_owner_comments", {})
    for key, original in owners.items() if isinstance(owners, dict) else ():
        if not isinstance(original, dict):
            continue
        rows.append((key, original, False))
    replies = state.get("bili_conversation_replies", {})
    for key, original in replies.items() if isinstance(replies, dict) else ():
        if not isinstance(original, dict):
            continue
        rows.append((original.get("oid"), original, True))
    receipts = {}
    for aid_value, row, followup in rows:
        aid, rpid = _identifier(aid_value), _identifier(row.get("rpid"))
        at = _timestamp(row)
        text = row.get("text")
        # Do not report a successful content-less boolean or a mere intent.
        if (row.get("status") != "sent" or row.get("code") != 0
                or not aid or not rpid or not at or at < float(since)
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000):
            continue
        if followup:
            root = _identifier(row.get("root"))
            owner = owners.get(aid, {}) if isinstance(owners, dict) else {}
            if not root or str(owner.get("rpid")) != root or owner.get("status") != "sent":
                continue
        else:
            root = rpid
        receipts[rpid] = {
            "key": f"bili:{rpid}", "source": "bilibili", "kind": "reply" if followup else "comment",
            "aid": aid, "rpid": rpid, "root_rpid": root,
            "text": text.strip(), "published_at": at,
        }
    return sorted(receipts.values(), key=lambda row: (row["published_at"], row["rpid"]))[:max(0, min(int(limit), 100))]


async def collect_bili_notices(state, bili, *, since=0, limit=100):
    """Resolve exact aid→BV metadata through the installed read-only client.

    Missing metadata leaves a receipt pending for another collection pass; it
    never changes a public comment or invents a successful delivery. The caller
    owns durable QQ deduplication, batching, quiet hours and send confirmation.
    """
    if bili is None:
        return []
    output, metadata = [], {}
    for receipt in confirmed_bili_receipts(state, since=since, limit=limit):
        aid = receipt["aid"]
        if aid not in metadata:
            try:
                payload, _ = await asyncio.wait_for(
                    bili._http_get("https://api.bilibili.com/x/web-interface/view", params={"aid": aid}),
                    timeout=12,
                )
                info = payload.get("data") or {}
                if (payload.get("code") != 0 or not BVID.fullmatch(str(info.get("bvid", "")))
                        or _identifier(info.get("aid")) != aid):
                    metadata[aid] = None
                else:
                    metadata[aid] = info
            except (Exception,):
                metadata[aid] = None
        info = metadata[aid]
        if not info:
            continue
        bvid = info["bvid"]
        url = f"https://www.bilibili.com/video/{bvid}/#reply{receipt['rpid']}"
        title = str(info.get("title") or "").replace("\r", " ").replace("\n", " ")[:160]
        stamp = datetime.fromtimestamp(receipt["published_at"], timezone.utc).astimezone(
            timezone(__import__("datetime").timedelta(hours=8))
        ).isoformat()
        verb = "已回复评论" if receipt["kind"] == "reply" else "已发表评论并艾特你"
        message = f"B站{verb}\n{title}\n{url}\n{receipt['text']}\n评论ID：{receipt['rpid']}"
        output.append({**receipt, "bvid": bvid, "url": url, "title": title,
                       "published_iso": stamp, "message": message})
    return output
