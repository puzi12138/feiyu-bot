"""Resolve the automatic tariff policy at each plugin-network boundary."""
from contextlib import nullcontext
from importlib import import_module

_MODULE = "data.plugins.feiyu_steward.peak_runtime"


def automatic_peak_paused():
    try:
        return import_module(_MODULE).automatic_peak_paused() is not False
    except Exception:
        return True


def automatic_scope():
    try:
        return import_module(_MODULE).automatic_scope()
    except Exception:
        return nullcontext()
