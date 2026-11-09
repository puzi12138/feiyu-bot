"""Read the bot's confirmed public comments as data, without authorizing a share.

Only publication receipts are accepted. This catalog has no private-chat,
model, public-write, or QQ-send path. Callers must inspect the source health
metadata when an empty or partial result is returned, and persist state.
"""

# External sources fail independently. Record only safe exception types.
# ruff: noqa: BLE001

import asyncio
import math
import re
import time

from .bili_notices import confirmed_bili_receipts
from .policy import PUBLIC_COMMENT_SENSITIVE
from .public_comment_guard import internal_comment_reason

_OWNER_PREFIX = re.compile(r"^@(?:一团普子|0)(?:\r\n|[ \t\r\n\u3000]|$)")
_POST_HEX = re.compile(r"[0-9a-fA-F]{8,64}\Z")
_HEYBOX_SQL = (
    "SELECT post_id,external_comment_id,content,confirmed_at,root_comment_id,parent_comment_id "
    "FROM outgoing_replies WHERE profile_id=? AND status='sent' AND confirmed_at>=? "
    "AND COALESCE(root_comment_id,'')='' AND COALESCE(parent_comment_id,'')='' "
    "ORDER BY confirmed_at DESC LIMIT 100"
)


def _numeric_id(value):
    if isinstance(value, bool):
        return ""
    text = str(value or "")
    return text if text.isascii() and text.isdigit() and int(text) > 0 else ""


def _post_id(value):
    text = str(value or "")
    return _numeric_id(value) or (text if _POST_HEX.fullmatch(text) else "")


def _public_text(value, *, bili=False):
    if not isinstance(value, str):
        return ""
    text = value
    if bili:
        # Remove only the owner's fixed leading mention, never another name
        # or an occurrence in the comment body. Keep the public quote intact.
        text = _OWNER_PREFIX.sub("", text, count=1)
    if (not 1 <= len(text) <= 2000 or not text.strip()
            or PUBLIC_COMMENT_SENSITIVE.search(text) or internal_comment_reason(text)):
        return ""
    return text


def _bili_receipts(state, since):
    """Use the existing receipt validator without its oldest-100 truncation."""
    owners = state.get("bili_owner_comments", {})
    if not isinstance(owners, dict):
        raise TypeError("invalid_public_comment_ledger")
    output = {}
    for aid, original in owners.items():
        batch = {"bili_owner_comments": {aid: original}}
        for row in confirmed_bili_receipts(batch, since=since, limit=100):
            # The validator trims for validation; retain this exact receipt's
            # original quote spacing rather than changing its public wording.
            output[row["key"]] = {**row, "text": original["text"]}
    return output.values()


class OwnCommentCatalog:
    async def collect_own_comments(self, since=0, limit=30) -> list[dict]:
        """Return the newest confirmed public quotes, at most 100 per call.

        Requires ``self.state`` and ``self.plugin(name)``. Source failures are
        isolated but recorded as ``ok: False`` in ``comment_share_sources``;
        absence of results must never be treated as a healthy empty source
        without checking that metadata. This method does not save or send.
        """
        if isinstance(since, bool) or isinstance(limit, bool):
            raise TypeError("invalid_catalog_bounds")
        since = float(since)
        limit = max(0, min(int(limit), 100))
        if not math.isfinite(since) or since < 0:
            raise ValueError("invalid_catalog_since")
        diagnostics = self.state.setdefault("comment_share_sources", {})
        output = {}
        try:
            rows = []
            for row in _bili_receipts(self.state, since):
                text = _public_text(row["text"], bili=True)
                aid, rpid = _numeric_id(row["aid"]), _numeric_id(row["rpid"])
                at = float(row["published_at"])
                if not text or not aid or not rpid or not math.isfinite(at) or at <= 0:
                    continue
                rows.append({"key": row["key"], "source": "bilibili", "text": text,
                             "url": f"https://www.bilibili.com/video/av{aid}/#reply{rpid}",
                             "published_at": at})
            output.update((row["key"], row) for row in rows)
            diagnostics["bilibili"] = {"ok": True, "count": len(rows), "checked_at": time.time()}
        except Exception as error:
            diagnostics["bilibili"] = {"ok": False, "reason": type(error).__name__,
                                       "checked_at": time.time()}

        try:
            plugin = self.plugin("astrbot_plugin_xiaoheihe_adapter")
            if plugin is None:
                diagnostics["heybox"] = {"ok": False, "reason": "plugin_unavailable",
                                         "checked_at": time.time()}
            else:
                async def fetch():
                    await plugin.runtime.ensure_started()
                    return await plugin.runtime.repository.db.fetchall(_HEYBOX_SQL, ("default", since))

                rows = []
                for original in await asyncio.wait_for(fetch(), timeout=15):
                    row = dict(original)
                    post, receipt = _post_id(row.get("post_id")), _numeric_id(row.get("external_comment_id"))
                    text = _public_text(row.get("content"))
                    try:
                        at = float(row.get("confirmed_at"))
                    except (ValueError, TypeError, OverflowError):
                        continue
                    if (not post or not receipt or not text or not math.isfinite(at)
                            or at <= 0 or at < since or isinstance(row.get("confirmed_at"), bool)
                            or row.get("status", "sent") != "sent"
                            or row.get("root_comment_id") or row.get("parent_comment_id")):
                        continue
                    rows.append({"key": "heybox:" + receipt, "source": "heybox", "text": text,
                                 "url": "https://example.invalid/configure-endpoint" + post,
                                 "published_at": at})
                output.update((row["key"], row) for row in rows)
                diagnostics["heybox"] = {"ok": True, "count": len(rows), "checked_at": time.time()}
        except Exception as error:
            diagnostics["heybox"] = {"ok": False, "reason": type(error).__name__,
                                     "checked_at": time.time()}
        return sorted(output.values(), key=lambda row: (row["published_at"], row["key"]), reverse=True)[:limit]
