"""Bridge one native freshly admitted post to the current incident owner."""

import asyncio
import sys
from contextlib import asynccontextmanager


@asynccontextmanager
async def processing_scope(adapter, notification, event_id):
    """Bind processing only after the adapter's native claim and reservation.

    Args:
        adapter: The current native adapter instance.
        notification: Its exact freshly claimed notification object.
        event_id: The native durable event identifier.

    Yields:
        Whether the current incident owner permits this processing attempt.
    """
    module = sys.modules.get("data.plugins.feiyu_steward.incident_hooks")
    factory = getattr(module, "fresh_heybox_processing", None)
    if not callable(factory):
        yield False
        return
    async with factory(adapter, notification, event_id) as allowed:
        yield allowed is True


def consume_ready_wake(adapter, feed):
    """Consume only the current owner's one ready signal on its native feed task.

    Args:
        adapter: The native adapter owning the feed task and wake event.
        feed: The exact feed object about to perform its next guarded cycle.

    Returns:
        Whether a confirmed new-content opportunity requests one immediate cycle.
    """
    wake = getattr(adapter, "_retry_wake", None)
    if (not isinstance(wake, asyncio.Event) or not wake.is_set()
            or getattr(adapter, "_feed_task", None) is not asyncio.current_task()):
        return False
    # A stale, forged or retired signal grants no authority and must not spin.
    wake.clear()
    module = sys.modules.get("data.plugins.feiyu_steward.incident_hooks")
    consume = getattr(module, "consume_heybox_retry_wake", None)
    return callable(consume) and consume(adapter, feed) is True


async def wait_feed_delay(adapter, feed, delay):
    """Wait normally unless one verified retry wakes the existing serial loop.

    Args:
        adapter: The native adapter, whose refresh flag is never changed here.
        feed: Its current feed object, used only to validate a retry signal.
        delay: The unchanged ordinary delay or error backoff, in seconds.

    Returns:
        Whether one ready retry interrupted this wait. Refresh and stop return False.

    Raises:
        asyncio.CancelledError: If the owning feed task is cancelled.
    """
    deadline = asyncio.get_running_loop().time() + max(0.0, delay)
    while not adapter._stop.is_set() and not adapter._refresh.is_set():
        if consume_ready_wake(adapter, feed):
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        waiters = [asyncio.create_task(flag.wait()) for flag in
                   (adapter._refresh, adapter._stop, adapter._retry_wake)]
        try:
            await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
    return False
