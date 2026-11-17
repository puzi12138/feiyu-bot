"""Observe only already-authorized non-stream Xiaoheihe model calls.

The collector never sends, retries, cancels, selects or closes a client. It does
not read request/response headers, bodies, exception text or arbitrary trace
values. Retiring an owner disables in-flight collectors and removes only its
own HTTPX request hooks.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import inspect
import json
import math
import ssl
import time
import uuid
import weakref

import httpcore
import httpx
import openai

_CURRENT = ContextVar("feiyu_model_transport_observation", default=None)
_EVENTS = frozenset(prefix + "." + phase for prefix in (
    "connection.connect_tcp", "proxy.start_tls", "http11.send_request_headers",
    "http11.send_request_body", "http11.receive_response_headers",
    "http11.receive_response_body", "http11.response_closed",
) for phase in ("started", "complete", "failed"))
_TRACE_LIMIT = 16


def _milliseconds(start):
    return max(0, min(3_600_000, int((time.monotonic() - start) * 1000)))


class ModelTransportDiagnostics:
    """Own fixed request hooks and event-local successful vision summaries."""

    def __init__(self):
        self.active = True
        self.generation = uuid.uuid4().hex
        self._salt = uuid.uuid4().bytes
        self._clients = weakref.WeakKeyDictionary()
        self._visions = weakref.WeakKeyDictionary()

    def _identity(self, value):
        return hashlib.sha256(self._salt + str(id(value)).encode("ascii")).hexdigest()

    def begin(self, checked, event, phase):
        """Start observation after the existing incident wrapper proves scope."""
        if not self.active or phase not in {"main", "vision"}:
            return None
        event_id = getattr(event, "event_id", None)
        if type(event_id) is not int or not 0 < event_id < 1_000_000_000:
            return None
        provider = checked._provider
        sdk = provider.client
        client = sdk._client
        if type(sdk) is not openai.AsyncOpenAI or type(client) is not httpx.AsyncClient:
            return None
        if client.is_closed:
            return None
        # The installed SDK uses its own URL type; normalize into this client's
        # HTTPX URL without replacing or reconfiguring either client.
        base_url = httpx.URL(str(sdk.base_url))
        transport = client._transport_for_url(base_url)
        pool = getattr(transport, "_pool", None)
        if type(pool) is not httpcore.AsyncHTTPProxy:
            return None
        context = pool._ssl_context
        proxy = pool._proxy_url
        if (not isinstance(context, ssl.SSLContext) or proxy.host != b"127.0.0.1"
                or proxy.scheme != b"http" or type(proxy.port) is not int or proxy.port != 7897):
            return None
        if not self._install(client):
            return None
        host = base_url.host
        path = base_url.path
        if type(host) is not str or type(path) is not str:
            return None
        data = {
            "schema": 1, "generation": self.generation, "invocation": uuid.uuid4().hex,
            "event_id": event_id, "phase": phase, "started_at": time.time(),
            "elapsed_ms": 0, "outcome": "running",
            "provider": self._identity(provider), "sdk": self._identity(sdk),
            "client": self._identity(client), "tls_context": self._identity(context),
            "host_sha256": hashlib.sha256(host.encode()).hexdigest(),
            "path_sha256": hashlib.sha256(path.encode()).hexdigest(),
            "certificate_verification": context.verify_mode == ssl.CERT_REQUIRED,
            "hostname_verification": context.check_hostname is True,
            "request_count": 0, "requests_truncated": False, "trace_truncated": False,
            "trace": [], "existing_trace_skipped": False, "route_changed": False,
            "scope_task_mismatch": False,
        }
        if phase == "main":
            visual = self._visions.get(event)
            if visual is not None:
                data["vision"] = {key: visual[key] for key in (
                    "invocation", "event_id", "outcome", "elapsed_ms", "provider",
                    "sdk", "client", "tls_context", "host_sha256", "path_sha256",
                )}
        span = {"owner": self, "event": event, "checked": checked,
                "sdk": sdk, "client": client, "context": context, "pool": pool,
                "host": host, "proxy_port": proxy.port,
                "task": asyncio.current_task(), "start": time.monotonic(),
                "data": data, "active": True}
        span["token"] = _CURRENT.set(span)
        return span

    def _install(self, client):
        hooks = client.event_hooks
        if type(hooks) is not dict or type(hooks.get("request")) is not list:
            return False
        known = self._clients.get(client)
        if known is not None:
            return any(item is known for item in hooks["request"])
        if len(self._clients) >= 16:
            return False
        owner_ref, client_ref = weakref.ref(self), weakref.ref(client)

        async def request_hook(request):
            owner, actual_client = owner_ref(), client_ref()
            try:
                span = _CURRENT.get()
                if (owner is not None and owner.active and actual_client is not None
                        and type(span) is dict and span.get("owner") is owner
                        and span.get("active") is True and span.get("client") is actual_client):
                    if span.get("task") is asyncio.current_task():
                        owner._attach(span, request)
                    else:
                        span["data"]["scope_task_mismatch"] = True
            except Exception:
                # Collection errors never change a model request.
                pass

        # Replace the list; never mutate an in-flight HTTPX iterator's list.
        hooks["request"] = hooks["request"] + [request_hook]
        self._clients[client] = request_hook
        return True

    def _attach(self, span, request):
        if type(request) is not httpx.Request or type(request.extensions) is not dict:
            return
        if request.url.host != span["host"] or request.url.scheme != "https":
            return
        data = span["data"]
        selected_pool = getattr(span["client"]._transport_for_url(request.url), "_pool", None)
        if (span["checked"]._provider.client is not span["sdk"]
                or span["sdk"]._client is not span["client"]
                or selected_pool is not span["pool"]
                or selected_pool._ssl_context is not span["context"]
                or selected_pool._proxy_url.host != b"127.0.0.1"
                or selected_pool._proxy_url.port != span["proxy_port"]):
            data["route_changed"] = True
            return
        if data["request_count"] >= 16:
            data["requests_truncated"] = True
            return
        data["request_count"] += 1
        previous = request.extensions.get("trace")
        if getattr(previous, "_feiyu_transport_span", None) is span:
            return
        # Unsupported existing callbacks are left completely unchanged.
        if previous is not None and not callable(previous):
            data["existing_trace_skipped"] = True
            return
        owner_ref = weakref.ref(self)

        async def traced(name, info):
            owner = owner_ref()
            if owner is not None and owner.active and span.get("active") is True:
                try:
                    owner._trace(span, name, info)
                except Exception:
                    pass
            if previous is not None:
                result = previous(name, info)
                # Match installed httpcore's existing callback contract exactly.
                if not inspect.iscoroutine(result):
                    raise TypeError(
                        "If you're using an asynchronous interface, "
                        "the callback of the `trace` extension should "
                        "be an asynchronous function rather than a normal function."
                    )
                await result

        traced._feiyu_transport_span = span
        request.extensions["trace"] = traced

    def _trace(self, span, name, info):
        if (type(name) is not str or name not in _EVENTS or type(info) is not dict
                or span.get("task") is not asyncio.current_task()):
            return
        data = span["data"]
        if len(data["trace"]) >= _TRACE_LIMIT:
            data["trace_truncated"] = True
            return
        row = {"event": name, "elapsed_ms": _milliseconds(span["start"])}
        if name == "connection.connect_tcp.complete":
            stream = info.get("return_value")
            local = stream.get_extra_info("client_addr") if stream is not None else None
            peer = stream.get_extra_info("server_addr") if stream is not None else None
            if (type(local) is tuple and type(peer) is tuple and len(local) >= 2 and len(peer) >= 2
                    and local[0] == peer[0] == "127.0.0.1"
                    and type(local[1]) is int and 0 < local[1] < 65536
                    and peer[1] == span["proxy_port"]):
                row["source_port"] = local[1]
        if name == "proxy.start_tls.started":
            row["context_matches"] = info.get("ssl_context") is span["context"]
            row["sni_matches"] = info.get("server_hostname") == span["host"]
            timeout = info.get("timeout")
            if type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 600:
                row["timeout_seconds"] = timeout
        data["trace"].append(row)

    def snapshot(self, checked=None, *, failed=False):
        """Return a detached fixed summary for an existing failure record."""
        span = _CURRENT.get()
        if (not self.active or type(span) is not dict or span.get("owner") is not self
                or span.get("active") is not True or span.get("task") is not asyncio.current_task()
                or (checked is not None and span.get("checked") is not checked)):
            return None
        if failed:
            span["data"]["outcome"] = "failed"
        data = dict(span["data"])
        data["elapsed_ms"] = _milliseconds(span["start"])
        return json.loads(json.dumps(data, allow_nan=False))

    def last_vision(self, event):
        """Read a completed event-local vision observation, never another event."""
        value = self._visions.get(event)
        return dict(value) if self.active and value is not None else None

    def finish(self, span, outcome):
        """Detach this task scope, keeping only a completed visual identity."""
        if span is None:
            return
        try:
            data = span["data"]
            data["elapsed_ms"] = _milliseconds(span["start"])
            if data["outcome"] != "failed":
                data["outcome"] = outcome
            if self.active and data["phase"] == "vision":
                # Keep full metadata in memory only until the normal group report.
                self._visions[span["event"]] = json.loads(json.dumps(data, allow_nan=False))
        finally:
            span["active"] = False
            _CURRENT.reset(span["token"])
            span["checked"] = span["event"] = span["sdk"] = span["client"] = None
            span["context"] = span["host"] = span["token"] = span["pool"] = None

    def close(self):
        """Remove this owner's hooks without touching any other hook or client."""
        self.active = False
        for client, hook in tuple(self._clients.items()):
            try:
                hooks = client.event_hooks
                if type(hooks) is dict and type(hooks.get("request")) is list:
                    hooks["request"] = [item for item in hooks["request"] if item is not hook]
            except Exception:
                pass
        self._clients.clear()
        self._visions.clear()
