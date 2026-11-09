"""Task-bound exception for the native Xiaoheihe proactive feed.

No scope in this module grants tools, QQ sends, notification replies or post
publication.  A feed action and its one separately dispatched event have
different capabilities.  Neither capability is inherited by child tasks.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import importlib
import inspect
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .policy import activity_block
from .token_pricing import TZ, _peak

_ADAPTER_MODULE = "data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.adapter"
_EVENT_MODULE = "data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.event"
_GATE_MODULE = "data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe.feiyu_auto_reply_gate"
_PLUGIN_MODULE = "data.plugins.astrbot_plugin_xiaoheihe_adapter.main"
_FEED = contextvars.ContextVar("feiyu_heybox_feed_scope", default=None)
_CANDIDATE = contextvars.ContextVar("feiyu_heybox_candidate_scope", default=None)
_EVENT = contextvars.ContextVar("feiyu_heybox_event_scope", default=None)
_MODEL = contextvars.ContextVar("feiyu_heybox_model_scope", default=None)
_AUX_MODEL = contextvars.ContextVar("feiyu_heybox_aux_model_scope", default=None)
_registered = {}
_candidates = {}


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _known_calendar():
    try:
        value = _peak(time.time())
        return value is True or value is False
    except Exception:
        return False


def _day():
    return datetime.now(TZ).date()


def _route(route):
    try:
        profile = str(route.profile_id)
        post = str(route.post_id)
        root = str(route.root_comment_id or "")
        parent = str(route.parent_comment_id or "")
        notification = str(route.notification_id or "")
        if (not profile or len(profile) > 100 or not re.fullmatch(r"[1-9][0-9]{0,29}", post)
                or root or parent or notification != "feed:" + post):
            return None
        return profile, post, root, parent, notification
    except Exception:
        return None


@dataclass
class _FeedGrant:
    adapter: object
    feed: object
    runtime: object
    task: object
    client: object
    deadline: float
    active: bool = True
    host: object | None = None


@dataclass
class _CandidateGrant:
    feed: _FeedGrant
    event_id: int
    route: tuple
    active: bool = True


@dataclass
class _EventGrant:
    candidate: _CandidateGrant
    event: object
    day: object
    image_bindings: tuple | None = None
    active: bool = True
    claimed: bool = False


@dataclass
class _EventScope:
    grant: _EventGrant
    task: object
    authorized: bool = False


@dataclass
class _AuxGrant:
    event: object
    checked: object
    provider: object
    phase: str
    session: str
    task: object
    request: dict
    active: bool = True


def _feed_alive(grant, *, same_task=True, require_control=False):
    if not isinstance(grant, _FeedGrant) or not grant.active or not _known_calendar():
        return False
    try:
        adapter, feed, runtime = grant.adapter, grant.feed, grant.runtime
        if (time.monotonic() >= grant.deadline or grant.task.done() or grant.task.cancelling()
                or (same_task and _task() is not grant.task)
                or adapter._feed_task is not grant.task or adapter._stop.is_set()
                or adapter._refresh.is_set() or adapter not in runtime._adapters
                or feed.repository is not runtime.repository or feed.client is not grant.client
                or not runtime.config.snapshot()["proactive_feed"].get("enabled", False)):
            return False
        module = importlib.import_module(_ADAPTER_MODULE)
        if module.get_runtime() is not runtime:
            return False
        host = grant.host
        if host is None:
            return not require_control
        plugin = host.plugin("astrbot_plugin_xiaoheihe_adapter")
        runtime_status = host._peak_runtime_handle.status()
        background_status = host._peak_background_handle.status()
        binding = host._heybox_reply_gate_binding
        gate = importlib.import_module(_GATE_MODULE)
        return bool(plugin is not None and plugin.runtime is runtime
                    and runtime_status.get("ready") is True
                    and background_status.get("installed") is True
                    and not background_status.get("missing_targets")
                    and runner_guard_ready(host)
                    and isinstance(binding, tuple) and len(binding) == 2
                    and binding[0] is gate and gate.peak_calendar_registered(binding[1])
                    and isinstance(host.state, dict)
                    and not activity_block(dict(host.state), datetime.now(TZ)))
    except Exception:
        return False


@contextlib.contextmanager
def feed_scope(adapter, feed):
    """Bind only the installed adapter's live, current feed-loop task."""
    task = _task()
    grant = None
    try:
        module = importlib.import_module(_ADAPTER_MODULE)
        runtime = module.get_runtime()
        dispatch = feed.synthetic_dispatch
        if (task is not None and type(adapter) is module.XiaoheihePlatformAdapter
                and getattr(dispatch, "__self__", None) is adapter
                and getattr(dispatch, "__func__", None) is type(adapter)._dispatch_synthetic
                and str(feed.profile_id) == str(adapter.config.get("profile_id", "default"))):
            grant = _FeedGrant(adapter, feed, runtime, task, feed.client, time.monotonic() + 3600)
            if not _feed_alive(grant):
                grant = None
    except Exception:
        grant = None
    token = _FEED.set(grant)
    try:
        yield bool(grant)
    finally:
        if grant is not None:
            grant.active = False
        _FEED.reset(token)


