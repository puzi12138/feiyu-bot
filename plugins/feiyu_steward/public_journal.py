"""Keep public-post journals private and preserve each source's evidence form."""

import asyncio
import json
import re
import time
from pathlib import Path

from .admin_info import owner_private as _owner_private
from .heybox import heybox_post_url
from .heybox_journal_history import comment_receipts
from .heybox_journal_store import HeyboxJournalStore
from .bili_journal_gate import notify_new_committed_note

QQBOT_ROOT = Path(__file__).resolve().parents[4]
_ACTIONS = {"recent", "history", "review", "read", "source", "journal", "inbox", "status"}
_SOURCES = {"heybox", "bilibili", "all"}
_MAX_ARGUMENTS = 180_000
_READING_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class _InvalidArguments(ValueError):
    """Mark an invalid bounded tool request without retaining its contents."""


def _keys(arguments, allowed, required=()):
    if set(arguments) - set(allowed) or not set(required) <= set(arguments):
        raise _InvalidArguments


def _integer(value, low=0, high=30_000):
    if type(value) is not int or not low <= value <= high:
        raise _InvalidArguments
    return value


def _reading_id(arguments):
    value = arguments.get("readingId")
    if not isinstance(value, str) or not _READING_ID.fullmatch(value):
        raise _InvalidArguments
    return value


