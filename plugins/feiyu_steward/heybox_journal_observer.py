"""Observe already successful public-post reads without changing their flow."""

import asyncio
import importlib
import math
import re
import time


def _public_snapshot(result, notification):
    """Copy bounded original-post evidence, excluding private runtime details."""
    thread = getattr(result, "thread", None)
    post_id = getattr(thread, "post_id", "")
    if not isinstance(post_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", post_id):
        return None
    expected_id = getattr(notification, "post_id", post_id)
    if str(expected_id) != post_id:
        return None
    body = getattr(thread, "body", None)
    if not isinstance(body, str) or not body.strip():
        return None

    def bounded_text(name, limit):
        value = getattr(thread, name, "")
        return value[:limit] if isinstance(value, str) else ""

    created = getattr(thread, "post_created_at", 0)
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        created = 0
    if not math.isfinite(created) or created < 0:
        created = 0
    return {
        "ok": True,
        "source": "heybox",
        "url": "https://example.invalid/configure-endpoint" + post_id,
        "post_id": post_id,
        "title": bounded_text("title", 200),
        "text": body[:12000],
        "truncated": len(body) > 12000,
        "read_basis": ["post_body"],
        "author": bounded_text("author_name", 120),
        "author_uid": bounded_text("author_uid", 80),
        "post_created_at": created,
        "observed_at": time.time(),
        "scope": "实际返回的原帖正文，最多12000字；未将评论或未识别图片当作正文证据。",
    }


def install_heybox_journal_observer(plugin, capture):
    """Return a reversible observer for this plugin's ContextBuilder class.

    The installed plugin keeps builders on platform adapter instances, rather
    than on the plugin or runtime. Wrapping its own builder class also covers
    instances created later by cold start or an adapter reconnect. The hook
    only copies a successful BuiltContext.thread to an async local callback;
    it never requests content, generates a note, or sends a message.
    """
    if not callable(capture):
        raise TypeError("capture must be an async callback")
    package = plugin.__class__.__module__.rsplit(".", 1)[0]
    module = importlib.import_module(package + ".xiaoheihe.context_builder")
    builder_class = module.ContextBuilder
    original = builder_class.build
    active = True

    async def observed_build(builder, *args, **kwargs):
        result = await original(builder, *args, **kwargs)
        if not active:
            return result
        try:
            notification = args[0] if args else kwargs.get("notification")
            doc = _public_snapshot(result, notification)
            if doc is not None:
                await capture(doc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001,S110 - Optional capture must not expose runtime internals or break reads.
            # Local evidence capture must not break a native read/comment.
            # Errors can contain public text or runtime internals: do not log.
            pass
        return result

    builder_class.build = observed_build

    def restore():
        nonlocal active
        active = False
        if builder_class.build is observed_build:
            builder_class.build = original

    return restore
