"""Reversible incident gates for identified automatic business entry points.

The incident ledger and notice delivery belong to ``host.incidents``.  This
module neither starts a task nor talks to a platform.  It wraps existing single
cycles and the last available authorization checks; it does not wrap a worker
loop.  A plugin may catch its own failures before these boundaries, and an
already dispatched network operation cannot be recalled.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import math
import sqlite3
import ssl
import sys
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar

from .failure_diagnostics import record_failure


_SYNC_AUTOMATIC = ContextVar("feiyu_incident_qzone_sync", default=False)
_SYNC_BARRIER = ContextVar("feiyu_incident_qzone_sync_barrier", default=None)
_BILI_AUTOMATIC = ContextVar("feiyu_incident_bili_browse", default=False)
_BILI_SHARE = ContextVar("feiyu_incident_bili_share", default=False)
_HEYBOX_COMMENT = ContextVar("feiyu_incident_heybox_comment", default=None)
_HEYBOX_COMMENT_BARRIER = ContextVar("feiyu_incident_heybox_comment_barrier", default=None)
_HEYBOX_VISION_GROUP = ContextVar("feiyu_incident_heybox_vision_group", default=None)
_QZONE_PUBLICATION = ContextVar("feiyu_incident_qzone_publication", default=False)
_NOTICE_POLL_TRACE = ContextVar("feiyu_incident_notice_poll_trace", default=None)
_NOTICE_SERVICE_SCOPE = ContextVar("feiyu_incident_notice_service_scope", default=None)
_AUTOMATIC_FEED_SCOPE = ContextVar("feiyu_incident_automatic_feed_scope", default=None)
_HEYBOX_RETRY_INTAKE = ContextVar("feiyu_incident_retry_intake", default=None)
_HEYBOX_RETRY_EVENT = ContextVar("feiyu_incident_retry_event", default=None)
_HEYBOX_MODEL_EXCEPTION = ContextVar("feiyu_incident_model_exception", default=None)
_ABSENT = object()
_ACTIVE_INCIDENT_HOOKS = None
_HEYBOX_API_MODULE = "data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.api_client"
_HEYBOX_API_CATEGORIES = {
    "send_unknown": "delivery_unknown",
    "credential_invalid": "credential_invalid",
    "network": "connection_failed",
    "http": "request_failed",
    "server": "service_unavailable",
    "rate_limited": "rate_limited",
    "forbidden": "access_denied",
    "upstream_rejected": "request_rejected",
    "response_shape": "response_invalid",
    "platform_content_rejected": "platform_content_rejected",
}

# Fixed result codes only.  Empty results, ordinary policy/interest skips and
# plain False values have different meanings in these upstream functions.
_AUTOPOST_FAILURE = {
    "storage_unavailable": "storage_failed",
    "approved_gallery_unavailable": "storage_failed",
    "comic_story_history_invalid": "storage_failed",
    "preflight_unavailable": "request_failed",
    "public_art_result_unknown": "delivery_unknown",
    "public_art_unavailable": "model_failed",
    "autopost_not_completed": "unexpected_exception",
}
_SYNC_FAILURE = {
    "qzone_guard_unavailable": "guard_failed",
    "sync_image_storage_unavailable": "storage_failed",
    "source_list_unavailable": "request_failed",
    "source_id_invalid": "request_failed",
    "source_pagination_unavailable": "request_failed",
    "source_pagination_unstable": "request_failed",
    "source_list_contract_changed": "request_failed",
    "qzone_request_unavailable": "request_failed",
    "qzone_response_invalid": "request_failed",
    "qzone_feed_unavailable": "request_failed",
    "qzone_feed_contract_changed": "request_failed",
    "qzone_feed_unstable": "request_failed",
    "qzone_feed_incomplete": "request_failed",
    "qzone_detail_unavailable": "request_failed",
    "sync_unavailable": "unexpected_exception",
    "qzone_receipt_missing": "delivery_unknown",
    "sync_readback_unconfirmed": "delivery_unknown",
}
_SHARE_FAILURE = {
    "plugin_unavailable": "guard_failed",
    "mention_guard_unavailable": "guard_failed",
    "invalid_native_logs": "storage_failed",
    "failed": "unexpected_exception",
    "unknown": "delivery_unknown",
}
_QZONE_CANDIDATE_FAILURE = {
    "source_fetch_failed": "request_failed",
    "qzone_unavailable": "guard_failed",
    "TimeoutError": "request_failed",
    "ConnectionError": "request_failed",
    "OSError": "unexpected_exception",
    "ValueError": "unexpected_exception",
    "TypeError": "unexpected_exception",
    "KeyError": "unexpected_exception",
    "AttributeError": "unexpected_exception",
    "RuntimeError": "unexpected_exception",
    "JSONDecodeError": "unexpected_exception",
}
_IGNORED_EXCEPTIONS = frozenset({
    ("data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.api_client", "RequestNotAuthorized"),
    ("data.plugins.feiyu_steward.peak_policy", "PeakTariffPaused"),
    ("data.plugins.feiyu_steward.peak_runtime", "OwnerScopeRequired"),
    ("data.plugins.feiyu_steward.incident_hooks", "IncidentFeaturePaused"),
})


class IncidentFeaturePaused(RuntimeError):
    """An automatic operation cannot continue through a retired boundary."""


@contextmanager
def automatic_qzone_publication_scope():
    """Mark a known automatic call for incident checks; grants no authority."""
    token = _QZONE_PUBLICATION.set(True)
    try:
        yield
    finally:
        _QZONE_PUBLICATION.reset(token)


def _heybox_api_category(error):
    """Return only a fixed category from the currently loaded adapter class."""
    module = sys.modules.get(_HEYBOX_API_MODULE)
    base = getattr(module, "XiaoheiheApiError", None)
    mro = type(error).__mro__
    if (not isinstance(base, type) or base.__module__ != _HEYBOX_API_MODULE
            or not any(item is base for item in mro)):
        return None
    uncertain = getattr(module, "SendUncertainError", None)
    invalid = getattr(module, "CredentialInvalidError", None)
    if isinstance(uncertain, type) and any(item is uncertain for item in mro):
        return "send_unknown"
    if isinstance(invalid, type) and any(item is invalid for item in mro):
        return "credential_invalid"
    rejected = getattr(module, "PlatformContentRejectedError", None)
    if (isinstance(rejected, type) and rejected.__module__ == _HEYBOX_API_MODULE
            and any(item is rejected for item in mro)):
        return "platform_content_rejected"
    category = getattr(error, "category", None)
    # Content rejection is definitive only for the adapter's dedicated class;
    # a category string on an unrelated or generic exception cannot assert it.
    if category == "platform_content_rejected":
        return None
    return category if type(category) is str and category in _HEYBOX_API_CATEGORIES else None


def _source_time(notification):
    """Read a source timestamp without using the parser's observation fallback."""
    raw = getattr(notification, "raw", None)
    kind = getattr(getattr(notification, "event_type", None), "value", "")
    return _source_time_raw(kind, raw)


def _source_time_raw(kind, raw):
    """Use the original source fields, including for synthetic feed events."""
    if not isinstance(raw, Mapping):
        return None
    if kind not in {"mention", "reply", "proactive_feed"}:
        return None
    message_type = raw.get("message_type")
    if message_type is None or message_type == "":
        message_type = raw.get("type", "")
    post_mention = kind == "mention" and str(message_type).casefold() == "16"
    if kind == "proactive_feed" or post_mention:
        source = raw.get("post", raw.get("link", {}))
        source = source if isinstance(source, Mapping) else {}
    else:
        source = raw.get("comment")
        source = source if isinstance(source, Mapping) else raw
    value = None
    for mapping, keys in ((source, ("created_at", "create_at", "timestamp", "time")),
                          (raw, ("created_at", "timestamp", "time"))):
        for key in keys:
            candidate = mapping.get(key)
            if candidate is not None and candidate != "":
                value = candidate
                break
        if value is not None:
            break
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    if stamp > 10_000_000_000:
        stamp /= 1000
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def _category(error):
    """Use only fixed categories; do not inspect or retain exception text."""
    if (isinstance(error, IncidentFeaturePaused)
            or any((base.__module__, base.__name__) in _IGNORED_EXCEPTIONS
                   for base in type(error).__mro__)):
        return None
    api_category = _heybox_api_category(error)
    if api_category is not None:
        return _HEYBOX_API_CATEGORIES[api_category]
    if isinstance(error, sqlite3.Error):
        return "storage_failed"
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "request_failed"
    if isinstance(error, OSError):
        return "storage_failed"
    return "unexpected_exception"


def _retry_exception_kind(error):
    """Accept only real transport classes; no error text/category grants a trial."""
    chain, current = [], error
    while isinstance(current, BaseException) and all(current is not item for item in chain) and len(chain) < 8:
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return None
    if any(isinstance(item, PermissionError) or _heybox_api_category(item) in {
            "credential_invalid", "forbidden", "rate_limited", "send_unknown"} for item in chain):
        return None
    for module_name, names in (
        ("openai", ("AuthenticationError", "PermissionDeniedError", "RateLimitError", "APIStatusError")),
        ("httpx", ("HTTPStatusError",)),
    ):
        module = sys.modules.get(module_name)
        if any(isinstance(cls, type) and isinstance(item, cls)
               for item in chain for cls in (getattr(module, name, None) for name in names)):
            return None
    for kind, checks in (
        ("timeout", (("openai", "APITimeoutError"), ("httpx", "TimeoutException"))),
        ("connection", (("openai", "APIConnectionError"), ("httpx", "ConnectError"))),
    ):
        for module_name, name in checks:
            cls = getattr(sys.modules.get(module_name), name, None)
            if isinstance(cls, type) and isinstance(error, cls):
                return kind
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionResetError):
        return "connection"
    security = sys.modules.get("data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.security")
    cls = getattr(security, "ImageHostResolutionError", None)
    if isinstance(cls, type) and isinstance(error, cls) and len(chain) > 1:
        return _retry_exception_kind(chain[1])
    return None


@asynccontextmanager
async def fresh_heybox_processing(adapter, notification, event_id):
    """Enter only from the adapter's native fresh-claim processing bridge."""
    owner = _ACTIVE_INCIDENT_HOOKS
    if owner is None or owner._active is not True:
        yield False
        return
    async with owner._fresh_processing(adapter, notification, event_id) as allowed:
        yield allowed


def consume_heybox_retry_wake(adapter, feed):
    """Consume a wake signal without granting source/model/send permission."""
    owner = _ACTIVE_INCIDENT_HOOKS
    return owner is not None and owner._consume_retry_wake(adapter, feed) is True


def _request_not_authorized():
    """Build the adapter's fixed pause exception without importing it eagerly."""
    module = sys.modules.get(_HEYBOX_API_MODULE)
    cls = getattr(module, "RequestNotAuthorized", None)
    if isinstance(cls, type):
        try:
            if issubclass(cls, BaseException):
                return cls("incident_hooks_not_ready")
        except TypeError:
            pass
    return IncidentFeaturePaused("incident_hooks_not_ready")


