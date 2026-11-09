"""Reversible, late-bound guards for known automatic plugin entry points.

This module never imports or starts another plugin.  ``refresh`` attaches to
classes already loaded by AstrBot, including replacements after a plugin
reload.  Per-request checks in those plugins are still needed for a single
round which crosses the tariff boundary while it is already in progress.
"""

from __future__ import annotations

import functools
import inspect
import sys
from datetime import timedelta

from .peak_runtime import automatic_peak_paused, automatic_scope, business_paused
from . import heybox_peak


# (module, class, method, mode, result when skipped)
# Scope-only wrappers must never skip a long-running loop at its entry: its
# individual cycles or network requests are guarded separately.
_TARGETS = (
    ("astrbot_plugin_xiaoheihe_adapter.xiaoheihe.adapter", "XiaoheihePlatformAdapter", "_feed_loop", "scope", None),
    ("astrbot_plugin_xiaoheihe_adapter.xiaoheihe.notification_service", "NotificationService", "_poll_loop", "scope", None),
    ("astrbot_plugin_xiaoheihe_adapter.xiaoheihe.notification_service", "NotificationService", "_worker", "scope", None),
    ("astrbot_plugin_xiaoheihe_adapter.xiaoheihe.notification_service", "NotificationService", "poll_once", "automatic", None),
    ("astrbot_plugin_xiaoheihe_adapter.xiaoheihe.notification_service", "NotificationService", "_poll_event_type", "automatic", None),
    ("astrbot_plugin_xiaoheihe_adapter.xiaoheihe.notification_service", "NotificationService", "_poll_backfill", "automatic", None),
    ("astrbot_plugin_xiaoheihe_adapter.xiaoheihe.feed_service", "FeedService", "run_once", "heybox_feed", 0),
    ("astrbot_plugin_xiaoheihe_adapter.xiaoheihe.feed_service", "FeedService", "_load_recommendation_posts", "heybox_feed", []),
    ("astrbot_plugin_bilibili_ai_bot.main", "BiliBiliBot", "_main_loop", "scope", None),
    ("astrbot_plugin_bilibili_ai_bot.core.reply", "ReplyMixin", "_poll_unified", "automatic", None),
    ("astrbot_plugin_bilibili_ai_bot.core.private_messages", "PrivateMessageMixin", "_poll_private_messages", "automatic", None),
    ("astrbot_plugin_private_companion.proactive", "ProactiveMixin", "_scheduler_loop", "scope", None),
    ("astrbot_plugin_private_companion.proactive", "ProactiveMixin", "_run_scheduler_cycle", "automatic", None),
    ("astrbot_plugin_private_companion.daily_state", "DailyStateMixin", "_tick", "automatic", None),
    ("astrbot_plugin_private_companion.daily_state_tick", "DailyStateTickMixin", "_tick_user", "automatic", None),
    ("astrbot_plugin_private_companion.core_store", "CoreStoreMixin", "_startup_prepare_today", "automatic", None),
    ("astrbot_plugin_private_companion.daily_review", "DailyReviewMixin", "_daily_review_loop", "scope", None),
    ("astrbot_plugin_private_companion.daily_review", "DailyReviewMixin", "_ensure_daily_review", "business", None),
    ("astrbot_plugin_virtual_life.main", "ProactiveVirtualDailyPlugin", "_daily_refresh", "automatic", None),
    ("astrbot_plugin_virtual_life.main", "ProactiveVirtualDailyPlugin", "_refresh_all", "business", None),
    ("astrbot_plugin_virtual_life.main", "ProactiveVirtualDailyPlugin", "_run_window", "automatic", None),
    ("astrbot_plugin_virtual_life.main", "ProactiveVirtualDailyPlugin", "_run_delayed_window", "automatic", None),
    ("astrbot_plugin_virtual_life.main", "ProactiveVirtualDailyPlugin", "_run_sleep", "automatic", None),
    ("astrbot_plugin_virtual_life.main", "ProactiveVirtualDailyPlugin", "_run_idle", "automatic_idle", None),
    ("astrbot_plugin_virtual_life.main", "ProactiveVirtualDailyPlugin", "_attempt_unsolicited", "business", (False, "peak paused")),
)


