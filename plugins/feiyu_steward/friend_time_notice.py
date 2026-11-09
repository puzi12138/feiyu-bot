"""Single fixed friend notices at verified time-window transitions."""

import asyncio
import errno
import math
import json
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

from . import qq_friend_policy as friend
from .failure_diagnostics import record_failure
from .token_pricing import TZ, _peak


FEATURE = "friend_time_notice"
STAGE = "friend_time_send"
TEXT = {
    "day": "闲下来啦，来跟你打个招呼～",
    "peak": "我先去忙啦，闲下来再聊～",
    "night": "我先睡觉啦，明天再聊～",
}
_MAX_LATENESS = 120
_QQ_ONLINE_MONITOR = Path("D:/QQBot/logs/qq-online-monitor/latest.json")
_QQ_ONLINE_MAX_AGE = 90
_MARKER = b"friend-time-notice-v1\n"
_SCOPE = ContextVar("feiyu_friend_time_notice_scope", default=None)


class NoticeLockBusy(OSError):
    """Another process currently owns this independent notice ledger."""


def time_window(at):
    """Return one verified continuous window and its fixed message kind."""
    if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
        return None
    try:
        current = datetime.fromtimestamp(at, TZ)
        tariff = _peak(at)
        if tariff is not True and tariff is not False:
            return None
        midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
        if current.hour < 8:
            start, end, kind = midnight - timedelta(hours=2), midnight + timedelta(hours=8), "night"
        elif current.hour >= 22:
            start, end, kind = midnight + timedelta(hours=22), midnight + timedelta(days=1, hours=8), "night"
        else:
            morning = _peak((midnight + timedelta(hours=10)).timestamp())
            afternoon = _peak((midnight + timedelta(hours=15)).timestamp())
            if morning is False and afternoon is False and tariff is False:
                start, end, kind = midnight + timedelta(hours=8), midnight + timedelta(hours=22), "day"
            elif morning is True and afternoon is True:
                if current.hour < 9 and tariff is False:
                    first, last, kind = 8, 9, "day"
                elif current.hour < 12 and tariff is True:
                    first, last, kind = 9, 12, "peak"
                elif current.hour < 14 and tariff is False:
                    first, last, kind = 12, 14, "day"
                elif current.hour < 18 and tariff is True:
                    first, last, kind = 14, 18, "peak"
                elif tariff is False:
                    first, last, kind = 18, 22, "day"
                else:
                    return None
                start, end = midnight + timedelta(hours=first), midnight + timedelta(hours=last)
            else:
                return None
        begin, finish = int(start.timestamp()), int(end.timestamp())
        if not begin <= at < finish:
            return None
        # The two endpoints can fall in different calendar years at night.
        if _peak(begin) is None or _peak(finish - 1) is None:
            return None
        return (begin, finish, kind)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


