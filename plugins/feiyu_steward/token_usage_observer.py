"""Count provider-reported usage without retaining prompts or message content.

The public wrappers are deliberately synchronous: attribution is captured at
the call site, before ``wait_for`` or a streaming consumer creates a new task.
"""

import asyncio
import contextvars
import inspect
import re
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from functools import wraps

from .token_usage_store import FEATURES, SCENES

_scene = contextvars.ContextVar("feiyu_usage_scene", default="other")
_scope = contextvars.ContextVar("feiyu_usage_scope", default=None)
_running = contextvars.ContextVar("feiyu_usage_running", default=frozenset())
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}\Z")
_MARKER = "_feiyu_usage_observer_state"
_SPAN_MARKER = "_feiyu_usage_observer_span"
_MISSING = object()
_LIMIT = 1_000_000_000


def set_usage_scene(scene):
    """Set a trusted enum string in this task; return its reset token."""
    return _scene.set(scene if isinstance(scene, str) and scene in SCENES else "other")


def reset_usage_scene(token):
    _scene.reset(token)


@contextmanager
def usage_scope(feature, scene="background"):
    """Precisely attribute a known local task, restoring its caller afterwards."""
    feature = feature if isinstance(feature, str) and feature in FEATURES else "other"
    scene = scene if isinstance(scene, str) and scene in SCENES else "other"
    token = _scope.set((feature, scene))
    try:
        yield
    finally:
        _scope.reset(token)


def _read(obj, name, default=None):
    try:
        return obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)
    except Exception:  # noqa: BLE001 - Optional numeric metadata must not break the provider call.
        return default


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _LIMIT else None


def _raw_usage(usage):
    """Normalize only true upstream usage fields, keeping cached input distinct."""
    if usage is None:
        return None
    prompt = _count(_read(usage, "prompt_tokens"))
    completion = _count(_read(usage, "completion_tokens"))
    if prompt is not None and completion is not None:
        cached = _count(_read(usage, "prompt_cache_hit_tokens"))
        if cached is None:
            cached = _count(_read(_read(usage, "prompt_tokens_details"), "cached_tokens"))
        cached = min(cached or 0, prompt)
        return prompt - cached, cached, completion
    input_tokens = _count(_read(usage, "input_tokens"))
    output_tokens = _count(_read(usage, "output_tokens"))
    if input_tokens is not None and output_tokens is not None:
        # Anthropic reports uncached input separately from cache creation/read.
        cache_read = _count(_read(usage, "cache_read_input_tokens"))
        cache_create = _count(_read(usage, "cache_creation_input_tokens"))
        if cache_read is not None or cache_create is not None:
            return min(_LIMIT, input_tokens + (cache_create or 0)), cache_read or 0, output_tokens
        cached = _count(_read(_read(usage, "input_tokens_details"), "cached_tokens")) or 0
        cached = min(cached, input_tokens)
        return input_tokens - cached, cached, output_tokens
    return None


def extract_usage(response):
    """Return (known, other, cached, output), without touching response text."""
    raw = _read(response, "raw_completion")
    normalized = _raw_usage(_read(raw, "usage"))
    if normalized is not None:
        return (True, *normalized)
    # Gemini's raw response uses usage_metadata instead of usage.
    metadata = _read(raw, "usage_metadata")
    prompt = _count(_read(metadata, "prompt_token_count"))
    completion = _count(_read(metadata, "candidates_token_count"))
    if prompt is not None and completion is not None:
        cached = min(_count(_read(metadata, "cached_content_token_count")) or 0, prompt)
        thinking = _count(_read(metadata, "thoughts_token_count")) or 0
        return True, prompt - cached, cached, min(_LIMIT, completion + thinking)
    usage = _read(response, "usage")
    counts = tuple(_count(_read(usage, key)) for key in ("input_other", "input_cached", "output"))
    if all(value is not None for value in counts) and any(counts):
        return (True, *counts)
    normalized = _raw_usage(usage)
    if normalized is not None and any(normalized):
        return (True, *normalized)
    # AstrBot can produce default all-zero normalized TokenUsage when upstream
    # usage is missing. Such zeros must not masquerade as measured zero usage.
    return False, 0, 0, 0


def _safe_identifier(value):
    return value if isinstance(value, str) and "://" not in value and _IDENTIFIER.fullmatch(value) else ""