def _paused(mode):
    try:
        if mode in {"automatic", "automatic_idle"}:
            return automatic_peak_paused() is not False
        return business_paused() is not False
    except Exception:
        return True


class PeakBackgroundHandle:
    """Own and restore only the wrappers installed by this handle."""

    def __init__(self, host):
        self.host = host
        self._active = True
        self._bindings = []
        self._missing = []
        self.refresh()

    def _wrap(self, cls, name, mode, skipped):
        original = cls.__dict__.get(name)
        state = getattr(original, "_peak_background_state", None)
        if isinstance(state, dict):
            if (state.get("owner") is self and state.get("active")
                    and state.get("wrapped") is original):
                return True
            restore = state.get("restore")
            if callable(restore):
                restore()
            original = cls.__dict__.get(name)
        if not inspect.iscoroutinefunction(original):
            return False

        @functools.wraps(original)
        async def wrapped(instance, *args, **kwargs):
            if mode == "scope":
                with automatic_scope():
                    return await original(instance, *args, **kwargs)
            if mode == "heybox_feed":
                with automatic_scope():
                    if _paused("automatic") and not heybox_peak.feed_active(instance):
                        return list(skipped) if isinstance(skipped, list) else skipped
                    return await original(instance, *args, **kwargs)
            if mode in {"automatic", "automatic_idle"}:
                with automatic_scope():
                    if _paused(mode):
                        if mode == "automatic_idle":
                            umo = args[0] if args else kwargs.get("umo")
                            if umo:
                                instance._schedule_idle(
                                    umo, run_at=instance._now() + timedelta(minutes=30)
                                )
                        return list(skipped) if isinstance(skipped, list) else skipped
                    return await original(instance, *args, **kwargs)
            if _paused(mode):
                return list(skipped) if isinstance(skipped, list) else skipped
            return await original(instance, *args, **kwargs)

        state = {"owner": self, "active": True}

        def restore():
            state["active"] = False
            if cls.__dict__.get(name) is wrapped:
                setattr(cls, name, original)

        state["restore"] = restore
        state["wrapped"] = wrapped
        wrapped._peak_background_state = state
        setattr(cls, name, wrapped)
        self._bindings.append((cls, name, original, wrapped, state))
        return True

    def refresh(self):
        """Attach to loaded targets and report any unavailable target names."""

        if not self._active:
            return self.status()
        missing = []
        for module_name, class_name, method_name, mode, skipped in _TARGETS:
            module = sys.modules.get("data.plugins." + module_name)
            cls = getattr(module, class_name, None) if module else None
            if cls is None or not self._wrap(cls, method_name, mode, skipped):
                missing.append(f"{module_name}:{class_name}.{method_name}")
        self._missing = missing
        return self.status()

    def status(self):
        # Do not claim readiness solely from the last refresh: an outer plugin
        # can replace a binding between refresh cycles.
        missing = set(self._missing)
        for module_name, class_name, method_name, _, _ in _TARGETS:
            module = sys.modules.get("data.plugins." + module_name)
            cls = getattr(module, class_name, None) if module else None
            current = cls.__dict__.get(method_name) if cls else None
            if not any(obj is cls and name == method_name and wrapped is current and state["active"]
                       for obj, name, _, wrapped, state in self._bindings):
                missing.add(f"{module_name}:{class_name}.{method_name}")
        return {
            "installed": self._active,
            "automatic_paused": _paused("automatic"),
            "bindings": sum(1 for cls, name, _, wrapped, state in self._bindings
                            if state["active"] and cls.__dict__.get(name) is wrapped),
            "missing_targets": sorted(missing),
        }

    def restore(self):
        if not self._active:
            return self.status()
        self._active = False
        for _, _, _, _, state in reversed(self._bindings):
            state["restore"]()
        return self.status()


def install_peak_background(host):
    """Install non-networking guards; caller owns refresh and restoration."""

    return PeakBackgroundHandle(host)
