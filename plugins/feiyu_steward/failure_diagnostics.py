"""Content-free failure metadata, separate from all delivery ledgers.

Best effort only: diagnostics cannot change a business result or authorize a
retry. Only fixed categories/codes and audited source basenames are retained.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import sqlite3
import ssl
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


_FEATURES = frozenset({"heybox_comment", "heybox_qzone_sync", "friend_time_notice", "bili_journal",
                      "heybox_vision", "heybox_feed", "bili_browse", "bili_share", "heybox_autopost",
                      "qzone_publish"})
_STAGES = frozenset({"notice_poll", "qzone_sync_once", "friend_time_send", "friend_time_monitor",
                    "bili_journal_prepare", "bili_journal_read", "bili_journal_source",
                    "bili_journal_model", "bili_journal_journal", "bili_journal_share",
                    "bili_journal_once", "incident_notice_send", "vision_provider", "heybox_retry_decision",
                    "heybox_send", "heybox_image_dns", "heybox_main_capture", "heybox_main_model",
                    "qzone_session_capture", "qzone_session_refresh", "unknown"})
_CLASSES = frozenset({"sqlite", "timeout", "connection", "http_client", "heybox_api", "os",
                     "type", "attribute", "key", "value", "runtime", "permission", "other",
                     "private_review_model", "native_unknown", "native_transport", "qq_action_failed", "qq_api_not_available", "qq_timing_error"})
_REASONS = frozenset({"", "other", "receipt_missing", "receipt_invalid", "notice_cancelled",
                     "native_bridge_request_unconfirmed", "native_delivery_unknown_do_not_resend",
                     "incident_notice_unknown_do_not_resend", "incident_native_route_unavailable",
                     "incident_notice_not_authorized", "native_action_not_authorized",
                     "native_identity_unconfirmed", "native_receipt_unconfirmed",
                     "native_reservation_corrupt", "native_transport_disabled",
                     "native_retired_transport_requires_current_route", "native_authorizer_required",
                     "friend_time_notice_outbound_not_allowed", "outbound_business_paused_during_peak",
                     "private_review_provider_unavailable", "private_review_call_failed",
                     "private_review_multiple_attempts", "private_review_window_unavailable",
                     "journal_ledger_unavailable", "source_read_failed", "provider_unavailable",
                     "invalid_model_or_citation", "native_watch_unavailable", "source_identity_mismatch",
                     "source_coverage_invalid", "source_identity_or_coverage_invalid",
                     "source_pagination_invalid", "source_route_invalid",
                     "qzone_session_route_invalid", "qzone_session_route_unavailable",
                     "qzone_session_target_unavailable", "qzone_session_client_changed",
                     "source_contract_invalid", "source_unavailable", "same_task_deadline_expired",
                     "vision_retry_graph_unavailable", "vision_retry_tls_certificate_present",
                     "vision_retry_tls_eof_unmatched", "vision_retry_tls_other",
                     "vision_retry_protected_exception", "vision_retry_http_status",
                     "vision_retry_image_host_missing_cause", "vision_retry_tls_eof_connection",
                     "vision_retry_connection", "vision_retry_timeout", "vision_retry_unclassified",
                     "main_retry_context_missing", "main_retry_classification_unavailable"}) | frozenset(
    "retry_" + kind + "_" + gate
    for kind in ("tls_eof", "connection", "timeout", "other")
    for gate in ("runtime_unavailable", "trial_failed", "duplicate", "not_available",
                 "terminal_unverified", "disallowed", "report_unavailable", "ready_unconfirmed",
                 "wake_requested", "wake_unavailable")
) | frozenset("main_retry_" + suffix for suffix in (
    "graph_unavailable", "tls_certificate_present", "tls_eof_unmatched", "tls_other",
    "protected_exception", "http_status", "image_host_missing_cause", "tls_eof_connection",
    "connection", "timeout", "unclassified"))
_STEWARD_FILES = frozenset({"incident_policy.py", "incident_hooks.py", "friend_time_notice.py",
                          "bili_journal_review.py", "bili_recommendation.py", "heybox_qzone_sync.py",
                          "native_private_transport.py", "native_private_integration.py",
                          "peak_policy.py", "peak_runtime.py", "private_review_model.py",
                          "bili_humanism.py", "main.py", "reconnect.py"})
_ADAPTER_FILES = frozenset({"api_client.py", "notification_service.py", "feed_service.py", "runtime.py"})
_HERE = Path(__file__).resolve().parent
_PRODUCTION = Path("D:/QQBot/data/astrbot/plugins/feiyu_steward").resolve()
_ADAPTER = Path("D:/QQBot/data/astrbot/plugins/astrbot_plugin_xiaoheihe_adapter/xiaoheihe").resolve()
_VALIDATION_CODES = frozenset({
    "output_type", "output_empty", "output_too_large", "json_invalid", "decision_structure",
    "worthy_type", "reason_invalid", "skipped_note_nonnull", "note_structure", "note_field_type",
    "note_field_too_long", "note_required_empty", "evidence_structure", "a2_author_attribution",
    "evidence_type_invalid", "evidence_empty", "evidence_too_many",
    "evidence_entry_structure", "segment_id_invalid", "quote_format_invalid", "quote_no_alnum",
    "quote_not_verbatim", "claim_invalid", "evidence_duplicate",
})
_FINISH_REASONS = frozenset({"unavailable", "other", "stop", "length", "content_filter",
                             "tool_calls", "function_call"})
_MODEL_FAILURE_KINDS = frozenset({
    "upstream_server_error", "rate_limit", "authentication", "permission",
    "connection", "timeout", "http_status_error", "other",
})

# Names are mapped only to fixed labels; exception messages, requests, URLs,
# response objects, and arbitrary exception type names are never serialized.
_TRANSPORT_HTTP_TYPES = (
    ("ConnectTimeout", "connect_timeout"), ("ReadTimeout", "read_timeout"),
    ("WriteTimeout", "write_timeout"), ("PoolTimeout", "pool_timeout"),
    ("ConnectError", "connect_error"), ("ReadError", "read_error"),
    ("WriteError", "write_error"), ("CloseError", "close_error"),
    ("RemoteProtocolError", "remote_protocol_error"),
    ("LocalProtocolError", "local_protocol_error"), ("ProtocolError", "protocol_error"),
    ("NetworkError", "network_error"), ("TimeoutException", "timeout"),
)
_TRANSPORT_KINDS = frozenset({
    "tls_certificate", "tls_eof", "tls_want_read", "tls_want_write", "tls_zero_return", "tls_syscall", "tls_other", "dns_error", "connection_reset",
    "connection_refused", "connection_aborted", "broken_pipe", "timeout", "os_error",
    "eof", "anyio_end_of_stream", "anyio_broken_resource", "heybox_api", "other",
    "openai_api_timeout", "openai_api_connection", "openai_authentication", "openai_permission",
    "openai_rate_limit", "openai_api_status", "openai_api_error",
} | {module + "_" + label for module in ("httpx", "httpcore")
     for _, label in _TRANSPORT_HTTP_TYPES})
_IMAGE_DNS_PHASES = frozenset({"system_dns", "secure_dns", "connect", "write", "read_headers",
                               "validate_http", "read_body", "validate_dns", "close", "unknown"})
_IMAGE_DNS_REASONS = frozenset({"system_error", "empty_result", "timeout", "total_timeout",
                                "connection_reset", "tls_error", "http_status", "response_validation",
                                "other", "unknown"})
_IMAGE_DNS_CAUSES = frozenset({"gaierror", "TimeoutError", "ConnectionResetError", "SSLError",
                               "SSLCertVerificationError", "IncompleteReadError", "LimitOverrunError",
                               "ValueError", "OSError", "unknown"})
_IMAGE_SECURITY_MODULE = "data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.security"


def _valid_image_dns(value):
    return (type(value) is dict and set(value) == {"phase", "reason", "cause", "attempts"}
            and type(value["phase"]) is str and value["phase"] in _IMAGE_DNS_PHASES
            and type(value["reason"]) is str and value["reason"] in _IMAGE_DNS_REASONS
            and type(value["cause"]) is str and value["cause"] in _IMAGE_DNS_CAUSES
            and type(value["attempts"]) is int and value["attempts"] in {0, 1, 2})


def _valid_poll_pool(value):
    # Fixed counts only. No origin, request, identity, exception or network data.
    fields = {"limit", "connections", "requests", "queued", "unrecognized_connections",
              "unconnected_unclassified", "unconnected_active",
              "unconnected_active_unassigned", "unassigned_stream_closed",
              "unassigned_stream_open", "unassigned_stream_unknown"}
    if type(value) is not dict or set(value) != fields:
        return False
    if any(type(value[key]) is not int or not 0 <= value[key] <= 4096 for key in fields):
        return False
    return (value["limit"] > 0 and value["queued"] <= value["requests"]
            and value["unconnected_active"] <= value["connections"]
            and value["unconnected_unclassified"] + value["unconnected_active"]
            + value["unrecognized_connections"] <= value["connections"]
            and value["unconnected_active_unassigned"] <= value["unconnected_active"]
            and value["unassigned_stream_closed"] + value["unassigned_stream_open"]
            + value["unassigned_stream_unknown"] == value["unconnected_active_unassigned"])


def _valid_poll_client(value):
    bool_fields = {"service_registered", "client_present", "client_same_since_start", "runtime_client_same",
                   "client_class_match", "owns_client", "httpx_class_match", "loopback_7897"}
    required = bool_fields | {"mode", "transport_kind", "poll_phase"}
    return (type(value) is dict and set(value) in (required, required | {"pool"})
            and ("pool" not in value or _valid_poll_pool(value["pool"]))
            and all(value[key] is None or type(value[key]) is bool for key in bool_fields)
            and type(value["mode"]) is str and value["mode"] in {
                "proxy_marked", "direct_marked", "unmarked", "other", "unavailable"}
            and type(value["transport_kind"]) is str and value["transport_kind"] in {
                "proxy", "direct", "other", "unavailable"}
            and type(value["poll_phase"]) is str and value["poll_phase"] in {"poll_once", "event_type", "unknown"})


def _image_dns_metadata(error):
    module = sys.modules.get(_IMAGE_SECURITY_MODULE)
    cls = getattr(module, "ImageHostResolutionError", None)
    if (not isinstance(cls, type) or cls.__module__ != _IMAGE_SECURITY_MODULE
            or not isinstance(error, cls)):
        return None
    values = BaseException.__dict__["__dict__"].__get__(error)
    value = {key: values.get(key) for key in ("phase", "reason", "cause", "attempts")}
    return value if _valid_image_dns(value) else None


def _transport_kind(error):
    if isinstance(error, ssl.SSLCertVerificationError):
        return "tls_certificate"
    if isinstance(error, ssl.SSLEOFError):
        return "tls_eof"
    for cls, label in ((ssl.SSLWantReadError, "tls_want_read"), (ssl.SSLWantWriteError, "tls_want_write"),
                       (ssl.SSLZeroReturnError, "tls_zero_return"), (ssl.SSLSyscallError, "tls_syscall")):
        if isinstance(error, cls):
            return label
    if isinstance(error, ssl.SSLError):
        return "tls_other"
    openai = sys.modules.get("openai")
    for name, label in (("APITimeoutError", "openai_api_timeout"),
                        ("APIConnectionError", "openai_api_connection"),
                        ("AuthenticationError", "openai_authentication"),
                        ("PermissionDeniedError", "openai_permission"),
                        ("RateLimitError", "openai_rate_limit"),
                        ("APIStatusError", "openai_api_status"), ("APIError", "openai_api_error")):
        cls = getattr(openai, name, None)
        if isinstance(cls, type) and isinstance(error, cls):
            return label
    for name in ("httpx", "httpcore"):
        module = sys.modules.get(name)
        for type_name, label in _TRANSPORT_HTTP_TYPES:
            cls = getattr(module, type_name, None)
            if isinstance(cls, type) and isinstance(error, cls):
                return name + "_" + label
    for name, label in (("EndOfStream", "anyio_end_of_stream"),
                        ("BrokenResourceError", "anyio_broken_resource")):
        cls = getattr(sys.modules.get("anyio"), name, None)
        if isinstance(cls, type) and isinstance(error, cls):
            return label
    for cls, label in ((socket.gaierror, "dns_error"), (ConnectionResetError, "connection_reset"),
                       (ConnectionRefusedError, "connection_refused"),
                       (ConnectionAbortedError, "connection_aborted"), (BrokenPipeError, "broken_pipe"),
                       (TimeoutError, "timeout"), (EOFError, "eof"), (OSError, "os_error")):
        if isinstance(error, cls):
            return label
    return "heybox_api" if _class(error) == "heybox_api" else "other"


def _transport_graph(error, *, include_suppressed=False):
    """Bounded, content-free cause/context graph; never controls authorization."""
    if not isinstance(error, BaseException):
        return None
    pending, seen, nodes, repeated = [(error, -1, "root")], set(), [], False
    while pending and len(nodes) < 32:
        item, parent, edge = pending.pop(0)
        if not isinstance(item, BaseException):
            return None
        if id(item) in seen:
            repeated = True
            continue
        seen.add(id(item))
        index = len(nodes)
        node = {"parent": parent, "edge": edge, "kind": _transport_kind(item)}
        if include_suppressed:
            node["suppressed"] = BaseException.__suppress_context__.__get__(item) is True
        nodes.append(node)
        for label, descriptor in (("cause", BaseException.__cause__), ("context", BaseException.__context__)):
            child = descriptor.__get__(item)
            if child is not None:
                pending.append((child, index, label))
    return {"nodes": nodes, "truncated": bool(pending), "repeated": repeated}


def _valid_transport_graph(value):
    if (type(value) is not dict or set(value) != {"nodes", "truncated", "repeated"}
            or type(value["truncated"]) is not bool or type(value["repeated"]) is not bool
            or type(value["nodes"]) is not list or not 1 <= len(value["nodes"]) <= 32):
        return False
    for index, node in enumerate(value["nodes"]):
        if (type(node) is not dict or set(node) not in ({"parent", "edge", "kind"},
                                                        {"parent", "edge", "kind", "suppressed"})
                or type(node["parent"]) is not int or type(node["edge"]) is not str
                or type(node["kind"]) is not str or node["kind"] not in _TRANSPORT_KINDS
                or "suppressed" in node and type(node["suppressed"]) is not bool):
            return False
        if index == 0:
            if node["parent"] != -1 or node["edge"] != "root":
                return False
        elif node["parent"] < 0 or node["parent"] >= index or node["edge"] not in {"cause", "context"}:
            return False
    return True


def _valid_model_failure(value):
    """Allow only fixed classification and a bounded integer HTTP status."""
    return (type(value) is dict and len(value) == 2
            and all(type(key) is str for key in value)
            and set(value) == {"kind", "http_status"}
            and type(value["kind"]) is str and value["kind"] in _MODEL_FAILURE_KINDS
            and (value["http_status"] is None or
                 type(value["http_status"]) is int and 100 <= value["http_status"] <= 599))


def _valid_decision_shape(value):
    """Validate fixed structure metadata; arbitrary model keys are forbidden."""
    if (type(value) is not dict or set(value) != {
            "top_type", "missing_mask", "extra_keys_count"}
            or type(value["top_type"]) is not str or value["top_type"] not in {
                "object", "array", "string", "number", "boolean", "null", "unavailable"}):
        return False
    missing, extra = value["missing_mask"], value["extra_keys_count"]
    if value["top_type"] == "object":
        return (type(missing) is int and 0 <= missing <= 7
                and type(extra) is int and 0 <= extra <= 65
                and (missing != 0 or extra != 0))
    return missing is None and extra is None


def _valid_model_validation(value):
    """Accept only this bounded, content-free optional diagnostic extension."""
    fields = {
            "code", "completion_chars", "source_segments", "source_chars",
            "finish_reason", "response_choices"}
    if type(value) is not dict or set(value) not in (fields, fields | {"decision_shape"}):
        return False
    if "decision_shape" in value and (
            value["code"] != "decision_structure" or not _valid_decision_shape(value["decision_shape"])):
        return False
    return (type(value["code"]) is str and value["code"] in _VALIDATION_CODES
            and type(value["finish_reason"]) is str and value["finish_reason"] in _FINISH_REASONS
            and (value["completion_chars"] is None or
                 type(value["completion_chars"]) is int and 0 <= value["completion_chars"] <= 80001)
            and type(value["source_segments"]) is int and 0 <= value["source_segments"] <= 30001
            and type(value["source_chars"]) is int and 0 <= value["source_chars"] <= 40001
            and (value["response_choices"] is None or
                 type(value["response_choices"]) is int and 0 <= value["response_choices"] <= 17))


def _class(error):
    if error is None:
        return "other"
    mro = type(error).__mro__
    for base in mro:
        module, name = base.__module__, base.__name__
        if module.endswith(".native_private_transport") and name == "NativeDeliveryUnknown":
            return "native_unknown"
        if module.endswith(".native_private_transport") and name == "NativeTransportError":
            return "native_transport"
        if module.endswith(".private_review_model") and name == "PrivateReviewModelError":
            return "private_review_model"
        if module == "aiocqhttp.exceptions" and name in {"ApiNotAvailable", "TimingError"}:
            return "qq_api_not_available" if name == "ApiNotAvailable" else "qq_timing_error"
        if module == "aiocqhttp.exceptions" and name in {"ActionFailed", "ApiError", "NetworkError"}:
            return "qq_action_failed"
        if module.endswith(".xiaoheihe.api_client") and name == "XiaoheiheApiError":
            return "heybox_api"
    if isinstance(error, sqlite3.Error):
        return "sqlite"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection"
    if any(base.__module__.split(".", 1)[0] in {"httpx", "httpcore", "aiohttp"} for base in mro):
        return "http_client"
    for cls, label in ((PermissionError, "permission"), (OSError, "os"), (TypeError, "type"),
                       (AttributeError, "attribute"), (KeyError, "key"), (ValueError, "value"),
                       (RuntimeError, "runtime")):
        if isinstance(error, cls):
            return label
    return "other"


def _reason(error, reason):
    if type(reason) is str:
        return reason if reason in _REASONS else "other"
    # Compare fixed codes only. Never serialize the error or any argument.
    args = getattr(error, "args", ())
    if type(args) is tuple and len(args) == 1 and type(args[0]) is str and args[0] in _REASONS:
        return args[0]
    return ""


def _sources(error):
    frames = []
    trace = getattr(error, "__traceback__", None)
    traversed = 0
    while trace is not None and traversed < 64:
        traversed += 1
        try:
            file = Path(trace.tb_frame.f_code.co_filename).resolve()
        except Exception:
            trace = trace.tb_next
            continue
        source = None
        if file.parent in {_HERE, _PRODUCTION} and file.name in _STEWARD_FILES:
            source = "feiyu_steward/" + file.name
        elif file.parent == _ADAPTER and file.name in _ADAPTER_FILES:
            source = "xiaoheihe/" + file.name
        if source is not None and type(trace.tb_lineno) is int and 0 < trace.tb_lineno < 1_000_000:
            frames.append({"file": source, "line": trace.tb_lineno})
        trace = trace.tb_next
    return frames[-8:]


def describe_failure(error=None, stage="unknown", reason=None, *, model_validation=None, poll_client=None):
    """Return only a fixed safe contract, even for hostile exception metadata."""
    fallback = {"stage": stage if type(stage) is str and stage in _STAGES else "unknown",
                "error_class": "other", "reason": "", "sources": [], "causes": []}
    try:
        result = {**fallback, "error_class": _class(error), "reason": _reason(error, reason),
                  "sources": _sources(error)}
        if result["stage"] in {"heybox_send", "heybox_main_capture", "heybox_main_model", "notice_poll"}:
            transport = _transport_graph(error, include_suppressed=result["stage"] != "heybox_send")
            if _valid_transport_graph(transport):
                result["transport"] = transport
        if result["stage"] == "notice_poll" and _valid_poll_client(poll_client):
            result["poll_client"] = dict(poll_client)
        if result["stage"] == "heybox_image_dns":
            dns = _image_dns_metadata(error)
            if _valid_image_dns(dns):
                result["image_dns"] = dns
        if (result["stage"] not in {"heybox_send", "heybox_image_dns", "heybox_main_capture", "heybox_main_model", "notice_poll"}
                and result["error_class"] == "private_review_model"
                and result["reason"] == "private_review_call_failed"):
            # Bypass arbitrary properties on exceptions; never touch SDK
            # request/response objects, body, URL, headers, or exception text.
            values = BaseException.__dict__["__dict__"].__get__(error)
            metadata = values.get("model_failure")
            if _valid_model_failure(metadata):
                result["model_failure"] = {
                    "kind": metadata["kind"], "http_status": metadata["http_status"],
                }
        seen, current = {id(error)}, error
        for _ in range(3):
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            result["causes"].append({"error_class": _class(current), "reason": _reason(current, None),
                                     "sources": _sources(current)})
        if (result["reason"] == "invalid_model_or_citation"
                and result["stage"] in {"bili_journal_once", "bili_journal_model"}
                and _valid_model_validation(model_validation)):
            # Copy only fixed scalar fields. The optional nested shape must
            # not remain an alias that a caller can mutate after validation.
            validation = {key: model_validation[key] for key in (
                "code", "completion_chars", "source_segments", "source_chars",
                "finish_reason", "response_choices")}
            if "decision_shape" in model_validation:
                shape = model_validation["decision_shape"]
                validation["decision_shape"] = {key: shape[key] for key in (
                    "top_type", "missing_mask", "extra_keys_count")}
            if _valid_model_validation(validation):
                result["model_validation"] = validation
        return result
    except Exception:
        return fallback


def _valid_description(value):
    fields = {"stage", "error_class", "reason", "sources", "causes"}
    if (type(value) is not dict or set(value) not in (
            fields, fields | {"model_validation"}, fields | {"model_failure"},
            fields | {"transport"}, fields | {"image_dns"}, fields | {"poll_client"},
            fields | {"transport", "poll_client"})
            or value["stage"] not in _STAGES or value["error_class"] not in _CLASSES
            or value["reason"] not in _REASONS or type(value["causes"]) is not list or len(value["causes"]) > 3):
        return False
    if "transport" in value and (
            value["stage"] not in {"heybox_send", "heybox_main_capture", "heybox_main_model", "notice_poll"}
            or not _valid_transport_graph(value["transport"])):
        return False
    if "poll_client" in value and (value["stage"] != "notice_poll" or not _valid_poll_client(value["poll_client"])):
        return False
    if "image_dns" in value and (
            value["stage"] != "heybox_image_dns" or not _valid_image_dns(value["image_dns"])):
        return False
    if "model_failure" in value and (
            value["error_class"] != "private_review_model"
            or value["reason"] != "private_review_call_failed"
            or not _valid_model_failure(value["model_failure"])):
        return False
    if "model_validation" in value and (
            value["reason"] != "invalid_model_or_citation"
            or value["stage"] not in {"bili_journal_once", "bili_journal_model"}
            or not _valid_model_validation(value["model_validation"])):
        return False
    def valid_sources(sources):
        allowed = {"feiyu_steward/" + name for name in _STEWARD_FILES} | {"xiaoheihe/" + name for name in _ADAPTER_FILES}
        return (type(sources) is list and len(sources) <= 8 and all(
            type(row) is dict and set(row) == {"file", "line"} and row["file"] in allowed
            and type(row["line"]) is int and 0 < row["line"] < 1_000_000 for row in sources))
    if not valid_sources(value["sources"]):
        return False
    return all(type(row) is dict and set(row) == {"error_class", "reason", "sources"}
               and row["error_class"] in _CLASSES and row["reason"] in _REASONS
               and valid_sources(row["sources"]) for row in value["causes"])


@contextmanager
def _file_lock(path):
    if path.is_symlink():
        raise OSError("diagnostic_lock_invalid")
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
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
        locked = True
        yield
    finally:
        if locked:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def record_failure(root, feature, stage, error=None, reason=None, *, model_validation=None, poll_client=None):
    """Keep first/latest failure per feature; any storage issue is non-fatal."""
    diagnostic = describe_failure(error, stage, reason, model_validation=model_validation, poll_client=poll_client)
    temporary = None
    try:
        if root is None or type(feature) is not str or feature not in _FEATURES:
            return diagnostic
        root = Path(root)
        if not root.is_absolute() or root.is_symlink():
            return diagnostic
        folder = root / "failure-diagnostics"
        if folder.is_symlink():
            return diagnostic
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (feature + ".json")
        if path.is_symlink():
            return diagnostic
        with _file_lock(folder / (feature + ".lock")):
            stamp = time.time()
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
                return diagnostic
            entry = {"at": stamp, "diagnostic": diagnostic}
            value = {"schema": 1, "feature": feature, "count": 1, "first": entry, "latest": entry}
            if path.exists():
                if path.stat().st_size > 32768:
                    return diagnostic
                prior = json.loads(path.read_text(encoding="utf-8"))
                fields = {"schema", "feature", "count", "first", "latest"}
                optional = {"last_model_validation", "last_vision_provider", "last_heybox_send", "last_image_dns",
                            "last_heybox_main_capture", "last_heybox_main_model", "last_heybox_notice_poll"}
                if (type(prior) is not dict or not fields.issubset(prior)
                        or not set(prior).issubset(fields | optional)
                        or prior["schema"] != 1 or prior["feature"] != feature
                        or type(prior["count"]) is not int or not 1 <= prior["count"] < 1_000_000_000):
                    return diagnostic
                keys = ("first", "latest") + tuple(
                    key for key in ("last_model_validation", "last_vision_provider", "last_heybox_send", "last_image_dns",
                                    "last_heybox_main_capture", "last_heybox_main_model", "last_heybox_notice_poll") if key in prior)
                for key in keys:
                    item = prior[key]
                    if (type(item) is not dict or set(item) != {"at", "diagnostic"}
                            or type(item["at"]) not in (int, float) or not math.isfinite(item["at"])
                            or item["at"] <= 0 or not _valid_description(item["diagnostic"])):
                        return diagnostic
                    if key == "last_model_validation" and (
                            feature != "bili_journal" or "model_validation" not in item["diagnostic"]):
                        return diagnostic
                    if key == "last_vision_provider" and (
                            feature != "heybox_vision" or item["diagnostic"]["stage"] != "vision_provider"):
                        return diagnostic
                    if key == "last_heybox_send" and (
                            feature != "heybox_comment" or item["diagnostic"]["stage"] != "heybox_send"):
                        return diagnostic
                    if key == "last_image_dns" and (
                            feature != "heybox_vision" or item["diagnostic"]["stage"] != "heybox_image_dns"
                            or "image_dns" not in item["diagnostic"]):
                        return diagnostic
                    if key == "last_heybox_main_capture" and (
                            feature != "heybox_comment" or item["diagnostic"]["stage"] != "heybox_main_capture"):
                        return diagnostic
                    if key == "last_heybox_main_model" and (
                            feature != "heybox_comment" or item["diagnostic"]["stage"] != "heybox_main_model"):
                        return diagnostic
                    if key == "last_heybox_notice_poll" and (
                            feature != "heybox_comment" or item["diagnostic"]["stage"] != "notice_poll"):
                        return diagnostic
                value["first"], value["count"] = prior["first"], prior["count"] + 1
                for key in ("last_model_validation", "last_vision_provider", "last_heybox_send", "last_image_dns",
                            "last_heybox_main_capture", "last_heybox_main_model", "last_heybox_notice_poll"):
                    if key in prior:
                        value[key] = prior[key]
            if feature == "bili_journal" and "model_validation" in diagnostic:
                # An incident-notice failure may immediately replace `latest`.
                # Keep the last validation separately without changing `first`.
                value["last_model_validation"] = entry
            if feature == "heybox_vision" and diagnostic["stage"] == "vision_provider":
                # The retry decision can replace latest immediately afterwards.
                # Retain only the same validated, content-free provider diagnostic.
                value["last_vision_provider"] = entry
            if feature == "heybox_comment" and diagnostic["stage"] == "heybox_send":
                # Later notice/retry diagnostics must not erase this cause graph.
                value["last_heybox_send"] = entry
            if feature == "heybox_vision" and diagnostic["stage"] == "heybox_image_dns" and "image_dns" in diagnostic:
                # Later retry decisions cannot replace the fixed resolver metadata.
                value["last_image_dns"] = entry
            if feature == "heybox_comment" and diagnostic["stage"] == "heybox_main_capture":
                value["last_heybox_main_capture"] = entry
            if feature == "heybox_comment" and diagnostic["stage"] == "heybox_main_model":
                value["last_heybox_main_model"] = entry
            if feature == "heybox_comment" and diagnostic["stage"] == "notice_poll":
                value["last_heybox_notice_poll"] = entry
            temporary = folder / (feature + "." + uuid.uuid4().hex + ".tmp")
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
    except Exception:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return diagnostic