def _metadata(provider, kwargs):
    try:
        meta = provider.meta()
    except Exception:  # noqa: BLE001 - Provider metadata is optional and never logged.
        meta = None
    provider_id = _safe_identifier(_read(meta, "id"))
    if not provider_id:
        provider_id = _safe_identifier(_read(_read(provider, "provider_config"), "id"))
    model = _safe_identifier(kwargs.get("model")) or _safe_identifier(_read(meta, "model"))
    if not model:
        model = _safe_identifier(_read(provider, "model_name"))
    return provider_id, model


def _attribution(kwargs):
    explicit = _scope.get()
    if explicit is not None:
        return explicit
    scene = _scene.get()
    # A fixed prefix is sufficient for image/context preprocessing; never copy
    # or parse the session value beyond these known implementation prefixes.
    session = kwargs.get("session_id")
    if isinstance(session, str):
        if session.startswith("xiaoheihe-image-"):
            return "heybox_vision", "heybox"
        if session.startswith("xiaoheihe-context-"):
            return "context_compression", "heybox"
    candidates = []
    route_scene = None
    main_runner = False
    frame = sys._getframe(1)
    try:
        for _ in range(40):
            if frame is None:
                break
            module = frame.f_globals.get("__name__", "")
            function = frame.f_code.co_name
            if not isinstance(module, str):
                module = ""
            # Cron and background-result wakes build/run an agent directly,
            # bypassing the pipeline's OnLLMRequest event-scene hook entirely.
            # Identify only these audited implementation routes; no event,
            # message, conversation, or arbitrary frame locals are inspected.
            if module == "astrbot.core.cron.manager" and function == "_woke_main_agent":
                route_scene = "cron"
            elif (module == "astrbot.core.astr_agent_tool_exec"
                  and function == "_wake_main_agent_for_background_result"
                  and route_scene is None):
                route_scene = "background"
                candidates.append("autonomous_plan")
            elif module == "astrbot.core.agent.runners.tool_loop_agent_runner":
                main_runner = True
            feature = None
            if "remember_you" in module:
                feature = "memory"
            elif ("context_compress" in module or "context_summar" in module
                  or ".context.compressor" in module):
                feature = "context_compression"
            elif "private_companion" in module and module.endswith(".tts_enhancement"):
                feature = "voice_text"
            elif "feiyu_steward" in module:
                leaf = module.rsplit(".", 1)[-1]
                feature = {
                    "journal_review": "journal_review", "journal_history": "journal_review",
                    "video_media": "video_media", "hltv": "hltv",
                    "comment_sharing": "comment_sharing", "qq_profile": "qq_profile_vision",
                    "proactive": "proactive_chat", "reconnect": "private_chat",
                }.get(leaf)
                if leaf == "main" and function == "candidate":
                    feature = "qzone_draft"
                elif leaf == "social":
                    feature = "heybox_vision"
            elif "meme_manager" in module:
                feature = "meme_analysis"
            elif "private_companion" in module:
                feature = "proactive_chat"
            elif "bilibili_ai_bot" in module:
                if ".media." in module:
                    feature = "video_media"
                elif "vision" in module:
                    feature = "bili_vision"
                elif function == "_task_llm_call":
                    # This is the only inspected local: a known implementation
                    # enum, never the prompt, text, kwargs, or arbitrary locals.
                    purpose = frame.f_locals.get("purpose")
                    if isinstance(purpose, str) and purpose == "SUMMARY":
                        feature = "bili_summary"
                    elif isinstance(purpose, str) and purpose == "GENERATION":
                        feature = "bili_comment"
                elif "schedule" in module and "plan" in function:
                    feature = "autonomous_plan"
                else:
                    feature = "bili_other"
            elif "astrbot_plugin_qzone" in module:
                feature = "qzone_draft" if function == "generate_post" else "qzone_reply"
            elif "xiaoheihe" in module:
                feature = "context_compression" if "compress" in module else "heybox_comment"
            if feature:
                candidates.append(feature)
            frame = frame.f_back
    finally:
        del frame
    if route_scene is not None:
        # The actual scheduled implementation path supersedes a ContextVar
        # inherited from the task which happened to create this cron job.
        scene = route_scene
    elif scene == "other" and main_runner and isinstance(session, str):
        # A main-runner fallback only consumes an existing transport-session
        # enum/known owner identity; the session itself never reaches records.
        if session == "snowluma-qq:FriendMessage:0":
            scene = "owner_private"
        elif re.fullmatch(r"snowluma-qq:GroupMessage:[1-9]\d{4,19}(?:![1-9]\d{4,19})?", session):
            scene = "group"
    if candidates:
        # Prefer a concrete inner operation over generic platform dispatch.
        specific = next((value for value in candidates if value not in {"heybox_comment", "bili_other"}), candidates[0])
        if scene == "other":
            scene = "heybox" if specific in {"heybox_comment", "heybox_vision"} else "background"
        return specific, scene
    if route_scene == "background":
        return "autonomous_plan", scene
    return {
        "owner_private": "private_chat", "group": "group_chat",
        "cron": "autonomous_plan", "heybox": "heybox_comment",
    }.get(scene, "other"), scene


