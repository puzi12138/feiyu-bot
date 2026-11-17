"""Bounded maintenance of the current model's existing proxy pool."""

import asyncio
import sys
import time
import weakref
from collections.abc import Mapping

import httpcore
import httpx
from httpcore._synchronization import AsyncShieldCancellation

from .heybox_write import _guard_heybox_proxy_transport, _is_abandoned_heybox_tunnel
from .incident_hooks import _current_heybox
from .private_review_model import _known_openai_provider


def _configured_provider(host):
    """Read only the current configured provider and its original map."""
    hooks = getattr(host, "_incident_hooks", None)
    if hooks is None or getattr(hooks, "host", None) is not host:
        return None
    plugin, runtime, _ = _current_heybox(hooks)
    module = sys.modules.get(getattr(type(plugin), "__module__", ""))
    main = getattr(module, "AUTO_MAIN_PROVIDER_ID", None)
    raw = getattr(getattr(runtime, "config", None), "raw", {})
    configured = raw.get("providers", {}) if isinstance(raw, Mapping) else {}
    manager = getattr(getattr(host, "context", None), "provider_manager", None)
    mapping = getattr(manager, "inst_map", None)
    for _ in range(2):
        if getattr(mapping, "_peak_runtime_provider_map", False) is not True:
            break
        mapping = getattr(mapping, "_original", None)
    if (getattr(mapping, "_peak_runtime_provider_map", False) is True
            or not isinstance(mapping, dict) or not isinstance(configured, Mapping)):
        return None
    settings = getattr(manager, "provider_settings", {})
    image = configured.get("image_provider_id") or (
        settings.get("default_image_caption_provider_id", "") if isinstance(settings, dict) else "")
    configured_main = configured.get("llm_provider_id") or getattr(manager, "default_chat_provider_id", "")
    if main != "deepseek-flash" or image != main or configured_main not in ("", main):
        return None
    provider = mapping.get(main)
    config = getattr(provider, "provider_config", None)
    if (not isinstance(config, dict) or config.get("id") != main
            or config.get("model") != main or config.get("enable", True) is not True):
        return None
    return provider


def _initial_pool_counts(host):
    hooks = getattr(host, "_incident_hooks", None)
    inspect = getattr(hooks, "_model_http_pool_status", None)
    if not callable(inspect):
        return None
    status = inspect()
    if not isinstance(status, dict) or status.get("observed") is not True:
        return None
    vision = status.get("vision")
    if (not isinstance(vision, dict) or vision.get("provider_present") is not True
            or vision.get("http_client_present") is not True
            or vision.get("mounted_unique_pool_count") != 1):
        return None
    mounted = vision.get("mounted_pools")
    if not isinstance(mounted, list) or len(mounted) != 1:
        return None
    row = mounted[0]
    if (not isinstance(row, dict) or row.get("observed") is not True
            or row.get("sample_truncated") is not False or row.get("abandoned_unknown") != 0):
        return None
    keys = ("max_connections", "connections", "requests", "active", "unconnected",
            "stream_closed", "unassigned_connections", "abandoned_eligible")
    if any(type(row.get(key)) is not int or row[key] < 0 for key in keys):
        return None
    return {key: row[key] for key in keys}


