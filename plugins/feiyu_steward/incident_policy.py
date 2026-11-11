"""Durable, feature-scoped pauses and one owner-only fault notice per incident.

Only explicit callers at protected business boundaries may report a fixed
feature/category/stage. No exception text, external content or chat is stored
or sent. A pending intent is committed before any attempted notification.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .failure_diagnostics import record_failure
from .new_content_retry import RetryStore, RetryUnavailable


FEATURES = {
    "heybox_vision": "小黑盒图片识别",
    "heybox_comment": "小黑盒自动评论",
    "heybox_feed": "小黑盒自动浏览",
    "bili_browse": "B站自动浏览",
    "bili_journal": "B站私人手账",
    "bili_share": "B站自动分享",
    "heybox_autopost": "小黑盒自动投稿",
    "heybox_qzone_sync": "小黑盒到QQ空间自动同步",
    "qzone_publish": "QQ空间自动发表",
    "friend_time_notice": "指定好友时段问候",
}
CATEGORIES = {
    "vision_failed": "图片识别未完成",
    "model_failed": "模型处理未完成",
    "model_connection_failed": "模型服务连接失败",
    "model_timeout": "模型服务响应超时",
    "request_failed": "服务请求未完成",
    "connection_failed": "平台服务连接失败",
    "service_unavailable": "平台服务暂时不可用",
    "rate_limited": "平台限制了请求频率",
    "access_denied": "平台拒绝了当前访问权限",
    "response_invalid": "平台响应格式不符合预期",
    "request_rejected": "平台拒绝了当前请求",
    "platform_content_rejected": "平台明确拒绝本条评论内容，本条不会重发",
    "storage_failed": "本机记录不可用",
    "delivery_unknown": "发送结果尚未确认",
    "credential_invalid": "登录状态不可用",
    "guard_failed": "安全保护不可用",
    "unexpected_exception": "处理时出现异常",
}
STAGES = frozenset({
    "", "feed_once", "notice_poll", "notice_event", "bili_browse_once",
    "bili_journal_once", "bili_share_once", "autopost_once", "autopost_send",
    "qzone_sync_once", "qzone_sync_send", "public_send", "reply_send", "friend_time_send",
})
MAX_INCIDENTS = 256
_LOCK_ATTEMPTS = 10
_LOCK_RETRY_SECONDS = 0.05
_INITIALIZED = b"feiyu-incident-policy-v1\n"
_ID = re.compile(r"[a-f0-9]{32}\Z")
_HEYBOX_REJECTION_ID = re.compile(r"heybox-reject:[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_MESSAGE_ID = re.compile(r"[1-9]\d{0,23}\Z")
_ROW_KEYS = frozenset({
    "id", "feature", "category", "stage", "event_digest", "state",
    "notification", "created_at", "resolved_at", "message_id", "receipt_kind",
})


class IncidentStoreBusy(OSError):
    """A different cooperating writer holds the incident ledger lock."""


def _notice(row):
    return (
        f"主人，{FEATURES[row['feature']]}遇到问题：{CATEGORIES[row['category']]}。\n"
        "我会先暂停这项自动功能，旧任务不会因为恢复而自动重发。\n"
        f"功能：{row['feature']}\n故障编号：{row['id']}\n"
        "处理好后可用 /恢复功能 功能编号 故障编号 解除暂停。"
    )


def _retry_notice(row):
    """Fixed first notice for a narrowly eligible new-content trial."""
    return (
        f"主人，{FEATURES[row['feature']]}遇到问题：{CATEGORIES[row['category']]}。\n"
        "我会先跳过这条，立即尝试另一条未处理过的新内容；"
        "如果没有合适内容、再次失败或结果无法确认，就暂停这项功能。旧任务不会自动重发。\n"
        f"功能：{row['feature']}\n故障编号：{row['id']}"
    )


def _retry_failure_notice(row):
    return (
        f"主人，{FEATURES[row['feature']]}的新内容尝试未能完成。\n"
        "我已暂停这项自动功能，旧任务不会自动重发。\n"
        f"功能：{row['feature']}\n故障编号：{row['incident_id']}\n"
        "处理好后可用 /恢复功能 功能编号 故障编号 解除暂停。"
    )


def _stamp(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


class IncidentPolicy:
    """Store a bounded ledger without modifying the steward's other state.

    ``blocked`` and ``status`` fail closed for the protected features when the
    ledger cannot be read. They never prevent normal owner chat or repair UI.
    """

    def __init__(self, path, send_notice, *, now=time.time, notice_ready=None):
        self.path = Path(path)
        self.send_notice = send_notice
        self.notice_ready = notice_ready
        self.now = now
        self._store_error = "not_initialized"
        self._closing = set()
        self._busy = set()
        self._inflight = {}
        # Item notices use an independent ledger and are created only by the
        # explicit rejection path. No import/init or replay on plugin startup.
        self._heybox_rejection_notices = None
        self._heybox_rejection_notice_binding = None
        # An absent store means the optional trial is not installed. Partial or
        # corrupt installed state never creates a permission to do more work.
        self._retry_store = RetryStore(self.path.with_name("incident-new-content-trials.json"), now=now)

    @contextmanager
    def _file_lock(self, lock_path):
        """Lock one byte until the context exits, also across processes."""
        if lock_path.is_symlink():
            raise OSError("incident_lock_invalid")
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
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
                    raise IncidentStoreBusy("incident_store_busy") from None
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

    def _locked(self):
        """Serialize ledger mutations without holding this lock over await."""
        return self._file_lock(self.path.with_name(self.path.name + ".lock"))

    def _flight_locked(self, incident_id):
        """Lease one in-flight notification independently of other incidents."""
        if not isinstance(incident_id, str) or not _ID.fullmatch(incident_id):
            raise ValueError("invalid_incident_id")
        return self._file_lock(self.path.with_name(self.path.name + "." + incident_id + ".flight"))

    def _sentinel_path(self):
        return self.path.with_name(self.path.name + ".initialized")

    def _check_sentinel(self):
        path = self._sentinel_path()
        if path.is_symlink() or not path.is_file() or path.stat().st_size != len(_INITIALIZED):
            raise ValueError("incident_sentinel_invalid")
        if path.read_bytes() != _INITIALIZED:
            raise ValueError("incident_sentinel_invalid")

    def _install_sentinel(self):
        """Persist installation identity before first writing an empty ledger."""
        path = self._sentinel_path()
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_INITIALIZED)
            stream.flush()
            os.fsync(stream.fileno())

    def _has_prior_incident_artifacts(self):
        if any(self.path.parent.glob(self.path.name + ".*.flight")):
            return True
        return any(self._marker_path(feature).exists() or self._marker_path(feature).is_symlink()
                   for feature in FEATURES)

    def _marker_path(self, feature):
        if not isinstance(feature, str) or feature not in FEATURES:
            raise ValueError("invalid_feature")
        return self.path.with_name(self.path.name + "." + feature + ".blocked")

    def _marker(self, feature):
        path = self._marker_path(feature)
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 4096:
            raise ValueError("invalid_incident_marker")
        row = json.loads(path.read_text(encoding="utf-8"))
        keys = {"id", "feature", "category", "stage", "created_at", "notify"}
        if (not isinstance(row, dict)
                or set(row) not in (keys, keys | {"event_digest"})
                or not isinstance(row["id"], str) or not _ID.fullmatch(row["id"])
                or row["feature"] != feature or row["category"] not in CATEGORIES
                or row["stage"] not in STAGES or not _stamp(row["created_at"])
                or row["created_at"] <= 0 or type(row["notify"]) is not bool
                or ("event_digest" in row and (feature != "friend_time_notice"
                    or not isinstance(row["event_digest"], str) or not _HASH.fullmatch(row["event_digest"])) )):
            raise ValueError("invalid_incident_marker")
        return row

    def _put_marker(self, feature, category, stage, notify, *, event_digest=""):
        """Atomically install a single complete no-content marker per feature."""
        existing = self._marker(feature)
        if existing:
            return existing
        stamp = self.now()
        if not _stamp(stamp) or stamp <= 0:
            raise ValueError("invalid_clock")
        row = {"id": uuid.uuid4().hex, "feature": feature, "category": category,
               "stage": stage, "created_at": stamp, "notify": notify is True}
        if event_digest:
            if feature != "friend_time_notice" or not _HASH.fullmatch(event_digest):
                raise ValueError("invalid_incident_event_digest")
            row["event_digest"] = event_digest
        target = self._marker_path(feature)
        temp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".marker-tmp")
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                # A concurrent reporter owns the first durable occurrence.
                existing = self._marker(feature)
                if existing is None:
                    raise IncidentStoreBusy("incident_marker_changed")
                return existing
            return row
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _clear_marker(self, feature, expected_id=None):
        """Called only under the ledger mutation lock after durable adoption."""
        path = self._marker_path(feature)
        marker = self._marker(feature)
        if marker is not None and (expected_id is None or marker["id"] == expected_id):
            path.unlink()

    def _reconcile_pending(self, value):
        """Mark abandoned sends unknown; never interfere with a live sender.

        The ledger lock is already held. Persist the transition before clearing
        any local latches, and do not keep either lock across an await.
        """
        changed = False
        recovered = []
        for row in value["incidents"]:
            if row["state"] != "closing" and row["notification"] != "pending":
                continue
            try:
                with self._flight_locked(row["id"]):
                    if row["state"] == "closing":
                        row["state"] = "paused"
                        changed = True
                    if row["notification"] == "pending":
                        row["notification"] = "unknown"
                        changed = True
                    recovered.append(row["feature"])
            except IncidentStoreBusy:
                # The exact sender still owns its lease, possibly in another
                # instance. Its durable pending intent remains authoritative.
                continue
        if changed:
            self._write(value)
            for feature in recovered:
                self._closing.discard(feature)
                self._busy.discard(feature)

    def _read(self, *, check_sentinel=True):
        if check_sentinel:
            self._check_sentinel()
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("incident_store_missing")
        if not 0 < self.path.stat().st_size <= 512 * 1024:
            raise ValueError("incident_store_size")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) != {"version", "incidents"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["incidents"], list)
                or len(value["incidents"]) > MAX_INCIDENTS):
            raise ValueError("incident_store_invalid")
        ids, active = set(), set()
        for row in value["incidents"]:
            if (not isinstance(row, dict) or set(row) != _ROW_KEYS
                    or not isinstance(row["id"], str) or not _ID.fullmatch(row["id"])
                    or row["id"] in ids or row["feature"] not in FEATURES
                    or row["category"] not in CATEGORIES or row["stage"] not in STAGES
                    or row["state"] not in {"closing", "paused", "resolved"}
                    or row["notification"] not in {"pending", "sent", "unknown", "not_requested"}
                    or not _stamp(row["created_at"]) or row["created_at"] <= 0
                    or not _stamp(row["resolved_at"])
                    or not isinstance(row["event_digest"], str)
                    or (row["event_digest"] and not _HASH.fullmatch(row["event_digest"]))
                    or not isinstance(row["message_id"], str)
                    or row["receipt_kind"] not in {"", "native"}
                    or (row["notification"] == "sent") != bool(row["message_id"])
                    or (row["notification"] == "sent") != (row["receipt_kind"] == "native")
                    or (row["message_id"] and not _MESSAGE_ID.fullmatch(row["message_id"]))
                    or (row["state"] == "resolved") != bool(row["resolved_at"])):
                raise ValueError("incident_store_invalid")
            ids.add(row["id"])
            if row["state"] != "resolved":
                if row["feature"] in active:
                    raise ValueError("incident_store_conflict")
                active.add(row["feature"])
        return value

    def _write(self, value):
        if self.path.is_symlink():
            raise OSError("incident_store_invalid")
        temp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def init(self):
        """Create or load a ledger. Never let a damaged ledger abort startup."""
        try:
            with self._locked():
                ledger_exists = self.path.exists() or self.path.is_symlink()
                sentinel = self._sentinel_path()
                sentinel_exists = sentinel.exists() or sentinel.is_symlink()
                if not sentinel_exists:
                    if ledger_exists:
                        # Only migrate a complete, valid pre-sentinel ledger.
                        self._read(check_sentinel=False)
                    elif self._has_prior_incident_artifacts():
                        raise ValueError("incident_store_missing")
                    self._install_sentinel()
                else:
                    self._check_sentinel()
                if not ledger_exists:
                    if sentinel_exists:
                        raise ValueError("incident_store_missing")
                    self._write({"version": 1, "incidents": []})
                value = self._read()
                self._reconcile_pending(value)
            self._store_error = "store_full" if len(value["incidents"]) >= MAX_INCIDENTS else ""
            return not bool(self._store_error)
        except IncidentStoreBusy:
            # An old live process may still be finishing a notice. Read the
            # complete atomic snapshot without converting its pending row.
            try:
                value = self._read()
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                self._store_error = "store_busy"
                return False
            self._store_error = "store_full" if len(value["incidents"]) >= MAX_INCIDENTS else ""
            return not bool(self._store_error)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._store_error = "store_unavailable"
            return False

    def resume_barrier(self, features):
        """Return the latest resolved boundary, or None when it is unavailable."""
        snapshot = self.resume_snapshot(features)
        return None if snapshot is None else snapshot[0]

    def resume_snapshot(self, features):
        """Return a boundary and incident generation from one validated read."""
        if (self._store_error or not isinstance(features, (tuple, list)) or not features
                or any(not isinstance(feature, str) or feature not in FEATURES for feature in features)):
            return None
        try:
            value = self._read()
            if len(value["incidents"]) >= MAX_INCIDENTS:
                return None
            rows = [row for row in value["incidents"] if row["feature"] in features]
            boundary = max((row["resolved_at"] for row in rows if row["state"] == "resolved"), default=0.0)
            # A scoped trial never admits an old queued source. Its original
            # paused incident remains in the generation until terminal success.
            for row in rows:
                if row["state"] == "paused" and self._retry_store.allows_event(row["id"]):
                    boundary = max(boundary, row["created_at"])
            generation = tuple((row["id"], row["state"], row["resolved_at"]) for row in rows)
            return boundary, generation
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def blocked(self, feature):
        if not isinstance(feature, str) or feature not in FEATURES or self._store_error or feature in self._closing:
            return True
        try:
            if self._marker_path(feature).exists() or self._marker_path(feature).is_symlink():
                return True
            value = self._read()
            if feature == "heybox_comment" and self._heybox_rejection_gate(value)["blocked"]:
                return True
            if len(value["incidents"]) >= MAX_INCIDENTS:
                self._store_error = "store_full"
                return True
            active = [row for row in value["incidents"]
                      if row["feature"] == feature and row["state"] != "resolved"]
            if active:
                return not (len(active) == 1 and active[0]["state"] == "paused"
                            and self._retry_store.allows_event(active[0]["id"])
                            and self._retry_current(active[0]["id"]) is not None)
            # If the process stopped after accepting a trial but before its
            # journal settled, a resolved incident is not enough to reopen it.
            if (feature in {"heybox_vision", "heybox_comment", "bili_journal"}
                    and self._retry_store.installed() and self._retry_store.unsettled_feature(feature)):
                return True
            return self._marker_path(feature).exists() or self._marker_path(feature).is_symlink()
        except RetryUnavailable:
            # Trial corruption fails closed for this query without poisoning
            # the separate incident ledger or unrelated features.
            return True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._store_error = "store_unavailable"
            return True

    def status(self, feature=None):
        if feature is not None and (not isinstance(feature, str) or feature not in FEATURES):
            return {"ok": False, "store_error": "invalid_feature", "active": {}}
        if self._store_error:
            return {"ok": False, "store_error": self._store_error, "active": {},
                    "protected_features_blocked": True}
        try:
            value = self._read()
            if len(value["incidents"]) >= MAX_INCIDENTS:
                self._store_error = "store_full"
                return {"ok": False, "store_error": "store_full", "active": {},
                        "protected_features_blocked": True}
            active = {row["feature"]: dict(row) for row in value["incidents"]
                      if row["state"] != "resolved" and (feature is None or row["feature"] == feature)}
            invalid_markers = []
            for name in (FEATURES if feature is None else (feature,)):
                try:
                    marker = self._marker(name)
                    if marker and name not in active:
                        active[name] = {**marker, "state": "waiting_for_store", "notification": "not_attempted"}
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    invalid_markers.append(name)
            issue = "marker_unavailable" if invalid_markers else "store_busy" if self._busy else ""
            return {"ok": not bool(issue), "store_error": issue, "marker_errors": invalid_markers,
                    "active": active, "closing_features": sorted(self._closing),
                    "record_count": len(value["incidents"]), "max_records": MAX_INCIDENTS,
                    "heybox_rejection_gate": self._heybox_rejection_gate(value)}
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._store_error = "store_unavailable"
            return {"ok": False, "store_error": self._store_error, "active": {},
                    "protected_features_blocked": True}

    def _retry_current(self, incident_id):
        """Return the single matching paused, confirmed incident, or None."""
        if self._store_error or type(incident_id) is not str or not _ID.fullmatch(incident_id):
            return None
        try:
            if not self._retry_store.owns_generation():
                return None
            trial = self._retry_store.state(incident_id)
            if trial is None or trial["feature"] in self._closing or self._marker(trial["feature"]):
                return None
            rows = self._read()["incidents"]
            row = next((item for item in rows if item["id"] == incident_id), None)
            if (not row or row["feature"] != trial["feature"] or row["state"] != "paused"
                    or row["notification"] != "sent" or row["receipt_kind"] != "native"
                    or not row["message_id"]):
                return None
            group = ({"heybox_vision", "heybox_comment", "heybox_feed"}
                     if trial["scope"] == "heybox" else {"bili_journal"})
            if any(item["feature"] in group and item["state"] != "resolved"
                   and item["id"] != incident_id for item in rows):
                return None
            return trial
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, RetryUnavailable):
            return None

    def retry_activate_runtime(self):
        """Called only by the newly installed, current hook owner once."""
        try:
            return self._retry_store.installed() and self._retry_store.activate_runtime()
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return False

    def retry_ready(self, incident_id, failed_key, *, verified_terminal=False):
        """Arm only after the caller proves the old source terminal and unsent."""
        try:
            return (verified_terminal is True and self._retry_current(incident_id) is not None
                    and self._retry_store.ready(incident_id, failed_key))
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return False

    def retry_intake(self, scope):
        """A ready slot permits explicit source enumeration, never model/send."""
        try:
            candidate = self._retry_store.intake(scope)
            return candidate if candidate and self._retry_current(candidate["incident_id"]) else None
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return None

    def retry_intake_allowed(self, incident_id):
        trial = self._retry_current(incident_id)
        if trial is None:
            return False
        slot = self.retry_intake(trial["scope"])
        return bool(slot and slot["incident_id"] == incident_id)

    def retry_snapshot(self, incident_id, features):
        """Return the same generation/boundary as the eventual event scope.

        This only supports explicit source enumeration/selection. It grants no
        model or send permission and never changes ``blocked()``.
        """
        trial = self._retry_current(incident_id)
        if (trial is None or trial["generation"] != self._retry_store.generation
                or trial["phase"] not in {"ready", "reserved", "started"}
                or not isinstance(features, (tuple, list)) or not features
                or trial["feature"] not in features
                or any(not isinstance(feature, str) or feature not in FEATURES for feature in features)):
            return None
        try:
            value = self._read()
            if len(value["incidents"]) >= MAX_INCIDENTS:
                return None
            rows = [row for row in value["incidents"] if row["feature"] in features]
            original = next((row for row in rows if row["id"] == incident_id), None)
            if original is None or original["state"] != "paused":
                return None
            boundary = max([original["created_at"]] +
                [row["resolved_at"] for row in rows if row["state"] == "resolved"])
            return boundary, tuple((row["id"], row["state"], row["resolved_at"]) for row in rows)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def retry_reserve(self, incident_id, candidate_key, observed_at):
        """Platform validates source provenance and historical dedup first."""
        try:
            if not self.retry_intake_allowed(incident_id):
                return None
            return self._retry_store.reserve(incident_id, candidate_key, observed_at)
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return None

    def retry_release(self, incident_id, token, candidate_key, *, definitely_not_started=False):
        try:
            return (self._retry_current(incident_id) is not None and self._retry_store.release(
                incident_id, token, candidate_key, definitely_not_started=definitely_not_started))
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return False

    def retry_start(self, incident_id, token, candidate_key, event_key):
        try:
            return (self._retry_current(incident_id) is not None
                    and self._retry_store.start(incident_id, token, candidate_key, event_key))
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return False

    def retry_check(self, incident_id, token, candidate_key, event_key):
        try:
            return (self._retry_current(incident_id) is not None
                    and self._retry_store.check(incident_id, token, candidate_key, event_key))
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return False

    @contextmanager
    def retry_scope(self, incident_id, mode, *, token=None, candidate_key=None, event_key=None):
        if mode == "intake":
            if not self.retry_intake_allowed(incident_id):
                raise RetryUnavailable("intake_unavailable")
            # Intake grants no change to blocked(); explicit source enumeration
            # code must consult retry_intake_allowed, and cannot send/model.
            yield
            return
        if mode != "event" or not self.retry_check(incident_id, token, candidate_key, event_key):
            raise RetryUnavailable("event_unavailable")
        with self._retry_store.event_scope(incident_id, token, candidate_key, event_key):
            yield

    def _resolve_accepted_retry(self, incident_id):
        """Resolve only an already accepted durable trial, with F060 locks."""
        if self._retry_current(incident_id) is None:
            return False
        try:
            with self._locked():
                value = self._read()
                row = next((item for item in value["incidents"] if item["id"] == incident_id), None)
                trial = self._retry_store.state(incident_id)
                if (not row or row["state"] != "paused" or row["notification"] != "sent"
                        or not trial or trial["phase"] != "accepted"
                        or trial["generation"] != self._retry_store.generation or self._marker(row["feature"])):
                    return False
                with self._flight_locked(incident_id):
                    stamp = self.now()
                    if not _stamp(stamp) or stamp <= 0:
                        return False
                    row.update(state="resolved", resolved_at=stamp)
                    self._write(value)
            # An accepted-but-not-settled row keeps blocked() closed if this
            # final persistence fails or the process stops between the writes.
            return self._retry_store.settle(incident_id)
        except (IncidentStoreBusy, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, RetryUnavailable):
            return False

    async def retry_finish(self, incident_id, token, candidate_key, event_key, *, outcome):
        """Conclude exactly this item; uncertain second notices never resend."""
        try:
            if self._retry_current(incident_id) is None:
                return False
            row = self._retry_store.finish(incident_id, token, candidate_key, event_key, outcome)
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return False
        if row is None:
            return False
        if outcome == "success":
            return self._resolve_accepted_retry(incident_id)
        return await self._notify_retry_block(row)

    async def retry_abort_ready(self, incident_id, *, other_fault_notified=False):
        """Stop discovery; a separately notified fault suppresses duplicate notice."""
        try:
            if type(other_fault_notified) is not bool or self._retry_current(incident_id) is None:
                return False
            # Suppression is valid only if an independent Bili browsing fault
            # already owns the notice/pause, including an unknown receipt.
            if other_fault_notified:
                active = self.status("bili_browse").get("active", {}).get("bili_browse", {})
                if (active.get("state") not in {"closing", "paused"}
                        or active.get("notification") not in {"pending", "sent", "unknown"}):
                    return False
            row = self._retry_store.abort_ready(incident_id, notify=not other_fault_notified)
        except (OSError, ValueError, TypeError, RetryUnavailable):
            return False
        if row is None:
            return False
        if other_fault_notified:
            return True
        return await self._notify_retry_block(row)

    async def _notify_retry_block(self, row):
        """Deliver at most one independently persisted terminal trial notice."""
        notice_id = row["notice_id"]
        text, task, confirmed_id = _retry_failure_notice(row), asyncio.current_task(), ""
        try:
            self._inflight[notice_id] = (task, notice_id, text)
            from .peak_runtime import same_task_deadline
            async with same_task_deadline(40):
                result = await self.send_notice(notice_id, text)
            identifier = result.get("message_id") if isinstance(result, dict) else None
            if (isinstance(result, dict) and result.get("confirmed") is True
                    and result.get("transport") == "native"
                    and isinstance(identifier, (str, int)) and not isinstance(identifier, bool)
                    and _MESSAGE_ID.fullmatch(str(identifier))):
                confirmed_id = str(identifier)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._inflight.pop(notice_id, None)
            try:
                self._retry_store.finish_secondary_notice(notice_id, confirmed_id)
            except (OSError, ValueError, TypeError, RetryUnavailable):
                pass
        return False

    def notice_authorized(self, incident_id, text, task):
        """Authorize only report()'s live, same-task, exact fixed notice."""
        if isinstance(incident_id, str) and incident_id.startswith("heybox-reject:"):
            if (not _HEYBOX_REJECTION_ID.fullmatch(incident_id)
                    or self._store_error or task is None or task is not asyncio.current_task()):
                return False
            delegate = self._heybox_rejection_notices
            binding = self._heybox_rejection_notice_binding
            if (delegate is None or binding is None or binding[0] is not delegate
                    or binding[1] is not self.send_notice or delegate.send_notice is not self.send_notice):
                return False
            return delegate.notice_authorized(incident_id, text, task) is True
        current = self._inflight.get(incident_id)
        if (self._store_error not in {"", "store_full"} or task is None or task is not asyncio.current_task()
                or not isinstance(current, tuple) or len(current) != 3
                or current != (task, incident_id, text)):
            return False
        try:
            value = self._read()
            row = next((row for row in value["incidents"] if row["id"] == incident_id), None)
            if row and row["state"] == "closing" and row["notification"] == "pending":
                if text == _notice(row):
                    return True
                return bool(text == _retry_notice(row) and self._retry_store.planned_notice(incident_id))
            # A second notice has a fresh durable ID; it never reuses the
            # original incident's native delivery receipt or repeats pending.
            trial = self._retry_store.secondary_notice(incident_id)
            original = next((item for item in value["incidents"]
                             if trial and item["id"] == trial["incident_id"]), None)
            return bool(trial and trial["phase"] == "blocked" and original
                        and original["state"] == "paused" and text == _retry_failure_notice(trial))
        except RetryUnavailable:
            return False
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._store_error = "store_unavailable"
            return False

    def _heybox_rejection_gate(self, value):
        try:
            from .heybox_rejection_notice import rejection_notice_gate
            return rejection_notice_gate(self.path.with_name("heybox-rejection-notices.sqlite"), value["incidents"])
        except Exception:
            return {"ok": False, "blocked": True, "unhanded_count": None, "reason": "rejection_store_unavailable"}

    def heybox_rejection_record(self, event_id, outgoing_id):
        delegate = self._heybox_rejection_notices
        binding = self._heybox_rejection_notice_binding
        if delegate is None or binding is None or binding[0] is not delegate:
            return None
        return delegate.find(event_id, outgoing_id)

    def acknowledge_heybox_rejection_pause(self, notice_id, incident_id):
        """Acknowledge only a matching durable F060 pause; never change receipt."""
        delegate = self._heybox_rejection_notices
        binding = self._heybox_rejection_notice_binding
        if (delegate is None or binding is None or binding[0] is not delegate
                or not isinstance(incident_id, str) or not _ID.fullmatch(incident_id)):
            return {"ok": False, "reason": "rejection_handoff_not_authorized"}
        try:
            value = self._read()
            incident = next((row for row in value["incidents"] if row["id"] == incident_id), None)
            return delegate.acknowledge_pause(notice_id, incident)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return {"ok": False, "reason": "rejection_handoff_unavailable"}

    async def report_heybox_rejection(self, event_id, outgoing_id, *, stage="reply_send"):
        """Record one confirmed content rejection without changing F060 rows.

        Only a ``sent`` native receipt permits the caller to continue new
        work. This method never resumes incidents or replays a prior item.
        Missing/unknown notice results must be paused by the caller, with
        notify=False once an attempted notice has an uncertain result.
        """
        if self._store_error:
            return {"ok": False, "isolated": False, "reason": self._store_error, "notification": "unknown"}
        try:
            delegate = self._heybox_rejection_notices
            if delegate is None:
                from .heybox_rejection_notice import HeyboxRejectionNotices
                delegate = HeyboxRejectionNotices(
                    self.path.with_name("heybox-rejection-notices.sqlite"), self.send_notice,
                    now=self.now, notice_ready=self.notice_ready)
                self._heybox_rejection_notices = delegate
                self._heybox_rejection_notice_binding = (delegate, self.send_notice)
                result = delegate.init()
                if result.get("ok") is not True:
                    return {"ok": False, "isolated": False, "reason": "rejection_store_unavailable", "notification": "unknown"}
            binding = self._heybox_rejection_notice_binding
            if (binding is None or binding[0] is not delegate or binding[1] is not self.send_notice
                    or delegate.send_notice is not self.send_notice):
                return {"ok": False, "isolated": False, "reason": "rejection_notice_binding_invalid", "notification": "unknown"}
            return await delegate.report(event_id, outgoing_id, stage=stage)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No exception text or external content crosses this boundary.
            return {"ok": False, "isolated": False, "reason": "rejection_notice_unavailable", "notification": "unknown"}

    async def report(self, feature, category, *, stage="", event_id="", notify=True, retry=None, _marker_seen=None):
        """Close this feature to new work, attempt one notice, then pause it.

        A prior active incident (including unknown delivery) is never resent.
        event_id is only hashed; no caller-supplied content is persisted.
        """
        if (not isinstance(feature, str) or feature not in FEATURES
                or not isinstance(category, str) or category not in CATEGORIES
                or not isinstance(stage, str) or stage not in STAGES):
            return {"ok": False, "reason": "invalid_incident"}
        event = str(event_id) if isinstance(event_id, (int, str)) and not isinstance(event_id, bool) else ""
        digest = hashlib.sha256(event.encode("utf-8")).hexdigest() if event and len(event) <= 256 else ""
        friend_digest = digest if feature == "friend_time_notice" else ""
        if not friend_digest and feature == "friend_time_notice" and isinstance(_marker_seen, dict):
            candidate = _marker_seen.get("event_digest", "")
            if isinstance(candidate, str) and _HASH.fullmatch(candidate):
                friend_digest = candidate
        self._closing.add(feature)
        if self._store_error:
            return {"ok": False, "reason": self._store_error, "blocked": True}
        flight = None
        flight_acquired = False
        retry_notice = False
        marker_seen = _marker_seen
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                with self._locked():
                    value = self._read()
                    try:
                        marker = self._marker(feature)
                    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                        self._busy.add(feature)
                        return {"ok": False, "reason": "marker_unavailable", "blocked": True}
                    if friend_digest:
                        prior_event = next((item for item in value["incidents"]
                                            if item["feature"] == feature
                                            and item["event_digest"] == friend_digest), None)
                        if prior_event:
                            if marker and marker.get("event_digest") == friend_digest:
                                self._clear_marker(feature, marker["id"])
                                marker = None
                            blocked = bool(marker or any(item["feature"] == feature and item["state"] != "resolved"
                                                         for item in value["incidents"]))
                            self._closing.discard(feature)
                            self._busy.discard(feature)
                            return {"ok": True, "reason": "already_recorded", "blocked": blocked,
                                    "incident_id": prior_event["id"], "notification": prior_event["notification"],
                                    "event_recorded": True}
                    existing = next((item for item in value["incidents"]
                                     if item["feature"] == feature and item["state"] != "resolved"), None)
                    if existing:
                        # An incident was committed before its marker was
                        # removed, or another error overlapped an active one.
                        if marker and not (feature == "friend_time_notice" and marker.get("event_digest")
                                           and marker.get("event_digest") != existing["event_digest"]):
                            try:
                                self._clear_marker(feature, marker["id"])
                            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                                pass  # The surviving marker continues to block.
                        self._closing.discard(feature)
                        self._busy.discard(feature)
                        return {"ok": True, "reason": "already_active", "blocked": True,
                                "incident_id": existing["id"], "notification": existing["notification"]}
                    if marker_seen:
                        prior = next((item for item in value["incidents"]
                                      if item["id"] == marker_seen["id"]), None)
                        if prior:
                            # A different instance adopted and even resolved
                            # this very marker while this task was waiting.
                            if marker and marker["id"] == marker_seen["id"]:
                                self._clear_marker(feature, marker["id"])
                            self._closing.discard(feature)
                            self._busy.discard(feature)
                            return {"ok": True, "reason": "already_resolved",
                                    "blocked": bool(marker and marker["id"] != marker_seen["id"]),
                                    "incident_id": prior["id"], "notification": prior["notification"]}
                        if not marker or marker["id"] != marker_seen["id"]:
                            self._busy.add(feature)
                            return {"ok": False, "reason": "marker_changed", "blocked": True,
                                    "incident_id": marker_seen["id"]}
                    if len(value["incidents"]) >= MAX_INCIDENTS:
                        self._store_error = "store_full"
                        return {"ok": False, "reason": self._store_error, "blocked": True}
                    if marker:
                        incident_id, category, stage = marker["id"], marker["category"], marker["stage"]
                        stamp, notify, digest = marker["created_at"], marker["notify"], marker.get("event_digest", "")
                    else:
                        incident_id, stamp = uuid.uuid4().hex, self.now()
                        if not _stamp(stamp) or stamp <= 0:
                            raise ValueError("invalid_clock")
                    if notify is True and self.notice_ready is not None:
                        try:
                            ready = callable(self.notice_ready) and self.notice_ready() is True
                        except Exception:
                            ready = False
                        if not ready:
                            # No send was attempted. Keep the original marker so
                            # recovery may make the first attempt when ready.
                            try:
                                marker = marker or self._put_marker(
                                    feature, category, stage, True,
                                    **({"event_digest": friend_digest} if friend_digest else {}))
                            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                                self._busy.add(feature)
                                return {"ok": False, "reason": "marker_unavailable", "blocked": True}
                            self._closing.discard(feature)
                            self._busy.discard(feature)
                            return {"ok": False, "reason": "notice_route_unavailable", "blocked": True,
                                    "incident_id": marker["id"], "notification": "not_attempted",
                                    **({"event_recorded": bool(friend_digest and marker.get("event_digest") == friend_digest)}
                                       if feature == "friend_time_notice" else {})}
                    row = {"id": incident_id, "feature": feature, "category": category,
                           "stage": stage, "event_digest": digest, "state": "closing",
                           "notification": "pending" if notify is True else "not_requested",
                           "created_at": stamp, "resolved_at": 0, "message_id": "", "receipt_kind": ""}
                    # Claim this lease while the ledger lock still excludes
                    # another initializer; retain it across the notice await.
                    flight = self._flight_locked(row["id"])
                    flight.__enter__()
                    flight_acquired = True
                    value["incidents"].append(row)
                    self._write(value)
                    if notify is True and retry is not None:
                        try:
                            # The incident is already durably closing. A
                            # missing/bad trial store simply keeps F060.
                            retry_notice = self._retry_store.prepare(row, retry) is True
                        except (OSError, ValueError, TypeError, RetryUnavailable):
                            retry_notice = False
                    if marker:
                        try:
                            self._clear_marker(feature, marker["id"])
                        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                            # The ledger is authoritative. A surviving marker is
                            # harmless and can be cleared on a later recovery.
                            pass
                    self._busy.discard(feature)
                break
            except IncidentStoreBusy:
                if flight_acquired:
                    flight.__exit__(None, None, None)
                    flight_acquired = False
                self._busy.add(feature)
                if marker_seen is None:
                    try:
                        # Install before the first await, including when the
                        # reporting task is subsequently cancelled or killed.
                        marker_seen = self._put_marker(
                            feature, category, stage, notify,
                            **({"event_digest": friend_digest} if friend_digest else {}))
                    except IncidentStoreBusy:
                        pass  # A concurrent adoption changed the marker.
                    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                        return {"ok": False, "reason": "marker_unavailable", "blocked": True}
                    except OSError:
                        self._store_error = "store_unavailable"
                        return {"ok": False, "reason": self._store_error, "blocked": True}
                if attempt + 1 == _LOCK_ATTEMPTS:
                    return {"ok": False, "reason": "store_busy", "blocked": True,
                            "incident_id": marker_seen["id"] if marker_seen else "",
                            **({"event_recorded": bool(friend_digest and marker_seen
                                                        and marker_seen.get("event_digest") == friend_digest)}
                               if feature == "friend_time_notice" else {})}
                await asyncio.sleep(_LOCK_RETRY_SECONDS)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                if flight_acquired:
                    flight.__exit__(None, None, None)
                self._store_error = "store_unavailable"
                return {"ok": False, "reason": self._store_error, "blocked": True}

        notice_state, message_id = ("unknown", "") if notify is True else ("not_requested", "")
        persisted = False
        task = asyncio.current_task()
        try:
            if notify is True:
                text = _retry_notice(row) if retry_notice else _notice(row)
                self._inflight[row["id"]] = (task, row["id"], text)
                # Keep the owner-notice grant on this task on Python 3.10/3.12.
                from .peak_runtime import same_task_deadline
                async with same_task_deadline(40):
                    result = await self.send_notice(row["id"], text)
                identifier = result.get("message_id") if isinstance(result, dict) else None
                if (isinstance(result, dict) and result.get("confirmed") is True
                        and result.get("transport") == "native"
                        and isinstance(identifier, (str, int)) and not isinstance(identifier, bool)
                        and _MESSAGE_ID.fullmatch(str(identifier))):
                    notice_state, message_id = "sent", str(identifier)
                else:
                    record_failure(self.path.parent, feature, "incident_notice_send",
                                   reason="receipt_missing" if identifier is None else "receipt_invalid")
        except asyncio.CancelledError:
            record_failure(self.path.parent, feature, "incident_notice_send", reason="notice_cancelled")
            raise
        except Exception as error:
            record_failure(self.path.parent, feature, "incident_notice_send", error=error)
        finally:
            self._inflight.pop(row["id"], None)
            try:
                for attempt in range(_LOCK_ATTEMPTS):
                    try:
                        with self._locked():
                            value = self._read()
                            current = next((item for item in value["incidents"] if item["id"] == row["id"]), None)
                            if current is None or current["state"] == "resolved":
                                raise ValueError("incident_state_changed")
                            current.update(state="paused", notification=notice_state, message_id=message_id,
                                           receipt_kind="native" if notice_state == "sent" else "")
                            self._write(value)
                        persisted = True
                        self._closing.discard(feature)
                        self._busy.discard(feature)
                        break
                    except IncidentStoreBusy:
                        self._busy.add(feature)
                        if attempt + 1 < _LOCK_ATTEMPTS:
                            await asyncio.sleep(_LOCK_RETRY_SECONDS)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                self._store_error = "store_unavailable"
            finally:
                if flight_acquired:
                    flight.__exit__(None, None, None)
            if retry_notice:
                try:
                    # A missing/ambiguous notification never opens a trial.
                    self._retry_store.first_notice(row["id"], persisted and notice_state == "sent")
                except (OSError, ValueError, TypeError, RetryUnavailable):
                    pass
        error = self._store_error or ("store_busy" if feature in self._busy else "")
        return {"ok": not bool(error), "reason": error or "paused",
                "blocked": True, "incident_id": row["id"],
                "notification": notice_state if persisted else "pending",
                **({"event_recorded": bool(friend_digest and row["event_digest"] == friend_digest)}
                   if feature == "friend_time_notice" else {})}

    async def recover_markers(self):
        """Adopt blocked markers and reconcile abandoned intents, without resending.

        The caller may schedule this periodically. Only a marker never yet
        committed to the main ledger can start its first owner notification.
        """
        if self._store_error == "store_busy":
            self.init()
        if self._store_error:
            return {"ok": False, "reason": self._store_error, "results": {}}
        try:
            with self._locked():
                value = self._read()
                self._reconcile_pending(value)
                for feature in tuple(self._busy):
                    if (not self._marker_path(feature).exists()
                            and not self._marker_path(feature).is_symlink()
                            and any(row["feature"] == feature for row in value["incidents"])):
                        self._busy.discard(feature)
                        self._closing.discard(feature)
        except IncidentStoreBusy:
            return {"ok": False, "reason": "store_busy", "results": {}}
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._store_error = "store_unavailable"
            return {"ok": False, "reason": self._store_error, "results": {}}
        results = {}
        for feature in FEATURES:
            try:
                marker = self._marker(feature)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                results[feature] = {"ok": False, "reason": "marker_unavailable", "blocked": True}
                continue
            if marker:
                results[feature] = await self.report(feature, marker["category"], stage=marker["stage"],
                                                     notify=marker["notify"], _marker_seen=marker)
        return {"ok": all(result["ok"] for result in results.values()), "results": results}

    def resolve(self, feature, incident_id, *, actor=""):
        """Clear only the named active incident after owner authentication.

        Callers must authenticate an actual owner private event or local API;
        actor is a routing label, never proof of identity by itself.
        """
        if (not isinstance(feature, str) or feature not in FEATURES
                or not isinstance(incident_id, str) or not _ID.fullmatch(incident_id)):
            return {"ok": False, "reason": "invalid_incident"}
        if not isinstance(actor, str) or actor not in {"owner", "owner_api"}:
            return {"ok": False, "reason": "owner_required"}
        if self._store_error or feature in self._closing:
            return {"ok": False, "reason": self._store_error or "notice_in_progress"}
        try:
            with self._locked():
                try:
                    marker = self._marker(feature)
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    return {"ok": False, "reason": "marker_unavailable"}
                if marker:
                    return {"ok": False, "reason": "marker_pending", "incident_id": marker["id"]}
                value = self._read()
                row = next((item for item in value["incidents"] if item["id"] == incident_id), None)
                if not row or row["feature"] != feature:
                    return {"ok": False, "reason": "incident_not_active"}
                try:
                    with self._flight_locked(incident_id):
                        if row["state"] != "paused":
                            return {"ok": False, "reason": "incident_not_active"}
                        stamp = self.now()
                        if not _stamp(stamp) or stamp <= 0:
                            raise ValueError("invalid_clock")
                        try:
                            if self._retry_store.installed():
                                trial = self._retry_store.state(incident_id)
                                if (trial and trial["phase"] != "settled"
                                        and not self._retry_store.owner_resolved(incident_id)):
                                    return {"ok": False, "reason": "retry_generation_changed"}
                        except RetryUnavailable:
                            return {"ok": False, "reason": "retry_store_unavailable"}
                        row.update(state="resolved", resolved_at=stamp)
                        self._write(value)
                except IncidentStoreBusy:
                    return {"ok": False, "reason": "notice_in_progress"}
            return {"ok": True, "reason": "resolved", "incident_id": incident_id}
        except IncidentStoreBusy:
            return {"ok": False, "reason": "store_busy"}
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._store_error = "store_unavailable"
            return {"ok": False, "reason": self._store_error}


async def send_incident_notice(host, incident_id, text):
    """Delegate only to the current protected native owner-private producer."""
    handle = getattr(host, "_native_private_handle", None)
    if handle is None or not callable(getattr(handle, "send_incident_notice", None)):
        raise RuntimeError("incident_native_route_unavailable")
    return await handle.send_incident_notice(incident_id, text)
