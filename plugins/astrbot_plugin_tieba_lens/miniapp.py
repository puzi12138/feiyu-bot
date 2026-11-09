"""Resolve explicit Tieba mini-program shares through the logged-in QQ client.

QQ's HTTP landing page contains a QR code rather than the underlying page path.
NapCat / SnowLuma expose the read-only GetAppInfoByLink query via send_packet.
Only this fixed command and a validated QQ share URL can leave this module.
"""
from __future__ import annotations

import asyncio
import re

from .links import parse_miniapp_path, parse_miniapp_share_url
from .vendor.proto import BYTES, VARINT, Message, encode_bytes, encode_uint

COMMAND = "LightAppSvc.mini_app_info.GetAppInfoByLink"
TIEBA_APP_ID = "wx7a2de562948f667b"
TIMEOUT = 12
MAX_RESPONSE = 256 * 1024


def _field(message: Message, number: int, wire: int, default=None):
    values = message.fields.get(number, ())
    if not values:
        return default
    if len(values) != 1 or values[0][0] != wire:
        raise ValueError("小程序查询响应字段异常，请改发原帖链接。")
    return values[0][1]


def decode_target(response: str) -> dict:
    """Decode the bounded QWeb response and verify the app before reading its path."""
    if (not isinstance(response, str) or not response or len(response) > MAX_RESPONSE * 2
            or len(response) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", response)):
        raise ValueError("QQ 未返回有效的小程序查询结果，请改发原帖链接。")
    try:
        raw = bytes.fromhex(response)
        # Some compatible backends retain the exact WUP length prefix.
        if len(raw) >= 4 and int.from_bytes(raw[:4], "big") == len(raw):
            raw = raw[4:]
        outer = Message.load(raw)
        if _field(outer, 2, VARINT, 0) != 0:
            raise ValueError("QQ 小程序短链查询失败，请重新分享或改发原帖链接。")
        body = Message.load(_field(outer, 4, BYTES, b""))
        app = Message.load(_field(body, 2, BYTES, b""))
        if _field(app, 1, BYTES, b"") != TIEBA_APP_ID.encode():
            raise ValueError("小程序查询结果不是贴吧，已停止解析。")
        first = Message.load(_field(app, 11, BYTES, b""))
        path = _field(first, 1, BYTES, b"").decode("utf-8")
        target = parse_miniapp_path(path)
    except UnicodeError:
        raise ValueError("小程序页面地址无效，请改发原帖链接。") from None
    if target is None:
        raise ValueError("小程序未返回可识别的贴吧地址，请改发原帖链接。")
    return target


async def resolve_target(target: dict, event) -> dict:
    """Resolve a queued mini-program card; ordinary links need no QQ query."""
    if target.get("kind") != "miniapp":
        return target
    share = parse_miniapp_share_url(target.get("url"))
    if share is None or share != target:
        raise ValueError("小程序分享地址无效，请改发原帖链接。")
    action = getattr(getattr(event, "bot", None), "call_action", None)
    if not callable(action):
        raise ValueError("当前接入端无法查询 QQ 小程序短链，请改发原帖链接。")  # noqa: TRY004
    # QWebReq: sequence, client version, StGetAppInfoByLinkReq(link, type).
    body = encode_bytes(2, share["url"]) + encode_uint(3, 0)
    request = (encode_uint(1, 1) + encode_bytes(2, "V1_PC_MINISDK_99.99.99_1_APP_A")
               + encode_bytes(4, body))
    try:
        response = await asyncio.wait_for(action("send_packet", cmd=COMMAND,
            data=request.hex(), rsp=True), timeout=TIMEOUT)
    except TimeoutError:
        raise ValueError("QQ 小程序短链查询超时，请稍后重试或改发原帖链接。") from None
    except Exception:  # noqa: BLE001 -- connectors expose different exception classes
        raise ValueError("QQ 接入端无法查询小程序短链，请检查接入端版本及发包能力，或改发原帖链接。") from None
    # aiocqhttp returns the action's data, not the OneBot HTTP envelope.
    return decode_target(response)
