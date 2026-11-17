"""Conservative hold for automatic producers after a stale state recovery.

The independent adapter, incident, and reconnect ledgers are not changed here.
Legacy and malformed markers keep every affected lane closed. Schema 2 permits
only explicitly listed lanes with an implemented fresh-content boundary or
separately proven complete deduplication ledger.
This module never migrates a marker, changes authorization, or replays work.
"""

from __future__ import annotations

import functools
import inspect
import math
import time


MARKER = "_state_recovery_v1"
DEFAULT_HELD_LANES = (
    "publication_notices", "private_items", "comment_sharing", "hltv",
    "token_usage_reports", "heybox_history_review", "listen_together",
    "owner_proactive", "bili_owner_comments", "bili_conversations",
    "qzone_publication", "heybox_autopost", "heybox_qzone_sync", "reconnect_history",
)

# These loops have no independent housekeeping that needs to run while held.
# Keep token_usage_monitor (which also maintains observers), journal_history_monitor
# (which also services the independent Bili ledger), reconnect, peak-deferred,
# incident recovery, and the independent friend-time notice loop running.
HELD_MONITORS = {
    "heybox_review_notifications": "heybox_history_review",
    "comment_share_monitor": "comment_sharing",
    "hltv_monitor": "hltv",
    "listen_together_monitor": "listen_together",
    "peak_owner_chat_monitor": "owner_proactive",
    "heybox_autopost_monitor": "heybox_autopost",
    "heybox_qzone_sync_monitor": "heybox_qzone_sync",
}

# Extend this set only with an installed, tested lane boundary. A valid-looking
# marker cannot resume an unimplemented producer or a deliberately cancelled one.
# Bili owner comments are a ledger-proven exact resumption: the independent
# paused period has no newer comment/share actions and all 116 old rows stay.
RESUMABLE_LANES = frozenset({
    "publication_notices", "token_usage_reports", "bili_owner_comments",
    "private_items", "owner_proactive",
})


def _valid_marker(marker):
    """Validate the entire marker before accepting any partial resumption.

    Args:
        marker: Persisted recovery metadata.

    Returns:
        Whether the marker is a recognized, fully valid schema.
    """
    if (not isinstance(marker, dict) or marker.get("status") != "held"
            or not isinstance(marker.get("held_lanes"), list)
            or not marker["held_lanes"]
            or any(not isinstance(lane, str) or lane not in DEFAULT_HELD_LANES
                   for lane in marker["held_lanes"])
            or len(set(marker["held_lanes"])) != len(marker["held_lanes"])):
        return False
    if set(marker) == {"status", "held_lanes"}:
        return True
    if (set(marker) != {"schema", "status", "held_lanes", "resumed_lanes"}
            or type(marker.get("schema")) is not int or marker["schema"] != 2
            or set(marker["held_lanes"]) != set(DEFAULT_HELD_LANES)
            or not isinstance(marker.get("resumed_lanes"), dict)):
        return False
    for lane, boundary in marker["resumed_lanes"].items():
        if (lane not in RESUMABLE_LANES or not isinstance(boundary, dict)
                or set(boundary) != {"since", "not_before"}
                or any(type(boundary.get(name)) not in (int, float)
                       or not 0 < boundary[name] <= 253402300799
                       or not math.isfinite(boundary[name])
                       for name in ("since", "not_before"))
                or boundary["not_before"] < boundary["since"]):
            return False
    return True


def recovery_boundary(state, lane):
    """Return a copy of an explicit validated lane boundary, never infer one.

    Args:
        state: The owning plugin's state object.
        lane: Exact name of the producer being checked.

    Returns:
        A boundary dictionary, or None when the lane has no approved boundary.
    """
    if not isinstance(state, dict):
        return None
    marker = state.get(MARKER)
    if not _valid_marker(marker) or marker.get("schema") != 2:
        return None
    boundary = marker["resumed_lanes"].get(lane)
    return dict(boundary) if isinstance(boundary, dict) else None


def recovery_held(state, lane):
    """Keep legacy, unsupported, malformed, and unlisted lanes closed.

    Args:
        state: The owning plugin's state object.
        lane: Exact name of the producer being checked.

    Returns:
        Whether the state-recovery guard must hold this producer.
    """
    if not isinstance(state, dict):
        return True
    if MARKER not in state:
        return False
    return recovery_boundary(state, lane) is None


def recovery_status(state):
    """Return only fixed, non-sensitive metadata about the recovery marker."""
    if not isinstance(state, dict):
        return {"held": True, "marker_valid": False, "held_lanes": []}
    if MARKER not in state:
        return {"held": False, "marker_valid": True, "held_lanes": []}
    marker = state[MARKER]
    valid = _valid_marker(marker)
    resumed = sorted(marker["resumed_lanes"]) if valid and marker.get("schema") == 2 else []
    return {"held": True, "marker_valid": bool(valid),
            "held_lanes": [lane for lane in marker["held_lanes"] if lane not in resumed] if valid else [],
            "resumed_lanes": resumed}