@contextmanager
def _file_lock(path):
    """Hold an operating-system lock through reservation and one send."""
    path = Path(path)
    if path.is_symlink():
        raise OSError("friend_notice_lock_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
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
                raise NoticeLockBusy("friend_notice_lock_busy") from None
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


class NoticeStore:
    """A separate no-content ledger; callers hold its operating-system lock."""

    def __init__(self, path):
        self.path = Path(path)
        self.marker = self.path.with_name(self.path.name + ".initialized")
        self.lock = self.path.with_name(self.path.name + ".lock")

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or self.marker.is_symlink():
            raise OSError("friend_notice_store_invalid")
        exists, marked = self.path.is_file(), self.marker.is_file()
        if not exists and not marked:
            fd = os.open(str(self.marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(_MARKER)
                stream.flush()
                os.fsync(stream.fileno())
            with closing(sqlite3.connect(self.path, timeout=1)) as db:
                with db:
                    db.execute("CREATE TABLE meta (version INTEGER NOT NULL, highwater REAL NOT NULL)")
                    db.execute("INSERT INTO meta VALUES (1, 0)")
                    db.execute("CREATE TABLE windows (start INTEGER PRIMARY KEY, finish INTEGER NOT NULL, "
                               "kind TEXT NOT NULL, state TEXT NOT NULL, observed REAL NOT NULL, "
                               "receipt TEXT NOT NULL, incident_pending INTEGER NOT NULL)")
            exists, marked = True, True
        if (not exists or not marked or self.marker.stat().st_size != len(_MARKER)
                or self.marker.read_bytes() != _MARKER):
            raise OSError("friend_notice_store_missing")
        db = sqlite3.connect(self.path, timeout=1)
        try:
            meta = db.execute("SELECT version, highwater FROM meta").fetchall()
            if (len(meta) != 1 or meta[0][0] != 1 or type(meta[0][1]) not in (int, float)
                    or not math.isfinite(meta[0][1]) or meta[0][1] < 0
                    or db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",)):
                raise ValueError("friend_notice_store_invalid")
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _recover_pending(db):
        # The global OS lock spans reservation and send. Once a new observer
        # holds it, every surviving pending row belongs to an interrupted
        # sender, including when this observer already seeded its own view.
        db.execute("UPDATE windows SET state='unknown', incident_pending=1 WHERE state='pending'")
        return db.execute("SELECT 1 FROM windows WHERE incident_pending=1 LIMIT 1").fetchone() is not None

    def recover_pending(self):
        """Recover interrupted sends even when no calendar window is usable."""
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            unreported = self._recover_pending(db)
            db.commit()
            return unreported

    def observe(self, window, at, *, seed, transitioned, eligible):
        """Atomically reserve one observed window, or seed/skip it permanently."""
        start, finish, kind = window
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            highwater = db.execute("SELECT highwater FROM meta").fetchone()[0]
            unreported = self._recover_pending(db)
            row = db.execute("SELECT finish, kind, state FROM windows WHERE start=?", (start,)).fetchone()
            if row and (row[0] != finish or row[1] != kind
                        or row[2] not in {"seeded", "skipped", "pending", "snow_returned", "unknown"}):
                raise ValueError("friend_notice_window_invalid")
            rollback = at < highwater
            attempt = (not seed and transitioned and not rollback and not unreported and eligible
                       and 0 <= at - start < _MAX_LATENESS and row is None)
            state = "pending" if attempt else "seeded" if seed else "skipped"
            db.execute("INSERT OR IGNORE INTO windows VALUES (?, ?, ?, ?, ?, '', 0)",
                       (start, finish, kind, state, at))
            db.execute("UPDATE meta SET highwater=?", (max(at, highwater),))
            db.commit()
            return attempt, rollback

    def finish(self, start, receipt):
        """Record a Snow return or an unknown result without retrying."""
        returned = (isinstance(receipt, str) and re.fullmatch(r"-?[1-9][0-9]{0,9}", receipt) is not None
                    and -(2 ** 31) <= int(receipt) < 2 ** 31)
        with closing(self.connect()) as db:
            with db:
                cursor = db.execute(
                    "UPDATE windows SET state=?, receipt=?, incident_pending=? WHERE start=? AND state='pending'",
                    ("snow_returned" if returned else "unknown", receipt if returned else "",
                     0 if returned else 1, start),
                )
                if cursor.rowcount != 1:
                    raise ValueError("friend_notice_reservation_missing")
        return returned

    def incident_pending(self):
        with closing(self.connect()) as db:
            return bool(db.execute("SELECT 1 FROM windows WHERE incident_pending=1 LIMIT 1").fetchone())

    def first_incident(self):
        with closing(self.connect()) as db:
            row = db.execute("SELECT start FROM windows WHERE incident_pending=1 ORDER BY start LIMIT 1").fetchone()
        if row is None:
            return None
        if not isinstance(row[0], int) or isinstance(row[0], bool) or row[0] < 0:
            raise ValueError("friend_notice_window_invalid")
        return row[0]

    def clear_incident(self, start):
        with closing(self.connect()) as db:
            with db:
                db.execute("UPDATE windows SET incident_pending=0 WHERE start=? AND incident_pending=1", (start,))


def _includes(value, expected):
    seen = set()
    while callable(value) and id(value) not in seen and len(seen) < 32:
        if value is expected:
            return True
        seen.add(id(value))
        value = getattr(value, "__wrapped__", None)
    return False


def _current_runtime_client(runtime, host, client):
    """Require this host's active client guard in the actual call chain."""
    if getattr(runtime, "host", None) is not host or getattr(runtime, "_active", None) is not True:
        return False
    for obj, _, wrapped in runtime._client_bindings:
        state = getattr(wrapped, "_peak_runtime_state", None)
        lifetime = state.get("active") if isinstance(state, dict) else None
        if (obj is client and isinstance(state, dict) and state.get("owner") is runtime
                and state.get("wrapped") is wrapped and isinstance(lifetime, dict)
                and lifetime.get("active") is True and _includes(client.call_action, wrapped)):
            return True
    return False


def _resume_snapshot(host, at):
    """Return the current notice generation without reviving an old window."""
    try:
        policy = host.incidents
        if policy.blocked(FEATURE) is not False:
            return None
        value = policy.resume_snapshot((FEATURE,))
        if (not isinstance(value, tuple) or len(value) != 2
                or type(value[0]) not in (int, float) or not math.isfinite(value[0])
                or not 0 <= value[0] <= at or not isinstance(value[1], tuple)):
            return None
        return value
    except Exception:
        return None


def _current_route_evidence(host, client, at):
    """Require recent health evidence and the pinned local QQ generation."""
    try:
        if type(at) not in (int, float) or not math.isfinite(at):
            return False
        path = _QQ_ONLINE_MONITOR
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        sample = value.get("sample") if isinstance(value, dict) else None
        if value.get("state") != "healthy" or not isinstance(sample, dict):
            return False
        checked_text = sample.get("checked_at_utc")
        created_text = sample.get("expected_created_utc")
        if (type(checked_text) is not str or len(checked_text) > 40
                or type(created_text) is not str or len(created_text) > 40):
            return False
        checked_time = datetime.fromisoformat(checked_text.replace("Z", "+00:00"))
        created_time = datetime.fromisoformat(created_text.replace("Z", "+00:00"))
        if checked_time.tzinfo is None or created_time.tzinfo is None:
            return False
        checked, created = checked_time.timestamp(), created_time.timestamp()
        if checked > at + 2 or at - checked > _QQ_ONLINE_MAX_AGE:
            return False
        pid = sample.get("expected_pid")
        expected_exe = os.path.normcase(os.path.abspath("D:/QQBot/apps/QQ/QQ.exe"))
        if (type(pid) is not int or not 0 < pid < 2 ** 32 or not 0 < created <= checked
                or type(sample.get("expected_exe")) is not str
                or os.path.normcase(os.path.abspath(sample["expected_exe"])) != expected_exe):
            return False
        if not all(sample.get(key) is True for key in (
            "healthy", "same_generation", "native_online", "snow_online",
            "native_identity_match", "snow_identity_match",
            "native_listener_owner_match", "snow_listener_verified", "astrbot_listening")):
            return False
        # The monitor can be up to 90 seconds old. Recheck its pinned process
        # and current native listener locally; cached health is not live status.
        import psutil
        try:
            process = psutil.Process(pid)
            if (abs(process.create_time() - created) >= .001
                    or os.path.normcase(os.path.abspath(process.exe())) != expected_exe):
                return False
            native = [row for row in psutil.net_connections(kind="tcp")
                      if row.status == psutil.CONN_LISTEN and row.laddr.port == 16199]
            if (len(native) != 1 or native[0].pid != pid
                    or native[0].laddr.ip != "127.0.0.1"):
                return False
        except (psutil.Error, OSError):
            return False
        adapter = host.context.get_platform_inst("snowluma-qq")
        if adapter is None or adapter.get_client() is not client:
            return False
        from aiocqhttp import CQHttp
        from aiocqhttp.api_impl import UnifiedApi, WebSocketReverseApi
        if not isinstance(client, CQHttp):
            return False
        mapping = vars(client).get("_wsr_api_clients")
        api = vars(client).get("_api")
        return (type(mapping) is dict and len(mapping) == 1 and mapping.get(friend.SELF) is not None
                and isinstance(api, UnifiedApi) and isinstance(api._wsr_api, WebSocketReverseApi)
                and api._wsr_api._api_clients is mapping)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
        return False

def notice_client(host, window, at):
    """Check the current policy, incident guard, platform and wrapper chain."""
    try:
        snapshot = _resume_snapshot(host, at)
        if (getattr(host, "_friend_time_notice_stopping", True)
                or time_window(at) != window or snapshot is None
                or (snapshot[0] and window[0] <= snapshot[0])):
            return None
        policy, pause = friend.read_friend_policy(), friend.read_qq_chat_pause()
        if (not policy.get("valid") or not policy.get("enabled")
                or window[0] < policy["effective_from"]
                or not pause["valid"] or not pause["present"] or not pause["paused"]
                or not friend._guards_current()):
            return None
        interval = pause["intervals"][-1]
        if window[0] < math.ceil(interval.get("start_exact", interval["start"])):
            return None
        guards = host._friend_guard_restore
        runtime, hooks = host._peak_runtime_handle, host._incident_hooks
        if (not guards or guards.is_current() is not True or runtime.status().get("ready") is not True
                or hooks is None or hooks.status().get("ready") is not True
                or hooks._blocked(FEATURE)):
            return None
        adapter = host.context.get_platform_inst("snowluma-qq")
        client = adapter.get_client() if adapter else None
        reconnect, outbound = host._reconnect_guard, host._group_outbound_restore
        if (not reconnect or reconnect[0] is not adapter or reconnect[3] is not client
                or not client or not outbound or not _includes(client.call_action, outbound.wrapped)
                or not _current_runtime_client(runtime, host, client)
                or not _current_route_evidence(host, client, at)):
            return None
        return client
    except Exception:
        return None


def notice_scope_present():
    return _SCOPE.get() is not None


def notice_action_allowed(action, params, *, client=None, claim=False):
    """Authorize only this task's reserved fixed text to the exact client."""
    scope = _SCOPE.get()
    try:
        task = asyncio.current_task()
        host, window = scope["host"], scope["window"]
        if (not scope["active"] or task is None or task is not scope["task"]
                or task is not getattr(host, "_friend_time_notice_task", None)
                or getattr(task, "cancelling", lambda: 0)()
                or getattr(host, "_friend_time_notice_capability", None) is not scope
                or scope.get("generation") is None
                or _resume_snapshot(host, time.time()) != scope["generation"]
                or not 0 <= time.time() - window[0] < _MAX_LATENESS
                or notice_client(host, window, time.time()) is not scope["client"]
                or (client is not None and client is not scope["client"])
                or action != "send_private_msg" or not isinstance(params, dict)
                or set(params) != {"user_id", "self_id", "message"}
                or str(params["user_id"]) != friend.FRIEND or str(params["self_id"]) != friend.SELF
                or params["message"] != [{"type": "text", "data": {"text": TEXT[window[2]]}}]):
            return False
        if claim:
            if scope["claimed"]:
                return False
            scope["claimed"] = True
        return True
    except Exception:
        return False


async def _report_pending(host, store, category, *, fallback_start=None):
    """Transfer one unknown window to the durable, idempotent incident route."""
    hooks = getattr(host, "_incident_hooks", None)
    if hooks is None:
        return
    try:
        with _file_lock(store.lock):
            store.recover_pending()
            start = store.first_incident()
    except (NoticeLockBusy, OSError, ValueError, sqlite3.Error):
        start = fallback_start
    if start is None:
        if category != "delivery_unknown":
            await hooks._report(FEATURE, category, STAGE)
        return
    try:
        result = await host.incidents.report(FEATURE, category, stage=STAGE,
                                             event_id=f"{FEATURE}:{start}")
        if isinstance(result, dict) and result.get("event_recorded") is True:
            try:
                with _file_lock(store.lock):
                    store.clear_incident(start)
            except (NoticeLockBusy, OSError, ValueError, sqlite3.Error):
                pass
        elif not isinstance(result, dict) or result.get("blocked") is not True:
            local = getattr(hooks, "_local_blocked", None)
            if isinstance(local, set):
                local.add(FEATURE)
    except Exception:
        local = getattr(hooks, "_local_blocked", None)
        if isinstance(local, set):
            local.add(FEATURE)


async def friend_time_notice_monitor(host):
    """Observe actual time; startup and missed windows are never replayed."""
    task = asyncio.current_task()
    previous_task = getattr(host, "_friend_time_notice_task", None)
    if task is None or (previous_task is not None and not previous_task.done()):
        return
    host._friend_time_notice_task = task
    host._friend_time_notice_capability = None
    host._friend_time_notice_status = {"running": True, "guard_ready": False,
                                      "last_decision": "waiting", "attempts": 0,
                                      "snow_returned": 0}
    store = NoticeStore(host.root / "qq-friend-time-notice.sqlite")
    previous = None
    try:
        while not getattr(host, "_friend_time_notice_stopping", True):
            pending_incident = False
            failure = None
            attempt_start = None
            try:
                now = time.time()
                window = time_window(now)
                status = host._friend_time_notice_status
                if window is None:
                    previous = None
                    status.update(guard_ready=False, last_decision="calendar_unavailable")
                    with _file_lock(store.lock):
                        pending_incident = store.recover_pending()
                else:
                    with _file_lock(store.lock):
                        seed = previous is None
                        client = notice_client(host, window, now)
                        # A missing gate at the first observation permanently skips this window.
                        seed = seed and client is not None
                        status["guard_ready"] = client is not None
                        attempt, rollback = store.observe(window, now, seed=seed,
                                                          transitioned=previous != window[0],
                                                          eligible=client is not None)
                        previous = window[0]
                        pending_incident = store.incident_pending()
                        status["last_decision"] = ("seeded" if seed else "clock_rollback" if rollback
                                                   else "reserved" if attempt else "waiting")
                        if attempt:
                            attempt_start = window[0]
                            status["attempts"] += 1
                            cap = {"host": host, "client": client, "window": window, "task": task,
                                   "active": True, "claimed": False,
                                   "generation": _resume_snapshot(host, now)}
                            host._friend_time_notice_capability = cap
                            token = _SCOPE.set(cap)
                            receipt = ""
                            send_error = None
                            receipt_reason = "receipt_missing"
                            try:
                                # The helper uses cancellation of this task, not a child task.
                                from .peak_runtime import same_task_deadline
                                async with same_task_deadline(20):
                                    result = await client.call_action(
                                        "send_private_msg", user_id=int(friend.FRIEND), self_id=int(friend.SELF),
                                        message=[{"type": "text", "data": {"text": TEXT[window[2]]}}],
                                    )
                                value = result.get("message_id") if isinstance(result, dict) else None
                                if isinstance(value, int) and not isinstance(value, bool):
                                    receipt = str(value)
                                    receipt_reason = "receipt_invalid"
                                elif value is not None:
                                    receipt_reason = "receipt_invalid"
                            except Exception as error:
                                send_error = error
                                failure = "delivery_unknown"
                            finally:
                                cap["active"] = False
                                _SCOPE.reset(token)
                                host._friend_time_notice_capability = None
                                returned = store.finish(window[0], receipt)
                                if returned:
                                    status["snow_returned"] += 1
                                    status["last_decision"] = "snow_returned"
                                else:
                                    record_failure(host.root, FEATURE, STAGE, error=send_error,
                                                   reason=receipt_reason if send_error is None else None)
                                    pending_incident = True
                                    status["last_decision"] = "unknown"
                if pending_incident:
                    await _report_pending(host, store, "delivery_unknown")
            except NoticeLockBusy:
                host._friend_time_notice_status["last_decision"] = "lock_busy"
            except (OSError, ValueError, sqlite3.Error) as error:
                record_failure(host.root, FEATURE, "friend_time_monitor", error=error)
                failure = "storage_failed"
                host._friend_time_notice_status["last_decision"] = "storage_unavailable"
            except Exception as error:
                record_failure(host.root, FEATURE, "friend_time_monitor", error=error)
                failure = "unexpected_exception"
                host._friend_time_notice_status["last_decision"] = "failed"
            if failure and not pending_incident:
                await _report_pending(host, store, failure, fallback_start=attempt_start)
            await asyncio.sleep(60)
    finally:
        cap = getattr(host, "_friend_time_notice_capability", None)
        if isinstance(cap, dict):
            cap["active"] = False
        host._friend_time_notice_capability = None
        host._friend_time_notice_status["running"] = False
        if getattr(host, "_friend_time_notice_task", None) is task:
            host._friend_time_notice_task = None


def friend_time_notice_status(host):
    status = getattr(host, "_friend_time_notice_status", None)
    if not isinstance(status, dict):
        return {"running": False, "guard_ready": False, "last_decision": "not_started",
                "attempts": 0, "snow_returned": 0}
    return {key: status[key] for key in
            ("running", "guard_ready", "last_decision", "attempts", "snow_returned")}
