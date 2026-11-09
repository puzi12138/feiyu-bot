"""Fetch bounded public video/audio and sample timestamped local study assets."""

import asyncio
import importlib.util
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path("D:/QQBot")
FFMPEG = ROOT / "runtime/ffmpeg/bin/ffmpeg.exe"


async def download(client, url, target, limit):
    """Download only a Bilibili media host without passing account cookies."""
    host = urlsplit(url).hostname or ""
    if not (host == "bilivideo.com" or host.endswith(".bilivideo.com")):
        raise ValueError("UNEXPECTED_MEDIA_HOST")
    size = 0
    async with client.stream(
        "GET",
        url,
        headers={"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"},
    ) as response:
        response.raise_for_status()
        with target.open("wb") as output:
            async for block in response.aiter_bytes(262144):
                size += len(block)
                if size > limit:
                    raise ValueError("MEDIA_SIZE_LIMIT")
                output.write(block)
    return size


def ffmpeg(*arguments):
    """Run bounded local decoding without a shell or signed URL arguments."""
    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", *map(str, arguments)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=40,
        creationflags=0x08000000,
    )


async def main():
    logging.disable(logging.CRITICAL)
    bvid = sys.argv[1]
    if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
        raise ValueError("INVALID_BVID")
    spec = importlib.util.spec_from_file_location(
        "bili_bridge", ROOT / "scripts/BiliScrollMcp.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    from bilibili_api import video

    item = video.Video(bvid=bvid, credential=bridge.load_shared_credential())
    info = await item.get_info()
    duration = float(info["duration"])
    if not 1 <= duration <= 2700:
        raise ValueError("VIDEO_OVER_45_MINUTES")
    folder = ROOT / "data/video-reading" / bvid
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "assets.json"
    if manifest.exists():
        result = json.loads(manifest.read_text(encoding="utf-8"))
        if all(
            Path(row["path"]).is_file()
            for row in result["frames"] + result["audio_samples"]
        ):
            print(json.dumps(result, ensure_ascii=True))
            return
    streams = await item.get_download_url(page_index=0)
    dash = streams.get("dash", {})
    videos = [
        row
        for row in dash.get("video", [])
        if row.get("codecid") == 7 and row.get("height", 0) <= 480
    ] or dash.get("video", [])
    audios = dash.get("audio", [])
    if not videos or not audios:
        raise ValueError("PUBLIC_MEDIA_UNAVAILABLE")
    visual = min(videos, key=lambda row: row.get("bandwidth", 10**9))
    audio = min(audios, key=lambda row: row.get("bandwidth", 10**9))
    visual_path = folder / "video.m4s"
    audio_path = folder / "audio.m4s"
    async with httpx.AsyncClient(
        timeout=90, follow_redirects=False, trust_env=True
    ) as client:
        sizes = await asyncio.gather(
            download(
                client,
                visual.get("baseUrl") or visual["base_url"],
                visual_path,
                180_000_000,
            ),
            download(
                client,
                audio.get("baseUrl") or audio["base_url"],
                audio_path,
                60_000_000,
            ),
        )
    frames = []
    for index in range(18):
        at = min(duration - 0.1, max(0, 2 + (duration - 4) * index / 17))
        path = folder / f"frame-{index:02d}.jpg"
        await asyncio.to_thread(
            ffmpeg,
            "-ss",
            f"{at:.3f}",
            "-i",
            visual_path,
            "-frames:v",
            1,
            "-vf",
            "scale=640:-2",
            "-q:v",
            3,
            path,
        )
        frames.append({"at": round(at, 3), "path": str(path)})
    length = min(200, duration / 3)
    windows = (
        [
            (0, length),
            (max(length, duration / 2 - length / 2), length),
            (max(length * 2, duration - length), length),
        ]
        if duration > 600
        else [(0, duration)]
    )
    samples = []
    for index, (at, seconds) in enumerate(windows):
        path = folder / f"audio-{index:02d}.wav"
        await asyncio.to_thread(
            ffmpeg,
            "-ss",
            f"{at:.3f}",
            "-i",
            audio_path,
            "-t",
            f"{seconds:.3f}",
            "-ar",
            16000,
            "-ac",
            1,
            path,
        )
        samples.append(
            {"start": round(at, 3), "seconds": round(seconds, 3), "path": str(path)}
        )
    result = {
        "ok": True,
        "bvid": bvid,
        "title": info["title"],
        "duration": duration,
        "frames": frames,
        "frame_scope": "18 uniformly sampled frames across the timeline; not continuous viewing.",
        "audio_samples": samples,
        "audio_seconds": sum(row["seconds"] for row in samples),
        "bytes": sum(sizes),
    }
    manifest.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - subprocess receipt boundary
        print(json.dumps({"ok": False, "reason": type(error).__name__}))
        sys.exit(1)
