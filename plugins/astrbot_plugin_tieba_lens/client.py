"""Read-only, bounded Tieba content provider, independent of the AstrBot SDK.

Wire request packing includes MIT-licensed adaptations; see vendor/NOTICE.md
for attribution. All public output is JSON serialisable. Missing forum
levels stay None, mixed fragments retain wire order, fetch failures raise.
"""
from __future__ import annotations

import asyncio
import html
import inspect
import ipaddress
import json
import re
import socket
import time
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .links import parse_url
from .vendor import protocol as p
from .vendor.proto import Message, ProtobufError

_NETWORK_HOSTS = frozenset({"tiebac.baidu.com", "tieba.baidu.com", "m.tieba.baidu.com",
                            "wapp.tieba.baidu.com", "tb.baidu.com"})
_API_PATHS = frozenset({"/c/f/pb/page", "/c/f/frs/page", "/c/u/user/profile"})
_BLOCK_STATUSES = frozenset({403, 429, 456, 503})
_BLOCK_MARKERS = ("百度安全验证", "security_verify", "wappass.baidu.com", "访问过于频繁")
_MEDIA_HOSTS = frozenset({"tb.himg.baidu.com", "himg.baidu.com", "himg.bdimg.com",
                          "imgsrc.baidu.com", "tiebapic.baidu.com", "tb2.bdstatic.com",
                          "tb1.bdstatic.com", "gss0.bdstatic.com", "gss3.bdstatic.com"})
_MAX_RESPONSE = 8 * 1024 * 1024
_BLOCK_SECONDS = 900
_SHARED_BLOCK_UNTIL = 0.0
_SHARED_LAST_START = 0.0
_RATE_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_MISSING_LIKES_WARNING = "部分回复接口未提供点赞数，已排在可比较的回复后"


class TiebaError(RuntimeError):
    """A meaningful provider failure; contains no raw response or credentials."""
    def __init__(self, message: str, *, code="fetch_failed", retry_after=0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class TiebaBlockedError(TiebaError):
    pass


def _clamp(options, key, default, lo, hi, *, floating=False):
    try:
        value = float(options.get(key, default)) if floating else int(options.get(key, default))
        if floating and not (-float("inf") < value < float("inf")):
            return default
        return min(hi, max(lo, value))
    except (ValueError, TypeError, OverflowError):
        return default


def _utc(value=None):
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat() if value else ""
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _optional(msg: Message, field: int):
    return msg.uint(field) if any(wire == 0 for wire, _ in msg.fields.get(field, ())) else None


def _safe_media(value: str) -> str:
    value = html.unescape(value.strip())
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("http://"):
        value = "https://" + value[7:]
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.hostname not in _MEDIA_HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port not in {None, 443} or "\\" in value
                or len(value) > 2048 or any(ord(c) < 33 for c in value)):
            return ""
    except ValueError:
        return ""
    return value


def _avatar(portrait: str) -> str:
    if "://" in portrait or portrait.startswith("//"):
        return _safe_media(portrait).replace("https://tb.himg.baidu.com/sys/portrait", "https://himg.bdimg.com/sys/portrait", 1)
    portrait = portrait.split("?", 1)[0]
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,192}", portrait):
        return "https://himg.bdimg.com/sys/portrait/item/" + portrait
    return ""


def _author(user: Message | None = None, uid=0) -> dict:
    user = user or Message()
    return {"id": str(user.uint(p.USER_ID) or uid or ""),
            "name": user.text(p.USER_NAME_SHOW).strip() or user.text(p.USER_NAME).strip(),
            "avatar": _avatar(user.text(p.USER_PORTRAIT)),
            "level": _optional(user, p.USER_LEVEL)}


def _segments(parts: list[Message]) -> list[dict]:
    result = []
    for part in parts:
        kind = part.uint(p.PC_TYPE)
        text = part.text(p.PC_TEXT)
        link = part.text(p.PC_LINK)
        if kind in {0, 4, 9, 18, 27}:
            result.append({"type": "text", "text": text, "url": "", "alt": ""})
        elif kind == 1:
            # Links are displayed as content, never fetched by this provider.
            safe = link if _display_link(link) else ""
            result.append({"type": "link", "text": text or link, "url": safe, "alt": ""})
        elif kind in {2, 11}:
            name = part.text(p.PC_EMOJI_NAME).strip() or text.strip() or "表情"
            src = _safe_media(part.text(p.PC_SRC) or link)
            if not src and re.fullmatch(r"image_emoticon[0-9]{1,4}", text):
                src = f"https://tb2.bdstatic.com/tb/editor/images/client/{text}.png"
            result.append({"type": "emoji", "text": f"[{name}]", "url": src, "alt": name})
        elif kind in {3, 20}:
            src = next((u for f in (25, 9, 8, 4) if (u := _safe_media(part.text(f)))), "")
            result.append({"type": "image", "text": "", "url": src, "alt": "帖子配图"})
        elif kind == 5:
            result.append({"type": "video", "text": text or "[视频]",
                           "url": link if _display_link(link) else "", "alt": "视频"})
        elif kind == 10:
            result.append({"type": "text", "text": "[语音]", "url": "", "alt": "语音"})
        else:
            # Keep readable text for an unknown fragment instead of dropping it.
            result.append({"type": "text", "text": text or f"[未支持的内容类型 {kind}]",
                           "url": "", "alt": ""})
    return result


