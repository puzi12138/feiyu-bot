"""Choose an available renderer and prepare local engines without setup fields."""
from __future__ import annotations

import asyncio
import contextlib
import time

import httpx

from .browser import _missing_browser, _missing_libraries
from .renderer import _is_capture_size_error

RENDER_SETUP_MESSAGE = '未配置可用的渲染服务，请前往 AstrBot WebUI → 插件 → 贴吧拾光 → 贴吧控制台 → 渲染管理，选择已有服务或下载精简浏览器内核。'


class RendererUnavailable(RuntimeError):
    pass


class RenderManager:
    def __init__(self, local, service_render, settings, endpoint):
        self.local = local
        self.service_render = service_render
        self.settings = settings
        self.endpoint = endpoint
        self.install_task = None
        self.install = {'status': 'idle', 'engine': 'headless', 'stage': '', 'error': ''}
        self._probe_key = ''
        self._probe_until = 0.0
        self._available = False
        self.active = 'pending'

    async def _service_available(self, refresh=False):
        endpoint = self.endpoint().rstrip('/')
        if not endpoint:
            return False
        if not refresh and self._probe_key == endpoint and time.monotonic() < self._probe_until:
            return self._available
        self._probe_key = endpoint
        self._probe_until = time.monotonic() + 60
        self._available = False
        try:
            async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                response = await client.get(endpoint)
            self._available = response.status_code in {200, 204, 404, 405}
        except httpx.HTTPError:
            pass
        return self._available

    async def status(self, refresh=False):
        settings = self.settings()
        available = await self._service_available(refresh)
        local = self.local.status()
        install = dict(self.install)
        if install['status'] == 'running':
            install['stage'] = self.local.progress() or '正在准备本地浏览器'
        mode = settings.get('render_backend', 'auto')
        active = 'service' if mode == 'service' or (mode == 'auto' and available) else 'local'
        if active == 'service' and not available:
            active = 'pending'
        if active == 'local' and not self._local_ready(local, settings):
            active = 'pending'
        if active == 'local' and install['status'] == 'running':
            active = 'pending'
        return {'backend': mode, 'engine': settings.get('browser_engine', 'headless'),
                'active': active, 'service': {'configured': bool(self.endpoint()), 'available': available},
                'local': {'available': local['available'], 'browser': local.get('browser', '')},
                'install': install, 'engines': local['engines']}

    @staticmethod
    def _local_ready(local, settings):
        if settings.get('render_backend', 'auto') == 'auto':
            return local['available']
        engine = settings.get('browser_engine', 'headless')
        return any(item['id'] == engine and item['installed'] for item in local['engines'])

    async def is_ready(self):
        return (await self.status())['active'] != 'pending'

    def unavailable_message(self):
        if self.install['status'] == 'running':
            return '渲染环境正在准备，请稍后重试；可在贴吧控制台 → 渲染管理查看下载进度。'
        if self.install['status'] == 'failed':
            return '渲染环境准备失败，请前往贴吧控制台 → 渲染管理查看原因并重试，或选择已有渲染服务。'
        if self.endpoint() and self.settings().get('render_backend', 'auto') != 'local':
            return '当前渲染服务无法连接，请前往贴吧控制台 → 渲染管理重新检测，或准备精简浏览器作为备用。'
        return RENDER_SETUP_MESSAGE

    async def render(self, template, data, options):
        settings = self.settings()
        mode = settings.get('render_backend', 'auto')
        use_service = mode == 'service' or (mode == 'auto' and await self._service_available())
        if use_service:
            try:
                self.active = 'service'
                # A failed shared service should not stall every future capture.
                # The screenshot itself allows 60 s. Cutting the request at
                # 35 s could start a fallback while the service still renders.
                return await asyncio.wait_for(self.service_render(template, data, options), timeout=70)
            except Exception as exc:
                if mode == 'service' or _is_capture_size_error(exc):
                    raise
                self._available = False
                self._probe_until = time.monotonic() + 60
        self.active = 'local'
        local = self.local.status()
        if not self._local_ready(local, settings) or self.install['status'] == 'running':
            raise RendererUnavailable(self.unavailable_message())
        engine = settings.get('browser_engine', 'headless')
        if mode == 'auto' and not any(item['id'] == engine and item['installed'] for item in local['engines']):
            engine = next((item['id'] for item in local['engines'] if item['installed']), engine)
        try:
            return await self.local.render(template, data, {**options, 'browser_engine': engine, 'prefer_system': mode == 'auto'})
        except Exception as exc:
            if _missing_browser(exc) or _missing_libraries(exc):
                raise RendererUnavailable('本地浏览器暂不可用，请前往贴吧控制台 → 渲染管理重新准备浏览器，或选择已有渲染服务。') from exc
            raise

    def prepare(self, engine):
        if not isinstance(engine, str) or engine not in {'headless', 'chromium'}:
            raise ValueError('请选择精简 Chromium 或完整 Chromium')
        if self.install_task and not self.install_task.done():
            return
        self.install = {'status': 'running', 'engine': engine, 'stage': '等待准备浏览器', 'error': ''}
        self.install_task = asyncio.create_task(self._prepare(engine))

    async def _prepare(self, engine):
        try:
            await self.local.prepare(engine)
            self.install.update(status='done', stage='浏览器已就绪', error='')
        except asyncio.CancelledError:
            self.install.update(status='failed', stage='准备已中断', error='插件停止或重载，可稍后重新准备。')
            raise
        except Exception as exc:
            self.install.update(status='failed', stage='浏览器准备失败', error=str(exc)[:300])

    async def close(self):
        if self.install_task and not self.install_task.done():
            self.install_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.install_task
        await self.local.close()
