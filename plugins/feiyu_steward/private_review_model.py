"""Bounded, private model calls for the Bilibili journal handoff.

This module keeps the existing provider ``text_chat`` usage observer path, adding a
small guard around the currently audited OpenAI provider implementation.  The
guard is installed on the provider *instance* (never on a provider class), and
its scope is held by a ``ContextVar`` stored on that instance.  That makes a
plugin hot reload reuse the same scope seen by a wrapper that was installed by
an older module object.

No prompt, completion, provider configuration, API key, or exception text is
written by this module.
"""

from __future__ import annotations

import contextlib
import contextvars
import asyncio
import inspect
import logging
import sys
import types
import weakref
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Iterator


# These strings are intentionally fixed.  Callers can persist the code without
# retaining provider error text, which may contain private prompt/completion
# data.
PRIVATE_REVIEW_PROVIDER_UNAVAILABLE = "private_review_provider_unavailable"
PRIVATE_REVIEW_CALL_FAILED = "private_review_call_failed"
PRIVATE_REVIEW_MULTIPLE_ATTEMPTS = "private_review_multiple_attempts"
PRIVATE_REVIEW_WINDOW_UNAVAILABLE = "private_review_window_unavailable"

_MODEL_FAILURE_KINDS = frozenset({
    "upstream_server_error", "rate_limit", "authentication", "permission",
    "connection", "timeout", "http_status_error", "other",
})


def _validated_model_failure(value: Any) -> dict[str, Any] | None:
    """Copy two fixed scalars, never provider text or arbitrary attributes."""
    if (type(value) is not dict or len(value) != 2
            or not all(type(key) is str for key in value)
            or set(value) != {"kind", "http_status"}):
        return None
    kind, status = value["kind"], value["http_status"]
    if (type(kind) is not str or kind not in _MODEL_FAILURE_KINDS
            or not (status is None or type(status) is int and 100 <= status <= 599)):
        return None
    return {"kind": kind, "http_status": status}


def _exception_model_failure(error: BaseException) -> dict[str, Any] | None:
    # Read the BaseException storage directly: a foreign property or
    # __getattribute__ cannot turn metadata extraction into a response read.
    values = BaseException.__dict__["__dict__"].__get__(error)
    return _validated_model_failure(values.get("model_failure"))


def _safe_model_failure(error: BaseException) -> dict[str, Any]:
    """Classify audited exception types without reading message/response data."""
    kind, status = "other", None
    mro = type.__getattribute__(type(error), "__mro__")
    if TimeoutError in mro:
        kind = "timeout"
    elif ConnectionError in mro:
        kind = "connection"
    try:
        import openai
    except ImportError:  # Offline environments may lack the optional SDK.
        return {"kind": kind, "http_status": status}
    if openai.APITimeoutError in mro:
        kind = "timeout"
    elif openai.APIConnectionError in mro:
        kind = "connection"
    elif openai.APIStatusError in mro:
        values = BaseException.__dict__["__dict__"].__get__(error)
        candidate = values.get("status_code")
        if type(candidate) is int and 100 <= candidate <= 599:
            status = candidate
        kind = ("upstream_server_error" if status is not None and status >= 500 else
                "rate_limit" if status == 429 else
                "authentication" if status == 401 else
                "permission" if status == 403 else "http_status_error")
    return {"kind": kind, "http_status": status}

_KNOWN_PROVIDER_MODULE = "astrbot.core.provider.sources.openai_source"
_KNOWN_PROVIDER_NAME = "ProviderOpenAIOfficial"

# Instance attributes are deliberately long and module-independent.  A reload
# creates a new module object, but these values remain on the live provider.
_INSTALL_ATTR = "_feiyu_private_review_model_boundary"
_SCOPE_ATTR = "_feiyu_private_review_model_scope"
_LOGGER_STATE_ATTR = "_feiyu_private_review_model_logger_state"
_FILTER_MARKER = "_feiyu_private_review_model_log_filter"
_BOUNDARY_VERSION = 1
_XIAOHEIHE_GUARD_ATTR = "_feiyu_xiaoheihe_reply_guard_v1"
_XIAOHEIHE_MODULE = "data.plugins.astrbot_plugin_xiaoheihe_adapter.main"


try:  # AstrBot exposes a standard logging.Logger at astrbot.logger.
    from astrbot import logger as logger  # type: ignore
