"""Loopback-only, on-demand stable-diffusion.cpp image generation broker.

No model, image, or user data is downloaded by this module. Each accepted request
owns one CLI child; completing/cancelling that request releases its GPU process.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from aiohttp import web
from PIL import Image

MODEL_ID = "Counterfeit-V3.0"
HOST = "127.0.0.1"
PORT = 18889
DEFAULT_CONFIG = Path("D:/QQBot/config/local-image.json")
MAX_REQUEST_BYTES = 24 * 1024
MAX_PROMPT_CHARS = 4096
MAX_NEGATIVE_CHARS = 2048
MAX_PNG_BYTES = 12 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
RETENTION_SECONDS = 30 * 86400
CLI_TIMEOUT_SECONDS = 540
SIZES = {"512x512": (512, 512), "512x768": (512, 768), "768x512": (768, 512)}
DEFAULT_NEGATIVE = (
    "worst quality, low quality, blurry, bad anatomy, bad hands, extra fingers, "
    "text, watermark, signature"
)
OWN_IMAGE = re.compile(r"image-[0-9a-f]{32}\.png\Z")
OWN_AUDIT = re.compile(r"audit-\d{4}-\d{2}-\d{2}\.jsonl\Z")
EAST_ASIAN_TEXT = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


class ImageRequestError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str
    sd_cli: Path
    model_path: Path
    output_dir: Path

    @classmethod
    def load(cls, path: Path) -> Settings:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        token = raw.get("token", "")
        if not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError("local image token must have at least 32 non-space characters")
        return cls(
            token=token,
            sd_cli=Path(raw.get("sd_cli", "D:/QQBot/apps/stable-diffusion-cpp/sd-cli.exe")),
            model_path=Path(raw.get(
                "model_path",
                "D:/QQBot/data/image-generation/models/Counterfeit-V3.0_fix_fp16.safetensors",
            )),
            output_dir=Path(raw.get("output_dir", "D:/QQBot/data/image-generation/generated")),
        )

    @property
    def ready(self) -> bool:
        return self.sd_cli.is_file() and self.model_path.is_file()


@dataclass(frozen=True)
class Generation:
    prompt: str
    negative: str
    width: int
    height: int


def parse_generation(raw: Any) -> Generation:
    if not isinstance(raw, dict):
        raise ImageRequestError("request must be a JSON object")
    allowed = {"model", "prompt", "negative_prompt", "n", "size", "response_format", "user"}
    if set(raw) - allowed:
        raise ImageRequestError("unsupported image generation parameters")
    if raw.get("model", MODEL_ID) != MODEL_ID:
        raise ImageRequestError("unsupported model")
    if type(raw.get("n", 1)) is not int or raw.get("n", 1) != 1:
        raise ImageRequestError("only n=1 is supported")
    if raw.get("response_format", "b64_json") != "b64_json":
        raise ImageRequestError("only b64_json is supported")
    size = raw.get("size", "512x512")
    if not isinstance(size, str) or size not in SIZES:
        raise ImageRequestError("size must be 512x512, 512x768, or 768x512")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise ImageRequestError("prompt must contain 1 to 4096 characters")
    negative = raw.get("negative_prompt", DEFAULT_NEGATIVE)
    if not isinstance(negative, str) or len(negative) > MAX_NEGATIVE_CHARS:
        raise ImageRequestError("negative_prompt must contain at most 2048 characters")
    if "\x00" in prompt or "\x00" in negative:
        raise ImageRequestError("prompt contains an unsupported character")
    if EAST_ASIAN_TEXT.search(prompt + negative) or not re.search(r"[A-Za-z]", prompt):
        raise ImageRequestError("translate the prompt into English before generation")
    width, height = SIZES[size]
    return Generation(prompt.strip(), negative.strip(), width, height)


def cli_arguments(settings: Settings, generation: Generation, output: Path,
                  prompt_file: Path, negative_file: Path) -> list[str]:
    # All paths originate in local startup configuration or broker-created files.
    # No shell runs, and prompt contents are absent from the process command line.
    return [
        str(settings.sd_cli), "--model", str(settings.model_path),
        "--prompt-file", str(prompt_file), "--negative-prompt-file", str(negative_file),
        "--output", str(output), "--width", str(generation.width),
        "--height", str(generation.height), "--batch-count", "1",
        "--steps", "20", "--cfg-scale", "7", "--sampling-method", "euler_a",
        "--seed", "-1", "--clip-skip", "2", "--threads", "4",
        "--backend", "diffusion=cuda0,te=cpu,vae=cpu", "--params-backend", "cpu",
        "--max-vram", "2.2", "--flash-attn", "auto", "--batched-cfg", "off",
        "--vae-tiling", "--disable-image-metadata",
    ]


def _owned_file(path: Path, root: Path, name_pattern: re.Pattern[str]) -> bool:
    if not name_pattern.fullmatch(path.name) or path.is_symlink():
        return False
    try:
        return path.is_file() and path.resolve().parent == root.resolve()
    except OSError:
        return False


def prune_images(root: Path, *, now: float | None = None,
                 max_bytes: int = MAX_OUTPUT_BYTES) -> None:
    """Only remove broker-named direct image files from the output directory."""
    now = time.time() if now is None else now
    if not root.is_dir():
        return
    surviving = []
    for path in root.iterdir():
        if not _owned_file(path, root, OWN_IMAGE):
            continue
        try:
            stat = path.stat()
            if now - stat.st_mtime > RETENTION_SECONDS:
                path.unlink()
            else:
                surviving.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            continue
    total = sum(row[1] for row in surviving)
    for _, size, path in sorted(surviving):
        if total <= max_bytes:
            break
        if _owned_file(path, root, OWN_IMAGE):
            try:
                path.unlink()
                total -= size
            except OSError:
                pass


def numeric_audit(root: Path, row: dict[str, Any]) -> None:
    """Record numeric job metadata only, never text supplied by a user or CLI."""
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    path = root / f"audit-{datetime.now(UTC).date().isoformat()}.jsonl"
    permitted = {"at", "width", "height", "elapsed_ms", "success", "outcome", "bytes"}
    if set(row) - permitted or any(type(value) not in (int, float, bool) for value in row.values()):
        raise ValueError("audit rows must contain numeric metadata only")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    for old in root.iterdir():
        if _owned_file(old, root, OWN_AUDIT):
            try:
                if now - old.stat().st_mtime > RETENTION_SECONDS:
                    old.unlink()
            except OSError:
                pass


def png_bytes(path: Path, width: int, height: int) -> bytes:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_PNG_BYTES:
        raise ValueError("invalid generated image")
    data = path.read_bytes()
    with Image.open(BytesIO(data)) as image:
        if image.format != "PNG" or image.size != (width, height):
            raise ValueError("invalid generated image")
        image.verify()
    return data


class LocalImageService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.busy = False
        self.child = None
        self.timeout = CLI_TIMEOUT_SECONDS

    async def _stop_child(self, child) -> None:
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        await child.wait()

    @staticmethod
    async def _finish_cleanup(awaitable):
        """Finish one owned cleanup even if disconnect and shutdown both cancel.

        Only use while already propagating cancellation or during final cleanup;
        the caller remains responsible for re-raising its original cancellation.
        Shield alone protects the child task but not the caller's await, so keep
        its task reference and wait again after every additional cancellation.
        """
        task = asyncio.ensure_future(awaitable)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()

    async def generate(self, generation: Generation) -> bytes:
        root = self.settings.output_dir
        root.mkdir(parents=True, exist_ok=True)
        output = root / f"image-{uuid.uuid4().hex}.png"
        started = time.monotonic()
        result = b""
        outcome = 1  # 0 success, 1 failure, 2 timeout, 3 cancelled.
        child = None
        try:
            # Private temporary files are removed even after a failure/cancellation.
            with tempfile.TemporaryDirectory(prefix="feiyu-image-") as temporary:
                prompt_file, negative_file = Path(temporary) / "prompt.txt", Path(temporary) / "negative.txt"
                prompt_file.write_text(generation.prompt, encoding="utf-8")
                negative_file.write_text(generation.negative, encoding="utf-8")
                args = cli_arguments(self.settings, generation, output, prompt_file, negative_file)
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    stdin=asyncio.subprocess.DEVNULL,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                ))
                try:
                    child = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    # Cancellation during CreateProcess must not lose the only
                    # reference to a newly started GPU process.
                    child = await self._finish_cleanup(spawn)
                    self.child = child
                    raise
                self.child = child
                try:
                    returncode = await asyncio.wait_for(child.wait(), timeout=self.timeout)
                except TimeoutError:
                    outcome = 2
                    await self._stop_child(child)
                    raise
                if returncode != 0:
                    raise RuntimeError("image generation failed")
                result = png_bytes(output, generation.width, generation.height)
                outcome = 0
                return result
        except asyncio.CancelledError:
            outcome = 3
            if child is not None:
                await self._finish_cleanup(self._stop_child(child))
            raise
        finally:
            if child is not None and child.returncode is None:
                await self._finish_cleanup(self._stop_child(child))
            self.child = None
            if outcome != 0 and _owned_file(output, root, OWN_IMAGE):
                try:
                    output.unlink()
                except OSError:
                    pass
            try:
                numeric_audit(root.parent / "audit", {
                    "at": int(time.time()), "width": generation.width,
                    "height": generation.height, "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "success": outcome == 0, "outcome": outcome, "bytes": len(result),
                })
                prune_images(root)
            except OSError:
                # Disk audit/cleanup issues must not turn a completed image into
                # a retryable generation error that spends GPU time again.
                pass

    def authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "")
        expected = "Bearer " + self.settings.token
        return hmac.compare_digest(supplied.encode(), expected.encode())

    @staticmethod
    def error(message: str, status: int) -> web.Response:
        return web.json_response({"error": {"message": message, "type": "local_image_error"}}, status=status)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ready" if self.settings.ready else "not_ready",
                                  "model": MODEL_ID, "busy": self.busy,
                                  "on_demand": True, "edits_supported": False})

    async def models(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return self.error("authentication required", 401)
        return web.json_response({"object": "list", "data": [
            {"id": MODEL_ID, "object": "model", "owned_by": "local"},
        ]})

    async def edits(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return self.error("authentication required", 401)
        return self.error("image edits are not supported by this local backend", 501)

    async def generations(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return self.error("authentication required", 401)
        if self.busy:
            return self.error("local image generator is busy; no job was queued", 409)
        if not self.settings.ready:
            return self.error("local image backend is not ready", 503)
        try:
            raw = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return self.error("image request is too large", 413)
        except (ValueError, UnicodeError):
            return self.error("invalid JSON image request", 400)
        try:
            generation = parse_generation(raw)
        except ImageRequestError as exc:
            return self.error(str(exc), 400)
        # Recheck after reading the request body; another request may have won.
        if self.busy:
            return self.error("local image generator is busy; no job was queued", 409)
        self.busy = True
        try:
            data = await self.generate(generation)
            return web.json_response({"created": int(time.time()), "data": [
                {"b64_json": base64.b64encode(data).decode("ascii")},
            ]})
        except TimeoutError:
            return self.error("image generation timed out; the local process was stopped", 504)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- never expose prompt-bearing CLI/Pillow exception details
            return self.error("local image generation failed; no automatic retry was made", 500)
        finally:
            self.busy = False

    async def cleanup(self, app: web.Application) -> None:
        if self.child is not None:
            await self._stop_child(self.child)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_REQUEST_BYTES)
        app.router.add_get("/health", self.health)
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/v1/images/generations", self.generations)
        app.router.add_post("/v1/images/edits", self.edits)
        app.on_cleanup.append(self.cleanup)
        return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Feiyu local image backend")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    settings = Settings.load(args.config)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    prune_images(settings.output_dir)
    # Bind address/port are deliberately not supplied by configuration or HTTP.
    web.run_app(LocalImageService(settings).app(), host=HOST, port=PORT,
                access_log=None, print=None, handler_cancellation=True)


if __name__ == "__main__":
    main()
