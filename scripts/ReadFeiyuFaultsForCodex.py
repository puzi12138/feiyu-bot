"""Read F060 metadata; acknowledge only snapshots already delivered by Codex.

No business module is imported. Source files are opened only for reading.
The separate intake directory contains the cursor and checksummed safe snapshots.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid

SOURCE = Path("D:/QQBot/data/astrbot/plugin_data/feiyu_steward")
STATE = Path("D:/QQBot/data/codex-fault-intake")
ID = re.compile(r"[a-f0-9]{32}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
FEATURES = set("heybox_vision heybox_comment heybox_feed bili_browse bili_journal bili_share heybox_autopost heybox_qzone_sync qzone_publish friend_time_notice".split())
CODES = set("vision_failed model_failed model_connection_failed model_timeout request_failed connection_failed service_unavailable rate_limited access_denied response_invalid request_rejected platform_content_rejected storage_failed delivery_unknown credential_invalid guard_failed unexpected_exception".split())
STAGES = set("feed_once notice_poll notice_event bili_browse_once bili_journal_once bili_source_read bili_share_once autopost_once autopost_send qzone_sync_once qzone_sync_send public_send reply_send friend_time_send".split()) | {""}
PHASES = set("pending_notice pending_terminal ready reserved started accepted blocked settled".split())
INCIDENT_FIELDS = set("id feature category stage event_digest state notification created_at resolved_at message_id receipt_kind".split())
TRIAL_FIELDS = set("incident_id scope feature category stage failure_kind failed_key_hash failed_at phase candidate_key_hash observed_at event_key_hash token_hash generation notice_id notification notice_message_id notice_receipt_kind created_at updated_at".split())
EVENT_FIELDS = set("id feature code origin stage state created_at resolved_at notification trial_phase trial_notification trial_updated_at".split())


class IntakeError(Exception):
    """A fixed output code; never include external values or exception text."""


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def read(path, limit):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise IntakeError("source_unavailable")
    return path.read_bytes()


def parse(raw):
    return json.loads(raw, object_pairs_hook=unique_pairs)


def choice(value, allowed):
    return value if type(value) is str and value in allowed else "unknown"


def stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 4102444800 else "unknown"


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def status(value):
    return tuple(value[k] for k in ("id", "state", "notification", "trial_phase", "trial_notification"))


def valid_event(row):
    return (type(row) is dict and set(row) == EVENT_FIELDS and type(row["id"]) is str
        and ID.fullmatch(row["id"]) and row["feature"] in FEATURES | {"unknown"}
        and row["code"] in CODES | {"unknown"} and row["origin"] == "feiyu_incident_policy"
        and row["stage"] in STAGES | {"unknown"} and row["state"] in {"closing", "paused", "resolved", "unknown"}
        and row["notification"] in {"pending", "sent", "unknown", "not_requested"}
        and row["trial_phase"] in PHASES | {"none", "unknown"}
        and row["trial_notification"] in {"none", "pending", "sent", "unknown"}
        and all(stamp(row[k]) == row[k] for k in ("created_at", "resolved_at", "trial_updated_at")))


def project(incidents, trials):
    if (type(incidents) is not dict or set(incidents) != {"version", "incidents"}
            or type(incidents["version"]) is not int or incidents["version"] != 1
            or type(incidents["incidents"]) is not list or len(incidents["incidents"]) > 256
            or type(trials) is not dict or set(trials) != {"version", "owner_generation", "trials"}
            or type(trials["version"]) is not int or trials["version"] != 1
            or type(trials["owner_generation"]) is not str or not ID.fullmatch(trials["owner_generation"])
            or type(trials["trials"]) is not list or len(trials["trials"]) > 4096):
        raise IntakeError("source_invalid")
    rows = {}
    for row in incidents["incidents"]:
        if (type(row) is not dict or set(row) != INCIDENT_FIELDS or type(row["id"]) is not str
                or not ID.fullmatch(row["id"]) or row["id"] in rows):
            raise IntakeError("source_invalid")
        rows[row["id"]] = {"id": row["id"], "feature": choice(row["feature"], FEATURES),
            "code": choice(row["category"], CODES), "origin": "feiyu_incident_policy",
            "stage": choice(row["stage"], STAGES), "state": choice(row["state"], {"closing", "paused", "resolved"}),
            "created_at": stamp(row["created_at"]), "resolved_at": stamp(row["resolved_at"]),
            "notification": choice(row["notification"], {"pending", "sent", "unknown", "not_requested"}),
            "trial_phase": "none", "trial_notification": "none", "trial_updated_at": 0}
    seen = set()
    for row in trials["trials"]:
        if (type(row) is not dict or set(row) != TRIAL_FIELDS or type(row["incident_id"]) is not str
                or not ID.fullmatch(row["incident_id"]) or row["incident_id"] in seen
                or row["incident_id"] not in rows):
            raise IntakeError("source_invalid")
        seen.add(row["incident_id"])
        target = rows[row["incident_id"]]
        if choice(row["feature"], FEATURES) != target["feature"]:
            raise IntakeError("source_invalid")
        target.update(trial_phase=choice(row["phase"], PHASES),
                      trial_notification=choice(row["notification"], {"none", "pending", "sent", "unknown"}),
                      trial_updated_at=stamp(row["updated_at"]))
    return rows


def atomic(path, value):
    if path.is_symlink():
        raise IntakeError("intake_invalid")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Intake:
    def __init__(self, source=SOURCE, state_dir=STATE):
        self.source, self.folder = Path(source).resolve(), Path(state_dir).resolve()
        if self.source == self.folder or self.source in self.folder.parents or self.folder in self.source.parents:
            raise IntakeError("paths_overlap")
        self.state_path = self.folder / "state.json"

    @contextmanager
    def locked(self):
        if self.folder.is_symlink():
            raise IntakeError("intake_invalid")
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / "intake.lock"
        if path.is_symlink():
            raise IntakeError("intake_invalid")
        with path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0"); stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def sources(self):
        paths = [self.source / "incidents.json", self.source / "incident-new-content-trials.json"]
        sentinels = [p.with_name(p.name + ".initialized") for p in paths]
        expected = [b"feiyu-incident-policy-v1\n", b"feiyu-new-content-retry-v1\n"]
        before = [read(p, 1024 * 1024) for p in paths]
        if [read(p, 64) for p in sentinels] != expected:
            raise IntakeError("source_invalid")
        rows = project(*(parse(raw) for raw in before))
        if before != [read(p, 1024 * 1024) for p in paths]:
            raise IntakeError("source_changing")
        return rows

    def state(self):
        if not self.state_path.exists():
            raise IntakeError("not_initialized")
        state = parse(read(self.state_path, 16 * 1024 * 1024))
        if (type(state) is not dict or set(state) != {"schema", "records", "acked"}
                or type(state["schema"]) is not int or state["schema"] != 1
                or type(state["records"]) is not dict or type(state["acked"]) is not list
                or any(type(i) is not str or not HEX.fullmatch(i) for i in state["acked"])):
            raise IntakeError("intake_invalid")
        for key, entry in state["records"].items():
            if (not ID.fullmatch(key) or type(entry) is not dict or set(entry) != {"value", "scan_ns"}
                    or type(entry["scan_ns"]) is not int or entry["scan_ns"] <= 0
                    or not valid_event(entry["value"])
                    or entry["value"]["id"] != key):
                raise IntakeError("intake_invalid")
        return state

    def run(self, action, snapshot_id=None):
        with self.locked():
            if action == "initialize":
                if self.state_path.exists() or self.state_path.is_symlink():
                    raise IntakeError("already_initialized")
                rows = self.sources()
                atomic(self.state_path, {"schema": 1,
                    "records": {key: {"value": value, "scan_ns": 1} for key, value in rows.items()}, "acked": []})
                return {"ok": True, "code": "initialized", "baseline_count": len(rows)}
            state, rows = self.state(), self.sources()
            if not set(state["records"]).issubset(rows):
                raise IntakeError("source_incomplete")
            if any(entry["value"]["trial_phase"] != "none" and rows[key]["trial_phase"] == "none"
                   for key, entry in state["records"].items()):
                raise IntakeError("source_incomplete")
            if action == "scan":
                events = [value for key, value in sorted(rows.items())
                          if key not in state["records"] or status(value) != status(state["records"][key]["value"])]
                if not events:
                    return {"ok": True, "code": "no_changes", "events": []}
                prior = {r["id"]: state["records"].get(r["id"]) for r in events}
                snapshot_id = fingerprint({"events": events, "prior": prior})
                folder = self.folder / "snapshots"
                if folder.is_symlink():
                    raise IntakeError("intake_invalid")
                folder.mkdir(exist_ok=True)
                path = folder / (snapshot_id + ".json")
                if not path.exists():
                    # Allocate under the intake lock; wall-clock changes cannot reorder acknowledgements.
                    sequences = [r["scan_ns"] for r in state["records"].values()]
                    for previous in folder.glob("*.json"):
                        if not HEX.fullmatch(previous.stem):
                            raise IntakeError("snapshot_invalid")
                        sequences.append(self.snapshot(previous.stem)["scan_ns"])
                    payload = {"schema": 1, "snapshot_id": snapshot_id, "events": events,
                               "scan_ns": max(sequences, default=0) + 1}
                    atomic(path, {"payload": payload, "signature": fingerprint(payload)})
                self.snapshot(snapshot_id)
                return {"ok": True, "code": "changes", "snapshot_id": snapshot_id, "events": events}
            if action != "ack" or type(snapshot_id) is not str or not HEX.fullmatch(snapshot_id):
                raise IntakeError("invalid_action")
            if snapshot_id in state["acked"]:
                return {"ok": True, "code": "already_acknowledged", "snapshot_id": snapshot_id}
            payload = self.snapshot(snapshot_id)
            if any(value["id"] not in rows for value in payload["events"]):
                raise IntakeError("source_incomplete")
            if any(value["trial_phase"] != "none" and rows[value["id"]]["trial_phase"] == "none"
                   for value in payload["events"]):
                raise IntakeError("source_incomplete")
            for value in payload["events"]:
                existing = state["records"].get(value["id"])
                if existing is None or payload["scan_ns"] > existing["scan_ns"]:
                    state["records"][value["id"]] = {"value": value, "scan_ns": payload["scan_ns"]}
            state["acked"].append(snapshot_id)
            atomic(self.state_path, state)
            return {"ok": True, "code": "acknowledged", "snapshot_id": snapshot_id}

    def snapshot(self, snapshot_id):
        if (self.folder / "snapshots").is_symlink():
            raise IntakeError("intake_invalid")
        value = parse(read(self.folder / "snapshots" / (snapshot_id + ".json"), 1024 * 1024))
        if (type(value) is not dict or set(value) != {"payload", "signature"}
                or type(value["signature"]) is not str
                or value["signature"] != fingerprint(value["payload"])):
            raise IntakeError("snapshot_invalid")
        payload = value["payload"]
        if (type(payload) is not dict or set(payload) != {"schema", "snapshot_id", "events", "scan_ns"}
                or payload["schema"] != 1 or payload["snapshot_id"] != snapshot_id
                or type(payload["scan_ns"]) is not int or payload["scan_ns"] <= 0
                or type(payload["events"]) is not list or not payload["events"]
                or any(not valid_event(r) for r in payload["events"])):
            raise IntakeError("snapshot_invalid")
        return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--initialize", action="store_true")
    action.add_argument("--scan", action="store_true")
    action.add_argument("--ack", metavar="SNAPSHOT_ID")
    parser.add_argument("--source-dir", type=Path, default=SOURCE, help="Isolated test fixture override")
    parser.add_argument("--state-dir", type=Path, default=STATE, help="Separate intake data directory")
    args = parser.parse_args()
    try:
        result = Intake(args.source_dir, args.state_dir).run(
            "initialize" if args.initialize else "scan" if args.scan else "ack", args.ack)
    except IntakeError as error:
        result = {"ok": False, "code": str(error)}
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        result = {"ok": False, "code": "intake_unavailable"}
    print(encode(result).decode("ascii"))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
