"""Strict local state persistence; recovery always requires an explicit action.

Atomic rename alone does not make file contents durable.  Prepare and fsync a
unique sibling, verify its bytes, preserve the previous validated generation,
then replace the live file.  A corrupt live file is never silently replaced or
loaded from a historical copy: that could rewind send deduplication metadata.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import uuid


class StateIntegrityError(ValueError):
    """Persisted state is unusable; its contents must not enter logs."""


def _reject_constant(_value):
    raise StateIntegrityError("nonfinite_state_number")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateIntegrityError("duplicate_state_key")
        result[key] = value
    return result


def _decode(payload):
    try:
        value = json.loads(payload.decode("utf-8"),
                           parse_constant=_reject_constant,
                           object_pairs_hook=_object)
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise StateIntegrityError("invalid_state_json") from error
    if not isinstance(value, dict):
        raise StateIntegrityError("state_object_required")
    return value


def load_state(path):
    """Read a state object, without creating it or falling back to old data."""
    return _decode(Path(path).read_bytes())


def _write_verified(path, payload):
    """Create an exclusive sibling and verify its durable content before use."""
    with path.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    actual = path.read_bytes()
    if actual != payload:
        raise StateIntegrityError("state_write_verification_failed")
    _decode(actual)


def save_state(path, value):
    """Persist one validated generation while retaining a validated predecessor.

    This synchronous helper is called on AstrBot's event-loop thread.  It does
    not claim a transaction across processes or immunity to storage failure.
    Every temporary file is unique so abandoned writes cannot be reused.
    """
    if not isinstance(value, dict):
        raise StateIntegrityError("state_object_required")
    try:
        payload = (json.dumps(value, ensure_ascii=False, indent=2,
                              allow_nan=False) + "\n").encode("utf-8")
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise StateIntegrityError("state_serialization_failed") from error
    _decode(payload)
    path = Path(path)
    previous = path.with_name(path.name + ".previous")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    previous_tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".previous.tmp")
    # Validate before writing anything: do not erase a corrupt source or turn
    # it into the only retained predecessor of otherwise useful in-memory data.
    old = path.read_bytes() if path.exists() else None
    if old is not None:
        _decode(old)
    try:
        _write_verified(temporary, payload)
        if old is not None:
            _write_verified(previous_tmp, old)
            os.replace(previous_tmp, previous)
        os.replace(temporary, path)
        # Replacement uses the already fsynced, byte-verified file.  A final
        # read detects external interference; failures never trigger replay.
        if path.read_bytes() != payload:
            raise StateIntegrityError("state_replace_verification_failed")
    finally:
        for item in (temporary, previous_tmp):
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass
