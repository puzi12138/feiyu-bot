"""Fixed official website periods, bounded numeric snapshots and no QQ I/O."""
import asyncio
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.request import urlopen

from .policy import TZ
from .peak_policy import automatic_peak_paused

ROOT = Path('D:/QQBot')
DIRECTORY = ROOT / 'data/deepseek-billing'
MODE = 'official_website_v1'
PERIODS = ('today', 'yesterday', 'last7', 'day')
MAX_AGE = 15 * 60
COMMANDS = {name: 'summary_' + name for name in PERIODS if name != 'day'}


def unsupported_period():
    return {'ok': True, 'sent': False, 'queued': False, 'status': 'unsupported_period',
            'report_mode': MODE, 'supported_periods': list(PERIODS),
            'note': '现在按官网周期查询：今天、昨天、近7天或指定自然日。小时、分钟和精确起止时刻不再作为查询范围；请选一个官网周期。'}


def unavailable(reason='official_data_unavailable'):
    return {'ok': False, 'sent': False, 'queued': False, 'status': reason,
            'report_mode': MODE, 'supported_periods': list(PERIODS),
            'note': ('官网登录已失效，请在专用官网窗口完成登录后再查询。' if reason == 'official_login_required'
                     else '官网数据暂未更新成功，请稍后查询今天、昨天或近7天。'),
            'text': '官网数据暂未更新成功，请稍后查询。'}


def _read_json(path, max_size=32768):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_size:
        raise ValueError('invalid_snapshot')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _stamp(value):
    if not isinstance(value, str) or len(value) > 48:
        raise ValueError('invalid_snapshot')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('invalid_snapshot')
    return stamp.astimezone(TZ)


def _counts(raw):
    if not isinstance(raw, dict) or set(raw) != {'cost_cny', 'request_count', 'total_tokens'}:
        raise ValueError('invalid_snapshot')
    for field in ('request_count', 'total_tokens'):
        if type(raw[field]) is not int or not 0 <= raw[field] <= 10**12:
            raise ValueError('invalid_snapshot')
    cost = raw['cost_cny']
    if not isinstance(cost, str) or len(cost) > 40 or not re.fullmatch(r'\d+(?:\.\d{1,12})?', cost):
        raise ValueError('invalid_snapshot')
    if not 0 <= Decimal(cost) <= 10**12:
        raise ValueError('invalid_snapshot')
    if raw['request_count'] == 0 and (raw['total_tokens'] != 0 or Decimal(cost) != 0):
        raise ValueError('invalid_snapshot')
    return dict(raw)


def validate_snapshot(raw, period, *, now=None, request_id=None, max_age=MAX_AGE):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if period not in COMMANDS or not isinstance(raw, dict):
        raise ValueError('invalid_snapshot')
    expected = ({'kind': 'last_n_days', 'days': 7} if period == 'last7'
                else {'kind': 'calendar_day', 'selection': period})
    if (raw.get('schema') != 'deepseek_official_period_summary_v1'
            or raw.get('source') != 'official_visible_usage_headlines'
            or raw.get('period') != period or raw.get('relative_range') != expected
            or raw.get('key_filter') != 'all' or raw.get('currency') != 'CNY'
            or raw.get('timezone') != 'UTC+08:00' or raw.get('summary_stable_readback') is not True
            or raw.get('export_requested') is not False or raw.get('data_lag_minutes') != 5
            or not re.fullmatch(r'[a-f0-9]{32}', str(raw.get('request_id', '')))
            or request_id is not None and raw.get('request_id') != request_id):
        raise ValueError('invalid_snapshot')
    observed = _stamp(raw.get('observed_at'))
    if not 0 <= (now - observed).total_seconds() <= max_age or observed.date() != now.date():
        raise ValueError('stale_snapshot')
    if period != 'last7':
        day = (observed.date() - timedelta(days=period == 'yesterday')).isoformat()
        if raw.get('day') != day:
            raise ValueError('invalid_snapshot')
    else:
        day = None
        if raw.get('day') is not None or raw.get('absolute_interval') is not None:
            raise ValueError('invalid_snapshot')
    return {'report_mode': MODE, 'period': period, 'day': day, 'days': 7 if period == 'last7' else 1,
            'period_label': '官网近7天（含今天）' if period == 'last7' else day,
            'observed_at': observed.isoformat(), 'source': raw['source'], 'key_filter': 'all',
            'data_lag_minutes': 5, 'fresh': True, **_counts(raw.get('summary'))}