def feed_active(feed=None):
    grant = _FEED.get()
    return _feed_alive(grant) and (feed is None or grant.feed is feed)


@contextlib.contextmanager
def feed_control(host, feed):
    """Tie a native run to its live steward, guard readiness and rest state."""
    grant = _FEED.get()
    if not _feed_alive(grant) or grant.feed is not feed or grant.host is not None:
        yield False
        return
    grant.host = host
    try:
        yield _feed_alive(grant, require_control=True)
    finally:
        grant.host = None


def feed_request_allowed(client, endpoint):
    grant = _FEED.get()
    return bool(_feed_alive(grant, require_control=True) and grant.client is client
                and str(endpoint) in {"feed", "topic_feed"})


@contextlib.contextmanager
def candidate_scope(adapter, notification, event_id):
    """Bind one freshly claimed, top-level feed candidate to the feed task."""
    feed = _FEED.get()
    grant = None
    try:
        route = _route(notification.route)
        recovery = importlib.import_module(_GATE_MODULE.rsplit(".", 1)[0] + ".feiyu_recovery")
        recovery_identity = recovery.identity_allowed(
            feed.runtime.repository, event_id, notification=notification) is not False
        if (_feed_alive(feed, require_control=True) and feed.adapter is adapter and type(event_id) is int and event_id > 0
                and str(notification.event_type.value) == "proactive_feed" and route is not None
                and recovery_identity
                and str(notification.external_event_id) == "feed:" + route[1]
                and str(notification.external_comment_id) == "feed:" + route[1]
                and route[0] == str(feed.feed.profile_id)):
            key = (id(feed.runtime.repository), event_id)
            if key not in _candidates:
                grant = _CandidateGrant(feed, event_id, route)
                _candidates[key] = grant
    except Exception:
        grant = None
    token = _CANDIDATE.set(grant)
    try:
        yield bool(grant)
    finally:
        if grant is not None:
            grant.active = False
            key = (id(grant.feed.runtime.repository), grant.event_id)
            if _candidates.get(key) is grant:
                _candidates.pop(key, None)
        _CANDIDATE.reset(token)


def _candidate_alive(grant, *, same_task=True):
    return bool(isinstance(grant, _CandidateGrant) and grant.active
                and _feed_alive(grant.feed, same_task=same_task, require_control=True))


def gate_allowed(repository, event_id, route):
    """Authorize the *same* candidate for reserve/context/dispatch/send checks."""
    candidate = _CANDIDATE.get()
    if (_candidate_alive(candidate) and candidate.feed.runtime.repository is repository
            and candidate.event_id == event_id and candidate.route == _route(route)):
        return True
    scope = _EVENT.get()
    if not _event_scope_valid(scope):
        return False
    candidate = scope.grant.candidate
    return bool(candidate.feed.runtime.repository is repository and candidate.event_id == event_id
                and candidate.route == _route(route))


