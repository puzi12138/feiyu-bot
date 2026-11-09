"""Isolated notices for one platform-rejected Xiaoheihe comment.

This policy deliberately does not import or modify the F060 incident ledger.
A record is a durable, no-content *item* record:
one rejected event/outgoing pair is isolated and can be reported to the owner
without itself pausing the feed or comment feature. The production caller
continues only after a native notice receipt; other errors remain protected.

The record contains only fixed reason/stage values and numeric event IDs.  It
never stores comment text, URLs, exception text, credentials, or response
bodies.  A ``pending`` notification is an intent, not a receipt.  A process
that recovers an abandoned pending intent marks it ``unknown``; it never sends
that intent again.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable


_INITIALIZED = b"feiyu-heybox-rejection-notice-v2\n"
_ID = re.compile(r"[1-9]\d{0,23}\Z")
_NOTICE_ID = re.compile(r"heybox-reject:[a-f0-9]{32}\Z")
_INCIDENT_ID = re.compile(r"[a-f0-9]{32}\Z")
_MESSAGE_ID = re.compile(r"[1-9]\d{0,23}\Z")
_STAGES = frozenset({"reply_send", "notice_event"})
_REASONS = {"platform_content_rejected": "平台拒绝了这条评论内容"}
_NOTIFICATION = frozenset({"not_attempted", "pending", "sent", "unknown"})
_F060_CATEGORIES = frozenset({
    "vision_failed", "model_failed", "model_connection_failed", "model_timeout", "request_failed",
    "connection_failed", "service_unavailable", "rate_limited", "access_denied", "response_invalid",
    "request_rejected", "platform_content_rejected", "storage_failed", "delivery_unknown",
    "credential_invalid", "guard_failed", "unexpected_exception",
})


class RejectionNoticeStoreBusy(OSError):
    """Another cooperating process currently owns the store lock."""


class RejectionNotice:
    """A metadata-only view returned by ``get`` and public operations."""

    __slots__ = (
        "notice_id", "event_id", "outgoing_id", "reason", "stage", "created_at",
        "notification", "message_id", "receipt_kind",
        "handoff_incident_id",
    )

    def __init__(self, row: sqlite3.Row):
        self.notice_id = row["notice_id"]
        self.event_id = row["event_id"]
        self.outgoing_id = row["outgoing_id"]
        self.reason = row["reason"]
        self.stage = row["stage"]
        self.created_at = row["created_at"]
        self.notification = row["notification"]
        self.message_id = row["message_id"]
        self.receipt_kind = row["receipt_kind"]
        self.handoff_incident_id = row["handoff_incident_id"]

    def as_dict(self) -> dict[str, Any]:
        return {slot: getattr(self, slot) for slot in self.__slots__}


def _safe_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if _ID.fullmatch(text) else None


def _safe_stamp(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _fixed_text(row: dict[str, Any] | sqlite3.Row) -> str:
    """Build the only text this policy may authorize for owner delivery."""
    reason = _REASONS[row["reason"]]
    return (
        "主人，小黑盒平台拒绝了一条评论，我已单独隔离，不会重试这条评论。\n"
        "后续新帖会继续按现有保护检查处理。\n"
        f"原因：{reason}\n"
        f"事件编号：{row['event_id']}\n出站编号：{row['outgoing_id']}\n"
        f"通知编号：{row['notice_id']}"
    )


def _validated(row):
    if row is None:
        return None
    if (
        not isinstance(row["notice_id"], str) or not _NOTICE_ID.fullmatch(row["notice_id"])
        or _safe_id(row["event_id"]) is None or _safe_id(row["outgoing_id"]) is None
        or row["reason"] not in _REASONS or row["stage"] not in _STAGES
        or not _safe_stamp(row["created_at"]) or row["notification"] not in _NOTIFICATION
        or (row["notification"] == "sent" and (
            not isinstance(row["message_id"], str) or not _MESSAGE_ID.fullmatch(row["message_id"])
            or row["receipt_kind"] != "native"))
        or (row["notification"] != "sent" and (row["message_id"] != "" or row["receipt_kind"] != ""))
        or not isinstance(row["handoff_incident_id"], str)
        or (row["handoff_incident_id"] != "" and (
            not _INCIDENT_ID.fullmatch(row["handoff_incident_id"]) or row["notification"] == "sent"))
    ):
        raise ValueError("rejection_notice_store_invalid")
    return row


def rejection_notice_gate(path, incidents):
    """Read only: block unresolved notice intents, including after restart.

    No first-run files are created here. A handed-off unknown remains unknown;
    the exact existing F060 row controls its later owner-authorized recovery.
    """
    path = Path(path).absolute()
    probe = HeyboxRejectionNotices(path, None)
    artifacts = path.exists() or path.is_symlink() or probe.marker_path.exists() or probe.marker_path.is_symlink()
    if not artifacts:
        artifacts = any(path.parent.glob(path.name + ".*.flight"))
    if not artifacts:
        return {"ok": True, "blocked": False, "unhanded_count": 0, "reason": "absent"}
    try:
        probe._validate_store()
        probe._store_error = ""
        by_id = {row["id"]: row for row in incidents}
        blocked_count = 0
        with probe._db(readonly=True) as db:
            for row in db.execute("SELECT * FROM rejections WHERE notification!='sent'"):
                _validated(row)
                probe._validate_identity(db, row)
                handoff = row["handoff_incident_id"]
                if not handoff:
                    blocked_count += 1
                    continue
                incident = by_id.get(handoff)
                if not _valid_handoff(row, incident, allow_resolved=True):
                    raise ValueError("rejection_notice_handoff_invalid")
        return {"ok": True, "blocked": blocked_count > 0, "unhanded_count": blocked_count,
                "reason": "notice_unconfirmed" if blocked_count else "clear"}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return {"ok": False, "blocked": True, "unhanded_count": None, "reason": "rejection_store_unavailable"}


def _valid_handoff(row, incident, *, allow_resolved=False):
    compatible = bool(
        isinstance(incident, dict)
        and isinstance(incident.get("id"), str) and _INCIDENT_ID.fullmatch(incident["id"])
        and incident.get("feature") == "heybox_comment"
        and incident.get("state") in ({"paused", "resolved"} if allow_resolved else {"paused"})
        and incident.get("category") in _F060_CATEGORIES
        and _safe_stamp(incident.get("created_at"))
    )
    if not compatible:
        return False
    exact = bool(
        incident.get("category") in {"delivery_unknown", "guard_failed", "storage_failed"}
        and incident.get("stage") == "reply_send"
        and (incident.get("notification") == "not_requested" or (
            row["notification"] == "not_attempted" and incident.get("category") == "guard_failed"
            and incident.get("notification") in {"sent", "unknown"}))
        and incident.get("event_digest") == hashlib.sha256(row["notice_id"].encode("utf-8")).hexdigest()
    )
    # A genuinely new concurrent F060 pause already covers this item. Keep
    # that incident's source and notice untouched; bind only its durable ID.
    return exact or incident["created_at"] >= row["created_at"]


class HeyboxRejectionNotices:
    """Durable per-item isolation and one owner notice.

    Parameters
    ----------
    path:
        Candidate SQLite path.  No file is opened by the constructor.
    send_notice:
        Async callback accepting ``(notice_id, fixed_text)``.  The production
        adapter must route this through the existing native owner-private
        transport and return ``{"confirmed": True, "transport": "native", "message_id": ...}`` only
        after a native receipt is durably confirmed.
    now:
        Injectable clock for offline checks.
    notice_ready:
        Optional zero-argument readiness predicate.  If false, the item stays
        isolated as ``not_attempted`` and can be considered later.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        send_notice: Callable[[str, str], Awaitable[dict[str, Any]]],
        *,
        now: Callable[[], float] = time.time,
        notice_ready: Callable[[], bool] | None = None,
    ) -> None:
        self.path = Path(path).absolute()
        self.send_notice = send_notice
        self.now = now
        self.notice_ready = notice_ready
        self._store_error = "not_initialized"
        self._retired = False
        self._inflight: dict[str, tuple[asyncio.Task[Any], str]] = {}

    @property
    def initialized(self) -> bool:
        return not self._store_error

    @property
    def marker_path(self) -> Path:
        return self.path.with_name(self.path.name + ".initialized")

    def init(self) -> dict[str, Any]:
        """Create/validate the isolated store; never sends or changes F060."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink() or self.marker_path.is_symlink():
                raise OSError("rejection_notice_store_invalid")
            if not self.path.exists() and not self.marker_path.exists():
                if any(self.path.parent.glob(self.path.name + ".*.flight")):
                    raise ValueError("rejection_notice_store_missing")
                fd = os.open(str(self.marker_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(_INITIALIZED)
                    stream.flush()
                    os.fsync(stream.fileno())
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute(
                        "CREATE TABLE meta (version INTEGER NOT NULL, highwater REAL NOT NULL)"
                    )
                    db.execute("INSERT INTO meta VALUES (1, 0)")
                    db.execute(
                        "CREATE TABLE rejections ("
                        "notice_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, "
                        "outgoing_id TEXT NOT NULL UNIQUE, reason TEXT NOT NULL, "
                        "stage TEXT NOT NULL, created_at REAL NOT NULL, "
                        "notification TEXT NOT NULL, message_id TEXT NOT NULL, "
                        "receipt_kind TEXT NOT NULL, handoff_incident_id TEXT NOT NULL)"
                    )
                    db.execute(
                        "CREATE INDEX rejections_notification ON rejections(notification)"
                    )
                    db.commit()
            self._validate_store()
            self._store_error = ""
            return {"ok": True, "reason": "ready"}
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self._store_error = "store_unavailable"
            return {"ok": False, "reason": self._store_error}

    def _validate_store(self) -> None:
        if (
            self.path.is_symlink()
            or self.marker_path.is_symlink()
            or not self.path.is_file()
            or not self.marker_path.is_file()
            or self.marker_path.stat().st_size != len(_INITIALIZED)
            or self.marker_path.read_bytes() != _INITIALIZED
        ):
            raise ValueError("rejection_notice_store_invalid")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            meta = db.execute("SELECT version, highwater FROM meta").fetchall()
            if (
                len(meta) != 1
                or meta[0][0] != 1
                or not _safe_stamp(meta[0][1]) and meta[0][1] != 0
                or tuple(db.execute("PRAGMA quick_check(1)").fetchone()) != ("ok",)
            ):
                raise ValueError("rejection_notice_store_invalid")
            required_unique = {("notice_id",), ("event_id",), ("outgoing_id",)}
            actual_unique = {
                tuple(item[0] for item in db.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)))
                for index in db.execute("PRAGMA index_list(rejections)")
                if index[2] == 1 and index[4] == 0
            }
            if not required_unique <= actual_unique:
                raise ValueError("rejection_notice_identity_constraints_missing")
            rows = db.execute(
                "SELECT notice_id,event_id,outgoing_id,reason,stage,created_at,"
                "notification,message_id,receipt_kind,handoff_incident_id FROM rejections"
            ).fetchall()
            for row in rows:
                _validated(row)

    @contextmanager
    def _db(self, *, readonly=False):
        if self._store_error:
            raise OSError("rejection_notice_store_unavailable")
        if (self.path.is_symlink() or self.marker_path.is_symlink()
                or not self.path.is_file() or not self.marker_path.is_file()
                or self.marker_path.stat().st_size != len(_INITIALIZED)
                or self.marker_path.read_bytes() != _INITIALIZED):
            raise ValueError("rejection_notice_store_invalid")
        db = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=0.2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=200")
            yield db
        finally:
            db.close()

    @contextmanager
    def _flight(self, notice_id: str):
        """Nonblocking per-notice lease held over the one native await."""
        if not isinstance(notice_id, str) or not _NOTICE_ID.fullmatch(notice_id):
            raise ValueError("invalid_rejection_notice_id")
        # ':' is legal in the native source identifier but is not a Windows
        # filename character. Use only its checked UUID component for leases.
        path = self.path.with_name(self.path.name + "." + notice_id.split(":", 1)[1] + ".flight")
        if path.is_symlink():
            raise OSError("rejection_notice_lock_invalid")
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(error, "winerror", None) in {33, 36}:
                    raise RejectionNoticeStoreBusy("rejection_notice_flight_busy") from None
                raise
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _row(self, db: sqlite3.Connection, notice_id: str) -> sqlite3.Row | None:
        row = _validated(db.execute(
            "SELECT notice_id,event_id,outgoing_id,reason,stage,created_at,"
            "notification,message_id,receipt_kind,handoff_incident_id FROM rejections WHERE notice_id=?",
            (notice_id,),
        ).fetchone())
        self._validate_identity(db, row)
        return row

    def _validate_identity(self, db, row):
        if row is not None and db.execute(
            "SELECT COUNT(*) FROM rejections WHERE notice_id=? OR event_id=? OR outgoing_id=?",
            (row["notice_id"], row["event_id"], row["outgoing_id"]),
        ).fetchone()[0] != 1:
            raise ValueError("rejection_notice_identity_conflict")

    def get(self, notice_id: str) -> dict[str, Any] | None:
        if not isinstance(notice_id, str) or not _NOTICE_ID.fullmatch(notice_id) or not self.initialized:
            return None
        try:
            with self._db() as db:
                row = self._row(db, notice_id)
                return RejectionNotice(row).as_dict() if row else None
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self._store_error = "store_unavailable"
            return None

    def _result(self, row: sqlite3.Row, *, reason: str = "already_recorded") -> dict[str, Any]:
        return {
            "ok": True,
            "isolated": True,
            "reason": reason,
            "notice_id": row["notice_id"],
            "event_id": row["event_id"],
            "outgoing_id": row["outgoing_id"],
            "notification": row["notification"],
        }

    def _ready(self) -> bool:
        if self._retired:
            return False
        if self.notice_ready is None:
            return True
        try:
            return self.notice_ready() is True
        except Exception:
            return False

    def _insert_or_find(self, event_id: str, outgoing_id: str, reason: str, stage: str) -> tuple[sqlite3.Row, bool]:
        stamp = self.now()
        if not _safe_stamp(stamp):
            raise ValueError("invalid_clock")
        notice_id = "heybox-reject:" + uuid.uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = _validated(db.execute(
                "SELECT notice_id,event_id,outgoing_id,reason,stage,created_at,"
                "notification,message_id,receipt_kind,handoff_incident_id FROM rejections "
                "WHERE event_id=? OR outgoing_id=? LIMIT 1",
                (event_id, outgoing_id),
            ).fetchone())
            if row:
                self._validate_identity(db, row)
                if row["event_id"] != event_id or row["outgoing_id"] != outgoing_id:
                    raise ValueError("rejection_notice_identity_conflict")
                db.commit()
                return row, False
            db.execute(
                "INSERT INTO rejections VALUES (?,?,?,?,?,?,?,?,?,?)",
                (notice_id, event_id, outgoing_id, reason, stage, stamp, "not_attempted", "", "", ""),
            )
            db.execute("UPDATE meta SET highwater=?", (stamp,))
            row = self._row(db, notice_id)
            db.commit()
            if row is None:
                raise ValueError("rejection_notice_insert_missing")
            return row, True

    def _set_notification(self, notice_id: str, notification: str, message_id: str = "") -> sqlite3.Row:
        if notification not in _NOTIFICATION or (message_id and not _MESSAGE_ID.fullmatch(message_id)):
            raise ValueError("invalid_rejection_notice_state")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE rejections SET notification=?,message_id=?,receipt_kind=? WHERE notice_id=?",
                (notification, message_id, "native" if notification == "sent" else "", notice_id),
            )
            row = self._row(db, notice_id)
            db.commit()
            if row is None:
                raise ValueError("rejection_notice_missing")
            return row

    def notice_text(self, notice_id: str) -> str | None:
        row = self.get(notice_id)
        if not row:
            return None
        return _fixed_text(row)

    def find(self, event_id, outgoing_id):
        """Read exact item metadata only, including after a cancelled await."""
        event, outgoing = _safe_id(event_id), _safe_id(outgoing_id)
        if event is None or outgoing is None or not self.initialized:
            return None
        try:
            with self._db(readonly=True) as db:
                row = _validated(db.execute(
                    "SELECT * FROM rejections WHERE event_id=? AND outgoing_id=?", (event, outgoing),
                ).fetchone())
                self._validate_identity(db, row)
                return RejectionNotice(row).as_dict() if row else None
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self._store_error = "store_unavailable"
            return None

    def acknowledge_pause(self, notice_id, incident):
        """Transfer only an exact persisted unresolved notice to its F060 row."""
        if not isinstance(notice_id, str) or not _NOTICE_ID.fullmatch(notice_id):
            return {"ok": False, "reason": "invalid_rejection_notice"}
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = self._row(db, notice_id)
                if row is None or row["notification"] == "sent" or not _valid_handoff(row, incident):
                    return {"ok": False, "reason": "rejection_handoff_not_authorized"}
                previous = row["handoff_incident_id"]
                if previous and previous != incident["id"]:
                    return {"ok": False, "reason": "rejection_handoff_conflict"}
                db.execute("UPDATE rejections SET handoff_incident_id=? WHERE notice_id=?",
                           (incident["id"], notice_id))
                db.commit()
            return {"ok": True, "reason": "handed_off"}
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self._store_error = "store_unavailable"
            return {"ok": False, "reason": "rejection_store_unavailable"}

    def notice_authorized(self, notice_id: str, text: str, task: asyncio.Task[Any] | None) -> bool:
        """Authorize only the exact current task and durable pending row."""
        if (not isinstance(notice_id, str) or not _NOTICE_ID.fullmatch(notice_id)
                or not isinstance(text, str) or self._retired
                or task is None or task is not asyncio.current_task()):
            return False
        current = self._inflight.get(notice_id)
        if not isinstance(current, tuple) or current != (task, text):
            return False
        try:
            with self._db() as db:
                row = self._row(db, notice_id)
                return bool(row and row["notification"] == "pending" and text == _fixed_text(row))
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self._store_error = "store_unavailable"
            return False

    async def _attempt(self, row: sqlite3.Row) -> dict[str, Any]:
        notice_id = row["notice_id"]
        try:
            with self._flight(notice_id):
                # A different process may have finalized this item while this
                # coroutine was acquiring its lease.
                with self._db() as db:
                    current = self._row(db, notice_id)
                if current is None:
                    return {"ok": False, "isolated": False, "reason": "record_missing", "notification": "unknown"}
                if current["notification"] != "not_attempted" or current["handoff_incident_id"]:
                    return self._result(current)
                if not self._ready():
                    return self._result(current, reason="notice_route_unavailable")
                # Import before persisting the attempt. Failure to provide the
                # established same-task deadline must not consume the intent.
                from .peak_runtime import same_task_deadline
                current = self._set_notification(notice_id, "pending")
                task = asyncio.current_task()
                text = _fixed_text(current)
                self._inflight[notice_id] = (task, text)
                try:
                    async with same_task_deadline(40):
                        result = await self.send_notice(notice_id, text)
                except asyncio.CancelledError:
                    try:
                        current = self._set_notification(notice_id, "unknown")
                    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                        # Keep the durable pending intent as uncertainty. A
                        # broken final write must never swallow task shutdown.
                        self._store_error = "store_unavailable"
                    raise
                except Exception:
                    current = self._set_notification(notice_id, "unknown")
                    return self._result(current, reason="notice_unknown")
                finally:
                    self._inflight.pop(notice_id, None)
                message_id = result.get("message_id") if isinstance(result, dict) else None
                if (
                    isinstance(result, dict)
                    and result.get("confirmed") is True
                    and result.get("transport") == "native"
                    and isinstance(message_id, (str, int))
                    and not isinstance(message_id, bool)
                    and _MESSAGE_ID.fullmatch(str(message_id))
                ):
                    current = self._set_notification(notice_id, "sent", str(message_id))
                    return self._result(current, reason="notice_sent")
                current = self._set_notification(notice_id, "unknown")
                return self._result(current, reason="notice_unknown")
        except RejectionNoticeStoreBusy:
            current = self.get(notice_id)
            return self._result(current, reason="already_in_flight") if current else {
                "ok": False, "isolated": False, "reason": "store_busy", "notice_id": notice_id,
                "notification": "unknown",
            }
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, ImportError):
            self._store_error = "store_unavailable"
            return {"ok": False, "isolated": False, "reason": self._store_error, "notice_id": notice_id,
                    "notification": "unknown"}

    async def report(
        self,
        event_id: Any,
        outgoing_id: Any,
        *,
        reason: str = "platform_content_rejected",
        stage: str = "reply_send",
    ) -> dict[str, Any]:
        """Record one strict platform rejection and attempt one notice."""
        event = _safe_id(event_id)
        outgoing = _safe_id(outgoing_id)
        if (event is None or outgoing is None or not isinstance(reason, str) or reason not in _REASONS
                or not isinstance(stage, str) or stage not in _STAGES):
            return {"ok": False, "isolated": False, "reason": "invalid_rejection", "notification": "not_attempted"}
        if self._retired or not self.initialized:
            return {"ok": False, "isolated": False, "reason": self._store_error or "retired", "notification": "unknown"}
        try:
            row, created = self._insert_or_find(event, outgoing, reason, stage)
            if not created:
                return self._result(row)
            return await self._attempt(row)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self._store_error = "store_unavailable"
            return {"ok": False, "isolated": False, "reason": self._store_error, "notification": "unknown"}

    async def recover_waiting(self, limit: int = 16) -> dict[str, Any]:
        """Only reconcile abandoned pending; never make any notification."""
        if self._retired or not self.initialized:
            return {"ok": False, "reason": self._store_error or "retired", "processed": 0}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 64:
            return {"ok": False, "reason": "invalid_limit", "processed": 0}
        processed = 0
        try:
            with self._db() as db:
                rows = db.execute(
                    "SELECT notice_id,event_id,outgoing_id,reason,stage,created_at,"
                    "notification,message_id,receipt_kind,handoff_incident_id FROM rejections "
                    "WHERE notification='pending' AND handoff_incident_id='' "
                    "ORDER BY created_at LIMIT ?",
                    (limit,),
                ).fetchall()
            for row in rows:
                _validated(row)
                if row["notification"] == "pending":
                    # No active lease means the previous process stopped after
                    # persisting the intent.  Mark it unknown and never retry.
                    try:
                        with self._flight(row["notice_id"]):
                            current = self.get(row["notice_id"])
                            if current is None or self._store_error:
                                return {"ok": False, "reason": self._store_error or "record_missing", "processed": processed}
                            if current and current["notification"] == "pending":
                                self._set_notification(row["notice_id"], "unknown")
                                processed += 1
                    except RejectionNoticeStoreBusy:
                        continue
                    continue
            return {"ok": True, "reason": "reconciled", "processed": processed}
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self._store_error = "store_unavailable"
            return {"ok": False, "reason": self._store_error, "processed": processed}

    def status(self) -> dict[str, Any]:
        if not self.initialized:
            return {"ok": False, "reason": self._store_error, "count": 0}
        try:
            with self._db() as db:
                count = db.execute("SELECT COUNT(*) FROM rejections").fetchone()[0]
                pending = db.execute(
                    "SELECT COUNT(*) FROM rejections WHERE notification='pending'"
                ).fetchone()[0]
            return {"ok": True, "reason": "ready", "count": int(count), "pending": int(pending)}
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self._store_error = "store_unavailable"
            return {"ok": False, "reason": self._store_error, "count": 0}

    def retire(self) -> None:
        """Invalidate in-memory authorizations during a plugin reload."""
        self._retired = True
        self._inflight.clear()

__all__ = ["HeyboxRejectionNotices", "RejectionNoticeStoreBusy"]