except Exception:  # pragma: no cover - the offline candidate has no AstrBot import.
    logger = logging.getLogger("astrbot")


class PrivateReviewModelError(RuntimeError):
    """A fixed, safe error for callers of the candidate boundary."""

    def __init__(self, code: str = PRIVATE_REVIEW_CALL_FAILED, *,
                 model_failure: Any = None) -> None:
        self.code = code
        metadata = _validated_model_failure(model_failure)
        if metadata is not None:
            self.model_failure = metadata
        super().__init__(code)


class _PrivateReviewAbort(BaseException):
    """Internal fixed abort that cannot enter text_chat's recovery branch.

    AstrBot's OpenAI ``text_chat`` catches ``Exception`` around ``_query`` and
    then invokes ``_handle_api_error``.  This marker deliberately inherits
    directly from ``BaseException`` so that branch is skipped, preserving any
    existing provider/friend guard identity while still ending the current
    request.  It contains no original exception or response text.
    """

    def __init__(self, code: str = PRIVATE_REVIEW_CALL_FAILED, *,
                 model_failure: Any = None) -> None:
        self.code = code
        metadata = _validated_model_failure(model_failure)
        if metadata is not None:
            self.model_failure = metadata
        super().__init__(code)


@dataclass(frozen=True)
class _ScopeState:
    """The value held in the provider's ContextVar.

    The wrappers only rely on the two attribute names.  Keeping this value
    immutable means a child task that inherits a review scope can advance its
    own query count without mutating a sibling task's value.
    """

    active: bool = True
    query_calls: int = 0
    preflight: Callable[[], bool] | None = None


def _scope_var(provider: Any) -> contextvars.ContextVar[Any] | None:
    marker = getattr(provider, _INSTALL_ATTR, None)
    value = marker.get("scope") if isinstance(marker, dict) else None
    if isinstance(value, contextvars.ContextVar):
        return value
    value = getattr(provider, _SCOPE_ATTR, None)
    return value if isinstance(value, contextvars.ContextVar) else None


def _current_scope(provider: Any) -> Any | None:
    scope = _scope_var(provider)
    if scope is None:
        return None
    try:
        value = scope.get()
    except Exception:  # pragma: no cover - defensive against a foreign ContextVar.
        return None
    if isinstance(value, dict):
        return value if value.get("active") is True else None
    if getattr(value, "active", False) is True:
        return value
    return None


def _translate_observed_error(provider: Any, original: BaseException, abort: BaseException) -> None:
    """Preserve a recorded parser failure when translating its control flow.

    The observer has already saved usage for this exact parser exception.
    Only translate that numeric identity; never change counts, records, or a
    different exception. Without this, end_span would count a second unknown.
    """
    # This helper is already called by the authenticated old wrapper. Keep
    # that wrapper's bytecode unchanged so reloads can reuse its identity.
    try:
        abort.model_failure = _safe_model_failure(original)
    except Exception:
        pass
    try:
        span_var = getattr(provider, "_feiyu_usage_observer_span", None)
        if isinstance(span_var, contextvars.ContextVar):
            span = span_var.get()
            if isinstance(span, dict) and span.get("last_parse_error") == id(original):
                span["last_parse_error"] = id(abort)
    except Exception:
        # Optional telemetry must never let the original error enter recovery.
        pass


def _known_openai_provider(provider: Any) -> bool:
    """Accept only the audited OpenAI provider and a disabled SDK retry.

    The exact class module/name check prevents an OpenAI-compatible or test
    provider from silently bypassing the boundary.  Importing the class is
    intentionally optional so AST/offline tests can use a class with the same
    audited identity without importing all AstrBot dependencies.
    """

    if provider is None:
        return False
    cls = type(provider)
    if cls.__module__ != _KNOWN_PROVIDER_MODULE or cls.__name__ != _KNOWN_PROVIDER_NAME:
        return False

    try:
        from astrbot.core.provider.sources.openai_source import (  # type: ignore
            ProviderOpenAIOfficial,
        )
    except Exception:
        ProviderOpenAIOfficial = None  # type: ignore[assignment]
    if ProviderOpenAIOfficial is not None and cls is not ProviderOpenAIOfficial:
        return False

    client = getattr(provider, "client", None)
    # ``bool`` is an ``int`` subclass; accept only the actual integer zero.
    return type(getattr(client, "max_retries", None)) is int and client.max_retries == 0