def tracked_event(repository, event_id):
    """Identify an in-flight candidate even after its capability is revoked."""
    candidate = _candidates.get((id(repository), event_id))
    return bool(isinstance(candidate, _CandidateGrant)
                and candidate.feed.runtime.repository is repository)


def revoke_event(repository, event_id):
    """Revoke a matching live candidate before awaiting its durable update."""
    candidate = _candidates.get((id(repository), event_id))
    if (isinstance(candidate, _CandidateGrant) and candidate.event_id == event_id
            and candidate.feed.runtime.repository is repository):
        candidate.active = False
    for grant in tuple(_registered.values()):
        candidate = grant.candidate
        if candidate.event_id == event_id and candidate.feed.runtime.repository is repository:
            grant.active = False
            candidate.active = False


def _event_identity_matches(event, candidate):
    try:
        raw = event.message_obj.raw_message
        platform = str(candidate.feed.adapter.config.get("id", "xiaoheihe"))
        session = "xhh_post_" + candidate.route[1]
        recovery = importlib.import_module(_GATE_MODULE.rsplit(".", 1)[0] + ".feiyu_recovery")
        recovery_identity = recovery.identity_allowed(
            candidate.feed.runtime.repository, candidate.event_id, event=event) is not False
        return (isinstance(raw, dict) and raw.get("event_type") == "proactive_feed"
                and recovery_identity
                and raw.get("incoming_event_id") == candidate.event_id
                and raw.get("proactive") is True
                and raw.get("external_event_id") == "feed:" + candidate.route[1]
                and raw.get("external_comment_id") == "feed:" + candidate.route[1]
                and event.get_platform_name() == "xiaoheihe"
                and str(event.get_platform_id()) == platform
                and str(event.session_id) == session
                and str(event.unified_msg_origin) == f"{platform}:GroupMessage:{session}")
    except Exception:
        return False


def _capture_event_images(event):
    """Capture only the adapter-created image components before dispatch."""
    try:
        from astrbot.api.message_components import Plain

        adapter_module = importlib.import_module(_ADAPTER_MODULE)
        image_type = adapter_module._FeedEventImage
        raw = event.message_obj.raw_message
        urls = raw.get("image_urls")
        messages = event.get_messages()
        if (not isinstance(urls, list) or not isinstance(messages, list)
                or len(messages) != len(urls) + 1 or type(messages[0]) is not Plain):
            return None
        bindings = []
        for index, (component, url) in enumerate(zip(messages[1:], urls, strict=True), start=1):
            if (type(component) is not image_type or not isinstance(url, str)
                    or not url.startswith("https://") or component.file != url
                    or component.url != url or component.path
                    or component._event_source_url != url
                    or component._event_materialized is not None):
                return None
            bindings.append((index, component, url))
        return tuple(bindings)
    except Exception:
        return None


def _event_image_refs_valid(event, grant, requested):
    """Match current references to the same event's original components."""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        bindings = grant.image_bindings
        if (grant.event is not event or not isinstance(bindings, tuple)
                or not isinstance(requested, (list, tuple)) or not requested
                or any(not isinstance(ref, str) or not ref for ref in requested)
                or len(set(requested)) != len(requested)):
            return False
        raw = event.message_obj.raw_message
        if raw.get("image_urls") != [source for _, _, source in bindings]:
            return False
        plugin = importlib.import_module(_PLUGIN_MODULE)
        vault = event.get_extra(plugin.EARLY_IMAGE_VAULT_EXTRA, None)
        if (not isinstance(vault, dict) or vault.get("exposed") is not False
                or not isinstance(vault.get("hidden"), list)):
            return False
        original_by_index = {index: (component, source) for index, component, source in bindings}
        hidden = vault["hidden"]
        selected = []
        previous_index = 0
        for entry in hidden:
            if not isinstance(entry, tuple) or len(entry) != 2:
                return False
            index, component = entry
            original = original_by_index.get(index)
            if (type(index) is not int or index <= previous_index or original is None
                    or original[0] is not component):
                return False
            previous_index = index
            selected.append((index, component, original[1]))
        last_index = 0
        for ref in requested:
            matches = [(index, component, source) for index, component, source in selected
                       if component.file == ref and component.url == ref]
            if len(matches) != 1:
                return False
            index, component, source = matches[0]
            if index <= last_index or component._event_source_url != source:
                return False
            last_index = index
            if ref == source:
                if component.path or component._event_materialized is not None:
                    return False
                continue
            proof = component._event_materialized
            if (not isinstance(proof, tuple) or len(proof) != 8 or proof[0] != source
                    or proof[1] != ref or proof[2] is not _task()
                    or component.path != ref):
                return False
            temp_dir = Path(get_astrbot_temp_path()).resolve(strict=True)
            target = Path(ref)
            resolved = target.resolve(strict=True)
            info = target.stat(follow_symlinks=False)
            if (target != resolved or resolved.parent != temp_dir
                    or not resolved.name.startswith("media_image_") or target.is_symlink()
                    or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    != proof[3:]):
                return False
        return True
    except Exception:
        return False


@contextlib.contextmanager
def register_event(adapter, event):
    """Register the real adapter event only until its own dispatch completes."""
    candidate = _CANDIDATE.get()
    grant = None
    try:
        module = importlib.import_module(_EVENT_MODULE)
        if (_candidate_alive(candidate) and candidate.feed.adapter is adapter
                and type(event) is module.XiaoheiheMessageEvent
                and event.runtime is candidate.feed.runtime and event.proactive is True
                and event.event_id == candidate.event_id and _route(event.route) == candidate.route
                and _event_identity_matches(event, candidate) and id(event) not in _registered):
            grant = _EventGrant(candidate, event, _day(), _capture_event_images(event))
            _registered[id(event)] = grant
    except Exception:
        grant = None
    try:
        yield bool(grant)
    finally:
        if grant is not None:
            grant.active = False
            if _registered.get(id(event)) is grant:
                _registered.pop(id(event), None)


def _event_grant_valid(grant):
    if (not isinstance(grant, _EventGrant) or not grant.active
            or _registered.get(id(grant.event)) is not grant or grant.day != _day()
            or not _candidate_alive(grant.candidate, same_task=False)):
        return False
    try:
        event, candidate = grant.event, grant.candidate
        return (event.event_id == candidate.event_id and event.runtime is candidate.feed.runtime
                and event.proactive is True and _route(event.route) == candidate.route
                and _event_identity_matches(event, candidate) and not event._completion.is_set())
    except Exception:
        return False


def _event_scope_valid(scope, *, require_authorized=False):
    return bool(isinstance(scope, _EventScope) and scope.task is _task()
                and not scope.task.done() and not scope.task.cancelling()
                and (scope.authorized or not require_authorized)
                and _event_grant_valid(scope.grant))


@contextlib.asynccontextmanager
async def pipeline_scope(event):
    """Rebind a registered event to the scheduler task after a ledger check."""
    grant = _registered.get(id(event))
    if not _event_grant_valid(grant) or grant.claimed or _task() is None:
        yield False
        return
    # EventBus dispatches one scheduler task per event. A copied event object
    # must not mint additional scopes in child or competing tasks.
    grant.claimed = True
    scope = _EventScope(grant, _task())
    token = _EVENT.set(scope)
    try:
        gate = importlib.import_module(_GATE_MODULE)
        candidate = grant.candidate
        try:
            approved = await gate.authorize(candidate.feed.runtime.repository, candidate.event_id,
                                            event.route, phase="check")
        except Exception:
            approved = False
        scope.authorized = approved is True and _event_scope_valid(scope)
        yield scope.authorized
    finally:
        scope.authorized = False
        _EVENT.reset(token)


def event_active(event=None):
    scope = _EVENT.get()
    return bool(_event_scope_valid(scope, require_authorized=True)
                and (event is None or scope.grant.event is event))


def _runner_bound(runner):
    try:
        event = runner.run_context.context.event
        return (event_active(event) and runner.req.session_id == event.unified_msg_origin
                and runner.provider is not None)
    except Exception:
        return False


