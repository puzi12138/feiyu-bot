"""Validated Host-to-extension request dispatcher."""
from __future__ import annotations

import inspect
import asyncio
from copy import deepcopy
from typing import Any
from uuid import uuid4

from .contracts import validate_page_schema
from .registry import ExtensionRegistry

REQUEST_TIMEOUT_SECONDS = 30.0


class ExtensionDispatcher:
    def __init__(self, registry: ExtensionRegistry):
        self.registry = registry

    async def list_extensions(self) -> list[dict[str, Any]]:
        records = await self.registry.refresh()
        return [deepcopy(record.manifest) for record in records.values()]

    async def dispatch(
        self,
        extension_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = await self.registry.get(extension_id)
        if operation.startswith("page:"):
            allowed = {f"page:{page['id']}" for page in record.manifest["pages"]}
        elif operation.startswith("action:"):
            allowed = {f"action:{action['id'] if isinstance(action, dict) else action}" for action in record.manifest["actions"]}
        else:
            allowed = {"health"}
        if operation not in allowed:
            raise ValueError("extension operation is not declared in its manifest")
        request_id = uuid4().hex
        request = {
            "request_id": request_id,
            "operation": operation,
            "payload": dict(payload or {}),
            "actor": dict(actor or {}),
        }
        response = record.plugin.handle_bilibot_extension_request(request)
        if inspect.isawaitable(response):
            response = await asyncio.wait_for(response, timeout=REQUEST_TIMEOUT_SECONDS)
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise RuntimeError("extension returned an invalid response envelope")
        if not isinstance(response.get("ok"), bool):
            raise RuntimeError("extension response is missing ok")
        if response.get("ok") and operation.startswith("page:"):
            page = (response.get("data") or {}).get("page")
            validate_page_schema(page)
        return response
