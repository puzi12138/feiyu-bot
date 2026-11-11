"""Durable, bounded permission to attempt one different content item.

This module does not call a model, send a notice or resolve an incident.  Its
caller must independently verify the incident, source provenance, the old
item's terminal state and the new item's eligibility.  A reservation is
persisted before dispatch; an ambiguous or interrupted attempt stays closed.
Only irreversible, domain-separated hashes of content identifiers are stored.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
import uuid

_ID = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_SENTINEL = b"feiyu-new-content-retry-v1\n"
_CAPABILITY = ContextVar("feiyu_new_content_retry_capability", default=None)
_PHASES = frozenset({"pending_notice", "pending_terminal", "ready", "reserved",
                     "started", "accepted", "blocked", "settled"})
_SCOPES = frozenset({"heybox", "bili_journal"})
_ALLOWED = {
    ("heybox", "heybox_vision", "vision_failed", "notice_event", "connection"),
    ("heybox", "heybox_vision", "vision_failed", "notice_event", "timeout"),
    ("heybox", "heybox_comment", "model_connection_failed", "notice_event", "connection"),
    ("heybox", "heybox_comment", "model_timeout", "notice_event", "timeout"),
    ("bili_journal", "bili_journal", "model_failed", "bili_journal_once", "connection"),
    ("bili_journal", "bili_journal", "model_failed", "bili_journal_once", "timeout"),
}
_FIELDS = frozenset({"incident_id", "scope", "feature", "category", "stage", "failure_kind",
    "failed_key_hash", "failed_at", "phase", "candidate_key_hash", "observed_at", "event_key_hash",
    "token_hash", "generation", "notice_id", "notification", "notice_message_id", "notice_receipt_kind",
    "created_at", "updated_at"})


class RetryUnavailable(RuntimeError):
    """A bad/missing state or unavailable lock must never open a guard."""


def _stamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _key_hash(scope, value, kind="content"):
    if scope not in _SCOPES or type(value) not in (str, int) or isinstance(value, bool):
        raise ValueError("invalid_identity")
    text = str(value)
    if not text or len(text) > 256 or any(ord(c) < 32 for c in text):
        raise ValueError("invalid_identity")
    return hashlib.sha256(("feiyu-retry-v1\0" + scope + "\0" + kind + "\0" + text).encode()).hexdigest()


def content_hash(scope, value):
    """Return only a scoped hash, never the original post ID or BVID."""
    return _key_hash(scope, value)


class RetryStore:
    """One durable active slot per scope, with task-bound temporary grants."""

    def __init__(self, path, *, now=time.time):
        self.path = Path(path)
        self.now = now
        self.generation = uuid.uuid4().hex
        self._tokens = {}

    @property
    def sentinel(self):
        return self.path.with_name(self.path.name + ".initialized")

    @contextmanager
    def _lock(self):
        path = self.path.with_name(self.path.name + ".lock")
        if path.is_symlink():
            raise RetryUnavailable("invalid_lock")
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
                    raise RetryUnavailable("store_busy") from None
                raise
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def install(self):
        """Explicit one-time installation; never auto-recreate missing state."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if self.path.exists() or self.path.is_symlink() or self.sentinel.exists() or self.sentinel.is_symlink():
                self._read()
                return False
            with self.sentinel.open("xb") as stream:
                stream.write(_SENTINEL); stream.flush(); os.fsync(stream.fileno())
            self._write({"version": 1, "owner_generation": self.generation, "trials": []})
            return True

    def _read(self):
        if (self.sentinel.is_symlink() or not self.sentinel.is_file()
                or self.sentinel.read_bytes() != _SENTINEL or self.path.is_symlink()
                or not self.path.is_file() or not 0 < self.path.stat().st_size <= 1024 * 1024):
            raise RetryUnavailable("store_unavailable")
        try:
            def unique(items):
                result = {}
                for key, item in items:
                    if key in result:
                        raise ValueError("duplicate_key")
                    result[key] = item
                return result
            value = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=unique)
            if (type(value) is not dict or set(value) != {"version", "owner_generation", "trials"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or type(value["owner_generation"]) is not str or not _ID.fullmatch(value["owner_generation"])
                    or type(value["trials"]) is not list or len(value["trials"]) > 4096):
                raise ValueError("invalid_store")
            ids, active = set(), set()
            for row in value["trials"]:
                if type(row) is not dict or set(row) != _FIELDS:
                    raise ValueError("invalid_row")
                if (not _ID.fullmatch(row["incident_id"]) or row["incident_id"] in ids
                        or row["scope"] not in _SCOPES or row["phase"] not in _PHASES
                        or (row["scope"], row["feature"], row["category"], row["stage"], row["failure_kind"]) not in _ALLOWED
                        or not _HASH.fullmatch(row["failed_key_hash"])
                        or any(type(row[k]) is not str or (row[k] and not _HASH.fullmatch(row[k]))
                               for k in ("candidate_key_hash", "event_key_hash", "token_hash"))
                        or not all(_stamp(row[k]) for k in ("failed_at", "created_at", "updated_at"))
                        or not (row["observed_at"] == 0 or _stamp(row["observed_at"]))
                        or type(row["generation"]) is not str or not _ID.fullmatch(row["generation"])
                        or (row["notice_id"] and not _ID.fullmatch(row["notice_id"]))
                        or row["notification"] not in {"none", "pending", "sent", "unknown"}
                        or type(row["notice_message_id"]) is not str
                        or row["notice_receipt_kind"] not in {"", "native"}
                        or ((row["notification"] == "sent") != bool(row["notice_message_id"]))
                        or ((row["notification"] == "sent") != (row["notice_receipt_kind"] == "native"))
                        or (row["notice_message_id"] and not re.fullmatch(r"[1-9]\d{0,23}", row["notice_message_id"]))):
                    raise ValueError("invalid_row")
                if row["phase"] in {"reserved", "started", "accepted"} and not all(
                        row[k] for k in ("candidate_key_hash", "token_hash", "generation", "observed_at")):
                    raise ValueError("invalid_claim")
                if row["phase"] in {"started", "accepted"} and not row["event_key_hash"]:
                    raise ValueError("invalid_event")
                ids.add(row["incident_id"])
                if row["phase"] != "settled":
                    if row["scope"] in active:
                        raise ValueError("scope_conflict")
                    active.add(row["scope"])
            return value
        except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError):
            raise RetryUnavailable("store_invalid") from None

    def _write(self, value):
        if self.path.is_symlink():
            raise RetryUnavailable("store_invalid")
        tmp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with tmp.open("xb") as stream:
                stream.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())
                stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _now(self):
        stamp = self.now()
        if not _stamp(stamp):
            raise RetryUnavailable("clock_unavailable")
        return float(stamp)

    def _row(self, value, incident_id):
        if type(incident_id) is not str or not _ID.fullmatch(incident_id):
            raise ValueError("invalid_incident")
        return next((row for row in value["trials"] if row["incident_id"] == incident_id), None)

    def prepare(self, row, retry):
        """Reserve retry intent before first notice; old F060 incident stays closed."""
        if type(row) is not dict or type(retry) is not dict or set(retry) != {"scope", "failed_key", "failure_kind"}:
            return False
        key = (retry["scope"], row.get("feature"), row.get("category"), row.get("stage"), retry["failure_kind"])
        if (key not in _ALLOWED or row.get("state") not in {"closing", "paused"}
                or row.get("notification") not in {"pending", "sent"}
                or type(row.get("id")) is not str or not _ID.fullmatch(row["id"])
                or not _stamp(row.get("created_at"))):
            return False
        failed_hash = content_hash(retry["scope"], retry["failed_key"])
        now = self._now()
        if row["created_at"] > now + 2:
            return False
        with self._lock():
            value = self._read()
            if not self._owned(value):
                return False
            existing = self._row(value, row["id"])
            if existing:
                return (existing["scope"] == retry["scope"] and existing["failed_key_hash"] == failed_hash
                        and existing["failure_kind"] == retry["failure_kind"]
                        and existing["phase"] == "pending_notice" and existing["generation"] == self.generation)
            if len(value["trials"]) >= 4096 or any(x["scope"] == retry["scope"] and x["phase"] != "settled" for x in value["trials"]):
                return False
            value["trials"].append({"incident_id": row["id"], "scope": retry["scope"],
                "feature": row["feature"], "category": row["category"], "stage": row["stage"],
                "failure_kind": retry["failure_kind"], "failed_key_hash": failed_hash,
                "failed_at": float(row["created_at"]), "phase": "pending_notice",
                "candidate_key_hash": "", "observed_at": 0, "event_key_hash": "", "token_hash": "",
                "generation": self.generation, "notice_id": "", "notification": "none",
                "notice_message_id": "", "notice_receipt_kind": "", "created_at": now, "updated_at": now})
            self._write(value)
            return True

    def first_notice(self, incident_id, confirmed):
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            if not self._owned(value) or row is None or row["phase"] != "pending_notice" or row["generation"] != self.generation:
                return False
            row["phase"] = "pending_terminal" if confirmed is True else "blocked"
            row["updated_at"] = self._now(); self._write(value)
            return True

    def planned_notice(self, incident_id):
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            return bool(self._owned(value) and row and row["phase"] == "pending_notice" and row["generation"] == self.generation)

    def installed(self):
        """Distinguish a never-installed optional feature from damaged state."""
        exists = self.path.exists() or self.path.is_symlink()
        sentinel = self.sentinel.exists() or self.sentinel.is_symlink()
        if not exists and not sentinel:
            return False
        with self._lock():
            self._read()
        return True

    def activate_runtime(self):
        """Fence any older policy instance; never rearm its unfinished rows."""
        with self._lock():
            value = self._read()
            if value["owner_generation"] != self.generation:
                value["owner_generation"] = self.generation
                self._write(value)
            return True

    def _owned(self, value):
        return value["owner_generation"] == self.generation

    def owns_generation(self):
        with self._lock():
            return self._read()["owner_generation"] == self.generation

    def state(self, incident_id):
        with self._lock():
            row = self._row(self._read(), incident_id)
            return dict(row) if row is not None else None

    def unsettled_feature(self, feature):
        with self._lock():
            return any(row["feature"] == feature and row["phase"] != "settled"
                       for row in self._read()["trials"])

    def ready(self, incident_id, failed_key):
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            if (not self._owned(value) or row is None or row["phase"] != "pending_terminal" or row["generation"] != self.generation
                    or content_hash(row["scope"], failed_key) != row["failed_key_hash"]):
                return False
            row["phase"] = "ready"; row["updated_at"] = self._now(); self._write(value)
            return True

    def intake(self, scope):
        if scope not in _SCOPES:
            return None
        with self._lock():
            value = self._read()
            if not self._owned(value):
                return None
            rows = [row for row in value["trials"] if row["scope"] == scope
                    and row["phase"] == "ready" and row["generation"] == self.generation]
            if len(rows) != 1:
                return None
            return {key: rows[0][key] for key in ("incident_id", "failed_at", "failed_key_hash")}

    def reserve(self, incident_id, candidate_key, observed_at):
        now = self._now()
        if not _stamp(observed_at) or observed_at > now + 2:
            return None
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            if (not self._owned(value) or row is None or row["phase"] != "ready" or row["generation"] != self.generation
                    or observed_at <= row["failed_at"]):
                return None
            candidate = content_hash(row["scope"], candidate_key)
            if candidate == row["failed_key_hash"]:
                return None
            token = secrets.token_hex(32)
            row.update(phase="reserved", candidate_key_hash=candidate, observed_at=float(observed_at),
                token_hash=hashlib.sha256(token.encode()).hexdigest(), generation=self.generation, updated_at=now)
            self._write(value)
            self._tokens[incident_id] = token
            return token

    def _matches(self, row, token, candidate_key, event_key=None):
        if (row is None or row["generation"] != self.generation or type(token) is not str
                or not secrets.compare_digest(self._tokens.get(row["incident_id"], ""), token)
                or not secrets.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
                or row["candidate_key_hash"] != content_hash(row["scope"], candidate_key)):
            return False
        return event_key is None or row["event_key_hash"] == _key_hash(row["scope"], event_key, "event")

    def release(self, incident_id, token, candidate_key, *, definitely_not_started=False):
        """Release only a proven pre-dispatch reservation, never an ambiguous one."""
        if definitely_not_started is not True:
            return False
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            if not self._owned(value) or row is None or row["phase"] != "reserved" or not self._matches(row, token, candidate_key):
                return False
            row.update(phase="ready", candidate_key_hash="", observed_at=0, event_key_hash="",
                       token_hash="", generation=self.generation, updated_at=self._now())
            self._write(value); self._tokens.pop(incident_id, None)
            return True

    def start(self, incident_id, token, candidate_key, event_key):
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            if not self._owned(value) or row is None or row["phase"] != "reserved" or not self._matches(row, token, candidate_key):
                return False
            row.update(phase="started", event_key_hash=_key_hash(row["scope"], event_key, "event"), updated_at=self._now())
            self._write(value)
            return True

    def check(self, incident_id, token, candidate_key, event_key):
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            return bool(self._owned(value) and row and row["phase"] == "started" and self._matches(row, token, candidate_key, event_key))

    @contextmanager
    def event_scope(self, incident_id, token, candidate_key, event_key):
        task = asyncio.current_task()
        if task is None or not self.check(incident_id, token, candidate_key, event_key):
            raise RetryUnavailable("event_scope_invalid")
        bound = _CAPABILITY.set((self, task, incident_id, token, candidate_key, event_key))
        try:
            yield
        finally:
            _CAPABILITY.reset(bound)

    def allows_event(self, incident_id):
        try:
            cap = _CAPABILITY.get()
            return bool(cap and len(cap) == 6 and cap[0] is self and cap[1] is asyncio.current_task()
                        and cap[2] == incident_id and self.check(cap[2], cap[3], cap[4], cap[5]))
        except (RuntimeError, OSError, ValueError, TypeError, RetryUnavailable):
            return False

    def finish(self, incident_id, token, candidate_key, event_key, outcome):
        if outcome not in {"success", "failed", "unknown"}:
            return None
        try:
            with self._lock():
                value = self._read(); row = self._row(value, incident_id)
                if not self._owned(value) or row is None or row["phase"] != "started" or not self._matches(row, token, candidate_key, event_key):
                    return None
                row["phase"] = "accepted" if outcome == "success" else "blocked"
                if outcome != "success":
                    row["notice_id"] = uuid.uuid4().hex
                    row["notification"] = "pending"
                row["updated_at"] = self._now(); self._write(value)
                return dict(row)
        finally:
            # If durable completion fails, this process must not be able to
            # regain the same event grant or repeat a possibly attempted send.
            self._tokens.pop(incident_id, None)

    def abort_ready(self, incident_id, *, notify=True):
        """Close one live discovery opportunity before any candidate was claimed.

        A missing suitable item or an uncertain discovery is not permission to
        keep searching indefinitely.  The caller owns the current wake grant;
        this transaction only accepts the original ready slot once.
        """
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            if (not self._owned(value) or row is None or row["phase"] != "ready"
                    or row["generation"] != self.generation):
                return None
            if type(notify) is not bool:
                return None
            row.update(phase="blocked", notice_id=uuid.uuid4().hex if notify else "",
                       notification="pending" if notify else "none", updated_at=self._now())
            self._write(value)
            return dict(row)

    def settle(self, incident_id):
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            if not self._owned(value) or row is None or row["phase"] != "accepted":
                return False
            row["phase"] = "settled"; row["updated_at"] = self._now(); self._write(value)
            self._tokens.pop(incident_id, None)
            return True

    def secondary_notice(self, notice_id):
        with self._lock():
            value = self._read()
            if not self._owned(value):
                return None
            rows = [r for r in value["trials"] if r["notice_id"] == notice_id
                    and r["notification"] == "pending" and r["generation"] == self.generation]
            return dict(rows[0]) if len(rows) == 1 else None

    def finish_secondary_notice(self, notice_id, message_id=""):
        with self._lock():
            value = self._read()
            if not self._owned(value):
                return False
            rows = [r for r in value["trials"] if r["notice_id"] == notice_id
                    and r["notification"] == "pending" and r["generation"] == self.generation]
            if len(rows) != 1:
                return False
            valid = type(message_id) is str and re.fullmatch(r"[1-9]\d{0,23}", message_id)
            rows[0]["notification"] = "sent" if valid else "unknown"
            rows[0]["notice_message_id"] = message_id if valid else ""
            rows[0]["notice_receipt_kind"] = "native" if valid else ""
            rows[0]["updated_at"] = self._now(); self._write(value)
            return True

    def owner_resolved(self, incident_id):
        """Explicit owner resolution closes, never revives, any trial slot."""
        with self._lock():
            value = self._read(); row = self._row(value, incident_id)
            if not self._owned(value) or row is None or row["phase"] == "settled":
                return False
            row["phase"] = "settled"; row["updated_at"] = self._now(); self._write(value)
            self._tokens.pop(incident_id, None)
            return True