def _display_link(value: str) -> bool:
    if not value or len(value) > 2048 or "\\" in value or any(ord(c) < 33 for c in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme in {"http", "https"} and bool(parts.hostname)
                and parts.username is None and parts.password is None)
    except ValueError:
        return False


def _agree(msg: Message, field: int):
    return _optional(msg.message(field), 1) if msg.has(field) else None


def sort_document(document: dict, options: dict | None = None) -> dict:
    """Sort replies in place using real likes; safe to call again on cache copies.

    Floor 1 is pinned, including when an old cache put it elsewhere. Known
    likes sort descending and ties use real floors, then stable source order.
    Unknown likes remain unknown and retain source order behind known values.
    Explicit latest/default modes preserve the interface's existing order.
    This helper does not truncate content or change pagination metadata.
    """
    hot = (options or {}).get("sort", "hot") == "hot"
    missing_likes = False

    def known(post):
        likes = post.get("likes")
        return (bool(post.get("likes_available", likes is not None))
                and isinstance(likes, int) and not isinstance(likes, bool) and likes >= 0)

    def ranked(posts):
        nonlocal missing_likes
        if not hot:
            return posts
        missing_likes |= any(not known(post) for post in posts)

        def key(item):
            index, post = item
            if not known(post):
                return (1, 0, 0, index)
            floor = post.get("floor")
            real_floor = (isinstance(floor, int) and not isinstance(floor, bool)
                          and floor > 0 and not post.get("floor_inferred"))
            return (0, -post["likes"], floor if real_floor else float("inf"), index)

        return [post for _, post in sorted(enumerate(posts), key=key)]

    def prepare(post):
        available = known(post)
        post["likes_available"] = available
        if not available:
            # Older cached documents used 0 even when their flag was false.
            post["likes"] = None
        comments = post.get("comments") or []
        for comment in comments:
            prepare(comment)
        if comments:
            post["comments"] = ranked(comments)

    posts = document.get("posts") or []
    for post in posts:
        prepare(post)
    if hot and posts:
        first = next((index for index, post in enumerate(posts) if post.get("floor") == 1), 0)
        document["posts"] = [posts[first], *ranked(posts[:first] + posts[first + 1:])]
    if missing_likes:
        warnings = document.setdefault("warnings", [])
        if _MISSING_LIKES_WARNING not in warnings:
            warnings.append(_MISSING_LIKES_WARNING)
    return document


def _data(raw: bytes) -> Message:
    if not raw:
        raise TiebaError("贴吧返回了空响应", code="empty_response")
    try:
        msg = Message.load(raw)
        error = msg.message(1)
        errno = error.uint(1)
        if errno:
            # Deliberately do not echo arbitrary server errmsg, URLs or headers.
            raise TiebaError(f"贴吧接口拒绝了读取请求（错误码 {errno}）", code="api_error")
        data = msg.message(2)
        if not data:
            raise TiebaError("贴吧响应缺少内容数据，可能已删除或接口发生变化", code="missing_data")
        return data
    except ProtobufError as exc:
        raise TiebaError("贴吧响应格式无法识别，可能触发了验证或接口发生变化", code="invalid_response") from exc


def _users(data: Message, field: int):
    return {user.uint(p.USER_ID): user for user in data.messages(field) if user.uint(p.USER_ID)}


