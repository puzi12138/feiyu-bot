"""Bounded diagnostics containing only time, entry point and fixed reason codes."""
import json
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()
MAX_BYTES = 512 * 1024
REASONS = frozenset({
    'ambiguous_source', 'context_unavailable', 'ambiguous_period', 'unsupported_period',
    'invalid_usage_window', 'unsupported_feature_filter', 'unverified_scope',
    'status_scope_unverified', 'source_feature_unavailable', 'explicit_usage_request_required',
    'owner_private_only', 'query_unavailable', 'delivery_unconfirmed',
    'local_report_unavailable', 'local_reports_unavailable', 'local_report_interval_mismatch',
    'unsupported_feature', 'unsupported_source', 'unsupported_feature_source',
    'message_identity_unavailable', 'receipt_store_unavailable', 'legacy_queue_unavailable',
    'legacy_request_paused', 'qq_identity_unavailable', 'qq_unavailable',
    'report_intent_persist_failed', 'unknown', 'intent', 'unsupported_request_kind',
    'unsupported_filter', 'ambiguous_feature', 'invalid_request',
    'non_query_intent', 'official_login_required', 'official_data_unavailable',
})


def safe_reason(value, default='query_unavailable'):
    return value if isinstance(value, str) and value in REASONS else default


def _linked(path):
    return (path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
            or path.exists() and path.is_file() and path.stat().st_nlink > 1)


def _windows_check_handle(handle, path, *, directory):
    """Verify path, reparse attributes and link count from an open handle."""
    import ctypes
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD),
                    ('created', wintypes.FILETIME), ('accessed', wintypes.FILETIME),
                    ('modified', wintypes.FILETIME), ('volume', wintypes.DWORD),
                    ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD),
                    ('links', wintypes.DWORD), ('index_high', wintypes.DWORD),
                    ('index_low', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    information = kernel.GetFileInformationByHandle
    information.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    information.restype = wintypes.BOOL
    info = FileInformation()
    if (not information(handle, ctypes.byref(info)) or info.attributes & 0x400
            or bool(info.attributes & 0x10) != directory
            or not directory and info.links != 1):
        raise OSError('diagnostic_link_or_wrong_type')
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = final_path(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise OSError('diagnostic_final_path_unavailable')
    actual = buffer.value
    if actual.startswith('\\\\?\\UNC\\'):
        actual = '\\\\' + actual[8:]
    elif actual.startswith('\\\\?\\'):
        actual = actual[4:]
    if os.path.normcase(os.path.normpath(actual)) != os.path.normcase(os.path.abspath(path)):
        raise OSError('diagnostic_path_mismatch')


def _open_checked(path):
    """Open an append-only regular file and verify its handle before writing."""
    if os.name == 'nt':
        import _winapi
        import msvcrt

        # Open a reparse point itself, never its target. Disallow new writers
        # and renames while the checked file handle is in use.
        handle = _winapi.CreateFile(str(path), 0x0004 | 0x0080, 0x0001, 0,
                                    4, 0x00000080 | 0x00200000, 0)
        try:
            _windows_check_handle(handle, path, directory=False)
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND | os.O_BINARY)
        except Exception:
            _winapi.CloseHandle(handle)
            raise
    else:
        nofollow = getattr(os, 'O_NOFOLLOW', None)
        if nofollow is None:
            raise OSError('diagnostic_nofollow_unavailable')
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise OSError('diagnostic_link_or_nonfile')
        return fd
    except Exception:
        os.close(fd)
        raise


def record_usage_diagnostic(root, entrypoint, reason):
    """Logging failure must never change a query or its delivery state."""
    if entrypoint not in ('direct', 'model') or reason not in REASONS:
        return False
    try:
        base = Path(root).absolute()
        directory = base / 'logs' / 'token-query'
        path, previous = directory / 'decisions.jsonl', directory / 'decisions.previous.jsonl'
        with _LOCK:
            if any(_linked(item) for item in (base, base / 'logs', directory, path, previous)):
                return False
            parent_handles = []
            try:
                if os.name == 'nt':
                    import _winapi
                    for item in (base, base / 'logs', directory):
                        if item != base:
                            item.mkdir(exist_ok=True)
                        # Hold each verified directory without sharing delete,
                        # so ordinary replacement cannot redirect the next open.
                        handle = _winapi.CreateFile(str(item), 0x0080, 0x0003, 0, 3,
                                                    0x02000000 | 0x00200000, 0)
                        parent_handles.append(handle)
                        _windows_check_handle(handle, item, directory=True)
                else:
                    directory.mkdir(parents=True, exist_ok=True)
                record = {'at_utc': datetime.now(timezone.utc).isoformat(),
                          'entrypoint': entrypoint, 'reason': reason}
                fd = _open_checked(path)
                try:
                    if os.fstat(fd).st_size >= MAX_BYTES:
                        os.close(fd)
                        fd = None
                        if _linked(path) or _linked(previous):
                            return False
                        os.replace(path, previous)
                        fd = _open_checked(path)
                    data = (json.dumps(record, ensure_ascii=True) + '\n').encode('utf-8')
                    offset = 0
                    while offset < len(data):
                        written = os.write(fd, data[offset:])
                        if written <= 0:
                            raise OSError('diagnostic_short_write')
                        offset += written
                finally:
                    if fd is not None:
                        os.close(fd)
            finally:
                for handle in reversed(parent_handles):
                    _winapi.CloseHandle(handle)
        return True
    except (OSError, ValueError):
        return False