def _chain(method):
    seen = set()
    for _ in range(24):
        if method is None or id(method) in seen:
            break
        seen.add(id(method))
        yield method
        method = _read(method, "__wrapped__")


def install_provider_usage_observer(provider, record_callback):
    """Observe one provider; return idempotent restore with ``is_current()``.

    Later legitimate wrappers are never overwritten. A retired observer is
    disabled for new calls even inside another wrapper's __wrapped__ chain.
    Already-started calls retain their receipt across restore/reinstallation.
    OpenAI completions are counted before parsing can reject a billed result.
    """
    if not callable(record_callback):
        raise TypeError("record_callback must be callable")
    originals = {}
    installed = {}
    state = {"active": True, "inflight": 0}
    for name in ("text_chat", "text_chat_stream"):
        method = getattr(provider, name, None)
        if callable(method):
            originals[name] = method
            for wrapper in _chain(method):
                prior = _read(wrapper, _MARKER)
                if isinstance(prior, dict):
                    prior["active"] = False
    if not originals:
        raise TypeError("provider has no chat methods")
    parser_name = "_parse_openai_completion"
    parser = getattr(provider, parser_name, None)
    if callable(parser):
        originals[parser_name] = parser
    # Keep this on the provider so an in-flight call and a newly imported
    # observer can share its numeric-only span during a hot plugin reload.
    span_context = getattr(provider, _SPAN_MARKER, None)
    if not isinstance(span_context, contextvars.ContextVar):
        span_context = contextvars.ContextVar("feiyu_provider_usage_span", default=None)
        setattr(provider, _SPAN_MARKER, span_context)

    def begin(kwargs):
        feature, scene = _attribution(kwargs)
        provider_id, model = _metadata(provider, kwargs)
        return {
            "request_id": str(uuid.uuid4()), "at": time.time(),
            "feature": feature, "scene": scene, "provider_id": provider_id, "model": model,
        }

    def finish(base, started, status, usage):
        known, other, cached, output = usage
        record = {
            **base, "status": status, "usage_known": known,
            "input_other": other, "input_cached": cached, "output": output,
            "elapsed_ms": min(86_400_000, max(0, int((time.monotonic() - started) * 1000))),
        }
        try:
            record_callback(record)
        except BaseException:  # noqa: BLE001,S110 - Optional telemetry must never alter provider semantics.
            pass

    def restore_methods():
        for name, wrapper in installed.items():
            # A request already sent before restore may still receive a
            # completion which fails parsing. Leave this hook until it ends.
            if name == parser_name and state["inflight"]:
                continue
            if getattr(provider, name, None) is wrapper:
                setattr(provider, name, originals[name])

    def new_span(base, started):
        state["inflight"] += 1
        return {"base": base, "started": started, "finish": finish,
                "attempts": 0, "seen": set(), "parse_depth": 0,
                "last_parse_error": None}

    def end_span(span, status, usage, error_id, *, final_received=False):
        try:
            if (not span["attempts"]
                    or (status != "success" and not final_received
                        and error_id != span["last_parse_error"])):
                # With parsed completions, their usage was already persisted.
                # A different later failure can still be a request without a
                # returned receipt; keep that amount unknown instead of zero.
                base = dict(span["base"])
                if span["attempts"]:
                    base["request_id"] = str(uuid.uuid4())
                finish(base, span["started"], status,
                       usage if not span["attempts"] else (False, 0, 0, 0))
        finally:
            state["inflight"] -= 1
            if not state["active"]:
                restore_methods()

    if parser_name in originals:
        original_parser = originals[parser_name]

        @wraps(original_parser)
        async def observed_parser(completion, tools, *args, **kwargs):
            span = span_context.get()
            if span is None or span["parse_depth"]:
                return await original_parser(completion, tools, *args, **kwargs)
            # Read only upstream usage and fixed model/response identifiers.
            # Some compatible APIs put the completion inside a data wrapper.
            raw = completion
            counts = _raw_usage(_read(raw, "usage"))
            if counts is None:
                wrapped = _read(completion, "data")
                wrapped_counts = _raw_usage(_read(wrapped, "usage"))
                if wrapped_counts is not None:
                    raw, counts = wrapped, wrapped_counts
            usage = (True, *counts) if counts is not None else (False, 0, 0, 0)
            response_id = _safe_identifier(_read(raw, "id"))
            model = _safe_identifier(_read(raw, "model"))
            duplicate = bool(response_id and response_id in span["seen"])
            if response_id:
                span["seen"].add(response_id)
            status, error_id = "failed", None
            span["parse_depth"] += 1
            try:
                result = await original_parser(completion, tools, *args, **kwargs)
                status = "success"
                return result
            except BaseException as error:
                error_id = id(error)
                if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                    status = "cancelled"
                raise
            finally:
                span["parse_depth"] -= 1
                span["last_parse_error"] = error_id
                if not duplicate:
                    base = dict(span["base"])
                    if span["attempts"]:
                        base["request_id"] = str(uuid.uuid4())
                    if model:
                        base["model"] = model
                    span["attempts"] += 1
                    span["finish"](base, span["started"], status, usage)

        setattr(observed_parser, _MARKER, state)
        installed[parser_name] = observed_parser

    if "text_chat" in originals:
        original_chat = originals["text_chat"]

        @wraps(original_chat)
        def observed_chat(*args, **kwargs):
            base = begin(kwargs)

            async def run():
                if (not state["active"] or id(provider) in _running.get()
                        or span_context.get() is not None):
                    return await original_chat(*args, **kwargs)
                token = _running.set(_running.get() | {id(provider)})
                started = time.monotonic()
                span = new_span(base, started)
                span_token = span_context.set(span)
                status = "failed"
                usage = (False, 0, 0, 0)
                error_id = None
                try:
                    response = await original_chat(*args, **kwargs)
                    usage = extract_usage(response)
                    status = "success"
                    return response
                except BaseException as error:
                    error_id = id(error)
                    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                        status = "cancelled"
                    raise
                finally:
                    span_context.reset(span_token)
                    _running.reset(token)
                    end_span(span, status, usage, error_id)

            return run()

        setattr(observed_chat, _MARKER, state)
        installed["text_chat"] = observed_chat

    if "text_chat_stream" in originals:
        original_stream = originals["text_chat_stream"]

        @wraps(original_stream)
        def observed_stream(*args, **kwargs):
            base = begin(kwargs)

            async def run():
                enabled = (state["active"] and id(provider) not in _running.get()
                           and span_context.get() is None)
                started = time.monotonic()
                span = new_span(base, started) if enabled else None
                status = "failed"
                usage = (False, 0, 0, 0)
                final_usage = None
                final_received = False
                iterator = None
                error_id = None
                try:
                    token = _running.set(_running.get() | {id(provider)})
                    span_token = span_context.set(span) if enabled else None
                    try:
                        iterator = original_stream(*args, **kwargs)
                        if inspect.isawaitable(iterator):
                            iterator = await iterator
                    finally:
                        if enabled:
                            span_context.reset(span_token)
                        _running.reset(token)
                    iterator = iterator.__aiter__()
                    while True:
                        # Do not retain a ContextVar token across yield: callers
                        # can consume/close each chunk in a different Task, and
                        # consumer code between chunks is not a nested request.
                        token = _running.set(_running.get() | {id(provider)})
                        span_token = span_context.set(span) if enabled else None
                        try:
                            response = await iterator.__anext__()
                        except StopAsyncIteration:
                            break
                        finally:
                            if enabled:
                                span_context.reset(span_token)
                            _running.reset(token)
                        snapshot = extract_usage(response)
                        if not _read(response, "is_chunk", False):
                            final_received = True
                        if snapshot[0]:
                            usage = snapshot
                            if not _read(response, "is_chunk", False):
                                final_usage = snapshot
                        yield response
                    status = "success"
                except BaseException as error:
                    error_id = id(error)
                    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                        status = "cancelled"
                    raise
                finally:
                    if enabled:
                        end_span(span, status, final_usage or usage, error_id,
                                 final_received=final_received)
                    closer = getattr(iterator, "aclose", None)
                    if callable(closer):
                        try:
                            await closer()
                        except BaseException:  # noqa: BLE001,S110 - Preserve the original stream outcome.
                            pass

            return run()

        setattr(observed_stream, _MARKER, state)
        installed["text_chat_stream"] = observed_stream

    for name, wrapper in installed.items():
        setattr(provider, name, wrapper)

    def is_current():
        return state["active"] and bool(installed) and all(
            any(current is wrapper for current in _chain(getattr(provider, name, None)))
            for name, wrapper in installed.items()
        )

    def restore():
        state["active"] = False
        restore_methods()

    restore.is_current = is_current
    return restore