def _binding_is_current(owner, obj, name, wrapper=None):
    """Check that an owner still controls one exact current attribute."""
    try:
        current = getattr(obj, name, None)
        bindings = getattr(owner, "_bindings", {})
        matches = [item for item in bindings.values()
                   if item.get("object") is obj and item.get("name") == name]
        if len(matches) != 1 or matches[0].get("wrapped") is not current:
            return False
        return wrapper is None or matches[0].get("wrapped") is wrapper
    except Exception:
        return False


def _current_heybox(owner):
    """Return the current adapter plugin, runtime and service instances."""
    plugin = owner.host.plugin("astrbot_plugin_xiaoheihe_adapter")
    runtime = getattr(plugin, "runtime", None)
    adapters = getattr(runtime, "_adapters", ())
    if isinstance(adapters, Mapping):
        adapters = adapters.values()
    services = []
    for adapter in tuple(adapters):
        service = getattr(adapter, "_service", None)
        if service is not None:
            services.append(service)
    return plugin, runtime, tuple(services)


def _feed_chain_is_current(owner, feed):
    """Verify the steward daytime feed wrapper and any active peak wrapper."""
    guard = getattr(owner.host, "_heybox_feed_guard", None)
    if (not isinstance(guard, tuple) or len(guard) != 3
            or not callable(guard[1]) or not callable(guard[2])):
        return False
    cls, original, daytime = guard
    if cls is not type(feed):
        return False
    current = cls.__dict__.get("run_once", _ABSENT)
    if not callable(current):
        return False
    seen = set()
    found_daytime = False
    while callable(current) and id(current) not in seen:
        seen.add(id(current))
        if current is daytime:
            found_daytime = True
            break
        state = getattr(current, "_peak_background_state", None)
        if not isinstance(state, dict):
            # An unknown wrapper between the known guards is unsafe.  The
            # native feed could otherwise bypass the intended chain.
            return False
        handle = getattr(owner.host, "_peak_background_handle", None)
        if (state.get("owner") is not handle or state.get("wrapped") is not current
                or state.get("active") is not True):
            return False
        current = getattr(current, "__wrapped__", None)
    return found_daytime


def notice_service_ready(service, operation):
    """Authorize one native notification entry only through current wrappers.

    Native methods call this from inside their adapter instance.  The scoped
    context is deliberately required, so a direct call to an old unwrapped
    method cannot become authorized merely because a refresh installs wrappers.
    """
    global _ACTIVE_INCIDENT_HOOKS
    owner = _ACTIVE_INCIDENT_HOOKS
    if (owner is None or getattr(owner, "_active", False) is not True
            or operation not in {"poll", "enqueue", "handle"}):
        return False
    scope = _NOTICE_SERVICE_SCOPE.get()
    if (not isinstance(scope, tuple) or len(scope) != 4
            or scope[0] is not owner or scope[1] is not service
            or scope[2] != operation):
        return False
    try:
        # Refresh synchronously to close the first-start and replacement gap.
        # The scope still has to match the resulting exact wrapper below.
        owner.refresh()
        if getattr(owner, "_active", False) is not True:
            return False
        _, runtime, services = _current_heybox(owner)
        if runtime is None or not any(current is service for current in services):
            return False
        method_names = {"poll": "poll_once", "enqueue": "enqueue", "handle": "_handle"}
        if not all(_binding_is_current(owner, service, name)
                   for name in ("poll_once", "enqueue", "_handle")):
            return False
        if not (_binding_is_current(owner, runtime, "deliver")
                and _binding_is_current(owner, runtime, "get_client")):
            return False
        return _binding_is_current(owner, service, method_names[operation], scope[3])
    except Exception:
        return False


def automatic_feed_ready(feed):
    """Authorize native feed work only through the current owner chain."""
    owner = _ACTIVE_INCIDENT_HOOKS
    if owner is None or getattr(owner, "_active", False) is not True:
        return False
    scope = _AUTOMATIC_FEED_SCOPE.get()
    if (not isinstance(scope, tuple) or len(scope) != 3
            or scope[0] is not owner or scope[1] is not feed):
        return False
    try:
        owner.refresh()
        if getattr(owner, "_active", False) is not True:
            return False
        if not _binding_is_current(owner, owner.host, "run_automatic_heybox_feed", scope[2]):
            return False
        return _feed_chain_is_current(owner, feed)
    except Exception:
        return False


def automatic_feed_snapshot(feed, *, issuer=None):
    """Capture only the currently bound, unpaused native feed generation."""
    owner = _ACTIVE_INCIDENT_HOOKS
    if issuer is None:
        if not automatic_feed_ready(feed):
            return None
    elif (issuer is not owner or getattr(owner, "_active", False) is not True
          or not _binding_is_current(owner, owner.host, "run_automatic_heybox_feed")
          or not _feed_chain_is_current(owner, feed)):
        return None
    features = ("heybox_comment", "heybox_vision", "heybox_feed")
    try:
        if owner is None or (owner._blocked(*features) and not owner._retry_intake_allowed()):
            return None
        intake = _HEYBOX_RETRY_INTAKE.get()
        snapshot = (owner.host.incidents.retry_snapshot(intake["incident_id"], features)
                    if owner._retry_intake_allowed() else owner._resume_snapshot(features))
        allowed = not owner._blocked(*features) or owner._retry_intake_allowed()
        current = (owner.host.incidents.retry_snapshot(intake["incident_id"], features)
                   if owner._retry_intake_allowed() else owner._resume_snapshot(features))
        return snapshot if allowed and current == snapshot else None
    except Exception:
        return None


def automatic_feed_issuer(feed, snapshot):
    return _ACTIVE_INCIDENT_HOOKS if snapshot is not None and automatic_feed_snapshot(feed) == snapshot else None


def automatic_feed_post_allowed(feed, post, *, snapshot=None):
    """A current fetch may discover an unseen older source; claim proves it."""
    if snapshot is None or not isinstance(post, Mapping) or automatic_feed_snapshot(feed) != snapshot:
        return False
    try:
        source = _source_time_raw("proactive_feed", {"post": post})
        if source is None or source > time.time():
            return False
        return automatic_feed_snapshot(feed) == snapshot
    except Exception:
        return False