def _logger_state() -> dict[str, Any]:
    """Get the logger ContextVar and install one filter for this candidate."""

    target = logger
    state = getattr(target, _LOGGER_STATE_ATTR, None)
    if not isinstance(state, dict) or not isinstance(
        state.get("scope"), contextvars.ContextVar
    ):
        state = {"scope": contextvars.ContextVar("feiyu_private_review_log_scope", default=False)}
        setattr(target, _LOGGER_STATE_ATTR, state)
    # The framework logger survives plugin-module reloads.  This registry is
    # read from the trusted logger, never from a function being authenticated.
    if not isinstance(state.get("boundaries"), weakref.WeakKeyDictionary):
        state["boundaries"] = weakref.WeakKeyDictionary()

    filters = getattr(target, "filters", ())
    if not any(getattr(item, _FILTER_MARKER, False) for item in filters):
        log_filter = _PrivateReviewLogFilter(target)
        setattr(log_filter, _FILTER_MARKER, True)
        add_filter = getattr(target, "addFilter", None)
        if not callable(add_filter):
            raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)
        add_filter(log_filter)
    return state


def _private_log_scope_active(target: Any) -> bool:
    state = getattr(target, _LOGGER_STATE_ATTR, None)
    scope = state.get("scope") if isinstance(state, dict) else None
    if not isinstance(scope, contextvars.ContextVar):
        return False
    try:
        return scope.get() is True
    except Exception:  # pragma: no cover - defensive only.
        return False


class _PrivateReviewLogFilter(logging.Filter):
    """Suppress only provider records that can contain private text.

    The filter lives on the ``astrbot`` Logger and consults a task-local
    ContextVar.  Normal tasks keep every record, including DEBUG records; the
    global logger level and handlers are never changed.
    """

    def __init__(self, target: Any) -> None:
        super().__init__()
        self._target = target

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API name.
        # Even an empty message may carry private exception text in exc_info.
        return not _private_log_scope_active(self._target)


def _private_guard_layer_intact(provider: Any, marker: Any) -> bool:
    """Validate the installed function itself, not just writable metadata."""
    try:
        if (not isinstance(marker, dict) or marker.get("version") != _BOUNDARY_VERSION
                or getattr(provider, _INSTALL_ATTR, None) is not marker
                or not isinstance(marker.get("scope"), contextvars.ContextVar)
                or getattr(provider, _SCOPE_ATTR, None) is not marker.get("scope")):
            return False
        query = marker.get("query_wrapper")
        if type(query) is not types.FunctionType:
            return False
        logger_state = getattr(logger, _LOGGER_STATE_ATTR, None)
        registrations = logger_state.get("boundaries") if isinstance(logger_state, dict) else None
        if not isinstance(registrations, weakref.WeakKeyDictionary):
            return False
        registered = registrations.get(provider)
        if not isinstance(registered, weakref.ReferenceType) or registered() is not query:
            return False
        template = next(item for item in _install_provider_guards.__code__.co_consts
                        if isinstance(item, types.CodeType) and item.co_name == "query_wrapper")
        code = query.__code__
        fields = ("co_code", "co_consts", "co_names", "co_varnames", "co_freevars",
                  "co_cellvars", "co_flags", "co_argcount", "co_posonlyargcount",
                  "co_kwonlyargcount")
        if hasattr(template, "co_exceptiontable"):
            fields += ("co_exceptiontable",)
        if any(getattr(code, field) != getattr(template, field) for field in fields):
            return False
        closure = dict(zip(code.co_freevars, query.__closure__ or ()))
        expected = {"provider": provider, "scope": marker["scope"],
                    "original_query": marker.get("original_query")}
        if not all(key in closure and closure[key].cell_contents is value
                   for key, value in expected.items()):
            return False
        abort = query.__globals__.get("_PrivateReviewAbort")
        return (getattr(query, "__wrapped__", None) is marker.get("original_query")
                and isinstance(abort, type) and issubclass(abort, BaseException)
                and not issubclass(abort, Exception)
                and abort.__name__ == "_PrivateReviewAbort")
    except (AttributeError, RuntimeError, StopIteration, TypeError, ValueError):
        return False