async def maintain_model_proxy_pool(host):
    """Guard future failed handshakes and close only proven abandoned tunnels.

    This preserves the model provider, SDK, HTTP client, all current requests,
    and their normal retry/timeout settings. It never performs a network request.
    """
    deadline = time.monotonic() + 1.0
    result = {"ready": False, "guarded": False, "examined": 0, "attempted": 0,
              "closed": 0, "skipped_failed": 0,
              "total_closed": 0, "last_closed_at": None, "initial": None}
    try:
        if getattr(host, "_group_provider_terminated", False):
            return result
        provider = _configured_provider(host)
        if not _known_openai_provider(provider):
            return result
        sdk = provider.client
        client = getattr(sdk, "_client", None)
        if type(client) is not httpx.AsyncClient or client.is_closed:
            return result
        transport = client._transport_for_url(httpx.URL("https://unit.invalid/"))
        pool = getattr(transport, "_pool", None)
        if type(pool) is not httpcore.AsyncHTTPProxy:
            return result
        default_pool = getattr(getattr(client, "_transport", None), "_pool", None)
        if type(client._mounts) is not dict or len(client._mounts) != 1 or default_pool is None:
            return result
        for current in (pool, default_pool):
            if (type(current._connections) is not list or len(current._connections) > 128
                    or type(current._requests) is not list or len(current._requests) > 16):
                return result
        factory = pool.create_connection
        existing = getattr(pool, "_feiyu_tunnel_guard_v1", None)
        if existing is None:
            if (getattr(factory, "__self__", None) is not pool
                    or getattr(factory, "__func__", None) is not httpcore.AsyncHTTPProxy.create_connection):
                return result
        elif factory is not existing:
            return result
        if (_configured_provider(host) is not provider or provider.client is not sdk
                or sdk._client is not client):
            return result
        previous = getattr(host, "_model_proxy_pool_status", {})
        owner = getattr(host, "_model_proxy_pool_owner", None)
        if (isinstance(owner, weakref.ReferenceType) and owner() is pool and isinstance(previous, dict)
                and isinstance(previous.get("initial"), dict)):
            result["initial"] = previous.get("initial")
        else:
            result["initial"] = _initial_pool_counts(host)
        if not isinstance(result["initial"], dict) or time.monotonic() >= deadline:
            return result
        if (_configured_provider(host) is not provider or provider.client is not sdk
                or sdk._client is not client or transport._pool is not pool):
            return result
        pool = _guard_heybox_proxy_transport(client)
        if transport._pool is not pool or _configured_provider(host) is not provider:
            return result
        installed_factory = pool.create_connection
        failures = getattr(pool, "_feiyu_model_tunnel_close_failures", None)
        if failures is None:
            failures = weakref.WeakSet()
            pool._feiyu_model_tunnel_close_failures = failures
        if not isinstance(failures, weakref.WeakSet):
            return result
        if isinstance(owner, weakref.ReferenceType) and owner() is pool and isinstance(previous, dict):
            total = previous.get("total_closed")
            last = previous.get("last_closed_at")
            if type(total) is int and 0 <= total <= 1_000_000:
                result["total_closed"] = total
            if type(last) in (int, float) and 0 < last <= time.time():
                result["last_closed_at"] = last
        host._model_proxy_pool_owner = weakref.ref(pool)
        result.update(ready=True, guarded=True)
        for connection in tuple(pool.connections):
            if result["attempted"] >= 8 or time.monotonic() >= deadline:
                break
            result["examined"] += 1
            # All checks and the start of aclose run without an intervening
            # await. A live or newly assigned tunnel can never be selected.
            if (getattr(host, "_group_provider_terminated", False)
                    or _configured_provider(host) is not provider
                    or provider.client is not sdk or sdk._client is not client
                    or client.is_closed
                    or client._transport_for_url(httpx.URL("https://unit.invalid/")) is not transport
                    or transport._pool is not pool or pool.create_connection is not installed_factory
                    or getattr(pool, "_feiyu_tunnel_guard_v1", None) is not installed_factory
                    or type(pool._connections) is not list or len(pool._connections) > 128
                    or type(pool._requests) is not list or len(pool._requests) > 16):
                result["ready"] = False
                break
            if not any(current is connection for current in pool._connections):
                continue
            if connection in failures:
                if _is_abandoned_heybox_tunnel(pool, connection):
                    result["skipped_failed"] += 1
                    result["ready"] = False
                continue
            if not _is_abandoned_heybox_tunnel(pool, connection):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result["attempted"] += 1
            try:
                with AsyncShieldCancellation():
                    async with asyncio.timeout(min(0.25, remaining)):
                        await connection.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                failures.add(connection)
                result["ready"] = False
                continue
            result["closed"] += 1
            result["total_closed"] += 1
            result["last_closed_at"] = time.time()
    except asyncio.CancelledError:
        raise
    except Exception:
        # Do not log provider configuration, request data, or exception text.
        result["ready"] = False
        return result
    return result
