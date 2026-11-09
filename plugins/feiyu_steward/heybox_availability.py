"""Reversible pre-send availability gate for the installed Xiaoheihe adapter.

The feed entry gate saves generation work; this gate also covers a reply that
was already being generated when its owner went to sleep. A blocked public
write has no outgoing-attempt row and uses the adapter's durable retry_wait
state without consuming its network-error retry budget.
"""

import asyncio
from contextvars import ContextVar
from datetime import datetime

from .availability import refresh_availability
from .policy import TZ, activity_block, interest_driven, quiet_hours


class _AvailabilityChanged(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def install_heybox_availability(plugin, state, save, *, clock=None):
    """Install on one runtime, returning an idempotent restoration callback.

    Explicit owner comments use proactive=False and event_id=None, and retain
    their native behavior. The event-type lookup also recognizes proactive feed
    events recovered by this adapter, whose normal recovery path drops the flag.
    No HTTP method or global adapter class is changed.
    """
    runtime = plugin.runtime
    repository = runtime.repository
    now_fn = clock or (lambda: datetime.now(TZ))
    active = ContextVar("feiyu_heybox_proactive_write", default=False)
    deferred = ContextVar("feiyu_heybox_deferred_write", default=None)
    original_deliver = runtime.deliver
    original_attempt = repository.record_outgoing_attempt
    original_finish = repository.finish_feed_candidate_send

    def block_reason():
        now = now_fn()
        if refresh_availability(state, now):
            save()
        if interest_driven(state):
            return activity_block(state, now)
        if quiet_hours(now):
            return "quiet_hours"
        if state.get("dnd"):
            return "user_dnd"
        return ""

    async def defer(kwargs, reason):
        now = now_fn()
        delay = 1800.0
        if not interest_driven(state) and quiet_hours(now):
            wake = now.astimezone(TZ).replace(hour=8, minute=0, second=0, microsecond=0)
            delay = max(1.0, (wake - now).total_seconds())
        elif not interest_driven(state) and state.get("dnd_reason") in {"natural_sleep", "natural_busy"}:
            until = float(state.get("dnd_until", 0))
            if until > now.timestamp():
                delay = max(1.0, until - now.timestamp())
        event_id = kwargs.get("event_id")
        if event_id is not None:
            await repository.defer_event(
                event_id, "feiyu_availability:" + reason, delay_seconds=delay
            )
        deferred.set((asyncio.current_task(), reason))
        return {
            "status": "retry_wait",
            "reason": reason,
            "external_comment_id": "",
            "retry_after_seconds": delay,
        }

    async def guarded_attempt(*args, **kwargs):
        status = kwargs.get("status", args[4] if len(args) > 4 else None)
        # Native deliver calls this while holding its per-floor send lock,
        # immediately before recording and performing the actual HTTP write.
        if active.get() and status == "sending":
            reason = block_reason()
            if reason:
                raise _AvailabilityChanged(reason)
        return await original_attempt(*args, **kwargs)

    async def guarded_deliver(**kwargs):
        deferred.set(None)
        proactive = bool(kwargs.get("proactive", False))
        recovered_feed = False
        if not proactive and kwargs.get("event_id") is not None:
            row = await repository.db.fetchone(
                "SELECT event_type FROM incoming_events WHERE id = ?",
                (kwargs["event_id"],),
            )
            recovered_feed = bool(row and row["event_type"] == "proactive_feed")
            proactive = recovered_feed
        if not proactive:
            return await original_deliver(**kwargs)
        if recovered_feed:
            feed = runtime.config.snapshot()["proactive_feed"]
            if not feed.get("enabled", False):
                return await defer(kwargs, "feed_disabled")
            # Recovery uses the profile's general dry-run flag. Retain the
            # original feed's current publication/review policy instead.
            kwargs = {**kwargs, "proactive": True, "dry_run": bool(feed.get("dry_run", True))}
            if feed.get("review_required", True):
                candidate_id = await runtime.capture_feed_candidate(
                    event_id=kwargs["event_id"], route=kwargs["route"],
                    content=kwargs["content"], metadata={
                        "candidate_reason": "恢复的主动刷帖候选，等待审核"
                    }, generated_ms=kwargs.get("generated_ms"),
                )
                return {"status": "pending_review", "candidate_id": candidate_id,
                        "external_comment_id": ""}
        existing_attempt = None
        if kwargs.get("event_id") is not None:
            existing_attempt = await repository.latest_outgoing_for_event(kwargs["event_id"])
        # Let the native duplicate/uncertain-result branches reconcile existing
        # attempts. DND must never demote a confirmed sent or unknown write to
        # retry_wait. The in-lock attempt gate still protects any new HTTP write.
        reconcile_only = bool(existing_attempt and existing_attempt.get("status") != "retry_wait")
        if not kwargs.get("dry_run", False) and not reconcile_only:
            reason = block_reason()
            if reason:
                return await defer(kwargs, reason)
        token = active.set(True)
        try:
            return await original_deliver(**kwargs)
        except _AvailabilityChanged as exc:
            return await defer(kwargs, exc.reason)
        finally:
            active.reset(token)

    async def guarded_finish(candidate_id, status, edited_text, *, sent_comment_id=""):
        marker = deferred.get()
        if (status == "sent" and not sent_comment_id and marker
                and marker[0] is asyncio.current_task()):
            # Native approve assumes every deliver return has a real comment
            # ID. Restore its claimed candidate rather than manufacturing a
            # success or turning an intentional pause into a failed draft.
            await repository.db.execute(
                "UPDATE feed_candidates SET status='approved', edited_text=?, "
                "reviewed_at=? WHERE id=? AND status='sending'",
                (edited_text, now_fn().timestamp(), candidate_id),
            )
            deferred.set(None)
            raise ValueError("主动评论已暂缓，保留待发布候选：" + marker[1])
        return await original_finish(
            candidate_id, status, edited_text, sent_comment_id=sent_comment_id
        )

    runtime.deliver = guarded_deliver
    repository.record_outgoing_attempt = guarded_attempt
    repository.finish_feed_candidate_send = guarded_finish

    def restore():
        for obj, name, installed, original in (
            (runtime, "deliver", guarded_deliver, original_deliver),
            (repository, "record_outgoing_attempt", guarded_attempt, original_attempt),
            (repository, "finish_feed_candidate_send", guarded_finish, original_finish),
        ):
            if getattr(obj, name) is installed:
                setattr(obj, name, original)

    return restore
