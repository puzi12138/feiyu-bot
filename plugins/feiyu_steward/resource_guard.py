"""Bind expensive operations to the real QQ owner, at execution time too.

This does not classify ordinary discussion as a generation request. Native
command handlers, native media entry points and the actual tool executor are
guarded independently because hiding tools from a prompt is not an ACL.
"""

import asyncio
import copy
import functools
import re
import sys
import time
from contextlib import aclosing
from contextvars import ContextVar

try:
    # The peak-runtime candidate owns the strict owner-extra deadline.  It
    # cancels the current task instead of wrapping the operation in a child
    # task, so capability checks bound to asyncio.current_task() survive.
    from .peak_runtime import same_task_deadline
except (ImportError, ModuleNotFoundError):
    try:
        from peak_runtime import same_task_deadline  # type: ignore
    except (ImportError, ModuleNotFoundError):
        same_task_deadline = None

OWNER = "0"
OWNER_UMO = f"snowluma-qq:FriendMessage:{OWNER}"
GROUPS = frozenset({"0", "0"})
OWNER_ONLY = "owner_resource_only"

_OWNER_PHOTO_TIMEOUT_SECONDS = 660
_owner_photo_grant = ContextVar("owner_photo_timeout_grant", default=None)


class _OwnerPhotoTimeoutGrant:
    """Revocable, event-bound deadline shared by descendant tasks."""

    __slots__ = ("event", "deadline", "active")

    def __init__(self, event, timeout):
        self.event = event
        self.deadline = time.monotonic() + timeout
        self.active = True


def owner_photo_timeout_budget(event):
    """Return the current verified owner photo call's remaining seconds."""
    grant = _owner_photo_grant.get()
    if (grant is None or not grant.active or grant.event is not event
            or not owner_resource_identity(event)):
        return None
    remaining = grant.deadline - time.monotonic()
    return remaining if remaining > 0 else None

_DIRECT_COMMAND = re.compile(
    r"^(?:[/／#]\s*)?(?:翻唱(?:下载|模型|状态|发群)?|点歌翻唱|"
    r"生成图片|生成图像|画图|绘图|文生图|图生图|语音合成|合成语音|"
    r"语音转文字|音频转文字|视频深读|视频分析|音画分析|bili解析|bili看番|"
    r"(?:陪伴|私聊陪伴|主动陪伴)\s+(?:生图|画图|绘图|生成图片|出图|自拍|"
    r"拍照|拍一张|改图|修图|重绘|p图))"
    r"(?:\s|$)", re.IGNORECASE)
_RESOURCE_TOOL = re.compile(
    r"(?:^|_)(?:cover|rvc|tts|asr|stt|sovits|imagegen|txt2img|img2img|"
    r"transcribe|transcription|screenshot|cua|shell|powershell|terminal|"
    r"ipython|python|subagent|subagents|handoff)(?:_|$)"
    r"|(?:^|_)(?:generate|create|synthesize|render|draw)_(?:image|images|picture|"
    r"pictures|photo|photos|selfie|sticker|audio|voice|speech|music|video)(?:_|$)"
    r"|(?:^|_)(?:image|photo|selfie|sticker|audio|voice|speech|music|video)_(?:generate|generation|"
    r"synthesis|render|transcribe|transcription|analyze|analysis)(?:_|$)"
    r"|(?:^|_)(?:text_to_speech|speech_to_text|voice_conversion|video_media|"
    r"speak_private|heybox_post|watch_video|watch_videos|watch_bangumi|"
    r"watch_and_share_video_private|bili_parse_video|execute_browser|"
    r"execute_browser_batch|execute_shell|execute_python|execute_ipython|"
    r"run_browser_skill|spawn_agent|delegate_task|transfer_to)(?:_|$)", re.IGNORECASE)


