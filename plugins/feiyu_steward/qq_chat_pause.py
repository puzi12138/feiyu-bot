"""Persistent manual pause for QQ chat, independent of tariff policy."""

import json
import math
import os
from pathlib import Path
import sys
import time
import uuid


STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "plugin_data"
    / "feiyu_steward"
    / "qq-chat-pause.json"
)
_MAX_STATE_BYTES = 2_000_000


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def read_qq_chat_pause(path=None):
    """Read the state; missing defaults open and invalid state fails closed."""
    path = Path(path) if path is not None else STATE_FILE
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return {"valid": True, "present": False, "paused": False, "intervals": []}
    except OSError:
        return {"valid": False, "present": True, "paused": True, "intervals": []}
    try:
        if len(raw) > _MAX_STATE_BYTES:
            raise ValueError("state_too_large")
        state = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(state, dict) or set(state) != {"version", "paused", "intervals"}:
            raise ValueError("invalid_state")
        if type(state["version"]) is not int or state["version"] != 1 or not isinstance(state["paused"], bool):
            raise ValueError("invalid_state")
        intervals = state["intervals"]
        if not isinstance(intervals, list):
            raise ValueError("invalid_state")
        previous_end = 0.0
        for index, interval in enumerate(intervals):
            if not isinstance(interval, dict) or set(interval) not in (
                    {"start", "end"}, {"start", "end", "start_exact", "end_exact"}):
                raise ValueError("invalid_interval")
            start, end = interval["start"], interval["end"]
            if not _number(start) or start < 0 or start < previous_end:
                raise ValueError("invalid_interval")
            if "start_exact" in interval:
                exact_start, exact_end = interval["start_exact"], interval["end_exact"]
                if (not _number(exact_start) or exact_start < 0
                        or not math.floor(exact_start) <= start <= math.ceil(exact_start)):
                    raise ValueError("invalid_interval")
                if ((end is None and exact_end is not None)
                        or (end is not None and (not _number(exact_end)
                            or exact_end < exact_start or end != math.ceil(exact_end)))):
                    raise ValueError("invalid_interval")
            if end is None:
                if index != len(intervals) - 1 or not state["paused"]:
                    raise ValueError("invalid_interval")
            elif not _number(end) or end < start:
                raise ValueError("invalid_interval")
            else:
                previous_end = end
        open_interval = bool(intervals and intervals[-1]["end"] is None)
        if open_interval != state["paused"]:
            raise ValueError("invalid_interval")
        return {"valid": True, "present": True, "paused": state["paused"], "intervals": intervals}
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return {"valid": False, "present": True, "paused": True, "intervals": []}


def qq_chat_paused():
    return read_qq_chat_pause()["paused"]


def qq_chat_pause_status():
    state = read_qq_chat_pause()
    return {"paused": state["paused"], "valid": state["valid"],
            "configured": state["present"], "intervals": len(state["intervals"])}


def qq_chat_was_paused(at):
    """Reject an unknown time or an instant in a persisted pause interval."""
    state = read_qq_chat_pause()
    if not state["valid"]:
        return True
    if not state["intervals"]:
        return False
    try:
        if isinstance(at, bool):
            return True
        at = float(at)
        if not math.isfinite(at) or at < 0:
            return True
    except (TypeError, ValueError, OverflowError):
        return True
    return any(row["start"] <= at and (row["end"] is None or at <= row["end"])
               for row in state["intervals"])


def qq_chat_pending_before_pause(at, observed_at):
    """Recognize an existing row observed before a same-second pause began."""
    state = read_qq_chat_pause()
    if not state["valid"]:
        return False
    try:
        if isinstance(at, bool) or isinstance(observed_at, bool):
            return False
        at, observed_at = float(at), float(observed_at)
        if (not math.isfinite(at) or not math.isfinite(observed_at)
                or min(at, observed_at) < 0 or at > observed_at):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    matched = False
    for row in state["intervals"]:
        if row["start"] <= at and (row["end"] is None or at <= row["end"]):
            matched = True
            exact = row.get("start_exact")
            if (exact is None or math.floor(at) != math.floor(exact)
                    or math.floor(observed_at) != math.floor(exact) or observed_at >= exact):
                return False
    return matched


def set_qq_chat_paused(paused, path=None, now=None):
    """Atomically change state while retaining every earlier pause interval."""
    if not isinstance(paused, bool):
        raise ValueError("pause_must_be_boolean")
    path = Path(path) if path is not None else STATE_FILE
    state = read_qq_chat_pause(path)
    if not state["valid"]:
        raise ValueError("invalid_pause_state")
    if paused == state["paused"] and state["present"]:
        return state
    stamp = time.time() if now is None else now
    if not _number(stamp) or stamp < 0:
        raise ValueError("invalid_time")
    intervals = list(state["intervals"])
    if paused:
        # QQ history may timestamp messages only to the nearest whole second.
        rounded_start = math.floor(stamp)
        if intervals:
            rounded_start = max(rounded_start, intervals[-1]["end"])
        if rounded_start > math.ceil(stamp):
            raise ValueError("clock_before_previous_interval")
        intervals.append({"start": rounded_start, "end": None,
                          "start_exact": stamp, "end_exact": None})
    elif intervals and intervals[-1]["end"] is None:
        interval = dict(intervals[-1])
        if "start_exact" in interval:
            exact_end = max(stamp, interval["start_exact"])
            interval.update(end=math.ceil(exact_end), end_exact=exact_end)
        else:
            interval["end"] = max(math.ceil(stamp), interval["start"])
        intervals[-1] = interval
    data = {"version": 1, "paused": paused, "intervals": intervals}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return read_qq_chat_pause(path)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"pause", "resume", "status"}:
        raise SystemExit("usage: qq_chat_pause.py pause|resume|status")
    if sys.argv[1] != "status":
        set_qq_chat_paused(sys.argv[1] == "pause")
    print(json.dumps(qq_chat_pause_status(), sort_keys=True))