def _post(post: Message, users: dict, op_id: str, *, seq=0, comment_limit=6,
          collect_all_comments=False) -> dict:
    uid = post.uint(p.POST_AUTHOR_ID)
    author = _author(users.get(uid), uid)
    real_floor = post.uint(p.POST_FLOOR)
    likes = _agree(post, p.POST_AGREE)
    result = {"id": str(post.uint(p.POST_ID) or ""), "floor": real_floor or seq,
              "floor_inferred": not bool(real_floor), "author": author,
              "is_op": bool(op_id and author["id"] == op_id), "time": _utc(post.uint(p.POST_TIME)),
              "likes": likes, "likes_available": likes is not None,
              "segments": _segments(post.messages(p.POST_CONTENT)), "comments": [],
              "comment_total": post.uint(p.POST_SUB_POST_NUMBER)}
    seen = set()
    if comment_limit:
        for wrapper in post.messages(p.POST_SUB_POST_LIST):
            for sub in wrapper.messages(p.SUB_LIST):
                cid = str(sub.uint(p.SUBL_ID) or "")
                if cid and cid in seen:
                    continue
                seen.add(cid)
                author_id = sub.uint(p.SUBL_AUTHOR_ID)
                user = users.get(author_id) or sub.message(7)
                subauthor = _author(user, author_id)
                content = _segments(sub.messages(p.SUBL_SHELL))
                if not cid and not content:
                    continue
                sublikes = _agree(sub, p.SUBL_AGREE)
                result["comments"].append({
                    "id": cid, "floor": sub.uint(p.SUBL_FLOOR), "author": subauthor,
                    "is_op": bool(op_id and subauthor["id"] == op_id),
                    "time": _utc(sub.uint(p.SUBL_TIME)), "likes": sublikes,
                    "likes_available": sublikes is not None, "segments": content,
                    "comments": [], "comment_total": 0,
                })
                if not collect_all_comments and len(result["comments"]) >= comment_limit:
                    return result
    return result


def _summary(thread: Message, users: dict) -> dict:
    tid = str(thread.uint(p.TH_ID) or "")
    uid = thread.uint(p.TH_AUTHOR_ID)
    user = users.get(uid) or thread.message(p.TH_AUTHOR)
    content = _segments(thread.messages(142))
    return {"id": tid, "title": thread.text(p.TH_TITLE), "url": f"https://tieba.baidu.com/p/{tid}",
            "author": _author(user, uid), "excerpt": "".join(s["text"] for s in content if s["text"]),
            "images": [s["url"] for s in content if s["type"] == "image" and s["url"]],
            "replies": _optional(thread, p.TH_REPLY_NUM), "likes": _agree(thread, p.TH_AGREE),
            "time": _utc(thread.uint(p.TH_CREATE_TIME)), "is_top": bool(thread.uint(p.TH_IS_TOP)),
            "is_good": bool(thread.uint(p.TH_IS_GOOD))}


def _document(target: dict) -> dict:
    return {"kind": target["kind"], "id": target["id"], "title": "", "url": target["url"],
            "forum": "", "description": "", "avatar": "", "author": _author(),
            "stats": {key: None for key in ("replies", "likes", "views", "members", "threads", "followers")},
            "posts": [], "threads": [], "warnings": [], "fetched_at": _now(),
            "pagination": {"fetched": 0, "total": None, "has_more": False}}


def _html_redirect(raw: bytes) -> str:
    """Read bounded static redirects; never execute remote JavaScript."""
    text = raw[:262144].decode("utf-8", "replace")

    class Meta(HTMLParser):
        target = ""

        def handle_starttag(self, tag, attrs):
            if tag.lower() != "meta" or self.target:
                return
            attrs = {key.lower(): value or "" for key, value in attrs}
            if attrs.get("http-equiv", "").lower() != "refresh":
                return
            match = re.fullmatch(r"\s*[0-9.]+\s*;\s*url\s*=\s*(.{1,2048})\s*", attrs.get("content", ""), re.I)
            if match:
                self.target = match.group(1).strip().strip("\"'")

    parser = Meta(convert_charrefs=True)
    try:
        parser.feed(text)
    except ValueError:
        return ""
    if parser.target:
        return parser.target
    match = re.search(
        r"(?:window\.)?location(?:\.href|\.replace|\.assign)?\s*(?:=|\()\s*"
        r"(?P<quote>[\"'])(?P<target>[^\"'\r\n]{1,2048})(?P=quote)", text, re.I)
    if not match:
        return ""
    value = match.group("target")
    if match.group("quote") == '"':
        try:
            value = json.loads('"' + value + '"')
        except ValueError:
            return ""
    else:
        value = value.replace("\\/", "/")
    return html.unescape(value)


