"""Enforce the owner's publication budget at the native Qzone service boundary."""

import hashlib
import json
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime
from functools import wraps

from .policy import TZ, qzone_publication_usage
from .peak_policy import PeakTariffPaused
from .peak_runtime import business_paused

_RESERVED_ROUTE = ContextVar("feiyu_qzone_reserved_route", default=False)
_ATTEMPTED = {"publishing", "sending", "sent", "unknown", "verified",
              "sent_unverified", "failed_or_uncertain", "deleted_verified"}


@contextmanager
def qzone_publication_route():
    """Mark one service call whose steward sender already durably reserved a slot."""
    token = _RESERVED_ROUTE.set(True)
    try:
        yield
    finally:
        _RESERVED_ROUTE.reset(token)


def _request_key(post, kwargs):
    identifier = getattr(post, "id", None)
    if identifier is not None:
        identity = f"draft:{identifier}"
    else:
        text = getattr(post, "text", kwargs.get("text", "")) or ""
        images = getattr(post, "images", kwargs.get("images", [])) or []
        media = [hashlib.sha256(value if isinstance(value, bytes) else str(value).encode()).hexdigest()
                 for value in images]
        identity = json.dumps([text, media], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode()).hexdigest()


def _prior_attempt(state, post, key):
    if getattr(post, "tid", None):
        return True
    ledger = state.get("qzone_publication_attempts", {})
    if isinstance(ledger.get(key), dict):
        return True
    identifier = getattr(post, "id", None)
    if identifier is None:
        return False
    for row in state.get("candidates", []):
        if (isinstance(row, dict) and str(row.get("id")) == str(identifier)
                and (row.get("publication_attempt_at")
                     or row.get("status") in _ATTEMPTED and row.get("published_day"))):
            return True
    for row in state.get("comment_share_receipts", {}).values():
        if (isinstance(row, dict) and row.get("destination") == "qzone"
                and str(row.get("draft_id")) == str(identifier)
                and row.get("status") in _ATTEMPTED):
            return True
    return any(isinstance(row, dict) and str(row.get("draft_id")) == str(identifier)
               for row in ledger.values())


def install_qzone_publication_guard(steward, qzone):
    """Wrap the current native service; return an identity-safe restore callback."""
    service = qzone.service
    current = service.publish_post
    metadata = getattr(current, "__dict__", {})
    if metadata.get("_feiyu_qzone_guard_owner") is steward:
        return metadata["_feiyu_qzone_guard_restore"]
    previous_restore = metadata.get("_feiyu_qzone_guard_restore")
    if callable(previous_restore):
        previous_restore()
    original = service.publish_post

    @wraps(original)
    async def guarded(*args, **kwargs):
        if business_paused():
            raise PeakTariffPaused("automatic_qzone_publication_paused_during_peak")
        if _RESERVED_ROUTE.get():
            # Consume the marker before any await: a nested call or a second
            # publication in the same context still needs its own reservation.
            _RESERVED_ROUTE.set(False)
            return await original(*args, **kwargs)
        post = kwargs.get("post", args[0] if args else None)
        if post is None and not kwargs.get("text") and not kwargs.get("images"):
            raise ValueError("post、text、images 不能同时为空")
        key = _request_key(post, kwargs)
        async with steward.lock:
            if business_paused():
                raise PeakTariffPaused("automatic_qzone_publication_paused_during_peak")
            if _prior_attempt(steward.state, post, key):
                raise PermissionError("这条QQ空间发表已有发送记录，不能重复发送；请先核实实际状态。")
            now = datetime.now(TZ)
            usage = qzone_publication_usage(steward.state, now)
            if not usage["remaining"]:
                raise PermissionError(f"QQ空间今日已达发布上限（{usage['daily_limit']}条），明天再发。")
            ledger = steward.state.setdefault("qzone_publication_attempts", {})
            row = {"at": now.timestamp(), "publication_attempt_at": now.timestamp(), "status": "sending"}
            if getattr(post, "id", None) is not None:
                row["draft_id"] = post.id
            if getattr(post, "uin", None):
                row["uin"] = post.uin
            ledger[key] = row
            # Persist before touching the platform. A storage failure must not
            # turn an unrecorded publication into a retryable operation.
            steward.save()
            try:
                result = await original(*args, **kwargs)
                published = result if result is not None else post
                if not getattr(published, "tid", None):
                    raise ValueError("qzone_publication_receipt_missing")
                row.update(status="sent", tid=str(published.tid))
                if getattr(published, "id", None) is not None:
                    row["draft_id"] = published.id
                steward.save()
                return result
            except BaseException as error:
                row.update(status="unknown", reason=type(error).__name__)
                with suppress(Exception):
                    steward.save()
                raise

    def restore():
        if service.publish_post is guarded:
            service.publish_post = original

    guarded._feiyu_qzone_guard_owner = steward
    guarded._feiyu_qzone_guard_restore = restore
    service.publish_post = guarded
    return restore
