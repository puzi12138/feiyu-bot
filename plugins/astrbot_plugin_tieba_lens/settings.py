"""配置校验与公开配置视图。"""
from __future__ import annotations

import copy
import re

DEFAULTS = {
    "enabled": True, "auto_parse": True, "allow_private": True,
    "output_mode": "auto", "forward_original_images": False, "theme": "denia",
    "font_override": "lxgw", "comment_limit": 12,
    "render_backend": "auto", "browser_engine": "headless",
    "subcomment_limit": 6, "forum_thread_limit": 10, "sort": "hot",
    "only_author": False, "include_images": True, "include_emoji": True,
    "image_limit": 40, "image_width": 960, "page_height": 3000,
    "max_pages": 12, "show_source": True, "single_image": True,
    "reactions": True,
    "reaction_start": "289", "reaction_done": "124", "reaction_failed": "38",
    "blacklist": [],
    "group_overrides": {}, "dedup_seconds": 120, "cooldown_seconds": 5,
    "cache_minutes": 30, "retention_hours": 24, "max_cache_mb": 256,
    "request_timeout": 20, "request_delay": 1.0, "retries": 1,
    "max_concurrent": 2, "cookie": "", "max_links": 3,
}
RANGES = {
    "comment_limit": (0, 100), "subcomment_limit": (0, 20),
    "forum_thread_limit": (1, 30), "image_limit": (0, 100),
    "image_width": (640, 1200), "page_height": (1600, 5000),
    "max_pages": (1, 30), "dedup_seconds": (0, 3600),
    "cooldown_seconds": (0, 300), "cache_minutes": (0, 1440),
    "retention_hours": (1, 168), "max_cache_mb": (32, 2048),
    "request_timeout": (5, 60), "request_delay": (0.5, 10),
    "retries": (0, 3), "max_concurrent": (1, 4), "max_links": (1, 5),
}
ENUMS = {"output_mode": {"auto", "card", "forward"},
         "render_backend": {"auto", "service", "local"},
         "browser_engine": {"headless", "chromium"},
         "theme": {"paper", "midnight", "mist", "denia", "miku", "bilibili"},
         "font_override": {"lxgw", "custom"},
         "sort": {"default", "latest", "hot"}}
GROUP_KEYS = frozenset({"enabled", "auto_parse", "output_mode", "forward_original_images", "theme",
    "font_override", "single_image",
    "comment_limit", "subcomment_limit", "forum_thread_limit", "sort",
    "only_author", "include_images", "include_emoji", "image_limit",
    "show_source", "reactions", "cooldown_seconds", "dedup_seconds"})
GROUP_ID = re.compile(r"[1-9][0-9]{3,19}\Z")
THEME_ALIASES = {"daniya": "denia", "danyia": "denia"}
LEGACY_FONTS = frozenset({"auto", "system", "runyuan", "tangmeiren"})


def validate_patch(patch: dict, *, group: bool = False) -> dict:
    """拒绝拼错的键、类型和越界值，避免无效配置被悄悄接受。"""
    if not isinstance(patch, dict):
        raise ValueError("设置必须是 JSON 对象")
    out = {}
    for key, value in patch.items():
        if key == "cookie_set":
            continue
        if key not in DEFAULTS or (group and key not in GROUP_KEYS):
            raise ValueError(f"不支持的设置项：{key}")
        default = DEFAULTS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是开关值")
        elif key in RANGES:
            low, high = RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} 必须是数字")
            if key != "request_delay" and not isinstance(value, int):
                raise ValueError(f"{key} 必须是整数")
            if not low <= value <= high:
                raise ValueError(f"{key} 应在 {low}～{high} 之间")
        elif key in ENUMS:
            if key == "theme" and isinstance(value, str):
                value = THEME_ALIASES.get(value, value)
            elif key == "font_override" and isinstance(value, str) and value in LEGACY_FONTS:
                value = "lxgw"
            if not isinstance(value, str) or value not in ENUMS[key]:
                raise ValueError(f"{key} 的选项无效")
        elif key.startswith("reaction_"):
            if not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{1,7}", str(value)):
                raise ValueError("贴表情 ID 应为数字")
            value = str(value)
        elif key == "blacklist":
            if not isinstance(value, list) or len(value) > 1000:
                raise ValueError("群黑名单必须是最多 1000 项的数组")
            value = list(dict.fromkeys(str(v).strip() for v in value))
            if any(not GROUP_ID.fullmatch(v) for v in value):
                raise ValueError("群号应为 4～20 位正整数")
        elif key == "group_overrides":
            if not isinstance(value, dict) or len(value) > 500:
                raise ValueError("分群设置必须是最多 500 项的对象")
            groups = {}
            for gid, override in value.items():
                if not GROUP_ID.fullmatch(str(gid)):
                    raise ValueError(f"群号无效：{gid}")
                groups[str(gid)] = validate_patch(override, group=True)
            value = groups
        elif key == "cookie":
            if not isinstance(value, str) or len(value) > 16384 or any(c in value for c in "\r\n\x00"):
                raise ValueError("Cookie 格式无效")
            value = value.strip()
        out[key] = value
    return out


def effective_settings(config: dict, group_id: str = "") -> dict:
    result = copy.deepcopy(DEFAULTS)
    for key in DEFAULTS:
        if key in config:
            try:
                result.update(validate_patch({key: config[key]}))
            except ValueError:
                pass
    if group_id:
        result.update(result["group_overrides"].get(str(group_id), {}))
    return result


def public_settings(settings: dict) -> dict:
    result = copy.deepcopy(settings)
    result["cookie_set"] = bool(result.pop("cookie", ""))
    return result