def held_monitor(state, name):
    """Identify only known state-dependent whole-loop monitors."""
    if name == "heybox_review_notifications":
        # Its publication segment is independently guarded. Keep the legacy
        # review and discovery segments closed inside the running loop.
        return (recovery_held(state, "heybox_history_review")
                and recovery_held(state, "publication_notices"))
    lane = HELD_MONITORS.get(name)
    return lane is not None and recovery_held(state, lane)


def _argument(method, args, kwargs, name, default=None):
    try:
        bound = inspect.signature(method).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return bound.arguments.get(name, default)
    except (TypeError, ValueError):
        return default


def _block(name, method, args, kwargs, *, permit_manual=True):
    """Return the normal result shape for a blocked automatic operation."""
    basic = {"ok": False, "reason": "state_recovery_hold"}
    if name in {"candidate", "send_private_opportunity"}:
        # Existing callers mark an explicitly requested fresh action as manual.
        if permit_manual and _argument(method, args, kwargs, "manual", False) is True:
            return None
        return {**basic, "published": False} if name == "candidate" else basic
    if name == "review_heybox_history":
        if permit_manual and _argument(method, args, kwargs, "manual", False) is True:
            return None
        return basic
    if name == "retry_bili_recommendation":
        # A new journal's automatic recommendation uses stale owner-comment
        # receipts. A current explicit owner request follows its normal path.
        if permit_manual and _argument(method, args, kwargs, "new_journal", None) is None:
            return None
        # ``disabled`` is an existing, non-sent status understood by the Bili
        # journal worker; a new status there would be recorded as unknown.
        return {"ok": False, "status": "disabled", "reason": "state_recovery_hold",
                "bvid": _argument(method, args, kwargs, "bvid", ""),
                "recommended": False,
                "dry_run": bool(_argument(method, args, kwargs, "dry_run", True))}
    if name == "heybox_qzone_sync_once":
        if permit_manual and _argument(method, args, kwargs, "backfill_id", None) is not None:
            return None
        return {**basic, "sent": False}
    if name == "send_publication_notifications":
        # Even a manual batch request would drain an unverified old ledger.
        return {**basic, "sent": False, "count": 0}
    if name in {"curate_own_comments", "hltv_check", "send_hltv_digest",
                "maybe_autonomous_heybox_post"}:
        return {**basic, "sent": False}
    if name == "maybe_send_token_usage_report":
        return {"ok": False, "sent": False, "status": "state_recovery_hold"}
    if name == "maybe_deliver_pending_heybox_owner_discovery":
        return basic
    raise ValueError("unknown_recovery_guard")


_METHODS = {
    "candidate": "private_items",
    "send_private_opportunity": "private_items",
    "maybe_deliver_pending_heybox_owner_discovery": "private_items",
    "send_publication_notifications": "publication_notices",
    "curate_own_comments": "comment_sharing",
    "hltv_check": "hltv",
    "send_hltv_digest": "hltv",
    "maybe_send_token_usage_report": "token_usage_reports",
    "review_heybox_history": "heybox_history_review",
    "retry_bili_recommendation": "bili_owner_comments",
    "maybe_autonomous_heybox_post": "heybox_autopost",
    "heybox_qzone_sync_once": "heybox_qzone_sync",
}


class RecoveryGuards:
    def __init__(self, host):
        self.host = host
        self.bindings = []
        self.missing = []
        for name, lane in _METHODS.items():
            original = getattr(host, name, None)
            if not inspect.iscoroutinefunction(original):
                self.missing.append(name)
                continue
            wrapped = self._wrap(name, lane, original)
            setattr(host, name, wrapped)
            self.bindings.append((name, original, wrapped))

    def _wrap(self, name, lane, original):
        @functools.wraps(original)
        async def wrapped(*args, **kwargs):
            selected_lane = lane
            if name == "candidate" and _argument(original, args, kwargs, "channel", "draft") == "draft":
                selected_lane = "qzone_publication"
            state = getattr(self.host, "state", None)
            boundary = recovery_boundary(state, selected_lane)
            if (recovery_held(state, selected_lane)
                    or boundary is not None and time.time() < boundary["not_before"]):
                permit_manual = isinstance(state, dict) and _valid_marker(state.get(MARKER))
                result = _block(name, original, args, kwargs, permit_manual=permit_manual)
                if result is not None:
                    return result
            return await original(*args, **kwargs)
        return wrapped

    def status(self):
        return {"installed": len(self.bindings), "missing": sorted(self.missing)}

    def restore(self):
        for name, original, wrapped in reversed(self.bindings):
            if getattr(self.host, name, None) is wrapped:
                setattr(self.host, name, original)
        self.bindings = []


def install_recovered_state_guards(host):
    """Wrap this host's specified methods before registering external routes.

    The caller must preserve/restore the handle with other plugin hooks and
    must not claim readiness if required methods are missing. Three final
    boundaries are separate: Bili's owner-mention closure and conversation
    guard, and Qzone's native publisher guard.
    """
    return RecoveryGuards(host)
