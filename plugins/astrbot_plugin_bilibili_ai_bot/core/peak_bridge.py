"""Resolve the current steward tariff policy after either plugin reloads."""

from datetime import datetime, timedelta, timezone
from importlib import import_module
import time

TZ = timezone(timedelta(hours=8))


def automatic_bili_allowed(at=None):
    """Allow automatic browsing only in verified daytime off-peak hours.

    Args:
        at: Optional Unix timestamp for a policy check.

    Returns:
        True only within the verified local window; errors fail closed.
    """
    try:
        stamp = time.time() if at is None else at
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return False
        local = datetime.fromtimestamp(stamp, TZ)
        guard = import_module("data.plugins.feiyu_steward.peak_runtime")
        return 8 <= local.hour < 22 and guard.automatic_peak_paused(stamp) is False
    except Exception:
        return False


def peak_active(at=None):
    """Consult the currently loaded policy, pausing if it is unavailable."""
    try:
        return import_module("data.plugins.feiyu_steward.peak_policy").peak_active(at)
    except (ImportError, AttributeError):
        return True


def manual_bili_request(event):
    """Require a verified human owner's explicit browsing request."""
    try:
        return import_module("data.plugins.feiyu_steward.peak_policy").manual_bili_request(event)
    except (ImportError, AttributeError):
        return False


"""Resolve the shared peak policy at each automatic business boundary."""
from contextlib import nullcontext

_MODULE = "data.plugins.feiyu_steward.peak_runtime"


def automatic_peak_paused():
    try:
        return import_module(_MODULE).automatic_peak_paused() is not False
    except Exception:
        return True


def business_paused():
    try:
        return import_module(_MODULE).business_paused() is not False
    except Exception:
        return True


def automatic_scope():
    try:
        return import_module(_MODULE).automatic_scope()
    except Exception:
        return nullcontext()