def _model_active(runner=None):
    marker = _MODEL.get()
    return bool(isinstance(marker, tuple) and len(marker) == 2 and marker[1] is _task()
                and (runner is None or marker[0] is runner) and _runner_bound(marker[0]))


def _checked_provider(checked, *, runner, in_request):
    """Accept only the adapter's live, event-checked provider wrapper."""
    try:
        module = importlib.import_module(_PLUGIN_MODULE)
        if (type(checked) is not module._CheckedProvider or checked._runner is not runner
                or checked._disable_tools is not True or not callable(checked._valid)
                or checked._valid() is not True):
            return None
        provider, state = checked._provider, checked._state
        if (not isinstance(state, dict) or state.get("provider") is not provider
                or state.get("owner") is not module._GUARD_OWNER
                or state.get("active") is not True
                or state.get("generation") != checked._generation
                or getattr(provider, module._PROVIDER_GUARD_ATTRIBUTE, None) is not state
                or not module._query_guard_chain_intact(provider, state)
                or not checked._expected_model or provider.get_model() != checked._expected_model):
            return None
        if in_request:
            check = state["context"].get()
            if (getattr(check, "__self__", None) is not checked
                    or getattr(check, "__func__", None) is not type(checked)._require
                    or type(state.get("inflight")) is not int or state["inflight"] <= 0):
                return None
        return provider
    except Exception:
        return None


@contextlib.contextmanager
def auxiliary_model_scope(event, checked, phase, session, request):
    """Bind one adapter-selected image or context request to this event."""
    grant = None
    frame = None
    try:
        prefix = {"image": "xiaoheihe-image-", "context": "xiaoheihe-context-"}.get(phase)
        scope = _EVENT.get()
        actual = _checked_provider(checked, runner=None, in_request=False)
        module = importlib.import_module(_PLUGIN_MODULE)
        method = "_try_caption_thread_image_group" if phase == "image" else "_compress_thread_context"
        frame = None
        for candidate_frame in inspect.stack()[1:]:
            if candidate_frame.function == method:
                frame = candidate_frame.frame
                break
        if frame is None:
            raise RuntimeError("auxiliary_callsite_missing")
        owner = frame.f_locals.get("self")
        expected_code = frame.f_code
        locals_ = frame.f_locals
        registered_class = getattr(module, "XiaoheiheAdapterPlugin", None)
        registered_method = getattr(registered_class, method, None)
        if (registered_class is None or type(owner) is not registered_class
                or scope.grant.candidate.feed.host.plugin("astrbot_plugin_xiaoheihe_adapter") is not owner
                or owner.runtime is not scope.grant.candidate.feed.runtime
                or not callable(registered_method) or registered_method.__code__ is not expected_code):
            raise RuntimeError("auxiliary_owner_changed")
        keys = {"prompt", "session_id", "persist", "request_max_retries"}
        if phase == "image":
            keys.add("image_urls")
            parent = next((item.frame for item in inspect.stack()[1:]
                           if item.function == "_caption_thread_image_group"), None)
            if parent is None:
                raise RuntimeError("image_callsite_missing")
            candidates = parent.f_locals.get("resolved_candidates")
            selection_ok = (parent.f_code is getattr(registered_class, "_caption_thread_image_group").__code__
                            and parent.f_locals.get("self") is owner
                            and parent.f_locals.get("event") is event
                            and parent.f_locals.get("provider") is actual
                            and isinstance(candidates, (list, tuple))
                            and any(isinstance(item, tuple) and len(item) == 2 and item[0] is actual
                                    for item in candidates)
                            and request.get("image_urls") is locals_.get("urls")
                            and isinstance(request.get("image_urls"), list)
                            and bool(request["image_urls"])
                            and _event_image_refs_valid(event, scope.grant, request["image_urls"]))
        else:
            candidates = locals_.get("providers")
            selection_ok = (locals_.get("checked_provider") is checked
                            and locals_.get("provider") is actual
                            and isinstance(candidates, (list, tuple))
                            and any(isinstance(item, tuple) and len(item) == 2 and item[0] is actual
                                    for item in candidates)
                            and request.get("prompt") == locals_.get("prompt")
                            and str(locals_["source"].post_id) == scope.grant.candidate.route[1])
        if (event_active(event) and owner is not None
                and frame.f_code is expected_code and locals_.get("self") is owner
                and locals_.get("event") is event and locals_.get("model_request") is request
                and isinstance(request, dict) and set(request) == keys
                and isinstance(request.get("prompt"), str) and bool(request["prompt"])
                and request.get("persist") is False and request.get("request_max_retries") == 1
                and prefix is not None and isinstance(session, str)
                and request.get("session_id") == session
                and re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", session)
                and actual is not None and selection_ok):
            snapshot = dict(request)
            if "image_urls" in snapshot:
                snapshot["image_urls"] = tuple(snapshot["image_urls"])
            grant = _AuxGrant(event, checked, actual, phase, session, _task(), snapshot)
    except Exception:
        grant = None
    finally:
        del frame
    token = _AUX_MODEL.set(grant)
    try:
        yield grant is not None
    finally:
        if grant is not None:
            grant.active = False
        _AUX_MODEL.reset(token)


