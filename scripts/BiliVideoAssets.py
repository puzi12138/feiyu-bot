"""Fetch bounded public video/audio and sample timestamped local study assets."""

import asyncio
import importlib.util
import json
import logging
import math
import re
import subprocess
import sys
import wave
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path("D:/QQBot")
FFMPEG = ROOT / "runtime/ffmpeg/bin/ffmpeg.exe"
FFPROBE = ROOT / "runtime/ffmpeg/bin/ffprobe.exe"


def verified_source(path, media_type, duration):
    """Reuse complete local media; preserve and reject an invalid old file."""
    if not path.exists():
        return False
    limit = 180_000_000 if media_type == "video" else 60_000_000
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError("INVALID_CACHED_MEDIA")
    receipt = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,duration", "-of", "json", str(path)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=20, creationflags=0x08000000,
    )
    if len(receipt.stdout) > 100_000:
        raise ValueError("MEDIA_PROBE_OUTPUT_LIMIT")
    data = json.loads(receipt.stdout)
    if not any(row.get("codec_type") == media_type for row in data.get("streams", [])):
        raise ValueError("CACHED_MEDIA_TYPE_MISMATCH")
    seconds = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(seconds) or abs(seconds - duration) > max(2, duration * 0.01):
        raise ValueError("CACHED_MEDIA_DURATION_MISMATCH")
    return True


def cached_assets(folder, bvid):
    """Read legacy receipts locally before any metadata or download request."""
    manifest = folder / "assets.json"
    if not manifest.exists():
        return None
    result = json.loads(manifest.read_text(encoding="utf-8"))
    duration = result["duration"]
    if (result.get("bvid") != bvid or isinstance(duration, bool)
            or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or not 1 <= duration <= 2700):
        raise ValueError("INVALID_CACHED_ASSETS")
    rows = result["audio_samples"]
    declared = result["audio_seconds"]
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 3
            or isinstance(declared, bool) or not isinstance(declared, (int, float))
            or not math.isfinite(declared) or not 0 < declared <= 600.01):
        raise ValueError("INVALID_CACHED_AUDIO")
    total = 0
    actual_total = 0
    for row in rows:
        start, seconds = row.get("start"), row.get("seconds")
        if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) for value in (start, seconds))
                or start < 0 or seconds <= 0 or start + seconds > duration + 0.01):
            raise ValueError("INVALID_CACHED_AUDIO_WINDOW")
        total += seconds
        sample = Path(row["path"]).resolve(strict=True)
        if sample.parent != folder.resolve() or not sample.is_file():
            raise ValueError("INVALID_CACHED_SAMPLE_PATH")
        # The ASR helper already returns old asr.json unchanged. Only material
        # which could be newly transcribed needs local PCM duration verification.
        if not (folder / "asr.json").is_file():
            actual_total += verified_sample(sample, seconds)
    if total > 600.01 or abs(total - declared) > 0.01 or actual_total > 600.01:
        raise ValueError("INVALID_CACHED_AUDIO_BUDGET")
    visual = folder / "video.m4s"
    if not (folder / "vision.json").is_file():
        if not verified_source(visual, "video", float(result["duration"])):
            raise ValueError("CACHED_VISUAL_SOURCE_MISSING")
    return {**result, "visual_source": str(visual)}


def verified_sample(sample, seconds):
    """Check actual local PCM duration before reusing a previous sample."""
    if not sample.is_file() or not 0 < sample.stat().st_size <= 20_000_000:
        raise ValueError("INVALID_CACHED_AUDIO_SAMPLE")
    with wave.open(str(sample), "rb") as recording:
        if (recording.getframerate() != 16000 or recording.getnchannels() != 1
                or recording.getsampwidth() != 2):
            raise ValueError("INVALID_CACHED_AUDIO_FORMAT")
        actual = recording.getnframes() / recording.getframerate()
    if abs(actual - seconds) > 0.01:
        raise ValueError("CACHED_AUDIO_DURATION_MISMATCH")
    return actual


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
        partial = target.with_suffix(target.suffix + ".part")
        with partial.open("wb") as output:
            async for block in response.aiter_bytes(262144):
                size += len(block)
                if size > limit:
                    raise ValueError("MEDIA_SIZE_LIMIT")
                output.write(block)
        partial.replace(target)
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
    folder = ROOT / "data/video-reading" / bvid
    existing = cached_assets(folder, bvid)
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=True))
        return
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
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "assets.json"
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
        tasks = []
        reused_bytes = 0
        for path, media_type, stream, limit in (
            (visual_path, "video", visual, 180_000_000),
            (audio_path, "audio", audio, 60_000_000),
        ):
            if verified_source(path, media_type, duration):
                reused_bytes += path.stat().st_size
                continue
            tasks.append(download(
                client,
                stream.get("baseUrl") or stream["base_url"], path, limit,
            ))
        sizes = await asyncio.gather(*tasks)
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
        if path.exists():
            verified_sample(path, seconds)
        else:
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
        "frames": [],
        "visual_source": str(visual_path),
        "frame_scope": "Local video retained for dsh-video-storyboard; actual cell ranges are reported by the reader.",
        "audio_samples": samples,
        "audio_seconds": sum(row["seconds"] for row in samples),
        "bytes": reused_bytes + sum(sizes),
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