def build_result(summary):
    stamp = _stamp(summary['observed_at'])
    label = summary['period_label'] + (' · UTC+8自然日' if summary['period'] != 'last7' else ' · UTC+8')
    text = '\n'.join(('肥鱼的 Token 手账', label,
        f"{summary['total_tokens']:,} token · {summary['request_count']:,} 次请求",
        '官网消费金额 ¥' + summary['cost_cny'],
        '数据更新时间：' + stamp.strftime('%Y-%m-%d %H:%M') + '（官网数据可能延迟约5分钟）'))
    return {'ok': True, 'report_mode': MODE, 'summary': summary, 'text': text,
            'timezone': 'Asia/Shanghai', 'source': 'official_website', 'queued': False}


def cached_period(period, *, now=None):
    try:
        return build_result(validate_snapshot(_read_json(DIRECTORY / f'browser-summary-{period}.json'), period, now=now))
    except (OSError, ValueError, TypeError, OverflowError, ArithmeticError):
        return unavailable()


def historical_day(day, *, now=None):
    """Historical imports must prove a full natural day and ALL Key scope."""
    from .official_billing import REPORTS, IMPORTS, NAME, _snapshot, _stamp as billing_stamp
    current = now or datetime.now(TZ)
    try:
        if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            return unsupported_period()
        start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=TZ)
        end = start + timedelta(days=1)
        candidates = []
        for path in REPORTS.iterdir():
            if not NAME.fullmatch(path.name):
                continue
            try:
                item = _snapshot(path, IMPORTS, current)
                if item['key_filter'] != 'all' or end > item['exported'] - timedelta(minutes=5):
                    continue
                exact = [row for row in item['days'] if billing_stamp(row['start']) == start and billing_stamp(row['end']) == end]
                if len(exact) == 1:
                    candidates.append((item['exported'], exact[0]))
            except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
                continue
        if not candidates:
            return unavailable('official_day_not_exported')
        observed, row = max(candidates, key=lambda pair: pair[0])
        if any(stamp == observed and other != row for stamp, other in candidates):
            return unavailable('official_snapshot_ambiguous')
        return build_result({'report_mode': MODE, 'period': 'day', 'day': day, 'days': 1,
            'period_label': day, 'observed_at': observed.isoformat(), 'source': 'official_daily_csv',
            'key_filter': 'all', 'data_lag_minutes': 5, 'fresh': True,
            **_counts({'total_tokens': row['total_tokens'], 'request_count': row['request_count'], 'cost_cny': row['cost_cny']})})
    except (OSError, ValueError, TypeError, OverflowError, KeyError):
        return unavailable('official_day_not_exported')


def _worker_state():
    # Fixed loopback API; response is reduced to one allowlisted state.
    try:
        with urlopen('http://127.0.0.1:16302/api/status', timeout=3) as response:
            raw = json.loads(response.read(1024 * 1024))
        value = raw.get('browser_state', {}).get('state')
        return value if value in ('closed', 'opening', 'ready', 'export_available', 'login_required',
                                  'received_pending_review', 'export_needs_range_review', 'error', 'unavailable') else 'unavailable'
    except Exception:
        return 'unavailable'