def _sdk_request_allowed(checked, args, kwargs):
    if args or not isinstance(kwargs, dict) or kwargs.get("model") != checked._expected_model:
        return False
    extra = kwargs.get("extra_body") or {}
    return (isinstance(extra, dict) and not any(extra.get(key) for key in
            ("model", "tools", "tool_choice", "functions", "function_call"))
            and not kwargs.get("tools") and kwargs.get("tool_choice") in (None, "none")
            and not kwargs.get("functions") and not kwargs.get("function_call"))


def provider_allowed(session=None, *, provider=None, method=None, args=(), kwargs=None, sdk=False):
    scope = _EVENT.get()
    if not _event_scope_valid(scope, require_authorized=True):
        return False
    if _model_active():
        runner = _MODEL.get()[0]
        checked = runner.provider
        try:
            module = importlib.import_module(_PLUGIN_MODULE)
            if (checked._expected_model != module.AUTO_MAIN_PROVIDER_ID
                    or runner.req.func_tool is not None or runner.req.model is not None
                    or runner.req.image_urls or runner.req.audio_urls or runner.fallback_providers):
                return False
        except Exception:
            return False
        actual = _checked_provider(checked, runner=runner, in_request=True)
        event = scope.grant.event
        if actual is None or provider is not actual:
            return False
        if sdk:
            return _sdk_request_allowed(checked, args, kwargs)
        return bool(method in {"text_chat", "text_chat_stream"} and not args
                    and isinstance(kwargs, dict) and kwargs.get("func_tool") is None
                    and kwargs.get("model") == checked._expected_model
                    and kwargs.get("request_max_retries") == 1
                    and not kwargs.get("image_urls") and not kwargs.get("audio_urls")
                    and str(session) in {str(event.unified_msg_origin), str(event.session_id)})
    auxiliary = _AUX_MODEL.get()
    if (not isinstance(auxiliary, _AuxGrant) or not auxiliary.active
            or auxiliary.event is not scope.grant.event or auxiliary.task is not _task()):
        return False
    if (auxiliary.phase == "image" and not _event_image_refs_valid(
            auxiliary.event, scope.grant, auxiliary.request.get("image_urls"))):
        return False
    actual = _checked_provider(auxiliary.checked, runner=None, in_request=True)
    if actual is None or provider is not actual or actual is not auxiliary.provider:
        return False
    if sdk:
        return _sdk_request_allowed(auxiliary.checked, args, kwargs)
    if method != "text_chat" or args or not isinstance(kwargs, dict) or session != auxiliary.session:
        return False
    expected = dict(auxiliary.request)
    expected.update(model=auxiliary.checked._expected_model, func_tool=None)
    current = dict(kwargs)
    if isinstance(current.get("image_urls"), list):
        current["image_urls"] = tuple(current["image_urls"])
    return current == expected


