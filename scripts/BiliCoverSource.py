"""Fetch bounded public audio using the existing Bilibili plugin account."""
# ruff: noqa: BLE001, SIM117

import asyncio
import importlib.util
import json
import logging
import os
import re
import ssl
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass_environment

import httpx


def download_client_options(url: str) -> dict:
    """Keep the system proxy without httpx parsing unrelated IPv6 NO_PROXY.

    Some httpx releases treat a bare IPv6 CIDR in NO_PROXY as a port. Resolve
    the applicable proxy for this CDN first, keeping the process environment
    and the Bilibili account/API client's networking unchanged.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Invalid audio stream URL")
    proxies = getproxies()
    proxy = None
    if "*" not in [entry.strip() for entry in proxies.get("no", "").split(",")]:
        if not proxy_bypass_environment(parsed.netloc, proxies):
            proxy = proxies.get(parsed.scheme) or proxies.get("all")
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    options = {"timeout": 60, "trust_env": False, "proxy": proxy}
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        options["verify"] = ssl.create_default_context(
            cafile=os.environ.get("SSL_CERT_FILE"),
            capath=os.environ.get("SSL_CERT_DIR"),
        )
    return options


async def main():
    """Return only a local receipt; never log credentials or signed URLs."""
    logging.disable(logging.CRITICAL)
    bvid = sys.argv[1]
    if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
        raise ValueError("Invalid BV identifier")
    spec = importlib.util.spec_from_file_location(
        "bili_bridge", "D:/QQBot/scripts/BiliScrollMcp.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    from bilibili_api import video

    item = video.Video(bvid=bvid, credential=bridge.load_shared_credential())
    info = await item.get_info()
    if not 1 <= info.get("duration", 0) <= 600:
        raise ValueError("Only tracks up to ten minutes are supported")
    streams = await item.get_download_url(page_index=0)
    audio = streams.get("dash", {}).get("audio", [])
    if not audio:
        raise ValueError("No accessible public audio stream")
    stream = max(audio, key=lambda x: x.get("bandwidth", 0))
    root = Path("D:/QQBot/data/cover-input")
    root.mkdir(exist_ok=True)
    for old in root.glob("*.m4a"):
        if time.time() - old.stat().st_mtime > 86400:
            old.unlink()
    target = root / (uuid.uuid4().hex + ".m4a")
    try:
        stream_url = stream.get("baseUrl") or stream["base_url"]
        async with httpx.AsyncClient(**download_client_options(stream_url)) as client:
            async with client.stream(
                "GET",
                stream_url,
                headers={
                    "Referer": "https://www.bilibili.com/",
                    "User-Agent": "Mozilla/5.0",
                },
            ) as response:
                response.raise_for_status()
                size = 0
                with target.open("wb") as out:
                    async for block in response.aiter_bytes(262144):
                        size += len(block)
                        if size > 50_000_000:
                            raise ValueError("Audio exceeds size limit")
                        out.write(block)
        if size < 1000:
            raise ValueError("Empty audio")
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": str(target),
                    "bvid": bvid,
                    "title": info["title"],
                    "duration": info["duration"],
                },
                ensure_ascii=True,
            )
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(json.dumps({"ok": False, "reason": type(error).__name__}))
        sys.exit(1)
