"""Owner-authorized forwarding of real, bounded local cover artifacts."""

import asyncio
import hashlib
import re
import shutil
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from astrbot.api import logger

from .cover import clean_song_scope, explicit_cover_mode, normalize_cover_mode
from .peak_policy import PeakTariffPaused, QQChatPaused, peak_active
from .qq_chat_pause import qq_chat_paused

OWNER = "0"
GROUP = "0"
NATIVE_OUTPUTS = Path("D:/QQBot/data/astrbot/plugin_data/astrbot_plugin_ai_cover/outputs")
PREVIEWS = Path("D:/QQBot/data/cover-previews/guess-20261023")
MAX_ITEMS = 30
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
DEDUP_SECONDS = 300


def _digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _title_key(text):
    return re.sub(r"[\W_]", "", str(text)).casefold()


class CoverForwarding:
    """Archive generated media; never accept a model-supplied file or URL."""

    def init_cover_archive(self):
        self.cover_archive_dir = (self.root / "cover-archive").resolve()
        self.cover_archive_dir.mkdir(parents=True, exist_ok=True)
        self.cover_forward_lock = asyncio.Lock()
        self.state.setdefault("cover_archive", [])
        receipts = self.state.setdefault("cover_forward_receipts", [])
        for receipt in receipts:
            if receipt.get("status") == "sending":
                receipt["status"] = "unknown"
        if not self.state["cover_archive"]:
            # These two actual verified outputs have known titles and scopes.
            for mode, filename in (("chorus", "荧翻唱-猜不透-副歌.mp3"),
                                   ("full", "荧翻唱-猜不透-整首.mp3")):
                path = PREVIEWS / filename
                if path.is_file():
                    self._remember_cover(path, "猜不透", mode, "LumineV2", "BV18H4y1L7At",
                                         created=path.stat().st_mtime)
        self._prune_cover_archive()
        self.save()

    def _cover_path(self, path):
        if str(path).startswith("file:"):
            uri = urlsplit(str(path))
            if uri.netloc not in {"", "localhost"}:
                raise ValueError("invalid_cover_artifact")
            path = url2pathname(unquote(uri.path))
        path = Path(path).resolve()
        allowed = {NATIVE_OUTPUTS.resolve(), PREVIEWS.resolve(), self.cover_archive_dir}
        if (path.parent not in allowed or path.suffix.lower() != ".mp3"
                or not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES):
            raise ValueError("invalid_cover_artifact")
        return path

    def _prune_cover_archive(self):
        rows = sorted(self.state["cover_archive"], key=lambda row: row.get("created", 0), reverse=True)
        keep, total = [], 0
        for row in rows:
            try:
                path = self._cover_path(row["path"])
                size = path.stat().st_size
            except (KeyError, ValueError, OSError):
                continue
            if len(keep) < MAX_ITEMS and total + size <= MAX_ARCHIVE_BYTES:
                keep.append(row)
                total += size
            elif path.parent == self.cover_archive_dir and re.fullmatch(r"[a-f0-9]{64}\.mp3", path.name):
                path.unlink(missing_ok=True)
        self.state["cover_archive"] = list(reversed(keep))
        self._prune_cover_receipts()

    def _prune_cover_receipts(self):
        # A still-available artifact's uncertain send is a durable barrier.
        # Otherwise many newer successes could evict it and permit a duplicate.
        active = {row.get("sha256") for row in self.state["cover_archive"]}
        receipts = self.state["cover_forward_receipts"]
        protected, seen = [], set()
        for row in reversed(receipts):
            artifact = row.get("artifact")
            if (row.get("status") in {"sending", "unknown"} and artifact in active and artifact not in seen):
                protected.append(row)
                seen.add(artifact)
        protected_ids = {id(row) for row in protected}
        others = [row for row in receipts if id(row) not in protected_ids][-100:]
        keep_ids = protected_ids | {id(row) for row in others}
        self.state["cover_forward_receipts"] = [row for row in receipts if id(row) in keep_ids]

    def _remember_cover(self, path, title="", mode="", model="", source_bvid="", created=None):
        source = self._cover_path(path)
        digest = _digest(source)
        target = self.cover_archive_dir / f"{digest}.mp3"
        if source != target and not target.exists():
            temporary = target.with_suffix(".tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(target)
        existing = next((row for row in self.state["cover_archive"] if row.get("sha256") == digest), None)
        row = existing or {"sha256": digest, "path": str(target), "created": created or time.time()}
        if created is None:
            row["created"] = time.time()
        elif not existing:
            row["created"] = created
        if title:
            row["title"] = str(title).strip()[:180]
        if mode:
            row["mode"] = normalize_cover_mode(mode)
        if model:
            row["model"] = str(model)[:80]
        if re.fullmatch(r"BV[A-Za-z0-9]{10}", str(source_bvid)):
            row["source_bvid"] = source_bvid
        if not existing:
            self.state["cover_archive"].append(row)
        self._prune_cover_archive()
        self.save()
        return {key: row.get(key, "") for key in ("title", "mode", "model", "source_bvid")}

    def remember_cover(self, path, title="", mode="", model="", source_bvid=""):
        """Called only by trusted generation hooks, after a real artifact exists."""
        return self._remember_cover(path, title, mode, model, source_bvid)

    def install_cover_capture(self, plugin):
        """Capture only after the real native request has finished writing media."""
        original = plugin._request_cover

        async def captured(*args, **kwargs):
            result = await original(*args, **kwargs)
            try:
                source_name = kwargs.get("original_name", args[1] if len(args) > 1 else "")
                name = str(source_name).replace("\\", "/").rsplit("/", 1)[-1]
                title = "" if name.casefold() in {"", "song.m4a", "song.mp3", "audio.mp3", "audio.m4a"} else Path(name).stem
                self.remember_cover(result[0], title=title, mode=kwargs.get("cover_mode", "chorus"), model=result[1])
            except Exception as error:  # noqa: BLE001 - archive failures must not break the native plugin's completed delivery
                logger.warning("Feiyu cover archive capture failed (%s)", type(error).__name__)
            return result

        plugin._request_cover = captured

        def restore():
            if plugin._request_cover is captured:
                plugin._request_cover = original

        return restore

    async def forward_cover(self, event, group_id="", song_name="", mode="", dry_run=False):
        """Forward a selected generated cover only to the owner's configured group."""
        session = str(getattr(event, "unified_msg_origin", ""))
        if (str(event.get_sender_id()) != OWNER or session not in
                {f"snowluma-qq:FriendMessage:{OWNER}", f"snowluma-qq:GroupMessage:{GROUP}"}):
            return {"ok": False, "reason": "owner_only"}
        target = str(group_id or GROUP).strip()
        if re.sub(r"\s", "", target) in {"赛博猪圈", "赛博🐷圈", "赛博🐖圈", "赛博猪圈群", "赛博🐷圈群"}:
            target = GROUP
        if target != GROUP:
            return {"ok": False, "reason": "group_not_configured", "configured_group": GROUP}
        original = getattr(event, "get_extra", lambda *_: None)("feiyu_owner_original_text")
        text = str(original or getattr(event, "message_str", ""))
        try:
            scope = explicit_cover_mode(text) or (normalize_cover_mode(mode) if mode else "")
        except ValueError:
            return {"ok": False, "reason": "invalid_cover_mode"}
        title = clean_song_scope(str(song_name)).strip()
        if len(title) > 180 or re.search(r"https?://|[/\\]", title, re.IGNORECASE):
            return {"ok": False, "reason": "invalid_song_name"}
        async with self.cover_forward_lock:
            matches = []
            for row in self.state["cover_archive"]:
                if scope and row.get("mode") != scope:
                    continue
                if title and _title_key(title) not in _title_key(row.get("title", "")):
                    continue
                try:
                    self._cover_path(row["path"])
                except (OSError, KeyError, ValueError):
                    continue
                matches.append(row)
            if not matches:
                return {"ok": False, "reason": "cover_not_found", "message": "没有符合歌曲或范围的现存成品，请先完成翻唱。"}
            chosen = max(enumerate(matches), key=lambda item: (item[1].get("created", 0), item[0]))[1]
            public = {"title": chosen.get("title") or "最近生成的翻唱", "mode": chosen.get("mode") or "unknown", "group_id": GROUP}
            if dry_run:
                return {"ok": True, "dry_run": True, **public}
            request_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
            # An explicit new owner message saying "重发" permits a new attempt,
            # while the same message/tool replay retains the same dedup key.
            retry = request_id if request_id and re.search(r"重发|再发|重新发|再转|重新转", text) else ""
            receipt_key = hashlib.sha256(f"{GROUP}:{chosen['sha256']}:{retry}".encode()).hexdigest()
            now = time.time()
            recent = next((row for row in reversed(self.state["cover_forward_receipts"])
                           if request_id and row.get("request_id") == request_id
                           and row.get("artifact") == chosen["sha256"]), None)
            recent = recent or next((row for row in reversed(self.state["cover_forward_receipts"])
                           if not retry and row.get("artifact") == chosen["sha256"]
                           and row.get("status") in {"sending", "unknown"}), None)
            recent = recent or next((row for row in reversed(self.state["cover_forward_receipts"])
                           if row.get("key") == receipt_key and
                           (row.get("status") in {"sending", "unknown"} or now - row.get("at", 0) < DEDUP_SECONDS)), None)
            if recent:
                return {"ok": recent.get("status") == "sent", "reason": "recent_delivery",
                        "status": recent.get("status"),
                        **({"message_id": recent["message_id"]} if recent.get("message_id") else {}), **public}
            if qq_chat_paused():
                return {"ok": False, "reason": "qq_chat_paused", "status": "blocked", **public}
            adapter = self.context.get_platform_inst("snowluma-qq")
            if not adapter:
                return {"ok": False, "reason": "qq_unavailable", **public}
            client = adapter.get_client()
            try:
                info = await asyncio.wait_for(client.call_action("get_group_info", group_id=int(GROUP)), 20)
                if not isinstance(info, dict) or str(info.get("group_id")) != GROUP:
                    return {"ok": False, "reason": "group_unavailable", **public}
            except Exception:  # noqa: BLE001 - adapter errors must return a safe failure without exposing transport credentials
                return {"ok": False, "reason": "group_unavailable", **public}
            manual_paused = qq_chat_paused()
            if manual_paused or peak_active():
                return {"ok": False, "reason": "qq_chat_paused" if manual_paused else "peak_tariff",
                        "status": "blocked", **public}
            receipt = {"key": receipt_key, "at": now, "group_id": GROUP,
                       "artifact": chosen["sha256"], "request_id": request_id, "status": "sending"}
            self.state["cover_forward_receipts"].append(receipt)
            self._prune_cover_receipts()
            self.save()
            try:
                result = await asyncio.wait_for(client.call_action(
                    "send_group_msg", group_id=int(GROUP),
                    message=[{"type": "record", "data": {"file": Path(chosen["path"]).as_uri()}}]), 60)
                message_id = result.get("message_id") if isinstance(result, dict) else None
                receipt["status"] = "sent" if message_id else "unknown"
                if message_id:
                    receipt["message_id"] = str(message_id)
            except QQChatPaused:
                receipt.update(status="blocked", reason="qq_chat_paused")
                self.save()
                return {"ok": False, "reason": "qq_chat_paused", "status": "blocked", **public}
            except PeakTariffPaused:
                receipt.update(status="blocked", reason="peak_tariff")
                self.save()
                return {"ok": False, "reason": "peak_tariff", "status": "blocked", **public}
            except BaseException as error:
                receipt["status"] = "unknown"
                self._prune_cover_receipts()
                self.save()
                if isinstance(error, asyncio.CancelledError):
                    raise
                return {"ok": False, "status": "unknown", "reason": "send_result_unknown", **public}
            self._prune_cover_receipts()
            self.save()
            return {"ok": receipt["status"] == "sent", "status": receipt["status"],
                    **({"message_id": receipt["message_id"]} if receipt.get("message_id") else {}), **public}
