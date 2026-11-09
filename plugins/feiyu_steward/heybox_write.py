"""Adapt the installed plugin to the current web comment-create contract.

Protocol reference: k1m0206/better-XiaoHeiHe src/shared/workshop-signing.js
and src/content/api-signing.js. Python implementation is independent.
"""

import asyncio
import hashlib
import hmac
import importlib
import re

import httpx


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


def install_comment_compat(plugin):
    """Adapt native transport, comment groups and signing; restore on termination."""
    package = plugin.__class__.__module__.rsplit(".", 1)[0] + ".xiaoheihe"
    signing = importlib.import_module(package + ".request_signing")
    endpoints = importlib.import_module(package + ".endpoints")
    api_client = importlib.import_module(package + ".api_client")
    original_start = api_client.XiaoheiheApiClient.start

    async def direct_start(client):
        # This local account's API is reachable directly. An inherited desktop
        # proxy must not control this directly reachable service's transport.
        # Keep injected clients intact and retain native timeouts/cookie handling.
        if client._owns_client and not client._closed:
            if not hasattr(client, "_feiyu_transport_lock"):
                client._feiyu_transport_lock = asyncio.Lock()
            async with client._feiyu_transport_lock:
                if not getattr(client, "_feiyu_direct_transport", False):
                    if client._client is not None:
                        await client._client.aclose()
                    client._client = httpx.AsyncClient(
                        base_url=client._base_url,
                        timeout=client._timeout,
                        trust_env=False,
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
                    client._feiyu_direct_transport = True
        await original_start(client)

    api_client.XiaoheiheApiClient.start = direct_start
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
        # Public web-client protocol constant, not an account credential.
        key = b"Z7mFG4tQp9Ws2LxB8H"
        message = key + f"{params['nonce']}{params['_time']}:{params['nonce']}".encode()
        params["_rnd"] = "15:" + hmac.new(key, message, hashlib.sha256).hexdigest()
        return signing.SignedRequest(params=params, headers=signed.headers)

    signing.RequestSigner.sign = sign
    endpoints.ENDPOINTS[name] = endpoints.Endpoint(
        "POST", "/bbs/app/comment/create", True, False, endpoints.API_BASE_URL
    )

    def restore():
        if api_client.XiaoheiheApiClient.start is direct_start:
            api_client.XiaoheiheApiClient.start = original_start
        if signing.RequestSigner.sign is sign:
            signing.RequestSigner.sign = original
            endpoints.ENDPOINTS[name] = original_endpoint
        if api_client.parse_thread_context is parse_thread:
            api_client.parse_thread_context = original_parse

    return restore