def owner_resource_identity(event):
    """Require the exact trusted route and agreement of available raw IDs."""
    try:
        origin = str(getattr(event, "unified_msg_origin", ""))
        if str(event.get_sender_id()) != OWNER:
            return False
        route = origin.split(":")
        if len(route) != 3 or route[0] != "snowluma-qq":
            return False
        private = origin == OWNER_UMO
        group = route[2] if route[1] == "GroupMessage" and route[2] in GROUPS else ""
        if not private and not group:
            return False
        event_group = str(getattr(event, "get_group_id", lambda: "")() or "")
        if (private and event_group) or (group and event_group and event_group != group):
            return False
        message = getattr(event, "message_obj", None)
        object_group = str(getattr(message, "group_id", "") or "")
        if (private and object_group) or (group and object_group and object_group != group):
            return False
        sender = getattr(message, "sender", None)
        if sender is not None and str(getattr(sender, "user_id", "")) != OWNER:
            return False
        raw = getattr(message, "raw_message", None)
        if isinstance(raw, dict) and raw:
            expected_type = "private" if private else "group"
            if raw.get("message_type", expected_type) != expected_type:
                return False
            if raw.get("group_id") and str(raw["group_id"]) != group:
                return False
            if raw.get("user_id") is not None and str(raw["user_id"]) != OWNER:
                return False
            raw_sender = raw.get("sender")
            if (isinstance(raw_sender, dict) and raw_sender.get("user_id") is not None
                    and str(raw_sender["user_id"]) != OWNER):
                return False
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def resource_command_request(text):
    """Recognize direct registered commands, not talk about songs or drawing."""
    text = re.sub(r"\[CQ:at,[^\]]*\]", "", str(text or ""), flags=re.IGNORECASE)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", text).strip()
    text = re.sub(r"^(?:大肥鱼|肥鱼|bot)[,，:：\s]*", "", text, flags=re.IGNORECASE)
    return bool(_DIRECT_COMMAND.match(text))


def resource_tool_block(event, tool_name, args=None):
    """Last-execution gate; a model's arguments never grant owner authority."""
    if owner_resource_identity(event):
        return ""
    name = re.sub(r"[^a-z0-9]+", "_", str(tool_name).lower()).strip("_")
    if _RESOURCE_TOOL.search(name):
        return OWNER_ONLY
    # The combined tool has lightweight search/info actions too. Hide the
    # tool when preparing the request, but allow safe direct executions.
    if (re.search(r"(?:^|_)bili_bangumi(?:_|$)", name)
            and (args is None or str(args.get("action", "")).lower() == "watch")):
        return OWNER_ONLY
    return ""


def _mark(event, reason=OWNER_ONLY):
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        setter("feiyu_resource_block", reason)


class ResourceBoundary:
    """Mix-in for the early observer and the final model request hook."""

    async def run_authorized_public_art(self, slot_key, authorization, operation):
        """One bounded background-art operation under its own revocable grant.

        This is an internal entry point, not an owner event or an LLM tool. The
        worker creates the one-use ticket only after reserving its schedule slot.
        The existing event-bound owner checks remain unchanged.
        """
        ticket = getattr(self, "_public_art_resource_ticket", None)
        valid = getattr(self, "_autopost_generation_authorized", lambda *_: False)
        if (not isinstance(ticket, dict) or ticket.get("slot_key") != slot_key
                or ticket.get("used") is not False or not callable(operation)
                or getattr(self, "_public_art_resource_busy", False)
                or not valid(slot_key, authorization)):
            return {"ok": False, "reason": "public_art_guard_denied"}
        ticket["used"] = True
        self._public_art_resource_busy = True
        try:
            # No retry or fallback: a timeout after submission is unknown.
            if str(slot_key).endswith("/owner_extra_ai_friends"):
                if same_task_deadline is None:
                    # A missing helper is a closed state for the owner-extra
                    # path; do not fall back to wait_for (which changes task
                    # identity and could bypass the short-lived capability).
                    return {"ok": False, "reason": "public_art_guard_denied"}
                async with same_task_deadline(600):
                    return await operation()
            return await asyncio.wait_for(operation(), timeout=600)
        except asyncio.TimeoutError:
            return {"ok": False, "reason": "public_art_timeout_unknown"}
        finally:
            self._public_art_resource_busy = False

    def prepare_resource_request(self, event):
        if owner_resource_identity(event) or not resource_command_request(
                getattr(event, "message_str", "")):
            return False
        _mark(event)
        event.stop_event()
        return True

    def apply_resource_context(self, event, req):
        if owner_resource_identity(event):
            return
        req.system_prompt = str(getattr(req, "system_prompt", "") or "") + (
            '请仅依据当前公开来源及本地明确配置的信息处理请求；不要推测个人身份或私人偏好。'
        )
        tools = getattr(req, "func_tool", None)
        items = getattr(tools, "tools", None)
        if isinstance(items, list):
            selected = [tool for tool in items
                        if not resource_tool_block(event, getattr(tool, "name", ""))]
            if len(selected) != len(items):
                if selected:
                    filtered = copy.copy(tools)
                    filtered.tools = selected
                    req.func_tool = filtered
                else:
                    req.func_tool = None