async def _resolve_public(host: str, timeout=10) -> str:
    """Resolve once, validate every answer, then pin the selected public IP."""
    if host not in _NETWORK_HOSTS:
        raise TiebaError("已拒绝访问非贴吧服务器", code="unsafe_url")
    try:
        rows = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise TiebaError("无法解析贴吧服务器地址", code="dns_failed") from exc
    addresses = []
    for row in rows:
        try:
            address = ipaddress.ip_address(row[4][0])
        except ValueError as exc:
            raise TiebaError("贴吧服务器地址无效", code="unsafe_dns") from exc
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise TiebaError("已拒绝解析到私有或保留网络的地址", code="unsafe_dns")
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise TiebaError("贴吧服务器未返回可用地址", code="dns_failed")
    addresses.sort(key=lambda ip: ipaddress.ip_address(ip).version)
    return addresses[0]


class _PinnedTransport(httpx.AsyncBaseTransport):
    """Connect to the checked IP while retaining original Host and TLS SNI.

    Validation without pinning leaves a DNS-rebinding race between lookup and
    connection. Separate per-host pools also keep SNI identities isolated.
    """
    def __init__(self):
        self._transports = {}

    async def handle_async_request(self, request):
        host = request.url.host
        if (request.url.scheme != "https" or host not in _NETWORK_HOSTS
                or request.url.port not in {None, 443}):
            raise TiebaError("已拒绝不安全的网络地址", code="unsafe_url")
        address = await _resolve_public(host)
        transport = self._transports.get(host)
        if transport is None:
            transport = self._transports[host] = httpx.AsyncHTTPTransport(
                retries=0, limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))
        headers = request.headers.copy()
        headers["Host"] = host
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = host
        pinned = httpx.Request(request.method, request.url.copy_with(host=address),
                               headers=headers, stream=request.stream, extensions=extensions)
        return await transport.handle_async_request(pinned)

    async def aclose(self):
        for transport in self._transports.values():
            await transport.aclose()
        self._transports.clear()