def _note(arguments):
    value = arguments.get("json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise _InvalidArguments from None
    if not isinstance(value, dict):
        raise _InvalidArguments
    return value


def _page(arguments):
    _keys(arguments, {"limit", "offset"})
    return (
        _integer(arguments.get("limit", 10), 1, 20),
        _integer(arguments.get("offset", 0), 0, 1_000_000),
    )


class PublicJournals:
    """Offer owner-only access to typed Bilibili and Heybox local journals."""

    async def _heybox_journal_call(self, method, *arguments):
        """Run a fixed local store operation off the event loop."""
        store = HeyboxJournalStore(QQBOT_ROOT)
        return await asyncio.to_thread(getattr(store, method), *arguments)

    async def capture_heybox_reading(self, doc):
        """Capture a successful host reader result without generating a note.

        Only callers inside the host provide ``doc``. This method is never a
        model tool, accepts no paths, sends nothing, and does not call a model.
        """
        try:
            return await self._heybox_journal_call("store_reading", doc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - Local errors must not expose source text or runtime internals.
            return {"ok": False, "reason": "heybox_journal_storage_unavailable"}

    def _cached_heybox_reading(self, url, images):
        """Reuse only a recent real body receipt, never a feed or public summary."""
        cache = getattr(self, "state", {}).get("public_content_cache", {})
        if not isinstance(cache, dict):
            return None
        matches = []
        now = time.time()
        for row in list(cache.values())[-12:]:
            if not isinstance(row, dict):
                continue
            at, doc = row.get("at"), row.get("result")
            if (
                isinstance(at, bool)
                or not isinstance(at, (int, float))
                or not 0 <= now - at < 60
                or not isinstance(doc, dict)
                or doc.get("ok") is not True
                or doc.get("source") != "heybox"
                or doc.get("url") != url
                or not isinstance(doc.get("read_basis"), list)
                or "post_body" not in doc["read_basis"]
                or "feed_excerpt" in doc["read_basis"]
            ):
                continue
            if images and "image_vision" not in doc["read_basis"]:
                continue
            matches.append((at, doc))
        return max(matches, key=lambda row: row[0])[1] if matches else None

    async def _heybox_journal(self, action, arguments):
        if action == "status":
            _keys(arguments, set())
            return await self._heybox_journal_call("status")
        if action in {"recent", "history"}:
            _keys(arguments, {"limit", "offset"})
            limit = _integer(arguments.get("limit", 3), 1, 3)
            offset = _integer(arguments.get("offset", 0), 0, 1_000_000)
            rows = await self._heybox_journal_call(
                "candidates", limit, offset, "oldest" if action == "history" else "recent",
            )
            if rows.get("ok") is not True:
                return rows
            items = rows.get("items", [])
            receipts = await asyncio.to_thread(
                comment_receipts, QQBOT_ROOT,
                [row["url"] for row in items if isinstance(row, dict) and "url" in row],
            )
            return {**rows, "items": [
                {**row, "comment_history": receipts.get(row["url"], {})} for row in items
            ]}
        if action == "review":
            _keys(arguments, {"readingId", "decision", "reason", "defer_hours"},
                  {"readingId", "decision"})
            decision, reason = arguments["decision"], arguments.get("reason", "")
            if (
                not isinstance(decision, str) or decision not in {"skip", "defer"}
                or not isinstance(reason, str) or len(reason) > 500
            ):
                raise _InvalidArguments
            return await self._heybox_journal_call(
                "review", _reading_id(arguments), decision, reason,
                _integer(arguments.get("defer_hours", 24), 1, 720),
            )
        if action == "read":
            _keys(arguments, {"url", "images"}, {"url"})
            raw_url, images = arguments["url"], arguments.get("images", False)
            url = heybox_post_url(raw_url)
            if not url or type(images) is not bool:
                raise _InvalidArguments
            doc = self._cached_heybox_reading(url, images)
            if doc is None:
                doc = await self.read_public_content(url, images=images)
            if not isinstance(doc, dict) or doc.get("ok") is not True:
                return {"ok": False, "reason": "actual_heybox_reading_unavailable"}
            if doc.get("url") != url:
                return {"ok": False, "reason": "heybox_reading_target_mismatch"}
            return await self.capture_heybox_reading(doc)
        if action == "source":
            _keys(arguments, {"readingId", "offset"}, {"readingId"})
            return await self._heybox_journal_call(
                "source", _reading_id(arguments),
                _integer(arguments.get("offset", 0)),
            )
        if action == "journal":
            _keys(arguments, {"readingId", "json"}, {"readingId", "json"})
            return await self._heybox_journal_call(
                "journal", _reading_id(arguments), _note(arguments),
            )
        if action == "inbox":
            limit, offset = _page(arguments)
            return await self._heybox_journal_call("inbox", limit, offset)
        raise _InvalidArguments

    async def _bili_journal(self, action, arguments, *, tool_event):
        """Retain the original video IDs, timed evidence and inbox pagination."""
        if action == "status":
            _keys(arguments, set())
        elif action == "recent":
            _keys(arguments, {"limit"})
            _integer(arguments.get("limit", 3), 1, 3)
            action = "local_recent"
        elif action == "read":
            _keys(arguments, {"video", "page"}, {"video"})
            video = arguments["video"]
            if not isinstance(video, str) or not 1 <= len(video) <= 4096:
                raise _InvalidArguments
            if "page" in arguments:
                _integer(arguments["page"], 1, 100)
        elif action == "source":
            _keys(arguments, {"readingId", "offset"}, {"readingId"})
            _reading_id(arguments)
            if "offset" in arguments:
                _integer(arguments["offset"])
        elif action == "journal":
            _keys(arguments, {"readingId", "json", "public_reason_quote"}, {"readingId", "json"})
            _reading_id(arguments)
            arguments = {key: value for key, value in arguments.items() if key != "public_reason_quote"}
            arguments = {**arguments, "json": json.dumps(_note(arguments), ensure_ascii=False)}
        elif action == "inbox":
            _keys(arguments, {"action", "volume", "snapshot"})
            if not arguments:
                arguments = {"action": "index"}
            if arguments.get("action") not in {"index", "volume"}:
                raise _InvalidArguments
            if arguments["action"] == "index":
                _keys(arguments, {"action"})
            else:
                _keys(arguments, {"action", "volume", "snapshot"}, {"volume", "snapshot"})
                _integer(arguments["volume"], 0, 1_000_000)
                snapshot = arguments["snapshot"]
                if not isinstance(snapshot, str) or not 1 <= len(snapshot) <= 128:
                    raise _InvalidArguments
        else:
            raise _InvalidArguments
        return await self.humanism_call(action, arguments, tool_event=tool_event)

    async def public_journal_tool(
        self, event, action="status", source="all", arguments_json="{}",
    ):
        """Read and write local evidence journals only in the owner's private session.

        Heybox evidence cites exact source paragraphs, never video timestamps.
        Bilibili keeps its original video reading and evidence format. This
        Optional Bili public_reason_quote is matched verbatim to its public
        reading; it is never copied from a private note or sent upstream.
        This entry point cannot publish arbitrary source prose, change permissions,
        edit personality notes or access an arbitrary file.
        """
        if not _owner_private(event):
            return json.dumps({"error": "OWNER_PRIVATE_ONLY"})
        try:
            if (
                not isinstance(action, str) or action not in _ACTIONS
                or not isinstance(source, str) or source not in _SOURCES
                or not isinstance(arguments_json, str)
                or len(arguments_json.encode("utf-8")) > _MAX_ARGUMENTS
            ):
                raise _InvalidArguments
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise _InvalidArguments
            if source == "all":
                if action == "status":
                    _keys(arguments, set())
                elif action == "inbox":
                    _page(arguments)
                else:
                    raise _InvalidArguments
                heybox = await self._heybox_journal(action, arguments)
                bili = await self._bili_journal(
                    action, {"action": "index"} if action == "inbox" else {}, tool_event=event,
                )
                result = {
                    "action": action, "source": "all", "private_local_only": True,
                    "sources": {
                        "heybox": {"source": "heybox", "result": heybox},
                        "bilibili": {"source": "bilibili", "result": bili},
                    },
                }
                if action == "inbox":
                    result["limitation"] = (
                        "Heybox notes use limit/offset. Bilibili returns its native "
                        "index; read source=bilibili inbox volumes with the same "
                        "snapshot to cover the archive. This is not the full archive."
                    )
            else:
                result = await (
                    self._heybox_journal(action, arguments)
                    if source == "heybox" else self._bili_journal(action, arguments, tool_event=event)
                )
                if source == "bilibili" and action == "journal":
                    await notify_new_committed_note(
                        self, event, result, arguments.get("readingId"),
                        source_quote=arguments.get("public_reason_quote"))
                result = {"action": action, "source": source, "result": result,
                          "private_local_only": True}
            return json.dumps(result, ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return json.dumps({"error": "INVALID_JOURNAL_ARGUMENTS"})
        except Exception:  # noqa: BLE001 - Tool boundary returns no platform bodies or private paths.
            return json.dumps({"error": "PUBLIC_JOURNAL_UNAVAILABLE"})
