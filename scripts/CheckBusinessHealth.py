"""Read local business metadata only; never import plugins or contact services."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

TZ = timezone(timedelta(hours=8))
STAGE = "staging/napcat-trusted-native-bridge-review-v4.18.41/daily-runtime"
FEATURES = {
    "heybox_vision": "小黑盒识图", "heybox_comment": "小黑盒评论",
    "heybox_feed": "小黑盒浏览", "bili_browse": "B站浏览",
    "bili_journal": "B站手账", "bili_share": "B站分享",
    "heybox_autopost": "小黑盒投稿", "heybox_qzone_sync": "空间同步",
    "qzone_publish": "空间发表", "friend_time_notice": "好友时段问候",
}
CATEGORIES = {"vision_failed", "model_failed", "request_failed", "storage_failed",
              "delivery_unknown", "credential_invalid", "guard_failed", "unexpected_exception",
              "platform_content_rejected", "connection_failed", "service_unavailable",
              "rate_limited", "access_denied", "response_invalid", "request_rejected",
              "model_connection_failed", "model_timeout"}
NOTIFICATIONS = {"pending", "sent", "unknown", "not_requested"}
ROW_KEYS = {"id", "feature", "category", "stage", "event_digest", "state", "notification",
            "created_at", "resolved_at", "message_id", "receipt_kind"}
STAGES = {"", "feed_once", "notice_poll", "notice_event", "bili_browse_once", "bili_journal_once",
          "bili_share_once", "autopost_once", "autopost_send", "qzone_sync_once", "qzone_sync_send",
          "public_send", "reply_send", "friend_time_send"}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def read_json(path, limit=512 * 1024):
    """Bounded, stable, non-reparse read; return no raw diagnostic text."""
    try:
        before = path.lstat()
        if path.is_symlink() or getattr(before, "st_file_attributes", 0) & 1024:
            return None
        if not 0 < before.st_size <= limit:
            return None
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        after = path.stat()
        if len(raw) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        value = json.loads(raw.decode("utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def flag(value, key):
    result = value.get(key) if isinstance(value, dict) else None
    return result if type(result) is bool else None


def process_rows():
    try:
        import psutil  # Local process inspection only; no command lines read.
        rows = []
        for process in psutil.process_iter(["pid", "name"]):
            if str(process.info.get("name", "")).lower() not in {"qq.exe", "python.exe"}:
                continue
            try:
                rows.append(process.as_dict(attrs=["pid", "ppid", "exe", "create_time"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return rows
    except Exception:
        return None


def norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def identity(row):
    if row and type(row.get("pid")) is int and row["pid"] > 0 and finite(row.get("create_time")):
        return {"pid": row["pid"], "created_at": round(row["create_time"], 3)}
    return None


def matches(record, row, exe):
    try:
        stamp = record.get("CreatedUtc", record.get("Created"))
        if re.search(r"\.[0-9]{7,}", stamp):
            precise = re.fullmatch(
                r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6})[0-9]"
                r"(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", stamp)
            if not precise:
                return False
            # .NET uses 100ns ticks; Python 3.10 accepts microseconds only.
            stamp = precise.group(1) + precise.group(2)
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return False
        created = parsed.timestamp()
        return (record.get("Id") == row["pid"] and norm(record.get("Exe", "")) == norm(exe)
                and ("Parent" not in record or record["Parent"] == row.get("ppid"))
                and abs(created - row["create_time"]) < 0.02)
    except (ValueError, AttributeError, TypeError, KeyError):
        return False


def runtime(root, rows, now):
    core = read_json(root / "run/astrbot.json", 16384)
    pending = read_json(root / STAGE / "pending.json", 32768)
    binding = read_json(root / STAGE / "binding.json", 65536)
    astro_exe, qq_exe = root / "runtime/astrbot-venv/Scripts/python.exe", root / "apps/QQ/QQ.exe"
    rows = rows or []
    astro = next((r for r in rows if core and norm(r.get("exe") or "") == norm(astro_exe)
                  and matches(core, r, astro_exe)), None)
    qqs = [r for r in rows if norm(r.get("exe") or "") == norm(qq_exe) and identity(r)]
    qq_ids = {r["pid"] for r in qqs}
    roots = [r for r in qqs if r.get("ppid") not in qq_ids]
    qq = roots[0] if len(roots) == 1 else None
    valid_binding = bool(binding and binding.get("Schema") == 1 and binding.get("NoHistoricalReplay") is True
                         and re.fullmatch(r"[a-f0-9]{64}", str(binding.get("ConfigurationFingerprint", "")))
                         and isinstance(binding.get("NativeProcess"), dict))
    bound = (matches(binding["NativeProcess"], qq, qq_exe) if qq and valid_binding else None)
    valid_pending = bool(pending and pending.get("Schema") == 1 and isinstance(pending.get("NativeProcess"), dict)
                         and valid_binding and pending.get("ConfigurationFingerprint") == binding["ConfigurationFingerprint"])
    active = flag(pending, "Active") if valid_pending else None
    pending_matches = bool(qq and valid_pending and matches(pending["NativeProcess"], qq, qq_exe))
    settled = bool(astro and qq and bound and pending_matches and active is False
                   and pending.get("Phase") == "native_and_snow_ready" and now - astro["create_time"] >= 60)
    return {"astrbot": identity(astro), "qq": identity(qq), "qq_binding_matches": bound,
            "qq_pending": active, "startup": "metadata_ready" if settled else "metadata_pending",
            "business_loops": "unknown_not_observed"}


def incidents(root):
    path = root / "data/astrbot/plugin_data/feiyu_steward/incidents.json"
    value = read_json(path)
    result = {key: {"blocked": None, "category": None, "notification": None} for key in FEATURES}
    try:
        sentinel = path.with_name(path.name + ".initialized")
        info = sentinel.lstat()
        initialized = (not sentinel.is_symlink() and not getattr(info, "st_file_attributes", 0) & 1024
                       and info.st_size == 25 and sentinel.read_bytes() == b"feiyu-incident-policy-v1\n")
    except OSError:
        initialized = False
    if initialized and value and set(value) == {"version", "incidents"} and type(value.get("version")) is int and value["version"] == 1:
        rows = value.get("incidents")
        if isinstance(rows, list) and len(rows) < 256:
            valid = all(isinstance(r, dict) and set(r) == ROW_KEYS and r.get("feature") in FEATURES
                        and r.get("state") in {"closing", "paused", "resolved"}
                        and r.get("category") in CATEGORIES and r.get("notification") in NOTIFICATIONS
                        and isinstance(r.get("id"), str) and re.fullmatch(r"[a-f0-9]{32}", r["id"])
                        and r.get("stage") in STAGES and finite(r.get("created_at"))
                        and type(r.get("resolved_at")) in (int, float) and math.isfinite(r["resolved_at"]) and r["resolved_at"] >= 0
                        and (r["state"] == "resolved") == bool(r["resolved_at"])
                        and isinstance(r.get("event_digest"), str) and (not r["event_digest"] or re.fullmatch(r"[a-f0-9]{64}", r["event_digest"]))
                        and isinstance(r.get("message_id"), str) and (not r["message_id"] or re.fullmatch(r"[1-9]\d{0,23}", r["message_id"]))
                        and r.get("receipt_kind") in {"", "native"} and (r["notification"] == "sent") == bool(r["message_id"])
                        and (r["notification"] == "sent") == (r["receipt_kind"] == "native")
                        for r in rows)
            active = [r for r in rows if r.get("state") != "resolved"] if valid else []
            valid = valid and len({r["feature"] for r in active}) == len(active) and len({r["id"] for r in rows}) == len(rows)
            if valid:
                for entry in result.values():
                    entry["blocked"] = False
                for row in active:
                    result[row["feature"]] = {"blocked": True, "category": row["category"],
                                               "notification": row["notification"]}
    for feature, entry in result.items():
        try:
            marker = path.with_name(path.name + "." + feature + ".blocked")
            if marker.exists() or marker.is_symlink():
                entry["blocked"] = True  # A durable latch blocks even before ledger adoption.
        except OSError:
            entry["blocked"] = None
    return result


def assess(root, rows=None, now=None):
    now = time.time() if now is None else now
    config = root / "data/astrbot/config"
    hey = read_json(config / "astrbot_plugin_xiaoheihe_adapter_config.json")
    bili = read_json(config / "astrbot_plugin_bilibili_ai_bot_config.json")
    qzone_grant = read_json(root / "data/astrbot/plugin_data/feiyu_steward/heybox-qzone-sync/authorization.json", 8192)
    qzone_switches = [flag(qzone_grant, "enabled"), flag(qzone_grant, "auto_sync")]
    qzone_enabled = (all(qzone_switches) if qzone_grant and type(qzone_grant.get("schema")) is int
                     and qzone_grant["schema"] == 1 and all(v is not None for v in qzone_switches) else None)
    feed = hey.get("proactive_feed") if hey else None
    profiles = hey.get("profiles") if hey else None
    profile_ok = None
    if isinstance(profiles, list) and profiles:
        if any(flag(p, "enabled") is True and flag(p, "dry_run") is False for p in profiles):
            profile_ok = True
        elif all(flag(p, "enabled") is not None and flag(p, "dry_run") is not None for p in profiles):
            profile_ok = False
    switches = [flag(feed, "enabled"), not flag(feed, "dry_run") if flag(feed, "dry_run") is not None else None, profile_ok]
    hey_enabled = False if False in switches else True if all(v is True for v in switches) else None
    interval = bili.get("FEIYU_INTEREST_POLL_MINUTES") if bili else None
    interval = interval if finite(interval) and interval <= 525600 else None
    local = datetime.fromtimestamp(now, TZ)
    night = not 8 <= local.hour < 22
    pauses = incidents(root)
    projected = {}
    for feature, pause in pauses.items():
        enabled = (hey_enabled if feature == "heybox_feed" else flag(bili, "ENABLE_PROACTIVE") if feature == "bili_browse"
                   else qzone_enabled if feature == "heybox_qzone_sync" else None)
        dependencies = {"heybox_feed": ("heybox_comment", "heybox_vision"), "heybox_comment": ("heybox_vision",),
                        "heybox_qzone_sync": ("qzone_publish",)}
        inherited = any(pauses[key]["blocked"] is True for key in dependencies.get(feature, ()))
        state = ("disabled" if feature == "heybox_qzone_sync" and enabled is False
                 else "fault_paused" if pause["blocked"] is True or inherited
                 else "unknown" if pause["blocked"] is None or feature == "heybox_qzone_sync" and enabled is None
                 else "disabled" if enabled is False else "time_rest" if feature.startswith("bili_") and night
                 else "runtime_unknown")
        projected[feature] = {"status": state, **pause, "enabled": enabled,
                              "inherited_pause": inherited}
    def text_for(keys):
        names = [FEATURES[k].replace("小黑盒", "").replace("B站", "") for k in keys if projected[k]["status"] == "fault_paused"]
        if names:
            return "、".join(names) + "故障暂停"
        states = {projected[k]["status"] for k in keys}
        return "配置关闭" if states == {"disabled"} else "实际运行待确认" if states == {"time_rest"} else "运行状态待确认"
    runtime_state = runtime(root, rows, now)
    return {"schema": 1, "checked_at": local.isoformat(timespec="seconds"),
            "summary": "；".join(["小黑盒：" + text_for(["heybox_feed", "heybox_comment", "heybox_vision"]),
                                   "B站：" + text_for(["bili_browse", "bili_journal", "bili_share"])
                                   + ("，当前夜间时窗限制" if night else ""),
                                   "空间同步：" + text_for(["heybox_qzone_sync"]) ])
                       + ("；QQ登录/主服务就绪状态待核验" if runtime_state["startup"] == "metadata_pending" else ""),
            "runtime": runtime_state, "features": projected,
            "bili_interest_driven": flag(bili, "FEIYU_INTEREST_DRIVEN"), "bili_poll_minutes": interval,
            "bili_window": "08:00-22:00 Asia/Hong_Kong; off_peak_required",
            "peak_status": "unknown_not_queried", "runtime_gate": "unknown_not_observed", "runtime_success_verified": False}


@contextmanager
def own_lock(folder):
    for part in (folder, *folder.parents):
        if part.is_symlink() or part.exists() and getattr(part.stat(), "st_file_attributes", 0) & 1024:
            raise OSError("report_path_unavailable")
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".writer.lock"
    if lock.is_symlink() or lock.exists() and getattr(lock.stat(), "st_file_attributes", 0) & 1024:
        raise OSError("report_path_unavailable")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def write_json(path, value):
    if path.exists() and (path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 1024):
        raise OSError("report_path_unavailable")
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_latest(folder, report):
    current = read_json(folder / "latest.json")
    if current:
        try:
            if datetime.fromisoformat(current["checked_at"]) > datetime.fromisoformat(report["checked_at"]):
                return
        except (KeyError, ValueError, TypeError):
            return  # Preserve uncertain current evidence instead of overwriting it.
    write_json(folder / "latest.json", report)


def after_restart(root, report, now):
    folder = root / "logs/business-health"
    generation = {key: report["runtime"][key] for key in ("astrbot", "qq")}
    if not any(generation.values()):
        return False  # No verified process generation exists to deduplicate.
    key = hashlib.sha256(json.dumps(generation, sort_keys=True).encode()).hexdigest()[:24]
    with own_lock(folder):
        previous = read_json(folder / (key + ".json"))
        if not previous:
            previous = read_json(folder / (key + "-1.json"))
            if previous:
                write_json(folder / (key + ".json"), previous)
                write_latest(folder, previous)
        latest = read_json(folder / "latest.json")
        if previous and (not latest or latest.get("generation_key") != key or latest.get("attempt") != previous.get("attempt")):
            write_latest(folder, previous)
        attempt = previous.get("attempt") if previous else None
        if previous:
            if type(attempt) is not int or not 1 <= attempt <= 4:
                return False
            if (previous.get("runtime", {}).get("startup") != "metadata_pending" or attempt >= 4
                    or not finite(previous.get("next_eligible_at")) or now < previous["next_eligible_at"]):
                return False
        report.update(generation_key=key, attempt=(attempt + 1) if previous else 1,
                      next_eligible_at=now + 60 if report["runtime"]["startup"] == "metadata_pending" else None)
        # The held file lock serializes the archive existence check and writes.
        archive = folder / (key + "-" + str(report["attempt"]) + ".json")
        if archive.exists():
            recovered = read_json(archive)
            if recovered:
                write_json(folder / (key + ".json"), recovered)
                write_latest(folder, recovered)
            return False
        write_json(archive, report)
        write_json(folder / (key + ".json"), report)
        write_latest(folder, report)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("D:/QQBot"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--after-restart", action="store_true")
    args = parser.parse_args(argv)
    try:
        now = time.time()
        report = assess(args.root, process_rows(), now)
        if args.after_restart and not any(report["runtime"][key] for key in ("astrbot", "qq")):
            report["summary"] = "核心/QQ进程尚未核验，业务状态待确认"
            print(json.dumps(report, ensure_ascii=True))
            return 0
        if args.check or after_restart(args.root, report, now):
            print(json.dumps(report, ensure_ascii=True))
        else:
            print('{"schema":1,"skipped":true}')
        return 0
    except Exception:
        print('{"schema":1,"summary":"本机业务检查暂不可用","status":"unknown"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
