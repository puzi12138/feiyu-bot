"""Event-only F060 to Codex relay. This is a maintenance candidate, not a scheduler.

The legacy intake parser is imported for its whitelist projection only. Its scan,
acknowledgement, cursor and snapshot paths are never called or modified.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import uuid

THREAD = os.environ.get("FEIYU_CODEX_THREAD_ID", "").strip()
ID = re.compile(r"[a-f0-9]{32}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
FILES = frozenset({"incidents.json", "incident-new-content-trials.json"})
MARKER = b"codex-fault-event-relay-v1\n"
PHASES = {"baseline", "waiting", "claimed", "sending", "queued_unconfirmed", "unknown", "received_confirmed"}
FEATURE_NAMES = dict(zip(
    "heybox_vision heybox_comment heybox_feed bili_browse bili_journal bili_share heybox_autopost heybox_qzone_sync qzone_publish friend_time_notice unknown".split(),
    "小黑盒识图 小黑盒评论 小黑盒浏览 B站浏览 B站手账 B站分享 小黑盒投稿 小黑盒空间同步 空间发表 好友时段提醒 未知功能".split()))
CODE_NAMES = dict(zip(
    "vision_failed model_failed model_connection_failed model_timeout request_failed connection_failed service_unavailable rate_limited access_denied response_invalid request_rejected platform_content_rejected storage_failed delivery_unknown credential_invalid guard_failed unexpected_exception unknown".split(),
    "图片处理失败 模型处理失败 模型连接失败 模型超时 请求失败 连接失败 服务不可用 触发限流 权限拒绝 响应异常 请求拒绝 平台拒绝内容 持久化失败 发送结果未知 认证异常 安全保护阻断 未分类异常 未知原因".split()))


class RelayError(Exception):
    """Only fixed safe codes are exposed; external exception text is discarded."""


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RelayError("duplicate_key")
        result[key] = value
    return result


def parse(raw):
    return json.loads(raw, object_pairs_hook=pairs)


def safe_path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise RelayError("unsafe_path")
        try:
            # Python 3.10 lacks Path.is_junction. Windows exposes reparse bits
            # through lstat, so junctions cannot silently bypass that version.
            if part.lstat().st_file_attributes & 0x400:
                raise RelayError("unsafe_path")
        except (FileNotFoundError, AttributeError):
            pass
    return path


def read(path, limit):
    path = safe_path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise RelayError("file_unavailable")
    raw = path.read_bytes()
    if not 0 < len(raw) <= limit:
        raise RelayError("file_unavailable")
    return raw


def file_sha256(path, limit=1024 * 1024 * 1024, chunk_size=1024 * 1024):
    """Hash a bounded, stable regular file without loading it into memory.

    The official CLI can exceed 256 MiB. Keep binary identity verification
    separate from the small-JSON reader and bound both bytes and memory use.
    """
    path = safe_path(path)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise RelayError("file_unavailable")
        value, count = hashlib.sha256(), 0
        while True:
            block = stream.read(chunk_size)
            if not block:
                break
            count += len(block)
            if count > limit:
                raise RelayError("file_unavailable")
            value.update(block)
        after = os.fstat(stream.fileno())
    current = safe_path(path).stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if (count != before.st_size
            or any(getattr(before, key) != getattr(after, key) for key in fields)
            or any(getattr(after, key) != getattr(current, key) for key in fields)):
        raise RelayError("file_changing")
    return value.hexdigest()


def atomic(path, value):
    safe_path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Config:
    source: Path
    state_dir: Path
    parser_path: Path
    codex_exe: Path
    codex_sha256: str
    config_sha256: str = ""

    @classmethod
    def load(cls, path):
        if not UUID.fullmatch(THREAD):
            raise RelayError("thread_not_configured")
        raw = read(path, 16384)
        value = parse(raw)
        if (type(value) is not dict or set(value) != {"schema", "source_dir", "state_dir", "parser_path", "codex_exe", "codex_sha256"}
                or type(value["schema"]) is not int or value["schema"] != 1):
            raise RelayError("config_invalid")
        if any(type(value[k]) is not str or not Path(value[k]).is_absolute()
               for k in ("source_dir", "state_dir", "parser_path", "codex_exe")):
            raise RelayError("config_invalid")
        if type(value["codex_sha256"]) is not str or not HEX.fullmatch(value["codex_sha256"]):
            raise RelayError("config_invalid")
        return cls(*(safe_path(value[k]) for k in ("source_dir", "state_dir", "parser_path", "codex_exe")),
                   value["codex_sha256"], hashlib.sha256(raw).hexdigest())


class QueueSender:
    def __init__(self, config):
        self.config = config

    def __call__(self, message):
        if not UUID.fullmatch(THREAD):
            raise RelayError("thread_not_configured")
        exe = self.config.codex_exe
        if exe.suffix.lower() != ".exe" or file_sha256(exe) != self.config.codex_sha256:
            raise RelayError("cli_identity_changed")
        result = subprocess.run(
            [str(exe), "queue", "--thread", THREAD, "--message", message],
            shell=False, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=30, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or result.stderr or len(result.stdout) > 1024:
            raise RelayError("queue_result_unknown")
        try:
            output = result.stdout.decode("ascii")
        except UnicodeError:
            raise RelayError("queue_result_unknown") from None
        receipt = re.fullmatch(r"Queued message ([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}) for thread " + re.escape(THREAD) + r"\.(?:\r?\n)?", output)
        if not receipt:
            raise RelayError("queue_result_unknown")
        return receipt[1]


class Relay:
    def __init__(self, config, parser_module=None, queue=None):
        self.config = config
        self.source = safe_path(config.source)
        self.folder = safe_path(config.state_dir)
        if self.source == self.folder or self.source in self.folder.parents or self.folder in self.source.parents:
            raise RelayError("paths_overlap")
        parser_raw = read(config.parser_path, 1024 * 1024)
        self.parser_hash = hashlib.sha256(parser_raw).hexdigest()
        if parser_module is None:
            spec = importlib.util.spec_from_file_location("feiyu_safe_projection", str(config.parser_path))
            parser_module = importlib.util.module_from_spec(spec)
            exec(compile(parser_raw, str(config.parser_path), "exec"), parser_module.__dict__)
        self.parser = parser_module
        self.queue = queue if queue is not None else QueueSender(config)
        self.state_path = self.folder / "state.json"
        self.marker_path = self.folder / "initialized"
        self.have_lock = False

    @contextmanager
    def locked(self):
        safe_path(self.folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = safe_path(self.folder / "relay.lock")
        with path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RelayError("already_running") from None
            self.have_lock = True
            try:
                yield
            finally:
                self.have_lock = False
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def require_lock(self):
        if not self.have_lock:
            raise RelayError("lock_required")

    def sources(self):
        # Keep the old parser's exact shape, duplicate-key, size, sentinel and
        # stable-double-read checks, without constructing or using its Intake.
        p = self.parser
        paths = [self.source / name for name in ("incidents.json", "incident-new-content-trials.json")]
        before = [read(path, 1024 * 1024) for path in paths]
        expected = [b"feiyu-incident-policy-v1\n", b"feiyu-new-content-retry-v1\n"]
        if [read(path.with_name(path.name + ".initialized"), 64) for path in paths] != expected:
            raise RelayError("source_invalid")
        values = [p.parse(raw) for raw in before]
        rows = p.project(*values)
        # choice() maps malformed values to "unknown". Only a literal unknown
        # on disk proves the notification has actually reached that state.
        if any(row["notification"] not in {"pending", "sent", "unknown", "not_requested"}
               or row["state"] not in {"closing", "paused", "resolved"}
               for row in values[0]["incidents"]):
            raise RelayError("source_invalid")
        if any(row["notification"] not in {"none", "pending", "sent", "unknown"}
               for row in values[1]["trials"]):
            raise RelayError("source_invalid")
        if any(not p.valid_event(row) for row in rows.values()):
            raise RelayError("source_invalid")
        if before != [read(path, 1024 * 1024) for path in paths]:
            raise RelayError("source_changing")
        return rows

    def load(self):
        if read(self.marker_path, 64) != MARKER:
            raise RelayError("not_initialized")
        envelope = parse(read(self.state_path, 32 * 1024 * 1024))
        if (type(envelope) is not dict or set(envelope) != {"payload", "sha256"}
                or type(envelope["sha256"]) is not str or envelope["sha256"] != digest(envelope["payload"])):
            raise RelayError("state_invalid")
        state = envelope["payload"]
        if (type(state) is not dict or set(state) != {"schema", "thread", "parser_sha256", "records"}
                or type(state["schema"]) is not int or state["schema"] != 1 or state["thread"] != THREAD
                or state["parser_sha256"] != self.parser_hash or type(state["records"]) is not dict
                or len(state["records"]) > 100000):
            raise RelayError("state_invalid")
        for key, entry in state["records"].items():
            if (not ID.fullmatch(key) or type(entry) is not dict
                    or set(entry) != {"event", "phase", "reason", "intent_sha256", "queue_id", "received_user_message_id"}
                    or not self.parser.valid_event(entry["event"]) or entry["event"]["id"] != key
                    or entry["phase"] not in PHASES or entry["reason"] not in {"baseline", "new_incident", "acceptance_existing"}
                    or type(entry["intent_sha256"]) is not str or entry["intent_sha256"] and not HEX.fullmatch(entry["intent_sha256"])
                    or type(entry["queue_id"]) is not str or entry["queue_id"] and not UUID.fullmatch(entry["queue_id"])
                    or type(entry["received_user_message_id"]) is not str
                    or entry["received_user_message_id"] and not UUID.fullmatch(entry["received_user_message_id"])):
                raise RelayError("state_invalid")
            phase = entry["phase"]
            if ((phase in {"baseline", "waiting"} and (entry["intent_sha256"] or entry["queue_id"] or entry["received_user_message_id"]))
                    or (phase == "baseline") != (entry["reason"] == "baseline")
                    or (phase not in {"baseline", "waiting"} and not entry["intent_sha256"])
                    or (phase in {"queued_unconfirmed", "received_confirmed"}) != bool(entry["queue_id"])
                    or (phase == "received_confirmed") != bool(entry["received_user_message_id"])):
                raise RelayError("state_invalid")
        return state

    def save(self, state):
        self.require_lock()
        atomic(self.state_path, {"payload": state, "sha256": digest(state)})

    @staticmethod
    def record(event, phase, reason):
        return {"event": event, "phase": phase, "reason": reason, "intent_sha256": "", "queue_id": "", "received_user_message_id": ""}

    @staticmethod
    def eligible(event):
        return event["state"] in {"closing", "paused", "resolved"} and event["notification"] in {"sent", "unknown"}

    def initialize(self, include_existing=None):
        self.require_lock()
        if self.state_path.exists() or self.marker_path.exists():
            raise RelayError("already_initialized")
        rows = self.sources()
        if include_existing is not None and (type(include_existing) is not str or not ID.fullmatch(include_existing)
                or include_existing not in rows or rows[include_existing]["state"] != "paused"
                or not self.eligible(rows[include_existing])):
            raise RelayError("existing_not_eligible")
        state = {"schema": 1, "thread": THREAD, "parser_sha256": self.parser_hash,
                 "records": {key: self.record(value, "waiting" if key == include_existing else "baseline",
                              "acceptance_existing" if key == include_existing else "baseline") for key, value in rows.items()}}
        self.save(state)
        with self.marker_path.open("xb") as stream:
            stream.write(MARKER)
            stream.flush()
            os.fsync(stream.fileno())
        return {"ok": True, "code": "initialized", "baseline_count": len(rows) - int(include_existing is not None),
                "explicit_existing_count": int(include_existing is not None)}

    def recover_inflight(self):
        self.require_lock()
        state = self.load()
        changed = 0
        for entry in state["records"].values():
            if entry["phase"] in {"claimed", "sending"}:
                entry["phase"] = "unknown"
                changed += 1
        if changed:
            self.save(state)
        return changed

    def message(self, event, reason):
        note = "既有事故定向接入验收" if reason == "acceptance_existing" else "新事故"
        notice = "已取得发送回执" if event["notification"] == "sent" else "已尝试，结果未知"
        instruction = ("这是本次已知故障的自动接入验收，只登记接收；不要据此恢复业务、重复QQ通知或回放旧任务。"
                       if reason == "acceptance_existing" else
                       "这是故障事件自动转送；请先核对实时状态、既有处理和当前运行状态（本消息快照可能已过时）。"
                       "若服务仍受影响，在原授权和既有保护内优先做必要有限修复、精确解除相关暂停并核验后台可运行；"
                       "安全恢复不以查清全部根因为前提，根因仅在阻挡安全恢复或恢复后仍失败时继续查。"
                       "若事故已恢复、通知延后或只是正常时段休息，只核对并记录，不重复解除或恢复。"
                       "恢复不等于重发旧业务，不重复QQ通知，不授予额外试行，不放宽unknown/账号/权限/限流/数据损坏保护，不建定时任务；"
                       "授权目标优先级不意味着盲目解封。")
        return ("肥鱼故障事件自动转送（" + note + "）\n"
                "事故编号：" + event["id"] + "\n功能：" + FEATURE_NAMES[event["feature"]]
                + "\n类别：" + CODE_NAMES[event["code"]]
                + "\n保护状态：" + {"closing": "正在暂停", "paused": "已暂停", "resolved": "暂停已解除（当前记录）"}[event["state"]]
                + "\nQQ主人通知：" + notice
                + "\n" + instruction
                + "\n本消息只含白名单元数据；入队确认不代表已送达或已处理。")

    def process_once(self):
        self.require_lock()
        state, rows = self.load(), self.sources()
        # Lost/truncated source history is not a new baseline and never erases
        # reservations or old unknown results.
        if not set(state["records"]).issubset(rows):
            raise RelayError("source_incomplete")
        changed = False
        for key, event in rows.items():
            if key not in state["records"]:
                state["records"][key] = self.record(event, "waiting", "new_incident")
                changed = True
            elif (state["records"][key]["phase"] == "waiting"
                    and not self.eligible(state["records"][key]["event"])
                    and state["records"][key]["event"] != event):
                # A qualifying snapshot is immutable once durably observed.
                # Later source regression cannot erase its notification gate.
                state["records"][key]["event"] = event
                changed = True
        if changed:
            self.save(state)
        queued, unknown = 0, 0
        for key in sorted(rows):
            entry = state["records"][key]
            if entry["phase"] != "waiting" or not self.eligible(entry["event"]):
                continue
            if safe_path(self.folder.parent.parent / "run" / "services-paused").exists():
                raise RelayError("services_paused")
            message = self.message(entry["event"], entry["reason"])
            entry["intent_sha256"] = hashlib.sha256(message.encode("utf-8")).hexdigest()
            entry["phase"] = "claimed"
            self.save(state)  # fsync before any possible queue invocation.
            entry["phase"] = "sending"
            self.save(state)
            try:
                if safe_path(self.folder.parent.parent / "run" / "services-paused").exists():
                    raise RelayError("services_paused")
                queue_id = self.queue(message)
                if type(queue_id) is not str or not UUID.fullmatch(queue_id):
                    raise RelayError("queue_result_unknown")
            except Exception:
                entry["phase"] = "unknown"
                unknown += 1
            else:
                entry["phase"], entry["queue_id"] = "queued_unconfirmed", queue_id
                queued += 1
            self.save(state)
        return {"ok": True, "code": "processed", "queued_unconfirmed": queued, "unknown": unknown}

    def ack_received(self, incident_id, queue_id, user_message_id):
        self.require_lock()
        if (type(incident_id) is not str or not ID.fullmatch(incident_id)
                or type(queue_id) is not str or not UUID.fullmatch(queue_id)
                or type(user_message_id) is not str or not UUID.fullmatch(user_message_id)):
            raise RelayError("receipt_invalid")
        state = self.load()
        entry = state["records"].get(incident_id)
        if entry is None or entry["queue_id"] != queue_id or entry["phase"] not in {"queued_unconfirmed", "received_confirmed"}:
            raise RelayError("receipt_mismatch")
        if entry["phase"] == "received_confirmed":
            if entry["received_user_message_id"] != user_message_id:
                raise RelayError("receipt_mismatch")
            return {"ok": True, "code": "already_received"}
        entry["phase"], entry["received_user_message_id"] = "received_confirmed", user_message_id
        self.save(state)
        return {"ok": True, "code": "received_recorded", "evidence": "operator_verified_formal_read_thread"}

    def check(self):
        state = self.load()
        counts = {phase: 0 for phase in sorted(PHASES)}
        for entry in state["records"].values():
            counts[entry["phase"]] += 1
        return {"ok": True, "code": "state_valid", "thread": THREAD, "counts": counts,
                "delivery_semantics": "queue_confirmation_only_until_external_receipt"}


class WindowsWatch:
    """Overlapped native notifications; infinite waits, no timer or polling."""
    def __init__(self, folder, identity):
        if os.name != "nt":
            raise RelayError("windows_required")
        import ctypes
        from ctypes import wintypes as w
        self.c, self.w = ctypes, w
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]
        self.Overlapped = Overlapped
        signatures = {
            "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            "CreateEventW": ([ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
            "ReadDirectoryChangesW": ([w.HANDLE, ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped), ctypes.c_void_p], w.BOOL),
            "WaitForMultipleObjects": ([w.DWORD, ctypes.POINTER(w.HANDLE), w.BOOL, w.DWORD], w.DWORD),
            "GetOverlappedResult": ([w.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD), w.BOOL], w.BOOL),
            "CancelIoEx": ([w.HANDLE, ctypes.POINTER(Overlapped)], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL), "ResetEvent": ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            getattr(self.k, name).argtypes, getattr(self.k, name).restype = args, result
        self.directory = self.k.CreateFileW(str(folder), 1, 7, None, 3, 0x02000000 | 0x40000000, None)
        if self.directory == ctypes.c_void_p(-1).value:
            raise RelayError("watch_unavailable")
        self.event = self.k.CreateEventW(None, True, False, None)
        self.stop = self.k.CreateEventW(None, True, False, "Local\\CodexFeiyuFaultRelayStop-" + identity)
        if not self.event or not self.stop:
            self.close()
            raise RelayError("watch_unavailable")
        self.k.ResetEvent(self.stop)
        self.buffer = ctypes.create_string_buffer(65536)
        self.overlapped = Overlapped()
        self.overlapped.hEvent = self.event
        self.pending = False
        try:
            self.arm()
        except Exception:
            self.close()
            raise

    def arm(self):
        self.k.ResetEvent(self.event)
        self.overlapped.Internal = self.overlapped.InternalHigh = 0
        result = self.k.ReadDirectoryChangesW(self.directory, self.buffer, len(self.buffer), False,
                                               1 | 8 | 16, None, self.c.byref(self.overlapped), None)
        if not result and self.c.get_last_error() != 997:
            raise RelayError("watch_unavailable")
        self.pending = True

    @staticmethod
    def relevant(raw):
        offset = 0
        while offset < len(raw):
            if len(raw) - offset < 12:
                raise RelayError("watch_record_invalid")
            following, action, length = struct.unpack_from("<III", raw, offset)
            if length % 2 or offset + 12 + length > len(raw) or action not in {1, 2, 3, 4, 5}:
                raise RelayError("watch_record_invalid")
            name = raw[offset + 12:offset + 12 + length].decode("utf-16-le")
            if name.casefold() in FILES:
                return True
            if not following:
                return False
            if following < 12 + length or following % 4 or offset + following >= len(raw):
                raise RelayError("watch_record_invalid")
            offset += following
        return False

    def wait(self):
        handles = (self.w.HANDLE * 2)(self.stop, self.event)
        while True:
            result = self.k.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF)
            if result == 0:
                return False
            if result != 1:
                raise RelayError("watch_wait_failed")
            count = self.w.DWORD()
            ok = self.k.GetOverlappedResult(self.directory, self.c.byref(self.overlapped), self.c.byref(count), False)
            error = self.c.get_last_error() if not ok else 0
            self.pending = False
            raw = self.buffer.raw[:count.value]
            # Re-arm BEFORE projecting files or queueing; writes during CLI work
            # remain buffered. Overflow wakes exactly one conservative reread.
            self.arm()
            if error == 1022 or ok and not count.value:
                return True
            if error:
                raise RelayError("watch_unavailable")
            if self.relevant(raw):
                return True

    def close(self):
        if getattr(self, "pending", False):
            self.k.CancelIoEx(self.directory, self.c.byref(self.overlapped))
            count = self.w.DWORD()
            self.k.GetOverlappedResult(self.directory, self.c.byref(self.overlapped), self.c.byref(count), True)
        for name in ("directory", "event", "stop"):
            handle = getattr(self, name, None)
            if handle and handle != self.c.c_void_p(-1).value:
                self.k.CloseHandle(handle)
                setattr(self, name, None)


def identity(folder):
    return hashlib.sha256(str(folder).casefold().encode("utf-8")).hexdigest()[:24]


def request_stop(folder):
    if os.name != "nt":
        raise RelayError("windows_required")
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenEventW.argtypes, k.OpenEventW.restype = [w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE
    k.SetEvent.argtypes, k.SetEvent.restype = [w.HANDLE], w.BOOL
    k.CloseHandle.argtypes = [w.HANDLE]
    handle = k.OpenEventW(2, False, "Local\\CodexFeiyuFaultRelayStop-" + identity(folder))
    if not handle:
        return {"ok": True, "code": "not_running"}
    try:
        if not k.SetEvent(handle):
            raise RelayError("stop_failed")
    finally:
        k.CloseHandle(handle)
    return {"ok": True, "code": "stop_requested"}


def process_creation_filetime():
    if os.name != "nt":
        return "unavailable"
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = w.HANDLE
    k.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
    times = [w.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(k.GetCurrentProcess(), *(ctypes.byref(item) for item in times)):
        raise RelayError("process_identity_unavailable")
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def run(relay, launch_nonce=None):
    if launch_nonce is not None and (type(launch_nonce) is not str or not ID.fullmatch(launch_nonce)):
        raise RelayError("launch_nonce_invalid")
    with relay.locked():
        watch = WindowsWatch(relay.source, identity(relay.folder))
        runtime = {"schema": 1, "pid": os.getpid(), "ppid": os.getppid(), "nonce": launch_nonce or uuid.uuid4().hex,
                   "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "config_sha256": relay.config.config_sha256,
                   "process_creation_filetime": process_creation_filetime(), "status": "starting"}
        try:
            # Directory watch is already armed before recovery or source reads.
            relay.recover_inflight()
            atomic(relay.folder / "runtime.json", runtime)
            while True:
                try:
                    relay.process_once()
                    runtime["status"] = "watching"
                except (OSError, ValueError, TypeError, KeyError, OverflowError, RelayError, relay.parser.IntakeError):
                    # A partial write cannot authorize a send. Wait for the next
                    # native write/rename, without an interval, retry or timer.
                    runtime["status"] = "source_blocked"
                atomic(relay.folder / "runtime.json", runtime)
                if not watch.wait():
                    break
        finally:
            watch.close()
            runtime["status"] = "stopped"
            atomic(relay.folder / "runtime.json", runtime)
    return {"ok": True, "code": "stopped"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("init").add_argument("--include-existing")
    actions.add_parser("run").add_argument("--launch-nonce")
    for action in ("check", "stop"):
        actions.add_parser(action)
    ack = actions.add_parser("ack-received")
    for option in ("incident-id", "queue-id", "user-message-id"):
        ack.add_argument("--" + option, required=True)
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        if args.action == "stop":
            result = request_stop(config.state_dir)
        else:
            relay = Relay(config)
            if args.action == "run":
                result = run(relay, args.launch_nonce)
            elif args.action == "check":
                result = relay.check()
            else:
                with relay.locked():
                    result = relay.initialize(args.include_existing) if args.action == "init" else relay.ack_received(
                        args.incident_id, args.queue_id, args.user_message_id)
    except KeyboardInterrupt:
        result = {"ok": True, "code": "interrupted_no_retry"}
    except Exception:
        # No input, JSON, exception, stderr or CLI output is printed on failure.
        result = {"ok": False, "code": "relay_blocked"}
    print(encode(result).decode("ascii"))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
