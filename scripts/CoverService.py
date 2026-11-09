"""Run the unmodified RVC service with local GPU coordination."""

import json
import os
import subprocess
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Literal

from fastapi import File, Form, Header, UploadFile

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "RVC-AI-Cover-Service"
settings = json.loads(
    (ROOT / "config" / "cover-runtime.json").read_text(encoding="utf-8-sig")
)
os.environ.update(
    AI_COVER_HOST="127.0.0.1",
    AI_COVER_PORT="18888",
    AI_COVER_API_TOKEN=settings["api_token"],
    AI_COVER_MAX_UPLOAD_MB="50",
    AI_COVER_MAX_AUDIO_SECONDS="600",
    AI_COVER_JOB_ROOT=str(ROOT / "temp" / "cover-jobs"),
    AI_COVER_CACHE_ROOT=str(ROOT / "data" / "cover-cache"),
    HF_HOME=str(ROOT / "cache" / "huggingface"),
    RVC_CUDA_GRAPH="0",
)
sys.path.insert(0, str(APP))
os.chdir(APP)
from tools import ai_cover_service as service

original_pipeline = service.run_cover_pipeline
original_cover_endpoint = service.cover
request_cover_mode = ContextVar("request_cover_mode", default="chorus")


def coordinated_pipeline(*args, cover_mode=None, **kwargs):
    """Release only the managed TTS service and restore it after the GPU job."""
    mode = request_cover_mode.get() if cover_mode is None else cover_mode
    if mode not in {"chorus", "full"}:
        raise ValueError("cover_mode must be chorus or full")
    service._resolve_model(kwargs.get("model_name", args[4] if len(args) > 4 else ""))
    restore_voice = (ROOT / "run" / "gsv.json").exists()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def script(name):
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / name),
            ],
            check=True,
            timeout=40,
            creationflags=flags,
        )

    try:
        if restore_voice:
            script("StopVoice.ps1")
        import torch

        free, total = torch.cuda.mem_get_info()
        if free < 3 * 1024**3:
            raise RuntimeError(
                "GPU memory is busy; close GPU-heavy applications and retry."
            )
        result, metadata = original_pipeline(*args, **kwargs)
        if mode == "chorus":
            from CoverExcerpt import make_excerpt

            speed = kwargs.get("speed", args[12] if len(args) > 12 else 1.0)
            result, excerpt = make_excerpt(
                result, result.parent / "converted_vocal.wav", speed
            )
            metadata.update(excerpt)
        else:
            metadata.update(selection="full_song")
        metadata["cover_mode"] = mode
        return result, metadata
    finally:
        try:
            # The native job lock is still held, so no active source is evicted.
            base = service.CACHE_ENTRY_ROOT.resolve()
            expected = ROOT / "data" / "cover-cache" / "separation-v2"
            if base != expected or service.CACHE_ENTRY_ROOT.is_symlink():
                raise ValueError("Cover cache is outside its owned directory")
            entries = sorted(
                service.CACHE_ENTRY_ROOT.glob("*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            total = 0
            stale = []
            for index, folder in enumerate(entries):
                if (
                    not folder.is_dir()
                    or folder.is_symlink()
                    or not service._is_audio_hash(folder.name)
                    or folder.resolve().parent != base
                ):
                    continue
                total += sum(
                    p.stat().st_size
                    for p in folder.rglob("*")
                    if p.is_file() and not p.is_symlink()
                )
                if (
                    index >= 12
                    or total > 1_000_000_000
                    or time.time() - folder.stat().st_mtime > 7 * 86400
                ):
                    stale.append(folder.name)
            if stale:
                service._delete_cache_entries(stale)
        except (OSError, ValueError):
            service.logger.exception("Cover cache maintenance skipped")
        if restore_voice:
            try:
                script("StartVoice.ps1")
            except (OSError, subprocess.SubprocessError):
                service.logger.exception(
                    "Cover finished but managed voice could not be restored"
                )


service.run_cover_pipeline = coordinated_pipeline


async def cover_with_mode(
    audio: Annotated[UploadFile, File()],
    model: Annotated[str, Form()],
    transpose: Annotated[int, Form()] = 0,
    instrumental_transpose: Annotated[int, Form()] = 0,
    index_rate: Annotated[float, Form()] = 0.75,
    rms_mix_rate: Annotated[float, Form()] = 0.25,
    protect: Annotated[float, Form()] = 0.25,
    vocal_gain: Annotated[float, Form()] = 1.0,
    instrumental_gain: Annotated[float, Form()] = 1.0,
    speed: Annotated[float, Form()] = 1.0,
    cover_mode: Annotated[Literal["chorus", "full"], Form()] = "chorus",
    x_ai_cover_token: Annotated[str | None, Header()] = None,
):
    """Keep native validation/limits while choosing one request's output length.

    asyncio.to_thread copies this request context to the native GPU pipeline;
    concurrent requests therefore never change one another's mode or default.
    """
    token = request_cover_mode.set(cover_mode)
    try:
        response = await original_cover_endpoint(
            audio=audio,
            model=model,
            transpose=transpose,
            instrumental_transpose=instrumental_transpose,
            index_rate=index_rate,
            rms_mix_rate=rms_mix_rate,
            protect=protect,
            vocal_gain=vocal_gain,
            instrumental_gain=instrumental_gain,
            speed=speed,
            x_ai_cover_token=x_ai_cover_token,
        )
        response.headers["X-AI-Cover-Mode"] = cover_mode
        return response
    finally:
        request_cover_mode.reset(token)


# Replace only the native POST endpoint; authentication, upload/duration limits,
# job locking, cache handling and response cleanup remain in the native handler.
for route in list(service.app.router.routes):
    if getattr(route, "path", None) == "/cover" and "POST" in getattr(route, "methods", set()):
        service.app.router.routes.remove(route)
service.app.add_api_route("/cover", cover_with_mode, methods=["POST"])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(service.app, host="127.0.0.1", port=18888, log_level="info")