def _private_guard_chain_intact(provider: Any, marker: Any) -> bool:
    if not _private_guard_layer_intact(provider, marker):
        return False
    state = getattr(provider, _XIAOHEIHE_GUARD_ATTR, None)
    if state is None:
        inner = marker.get("original_query")
        code = getattr(inner, "__code__", None)
        if (getattr(code, "co_name", None) == "checked_query"
                and set(getattr(code, "co_freevars", ())) == {"original_query", "state"}):
            return False
        return getattr(provider, "_query", None) is marker.get("query_wrapper")
    peer = sys.modules.get(_XIAOHEIHE_MODULE)
    validate = getattr(peer, "_query_guard_chain_intact", None)
    return callable(validate) and validate(provider, state)


def _install_provider_guards(provider: Any) -> dict[str, Any]:
    """Install (or reuse) instance wrappers for the audited provider.

    The wrappers are closure functions assigned directly to the instance.  A
    direct assignment avoids modifying a shared class or replacing the shared
    client, API key, or provider configuration.
    """

    marker = getattr(provider, _INSTALL_ATTR, None)
    if isinstance(marker, dict):
        if _private_guard_chain_intact(provider, marker):
            # A reload must not add another wrapper layer.  Ensure privacy
            # filtering is still present, since logger setup may have changed.
            # importlib.reload can rebind the abort class in the old wrapper's
            # globals; keep error translation tied to the class it actually uses.
            marker["abort_type"] = marker["query_wrapper"].__globals__["_PrivateReviewAbort"]
            # Only refresh this helper after authenticating the exact old
            # wrapper and its closure; do not replace or stack query guards.
            marker["query_wrapper"].__globals__["_translate_observed_error"] = _translate_observed_error
            _logger_state()
            return marker
        # No older candidate was deployed. Unknown/changed wrappers are not
        # silently reused or stacked; the ordinary provider stays untouched.
        raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)

    if marker is not None:
        raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)
    state = getattr(provider, _XIAOHEIHE_GUARD_ATTR, None)
    if state is not None:
        peer = sys.modules.get(_XIAOHEIHE_MODULE)
        validate = getattr(peer, "_query_guard_chain_intact", None)
        if not callable(validate) or not validate(provider, state):
            raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)

    scope = marker.get("scope") if isinstance(marker, dict) else None
    if not isinstance(scope, contextvars.ContextVar):
        scope = getattr(provider, _SCOPE_ATTR, None)
    if not isinstance(scope, contextvars.ContextVar):
        scope = contextvars.ContextVar("feiyu_private_review_model_scope", default=None)
    setattr(provider, _SCOPE_ATTR, scope)

    original_query = getattr(provider, "_query", None)
    if not callable(original_query):
        raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)

    previous_marker = marker if isinstance(marker, dict) else {}

    @wraps(original_query)
    async def query_wrapper(*args: Any, **kwargs: Any) -> Any:
        active = _current_scope(provider)
        if active is not None:
            calls = int(getattr(active, "query_calls", 0))
            if isinstance(active, dict):
                calls = int(active.get("query_calls", 0))
            if calls >= 1:
                raise _PrivateReviewAbort(PRIVATE_REVIEW_MULTIPLE_ATTEMPTS)
            preflight = getattr(active, "preflight", None)
            if isinstance(active, dict):
                preflight = active.get("preflight")
            try:
                allowed = callable(preflight) and preflight() is True
            except Exception:
                allowed = False
            if not allowed:
                raise _PrivateReviewAbort(PRIVATE_REVIEW_WINDOW_UNAVAILABLE)
            if not _known_openai_provider(provider):
                raise _PrivateReviewAbort(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)
            # Keep the increment in this task's ContextVar.  Do not mutate a
            # dict inherited by a sibling task.
            scope.set(_ScopeState(active=True, query_calls=1, preflight=preflight))
            kwargs["request_max_retries"] = 1
            try:
                return await original_query(*args, **kwargs)
            except Exception as error:
                # Do not let text_chat's outer ``except Exception`` invoke its
                # recovery/key-rotation path.  The fixed BaseException marker
                # contains no provider error text.
                abort = _PrivateReviewAbort()
                _translate_observed_error(provider, error, abort)
                raise abort from None
        return await original_query(*args, **kwargs)

    registrations = _logger_state()["boundaries"]
    registrations[provider] = weakref.ref(query_wrapper)
    try:
        setattr(provider, "_query", query_wrapper)
        marker = {
            **previous_marker,
            "version": _BOUNDARY_VERSION,
            "scope": scope,
            "query_wrapper": query_wrapper,
            "original_query": original_query,
            "abort_type": _PrivateReviewAbort,
        }
        setattr(provider, _INSTALL_ATTR, marker)
    except BaseException:
        if getattr(provider, "_query", None) is query_wrapper:
            setattr(provider, "_query", original_query)
        registrations.pop(provider, None)
        raise
    return marker