def runner_guard_ready(host):
    """Report whether this host still owns both runner guard bindings."""
    try:
        restore = host._heybox_peak_runner_restore
        current = getattr(restore, "is_current", None)
        return (callable(restore) and callable(current)
                and current(getattr(host, "_friend_guard_restore", None)) is True)
    except Exception:
        return False


def install_runner_wait_guard():
    """Bind only the native runner's model step and retain abort support."""
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    cls = ToolLoopAgentRunner
    original = cls.__dict__.get("_await_or_stop")
    previous = getattr(original, "_heybox_wait_guard_state", None)
    if isinstance(previous, dict) and callable(previous.get("restore")):
        if cls.__dict__.get("_iter_llm_responses") is not previous.get("iter"):
            raise RuntimeError("heybox_runner_guard_wrapped")
        previous["restore"]()
        original = cls.__dict__.get("_await_or_stop")
    original_iter = cls.__dict__.get("_iter_llm_responses")
    if getattr(original_iter, "_heybox_model_guard_state", None) is not None:
        raise RuntimeError("heybox_runner_guard_retired_or_wrapped")
    if not inspect.iscoroutinefunction(original) or not inspect.isasyncgenfunction(original_iter):
        raise RuntimeError("heybox_runner_wait_unavailable")
    lifetime = {"active": True}

    @functools.wraps(original_iter)
    async def guarded_iter(runner, *args, **kwargs):
        if not lifetime["active"]:
            async for item in original_iter(runner, *args, **kwargs):
                yield item
            return
        iterator = original_iter(runner, *args, **kwargs).__aiter__()
        try:
            while True:
                token = _MODEL.set((runner, _task()) if _runner_bound(runner) else None)
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    return
                finally:
                    _MODEL.reset(token)
                yield item
        finally:
            await iterator.aclose()

    async def guarded(runner, awaitable):
        if not lifetime["active"] or not _model_active(runner):
            return await original(runner, awaitable)
        task = _task()
        if task is None or runner._abort_signal.is_set():
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            return None
        baseline = task.cancelling()
        fired = False

        async def watch_abort():
            nonlocal fired
            await runner._abort_signal.wait()
            if not task.done():
                fired = True
                task.cancel()

        watcher = asyncio.create_task(watch_abort(), context=contextvars.Context())
        try:
            result = await awaitable
            # A provider may suppress cancellation. Preserve an independent
            # outer cancellation, but consume exactly the cancellation that
            # our own abort watcher issued before reporting an abort.
            if task.cancelling() > baseline:
                if fired and task.cancelling() == baseline + 1:
                    task.uncancel()
                else:
                    raise asyncio.CancelledError
            return None if fired or runner._abort_signal.is_set() else result
        except asyncio.CancelledError:
            if fired and task.cancelling() == baseline + 1:
                task.uncancel()
                return None
            raise
        finally:
            watcher.cancel()

    cls._await_or_stop = guarded
    cls._iter_llm_responses = guarded_iter

    def restore():
        lifetime["active"] = False
        if cls.__dict__.get("_await_or_stop") is guarded:
            cls._await_or_stop = original
        if cls.__dict__.get("_iter_llm_responses") is guarded_iter:
            cls._iter_llm_responses = original_iter

    def is_current(friend_restore=None):
        if lifetime["active"] is not True or cls.__dict__.get("_await_or_stop") is not guarded:
            return False
        current_iter = cls.__dict__.get("_iter_llm_responses")
        if friend_restore is None:
            return current_iter is guarded_iter
        try:
            friend_current = getattr(friend_restore, "is_current", None)
            return (callable(friend_restore) and callable(friend_current)
                    and getattr(current_iter, "__wrapped__", None) is guarded_iter
                    and friend_current() is True)
        except Exception:
            return False

    restore.is_current = is_current
    guarded._heybox_wait_guard_state = {"restore": restore, "iter": guarded_iter}
    guarded_iter._heybox_model_guard_state = {"active": lifetime, "iter": guarded_iter}
    return restore