class IncidentHooks:
    """Own only these bindings, and refresh them after plugin replacement."""

    def __init__(self, host):
        global _ACTIVE_INCIDENT_HOOKS
        self.host = host
        self._active = True
        self._bindings = {}
        self._missing = []
        # If ledger reporting itself fails, fail closed in this process too.
        self._local_blocked = set()
        self._notice_claims = {}
        self._heybox_rejection_pending = set()
        # Only fresh ordinary events claimed under this handle may use the
        # content-rejection exception. A restart never reclassifies old work.
        self._heybox_rejection_enabled_at = time.time()
        self._retry_enabled_at = time.time()
        self._retry_claims = {}
        self._retry_events = {}
        self._retry_reported = set()
        self._retry_post_reports = {}
        self._retry_wakes = {}
        self._retry_wake_seen = set()
        self._retry_epoch_attempted = False
        self._retry_available = False
        _ACTIVE_INCIDENT_HOOKS = self
        self.refresh()

    def _blocked(self, *features):
        # In-flight wrappers and callbacks can outlive restore().  They must
        # keep enforcing the incident policy instead of opening mid-send.
        try:
            policy = self.host.incidents
            event_grant = _HEYBOX_RETRY_EVENT.get()
            if (isinstance(event_grant, dict) and event_grant.get("trial") is not None
                    and (not self._active or not self._retry_available or self._missing
                         or _ACTIVE_INCIDENT_HOOKS is not self)):
                return True
            intake = _HEYBOX_RETRY_INTAKE.get()
            if (isinstance(intake, dict) and intake.get("owner") is self
                    and intake.get("task") is asyncio.current_task() and intake.get("claimed")
                    and _HEYBOX_RETRY_EVENT.get() is None
                    and any(feature.startswith("heybox_") for feature in features)):
                return True
            if "heybox_comment" in features and getattr(self, "_heybox_rejection_pending", None):
                return True
            return any(f in self._local_blocked or policy.blocked(f) is not False for f in features)
        except Exception:
            return True

    def _resume_barrier(self, features):
        try:
            value = self.host.incidents.resume_barrier(features)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < 0 or value > time.time()):
                return None
            return value
        except Exception:
            return None

    def _resume_snapshot(self, features):
        try:
            snapshot = self.host.incidents.resume_snapshot(features)
            if (not isinstance(snapshot, tuple) or len(snapshot) != 2
                    or type(snapshot[0]) not in (int, float) or not math.isfinite(snapshot[0])
                    or snapshot[0] < 0 or snapshot[0] > time.time()
                    or not isinstance(snapshot[1], tuple)):
                return None
            return snapshot
        except Exception:
            return None

    def _notice_denial(self, notification, features):
        if self._blocked(*features):
            return "incident_feature_paused"
        barrier = self._resume_barrier(features)
        if barrier is None:
            return "incident_barrier_unavailable"
        kind = getattr(getattr(notification, "event_type", None), "value", "")
        if not barrier:
            return ""
        stamp = _source_time(notification)
        if stamp is None or stamp > time.time() or math.floor(stamp) <= math.floor(barrier):
            return "incident_resume_barrier"
        return ""

    async def _report(self, feature, category, stage, *, retry=None):
        if category is None:
            return
        try:
            kwargs = {"stage": stage}
            if retry is not None:
                kwargs["retry"] = retry
            result = await self.host.incidents.report(feature, category, **kwargs)
            if not isinstance(result, dict) or result.get("blocked") is not True:
                self._local_blocked.add(feature)
            return result
        except Exception:
            # No exception body or dynamic identifier is ever logged here.
            self._local_blocked.add(feature)

    def _retry_intake_allowed(self):
        grant = _HEYBOX_RETRY_INTAKE.get()
        try:
            return bool(isinstance(grant, dict) and grant.get("owner") is self
                        and self._retry_available
                        and self.retry_local_preflight("heybox")
                        and not self._heybox_rejection_pending
                        and not self._local_blocked.intersection({"heybox_feed", "heybox_comment", "heybox_vision"})
                        and grant.get("task") is asyncio.current_task() and not grant.get("claimed")
                        and self.host.incidents.retry_intake_allowed(grant["incident_id"]) is True)
        except Exception:
            return False

    def retry_local_preflight(self, scope):
        """No trial may bypass this exact active owner's local safety latch."""
        features = ({"heybox_feed", "heybox_comment", "heybox_vision"} if scope == "heybox"
                    else {"bili_journal"} if scope == "bili_journal" else None)
        required = ({"feed", "heybox_vision", "heybox_vision_group", "heybox_vision_provider",
                     "heybox_model_failure", "heybox_delivery", "heybox_get_client",
                     "heybox_retry_register", "heybox_retry_pipeline", "heybox_retry_model_class"}
                    if scope == "heybox" else {"bili_journal"})
        try:
            return bool(features is not None and self._active and _ACTIVE_INCIDENT_HOOKS is self
                        and getattr(self.host, "_incident_hooks", None) is self
                        and self._retry_available and not self._missing
                        and not self._local_blocked.intersection(features)
                        and required.issubset(self._bindings)
                        and all(getattr(self._bindings[key]["object"], self._bindings[key]["name"], None)
                                is self._bindings[key]["wrapped"] for key in required))
        except Exception:
            return False

    def _retry_scope(self, grant):
        if grant is None or grant.get("trial") is None:
            return nullcontext()
        if not self.retry_local_preflight("heybox"):
            raise IncidentFeaturePaused("retry_owner_retired")
        trial = grant["trial"]
        return self.host.incidents.retry_scope(trial["incident_id"], "event", token=trial["token"],
                                               candidate_key=grant["post_id"], event_key=grant["event_key"])

    async def _retry_terminal(self, runtime, event_id, route):
        try:
            row = await runtime.repository.db.fetchone(
                "SELECT e.profile_id,e.post_id,e.event_type,e.status,e.claimed_at,e.raw_json,e.retry_count,e.next_retry_at,"
                "EXISTS(SELECT 1 FROM outgoing_replies o WHERE o.incoming_event_id=e.id) AS outgoing,"
                "r.revoked FROM incoming_events e LEFT JOIN feiyu_auto_reply_reservations r ON r.event_id=e.id WHERE e.id=?",
                (event_id,))
            if not row:
                return False
            raw = json.loads(row["raw_json"])
            if (not isinstance(raw, dict) or any(str(key).startswith("_feiyu_recovery") for key in raw)
                    or row["retry_count"] != 0 or row["next_retry_at"] not in (None, 0)):
                return False
            for table in ("feiyu_recovery_attempts", "feiyu_recovery_permission_revision1_attempts",
                          "feiyu_recovery_owner_renewal1_attempts"):
                exists = await runtime.repository.db.fetchone(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
                if exists and await runtime.repository.db.fetchone(
                        "SELECT 1 FROM " + table + " WHERE new_event_id=?", (event_id,)):
                    return False
            return bool(row["profile_id"] == route.profile_id and row["post_id"] == route.post_id
                        and row["event_type"] in {"proactive_feed", "reply", "mention"}
                        and row["status"] in {"ignored", "dead_letter"} and not row["outgoing"]
                        and row["revoked"] == 1 and row["claimed_at"] >= self._retry_enabled_at)
        except Exception:
            return False

    async def _retry_finish_grant(self, grant, outcome):
        if grant is None or grant.get("trial") is None or grant.get("finished"):
            return False
        trial = grant["trial"]
        grant["finished"] = True
        return await self.host.incidents.retry_finish(trial["incident_id"], trial["token"],
            grant["post_id"], grant["event_key"], outcome=outcome)

    async def _retry_delivery_result(self, runtime, event_id, result):
        grant = self._retry_claims.get((id(runtime), event_id))
        if grant is None or grant.get("trial") is None or grant.get("finished"):
            return
        success = False
        if (isinstance(result, dict) and result.get("status") == "sent"
                and result.get("external_comment_id")
                and not result.get("duplicate_send_suppressed") and not result.get("confirmed_existing_attempt")):
            rows = await runtime.repository.db.fetchall(
                "SELECT e.status AS event_status,o.status,o.external_comment_id,o.confirmed_at,o.attempted_at "
                "FROM incoming_events e JOIN outgoing_replies o ON o.incoming_event_id=e.id "
                "WHERE e.id=? AND e.profile_id=? AND e.post_id=?",
                (event_id, grant["notification"].profile_id, grant["post_id"]))
            success = bool(len(rows) == 1 and rows[0]["event_status"] == rows[0]["status"] == "sent"
                           and rows[0]["external_comment_id"] == result["external_comment_id"]
                           and type(rows[0]["confirmed_at"]) in (int, float)
                           and rows[0]["confirmed_at"] >= rows[0]["attempted_at"] >= grant["notification"].observed_at)
        await self._retry_finish_grant(grant, "success" if success else "unknown")

    async def _retry_report_failure(self, runtime, event_id, route, feature, category, stage, kind):
        if runtime is None:
            await self._report(feature, category, stage)
            return False
        grant = self._retry_claims.get((id(runtime), event_id))
        if grant is not None and grant.get("trial") is not None:
            await self._retry_finish_grant(grant, "failed")
            return True
        # A real post is one failure even if two wrappers/events observe it.
        # Reserve this process-local report slot before any terminal-state await;
        # durable native post dedup and the trial's failed-key hash prevent replay.
        post_key = (id(runtime.repository), str(route.post_id))
        reports = getattr(self, "_retry_post_reports", None)
        if reports is None:
            reports = self._retry_post_reports = {}
        prior = reports.get(post_key)
        if prior is not None:
            if kind not in {"connection", "timeout"}:
                prior["hard"] = True
                self._local_blocked.add("heybox_comment")
            return False
        post_report = {"hard": kind not in {"connection", "timeout"}}
        reports[post_key] = post_report
        identity = (id(runtime), event_id)
        module = sys.modules.get(runtime.__class__.__module__)
        block = getattr(module, "block_event", None)
        if callable(block):
            await block(runtime.repository, event_id, "new_content_processing_failed")
        terminal = await self._retry_terminal(runtime, event_id, route)
        if identity in self._retry_reported:
            return False
        self._retry_reported.add(identity)
        retry = ({"scope": "heybox", "failed_key": str(route.post_id), "failure_kind": kind}
                 if self._retry_available and terminal and not post_report["hard"]
                 and kind in {"connection", "timeout"} else None)
        result = await self._report(feature, category, stage, retry=retry)
        incident_id = result.get("incident_id") if isinstance(result, dict) else None
        if post_report["hard"]:
            return False
        if retry is not None and type(incident_id) is str:
            ready = self.host.incidents.retry_ready(incident_id, str(route.post_id), verified_terminal=True) is True
            if ready:
                self._signal_retry_wake(runtime, route.profile_id, incident_id)
            return ready
        return False

    def _signal_retry_wake(self, runtime, profile_id, incident_id):
        """Signal only one live native loop after notice/terminal confirmation."""
        try:
            if (not self._active or not self._retry_available or self._missing
                    or _ACTIVE_INCIDENT_HOOKS is not self or self.host._incident_hooks is not self
                    or incident_id in self._retry_wake_seen
                    or _current_heybox(self)[1] is not runtime):
                return False
            ready = self.host.incidents.retry_intake("heybox")
            if not isinstance(ready, dict) or ready.get("incident_id") != incident_id:
                return False
            adapters = [adapter for adapter in runtime._adapters
                        if str(adapter.config.get("profile_id", "default")) == str(profile_id)]
            if len(adapters) != 1:
                return False
            adapter = adapters[0]
            task = adapter._feed_task
            if (not isinstance(task, asyncio.Task) or task.done() or task.cancelling()
                    or adapter._stop.is_set() or adapter._refresh.is_set()
                    or not isinstance(adapter._retry_wake, asyncio.Event)):
                return False
            self._retry_wakes[id(adapter)] = (adapter, task, runtime, incident_id)
            self._retry_wake_seen.add(incident_id)
            adapter._retry_wake.set()
            return True
        except Exception:
            return False

    def _consume_retry_wake(self, adapter, feed):
        """Allow one scheduling bypass only on the exact current feed task."""
        grant = self._retry_wakes.get(id(adapter))
        try:
            if (not isinstance(grant, tuple) or len(grant) != 4 or grant[0] is not adapter
                    or grant[1] is not asyncio.current_task()):
                return False
            self._retry_wakes.pop(id(adapter), None)
            if (adapter._feed_task is not grant[1]
                    or grant[1].done() or grant[1].cancelling()
                    or adapter._stop.is_set() or adapter._refresh.is_set()
                    or not self.retry_local_preflight("heybox")
                    or _current_heybox(self)[1] is not grant[2]
                    or adapter not in grant[2]._adapters
                    or feed.repository is not grant[2].repository
                    or not _feed_chain_is_current(self, feed)):
                return False
            ready = self.host.incidents.retry_intake("heybox")
            return isinstance(ready, dict) and ready.get("incident_id") == grant[3]
        except Exception:
            return False

    @asynccontextmanager
    async def _fresh_processing(self, adapter, notification, event_id):
        grant, bound = None, None
        try:
            _, runtime, _ = _current_heybox(self)
            module = sys.modules.get(runtime.__class__.__module__.rsplit(".", 1)[0] + ".feiyu_unseen_feed")
            route = notification.route
            base = (adapter in runtime._adapters and type(event_id) is int
                    and notification.event_type.value == "proactive_feed")
            if not base:
                yield False
                return
            valid = await module.valid(runtime.repository, event_id, route)
            if not valid:
                # Preserve separately authorized non-trial recovery paths. They
                # cannot acquire a new-content capability or reopen a pause.
                recovery = sys.modules.get(runtime.__class__.__module__.rsplit(".", 1)[0] + ".feiyu_recovery")
                allow = (not self._blocked("heybox_feed", "heybox_comment", "heybox_vision")
                         and recovery.identity_allowed(runtime.repository, event_id, notification=notification) is True)
                yield allow
                return
            grant = {"runtime": runtime, "adapter": adapter, "notification": notification,
                     "event_id": event_id, "post_id": str(route.post_id),
                     "event_key": str(route.profile_id) + ":" + str(event_id), "trial": None,
                     "active": True, "finished": False}
            intake = _HEYBOX_RETRY_INTAKE.get()
            if isinstance(intake, dict) and intake.get("owner") is self:
                config = runtime.config.snapshot()["proactive_feed"]
                if (not self._retry_intake_allowed() or config.get("dry_run", True)
                        or config.get("review_required", True)):
                    yield False
                    return
                policy = self.host.incidents
                token = policy.retry_reserve(intake["incident_id"], grant["post_id"], notification.observed_at)
                if not token:
                    yield False
                    return
                if not policy.retry_start(intake["incident_id"], token, grant["post_id"], grant["event_key"]):
                    policy.retry_release(intake["incident_id"], token, grant["post_id"], definitely_not_started=True)
                    yield False
                    return
                intake["claimed"] = True
                grant["trial"] = {"incident_id": intake["incident_id"], "token": token}
            self._retry_claims[(id(runtime), event_id)] = grant
            bound = _HEYBOX_RETRY_EVENT.set(grant)
            with self._retry_scope(grant):
                try:
                    yield True
                except Exception as error:
                    if grant.get("trial") is not None:
                        await self._retry_finish_grant(grant, "failed")
                        return
                    kind = _retry_exception_kind(error)
                    security = sys.modules.get(runtime.__class__.__module__.rsplit(".", 1)[0] + ".security")
                    cls = getattr(security, "ImageHostResolutionError", None)
                    if isinstance(cls, type) and isinstance(error, cls):
                        handled = await self._retry_report_failure(runtime, event_id, route,
                            "heybox_vision", "vision_failed", "notice_event", kind)
                        if handled:
                            return
                    raise
        finally:
            if bound is not None:
                _HEYBOX_RETRY_EVENT.reset(bound)
            if grant is not None:
                grant["active"] = False
                if grant.get("trial") is not None and not grant.get("finished"):
                    await self._retry_finish_grant(grant, "unknown")
                self._retry_claims.pop((id(grant["runtime"]), event_id), None)

    def _bind(self, key, obj, name, factory, *, asynchronous=True):
        current = getattr(obj, name, None)
        binding = self._bindings.get(key)
        if binding and binding["object"] is obj and current is binding["wrapped"]:
            return True
        if binding:
            self._restore_binding(binding)
            self._bindings.pop(key, None)
        if not callable(current) or (asynchronous and not inspect.iscoroutinefunction(current)):
            return False
        previous = getattr(obj, "__dict__", {}).get(name, _ABSENT)
        wrapped = factory(current)
        setattr(obj, name, wrapped)
        self._bindings[key] = {"object": obj, "name": name, "previous": previous, "wrapped": wrapped}
        return True

    @staticmethod
    def _restore_binding(binding):
        obj, name, wrapped = binding["object"], binding["name"], binding["wrapped"]
        if getattr(obj, name, None) is not wrapped:
            return
        if binding["previous"] is _ABSENT:
            delattr(obj, name)
        else:
            setattr(obj, name, binding["previous"])

    def _feed(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            feed = args[0] if args else kwargs.get("feed")
            scope_token = _AUTOMATIC_FEED_SCOPE.set((self, feed, wrapped))
            intake_token = None
            try:
                if self._blocked("heybox_feed", "heybox_comment", "heybox_vision"):
                    retry = self.host.incidents.retry_intake("heybox") if self._retry_available else None
                    if not isinstance(retry, dict):
                        return 0
                    intake_token = _HEYBOX_RETRY_INTAKE.set({**retry, "owner": self,
                        "task": asyncio.current_task(), "claimed": False})
                    if not self._retry_intake_allowed():
                        return 0
                try:
                    return await original(*args, **kwargs)
                except Exception as error:
                    security = sys.modules.get(
                        "data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.security"
                    )
                    resolution_error = getattr(security, "ImageHostResolutionError", None)
                    if isinstance(resolution_error, type) and isinstance(error, resolution_error):
                        await self._report("heybox_vision", "vision_failed", "feed_once")
                    else:
                        await self._report("heybox_feed", _category(error), "feed_once")
                    raise
            finally:
                if intake_token is not None:
                    _HEYBOX_RETRY_INTAKE.reset(intake_token)
                _AUTOMATIC_FEED_SCOPE.reset(scope_token)
        return wrapped

    def _notice_poll(self, original):
        service = getattr(original, "__self__", None)
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            scope_token = _NOTICE_SERVICE_SCOPE.set((self, service, "poll", wrapped))
            trace_token = None
            try:
                if not notice_service_ready(service, "poll"):
                    raise _request_not_authorized()
                trace = {"phase": "poll_once", "event_type": "unknown"}
                trace_token = _NOTICE_POLL_TRACE.set(trace)
                try:
                    return await original(*args, **kwargs)
                except Exception as error:
                    category = _category(error)
                    if category is not None:
                        record_failure(getattr(self.host, "root", None), "heybox_comment", "notice_poll", error=error)
                    await self._report("heybox_comment", category, "notice_poll")
                    if category is not None:
                        details = {"phase": trace["phase"], "event_type": trace["event_type"],
                                   "incident_category": category}
                        api_category = _heybox_api_category(error)
                        if api_category is not None:
                            details["api_category"] = api_category
                            status = getattr(error, "status_code", None)
                            if type(status) is int and 100 <= status <= 599:
                                details["http_status"] = status
                        try:
                            await service.repository.add_runtime_error(
                                "polling_diagnostic", "notice_poll_failure",
                                profile_id=service.profile_id, details=details)
                        except Exception:
                            pass
                    raise
                finally:
                    if trace_token is not None:
                        _NOTICE_POLL_TRACE.reset(trace_token)
            finally:
                _NOTICE_SERVICE_SCOPE.reset(scope_token)
        return wrapped

    def _notice_event_poll(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            trace = _NOTICE_POLL_TRACE.get()
            event_type = args[0] if args else kwargs.get("event_type")
            value = getattr(event_type, "value", None)
            if isinstance(trace, dict):
                trace["phase"] = "event_type"
                trace["event_type"] = value if type(value) is str and value in {"mention", "reply"} else "unknown"
            result = await original(*args, **kwargs)
            if isinstance(trace, dict):
                trace["phase"] = "poll_once"
                trace["event_type"] = "unknown"
            return result
        return wrapped

    def _notice_handle(self, original):
        service = getattr(original, "__self__", None)
        @functools.wraps(original, updated=())
        async def wrapped(notification, *, event_id=None):
            kind = getattr(getattr(notification, "event_type", None), "value", "")
            features = ("heybox_comment", "heybox_vision", "heybox_feed") if kind == "proactive_feed" else (
                "heybox_comment", "heybox_vision")
            denial = self._notice_denial(notification, features)
            if denial:
                # The worker has dequeued this item. Record a *new* event in
                # the adapter's existing ledger before marking it ignored, so
                # resumption does not automatically replay the old task.
                # Existing event IDs and duplicate rows are left untouched.
                try:
                    tracked = self._notice_claims.pop(id(notification), None)
                    if (event_id is not None and tracked is not None
                            and tracked[0] is notification and tracked[1] == event_id):
                        # This wrapper witnessed the current process enqueue
                        # this exact object and freshly claimed ID.  The SQL
                        # additionally refuses any dispatched, reserved, or
                        # previously attempted event, atomically.
                        stamp = time.time()
                        await service.repository.db.execute(
                            "UPDATE incoming_events SET status='ignored',error=?,should_filter=1,"
                            "completed_at=COALESCE(completed_at,?),updated_at=? "
                            "WHERE id=? AND status='claimed' "
                            "AND NOT EXISTS (SELECT 1 FROM outgoing_replies o WHERE o.incoming_event_id=?) "
                            "AND NOT EXISTS (SELECT 1 FROM feiyu_auto_reply_reservations r WHERE r.event_id=?)",
                            (denial, stamp, stamp, event_id, event_id, event_id))
                    elif event_id is None:
                        module = sys.modules.get(service.__class__.__module__)
                        state = getattr(getattr(module, "EventState", None), "IGNORED", None)
                        if state is None:
                            await self._report("heybox_comment", "guard_failed", "notice_event")
                            return None
                        event_id = await service.repository.claim_event(notification)
                        if event_id is not None:
                            await service.repository.mark_event(event_id, state,
                                error=denial, should_filter=True)
                except Exception:
                    await self._report("heybox_comment", "storage_failed", "notice_event")
                    raise
                return None
            self._notice_claims.pop(id(notification), None)
            try:
                return await original(notification, event_id=event_id)
            except Exception as error:
                security = sys.modules.get(
                    "data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.security"
                )
                resolution_error = getattr(security, "ImageHostResolutionError", None)
                if isinstance(resolution_error, type) and isinstance(error, resolution_error):
                    await self._report("heybox_vision", "vision_failed", "notice_event")
                else:
                    await self._report("heybox_comment", _category(error), "notice_event")
                raise
        inner = wrapped
        @functools.wraps(inner, updated=())
        async def scoped(notification, *, event_id=None):
            scope_token = _NOTICE_SERVICE_SCOPE.set((self, service, "handle", scoped))
            try:
                if not notice_service_ready(service, "handle"):
                    return None
                return await inner(notification, event_id=event_id)
            finally:
                _NOTICE_SERVICE_SCOPE.reset(scope_token)
        return scoped

    def _notice_enqueue(self, original):
        service = getattr(original, "__self__", None)
        @functools.wraps(original, updated=())
        async def wrapped(notification, *, recovered=False):
            kind = getattr(getattr(notification, "event_type", None), "value", "")
            features = ("heybox_comment", "heybox_vision", "heybox_feed") if kind == "proactive_feed" else (
                "heybox_comment", "heybox_vision")
            denial = self._notice_denial(notification, features) if not recovered else ""
            if denial:
                try:
                    module = sys.modules.get(service.__class__.__module__)
                    state = getattr(getattr(module, "EventState", None), "IGNORED", None)
                    if state is None:
                        raise RuntimeError("incident_event_state_unavailable")
                    event_id = await service.repository.claim_event(notification)
                    if event_id is not None:
                        await service.repository.mark_event(event_id, state,
                            error=denial, should_filter=True)
                    return False
                except Exception as error:
                    await self._report("heybox_comment", _category(error), "notice_event")
                    raise
            result = await original(notification, recovered=recovered)
            if result is True and not recovered:
                event_id = getattr(service, "_recovery_ids", {}).get(notification.external_event_id)
                if type(event_id) is int and event_id > 0:
                    self._notice_claims[id(notification)] = (notification, event_id)
            return result
        inner = wrapped
        @functools.wraps(inner, updated=())
        async def scoped(notification, *, recovered=False):
            scope_token = _NOTICE_SERVICE_SCOPE.set((self, service, "enqueue", scoped))
            try:
                if not notice_service_ready(service, "enqueue"):
                    return False
                return await inner(notification, recovered=recovered)
            finally:
                _NOTICE_SERVICE_SCOPE.reset(scope_token)
        return scoped

    @staticmethod
    def _vision_event_bound(plugin, event):
        """Accept only the adapter's already-bound automatic event."""
        try:
            event_id = getattr(event, "event_id", None)
            binding = plugin._automatic_binding(event)
            return (type(event_id) is int and event_id > 0
                    and event.runtime is plugin.runtime
                    and event.get_platform_name() == "xiaoheihe"
                    and event.get_extra("xiaoheihe_auto_guard_binding", None) == binding)
        except Exception:
            return False

    @staticmethod
    def _vision_event_finished(event):
        if getattr(event, "_final_send_done", False) is True:
            return True
        completed = getattr(event, "_completion", None)
        return bool(completed is not None and completed.is_set())

    async def _finish_vision_incident(self, event, failure_kind=None):
        """Atomically prevent a late send before recording this failure."""
        if self._vision_event_finished(event):
            return False

        async def record():
            # Close the group synchronously before the terminal-state await.
            self._local_blocked.add("heybox_vision")
            try:
                await event.runtime.fail_reply(event_id=event.event_id, route=event.route,
                                               error="incident_vision_failed", generated_ms=0)
                await self._retry_report_failure(event.runtime, event.event_id, event.route,
                    "heybox_vision", "vision_failed", "notice_event", failure_kind)
            finally:
                # Persistent F060 or the trial store now owns the closed gate.
                if self.host.incidents.blocked("heybox_vision") is True:
                    self._local_blocked.discard("heybox_vision")

        lock = getattr(event, "_send_lock", None)
        completed = getattr(event, "_completion", None)
        if lock is None or completed is None:
            # The live adapter event has both. If that contract changes,
            # still close by its existing gate before recording failure.
            if self._vision_event_finished(event):
                return False
            event.stop_event()
            await event.suppress_due_to_gate()
            await record()
            return True
        async with lock:
            if self._vision_event_finished(event):
                return False
            stopped = getattr(event, "is_stopped", None)
            if callable(stopped) and stopped():
                return False
            event.stop_event()
            event._final_send_done = True
            try:
                await record()
            finally:
                completed.set()
        return True

    async def _deny_vision_event(self, plugin, event):
        if not self._vision_event_finished(event):
            # The adapter revokes the reservation and marks the event ignored;
            # merely signalling completion would leave it dispatched.
            await plugin._deny_automatic_event(event, "vision_not_completed")

    def _vision_provider_failure(self, original):
        @functools.wraps(original, updated=())
        def wrapped(profile_id, purpose, provider_id, error, *, details=None):
            group = _HEYBOX_VISION_GROUP.get()
            if (group is not None and group["task"] is asyncio.current_task()
                    and purpose == "image" and _category(error) is not None):
                group["failed"] = True
                kind = _retry_exception_kind(error)
                if kind is None:
                    group["hard_failure"] = True
                elif group.get("failure_kind") is None:
                    group["failure_kind"] = kind
                record_failure(getattr(self.host, "root", None), "heybox_vision",
                               "vision_provider", error=error)
            return original(profile_id, purpose, provider_id, error, details=details)
        return wrapped

    def _vision_group(self, original):
        plugin = getattr(original, "__self__", None)
        @functools.wraps(original, updated=())
        async def wrapped(event, *args, **kwargs):
            settings = kwargs.get("context_settings")
            eligible = (self._vision_event_bound(plugin, event)
                        and not self._vision_event_finished(event)
                        and bool(kwargs.get("urls")) and isinstance(settings, dict)
                        and bool(settings.get("enable_image_understanding", True)))
            if not eligible:
                return await original(event, *args, **kwargs)
            group = {"task": asyncio.current_task(), "failed": False,
                     "hard_failure": False, "failure_kind": None}
            token = _HEYBOX_VISION_GROUP.set(group)
            try:
                try:
                    result = await original(event, *args, **kwargs)
                except Exception as error:
                    if _category(error) is None:
                        await self._deny_vision_event(plugin, event)
                    else:
                        await self._finish_vision_incident(event, _retry_exception_kind(error))
                    raise
            finally:
                _HEYBOX_VISION_GROUP.reset(token)
            if result is None and group["failed"]:
                await self._finish_vision_incident(event, None if group["hard_failure"] else group["failure_kind"])
                # Stop the current event before another image group can run.
                raise IncidentFeaturePaused("incident_vision_failed")
            return result
        return wrapped

    def _vision_caption(self, original):
        plugin = getattr(original, "__self__", None)
        @functools.wraps(original, updated=())
        async def wrapped(event, request, *, provider_settings, profile_id, context_settings):
            selected = bool(getattr(request, "image_urls", None))
            enabled = isinstance(context_settings, dict) and bool(context_settings.get("enable_image_understanding", True))
            proactive = getattr(event, "proactive", False) is True
            eligible = (proactive and selected and enabled and self._vision_event_bound(plugin, event)
                        and not self._vision_event_finished(event))
            try:
                result = await original(event, request, provider_settings=provider_settings,
                                        profile_id=profile_id, context_settings=context_settings)
            except Exception as error:
                if eligible:
                    if _category(error) is None:
                        await self._deny_vision_event(plugin, event)
                    else:
                        await self._finish_vision_incident(event, _retry_exception_kind(error))
                raise
            if eligible and result is False:
                # No explicit failed provider was observed. Preserve a safe
                # ignored result without manufacturing an incident.
                await self._deny_vision_event(plugin, event)
            return result
        return wrapped

    def _model_failure(self, original):
        try:
            runtime = self.host.plugin("astrbot_plugin_xiaoheihe_adapter").runtime
            if runtime.fail_reply != original:
                runtime = None
        except Exception:
            runtime = None
        @functools.wraps(original, updated=())
        async def wrapped(*, event_id, route, error, generated_ms):
            expected = event_id is not None and isinstance(error, str) and (
                error.startswith("heybox_main_model_failed:") or error == "AstrBot 原生管线返回空回复")
            if expected:
                self._local_blocked.add("heybox_comment")
            result = await original(event_id=event_id, route=route, error=error, generated_ms=generated_ms)
            # These are native fixed error codes; cancellation and arbitrary
            # exception messages cannot be mistaken for model failures.
            if event_id is not None and isinstance(error, str) and (
                    error.startswith("heybox_main_model_failed:")
                    or error == "AstrBot 原生管线返回空回复"):
                category = {
                    "heybox_main_model_failed:APIConnectionError": "model_connection_failed",
                    "heybox_main_model_failed:ConnectError": "model_connection_failed",
                    "heybox_main_model_failed:APITimeoutError": "model_timeout",
                    "heybox_main_model_failed:TimeoutException": "model_timeout",
                }.get(error, "model_failed")
                kind = _retry_exception_kind(_HEYBOX_MODEL_EXCEPTION.get())
                await self._retry_report_failure(runtime, event_id, route,
                    "heybox_comment", category, "notice_event", kind)
                if self.host.incidents.blocked("heybox_comment") is True:
                    self._local_blocked.discard("heybox_comment")
            return result
        return wrapped

    def _main_exception(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(checked, error):
            token = _HEYBOX_MODEL_EXCEPTION.set(error)
            try:
                return await original(checked, error)
            finally:
                _HEYBOX_MODEL_EXCEPTION.reset(token)
        return wrapped

    def _retry_register(self, original):
        @functools.wraps(original, updated=())
        @contextmanager
        def wrapped(adapter, event):
            runtime = getattr(event, "runtime", None)
            grant = self._retry_claims.get((id(runtime), getattr(event, "event_id", None)))
            valid = (grant is not None and grant.get("active") and grant["adapter"] is adapter
                     and getattr(event, "proactive", None) is True
                     and getattr(event, "route", None) == grant["notification"].route
                     and id(event) not in self._retry_events)
            if valid:
                grant["event"] = event
                self._retry_events[id(event)] = grant
            try:
                with original(adapter, event) as allowed:
                    yield allowed
            finally:
                if valid and self._retry_events.get(id(event)) is grant:
                    self._retry_events.pop(id(event), None)
        return wrapped

    def _retry_pipeline(self, original):
        @functools.wraps(original, updated=())
        @asynccontextmanager
        async def wrapped(event):
            grant = self._retry_events.get(id(event))
            if grant is None or grant.get("trial") is None:
                async with original(event) as allowed:
                    yield allowed
                return
            if (not self.retry_local_preflight("heybox")
                    or not grant.get("active") or grant.get("event") is not event
                    or grant.get("pipeline_task") is not None):
                yield False
                return
            grant["pipeline_task"] = asyncio.current_task()
            bound = _HEYBOX_RETRY_EVENT.set(grant)
            try:
                with self._retry_scope(grant):
                    async with original(event) as allowed:
                        yield allowed
            finally:
                _HEYBOX_RETRY_EVENT.reset(bound)
        return wrapped

    def _reply_expire(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*, event_id, route, generated_ms, reply_timeout_seconds):
            result = await original(event_id=event_id, route=route, generated_ms=generated_ms,
                                    reply_timeout_seconds=reply_timeout_seconds)
            if event_id is not None:
                await self._report("heybox_comment", "request_failed", "notice_event")
            return result
        return wrapped

    def _heybox_rejection_current(self, runtime, wrapper, policy):
        """Check ownership without opening a gate or importing a plugin."""
        try:
            module = sys.modules.get(_HEYBOX_API_MODULE.rsplit(".", 1)[0] + ".runtime")
            binding = self._bindings.get("heybox_delivery")
            return bool(_ACTIVE_INCIDENT_HOOKS is self and self._active is True
                        and getattr(sys.modules.get(__name__), "_ACTIVE_INCIDENT_HOOKS", None) is self
                        and getattr(module, "RuntimeServices", None) is type(runtime)
                        and isinstance(binding, dict) and binding.get("object") is runtime
                        and binding.get("name") == "deliver" and binding.get("wrapped") is wrapper
                        and binding.get("previous") is getattr(wrapper, "__wrapped__", None)
                        and self.host.incidents is policy
                        and self.host.plugin("astrbot_plugin_xiaoheihe_adapter").runtime is runtime
                        and _binding_is_current(self, runtime, "deliver", wrapper))
        except Exception:
            return False

    def _heybox_rejection_ready(self, policy, features, snapshot):
        """Allow notices for two already-failed items, never new platform work."""
        try:
            if (any(feature in self._local_blocked for feature in features)
                    or self._resume_snapshot(features) != snapshot):
                return False
            state = policy.status()
            if (not isinstance(state, dict) or state.get("ok") is not True
                    or state.get("store_error") or any(feature in state.get("active", {}) for feature in features)
                    or any(feature in state.get("closing_features", ()) for feature in features)):
                return False
            gate = state.get("heybox_rejection_gate")
            if not isinstance(gate, dict) or gate.get("ok") is not True:
                return False
            return all(policy.blocked(feature) is False for feature in features if feature != "heybox_comment")
        except Exception:
            return False

    async def _heybox_rejection_pause(self, runtime, wrapper, policy, category, notice_id="", *, notify=False):
        """Hand off only to a durable F060 pause; uncertain notices never repeat."""
        already_local = "heybox_comment" in self._local_blocked
        self._local_blocked.add("heybox_comment")
        if not self._heybox_rejection_current(runtime, wrapper, policy):
            return False
        try:
            result = await policy.report("heybox_comment", category, stage="reply_send",
                                         event_id=notice_id, notify=notify)
            if not self._heybox_rejection_current(runtime, wrapper, policy):
                return False
            if not (isinstance(result, dict) and result.get("ok") is True
                    and result.get("blocked") is True):
                return False
            if notice_id:
                ack = policy.acknowledge_heybox_rejection_pause(notice_id, result.get("incident_id"))
                if not (isinstance(ack, dict) and ack.get("ok") is True):
                    return False
            if not already_local:
                self._local_blocked.discard("heybox_comment")
            return True
        except Exception:
            return False

    async def _heybox_rejection_item(self, runtime, wrapper, policy, kwargs, features, snapshot, started_at):
        """Isolate one proven fresh rejection, preserving all original rows."""
        stopped = {"status": "ignored", "reason": "incident_feature_paused", "external_comment_id": ""}
        if not self._heybox_rejection_current(runtime, wrapper, policy):
            return stopped
        if not self._heybox_rejection_ready(policy, features, snapshot):
            await self._heybox_rejection_pause(runtime, wrapper, policy, "guard_failed", notify=True)
            return stopped
        event_id, route = kwargs.get("event_id"), kwargs.get("route")
        models = sys.modules.get(_HEYBOX_API_MODULE.rsplit(".", 1)[0] + ".models")
        route_type = getattr(models, "RoutingTarget", None)
        enabled = getattr(self, "_heybox_rejection_enabled_at", None)
        route_event = getattr(route, "incoming_event_id", None)
        if (type(event_id) is not int or event_id <= 0
                or not isinstance(route_type, type) or type(route) is not route_type
                or type(enabled) not in (int, float) or not math.isfinite(enabled)
                or enabled <= 0 or enabled > started_at
                or not (route_event is None or type(route_event) is int and route_event == event_id)
                or kwargs.get("dry_run") is not False):
            await self._heybox_rejection_pause(runtime, wrapper, policy, "guard_failed", notify=True)
            return stopped
        try:
            database = runtime.repository.db
            # Hold the adapter's existing write lock only during local reads.
            # The single snapshot ties the event, one attempt and reservation.
            async with database._write_lock:
                connection = database.connection
                await connection.execute("BEGIN")
                try:
                    async with connection.execute(
                        "SELECT e.id AS event_id,e.profile_id,e.post_id,e.root_comment_id,e.parent_comment_id,"
                        "e.notification_id,e.event_type,e.status AS event_status,e.retry_count,e.next_retry_at,e.claimed_at,"
                        "e.discovered_at,e.raw_json,o.id AS outgoing_id,o.profile_id AS outgoing_profile,"
                        "o.post_id AS outgoing_post,o.root_comment_id AS outgoing_root,"
                        "o.parent_comment_id AS outgoing_parent,o.status AS outgoing_status,"
                        "o.external_comment_id,o.confirmed_at,o.attempted_at,r.event_type AS reserved_type,"
                        "r.profile_id AS reserved_profile,r.dispatched_at,r.send_started_at,r.send_blocked,r.revoked "
                        "FROM incoming_events e LEFT JOIN outgoing_replies o ON o.incoming_event_id=e.id "
                        "LEFT JOIN feiyu_auto_reply_reservations r ON r.event_id=e.id WHERE e.id=?", (event_id,)
                    ) as cursor:
                        rows = await cursor.fetchall()
                    recovery = False
                    for table in ("feiyu_recovery_attempts", "feiyu_recovery_permission_revision1_attempts",
                                  "feiyu_recovery_owner_renewal1_attempts"):
                        async with connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ) as cursor:
                            exists = await cursor.fetchone()
                        if exists:
                            async with connection.execute(
                                "SELECT 1 FROM " + table + " WHERE new_event_id=?", (event_id,)
                            ) as cursor:
                                recovery = recovery or await cursor.fetchone() is not None
                finally:
                    await connection.rollback()
            row = rows[0] if len(rows) == 1 else None
            now = time.time()
            raw = json.loads(row["raw_json"]) if row is not None and row["raw_json"] else {}
            stamps = (row["claimed_at"], row["discovered_at"], row["attempted_at"],
                      row["dispatched_at"], row["send_started_at"]) if row is not None else ()
            fresh = (len(stamps) == 5 and all(type(stamp) in (int, float) and math.isfinite(stamp)
                                            and enabled < stamp <= now for stamp in stamps)
                     and row["attempted_at"] >= started_at and row["claimed_at"] > snapshot[0])
            fresh = bool(fresh and row["discovered_at"] <= row["claimed_at"] <= started_at)
            bound = (row is not None and row["profile_id"] == route.profile_id == row["outgoing_profile"]
                     and row["post_id"] == route.post_id == row["outgoing_post"]
                     and row["root_comment_id"] == route.root_comment_id == row["outgoing_root"]
                     and row["parent_comment_id"] == route.parent_comment_id == row["outgoing_parent"]
                     and row["notification_id"] == route.notification_id
                     and row["reserved_profile"] == route.profile_id
                     and row["reserved_type"] == row["event_type"]
                     and row["event_type"] in {"mention", "reply", "proactive_feed"})
            valid = (fresh and bound and not recovery and isinstance(raw, dict) and "_feiyu_recovery" not in raw
                     and row["event_status"] == "dead_letter" and type(row["retry_count"]) is int
                     and row["retry_count"] == 0 and row["next_retry_at"] is None
                     and type(row["outgoing_id"]) is int and row["outgoing_id"] > 0
                     and row["outgoing_status"] == "failed" and row["external_comment_id"] == ""
                     and row["confirmed_at"] is None and row["send_blocked"] == 0 and row["revoked"] == 0)
        except asyncio.CancelledError:
            await self._heybox_rejection_pause(runtime, wrapper, policy, "delivery_unknown")
            raise
        except Exception:
            await self._heybox_rejection_pause(runtime, wrapper, policy, "storage_failed", notify=True)
            return stopped
        if not valid:
            await self._heybox_rejection_pause(runtime, wrapper, policy, "storage_failed", notify=True)
            return stopped
        if not self._heybox_rejection_current(runtime, wrapper, policy):
            return stopped
        if not self._heybox_rejection_ready(policy, features, snapshot):
            await self._heybox_rejection_pause(runtime, wrapper, policy, "guard_failed", notify=True)
            return stopped
        outgoing_id = row["outgoing_id"]
        try:
            result = await policy.report_heybox_rejection(event_id, outgoing_id, stage="reply_send")
        except asyncio.CancelledError:
            notice_id = ""
            try:
                current = policy.heybox_rejection_record(event_id, outgoing_id)
                notice_id = current.get("notice_id", "") if isinstance(current, dict) else ""
            except Exception:
                pass
            await self._heybox_rejection_pause(runtime, wrapper, policy, "delivery_unknown", notice_id)
            raise
        except Exception:
            notice_id = ""
            try:
                current = policy.heybox_rejection_record(event_id, outgoing_id)
                notice_id = current.get("notice_id", "") if isinstance(current, dict) else ""
            except Exception:
                pass
            await self._heybox_rejection_pause(runtime, wrapper, policy, "storage_failed", notice_id)
            return stopped
        if not self._heybox_rejection_current(runtime, wrapper, policy):
            self._local_blocked.add("heybox_comment")
            return stopped
        if (isinstance(result, dict) and result.get("ok") is True and result.get("isolated") is True
                and result.get("notification") == "sent" and self._resume_snapshot(features) == snapshot):
            return {"status": "ignored", "reason": "platform_content_rejected", "external_comment_id": ""}
        notice_id = result.get("notice_id", "") if isinstance(result, dict) else ""
        reason = result.get("reason", "") if isinstance(result, dict) else ""
        notification = result.get("notification", "") if isinstance(result, dict) else ""
        category = ("storage_failed" if reason in {"store_unavailable", "rejection_store_unavailable"}
                    else "guard_failed" if notification == "not_attempted" else "delivery_unknown")
        await self._heybox_rejection_pause(runtime, wrapper, policy, category, notice_id,
                                           notify=notification == "not_attempted" and category == "guard_failed")
        return stopped

    def _comment_delivery(self, original):
        # Existing availability/social guards are ordinary closures. Keep
        # calling that exact installed chain; bound-method metadata is absent.
        try:
            runtime = self.host.plugin("astrbot_plugin_xiaoheihe_adapter").runtime
            if getattr(runtime, "deliver", None) is not original:
                runtime = None
        except Exception:
            runtime = None
        @functools.wraps(original, updated=())
        async def wrapped(**kwargs):
            event_id = kwargs.get("event_id")
            if event_id is None:
                # The independent manual intent is registered before delivery.
                # Check the reverse race too: a new native auto task may have
                # reserved this post while the manual tool was awaiting data.
                try:
                    active_runtime = self.host.plugin("astrbot_plugin_xiaoheihe_adapter").runtime
                    module_name = active_runtime.__class__.__module__.rsplit(".", 1)[0] + ".feiyu_unseen_feed"
                    fresh = sys.modules.get(module_name)
                    check = getattr(fresh, "manual_post_blocked", None)
                    route = kwargs.get("route")
                    if route is None or not callable(check) or await check(active_runtime.repository, route.post_id, profile_id=route.profile_id):
                        return {"status": "ignored", "reason": "fresh_feed_post_preserved", "external_comment_id": ""}
                except Exception:
                    return {"status": "ignored", "reason": "fresh_feed_guard_unavailable", "external_comment_id": ""}
                return await original(**kwargs)
            started_at = time.time()
            policy = self.host.incidents
            features = ("heybox_comment", "heybox_vision")
            if kwargs.get("proactive") is True:
                features += ("heybox_feed",)
            if self._blocked(*features):
                plugin = self.host.plugin("astrbot_plugin_xiaoheihe_adapter")
                active_runtime = plugin.runtime
                module = sys.modules.get(active_runtime.__class__.__module__)
                block = getattr(module, "block_event", None)
                if callable(block):
                    await block(active_runtime.repository, event_id, "incident_feature_paused")
                return {"status": "ignored", "reason": "incident_feature_paused", "external_comment_id": ""}
            snapshot = self._resume_snapshot(features)
            if snapshot is None:
                return {"status": "ignored", "reason": "incident_barrier_unavailable", "external_comment_id": ""}
            if snapshot[0]:
                try:
                    active_runtime = self.host.plugin("astrbot_plugin_xiaoheihe_adapter").runtime
                    repository = active_runtime.repository
                    row = await repository.db.fetchone(
                        "SELECT claimed_at,status,event_type,raw_json,EXISTS(SELECT 1 FROM outgoing_replies o "
                        "WHERE o.incoming_event_id=e.id) AS has_outgoing FROM incoming_events e WHERE id=?",
                        (event_id,))
                except Exception:
                    await self._report("heybox_comment", "storage_failed", "reply_send")
                    return {"status": "ignored", "reason": "incident_barrier_unavailable", "external_comment_id": ""}
                claim = row["claimed_at"] if row is not None else None
                try:
                    source = _source_time_raw(row["event_type"], json.loads(row["raw_json"])) if row is not None else None
                except (TypeError, ValueError, KeyError):
                    source = None
                valid = (type(claim) in (int, float) and math.isfinite(claim)
                         and snapshot[0] < claim <= time.time()
                         and source is not None and source <= time.time()
                         and math.floor(source) > math.floor(snapshot[0]))
                if not valid:
                    module_name = active_runtime.__class__.__module__.rsplit(".", 1)[0] + ".feiyu_recovery"
                    recovery = sys.modules.get(module_name)
                    source_exception = getattr(recovery, "source_exception", None)
                    if callable(source_exception):
                        valid = await source_exception(repository, event_id, snapshot) is True
                if not valid and row is not None and row["event_type"] == "proactive_feed":
                    module_name = active_runtime.__class__.__module__.rsplit(".", 1)[0] + ".feiyu_unseen_feed"
                    fresh = sys.modules.get(module_name)
                    source_exception = getattr(fresh, "source_exception", None)
                    if callable(source_exception):
                        valid = await source_exception(repository, event_id, snapshot) is True
                if not valid:
                    if row is not None and row["status"] in {"claimed", "context_ready", "dispatched", "generated"} and not row["has_outgoing"]:
                        module = sys.modules.get(active_runtime.__class__.__module__)
                        block = getattr(module, "block_event", None)
                        if callable(block):
                            await block(repository, event_id, "incident_resume_barrier")
                    return {"status": "ignored", "reason": "incident_resume_barrier", "external_comment_id": ""}
            # The database read above yields. A new incident or a resolution
            # during that read invalidates this attempt before native delivery.
            # The same snapshot is checked again at the final HTTP boundary.
            if self._blocked(*features) or self._resume_snapshot(features) != snapshot:
                active_runtime = self.host.plugin("astrbot_plugin_xiaoheihe_adapter").runtime
                module = sys.modules.get(active_runtime.__class__.__module__)
                block = getattr(module, "block_event", None)
                if callable(block):
                    await block(active_runtime.repository, event_id, "incident_generation_changed")
                return {"status": "ignored", "reason": "incident_generation_changed", "external_comment_id": ""}
            token = _HEYBOX_COMMENT.set(features)
            barrier_token = _HEYBOX_COMMENT_BARRIER.set(snapshot)
            try:
                try:
                    result = await original(**kwargs)
                    await self._retry_delivery_result(runtime, event_id, result)
                    return result
                except Exception as error:
                    grant = self._retry_claims.get((id(runtime), event_id))
                    if grant is not None and grant.get("trial") is not None:
                        await self._retry_finish_grant(grant, "unknown" if _heybox_api_category(error) == "send_unknown" else "failed")
                        raise
                    if _heybox_api_category(error) == "platform_content_rejected":
                        # Close the final HTTP gate before the first metadata
                        # await. Each call releases only its own temporary gate;
                        # a failed handoff keeps the separate fail-closed latch.
                        pending = getattr(self, "_heybox_rejection_pending", None)
                        if pending is None:
                            pending = self._heybox_rejection_pending = set()
                        ticket = object()
                        pending.add(ticket)
                        try:
                            return await self._heybox_rejection_item(
                                runtime, wrapped, policy, kwargs, features, snapshot, started_at)
                        finally:
                            pending.discard(ticket)
                    await self._report("heybox_comment", _category(error), "reply_send")
                    raise
            finally:
                _HEYBOX_COMMENT_BARRIER.reset(barrier_token)
                _HEYBOX_COMMENT.reset(token)
        return wrapped

    def _comment_send(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(route, content, *, before_request=None):
            features = _HEYBOX_COMMENT.get()
            if features is None:
                return await original(route, content, before_request=before_request)
            snapshot = _HEYBOX_COMMENT_BARRIER.get()
            async def checked():
                if before_request is not None and await before_request() is not True:
                    return False
                if snapshot is None or self._blocked(*features):
                    return False
                return self._resume_snapshot(features) == snapshot
            return await original(route, content, before_request=checked)
        return wrapped

    def _get_heybox_client(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            client = await original(*args, **kwargs)
            # A coroutine may return from the original get_client after this
            # handle was restored. Do not install a new ownerless wrapper or
            # allow that already-started automatic delivery to reach HTTP.
            if not self._active:
                if _HEYBOX_COMMENT.get() is not None:
                    raise IncidentFeaturePaused("incident_guard_retired")
                return client
            if not self._bind(f"comment_client_{id(client)}", client, "send_comment", self._comment_send):
                # Failure to install the last check must not allow this
                # automatic delivery to run without a gate.
                if _HEYBOX_COMMENT.get() is not None:
                    raise RuntimeError("incident_comment_guard_unavailable")
            return client
        return wrapped

    def _autopost(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            if self._blocked("heybox_autopost"):
                return {"ok": True, "sent": False, "reason": "incident_feature_paused"}
            try:
                result = await original(*args, **kwargs)
            except Exception as error:
                await self._report("heybox_autopost", _category(error), "autopost_once")
                raise
            if isinstance(result, dict) and result.get("ok") is False:
                category = _AUTOPOST_FAILURE.get(result.get("reason"))
                if category is None and result.get("operation_id"):
                    operations = getattr(self.host, "_heybox_posts_ops", {})
                    operation = operations.get(result["operation_id"], {}) if isinstance(operations, dict) else {}
                    phase = operation.get("phase") if isinstance(operation, dict) else None
                    if phase in {"unknown", "published_unverified", "dispatching"}:
                        category = "delivery_unknown"
                    elif phase == "rejected":
                        category = "request_failed"
                if category:
                    await self._report("heybox_autopost", category, "autopost_once")
            return result
        return wrapped

    def _post_authorization(self, original):
        @functools.wraps(original, updated=())
        def wrapped(*args, **kwargs):
            return not self._blocked("heybox_autopost") and original(*args, **kwargs)
        return wrapped

    def _qzone_sync(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            manual = kwargs.get("backfill_id") is not None
            if manual:
                return await original(*args, **kwargs)
            if self._blocked("heybox_qzone_sync", "qzone_publish"):
                return {"ok": True, "reason": "incident_feature_paused"}
            snapshot = self._resume_snapshot(("heybox_qzone_sync", "qzone_publish"))
            if snapshot is None:
                return {"ok": True, "reason": "incident_barrier_unavailable"}
            token = _SYNC_AUTOMATIC.set(True)
            barrier_token = _SYNC_BARRIER.set({"owner": self, "host": self.host,
                                               "snapshot": snapshot, "source_created_at": None})
            try:
                try:
                    result = await original(*args, **kwargs)
                except Exception as error:
                    await self._report("heybox_qzone_sync", _category(error), "qzone_sync_once")
                    raise
                if isinstance(result, dict) and result.get("ok") is False:
                    reason = result.get("reason")
                    category = _SYNC_FAILURE.get(reason) if type(reason) is str else None
                    if reason == "sync_unavailable":
                        error_class = result.get("error_class")
                        if type(error_class) is str:
                            category = {
                                "timeout": "request_failed", "connection": "request_failed",
                                "http_client": "request_failed", "heybox_api": "request_failed",
                                "sqlite": "storage_failed", "os": "storage_failed",
                            }.get(error_class, category)
                    if category:
                        await self._report("heybox_qzone_sync", category, "qzone_sync_once")
                return result
            finally:
                _SYNC_BARRIER.reset(barrier_token)
                _SYNC_AUTOMATIC.reset(token)
        return wrapped

    def _qzone_request(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(qzone, method, url, *args, **kwargs):
            if _SYNC_AUTOMATIC.get() and method == "POST":
                preflight = kwargs.get("_preflight")
                def checked():
                    if preflight is not None:
                        preflight()
                    if self._blocked("heybox_qzone_sync", "qzone_publish"):
                        raise ValueError("incident_feature_paused")
                    scope = _SYNC_BARRIER.get()
                    current = self._resume_snapshot(("heybox_qzone_sync", "qzone_publish"))
                    if (not isinstance(scope, dict) or scope.get("owner") is not self
                            or scope.get("host") is not self.host or current is None
                            or scope.get("snapshot") != current):
                        raise ValueError("incident_resume_barrier")
                    stamp = scope.get("source_created_at")
                    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                            or stamp <= 0 or stamp > time.time()
                            or math.floor(stamp) <= math.floor(current[0])):
                        raise ValueError("incident_resume_barrier")
                kwargs["_preflight"] = checked
            return await original(qzone, method, url, *args, **kwargs)
        return wrapped

    def _qzone_publication(self, original):
        # Existing sync checks this marker on the current service method.
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            if ((_SYNC_AUTOMATIC.get() and self._blocked("heybox_qzone_sync", "qzone_publish"))
                    or (_QZONE_PUBLICATION.get() and self._blocked("qzone_publish"))):
                raise ValueError("incident_feature_paused")
            return await original(*args, **kwargs)
        for name in ("_feiyu_qzone_guard_owner", "_feiyu_qzone_guard_restore"):
            if hasattr(original, name):
                setattr(wrapped, name, getattr(original, name))
        return wrapped

    def _qzone_candidate(self, original):
        signature = inspect.signature(original)
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            values = signature.bind(*args, **kwargs)
            values.apply_defaults()
            automatic = (values.arguments.get("manual") is False
                         and values.arguments.get("channel") == "draft")
            try:
                result = await original(*args, **kwargs)
            except Exception as error:
                if automatic:
                    await self._report("qzone_publish", _category(error), "public_send")
                raise
            if automatic and isinstance(result, dict) and result.get("ok") is False:
                category = _QZONE_CANDIDATE_FAILURE.get(result.get("reason"))
                if category:
                    await self._report("qzone_publish", category, "public_send")
            return result
        return wrapped

    def _journal(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(steward, *args, **kwargs):
            if steward is not self.host:
                return await original(steward, *args, **kwargs)
            intake = None
            if (not self._active or _ACTIVE_INCIDENT_HOOKS is not self
                    or "bili_journal" in self._local_blocked):
                return {"processed": 0, "status": "incident_feature_paused"}
            if self._blocked("bili_journal"):
                intake = self.host.incidents.retry_intake("bili_journal") if self._retry_available else None
                if (not self.retry_local_preflight("bili_journal") or not isinstance(intake, dict)
                        or self.host.incidents.retry_intake_allowed(intake["incident_id"]) is not True):
                    return {"processed": 0, "status": "incident_feature_paused"}
            try:
                result = await original(steward, *args, **kwargs)
            except Exception as error:
                await self._report("bili_journal", _category(error), "bili_journal_once")
                raise
            if isinstance(result, dict):
                retry = result.pop("_retry_failure", None)
                terminal = result.pop("_retry_terminal", False)
                if intake is not None and self.host.incidents.retry_intake("bili_journal") != intake:
                    # The worker owns this trial's one terminal/notice action.
                    # Do not manufacture another incident for its same failure.
                    return result
                status = result.get("status")
                if status in {"failed", "worker_unavailable"}:
                    error_class = result.get("error_class")
                    reason = result.get("failure_reason")
                    category = {
                        "timeout": "request_failed", "connection": "request_failed",
                        "http_client": "request_failed", "heybox_api": "request_failed",
                        "sqlite": "storage_failed", "os": "storage_failed",
                        "private_review_model": "model_failed",
                    }.get(error_class, "unexpected_exception") if type(error_class) is str else "unexpected_exception"
                    if type(reason) is str:
                        category = {
                            "journal_ledger_unavailable": "storage_failed",
                            "source_read_failed": "request_failed",
                            "provider_unavailable": "model_failed",
                            "invalid_model_or_citation": "model_failed",
                        }.get(reason, category)
                    valid_retry = (self._retry_available and terminal is True and category == "model_failed"
                        and type(retry) is dict and set(retry) == {"scope", "failed_key", "failure_kind"}
                        and retry.get("scope") == "bili_journal" and type(retry.get("failed_key")) is str
                        and len(retry["failed_key"]) == 12 and retry["failed_key"].startswith("BV")
                        and retry["failed_key"].isascii() and retry["failed_key"].isalnum()
                        and type(retry.get("failure_kind")) is str
                        and retry.get("failure_kind") in {"connection", "timeout"})
                    report = await self._report("bili_journal", category, "bili_journal_once",
                                                retry=retry if valid_retry else None)
                    if valid_retry and isinstance(report, dict) and type(report.get("incident_id")) is str:
                        if self.host.incidents.retry_ready(report["incident_id"], retry["failed_key"], verified_terminal=True) is True:
                            try:
                                from .bili_immediate_retry import request_immediate
                                request_immediate(self.host, self, report["incident_id"])
                            except Exception:
                                self._local_blocked.add("bili_journal")
                elif status == "journal_unknown":
                    await self._report("bili_journal", "delivery_unknown", "bili_journal_once")
            return result
        return wrapped

    def _bili_share(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(bvid, dry_run=True, *, new_journal=None, public_reason=None):
            if new_journal is None:
                return await original(bvid, dry_run=dry_run, new_journal=new_journal, public_reason=public_reason)
            if self._blocked("bili_share"):
                return {"bvid": bvid, "recommended": False, "dry_run": bool(dry_run),
                        "status": "incident_feature_paused", "ok": False}
            token = _BILI_SHARE.set(True)
            try:
                try:
                    result = await original(bvid, dry_run=dry_run, new_journal=new_journal, public_reason=public_reason)
                except Exception as error:
                    await self._report("bili_share", _category(error), "bili_share_once")
                    raise
                if isinstance(result, dict):
                    category = _SHARE_FAILURE.get(result.get("status"))
                    if category:
                        await self._report("bili_share", category, "bili_share_once")
                return result
            finally:
                _BILI_SHARE.reset(token)
        return wrapped

    def _bili_poll(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            if self._blocked("bili_browse"):
                return False
            try:
                return await original(*args, **kwargs)
            except Exception:
                # Only the existing scheduler/storage boundary runs here;
                # actual browsing has its separate established failure hook.
                await self._report("bili_browse", "storage_failed", "bili_browse_once")
                raise
        return wrapped

    def _bili_poll_finish(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            # Completion must still save cooling when another incident paused
            # the feature. This grants no browsing or automatic recovery.
            try:
                return await original(*args, **kwargs)
            except Exception:
                await self._report("bili_browse", "storage_failed", "bili_browse_once")
                raise
        return wrapped

    def _bili_browse(self, original):
        signature = inspect.signature(original)
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            values = signature.bind(*args, **kwargs)
            values.apply_defaults()
            automatic = values.arguments.get("automatic") is True
            if automatic and self._blocked("bili_browse"):
                return None
            token = _BILI_AUTOMATIC.set(automatic)
            try:
                try:
                    return await original(*args, **kwargs)
                except Exception as error:
                    if automatic:
                        await self._report("bili_browse", _category(error), "bili_browse_once")
                    raise
            finally:
                _BILI_AUTOMATIC.reset(token)
        return wrapped

    def _bili_inner(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(*args, **kwargs):
            if _BILI_AUTOMATIC.get() and self._blocked("bili_browse"):
                return None
            try:
                return await original(*args, **kwargs)
            except Exception as error:
                if _BILI_AUTOMATIC.get():
                    await self._report("bili_browse", _category(error), "bili_browse_once")
                raise
        return wrapped

    def _bili_action(self, original):
        @functools.wraps(original, updated=())
        async def wrapped(key, kind, target_id, handler, *, metadata=None):
            browsing = _BILI_AUTOMATIC.get()
            sharing = _BILI_SHARE.get() or (browsing and kind == "proactive_owner_recommend")
            if browsing or sharing:
                from .bili_journal_review import _OWNER_RECOVERY_SCOPE, _RESUME_SCOPE
                journal_scope = _RESUME_SCOPE.get()
                recovery_scope = _OWNER_RECOVERY_SCOPE.get()
                original_handler = handler
                async def checked_handler():
                    from .bili_journal_review import journal_generation_allowed
                    resume_token = _RESUME_SCOPE.set(journal_scope)
                    recovery_token = _OWNER_RECOVERY_SCOPE.set(recovery_scope)
                    try:
                        if sharing and not journal_generation_allowed():
                            return False
                        if ((browsing and self._blocked("bili_browse"))
                                or (sharing and self._blocked("bili_share"))):
                            return False
                        try:
                            result = original_handler()
                            return await result if inspect.isawaitable(result) else result
                        except Exception as error:
                            feature = "bili_share" if sharing else "bili_browse"
                            await self._report(feature, _category(error), "public_send")
                            raise
                    finally:
                        _OWNER_RECOVERY_SCOPE.reset(recovery_token)
                        _RESUME_SCOPE.reset(resume_token)
                handler = checked_handler
            return await original(key, kind, target_id, handler, metadata=metadata)
        return wrapped

    def refresh(self):
        if not self._active:
            return self.status()
        # A retired owner must not reinstall its wrappers over a newer
        # registry owner during the five-second replacement refresh.
        if _ACTIVE_INCIDENT_HOOKS is not None and _ACTIVE_INCIDENT_HOOKS is not self:
            return self.status()
        desired = {}
        def add(key, obj, name, factory, *, asynchronous=True):
            if obj is not None:
                desired[key] = (obj, name, factory, asynchronous)
        add("feed", self.host, "run_automatic_heybox_feed", self._feed)
        add("autopost", self.host, "maybe_autonomous_heybox_post", self._autopost)
        add("autopost_send", self.host, "authorized_autonomous_heybox_post", self._post_authorization,
            asynchronous=False)
        add("qzone_sync", self.host, "heybox_qzone_sync_once", self._qzone_sync)
        add("qzone_candidate", self.host, "candidate", self._qzone_candidate)
        add("qzone_request", self.host, "_hqz_request", self._qzone_request)
        add("bili_share", self.host, "retry_bili_recommendation", self._bili_share)
        module = sys.modules.get(self.host.__class__.__module__)
        add("bili_journal", module, "review_watched_video", self._journal)

        heybox = None
        try:
            heybox = self.host.plugin("astrbot_plugin_xiaoheihe_adapter")
            adapters = sorted(getattr(getattr(heybox, "runtime", None), "_adapters", ()), key=id)
        except Exception:
            adapters = ()
        services = [getattr(adapter, "_service", None) for adapter in adapters]
        services = [service for service in services if service is not None]
        for index, service in enumerate(services):
            add(f"notice_enqueue_{index}", service, "enqueue", self._notice_enqueue)
            add(f"notice_poll_{index}", service, "poll_once", self._notice_poll)
            add(f"notice_type_{index}", service, "_poll_event_type", self._notice_event_poll)
            add(f"notice_handle_{index}", service, "_handle", self._notice_handle)
        runtime = getattr(heybox, "runtime", None) if heybox is not None else None
        add("heybox_vision", heybox, "_caption_proactive_images", self._vision_caption)
        add("heybox_vision_group", heybox, "_caption_thread_image_group", self._vision_group)
        add("heybox_vision_provider", runtime, "report_auxiliary_provider_failure",
            self._vision_provider_failure, asynchronous=False)
        add("heybox_model_failure", runtime, "fail_reply", self._model_failure)
        add("heybox_reply_timeout", runtime, "expire_reply", self._reply_expire)
        add("heybox_delivery", runtime, "deliver", self._comment_delivery)
        add("heybox_get_client", runtime, "get_client", self._get_heybox_client)
        peer = sys.modules.get("data.plugins.feiyu_steward.heybox_peak")
        add("heybox_retry_register", peer, "register_event", self._retry_register, asynchronous=False)
        add("heybox_retry_pipeline", peer, "pipeline_scope", self._retry_pipeline, asynchronous=False)
        plugin_module = sys.modules.get(getattr(type(heybox), "__module__", ""))
        checked_type = getattr(plugin_module, "_CheckedProvider", None)
        add("heybox_retry_model_class", checked_type, "_report_exception", self._main_exception)
        clients = getattr(runtime, "_clients", {})
        if isinstance(clients, dict):
            for client in clients.values():
                add(f"comment_client_{id(client)}", client, "send_comment", self._comment_send)

        try:
            bili = self.host.plugin("astrbot_plugin_bilibili_ai_bot")
        except Exception:
            bili = None
        add("bili_browse", bili, "_run_proactive", self._bili_browse)
        add("bili_poll", bili, "_maybe_run_interest_proactive", self._bili_poll)
        add("bili_poll_finish", bili, "_finish_interest_proactive", self._bili_poll_finish)
        add("bili_inner", bili, "_run_proactive_inner", self._bili_inner)
        add("bili_action", bili, "_execute_proactive_action", self._bili_action)
        try:
            qzone = self.host.plugin("astrbot_plugin_qzone")
            service = getattr(qzone, "service", None)
        except Exception:
            service = None
        add("qzone_publication", service, "publish_post", self._qzone_publication)

        missing = []
        for key, (obj, name, factory, asynchronous) in desired.items():
            if not self._bind(key, obj, name, factory, asynchronous=asynchronous):
                missing.append(key)
        for key in set(self._bindings) - set(desired):
            self._restore_binding(self._bindings.pop(key))
        required = {"feed", "autopost", "autopost_send", "qzone_sync", "qzone_request", "qzone_candidate", "bili_share",
                    "bili_journal", "bili_browse", "bili_poll", "bili_poll_finish", "bili_inner", "bili_action", "qzone_publication",
                    "heybox_vision", "heybox_vision_group", "heybox_vision_provider",
                    "heybox_model_failure", "heybox_reply_timeout", "heybox_delivery",
                     "heybox_get_client", "heybox_retry_register", "heybox_retry_pipeline", "heybox_retry_model_class"}
        missing.extend(sorted(required - set(desired)))
        if (not callable(getattr(heybox, "_automatic_binding", None))
                or not callable(getattr(heybox, "_deny_automatic_event", None))):
            missing.append("heybox_vision_gate")
        if not services:
            missing.append("notice_service")
        self._missing = sorted(set(missing))
        if not self._missing and not self._retry_epoch_attempted:
            self._retry_epoch_attempted = True
            try:
                self._retry_available = self.host.incidents.retry_activate_runtime() is True
            except Exception:
                self._retry_available = False
        return self.status()

    def _native_feed_status(self):
        """Return only fixed identity booleans for the current native loop."""
        rows = []
        try:
            _, runtime, _ = _current_heybox(self)
            adapters = getattr(runtime, "_adapters", ())
            if isinstance(adapters, Mapping):
                adapters = adapters.values()
            for adapter in tuple(adapters):
                task = getattr(adapter, "_feed_task", None)
                if task is None or task.done():
                    continue
                coroutine = task.get_coro()
                feed = None
                # The peak scope wraps the native coroutine. Walk only its
                # bounded await chain; no local values leave this method.
                for _ in range(16):
                    frame = getattr(coroutine, "cr_frame", None)
                    local = frame.f_locals if frame is not None else {}
                    if local.get("feed") is not None:
                        feed = local["feed"]
                        break
                    coroutine = getattr(coroutine, "cr_await", None)
                    if coroutine is None:
                        break
                dispatch = getattr(feed, "synthetic_dispatch", None)
                function = getattr(dispatch, "__func__", None)
                getter = getattr(function, "__globals__", {}).get("get_runtime")
                repository = getattr(feed, "repository", None)
                feed_module = sys.modules.get(type(feed).__module__)
                repository_module = sys.modules.get(type(repository).__module__)
                producer = getattr(feed_module, "feiyu_unseen_feed", None)
                consumer = getattr(repository_module, "feiyu_unseen_feed", None)
                rows.append({
                    "builder_present": getattr(adapter, "_context_builder", None) is not None,
                    "feed_present": feed is not None,
                    "dispatch_bound_current": getattr(dispatch, "__self__", None) is adapter,
                    "dispatch_runtime_current": callable(getter) and getter() is runtime,
                    "repository_current": repository is getattr(runtime, "repository", None),
                    "feed_class_current": getattr(feed_module, "FeedService", None) is type(feed),
                    "admission_scope_shared": producer is not None and producer is consumer,
                })
            return {"observed": True, "loops": rows}
        except Exception:
            return {"observed": False, "loops": []}

    def status(self):
        stale = sorted(key for key, item in self._bindings.items()
                       if getattr(item["object"], item["name"], None) is not item["wrapped"])
        return {"installed": self._active, "ready": self._active and not self._missing and not stale,
                "bindings": len(self._bindings) - len(stale), "missing_targets": list(self._missing),
                "stale_targets": stale, "local_fail_closed": sorted(self._local_blocked),
                "new_content_retry_ready": self._retry_available,
                "native_feed_identity": self._native_feed_status()}

    def restore(self):
        global _ACTIVE_INCIDENT_HOOKS
        if self._active:
            self._active = False
            for binding in reversed(tuple(self._bindings.values())):
                self._restore_binding(binding)
            self._bindings.clear()
            self._notice_claims.clear()
            self._retry_available = False
            self._retry_events.clear()
            self._retry_claims.clear()
            self._retry_wakes.clear()
            self._retry_wake_seen.clear()
        if _ACTIVE_INCIDENT_HOOKS is self:
            _ACTIVE_INCIDENT_HOOKS = None
        return self.status()


def install_incident_hooks(host):
    return IncidentHooks(host)
