"""Read-only QQ health monitor. Never logs in to QQ, sends, injects or recovers.

Only --watch enables a persistent loop. --once prints one safe projection.
Both modes require an explicitly pinned QQ process generation. Snow control
authentication is separate from QQ account login and is cached in memory.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import urllib.error
import urllib.request


ROOT = Path("D:/QQBot")
STAGE = ROOT / "staging/napcat-trusted-native-bridge-review-v4.18.41"
OUTPUT = ROOT / "logs/qq-online-monitor"
EXPECTED_SELF = "0"
MAX_EVENT_BYTES = 2 * 1024 * 1024
REASONS = frozenset({"qq_generation_changed", "qq_process_missing", "qq_identity_unverified",
    "native_listener_unverified", "astrbot_listener_missing", "snow_listener_unverified",
    "listeners_unverified", "native_identity_mismatch", "native_offline", "native_status_unverified",
    "snow_target_unverified", "snow_contract_unverified", "snow_offline", "snow_identity_mismatch",
    "snow_status_unverified"})


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid_monitor_time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid_monitor_timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def project_transition(value):
    if not isinstance(value, dict) or value.get("event") not in ("health_issue_confirmed", "health_recovered"):
        raise ValueError("invalid_transition")
    identity, revision = value.get("incident_id"), value.get("alert_revision")
    if (not isinstance(identity, str) or len(identity) != 32
            or any(ch not in "0123456789abcdef" for ch in identity)
            or type(revision) is not int or not 1 <= revision <= 10**12):
        raise ValueError("invalid_transition_identity")
    result = {"event": value["event"], "at_utc": safe_time(value.get("at_utc")),
              "incident_id": identity, "alert_revision": revision,
              "event_id": f"{identity}:{revision}:{value['event']}"}
    if value["event"] == "health_issue_confirmed":
        reasons = value.get("reason_codes")
        if (not isinstance(reasons, list) or not reasons or len(reasons) > len(REASONS)
                or any(not isinstance(code, str) or code not in REASONS for code in reasons)
                or type(value.get("offline_evidence")) is not bool):
            raise ValueError("invalid_transition_reason")
        result.update(reason_codes=sorted(set(reasons)), offline_evidence=value["offline_evidence"])
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect_rejected", headers, fp)


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def http(path, *, service, token=None, payload=None):
    # Callers cannot select arbitrary hosts or follow a redirect carrying secrets.
    ports = {"native": 16199, "snow": 5099}
    allowed = {("native", "/v1/status"), ("snow", "/api/login"), ("snow", "/api/processes")}
    if (service, path) not in allowed:
        raise ValueError("unsupported_read_endpoint")
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{ports[service]}{path}", data, headers)
    with HTTP.open(request, timeout=3) as reply:
        body = reply.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("health_response_too_large")
        result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("invalid_health_response")
    return result


class Sampler:
    def __init__(self, pid, created, exe):
        self.pid = pid
        self.created = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        self.exe = os.path.normcase(os.path.abspath(exe))
        self.snow_token = None
        self.snow_auth_after = 0.0

    def snow(self):
        if self.snow_token is None:
            if time.monotonic() < self.snow_auth_after:
                raise ValueError("snow_auth_backoff")
            # Even an unavailable control service must not cause a login storm.
            self.snow_auth_after = time.monotonic() + 300
            credentials = read_json(ROOT / "config/bootstrap.json")
            reply = http("/api/login", service="snow", payload={"password": credentials["snowluma_password"]})
            del credentials
            if reply.get("success") is not True or not isinstance(reply.get("token"), str) or not reply["token"]:
                raise ValueError("snow_auth_unconfirmed")
            self.snow_token = reply["token"]
            self.snow_auth_after = 0.0
        try:
            return http("/api/processes", service="snow", token=self.snow_token)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                self.snow_token = None
            raise

    def sample(self):
        import psutil  # Provided by the existing AstrBot runtime (7.1.3).

        reasons = []
        result = {"checked_at_utc": utc_now(), "expected_pid": self.pid,
                  "expected_created_utc": datetime.fromtimestamp(self.created, timezone.utc).isoformat(),
                  "expected_exe": self.exe,
                  "same_generation": None, "native_online": None, "snow_online": None,
                  "native_identity_match": None, "snow_identity_match": None,
                  "native_listener_owner_match": None, "snow_listener_verified": None,
                  "astrbot_listening": None}
        try:
            process = psutil.Process(self.pid)
            generation = abs(process.create_time() - self.created) < .001
            image = os.path.normcase(os.path.abspath(process.exe())) == self.exe
            result["same_generation"] = generation and image
            if not result["same_generation"]:
                reasons.append("qq_generation_changed")
        except psutil.NoSuchProcess:
            result["same_generation"] = False
            reasons.append("qq_process_missing")
        except (psutil.Error, OSError):
            reasons.append("qq_identity_unverified")
        try:
            sockets = psutil.net_connections(kind="tcp")
            native = [s for s in sockets if s.status == psutil.CONN_LISTEN and s.laddr.port == 16199]
            result["native_listener_owner_match"] = (len(native) == 1 and native[0].pid == self.pid
                                                       and native[0].laddr.ip == "127.0.0.1")
            result["astrbot_listening"] = any(s.status == psutil.CONN_LISTEN and s.laddr.port == 6185
                                              and s.laddr.ip == "127.0.0.1" for s in sockets)
            if not result["native_listener_owner_match"]:
                reasons.append("native_listener_unverified")
            if not result["astrbot_listening"]:
                reasons.append("astrbot_listener_missing")
            snow = [s for s in sockets if s.status == psutil.CONN_LISTEN and s.laddr.port == 5099]
            result["snow_listener_verified"] = (len(snow) == 1 and snow[0].laddr.ip == "127.0.0.1"
                and snow[0].pid is not None and os.path.normcase(os.path.abspath(psutil.Process(snow[0].pid).exe()))
                    == os.path.normcase(os.path.abspath(ROOT / "apps/SnowLuma/node.exe")))
            if not result["snow_listener_verified"]:
                reasons.append("snow_listener_unverified")
        except (psutil.Error, OSError):
            reasons.append("listeners_unverified")
        # Do not give a local token to an unverified listener or replaced QQ.
        if result["same_generation"] is True and result["native_listener_owner_match"] is True:
            try:
                config = read_json(STAGE / "config/feiyu-native-bridge.json")
                reply = http("/v1/status", service="native", token=config["token"])
                del config
                data = reply.get("data")
                if reply.get("ok") is not True or not isinstance(data, dict) or type(data.get("online")) is not bool:
                    raise ValueError("native_contract_unverified")
                result["native_identity_match"] = data.get("self_id") == EXPECTED_SELF
                result["native_online"] = data["online"]
                if not result["native_identity_match"]:
                    reasons.append("native_identity_mismatch")
                if not data["online"]:
                    reasons.append("native_offline")
            except Exception:
                reasons.append("native_status_unverified")
        try:
            if result["snow_listener_verified"] is not True:
                raise ValueError("snow_listener_unverified")
            reply = self.snow()
            rows = reply.get("list")
            if not isinstance(rows, list):
                raise ValueError("snow_contract_unverified")
            targets = [r for r in rows if isinstance(r, dict) and type(r.get("pid")) is int and r["pid"] == self.pid]
            if len(targets) != 1:
                reasons.append("snow_target_unverified")
            else:
                target = targets[0]
                result["snow_identity_match"] = str(target.get("uin")) == EXPECTED_SELF
                flags = [target.get(k) for k in ("injected", "connected", "loggedIn")]
                if not all(type(flag) is bool for flag in flags) or target.get("status") not in ("online", "offline"):
                    reasons.append("snow_contract_unverified")
                else:
                    result["snow_online"] = all(flags) and target["status"] == "online"
                    if not result["snow_online"]:
                        reasons.append("snow_offline")
                if not result["snow_identity_match"]:
                    reasons.append("snow_identity_mismatch")
        except Exception:
            reasons.append("snow_status_unverified")
        result["reason_codes"] = sorted(set(reasons))
        result["healthy"] = not reasons
        # Network/control read errors are not evidence that Tencent kicked QQ.
        result["offline_evidence"] = any(code in reasons for code in ("qq_process_missing", "native_offline", "snow_offline"))
        return result


class Debouncer:
    def __init__(self, previous=None):
        self.active = None
        self.failures = 0
        self.successes = 0
        self.alert_revision = 0
        # Only restore our bounded machine fields; never serialize input prose.
        if isinstance(previous, dict):
            revision = previous.get("alert_revision")
            if type(revision) is int and 0 <= revision <= 10**12:
                self.alert_revision = revision
            active = previous.get("active_incident")
            if isinstance(active, dict) and isinstance(active.get("id"), str) and len(active["id"]) == 32:
                if all(ch in "0123456789abcdef" for ch in active["id"]):
                    try:
                        self.active = {"id": active["id"], "first_bad_at_utc": safe_time(active.get("first_bad_at_utc")),
                                       "confirmed_at_utc": safe_time(active.get("confirmed_at_utc"))}
                    except ValueError:
                        self.active = None

    def update(self, sample):
        event = None
        if sample["healthy"]:
            self.successes += 1
            self.failures = 0
            if self.active and self.successes >= 2:
                self.alert_revision += 1
                event = {"event": "health_recovered", "at_utc": sample["checked_at_utc"],
                         "incident_id": self.active["id"], "alert_revision": self.alert_revision}
                self.active = None
        else:
            self.successes = 0
            self.failures += 1
            if self.failures == 1:
                self.first_bad = sample["checked_at_utc"]
            if self.active is None and self.failures >= 3:
                self.alert_revision += 1
                self.active = {"id": uuid.uuid4().hex, "first_bad_at_utc": self.first_bad,
                               "confirmed_at_utc": sample["checked_at_utc"]}
                event = {"event": "health_issue_confirmed", "at_utc": sample["checked_at_utc"],
                         "incident_id": self.active["id"], "alert_revision": self.alert_revision,
                         "reason_codes": sample["reason_codes"], "offline_evidence": sample["offline_evidence"]}
        return project_transition(event) if event else None

    def state(self, sample):
        return {"schema": 1, "sample": sample, "active_incident": self.active,
                "state": "issue_confirmed" if self.active else "healthy" if sample["healthy"] else "checking",
                "consecutive_failures": self.failures, "consecutive_successes": self.successes,
                "alert_revision": self.alert_revision,
                "monitor_pid": os.getpid(), "sample_interval_seconds": 30,
                "qq_actions_sent": 0, "qq_relogin_attempts": 0}


def atomic_json(path, value):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=True, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def preserve_first_incident(folder, event):
    if event["event"] != "health_issue_confirmed":
        return
    path = folder / "first-incident.json"
    if path.exists():
        # Never overwrite the first evidence, even after a monitor restart.
        return
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as file:
            json.dump(event, file, ensure_ascii=True, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        try:
            # Install a complete file without replacing an existing first record.
            os.link(temp, path)
        except FileExistsError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def append_event(folder, event):
    path = folder / "events.jsonl"
    preserve_first_incident(folder, event)
    # Retried outbox delivery after a crash uses a stable event ID.
    if event.get("event_id"):
        for existing in (path, folder / "events.previous.jsonl"):
            if existing.exists():
                for line in existing.read_text(encoding="utf-8").splitlines():
                    try:
                        if json.loads(line).get("event_id") == event["event_id"]:
                            return
                    except (ValueError, AttributeError):
                        continue
    if path.exists() and path.stat().st_size >= MAX_EVENT_BYTES:
        os.replace(path, folder / "events.previous.jsonl")
    # Atomic bounded rewrite prevents an interrupted append leaving a torn line.
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        file.write(previous)
        if previous and not previous.endswith("\n"):
            file.write("\n")
        file.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def finish_outbox(folder, state):
    event = state.get("pending_transition")
    if event is None:
        return
    append_event(folder, project_transition(event))
    state["pending_transition"] = None
    atomic_json(folder / "latest.json", state)


def commit_sample(folder, debounce, sample, event):
    state = debounce.state(sample)
    state["pending_transition"] = event
    # Outbox and state are one durable commit. A restart repairs undelivered
    # events before new samples can replace the original anomaly evidence.
    atomic_json(folder / "latest.json", state)
    finish_outbox(folder, state)


def watch(sampler, folder):
    # OS file lock is released on crash; no stale PID-file ownership guessing.
    import msvcrt
    folder.mkdir(parents=True, exist_ok=True)
    lock = (folder / "monitor.lock").open("a+b")
    lock.seek(0, os.SEEK_END)
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise RuntimeError("monitor_already_running") from None
    try:
        try:
            previous = read_json(folder / "latest.json")
        except (OSError, ValueError):
            previous = None
        if isinstance(previous, dict) and previous.get("pending_transition") is not None:
            finish_outbox(folder, previous)
        debounce = Debouncer(previous)
        append_event(folder, {"event": "monitor_started", "at_utc": utc_now(), "monitor_pid": os.getpid()})
        last_heartbeat = time.monotonic()
        while not (folder / "stop.request").exists():
            started = time.monotonic()
            sample = sampler.sample()
            event = debounce.update(sample)
            commit_sample(folder, debounce, sample, event)
            if started - last_heartbeat >= 900:
                append_event(folder, {"event": "monitor_heartbeat", "at_utc": utc_now(),
                                      "monitor_pid": os.getpid(), "state": debounce.state(sample)["state"]})
                last_heartbeat = started
            time.sleep(max(0, 30 - (time.monotonic() - started)))
        append_event(folder, {"event": "monitor_stopped_by_request", "at_utc": utc_now(), "monitor_pid": os.getpid()})
    finally:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--watch", action="store_true")
    parser.add_argument("--expected-pid", type=int, required=True)
    parser.add_argument("--expected-created-utc", required=True)
    parser.add_argument("--expected-exe", required=True)
    args = parser.parse_args()
    if args.expected_pid <= 0 or args.expected_pid == 20164:
        raise ValueError("invalid_monitored_process")
    sampler = Sampler(args.expected_pid, args.expected_created_utc, args.expected_exe)
    if args.once:
        print(json.dumps(sampler.sample(), ensure_ascii=True))
    else:
        watch(sampler, OUTPUT)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print exception messages, response bodies, config or traceback.
        print(json.dumps({"ok": False, "failure_type": type(error).__name__}))
        raise SystemExit(1)
