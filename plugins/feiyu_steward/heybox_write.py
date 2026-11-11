"""Adapt the installed plugin to the current web comment-create contract.

Protocol reference: k1m0206/better-XiaoHeiHe src/shared/workshop-signing.js
and the current official website bundle. Python implementation is independent.
"""

import asyncio
import base64
import json
import secrets
import importlib
import re

import httpx



# Public protocol key from the current anonymous official website bundle.
# It identifies the web contract and is not an account credential.
_RND16_KEY = base64.b64decode("HFFSTsqmvG3uGxPNXDQW+1VARfqgG4nT6LS1PX7F8NQ=", validate=True)
_RND16_AAD = b"heybox-rnd:v16"


def generate_rnd16(timestamp, nonce, pathname, *, iv=None):
    """Independently encode the current web signature, without the ``16:`` prefix."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if type(timestamp) not in (int, str):
        raise ValueError("rnd16_time_invalid")
    stamp = str(timestamp)
    if not re.fullmatch(r"0|[1-9][0-9]*", stamp) or int(stamp) > 9007199254740991:
        raise ValueError("rnd16_time_invalid")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9A-F]{32}", nonce):
        raise ValueError("rnd16_nonce_invalid")
    if (not isinstance(pathname, str) or not pathname.startswith("/")
            or re.search(r"[\s?#\\]|[^\x21-\x7e]", pathname)):
        raise ValueError("rnd16_path_invalid")
    normalized_path = pathname if pathname.endswith("/") else pathname + "/"
    if iv is None:
        vector = secrets.token_bytes(12)
    elif isinstance(iv, (bytes, bytearray, memoryview)):
        vector = bytes(iv)
    else:
        raise ValueError("rnd16_iv_invalid")
    if len(vector) != 12:
        raise ValueError("rnd16_iv_invalid")
    plaintext = json.dumps([stamp, nonce, normalized_path], separators=(",", ":")).encode("utf-8")
    encrypted = AESGCM(_RND16_KEY).encrypt(vector, plaintext, _RND16_AAD)
    return base64.urlsafe_b64encode(vector + encrypted).decode("ascii").rstrip("=")


def normalize_comments(rows):
    """Unwrap native floor.comment arrays without losing root/reply identity."""
    result = []
    seen = set()

    def visit(value, root="", depth=0):
        if depth > 6 or len(result) >= 200:
            return
        if isinstance(value, list):
            for row in value[:200]:
                visit(row, root, depth + 1)
            return
        if not isinstance(value, dict):
            return
        if isinstance(value.get("root"), dict):
            first = value["root"]
            root = root or str(first.get("commentid", first.get("comment_id", "")))
        nested = value.get("comment")
        if isinstance(nested, (list, dict)):
            first = nested[0] if isinstance(nested, list) and nested else nested
            floor_id = (
                str(first.get("commentid", first.get("comment_id", "")))
                if isinstance(first, dict)
                else root
            )
            visit(nested, root or floor_id, depth + 1)
        elif any(
            key in value for key in ("text", "content", "commentid", "comment_id")
        ):
            row = dict(value)
            identifier = str(row.get("commentid", row.get("comment_id", "")))
            if identifier and identifier in seen:
                return
            if identifier:
                seen.add(identifier)
            row.setdefault("root_comment_id", root or identifier)
            row.setdefault(
                "parent_comment_id", str(row.get("replyid", row.get("reply_id", "")))
            )
            result.append(row)
            root = root or identifier
        for key in ("root", "replies", "children", "sub_comments"):
            if isinstance(value.get(key), (list, dict)):
                visit(value[key], root, depth + 1)

    visit(rows)
    return result


def _guard_heybox_proxy_transport(client):
    """Guard only this owned proxy pool; do not touch globally shared classes."""
    import httpcore
    from httpcore._async.http_proxy import AsyncTunnelHTTPConnection
    from httpcore._synchronization import AsyncShieldCancellation

    if (httpx.__version__, httpcore.__version__) != ("0.28.1", "1.0.9"):
        raise RuntimeError("heybox_tunnel_guard_unsupported_http_stack")
    if type(client) is not httpx.AsyncClient or client.is_closed or len(client._mounts) != 1:
        raise RuntimeError("heybox_tunnel_guard_client_mismatch")
    transport = client._transport_for_url(httpx.URL("https://unit.invalid/"))
    if (type(transport) is not httpx.AsyncHTTPTransport
            or transport is not client._transport_for_url(httpx.URL("http://unit.invalid/"))
            or type(transport._pool) is not httpcore.AsyncHTTPProxy):
        raise RuntimeError("heybox_tunnel_guard_transport_mismatch")
    pool = transport._pool
    origin = pool._proxy_url.origin
    if ((origin.scheme, origin.host, origin.port) != (b"http", b"127.0.0.1", 7897)
            or pool._http1 is not True or pool._http2 is not False):
        raise RuntimeError("heybox_tunnel_guard_proxy_mismatch")
    existing = getattr(pool, "_feiyu_tunnel_guard_v1", None)
    if existing is not None:
        if pool.create_connection is not existing:
            raise RuntimeError("heybox_tunnel_guard_changed")
        return pool

    original_factory = pool.create_connection
    def guarded_factory(origin):
        connection = original_factory(origin)
        if type(connection) is not AsyncTunnelHTTPConnection:
            return connection
        original_handle = connection.handle_async_request
        async def guarded_handle(request):
            try:
                return await original_handle(request)
            except BaseException:
                # The terminal request is dispatched only after _connected=True.
                # A failed CONNECT/TLS tunnel belongs exclusively to this call.
                if connection._connected is False and not connection.is_closed():
                    try:
                        with AsyncShieldCancellation():
                            async with asyncio.timeout(1.0):
                                await connection.aclose()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                raise
        connection.handle_async_request = guarded_handle
        return connection

    pool.create_connection = guarded_factory
    pool._feiyu_tunnel_guard_v1 = guarded_factory
    return pool


def _is_abandoned_heybox_tunnel(pool, connection):
    """Conservatively identify a released CONNECT-only HTTP/1.1 tunnel."""
    from anyio._backends._asyncio import SocketStream
    from httpcore._async.connection import AsyncHTTPConnection
    from httpcore._async.http11 import AsyncHTTP11Connection, HTTPConnectionState
    from httpcore._async.http_proxy import AsyncTunnelHTTPConnection
    from httpcore._backends.anyio import AnyIOStream

    def unlocked(lock):
        return (getattr(lock, "_backend", None) == "asyncio"
                and hasattr(lock, "_anyio_lock")
                and lock._anyio_lock.locked() is False)

    if (type(connection) is not AsyncTunnelHTTPConnection
            or connection._connected is not False
            or any(request.connection is connection for request in pool._requests)
            or not unlocked(connection._connect_lock)):
        return False
    proxy_connection = connection._connection
    if type(proxy_connection) is not AsyncHTTPConnection or not unlocked(proxy_connection._request_lock):
        return False
    http11_connection = proxy_connection._connection
    if (type(http11_connection) is not AsyncHTTP11Connection
            or http11_connection._state is not HTTPConnectionState.ACTIVE
            or not unlocked(http11_connection._state_lock)):
        return False
    network_stream = http11_connection._network_stream
    if type(network_stream) is not AnyIOStream:
        return False
    socket_stream = network_stream._stream
    return type(socket_stream) is SocketStream and getattr(socket_stream, "_closed", None) is True


async def _close_abandoned_heybox_tunnels(pool):
    """Close only rechecked, unassigned failed CONNECT tunnels; never replay."""
    from httpcore._synchronization import AsyncShieldCancellation
    for connection in tuple(pool.connections):
        # No await between this identity/state test and the close call.
        if _is_abandoned_heybox_tunnel(pool, connection):
            try:
                with AsyncShieldCancellation():
                    async with asyncio.timeout(1.0):
                        await connection.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass


def install_comment_compat(plugin):
    """Adapt native transport, comment groups and signing; restore on termination."""
    package = plugin.__class__.__module__.rsplit(".", 1)[0] + ".xiaoheihe"
    signing = importlib.import_module(package + ".request_signing")
    endpoints = importlib.import_module(package + ".endpoints")
    api_client = importlib.import_module(package + ".api_client")
    original_start = api_client.XiaoheiheApiClient.start

    async def proxy_start(client):
        # Pin only this adapter's owned client to the existing loopback proxy.
        # Never close or switch a live transport: an unknown in-flight write
        # must finish under its original client and remain protected.
        if client._owns_client and not client._closed:
            if not hasattr(client, "_feiyu_transport_lock"):
                client._feiyu_transport_lock = asyncio.Lock()
            async with client._feiyu_transport_lock:
                mode = getattr(client, "_feiyu_transport_mode", None)
                old_direct = getattr(client, "_feiyu_direct_transport", False)
                if mode == "existing_loopback_proxy_v1":
                    if (client._client is None or old_direct
                            or getattr(client._client, "is_closed", False) is True):
                        raise RuntimeError("heybox_transport_requires_controlled_rebuild")
                else:
                    if mode is not None or old_direct or client._client is not None:
                        raise RuntimeError("heybox_transport_requires_controlled_rebuild")
                    client._client = httpx.AsyncClient(
                        base_url=client._base_url,
                        timeout=client._timeout,
                        trust_env=False,
                        proxy="http://127.0.0.1:7897",
                        follow_redirects=False,
                        limits=httpx.Limits(
                            max_connections=10,
                            max_keepalive_connections=5,
                            keepalive_expiry=30,
                        ),
                        headers={
                            "Accept": "application/json",
                            "User-Agent": "AstrBot-Xiaoheihe-Adapter/1.1.3",
                        },
                    )
                    client._feiyu_transport_mode = "existing_loopback_proxy_v1"
                pool = _guard_heybox_proxy_transport(client._client)
                await _close_abandoned_heybox_tunnels(pool)
        await original_start(client)

    api_client.XiaoheiheApiClient.start = proxy_start
    original_parse = api_client.parse_thread_context

    def parse_thread(payload, post_id):
        # Reading accepts share aliases; posting requires the true numeric ID.
        body = payload.get("result", payload.get("data", payload))
        if isinstance(body, dict):
            for key in ("comments", "comment_list", "children"):
                if isinstance(body.get(key), list):
                    body = {**body, key: normalize_comments(body[key])}
                    payload = {**payload, "result": body}
                    break
        document = original_parse(payload, post_id)
        if isinstance(body, dict):
            post = body.get("post", body.get("link", body))
            if isinstance(post, dict):
                for key in ("link_id", "linkid", "post_id", "id"):
                    value = str(post.get(key, ""))
                    if re.fullmatch(r"\d{5,20}", value):
                        document.post_id = value
                        break
        return document

    api_client.parse_thread_context = parse_thread
    original = signing.RequestSigner.sign
    name = endpoints.EndpointName.CREATE_COMMENT
    original_endpoint = endpoints.ENDPOINTS[name]

    def sign(signer, method, path, **kwargs):
        signed = original(signer, method, path, **kwargs)
        if method != "POST" or path != "/bbs/app/comment/create":
            return signed
        params = {key: signed.params[key] for key in ("hkey", "_time", "nonce")}
        params.update(
            app="heybox",
            os_type="web",
            x_app="heybox_website",
            x_client_type="web",
            x_os_type="Windows",
            x_client_version="",
            client_type="web",
            web_version="3.0",
            version="999.0.4",
        )
        params["_rnd"] = "16:" + generate_rnd16(params["_time"], params["nonce"], path)
        return signing.SignedRequest(params=params, headers=signed.headers)

    signing.RequestSigner.sign = sign
    endpoints.ENDPOINTS[name] = endpoints.Endpoint(
        "POST", "/bbs/app/comment/create", True, False, endpoints.API_BASE_URL
    )

    def restore():
        if api_client.XiaoheiheApiClient.start is proxy_start:
            api_client.XiaoheiheApiClient.start = original_start
        if signing.RequestSigner.sign is sign:
            signing.RequestSigner.sign = original
            endpoints.ENDPOINTS[name] = original_endpoint
        if api_client.parse_thread_context is parse_thread:
            api_client.parse_thread_context = original_parse

    return restore