@contextlib.contextmanager
def private_review_scope(provider: Any, preflight: Callable[[], bool] | None = None) -> Iterator[None]:
    """Activate the task-local single-call and log privacy boundary."""

    marker = getattr(provider, _INSTALL_ATTR, None)
    scope = marker.get("scope") if isinstance(marker, dict) else None
    if not isinstance(scope, contextvars.ContextVar):
        raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)
    log_state = _logger_state()
    scope_token = scope.set(_ScopeState(preflight=preflight))
    log_token = log_state["scope"].set(True)
    try:
        yield
    finally:
        # Reset in reverse order even when the provider raises or the task is
        # cancelled.  This is what keeps a later ordinary task fully visible.
        log_state["scope"].reset(log_token)
        scope.reset(scope_token)


async def _provider_for(context: Any, provider_id: str) -> Any:
    manager = getattr(context, "provider_manager", None)
    getter = getattr(manager, "get_provider_by_id", None)
    if not isinstance(provider_id, str) or not provider_id or not callable(getter):
        raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)
    try:
        provider = getter(provider_id)
        if inspect.isawaitable(provider):
            provider = await provider
    except BaseException as error:
        if isinstance(error, (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt, SystemExit)):
            raise
        raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE) from None
    if not _known_openai_provider(provider):
        raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)
    return provider


async def generate_private_review(
    context: Any,
    provider_id: str,
    *,
    system_prompt: str,
    prompt: str,
    max_tokens: int,
    preflight: Callable[[], bool] | None = None,
) -> Any:
    """Use the validated provider's existing observed method, without re-resolving.

    This is the same text_chat call forwarded by context.llm_generate, with no
    tools, images or audio. No provider-manager lock is held across the request.
    """

    if not callable(preflight):
        raise PrivateReviewModelError(PRIVATE_REVIEW_PROVIDER_UNAVAILABLE)
    provider = await _provider_for(context, provider_id)
    try:
        _install_provider_guards(provider)
        text_chat = provider.text_chat
        with private_review_scope(provider, preflight):
            return await text_chat(
                system_prompt=system_prompt,
                prompt=prompt,
                func_tool=None,
                image_urls=None,
                audio_urls=None,
                contexts=[],
                request_max_retries=1,
                max_tokens=max_tokens,
            )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except PrivateReviewModelError:
        raise
    except BaseException as error:
        marker = getattr(provider, _INSTALL_ATTR, None)
        abort_type = marker.get("abort_type") if isinstance(marker, dict) else None
        if isinstance(abort_type, type) and isinstance(error, abort_type):
            code = getattr(error, "code", PRIVATE_REVIEW_CALL_FAILED)
            if code not in {PRIVATE_REVIEW_CALL_FAILED, PRIVATE_REVIEW_MULTIPLE_ATTEMPTS,
                            PRIVATE_REVIEW_PROVIDER_UNAVAILABLE, PRIVATE_REVIEW_WINDOW_UNAVAILABLE}:
                code = PRIVATE_REVIEW_CALL_FAILED
            raise PrivateReviewModelError(
                code, model_failure=_exception_model_failure(error),
            ) from None
        # Preserve unexpected non-Exception control-flow signals; only normal
        # provider failures receive a fixed public error below.
        if not isinstance(error, Exception):
            raise
        raise PrivateReviewModelError(
            PRIVATE_REVIEW_CALL_FAILED, model_failure=_safe_model_failure(error),
        ) from None


__all__ = [
    "PRIVATE_REVIEW_CALL_FAILED",
    "PRIVATE_REVIEW_MULTIPLE_ATTEMPTS",
    "PRIVATE_REVIEW_PROVIDER_UNAVAILABLE",
    "PRIVATE_REVIEW_WINDOW_UNAVAILABLE",
    "PrivateReviewModelError",
    "generate_private_review",
    "private_review_scope",
]
