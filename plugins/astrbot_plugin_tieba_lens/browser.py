"""Shared local Chromium screenshots; no HTTP renderer or page network access.

API/CLI contracts: https://playwright.dev/python/docs/browsers and
https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from astrbot.api import logger

MAX_PIXELS = 24_000_000
MAX_HEIGHT = 30_000
INSTALL_TIMEOUT = 1800
TEMP_RETENTION = 600
IDLE_TIMEOUT = 45
CLOSE_TIMEOUT = 3
ENGINES = {"headless": "精简 Chromium", "chromium": "完整 Chromium"}
_NO_ANIMATIONS = "<style>*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}</style>"

_READY_AND_SIZE = """async () => {
  await document.fonts.ready;
  await Promise.all(Array.from(document.images, image => image.decode().catch(() => {})));
  const body = document.body, root = document.documentElement;
  return {
    width: Math.ceil(Math.max(innerWidth, root.scrollWidth, body ? body.scrollWidth : 0)),
    height: Math.ceil(Math.max(root.scrollHeight, body ? body.scrollHeight : 0,
      body ? body.getBoundingClientRect().height : 0))
  };
}"""


def _missing_browser(exc: Exception) -> bool:
    if _missing_libraries(exc):
        return False
    message = str(exc).lower()
    return bool(re.search(r"executable[^\n]*(?:doesn't exist|does not exist|not found)", message)
                or ("no such file or directory" in message and
                    any(name in message for name in ("chromium", "chrome-headless", "chrome"))))


def _missing_libraries(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(part in message for part in ("error while loading shared libraries",
               "host system is missing dependencies", "missing libraries:"))


def _recoverable_browser_error(exc: Exception) -> bool:
    """Only a dead/hung browser warrants a fresh, bounded capture attempt."""
    message = str(exc).lower()
    return (isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError"
            or any(part in message for part in ("page crashed", "target crashed",
                "target page, context or browser has been closed", "browser has been closed",
                "browser disconnected", "connection closed while reading from the driver")))


def _can_install_libraries() -> bool:
    return (sys.platform.startswith("linux") and hasattr(os, "geteuid")
            and os.geteuid() == 0 and bool(shutil.which("apt-get")))


class LocalRenderer:
    def __init__(self, data_root):
        self.data_root = Path(data_root).resolve()
        self.tmp = self.data_root / "render_tmp"
        try:
            version = importlib.metadata.version("playwright")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        self.browser_cache = self.data_root / "browser" / re.sub(r"[^a-zA-Z0-9_.-]", "_", version)
        self._browser = None
        self._browser_engine = None
        self._playwright = None
        self._start_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(1)
        self._contexts = set()
        self._active_paths = set()
        self._start_task = None
        self._installer = None
        self._browser_install_tried = {engine: False for engine in ENGINES}
        self._deps_install_tried = False
        self._closed = False
        self._jinja = None
        self._templates = OrderedDict()
        self._active = 0
        self._idle_task = None
        self._bad_executables = set()
        self._known_installed = set()
        self.stage = "本地浏览器尚未启动"

    @staticmethod
    def _validate_engine(engine):
        if not isinstance(engine, str) or engine not in ENGINES:
            raise ValueError("请选择精简 Chromium 或完整 Chromium。")
        return engine

    def _system_browser(self):
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable", "msedge"):
            path = shutil.which(name)
            if path and path not in self._bad_executables:
                return path
        candidates = []
        if sys.platform == "win32":
            for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                if os.environ.get(key):
                    root = Path(os.environ[key])
                    candidates.extend(root / name for name in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe", "Chromium/Application/chrome.exe"))
        elif sys.platform == "darwin":
            for root in (Path("/Applications"), Path.home() / "Applications"):
                candidates.extend(root / name for name in ("Google Chrome.app/Contents/MacOS/Google Chrome", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Chromium.app/Contents/MacOS/Chromium"))
        return next((str(path) for path in candidates if str(path) not in self._bad_executables and path.is_file()), None)

    def _browser_roots(self, own_only=False):
        roots = [self.browser_cache]
        if own_only:
            return roots
        configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if configured and configured != "0":
            roots.append(Path(configured).expanduser())
        if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
        elif sys.platform == "darwin":
            roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
        else:
            roots.append(Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright")
        return roots

    def _find_executable(self, engine, own_only=False):
        if engine == "headless":
            pattern = "chromium_headless_shell-*"
            names = ("chrome-headless-shell-linux64/chrome-headless-shell", "chrome-headless-shell-win64/chrome-headless-shell.exe",
                     "chrome-headless-shell-mac-arm64/chrome-headless-shell", "chrome-headless-shell-mac-x64/chrome-headless-shell",
                     "chrome-linux/headless_shell", "headless_shell", "headless_shell.exe")
        else:
            pattern = "chromium-*"
            names = ("chrome-linux64/chrome", "chrome-linux/chrome", "chrome-win64/chrome.exe", "chrome-win/chrome.exe",
                     "chrome-mac/Chromium.app/Contents/MacOS/Chromium", "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
                     "chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium")
        for root in self._browser_roots(own_only):
            for directory in sorted(root.glob(pattern), reverse=True):
                for name in names:
                    path = directory / name
                    if str(path) not in self._bad_executables and path.is_file():
                        return str(path)
        return None

    def status(self):
        engines = [{"id": engine, "name": name,
                    "installed": engine in self._known_installed or bool(self._find_executable(engine))}
                   for engine, name in ENGINES.items()]
        system = self._system_browser()
        label = "系统浏览器" if self._browser_engine == "system" else ENGINES.get(self._browser_engine, "")
        if not label and system:
            label = "系统浏览器"
        if not label:
            label = next((item["name"] for item in engines if item["installed"]), "")
        return {"available": bool(system or any(item["installed"] for item in engines)), "browser": label, "engines": engines}

    def progress(self):
        return self.stage

    async def _begin_work(self):
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("本地图片渲染器已关闭。")
            self._active += 1
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None

    async def _end_work(self):
        async with self._start_lock:
            self._active -= 1
            if not self._active and not self._closed:
                self._idle_task = asyncio.create_task(self._idle_shutdown())

    async def _idle_shutdown(self):
        task = asyncio.current_task()
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
            async with self._start_lock:
                if not self._active and not self._closed:
                    await self._dispose_runtime()
                    self.stage = "本地浏览器已释放内存，下一次渲染自动启动。"
        finally:
            if self._idle_task is task:
                self._idle_task = None

    async def prepare(self, engine="headless"):
        engine = self._validate_engine(engine)
        async with self._slots:
            await self._begin_work()
            try:
                # An explicit user retry starts one new preparation budget. Auto
                # message renders keep their previous budget after a failure.
                self._browser_install_tried[engine] = False
                self._deps_install_tried = False
                self._bad_executables.clear()
                await self._ensure_browser(engine, prefer_system=False, allow_install=True)
                self.stage = ENGINES[engine] + " 已就绪"
            finally:
                await self._end_work()

    async def _start_playwright(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("本地图片渲染依赖尚未安装，请安装插件依赖后重载。") from exc
        return await async_playwright().start()

    def _render_html(self, template: str, data: dict) -> str:
        if self._jinja is None:
            try:
                from jinja2 import Environment
            except ImportError as exc:
                raise RuntimeError("本地图片排版依赖 Jinja2 尚未安装，请重载插件。") from exc
            self._jinja = Environment(autoescape=True)
        compiled = self._templates.get(template)
        if compiled is None:
            compiled = self._jinja.from_string(template)
            self._templates[template] = compiled
            if len(self._templates) > 4:
                self._templates.popitem(last=False)
        else:
            self._templates.move_to_end(template)
        return compiled.render(**data)

    async def _ensure_browser(self, engine="headless", prefer_system=True, allow_install=False):
        engine = self._validate_engine(engine)
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("本地图片渲染器已关闭。")
            if (self._browser is not None and self._browser.is_connected()
                    and (self._browser_engine == engine or (prefer_system and self._browser_engine == "system"))):
                return self._browser
            self._start_task = asyncio.current_task()
            try:
                if self._browser is not None:
                    with contextlib.suppress(Exception):
                        await self._browser.close()
                    self._browser = None
                if self._playwright is None:
                    self._playwright = await self._start_playwright()
                system = self._system_browser() if prefer_system else None
                executable = system or self._find_executable(engine)
                args = ["--disable-dev-shm-usage", "--disable-background-networking", "--disable-component-update"]
                if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
                    args.append("--no-sandbox")
                while True:
                    options = {"headless": True, "args": args, "timeout": 30000}
                    if executable:
                        options["executable_path"] = executable
                    elif engine == "chromium":
                        options["channel"] = "chromium"
                    try:
                        self._browser = await self._playwright.chromium.launch(**options)
                        self._browser_engine = "system" if executable and executable == system else engine
                        if self._browser_engine != "system":
                            self._known_installed.add(engine)
                        self.stage = ("系统浏览器" if self._browser_engine == "system" else ENGINES[engine]) + " 已就绪"
                        return self._browser
                    except Exception as exc:
                        if executable and prefer_system and executable == system:
                            self._bad_executables.add(executable)
                            executable = self._find_executable(engine)
                            system = None
                            continue
                        if _missing_browser(exc) and executable:
                            self._bad_executables.add(executable)
                            self._known_installed.discard(engine)
                            executable = self._find_executable(engine)
                            continue
                        if _missing_browser(exc) and allow_install and not self._browser_install_tried[engine]:
                            self._browser_install_tried[engine] = True
                            self.stage = "正在下载并安装" + ENGINES[engine] + "，首次准备需要一些时间。"
                            logger.info("[贴吧拾光] 本地渲染首次初始化，正在自动准备 Chromium 浏览器。")
                            await self._run_install("install", "--only-shell" if engine == "headless" else "--no-shell", "chromium")
                            executable = self._find_executable(engine, own_only=True)
                            if not executable:
                                raise RuntimeError("本地渲染初始化失败：浏览器准备后未找到内核文件。") from exc
                            continue
                        if _missing_libraries(exc) and allow_install and not self._deps_install_tried and _can_install_libraries():
                            self._deps_install_tried = True
                            self.stage = "正在自动安装 Linux 浏览器运行库。"
                            logger.info("[贴吧拾光] Chromium 缺少运行库，正在自动准备 Linux 浏览器依赖。")
                            await self._run_install("install-deps", "chromium")
                            continue
                        logger.error("[贴吧拾光] 本地图片渲染初始化失败：%s", exc)
                        self.stage = "本地图片渲染初始化失败：" + str(exc)[:180]
                        raise
            except asyncio.CancelledError:
                self.stage = "本地浏览器准备已取消。"
                raise
            except Exception as exc:
                self.stage = "本地图片渲染初始化失败：" + str(exc)[:180]
                raise
            finally:
                self._start_task = None

    async def _run_install(self, *arguments):
        self.browser_cache.mkdir(parents=True, exist_ok=True)
        options = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.STDOUT,
                   "env": {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(self.browser_cache),
                           "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": os.environ.get("PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT", "20000")}}
        if os.name == "posix":
            options["start_new_session"] = True
        elif os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "playwright", *arguments, **options)
        self._installer = process
        try:
            try:
                output = await asyncio.wait_for(self._read_install_output(process), INSTALL_TIMEOUT)
            except asyncio.TimeoutError as exc:
                await self._terminate_installer(process)
                raise RuntimeError("本地渲染初始化超时，准备已停止，请在渲染管理中重新准备。") from exc
            if process.returncode:
                detail = output.decode("utf-8", errors="replace")[-1200:].strip()
                raise RuntimeError(f"本地渲染初始化失败，浏览器自动准备未完成：{detail}")
        except asyncio.CancelledError:
            await self._terminate_installer(process)
            raise
        finally:
            if self._installer is process:
                self._installer = None

    async def _read_install_output(self, process):
        tail = b""
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                break
            tail = (tail + chunk)[-6000:]
            lines = re.split(r"[\r\n]+", chunk.decode("utf-8", errors="replace"))
            for line in lines:
                percentage = re.search(r"\b(\d{1,3})%\s+of\s+([\d.]+\s*\w+)", line)
                if percentage:
                    self.stage = f"正在下载浏览器组件：{percentage[1]}% / {percentage[2]}"
                elif "Downloading" in line:
                    self.stage = "正在连接下载源并准备浏览器组件。"
                elif "downloaded to" in line:
                    self.stage = "浏览器组件下载完成，正在检查运行环境。"
        await process.wait()
        return tail

    @staticmethod
    async def _terminate_installer(process):
        if process.returncode is not None:
            return
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        elif os.name == "nt":
            # The CLI spawns Node; terminate the whole install tree on Windows.
            try:
                killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                await asyncio.wait_for(killer.wait(), 5)
            except (OSError, asyncio.TimeoutError):
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
        else:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.wait(), 5)

    def _cleanup_tmp(self, active):
        cutoff = time.time() - TEMP_RETENTION
        for path in self.tmp.glob("*.png"):
            if path in active:
                continue
            with contextlib.suppress(OSError):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()

    async def render(self, template: str, data: dict, options: dict) -> str:
        options = options or {}
        width = options.get("viewport_width", data.get("width", 960))
        if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 4096:
            raise ValueError("本地渲染宽度须为 1～4096 像素。")
        timeout = options.get("timeout", 60000)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
            timeout = 60000
        timeout = min(120000, max(1000, int(timeout)))
        engine = self._validate_engine(options.get("browser_engine", "headless"))
        prefer_system = options.get("prefer_system", True)
        if not isinstance(prefer_system, bool):
            raise ValueError("prefer_system 必须是开关值。")
        async with self._slots:
            await self._begin_work()
            try:
                html = self._render_html(template, data)
                # add_style_tag can wait forever when page JavaScript is disabled.
                # Insert static CSS before parsing instead; preserve the doctype.
                if re.search(r"</head\s*>", html, re.I):
                    html = re.sub(r"</head\s*>", lambda match: _NO_ANIMATIONS + match.group(), html, count=1, flags=re.I)
                else:
                    html += _NO_ANIMATIONS
                self.tmp.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(self._cleanup_tmp, frozenset(self._active_paths))
                path = self.tmp / (uuid.uuid4().hex + ".png")
                self._active_paths.add(path)
                self.stage = "正在本地排版并生成卡片。"
                try:
                    for attempt in range(2):
                        try:
                            browser = await self._ensure_browser(engine, prefer_system=prefer_system)
                            await asyncio.wait_for(self._capture(browser, html, width, timeout, path), timeout / 1000 + 5)
                            break
                        except Exception as exc:
                            if not _recoverable_browser_error(exc):
                                raise
                            # A crashed page can leave a connected but unusable
                            # browser/driver. Dispose it before the next job too.
                            async with self._start_lock:
                                await self._dispose_runtime()
                            path.unlink(missing_ok=True)
                            if attempt:
                                raise
                            self.stage = "浏览器意外退出或超时，正在恢复并重试一次。"
                            logger.warning("[贴吧拾光] 本地浏览器异常，释放后重试一次：%s", type(exc).__name__)
                            await asyncio.sleep(0.2)
                    if not path.is_file():
                        raise RuntimeError("本地图片渲染未生成截图文件。")
                    self.stage = "本地卡片生成完成。"
                    return str(path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                    raise
                finally:
                    self._active_paths.discard(path)
            finally:
                await self._end_work()

    async def _capture(self, browser, html, width, timeout, path):
        context = await browser.new_context(viewport={"width": width, "height": 1},
            device_scale_factor=1, java_script_enabled=False, service_workers="block",
            offline=True, accept_downloads=False, reduced_motion="reduce")
        self._contexts.add(context)
        try:
            async def block_network(route):
                await route.abort()
            await context.route(re.compile(r"^https?://", re.I), block_network)
            page = await context.new_page()
            page.set_default_timeout(timeout)
            await page.set_content(html, wait_until="load", timeout=timeout)
            dimensions = await page.evaluate(_READY_AND_SIZE)
            actual_width = math.ceil(float(dimensions["width"]))
            height = math.ceil(float(dimensions["height"]))
            if actual_width < 1 or height < 1 or actual_width * height > MAX_PIXELS or height > MAX_HEIGHT:
                raise RuntimeError(f"截图尺寸超限：{actual_width}×{height} 像素，最大 2400 万像素或 30000 像素高。")
            await page.screenshot(path=str(path), type="png", full_page=True,
                animations="disabled", scale="css", timeout=timeout)
        finally:
            try:
                await self._close_resource(context.close(), "页面上下文")
            finally:
                self._contexts.discard(context)

    async def close(self):
        self._closed = True
        if self._idle_task is not None:
            self._idle_task.cancel()
            await asyncio.gather(self._idle_task, return_exceptions=True)
            self._idle_task = None
        task = self._start_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._start_lock:
            await self._dispose_runtime()

    async def _dispose_runtime(self):
        contexts = tuple(self._contexts)
        self._contexts.clear()
        browser, playwright = self._browser, self._playwright
        # Detach stale resources before awaiting their shutdown. A failed close
        # must not leave the next capture reusing a broken browser or driver.
        self._browser = self._playwright = None
        self._browser_engine = None
        try:
            await asyncio.gather(*(self._close_resource(context.close(), "页面上下文")
                                   for context in contexts))
        finally:
            try:
                if browser is not None:
                    await self._close_resource(browser.close(), "浏览器")
            finally:
                if playwright is not None:
                    await self._close_resource(playwright.stop(), "浏览器驱动")

    @staticmethod
    async def _close_resource(operation, label):
        try:
            await asyncio.wait_for(operation, CLOSE_TIMEOUT)
        except Exception as exc:
            logger.debug("[贴吧拾光] %s 关闭未完成：%s", label, type(exc).__name__)
