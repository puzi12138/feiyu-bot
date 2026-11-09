"""Lazy, failure-isolated discovery of duck-typed BiliBot extensions."""
from __future__ import annotations

import inspect
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - test fallback
    class _Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None
    logger = _Logger()

from .contracts import validate_extension_manifest
from .host_api import BiliBotExtensionHostAPI
from .permissions import ExtensionGrant

HOOK_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class RegisteredExtension:
    extension_id: str
    plugin: Any
    manifest: dict[str, Any]
    host_api: BiliBotExtensionHostAPI


def _plugin_instances(stars: Any) -> Iterable[Any]:
    values = stars.values() if isinstance(stars, dict) else (stars or [])
    for value in values:
        if getattr(value, "activated", True) is False:
            continue
        instance = getattr(value, "star_cls", getattr(value, "star", getattr(value, "instance", value)))
        if instance is not None:
            yield instance


async def _maybe_await(value: Any) -> Any:
    return await asyncio.wait_for(value, timeout=HOOK_TIMEOUT_SECONDS) if inspect.isawaitable(value) else value


class ExtensionRegistry:
    def __init__(self, context: Any, plugin: Any, action_executor: Any):
        self.context = context
        self.plugin = plugin
        self.action_executor = action_executor
        self.extensions: dict[str, RegisteredExtension] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def _unbind(self, record: RegisteredExtension) -> None:
        record.host_api.revoke()
        unbind = getattr(record.plugin, "unbind_bilibot_host", None)
        if callable(unbind):
            try:
                await _maybe_await(unbind())
            except Exception as exc:
                logger.warning(f"[BiliBot Extensions] unbind {record.extension_id} failed: {exc}")

    async def refresh(self) -> dict[str, RegisteredExtension]:
        async with self._lock:
            if self._closed:
                return {}
            return await self._refresh()

    async def _refresh(self) -> dict[str, RegisteredExtension]:
        getter = getattr(self.context, "get_all_stars", None)
        if not callable(getter):
            for record in self.extensions.values():
                await self._unbind(record)
            self.extensions = {}
            return self.extensions
        try:
            stars = await _maybe_await(getter())
        except Exception as exc:
            logger.warning(f"[BiliBot Extensions] discovery unavailable: {exc}")
            for record in self.extensions.values():
                await self._unbind(record)
            self.extensions = {}
            return self.extensions

        discovered: dict[str, RegisteredExtension] = {}
        for extension in _plugin_instances(stars):
            host_api = None
            manifest_getter = getattr(extension, "get_bilibot_extension_manifest", None)
            handler = getattr(extension, "handle_bilibot_extension_request", None)
            if not callable(manifest_getter) or not callable(handler):
                continue
            try:
                manifest = await _maybe_await(manifest_getter())
                validate_extension_manifest(manifest)
                manifest = deepcopy(dict(manifest))
                if manifest.get("enabled", True) is False:
                    continue
                extension_id = str(manifest["id"])
                if extension_id in discovered:
                    logger.warning(f"[BiliBot Extensions] duplicate id skipped: {extension_id}")
                    continue
                previous = self.extensions.get(extension_id)
                grant = ExtensionGrant.create(extension_id, manifest.get("permissions") or [])
                if previous is not None and previous.plugin is extension and previous.host_api._grant == grant:
                    previous.manifest = manifest
                    discovered[extension_id] = previous
                    continue
                if previous is not None:
                    self.extensions.pop(extension_id)
                    await self._unbind(previous)
                host_api = BiliBotExtensionHostAPI(grant, self.plugin, self.action_executor)
                binder = getattr(extension, "bind_bilibot_host", None)
                if callable(binder):
                    await _maybe_await(binder(host_api))
                discovered[extension_id] = RegisteredExtension(extension_id, extension, manifest, host_api)
            except asyncio.CancelledError:
                if host_api is not None:
                    host_api.revoke()
                raise
            except Exception as exc:
                if host_api is not None:
                    host_api.revoke()
                logger.warning(f"[BiliBot Extensions] skipped incompatible extension: {exc}")

        for extension_id, record in self.extensions.items():
            replacement = discovered.get(extension_id)
            if replacement is None or replacement.plugin is not record.plugin:
                await self._unbind(record)
        self.extensions = discovered
        return discovered

    async def get(self, extension_id: str) -> RegisteredExtension:
        await self.refresh()
        try:
            return self.extensions[extension_id]
        except KeyError as exc:
            raise KeyError(f"BiliBot extension not found: {extension_id}") from exc

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            for record in list(self.extensions.values()):
                await self._unbind(record)
            self.extensions = {}
