"""Run the pinned storyboard CLI on a locally downloaded video segment."""

import asyncio
import json
import math
import os
import tempfile
from pathlib import Path


ROOT = Path("D:/QQBot")
NODE = ROOT / "apps/SnowLuma/node.exe"
CLI = ROOT / "apps/dsh-video-storyboard/bin/storyboard.mjs"
FFMPEG = ROOT / "runtime/ffmpeg/bin/ffmpeg.exe"
FFPROBE = ROOT / "runtime/ffmpeg/bin/ffprobe.exe"


async def storyboard_segment(segment, allowed_root, *, node=NODE, cli=CLI,
                             ffmpeg=FFMPEG, ffprobe=FFPROBE, timeout=150):
    """Return a bounded image and actual sample times from a local segment.

    Args:
        segment: File previously downloaded by the Bilibili plugin.
        allowed_root: The plugin-owned temporary video directory.
        node: Fixed Node executable path.
        cli: Fixed, pinned storyboard CLI path.
        ffmpeg: Fixed local ffmpeg executable path.
        ffprobe: Fixed local ffprobe executable path.
        timeout: Maximum wall time in seconds.

    Returns:
        PNG bytes and a list of cell index and segment-relative timestamps.

    Raises:
        ValueError: The local input or generated output is invalid.
        RuntimeError: The local subprocess does not complete successfully.
    """
    root = Path(allowed_root).resolve(strict=True)
    source = Path(segment).resolve(strict=True)
    if not source.is_file() or source == root or root not in source.parents:
        raise ValueError("INVALID_LOCAL_SEGMENT")
    if not all(Path(value).is_file() for value in (node, cli, ffmpeg, ffprobe)):
        raise ValueError("STORYBOARD_DEPENDENCY_MISSING")
    with tempfile.TemporaryDirectory(prefix="storyboard-", dir=str(root)) as folder:
        task_dir = Path(folder)
        out = task_dir / "storyboard.png"
        manifest = task_dir / "manifest.json"
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        env = {
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "PATH": str(Path(system_root) / "System32"),
            "TEMP": str(task_dir),
            "TMP": str(task_dir),
            "DSH_HOME": str(task_dir),
            "DSH_VIDEO_CACHE_DIR": str(task_dir / "cache"),
            "DSH_VIDEO_MEDIA_DIR": str(task_dir / "media"),
            "DSH_FFMPEG_PATH": str(ffmpeg),
            "DSH_FFPROBE_PATH": str(ffprobe),
        }
        options = {"creationflags": 0x08000000} if os.name == "nt" else {}
        process = await asyncio.create_subprocess_exec(
            str(node), str(cli), str(source), "--cells", "9", "--sheets", "1",
            "--pick", "mid", "--detail", "low", "--transcript", "off",
            "--out", str(out), "--manifest", str(manifest),
            cwd=str(task_dir), env=env, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, **options,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except BaseException:
            if process.returncode is None:
                if os.name == "nt":
                    try:
                        killer = await asyncio.create_subprocess_exec(
                            str(Path(system_root) / "System32/taskkill.exe"),
                            "/PID", str(process.pid), "/T", "/F",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL, **options,
                        )
                        await asyncio.wait_for(killer.wait(), 5)
                    except (OSError, asyncio.TimeoutError):
                        pass
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                else:
                    process.kill()
                await process.wait()
            raise
        if process.returncode != 0 or not out.is_file() or not manifest.is_file():
            raise RuntimeError("STORYBOARD_PROCESS_FAILED")
        if not 0 < out.stat().st_size <= 10_000_000 or manifest.stat().st_size > 1_000_000:
            raise ValueError("STORYBOARD_OUTPUT_LIMIT")
        png = out.read_bytes()
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("INVALID_STORYBOARD_IMAGE")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or Path(data.get("input", "")).resolve() != source
                or not isinstance(data.get("transcript"), dict)
                or data["transcript"].get("source") != "off"):
            raise ValueError("INVALID_STORYBOARD_PROVENANCE")
        window = data.get("window")
        if (not isinstance(window, dict) or window.get("from") != 0
                or isinstance(window.get("to"), bool)
                or not isinstance(window.get("to"), (int, float))
                or not math.isfinite(window["to"]) or window["to"] <= 0):
            raise ValueError("INVALID_STORYBOARD_WINDOW")
        sheets = data.get("sheets") if isinstance(data, dict) else None
        if not isinstance(sheets, list) or len(sheets) != 1:
            raise ValueError("INVALID_STORYBOARD_SHEETS")
        rows = sheets[0].get("cells") if isinstance(sheets[0], dict) else None
        if not isinstance(rows, list) or not 1 <= len(rows) <= 25:
            raise ValueError("INVALID_STORYBOARD_CELLS")
        cells = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("INVALID_STORYBOARD_CELL")
            values = [row.get(name) for name in ("t_start", "t_end")]
            if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0 for value in values)
                    or not values[0] <= values[1] <= window["to"] + 0.01):
                raise ValueError("INVALID_STORYBOARD_TIME")
            index = row.get("i")
            if type(index) is not int or index != len(cells) + 1:
                raise ValueError("INVALID_STORYBOARD_INDEX")
            cell = {"cell": index, "start": round(values[0], 3), "end": round(values[1], 3)}
            if "sub" in row:
                sub = row["sub"]
                if type(sub) is not int or not 2 <= sub <= 4 or "t_frame" in row:
                    raise ValueError("INVALID_STORYBOARD_SUBFRAMES")
                cell["subgrid"] = sub
            else:
                frame = row.get("t_frame")
                if (isinstance(frame, bool) or not isinstance(frame, (int, float))
                        or not math.isfinite(frame) or not values[0] <= frame <= values[1]):
                    raise ValueError("INVALID_STORYBOARD_TIME")
                cell["frame"] = round(frame, 3)
            cells.append(cell)
        return png, cells