def install_resource_tool_guard(executor=None):
    """Guard before background/MCP dispatch; restore only our own wrapper."""
    if executor is None:
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

        executor = FunctionToolExecutor
    descriptor = executor.__dict__.get("execute")
    if not isinstance(descriptor, classmethod):
        raise TypeError("resource_tool_executor_unsupported")
    original = descriptor.__get__(None, executor)

    @functools.wraps(original)
    async def guarded(cls, tool, run_context, **tool_args):
        event = getattr(getattr(run_context, "context", None), "event", None)
        reason = resource_tool_block(event, getattr(tool, "name", ""), tool_args)
        if reason:
            from mcp.types import CallToolResult, TextContent

            _mark(event)
            yield CallToolResult(content=[TextContent(
                type="text", text='{"ok":false,"reason":"owner_resource_only"}')], isError=True)
            return
        execution_context = run_context
        owner_photo = (getattr(tool, "name", "") == "pc_generate_photo"
                       and owner_resource_identity(event))
        if owner_photo:
            # Local GPU generation needs headroom without changing sibling tools.
            execution_context = copy.copy(run_context)
            execution_context.tool_call_timeout = max(
                getattr(run_context, "tool_call_timeout", 120),
                _OWNER_PHOTO_TIMEOUT_SECONDS)
        grantable = owner_photo and not getattr(tool, "is_background_task", False)
        if grantable:
            # These executor branches do not use the local tool timeout.
            for module_name, type_name in (
                    ("astrbot.core.agent.mcp_client", "MCPTool"),
                    ("astrbot.core.agent.handoff", "HandoffTool")):
                tool_type = getattr(sys.modules.get(module_name), type_name, None)
                if isinstance(tool_type, type) and isinstance(tool, tool_type):
                    grantable = False
                    break
        async with aclosing(original(tool, execution_context, **tool_args)) as results:
            # Background work outlives this generator and has its own timeout;
            # MCP and handoff follow separate execution paths.
            if not grantable:
                async for item in results:
                    yield item
                return
            while True:
                grant = _OwnerPhotoTimeoutGrant(event, execution_context.tool_call_timeout)
                token = _owner_photo_grant.set(grant)
                try:
                    item = await anext(results)
                except StopAsyncIteration:
                    break
                finally:
                    # Descendant tasks can retain the context, so revoke the
                    # shared grant before resetting this task's context.
                    grant.active = False
                    _owner_photo_grant.reset(token)
                # No ContextVar token spans a yield into a different task.
                yield item

    installed = classmethod(guarded)
    executor.execute = installed

    def restore():
        if executor.__dict__.get("execute") is installed:
            executor.execute = descriptor

    return restore


