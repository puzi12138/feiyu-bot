"""会话差异配置管理器。"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from astrbot.api import logger


class SessionOverrideManager:
    """负责会话级差异配置的加载、存储与合并。"""

    OVERRIDES_FILE = "session_overrides.json"

    # 仅允许会话覆写这些根字段，避免污染全局配置
    ALLOWED_ROOT_KEYS = {
        "enable",
        "session_name",
        "proactive_prompt",
        "context_settings",
        "auto_trigger_settings",
        "schedule_settings",
        "tts_settings",
        "segmented_reply_settings",
        "group_idle_trigger_minutes",
    }

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.overrides_file = self.storage_dir / self.OVERRIDES_FILE
        self._overrides: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _ensure_storage_dir(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        self._ensure_storage_dir()

        if not self.overrides_file.exists():
            self._overrides = {}
            return

        try:
            with self.overrides_file.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._overrides = {
                    str(k): v for k, v in raw.items() if isinstance(v, dict)
                }
            else:
                self._overrides = {}
        except Exception as e:
            logger.warning(f"[主动消息] 读取会话差异配置失败喵，将使用空配置: {e}")
            self._overrides = {}

    async def _save(self) -> None:
        """异步保存会话差异配置到磁盘，避免阻塞事件循环。"""

        self._ensure_storage_dir()
        temp_file = self.overrides_file.with_suffix(self.overrides_file.suffix + ".tmp")
        # 由于这里是在类内多次修改同一份字典，安全起见先做一次深拷贝防止序列化期间被并发修改
        snap_overrides = copy.deepcopy(self._overrides)

        def _do_write():
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump(snap_overrides, f, ensure_ascii=False, indent=2)
            temp_file.replace(self.overrides_file)

        try:
            await asyncio.to_thread(_do_write)
        except Exception as e:
            logger.error(f"[主动消息] 保存会话差异配置失败喵: {e}")
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass

    def list_sessions(self) -> list[str]:
        return sorted(self._overrides.keys())

    def get_override(self, session_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._overrides.get(session_id, {}))

    async def set_override(
        self, session_id: str, override_patch: dict[str, Any]
    ) -> None:
        if not isinstance(override_patch, dict):
            raise ValueError("override_patch 必须是对象")

        patch = self._sanitize_patch(copy.deepcopy(override_patch))
        async with self._lock:
            if patch:
                self._overrides[session_id] = patch
            else:
                self._overrides.pop(session_id, None)

            await self._save()

    async def delete_override(self, session_id: str) -> None:
        async with self._lock:
            self._overrides.pop(session_id, None)
            await self._save()

    def get_effective(
        self, session_id: str, base_config: dict[str, Any] | None
    ) -> dict[str, Any]:
        base = copy.deepcopy(base_config or {})
        override = self._overrides.get(session_id, {})
        return self.deep_merge(base, override)

    async def update_session_from_effective(
        self,
        session_id: str,
        base_config: dict[str, Any],
        effective_config: dict[str, Any],
    ) -> None:
        if not isinstance(effective_config, dict):
            raise ValueError("effective_config 必须是对象")

        sanitized_base = self._sanitize_patch(copy.deepcopy(base_config)) or {}
        sanitized_effective = (
            self._sanitize_patch(copy.deepcopy(effective_config)) or {}
        )

        patch = self.compute_diff(sanitized_base, sanitized_effective)
        patch = self._sanitize_patch(patch)
        await self.set_override(session_id, patch or {})

    @classmethod
    def deep_merge(cls, base: Any, patch: Any) -> Any:
        if isinstance(base, dict) and isinstance(patch, dict):
            merged = copy.deepcopy(base)
            for key, value in patch.items():
                if key in merged:
                    merged[key] = cls.deep_merge(merged[key], value)
                else:
                    merged[key] = copy.deepcopy(value)
            return merged

        return copy.deepcopy(patch)

    @classmethod
    def compute_diff(cls, default_obj: Any, target_obj: Any) -> Any:
        if isinstance(default_obj, dict) and isinstance(target_obj, dict):
            result: dict[str, Any] = {}
            for key, value in target_obj.items():
                if key not in default_obj:
                    result[key] = copy.deepcopy(value)
                    continue

                diff_val = cls.compute_diff(default_obj[key], value)
                if diff_val is not None:
                    result[key] = diff_val

            return result if result else None

        if default_obj != target_obj:
            return copy.deepcopy(target_obj)

        return None

    def _sanitize_patch(self, patch: Any, depth: int = 0) -> Any:
        if patch is None:
            return None

        if not isinstance(patch, dict):
            return patch

        sanitized: dict[str, Any] = {}
        for key, value in patch.items():
            if depth == 0 and key not in self.ALLOWED_ROOT_KEYS:
                continue
            child = self._sanitize_patch(value, depth + 1)
            if isinstance(child, dict) and not child:
                continue
            if child is not None:
                sanitized[key] = child

        return sanitized
