"""Resolve the shared peak policy at each automatic business boundary."""
from contextlib import nullcontext
from importlib import import_module
import sys

_MODULE = "data.plugins.feiyu_steward.peak_runtime"
_HEYBOX_MODULE = "data.plugins.feiyu_steward.heybox_peak"
_INCIDENT_MODULE = "data.plugins.feiyu_steward.incident_hooks"


def incident_notice_ready(service, operation):
    """Allow a service entry only through its current incident wrapper."""
    try:
        module = sys.modules.get(_INCIDENT_MODULE)
        check = getattr(module, "notice_service_ready", None)
        return callable(check) and check(service, operation) is True
    except Exception:
        return False


def incident_feed_ready(feed):
    """Fail closed until the live automatic feed boundary is attached."""
    try:
        module = sys.modules.get(_INCIDENT_MODULE)
        check = getattr(module, "automatic_feed_ready", None)
        return callable(check) and check(feed) is True
    except Exception:
        return False


def feed_resume_snapshot(feed):
    """Capture a live recovery generation before the native feed fetch."""
    try:
        module = sys.modules.get(_INCIDENT_MODULE)
        check = getattr(module, "automatic_feed_snapshot", None)
        return check(feed) if callable(check) else None
    except Exception:
        return None


def feed_scope_issuer(feed, snapshot):
    try:
        module = sys.modules.get(_INCIDENT_MODULE)
        return module.automatic_feed_issuer(feed, snapshot)
    except Exception:
        return None


def feed_snapshot_current(feed, snapshot, issuer):
    try:
        module = sys.modules.get(_INCIDENT_MODULE)
        return issuer is not None and snapshot is not None and module.automatic_feed_snapshot(feed, issuer=issuer) == snapshot
    except Exception:
        return False


def feed_post_allowed(feed, post, snapshot=None):
    """Fail closed before reserving a native feed candidate's processing slot."""
    try:
        module = sys.modules.get(_INCIDENT_MODULE)
        check = getattr(module, "automatic_feed_post_allowed", None)
        return callable(check) and check(feed, post, snapshot=snapshot) is True
    except Exception:
        return False


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


def feed_paused(feed):
    if not incident_feed_ready(feed):
        return True
    if not automatic_peak_paused():
        return False
    try:
        return import_module(_HEYBOX_MODULE).feed_active(feed) is not True
    except Exception:
        return True


def feed_scope(adapter, feed):
    try:
        return import_module(_HEYBOX_MODULE).feed_scope(adapter, feed)
    except Exception:
        return nullcontext(False)


def candidate_scope(adapter, notification, event_id):
    try:
        return import_module(_HEYBOX_MODULE).candidate_scope(adapter, notification, event_id)
    except Exception:
        return nullcontext(False)


def register_event(adapter, event):
    try:
        return import_module(_HEYBOX_MODULE).register_event(adapter, event)
    except Exception:
        return nullcontext(False)


def gate_allowed(repository, event_id, route):
    try:
        return import_module(_HEYBOX_MODULE).gate_allowed(repository, event_id, route) is True
    except Exception:
        return False


def tracked_event(repository, event_id):
    try:
        return import_module(_HEYBOX_MODULE).tracked_event(repository, event_id) is True
    except Exception:
        return False


def revoke_event(repository, event_id):
    try:
        import_module(_HEYBOX_MODULE).revoke_event(repository, event_id)
    except Exception:
        pass


def feed_request_allowed(client, endpoint):
    try:
        return import_module(_HEYBOX_MODULE).feed_request_allowed(client, endpoint) is True
    except Exception:
        return False


def auxiliary_model_scope(event, checked, phase, session, request):
    try:
        return import_module(_HEYBOX_MODULE).auxiliary_model_scope(event, checked, phase, session, request)
    except Exception:
        return nullcontext(False)