class TiebaClient:
    def __init__(self, options: dict):
        self.options = dict(options)
        self._http = httpx.AsyncClient(transport=_PinnedTransport(), follow_redirects=False,
                                      trust_env=False, timeout=20)
        self._closed = False
        self._counts = {"requests": 0, "retries": 0, "success": 0, "failures": 0, "blocks": 0}

    async def close(self):
        if not self._closed:
            self._closed = True
            await self._http.aclose()

    def diagnostics(self):
        return {"provider": "Tieba public protobuf", "https_only": True, "dns_pinning": True,
                "cookie_set": bool(self.options.get("cookie")), "closed": self._closed,
                "cooldown_remaining": max(0, int(_SHARED_BLOCK_UNTIL - time.monotonic())),
                "max_response_bytes": _MAX_RESPONSE, "max_requests_per_parse": 18,
                "max_fetch_seconds": 180,
                "capabilities": {"thread": True, "forum": True, "user": "best_effort",
                                 "subcomments": True, "ordered_segments": True},
                "counts": dict(self._counts)}

    def _block(self):
        global _SHARED_BLOCK_UNTIL
        _SHARED_BLOCK_UNTIL = max(_SHARED_BLOCK_UNTIL, time.monotonic() + _BLOCK_SECONDS)
        self._counts["blocks"] += 1
        raise TiebaBlockedError("贴吧触发了安全验证，已进入 15 分钟冷却，请稍后重试",
                                code="blocked", retry_after=_BLOCK_SECONDS)

    def _check_block(self):
        left = _SHARED_BLOCK_UNTIL - time.monotonic()
        if left > 0:
            raise TiebaBlockedError(f"贴吧安全验证冷却中（约剩 {int(left) + 1} 秒）",
                                    code="cooldown", retry_after=int(left) + 1)

    async def _pace(self, options):
        global _SHARED_LAST_START
        loop = asyncio.get_running_loop()
        lock = _RATE_LOCKS.get(loop)
        if lock is None:
            lock = _RATE_LOCKS[loop] = asyncio.Lock()
        async with lock:
            self._check_block()
            wait = _SHARED_LAST_START + _clamp(options, "request_delay", 1.0, .1, 30, floating=True) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._check_block()
            _SHARED_LAST_START = time.monotonic()

    @asynccontextmanager
    async def _stream(self, method, url, *, content, headers, timeout):
        # build_request also applies httpx's CookieJar. Remove those cookies and
        # restore only our explicit per-host cookie. This prevents an API's
        # Domain=.baidu.com Set-Cookie from leaking on a later short-link fetch.
        request = self._http.build_request(method, url, content=content, headers=headers, timeout=timeout)
        request.headers.pop("Cookie", None)
        if headers.get("Cookie"):
            request.headers["Cookie"] = headers["Cookie"]
        response = await self._http.send(request, stream=True, follow_redirects=False)
        try:
            yield response
        finally:
            await response.aclose()
            self._http.cookies.clear()

    async def _request(self, method, url, options, budget, *, content=None, headers=None):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in _NETWORK_HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port not in {None, 443} or "\\" in url):
            raise TiebaError("已拒绝不安全的网络地址", code="unsafe_url")
        if parts.hostname == "tiebac.baidu.com":
            if parts.path not in _API_PATHS or method != "POST":
                raise TiebaError("已拒绝非只读内容接口", code="unsafe_url")
        elif method != "GET" or not parse_url(url):
            raise TiebaError("已拒绝非贴吧内容地址", code="unsafe_url")
        request_headers = dict(headers or {})
        cookie = self.options.get("cookie", "")
        if (isinstance(cookie, str) and cookie and len(cookie) <= 16384 and "\r" not in cookie
                and "\n" not in cookie and parts.hostname == "tiebac.baidu.com"):
            request_headers["Cookie"] = cookie
        retries = _clamp(options, "retries", 1, 0, 3)
        for attempt in range(retries + 1):
            if budget["requests"] >= 18:
                raise TiebaError("已达到本次读取的请求预算，请减少评论数量后重试", code="request_budget")
            await self._pace(options)
            budget["requests"] += 1
            self._counts["requests"] += 1
            try:
                timeout = _clamp(options, "request_timeout", 20, 3, 60, floating=True)
                remaining = budget.get("deadline", time.monotonic() + 180) - time.monotonic()
                if remaining <= 0:
                    raise TiebaError("已达到本次读取的时间预算，请减少评论数量后重试", code="time_budget")
                timeout = min(timeout, remaining)
                # httpx's read timeout is per chunk. A wall-clock timeout also
                # bounds a server that keeps streaming tiny chunks indefinitely.
                async with asyncio.timeout(timeout), self._stream(
                        method, url, content=content, headers=request_headers, timeout=timeout) as response:
                    if response.status_code in _BLOCK_STATUSES:
                        self._block()
                    if response.status_code >= 500:
                        raise httpx.HTTPStatusError("贴吧暂时不可用", request=response.request, response=response)
                    if response.status_code >= 400:
                        raise TiebaError(f"贴吧内容读取失败（HTTP {response.status_code}）", code="http_error")
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _MAX_RESPONSE:
                            raise TiebaError("贴吧响应超过单次大小限制", code="response_budget")
                    raw = bytes(buf)
                    if raw.lstrip().startswith((b"<", b"{")):
                        snippet = raw[:200000].decode("utf-8", "replace")
                        if any(marker in snippet for marker in _BLOCK_MARKERS):
                            self._block()
                    return response.status_code, response.headers, raw
            except (httpx.TransportError, httpx.HTTPStatusError, TimeoutError) as exc:
                if attempt >= retries:
                    raise TiebaError("连接贴吧服务器失败或超时，请稍后重试", code="network_error") from exc
                self._counts["retries"] += 1
                await asyncio.sleep(min(4, 2 ** attempt))
        raise TiebaError("贴吧请求未完成", code="network_error")

    async def _proto(self, url, cmd, payload, options, budget):
        body, content_type = p.multipart_body(payload)
        headers = p.proto_headers()
        headers["Content-Type"] = content_type
        status, _, raw = await self._request("POST", f"{url}?cmd={cmd}", options, budget,
                                              content=body, headers=headers)
        if status != 200:
            raise TiebaError("贴吧客户端接口返回了意外跳转，已停止读取", code="unexpected_redirect")
        return _data(raw)

    async def _resolve_short(self, target, options, budget):
        url = target["url"]
        seen = set()
        for _ in range(5):
            if url in seen:
                raise TiebaError("贴吧短链接出现循环跳转", code="redirect_loop")
            seen.add(url)
            status, headers, raw = await self._request("GET", url, options, budget,
                headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Mobile Safari/537.36"})
            location = ""
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location", "")
            elif status == 200:
                # Some public share endpoints use static HTML/JS redirects.
                location = _html_redirect(raw)
            if not location:
                raise TiebaError("贴吧短链接未返回可识别的帖子或主页地址", code="unresolved_shortlink")
            location = urljoin(url, location)
            # Original HTTP links are canonicalised on input. Redirects are
            # strictly HTTPS and revalidated before any follow-up network call.
            if urlsplit(location).scheme != "https":
                raise TiebaError("贴吧短链接跳转不是 HTTPS，已停止读取", code="unsafe_redirect")
            parsed = parse_url(location)
            if not parsed:
                raise TiebaError("贴吧短链接跳转到非允许的内容地址，已停止读取", code="unsafe_redirect")
            if not parsed.get("is_short"):
                return parsed
            url = parsed["url"]
        raise TiebaError("贴吧短链接超过最大跳转次数", code="redirect_budget")

    async def parse(self, target: dict, options: dict | None = None, progress=None) -> dict:
        if self._closed:
            raise TiebaError("贴吧客户端已关闭", code="closed")
        options = {**self.options, **(options or {})}
        options.setdefault("sort", "hot")
        normal = parse_url(target.get("url", "")) if isinstance(target, dict) else None
        if normal is None or (target.get("kind") and normal["kind"] != target["kind"]):
            raise TiebaError("无法识别贴吧帖子或主页链接", code="invalid_target")
        if str(target.get("id", normal["id"])) != normal["id"]:
            raise TiebaError("贴吧链接与内容编号不一致", code="invalid_target")
        budget = {"requests": 0, "deadline": time.monotonic() + 180}

        async def report(stage, value):
            if progress is not None:
                result = progress(stage, value)
                if inspect.isawaitable(result):
                    await result

        try:
            await report("识别贴吧链接", 10)
            self._check_block()
            if normal.get("is_short"):
                normal = await self._resolve_short(normal, options, budget)
                await report("短链接已解析", 15)
            if normal["kind"] == "thread":
                document = await self._thread(normal, options, budget, report)
            elif normal["kind"] == "forum":
                document = await self._forum(normal, options, budget, report)
            else:
                document = await self._user(normal, options, budget, report)
            self._apply_options(document, options)
            self._counts["success"] += 1
            await report("贴吧内容读取完成", 55)
            return document
        except asyncio.CancelledError:
            raise
        except (ProtobufError, ValueError, KeyError, TypeError) as exc:
            self._counts["failures"] += 1
            raise TiebaError("贴吧数据结构无法识别，未将本次读取记为空内容", code="invalid_response") from exc
        except Exception:
            self._counts["failures"] += 1
            raise

    async def _thread(self, target, options, budget, report):
        doc = _document(target)
        limit = 1 + _clamp(options, "comment_limit", 12, 0, 100)
        sublimit = _clamp(options, "subcomment_limit", 6, 0, 20)
        sort = {"default": 0, "latest": 1, "hot": 2}.get(options.get("sort"), 0)
        hot = sort == 2
        # Always load the real first floor in chronological mode. Sorted reply
        # requests may omit it, so only use them for replies after this request.
        first = await self._proto(p.PAGE_URL, p.PAGE_CMD, p.build_page_request(
            target["id"], page=1, rn=30, with_floor=bool(sublimit), floor_rn=sublimit,
            sort=0, only_author=bool(options.get("only_author"))), options, budget)
        thread = first.message(p.PAGE_THREAD)
        rows = first.messages(p.PAGE_POST_LIST)
        if not thread or not rows:
            raise TiebaError("帖子不存在、已删除、无权读取或没有可识别的首楼", code="thread_unavailable")
        returned_id = str(thread.uint(p.TH_ID) or "")
        if returned_id and returned_id != target["id"]:
            raise TiebaError("贴吧返回的帖子编号不符", code="mismatched_thread")
        users = _users(first, p.PAGE_USER_LIST)
        op_id = str(thread.uint(p.TH_AUTHOR_ID) or thread.message(p.TH_AUTHOR).uint(p.USER_ID) or "")
        first_pid = thread.uint(40)
        op_post = next((row for row in rows if row.uint(p.POST_FLOOR) == 1
                        or first_pid and row.uint(p.POST_ID) == first_pid), None)
        if op_post is None:
            if rows[0].uint(p.POST_FLOOR) > 1 or first_pid and rows[0].uint(p.POST_ID) != first_pid:
                raise TiebaError("帖子首楼已删除、隐藏或未由接口返回，无法完整解析该帖", code="first_floor_unavailable")
            op_post = rows[0]
            doc["warnings"].append("接口未返回首楼编号，首楼按时间顺序请求的第一条内容展示。")
        if not op_id:
            op_id = str(op_post.uint(p.POST_AUTHOR_ID) or "")
        doc["title"] = thread.text(p.TH_TITLE) or op_post.text(p.POST_TITLE)
        doc["forum"] = first.message(p.PAGE_FORUM).text(2) or thread.text(28)
        doc["author"] = _author(users.get(int(op_id or 0)) or thread.message(p.TH_AUTHOR), op_id)
        doc["avatar"] = doc["author"]["avatar"]
        doc["stats"].update({"replies": _optional(thread, p.TH_REPLY_NUM), "likes": _agree(thread, p.TH_AGREE),
                               "views": _optional(thread, p.TH_VIEW_NUM)})
        doc["posts"] = [_post(op_post, users, op_id, seq=1, comment_limit=sublimit,
                              collect_all_comments=hot)]
        doc["posts"][0]["floor"] = 1
        # First floor is established by the chronological request, even if the
        # legacy wire omitted its floor field. Other inferred floors are marked.
        seen = {doc["posts"][0]["id"]}
        page = 1
        current = first
        if sort and limit > 1:
            current = await self._proto(p.PAGE_URL, p.PAGE_CMD, p.build_page_request(
                target["id"], page=1, rn=30, with_floor=bool(sublimit), floor_rn=sublimit,
                sort=sort, only_author=bool(options.get("only_author"))), options, budget)
        while len(doc["posts"]) < limit:
            users = _users(current, p.PAGE_USER_LIST)
            rows = current.messages(p.PAGE_POST_LIST)
            new_rows = 0
            for row in rows:
                pid = str(row.uint(p.POST_ID) or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                new_rows += 1
                post = _post(row, users, op_id, seq=len(doc["posts"]) + 1,
                             comment_limit=sublimit, collect_all_comments=hot)
                if options.get("only_author") and post["author"]["id"] != op_id:
                    continue
                doc["posts"].append(post)
                # A hot response is only a candidate batch, not proof that the
                # first N rows have the most actual likes. Read it completely.
                if not hot and len(doc["posts"]) >= limit:
                    break
            if len(doc["posts"]) >= limit or not new_rows or len(rows) < 30 or page >= 5:
                break
            page += 1
            await report(f"读取帖子第 {page} 页", min(50, 25 + page * 5))
            current = await self._proto(p.PAGE_URL, p.PAGE_CMD, p.build_page_request(
                target["id"], page=page, rn=30, with_floor=bool(sublimit), floor_rn=sublimit,
                sort=sort, only_author=bool(options.get("only_author"))), options, budget)
        sort_document(doc, options)
        omitted_candidates = max(0, len(doc["posts"]) - limit)
        doc["posts"] = doc["posts"][:limit]
        if hot:
            def limit_comments(post):
                post["comments"] = post["comments"][:sublimit]
                for comment in post["comments"]:
                    limit_comments(comment)

            for post in doc["posts"]:
                limit_comments(post)
        if any(post.get("floor_inferred") for post in doc["posts"][1:]):
            doc["warnings"].append("部分原始楼层号缺失，卡片楼层号按接口返回顺序标记；排序模式下仅代表展示序号。")
        missing_sub = sum(max(0, post["comment_total"] - len(post["comments"])) for post in doc["posts"])
        if missing_sub:
            doc["warnings"].append(f"有 {missing_sub} 条楼中楼因数量设置或接口返回范围未展开。")
        total = doc["stats"]["replies"]
        has_more = (omitted_candidates > 0
                    or total is not None and total > max(0, len(doc["posts"]) - 1)
                    or any(str(row.uint(p.POST_ID) or "") not in seen for row in rows)
                    or page >= 5 and len(rows) >= 30)
        if options.get("only_author"):
            has_more = omitted_candidates > 0 or len(doc["posts"]) >= limit
        doc["pagination"] = {"fetched": len(doc["posts"]), "total": total + 1 if total is not None else None,
                             "has_more": has_more}
        if has_more:
            doc["warnings"].append(f"本次展示首楼及 {len(doc['posts']) - 1} 条回复，其余内容可在原帖查看。")
        return doc

    async def _forum(self, target, options, budget, report):
        limit = _clamp(options, "forum_thread_limit", 10, 1, 30)
        sort = {"default": 0, "latest": 1, "hot": 3}.get(options.get("sort"), 0)
        data = await self._proto(p.FRS_URL, p.FRS_CMD, p.build_frs_request(
            target["id"], rn=30, sort=sort), options, budget)
        forum = data.message(p.FRS_FORUM)
        rows = data.messages(p.FRS_THREAD_LIST)
        if not forum or not forum.text(p.FRS_FORUM_NAME):
            raise TiebaError("贴吧主页不存在、无权读取或响应缺少吧信息", code="forum_unavailable")
        doc = _document(target)
        doc["title"] = doc["forum"] = forum.text(p.FRS_FORUM_NAME)
        doc["stats"].update({"members": _optional(forum, 9), "threads": _optional(forum, 10)})
        users = _users(data, p.FRS_USER_LIST)
        seen = set()
        for row in rows:
            item = _summary(row, users)
            if item["id"] and item["id"] not in seen:
                seen.add(item["id"])
                doc["threads"].append(item)
                if len(doc["threads"]) >= limit:
                    break
        if not rows and doc["stats"]["threads"] != 0:
            raise TiebaError("贴吧主页有帖子但接口未返回列表，未将本次读取记为空内容", code="missing_threads")
        doc["pagination"] = {"fetched": len(doc["threads"]), "total": doc["stats"]["threads"],
                             "has_more": (len(rows) > len(doc["threads"]) or len(rows) >= 30
                                 or (doc["stats"]["threads"] or 0) > len(doc["threads"]))}
        if options.get("sort") == "hot":
            doc["warnings"].append("热门排序由贴吧接口决定；没有热门分区的贴吧可能返回默认顺序。")
        if doc["pagination"]["has_more"]:
            doc["warnings"].append(f"本次展示 {len(doc['threads'])} 条主页帖子，其余内容可在贴吧查看。")
        await report("贴吧主页读取完成", 50)
        return doc

    async def _user(self, target, options, budget, report):
        if target["id"].startswith("name:"):
            raise TiebaError("此旧版用户名主页无法可靠映射，请使用带 id 的贴吧用户主页链接", code="unsupported_user")
        data = await self._proto(p.PROFILE_URL, p.PROFILE_CMD, p.build_profile_request(target["id"]), options, budget)
        user = data.message(1)
        if not user.uint(p.USER_ID) or not (user.text(p.USER_NAME_SHOW) or user.text(p.USER_NAME)):
            raise TiebaError("用户主页暂不可访问，可能需要登录或该主页编号不受公开接口支持", code="user_unavailable")
        doc = _document(target)
        doc["author"] = _author(user)
        # A profile has no forum-level context. Growth level is a separate field
        # and must not be presented as the user's forum level.
        doc["author"]["level"] = None
        doc["title"] = doc["author"]["name"] + "的贴吧主页"
        doc["avatar"] = doc["author"]["avatar"]
        doc["description"] = user.text(34)
        doc["stats"].update({"followers": _optional(user, 30), "threads": _optional(user, 37),
                               "likes": _optional(data.message(14), 1)})
        limit = _clamp(options, "forum_thread_limit", 10, 1, 30)
        rows = data.messages(4)
        for row in rows[:limit]:
            tid = str(row.uint(2) or "")
            if not tid:
                continue
            content = _segments(row.messages(49))
            images = [s["url"] for s in content if s["type"] == "image" and s["url"]]
            for media in row.messages(16):
                src = next((u for f in (15, 3, 4, 2) if (u := _safe_media(media.text(f)))), "")
                if src and src not in images:
                    images.append(src)
            doc["threads"].append({"id": tid, "title": row.text(7), "url": f"https://tieba.baidu.com/p/{tid}",
                "author": dict(doc["author"]), "excerpt": "".join(s["text"] for s in content if s["text"]),
                "images": images, "replies": _optional(row, 17), "likes": _agree(row, 40),
                "time": _utc(row.uint(5)), "is_top": False, "is_good": False, "forum": row.text(6)})
        # Profile post_num includes replies, not just threads. Don't claim it is
        # the total number of displayed topics or infer empty means no activity.
        doc["stats"]["threads"] = None
        doc["pagination"] = {"fetched": len(doc["threads"]), "total": None,
                             "has_more": len(rows) > len(doc["threads"])}
        doc["warnings"].append("用户主页仅展示公开接口返回的首批动态；隐藏、登录可见及历史动态可能不在其中。")
        await report("用户主页读取完成", 50)
        return doc

    @staticmethod
    def _apply_options(doc, options):
        image_limit = _clamp(options, "image_limit", 40, 0, 100)
        image_count = 0
        omitted = 0

        def apply(post):
            nonlocal image_count, omitted
            for segment in post["segments"]:
                if segment["type"] == "image":
                    if not options.get("include_images", True) or image_count >= image_limit:
                        segment["url"] = ""
                        segment["alt"] = "配图未展开（图片设置或数量限制）"
                        omitted += 1
                    else:
                        image_count += 1
                elif segment["type"] == "emoji" and not options.get("include_emoji", True):
                    segment["url"] = ""
            for comment in post["comments"]:
                apply(comment)

        for post in doc["posts"]:
            apply(post)
        for thread in doc["threads"]:
            images = thread["images"]
            remain = max(0, image_limit - image_count) if options.get("include_images", True) else 0
            thread["images"] = images[:remain]
            omitted += len(images) - len(thread["images"])
            image_count += len(thread["images"])
        if omitted:
            doc["warnings"].append(f"有 {omitted} 张配图因图片设置或数量限制未展开，图文位置已保留。")