def install_native_resource_guard(host):
    """Guard native command media entry points, including automatic parsing.

    Registered handlers look up these instance methods at run time. Cover and
    Bilibili parsing use async generators; companion photo entry points are
    coroutines. Non-owner links and ordinary companion matching return without
    stopping the event, so the conversation pipeline may continue. Autonomous
    Bilibili browsing does not use _handle_bili_share and continues.
    """
    bindings = []
    resolver = getattr(host, "plugin", None)
    if not callable(resolver):
        raise TypeError("resource_native_plugin_resolver_missing")

    def bind(plugin, method):
        original = getattr(plugin, method, None)
        if not callable(original):
            return

        @functools.wraps(original)
        async def guarded(event, *args, **kwargs):
            if not owner_resource_identity(event):
                _mark(event)
                return
            async with aclosing(original(event, *args, **kwargs)) as results:
                async for item in results:
                    yield item

        setattr(plugin, method, guarded)
        bindings.append((plugin, method, original, guarded))

    def bind_coroutine(plugin, method, denied_result, *, stop=False):
        original = getattr(plugin, method, None)
        if not callable(original):
            return

        @functools.wraps(original)
        async def guarded(event, *args, **kwargs):
            if not owner_resource_identity(event):
                _mark(event)
                if stop:
                    event.stop_event()
                return denied_result
            if method != "_pc_generate_photo_impl":
                return await original(event, *args, **kwargs)

            inherited = _owner_photo_grant.get()
            if inherited is not None and inherited.event is event:
                remaining = owner_photo_timeout_budget(event)
                if remaining is None:
                    # Never renew an expired or revoked enclosing grant.
                    raise asyncio.TimeoutError
                return await asyncio.wait_for(
                    original(event, *args, **kwargs), timeout=remaining)

            # A direct native photo call gets its own real outer deadline.
            grant = _OwnerPhotoTimeoutGrant(event, _OWNER_PHOTO_TIMEOUT_SECONDS)
            token = _owner_photo_grant.set(grant)
            try:
                return await asyncio.wait_for(
                    original(event, *args, **kwargs), timeout=_OWNER_PHOTO_TIMEOUT_SECONDS)
            finally:
                grant.active = False
                _owner_photo_grant.reset(token)

        setattr(plugin, method, guarded)
        bindings.append((plugin, method, original, guarded))

    targets = (
        ("astrbot_plugin_ai_cover", "_run_cover"),
        ("astrbot_plugin_bilibili_ai_bot", "_handle_bili_share"),
        ("astrbot_plugin_private_companion", "_handle_companion_photo_command"),
        ("astrbot_plugin_private_companion", "_maybe_handle_natural_language_photo_request"),
        ("astrbot_plugin_private_companion", "_pc_generate_photo_impl"),
    )
    bind(resolver("astrbot_plugin_ai_cover"), "_run_cover")
    bind(resolver("astrbot_plugin_bilibili_ai_bot"), "_handle_bili_share")
    companion = resolver("astrbot_plugin_private_companion")
    bind_coroutine(companion, "_handle_companion_photo_command", True, stop=True)
    # This matcher runs for ordinary chat too. Returning False skips only its
    # image backend, allowing normal conversation and tool filtering to run.
    bind_coroutine(companion, "_maybe_handle_natural_language_photo_request", False)
    bind_coroutine(companion, "_pc_generate_photo_impl",
                   '{"ok":false,"reason":"owner_resource_only"}')

    def restore():
        for plugin, method, original, guarded in reversed(bindings):
            if getattr(plugin, method, None) is guarded:
                setattr(plugin, method, original)

    def is_current():
        # Plugin reloads replace instances without restarting steward. Late
        # plugin initialization also creates new targets after our first bind.
        current = [(resolver(folder), method) for folder, method in targets]
        current = [(plugin, method) for plugin, method in current
                   if callable(getattr(plugin, method, None))]
        if len(current) != len(bindings):
            return False
        return all(any(plugin is bound_plugin and method == bound_method
                       and getattr(plugin, method, None) is guarded
                       for bound_plugin, bound_method, _, guarded in bindings)
                   for plugin, method in current)

    restore.installed = tuple(method for _, method, _, _ in bindings)
    restore.is_current = is_current
    return restore
