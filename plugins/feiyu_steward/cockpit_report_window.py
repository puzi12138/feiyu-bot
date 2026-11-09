"""Local numeric reports allow minute windows without changing official gates."""
import math
from datetime import datetime

from .token_report_gate import TZ, freeze_report_window as _freeze, validate_report_window as _validate


def _stamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 48:
        raise ValueError('invalid_report_window')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('invalid_report_window')
    return result.astimezone(TZ)


def validate_report_window(window):
    if (isinstance(window, dict) and window.get('kind') == 'rolling'
            and set(window) == {'kind', 'start', 'end'}):
        begin, end = _stamp(window['start']), _stamp(window['end'])
        if 60 <= (end - begin).total_seconds() < 1800:
            return {'kind': 'rolling', 'start': begin.isoformat(), 'end': end.isoformat()}
    return _validate(window)


def freeze_report_window(summary):
    if isinstance(summary, dict) and 'window_hours' in summary:
        hours = summary['window_hours']
        if type(hours) in (int, float) and math.isfinite(hours) and 1 / 60 <= hours < .5:
            begin, end = _stamp(summary.get('from')), _stamp(summary.get('until'))
            if abs((end - begin).total_seconds() - hours * 3600) >= 1:
                raise ValueError('invalid_report_window')
            return validate_report_window({'kind': 'rolling', 'start': begin.isoformat(), 'end': end.isoformat()})
    return _freeze(summary)