def _windows_process_identity(pid):
    """Return a process birth time or 'dead'; uncertainty stays unknown."""
    if os.name != 'nt' or type(pid) is not int or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return 'dead' if ctypes.get_last_error() == 87 else None
    try:
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        exit_code = wintypes.DWORD()
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != 259:  # STILL_ACTIVE
            return 'dead'
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _recover_dead_lease(lease, command):
    """Release only a proven dead owner's lock; never disturb a command."""
    if os.name != 'nt':
        return False
    import msvcrt
    gate = lease.with_name('official-query-recovery.lock')
    fd = None
    locked = False
    try:
        if gate.is_symlink():
            return False
        fd = os.open(gate, os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        locked = True
        if command.exists() or lease.is_symlink():
            return False
        info = _read_json(lease, max_size=1024)
        if (set(info) != {'schema', 'pid', 'process_birth', 'lease_id'}
                or info.get('schema') != 1 or type(info.get('pid')) is not int
                or type(info.get('process_birth')) is not int
                or not re.fullmatch(r'[a-f0-9]{32}', str(info.get('lease_id', '')))):
            return False
        current = _windows_process_identity(info['pid'])
        if current is None or current == info['process_birth']:
            return False
        # The gate serializes competing recoveries. Keep an old, unproven
        # command for the worker; it must never be overwritten.
        if command.exists() or _read_json(lease, max_size=1024) != info:
            return False
        lease.unlink()
        return True
    except (OSError, ValueError, TypeError, OverflowError):
        return False
    finally:
        if fd is not None:
            if locked:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(fd)


def _acquire_lease(lease, temporary, command, request_id):
    birth = _windows_process_identity(os.getpid())
    if type(birth) is not int:
        raise OSError('lease_identity_unavailable')
    payload = {'schema': 1, 'pid': os.getpid(), 'process_birth': birth, 'lease_id': request_id}
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(payload, output)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, lease)
        except FileExistsError:
            if not _recover_dead_lease(lease, command):
                raise
            os.link(temporary, lease)
        return payload
    finally:
        temporary.unlink(missing_ok=True)


def _release_lease(lease, payload):
    try:
        if not lease.is_symlink() and _read_json(lease, max_size=1024) == payload:
            lease.unlink()
    except (OSError, ValueError, TypeError):
        pass


async def query_period(period, *, refresh=True, force_refresh=False, now=None, automatic=False):
    if automatic and automatic_peak_paused():
        return unavailable('peak_tariff')
    if period not in COMMANDS:
        return unsupported_period()
    if not force_refresh:
        cached = cached_period(period, now=now)
        if cached['ok'] or not refresh:
            return cached
    elif not refresh:
        return unavailable('official_refresh_disabled')
    worker = await asyncio.to_thread(_worker_state)
    if automatic and automatic_peak_paused():
        return unavailable('peak_tariff')
    if worker == 'login_required':
        return unavailable('official_login_required')
    if worker not in ('ready', 'export_available', 'received_pending_review', 'export_needs_range_review'):
        return unavailable('official_browser_unavailable')
    command = DIRECTORY / 'operator-command.json'
    lease = DIRECTORY / 'official-query.lock'
    request_id = uuid.uuid4().hex
    temporary = DIRECTORY / ('official-query-' + request_id + '.pending')
    temporary_lease = DIRECTORY / ('official-query-' + request_id + '.lease.pending')
    lease_payload = None
    try:
        # One exclusive client; never overwrite an operator request or recover an
        # uncertain request by issuing it again.
        lease_payload = _acquire_lease(lease, temporary_lease, command, request_id)
        if command.exists():
            return unavailable('official_query_busy')
        request = {'command': COMMANDS[period], 'request_id': request_id}
        if automatic:
            request['origin'] = 'periodic_reconcile_v1'
        temporary.write_text(json.dumps(request), encoding='utf-8')
        # Atomic create fails if any operator arrived after the existence check.
        if automatic and automatic_peak_paused():
            return unavailable('peak_tariff')
        os.link(temporary, command)
        temporary.unlink()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            try:
                result = _read_json(DIRECTORY / 'operator-result.json')
                if result.get('request_id') != request_id:
                    continue
                if result.get('state') == 'official_login_required':
                    return unavailable('official_login_required')
                if result.get('state') == 'peak_tariff':
                    return unavailable('peak_tariff')
                if result.get('state') != 'done':
                    return unavailable('official_query_not_completed')
                raw = _read_json(DIRECTORY / f'browser-summary-{period}.json')
                return build_result(validate_snapshot(raw, period, request_id=request_id))
            except (OSError, ValueError, TypeError, OverflowError, ArithmeticError):
                continue
        return unavailable('official_query_timeout')
    except (OSError, ValueError, TypeError):
        return unavailable('official_query_busy')
    finally:
        if lease_payload is not None:
            _release_lease(lease, lease_payload)
        temporary.unlink(missing_ok=True)
        temporary_lease.unlink(missing_ok=True)
