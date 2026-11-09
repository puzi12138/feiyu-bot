"""Owner-authorized public fan art, without an owner message or private context.

This mixin is only a candidate.  The host must supply the separate
``run_authorized_public_art`` resource boundary and the existing persistent
autopost authorization/slot checks.  This module does not publish or send.
Its durable ledger records an intent before any network submission and never
automatically retries an ambiguous result, even after a process restart.
"""

import asyncio
import base64
import hashlib
import importlib
import inspect
import math
import os
import re
import sqlite3
import sys
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .comic_reference_manifest import (
        ReferenceManifestError,
        default_archive_root,
        default_local_reference_root,
        load_manifest,
        resolve_reference_set,
        select_reference_ids,
    )
except ImportError:  # The production plugin loads this file as a flat module.
    from comic_reference_manifest import (  # type: ignore
        ReferenceManifestError,
        default_archive_root,
        default_local_reference_root,
        load_manifest,
        resolve_reference_set,
        select_reference_ids,
    )


_TZ = ZoneInfo("Asia/Hong_Kong")
_DAILY_LIMIT = 3
# Legacy publication authorization remains readable for the old immediate path.
# New policy-v2 scheduled comics reserve eight pages per day, while the explicit
# owner extra job has a separate four-page ledger and never borrows scheduled
# capacity.
_SCHEDULED_IMAGE_DAILY_LIMIT = 8
_EXTRA_IMAGE_DAILY_LIMIT = 4
_LEGACY_IMAGE_DAILY_LIMIT = 6
_IMAGE_DAILY_LIMIT = _SCHEDULED_IMAGE_DAILY_LIMIT
_COMIC_PAGE_LIMIT = 4
_COMIC_POLICY_VERSION = 2
_NEW_COMIC_RUNTIME_LIMIT = 8
_EXTRA_TASK_SUFFIX = "/owner_extra_ai_friends"
_COMIC_REFERENCE_MANIFEST_ID = "heybox-0-0-approved-v2"
_POST_TIMEOUT = 570.0
_IMAGE_BYTES = 10_000_000
_LEDGER_BYTES = 128_000_000
_MARKER = b"feiyu-public-art-ledger-v1\n"
_SLOT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}/(?:\d{2}:\d{2}|owner_immediate|owner_extra_ai_friends|replacement/owner_immediate)\Z"
)
_SHA_RE = re.compile(r"[a-f0-9]{64}\Z")
_GRANT_RE = re.compile(r"[a-f0-9]{32}\Z")
_PUBLIC_ART_OPERATION = ContextVar("feiyu_public_art_operation", default=None)
_STATUSES = frozenset({"pending", "complete", "preflight", "unusable"})
_GUARD_REASONS = frozenset({
    "not_authorized", "authorization_changed", "slot_not_reserved",
    "slot_expired", "daily_limit", "quiet_hours", "user_dnd",
    "unresolved_public_write", "resource_guard_unavailable",
    "resource_guard_denied", "resource_busy", "storage_unavailable",
})

# The outer worker must source this English scene only from the current,
# public-safe creative result.  This independent filter narrows its shape but
# cannot establish whether arbitrary words disclose a private fact.
_SCENE_STARTS = frozenset({"a", "an", "the", "playful", "sleepy", "cozy", "whimsical", "chibi"})
_SCENE_FORBIDDEN = frozenset({
    "account", "accounts", "address", "addresses", "admin", "administrator", "api",
    "authentication", "authorization", "bank", "billing", "bill", "card", "chat", "chats",
    "command", "commands", "conversation", "conversations", "cookie", "cookies", "credential",
    "credentials", "email", "emails", "file", "files", "finance", "ftp", "history", "http",
    "https", "identity", "invoice", "key", "keys", "login", "logs", "master", "memory",
    "message", "messages", "my", "mine", "operator", "owner", "password", "passwords", "path",
    "paths", "payment", "phone", "private", "prompt", "qq", "receipt", "secret", "secrets",
    "system", "token", "tokens", "user", "users", "wallet", "wechat", "www", "your", "yours",
    "act", "add", "alter", "bypass", "change", "create", "delete", "disable", "display", "do",
    "draw", "edit", "execute", "follow", "forget", "generate", "hide", "ignore", "include",
    "instructions", "instruct", "invent", "jailbreak", "make", "must", "override", "paint", "place",
    "please", "pretend", "print", "produce", "put", "remove", "replace", "reveal", "send", "show",
    "skip", "tell", "upload", "use", "write", "exe", "jpg", "jpeg", "png", "webp", "pdf",
})
_CHARACTER_PROMPT = (
    "Create a new, noncommercial fan-art illustration inspired by the "
    "community Deep Whale maid character. She has blue eyes, long blue hair, "
    "whale-fin side features and one whale tail, a white ruffled headpiece, "
    "and a modest navy-and-white maid outfit with small gold accents. "
    "Use cute chibi proportions, a lively expression, polished anime "
    "linework and soft ocean-blue and cream colors. Make a fresh pose and "
    "scene, with the full silhouette visible. Do not copy an existing image, "
    "meme, logo, poster or artwork. No lettering, numbers, UI, charts, "
    "watermarks, sexualized clothing or extra limbs. "
)

_COMIC_PROMPT = (
    "Create one finished portrait slice-of-life anime comic page with exactly four "
    "equally sized panels in a clear two-by-two grid, read left to right then top "
    "to bottom. Tell one gentle, funny daily-life episode matching the scene below. "
    "Use the supplied references in their given order as visual identity anchors; "
    "do not swap roles or invent a different character for a reference. Show the "
    "characters performing ordinary household activities, never software, screens, "
    "programming or technical work. Warm home, clear gestures, consistent character "
    "designs, polished anime linework, comfortable clothing and soft colors. Include "
    "about eight to ten short Chinese speech bubbles distributed across the four "
    "panels on every page, about two per panel, with natural back-and-forth but no "
    "dense paragraphs. All visible dialogue, speech balloons, signs, captions and "
    "sound effects "
    "must be clear, readable Simplified Chinese. Do not use English, Latin "
    "letters, other languages, invented glyphs or garbled text anywhere in the "
    "artwork. Use simple Chinese expressions; the accompanying post can carry "
    "the narrative. "
    "No watermark, UI, fabricated real events, explicit content or extra limbs. "
)

_COMIC_PROFILE_PROMPTS = {
    "ai_friends": (
        "This is the AI friends series. Supplied reference one is DeepSeek01, the "
        "blue-haired whale-maid Feiyu anchor. References two through four are the "
        "three selected AI friends for this date; keep those three identities stable "
        "across all four pages. Do not add any character or helper sprite that is "
        "not listed in this submission mapping. Use ordinary home activities and a "
        "small visual joke that "
        "develops across the four panels. "
    ),
    "dragon_helpers": (
        "This is the dragon helpers series. Supplied reference one is GPT03, the "
        "silver-white GPT dragon maiden. Reference two is DeepSeek01, the blue-haired "
        "whale-maid Feiyu anchor. Reference three is the approved six-color helper "
        "sprite; use its blue, pink, green, purple, cyan and orange accents for six "
        "tiny chibi dragon helpers. Keep all roles and helper colors stable across all "
        "four pages, with ordinary home activities and a gentle visual joke. "
    ),
}

_REFERENCE_ROLE_LABELS = {
    "deepseek-01": "DeepSeek01／肥鱼蓝发鲸鱼女仆",
    "gpt-03": "GPT03／银白GPT龙娘",
    "claude-05": "Claude05／橙发花饰AI朋友",
    "gemini-07": "Gemini07／蓝紫发AI朋友",
    "glm-09": "GLM09／黑发AI朋友",
    "kimi-11": "Kimi11／白发月亮装饰AI朋友",
    "qwen-15": "Qwen15／蓝发辫子AI朋友",
    "grok-17": "Grok17／金发双马尾AI朋友",
    "minimax-19": "MiniMax19／橙发白帽AI朋友",
    "assistant-icon-reference": "六色Q版小龙娘图标条／颜色轮廓参考",
}


def _reference_role_line(reference_ids):
    """Bind every submitted image index to its exact role and allow-list ID."""
    return "Reference mapping in submission order: " + "; ".join(
        f"reference {index} = {_REFERENCE_ROLE_LABELS.get(item, item)} [{item}]"
        for index, item in enumerate(reference_ids, 1)
    ) + ". "


def _failure(reason):
    return {"ok": False, "reason": reason}


def _valid_scene(value):
    if (not isinstance(value, str) or not 20 <= len(value) <= 200
            or value != value.strip() or not re.fullmatch(r"[A-Za-z0-9 ,.-]+", value, flags=re.ASCII)
            or re.search(r"[.,-]{2}|\d{4,}", value)
            or "." in value[:-1]):
        return False
    words = re.findall(r"[A-Za-z]+|[0-9]+", value.lower(), flags=re.ASCII)
    return bool(len(words) >= 4 and words[0] in _SCENE_STARTS
                and not any(word in _SCENE_FORBIDDEN for word in words))


def _is_link(path):
    """Reject links and, on supported Windows versions, junctions."""
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _slot_day(slot_key):
    if not isinstance(slot_key, str) or not _SLOT_RE.fullmatch(slot_key):
        return None
    try:
        if any(slot_key.endswith(suffix) for suffix in (
                "/owner_immediate", _EXTRA_TASK_SUFFIX, "/replacement/owner_immediate")):
            day = slot_key.split("/", 1)[0]
            parsed_day = date.fromisoformat(day)
            return day if parsed_day.isoformat() == day else None
        parsed = datetime.strptime(slot_key, "%Y-%m-%d/%H:%M")
        return parsed.date().isoformat() if parsed.strftime("%Y-%m-%d/%H:%M") == slot_key else None
    except ValueError:
        return None


def _is_comic_slot(slot_key):
    """Recognize only the daily scheduled slot and the explicit owner job.

    Recognition alone is not authorization: the host still has to validate
    the active attempt, persisted grant, per-page ticket and resource guard.
    """
    return bool(_slot_day(slot_key) and
                (slot_key.endswith("/12:30") or slot_key.endswith("/19:30")
                 or slot_key.endswith("/owner_immediate") or slot_key.endswith(_EXTRA_TASK_SUFFIX)))


def _is_new_comic_slot(slot_key):
    """Return true for policy-v2 scheduled or owner-extra comics."""
    return bool(_slot_day(slot_key) and (
        slot_key.endswith("/12:30") or slot_key.endswith("/19:30")
        or slot_key.endswith(_EXTRA_TASK_SUFFIX)))


def _slot_profile(slot_key, supplied=None):
    """Map a task key to its immutable series profile."""
    if not _is_comic_slot(slot_key):
        return None
    if slot_key.endswith("/12:30") or slot_key.endswith(_EXTRA_TASK_SUFFIX):
        expected = "ai_friends"
    elif slot_key.endswith("/19:30"):
        expected = "dragon_helpers"
    else:
        # The historical immediate comic was the dragon-helper series.
        expected = "dragon_helpers"
    if supplied is not None and supplied != expected:
        return None
    return expected


def _new_comic_authorization(value):
    """Validate the migrated policy-v2 authorization shape."""
    return bool(
        isinstance(value, dict)
        and type(value.get("daily_limit")) is int and value.get("daily_limit") == 2
        and type(value.get("runtime_public_art_daily_limit")) is int
        and value.get("runtime_public_art_daily_limit") == _NEW_COMIC_RUNTIME_LIMIT
        and type(value.get("comic_policy_version")) is int
        and value.get("comic_policy_version") == _COMIC_POLICY_VERSION
        and value.get("scheduled_profiles") == {
            "12:30": "ai_friends", "19:30": "dragon_helpers"
        }
    )


def _extra_capability_ok(host, slot_key, authorization):
    """Check the owner extra's process-local short-lived scope when present."""
    if not slot_key.endswith(_EXTRA_TASK_SUFFIX):
        return True
    verifier = getattr(host, "_validate_owner_extra_comic_capability", None)
    capability = getattr(host, "_owner_extra_comic_capability", None)
    if not callable(verifier) or capability is None:
        return False
    try:
        return verifier(capability) is True
    except Exception:
        return False


def _same_authorization(host, supplied):
    """Trust only a freshly read, existing authorization, not caller input."""
    try:
        reader = getattr(host, "_autopost_authorization", None)
        fresh = reader() if callable(reader) else None
        return bool(
            isinstance(supplied, dict) and isinstance(fresh, dict)
            and fresh == supplied
            and fresh.get("enabled") is True
            and isinstance(fresh.get("grant_id"), str)
            and _GRANT_RE.fullmatch(fresh["grant_id"])
            and fresh.get("public_original_gallery_only") is False
            and fresh.get("runtime_public_art_enabled") is True
            and type(fresh.get("runtime_public_art_daily_limit")) is int
            and fresh["runtime_public_art_daily_limit"] in {_DAILY_LIMIT, _NEW_COMIC_RUNTIME_LIMIT}
            and (fresh.get("comic_policy_version") in (None, _COMIC_POLICY_VERSION))
        )
    except Exception:
        return False


def _ticket_available(host, slot_key, authorization, ticket):
    try:
        checker = getattr(host, "_autopost_generation_authorized", None)
        return bool(
            callable(checker)
            and getattr(host, "_public_art_resource_busy", None) is True
            and isinstance(ticket, dict)
            and getattr(host, "_public_art_resource_ticket", None) is ticket
            and ticket.get("slot_key") == slot_key
            and ticket.get("used") is True
            and checker(slot_key, authorization) is True
        )
    except Exception:
        return False


def _active_resource_grant(host, slot_key, authorization, ticket, scene, binding):
    """Require the exact callback task, scene and one-use capability."""
    try:
        scope = _PUBLIC_ART_OPERATION.get()
        return bool(
            binding is not None
            and _ticket_available(host, slot_key, authorization, ticket)
            and isinstance(scope, tuple) and len(scope) == 7
            and scope[0] is binding and scope[1] is host and scope[2] is ticket
            and scope[3] is asyncio.current_task() and scope[4] == slot_key and scope[5] == scene
            and type(scope[6]) is int and ticket.get("page_number") == scope[6]
        )
    except Exception:
        return False


def _art_root(host):
    """Create only a dedicated output directory under the existing data root."""
    root = Path(host.root)
    autopost = Path(host._heybox_autopost_root)
    if _is_link(root) or _is_link(autopost) or not root.is_dir() or not autopost.is_dir():
        raise ValueError("storage_unavailable")
    if autopost.resolve(strict=True).parent != root.resolve(strict=True):
        raise ValueError("storage_unavailable")
    output = autopost / "generated_art"
    if _is_link(output):
        raise ValueError("storage_unavailable")
    output.mkdir(exist_ok=True)
    if _is_link(output) or not output.is_dir() or output.resolve(strict=True).parent != autopost.resolve(strict=True):
        raise ValueError("storage_unavailable")
    return output


def _ledger_suffix(ledger_name):
    if ledger_name == "public-art":
        return ""
    if ledger_name == "public-art-v2":
        return "-v2"
    if ledger_name == "public-art-extra-ai-friends":
        return "-extra-ai-friends"
    raise ValueError("storage_unavailable")


def _connect_ledger(directory, ledger_name="public-art"):
    """Open/create a fail-closed ledger; never recreate a missing half."""
    suffix = _ledger_suffix(ledger_name)
    marker = directory / ("public-art-ledger" + suffix + ".marker")
    database = directory / ("public-art-ledger" + suffix + ".sqlite3")
    paths = [marker, database] + [Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm")]
    if any(_is_link(path) for path in paths):
        raise ValueError("storage_unavailable")
    marker_exists, database_exists = marker.exists(), database.exists()
    if marker_exists != database_exists:
        raise ValueError("storage_unavailable")
    fresh = not marker_exists
    if fresh and any(path.exists() for path in paths[2:]):
        raise ValueError("storage_unavailable")
    if fresh:
        # Creating the marker first makes interruption during initialization a
        # closed state, rather than silently resetting the generation history.
        with marker.open("xb") as handle:
            handle.write(_MARKER)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        if (not marker.is_file() or marker.stat().st_size != len(_MARKER)
                or marker.read_bytes() != _MARKER or not database.is_file()
                or not 0 < database.stat().st_size <= _LEDGER_BYTES):
            raise ValueError("storage_unavailable")
    connection = sqlite3.connect(database, timeout=1.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=DELETE")
        if fresh:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE attempts (slot_id TEXT PRIMARY KEY, day TEXT NOT NULL, "
                "started_at INTEGER NOT NULL, status TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "width INTEGER NOT NULL, height INTEGER NOT NULL, mismatch INTEGER NOT NULL)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.execute("COMMIT")
        if connection.execute("PRAGMA user_version").fetchone() != (1,):
            raise ValueError("storage_unavailable")
        if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ValueError("storage_unavailable")
        columns = connection.execute("PRAGMA table_info(attempts)").fetchall()
        expected_columns = [
            ("slot_id", "TEXT", 0, 1), ("day", "TEXT", 1, 0),
            ("started_at", "INTEGER", 1, 0), ("status", "TEXT", 1, 0),
            ("sha256", "TEXT", 1, 0), ("width", "INTEGER", 1, 0),
            ("height", "INTEGER", 1, 0), ("mismatch", "INTEGER", 1, 0),
        ]
        if [(row[1], row[2], row[3], row[5]) for row in columns] != expected_columns:
            raise ValueError("storage_unavailable")
        objects = connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if objects != [("table", "attempts", "attempts")]:
            raise ValueError("storage_unavailable")
        return connection
    except BaseException:
        connection.close()
        raise


def _existing_ledger_pending(directory, ledger_name):
    """Read an existing ledger without creating it; malformed means unknown."""
    try:
        suffix = _ledger_suffix(ledger_name)
        marker = directory / ("public-art-ledger" + suffix + ".marker")
        database = directory / ("public-art-ledger" + suffix + ".sqlite3")
        sidecars = [Path(str(database) + extra) for extra in ("-journal", "-wal", "-shm")]
        if any(_is_link(path) for path in (marker, database, *sidecars)):
            return "unknown"
        if not marker.exists() and not database.exists():
            return "clear"
        if (not marker.is_file() or marker.read_bytes() != _MARKER
                or not database.is_file() or not 0 < database.stat().st_size <= _LEDGER_BYTES
                or any(path.exists() for path in sidecars)):
            return "unknown"
        uri = database.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=0.1, isolation_level=None)
        try:
            if connection.execute("PRAGMA user_version").fetchone() != (1,):
                return "unknown"
            rows = connection.execute("SELECT status FROM attempts").fetchall()
            if any(row[0] not in _STATUSES for row in rows):
                return "unknown"
            return "pending" if any(row[0] == "pending" for row in rows) else "clear"
        finally:
            connection.close()
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return "unknown"


def _reserve(directory, slot_key, day, timestamp, *, ledger_name="public-art", daily_limit=None):
    """Atomically reserve a budget unit before the one permitted POST."""
    slot_id = hashlib.sha256(slot_key.encode("ascii")).hexdigest()
    if daily_limit is None:
        daily_limit = _EXTRA_IMAGE_DAILY_LIMIT if ledger_name == "public-art-extra-ai-friends" else (
            _SCHEDULED_IMAGE_DAILY_LIMIT if ledger_name == "public-art-v2" else _LEGACY_IMAGE_DAILY_LIMIT)
    if type(daily_limit) is not int or daily_limit <= 0:
        raise ValueError("storage_unavailable")
    # Any pending or malformed ledger is a global uncertainty.  Quotas remain
    # independent, but no second remote request may start while an outcome is
    # unresolved in another series.
    for other_ledger in ("public-art", "public-art-v2", "public-art-extra-ai-friends"):
        if other_ledger == ledger_name:
            continue
        if _existing_ledger_pending(directory, other_ledger) != "clear":
            return "unknown", slot_id, None
    connection = _connect_ledger(directory, ledger_name)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT slot_id, day, started_at, status, sha256, width, height, mismatch FROM attempts"
        )
        existing = None
        pending = False
        daily_count = 0
        for row in rows:
            key, row_day, started, status, digest, width, height, mismatch = row
            if (not isinstance(key, str) or not _SHA_RE.fullmatch(key)
                    or not isinstance(row_day, str) or not isinstance(status, str) or status not in _STATUSES
                    or type(started) is not int or started <= 0 or started > timestamp + 2
                    or type(width) is not int or type(height) is not int
                    or type(mismatch) is not int or mismatch not in (0, 1)):
                raise ValueError("storage_unavailable")
            try:
                valid_day = date.fromisoformat(row_day).isoformat() == row_day
                valid_day = valid_day and datetime.fromtimestamp(started, _TZ).date().isoformat() == row_day
            except (ValueError, OverflowError, OSError):
                valid_day = False
            if not valid_day:
                raise ValueError("storage_unavailable")
            if status == "complete":
                if (not isinstance(digest, str) or not _SHA_RE.fullmatch(digest)
                        or not 256 <= width <= 4096 or not 256 <= height <= 4096
                        or width * height > 16_000_000):
                    raise ValueError("storage_unavailable")
            elif digest != "" or width != 0 or height != 0 or mismatch != 0:
                raise ValueError("storage_unavailable")
            if status == "pending":
                pending = True
            if row_day == day:
                daily_count += 1
            if key == slot_id:
                existing = row
        if pending:
            connection.execute("ROLLBACK")
            return "unknown", slot_id, None
        if existing:
            connection.execute("ROLLBACK")
            return "existing", slot_id, existing
        if daily_count >= daily_limit:
            connection.execute("ROLLBACK")
            return "daily_limit", slot_id, None
        connection.execute(
            "INSERT INTO attempts VALUES (?, ?, ?, 'pending', '', 0, 0, 0)",
            (slot_id, day, timestamp),
        )
        connection.execute("COMMIT")
        return "reserved", slot_id, None
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _finish(directory, slot_id, status, *, digest="", width=0, height=0, mismatch=0,
            ledger_name="public-art"):
    # Never commit a malformed terminal row: a corrupt row fails every later
    # reservation and obscures which page had a confirmed result.  Pending
    # means the submission outcome may be unknown, so it is not a valid
    # requested transition; only a known preflight/unusable or completed page
    # can leave it.
    if (not isinstance(slot_id, str) or not _SHA_RE.fullmatch(slot_id)
            or not isinstance(status, str) or status not in {"complete", "preflight", "unusable"}
            or type(width) is not int or type(height) is not int
            or type(mismatch) is not int or mismatch not in (0, 1)):
        raise ValueError("storage_unavailable")
    if status == "complete":
        if (not isinstance(digest, str) or not _SHA_RE.fullmatch(digest)
                or not 256 <= width <= 4096 or not 256 <= height <= 4096
                or width * height > 16_000_000):
            raise ValueError("storage_unavailable")
    elif digest != "" or width != 0 or height != 0 or mismatch != 0:
        raise ValueError("storage_unavailable")
    connection = _connect_ledger(directory, ledger_name)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE attempts SET status=?, sha256=?, width=?, height=?, mismatch=? "
            "WHERE slot_id=? AND status='pending'",
            (status, digest, width, height, mismatch, slot_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("storage_unavailable")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _existing_result(directory, slot_id, row):
    if row[3] != "complete":
        return _failure("slot_already_used")
    image = directory / (slot_id + ".png")
    if (_is_link(image) or not image.is_file() or image.resolve(strict=True).parent != directory.resolve(strict=True)
            or not 0 < image.stat().st_size <= _IMAGE_BYTES):
        return _failure("image_asset_unavailable")
    if hashlib.sha256(image.read_bytes()).hexdigest() != row[4]:
        return _failure("image_asset_unavailable")
    return {"ok": True, "path": str(image), "sha256": row[4], "width": row[5], "height": row[6],
            "aspect_mismatch": bool(row[7])}


def _ledger_read_only_status_base(host):
    """Inspect only ledger schema/status; never create or repair any file."""
    unavailable = {"ledger": "unavailable", "pending_unknown_count": None}
    try:
        root = Path(host.root)
        autopost = Path(host._heybox_autopost_root)
        if (_is_link(root) or _is_link(autopost) or not root.is_dir() or not autopost.is_dir()
                or autopost.resolve(strict=True).parent != root.resolve(strict=True)):
            return unavailable
        directory = autopost / "generated_art"
        if _is_link(directory):
            return unavailable
        if not directory.exists():
            return {"ledger": "not_initialized", "pending_unknown_count": None}
        if not directory.is_dir() or directory.resolve(strict=True).parent != autopost.resolve(strict=True):
            return unavailable
        marker = directory / "public-art-ledger.marker"
        database = directory / "public-art-ledger.sqlite3"
        sidecars = [Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm")]
        if any(_is_link(path) for path in [marker, database, *sidecars]) or any(path.exists() for path in sidecars):
            return unavailable
        if not marker.exists() and not database.exists():
            return {"ledger": "not_initialized", "pending_unknown_count": None}
        if (not marker.is_file() or marker.stat().st_size != len(_MARKER) or marker.read_bytes() != _MARKER
                or not database.is_file() or not 0 < database.stat().st_size <= _LEDGER_BYTES):
            return unavailable
        # An immutable SQLite connection cannot create a journal or shared
        # memory file, including if another process changes journal mode while
        # this status is being read.  It also does not take locks, so reject
        # WAL databases and report a changed file or any sidecar as unknown.
        before = database.stat()
        with database.open("rb") as handle:
            header = handle.read(20)
        if len(header) != 20 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            return unavailable
        uri = database.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=0.1, isolation_level=None)
        try:
            connection.execute("PRAGMA query_only=ON")
            if (connection.execute("PRAGMA user_version").fetchone() != (1,)
                    or connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",)):
                return unavailable
            columns = connection.execute("PRAGMA table_info(attempts)").fetchall()
            expected = [
                ("slot_id", "TEXT", 0, 1), ("day", "TEXT", 1, 0),
                ("started_at", "INTEGER", 1, 0), ("status", "TEXT", 1, 0),
                ("sha256", "TEXT", 1, 0), ("width", "INTEGER", 1, 0),
                ("height", "INTEGER", 1, 0), ("mismatch", "INTEGER", 1, 0),
            ]
            if [(row[1], row[2], row[3], row[5]) for row in columns] != expected:
                return unavailable
            objects = connection.execute(
                "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if objects != [("table", "attempts", "attempts")]:
                return unavailable
            pending = 0
            timestamp = int(datetime.now(_TZ).timestamp())
            rows = connection.execute(
                "SELECT slot_id, day, started_at, status, sha256, width, height, mismatch FROM attempts"
            )
            for key, row_day, started, state, digest, width, height, mismatch in rows:
                if (not isinstance(key, str) or not _SHA_RE.fullmatch(key)
                        or not isinstance(row_day, str) or not isinstance(state, str) or state not in _STATUSES
                        or type(started) is not int or started <= 0 or started > timestamp + 2
                        or type(width) is not int or type(height) is not int
                        or type(mismatch) is not int or mismatch not in (0, 1)):
                    return unavailable
                try:
                    valid_day = date.fromisoformat(row_day).isoformat() == row_day
                    valid_day = valid_day and datetime.fromtimestamp(started, _TZ).date().isoformat() == row_day
                except (ValueError, OverflowError, OSError):
                    valid_day = False
                if not valid_day:
                    return unavailable
                if state == "complete":
                    if (not isinstance(digest, str) or not _SHA_RE.fullmatch(digest)
                            or not 256 <= width <= 4096 or not 256 <= height <= 4096
                            or width * height > 16_000_000):
                        return unavailable
                elif digest != "" or width != 0 or height != 0 or mismatch != 0:
                    return unavailable
                pending += state == "pending"
            after = database.stat()
            if (any(_is_link(path) for path in [marker, database, *sidecars])
                    or any(path.exists() for path in sidecars)
                    or marker.read_bytes() != _MARKER
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                return unavailable
            return {"ledger": "pending" if pending else "clear", "pending_unknown_count": pending}
        finally:
            connection.close()
    except (AttributeError, OSError, ValueError, TypeError, sqlite3.Error):
        return unavailable


def _ledger_read_only_status(host):
    """Expose scheduled and independent-extra ledger states read-only."""
    status = _ledger_read_only_status_base(host)
    if not isinstance(status, dict):
        return status
    extra = dict(status)
    extra["scheduled_ledger"] = "unavailable"
    extra["scheduled_pending_unknown_count"] = None
    extra["extra_ledger"] = "unavailable"
    extra["extra_pending_unknown_count"] = None
    try:
        root = Path(host.root)
        autopost = Path(host._heybox_autopost_root)
        if (_is_link(root) or _is_link(autopost) or not root.is_dir() or not autopost.is_dir()
                or autopost.resolve(strict=True).parent != root.resolve(strict=True)):
            return extra
        directory = autopost / "generated_art"
        if _is_link(directory) or not directory.exists():
            extra["scheduled_ledger"] = "not_initialized"
            extra["extra_ledger"] = "not_initialized"
            return extra
        scheduled_state = _existing_ledger_pending(directory, "public-art-v2")
        if scheduled_state == "clear":
            extra["scheduled_ledger"] = "clear"
            extra["scheduled_pending_unknown_count"] = 0
        elif scheduled_state == "pending":
            extra["scheduled_ledger"] = "pending"
            extra["scheduled_pending_unknown_count"] = 1
        marker = directory / "public-art-ledger-extra-ai-friends.marker"
        database = directory / "public-art-ledger-extra-ai-friends.sqlite3"
        sidecars = [Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm")]
        if any(_is_link(path) for path in (marker, database, *sidecars)) or any(path.exists() for path in sidecars):
            return extra
        if not marker.exists() and not database.exists():
            extra["extra_ledger"] = "not_initialized"
            return extra
        if (not marker.is_file() or marker.read_bytes() != _MARKER
                or not database.is_file() or not 0 < database.stat().st_size <= _LEDGER_BYTES):
            return extra
        uri = database.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=0.1, isolation_level=None)
        try:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA user_version").fetchone() != (1,):
                return extra
            rows = connection.execute("SELECT status FROM attempts").fetchall()
            if any(row[0] not in _STATUSES for row in rows):
                return extra
            extra["extra_pending_unknown_count"] = sum(row[0] == "pending" for row in rows)
            extra["extra_ledger"] = "pending" if extra["extra_pending_unknown_count"] else "clear"
        finally:
            connection.close()
    except (AttributeError, OSError, ValueError, TypeError, sqlite3.Error):
        return extra
    return extra


def _image_endpoint(host):
    """Use the active image plugin's configured Cockpit endpoint only."""
    resolver = getattr(host, "plugin", None)
    service = resolver("astrbot_plugin_image_companion") if callable(resolver) else None
    if service is None or getattr(service, "enabled", None) is not True:
        raise ValueError("image_service_unavailable")
    setting = getattr(service, "image_setting", None)
    if not callable(setting) or setting("photo_generation_backend", None) != "external":
        raise ValueError("image_service_unavailable")
    endpoints = setting("external_image_api_endpoints", None)
    if not isinstance(endpoints, list) or not endpoints or len(endpoints) > 12:
        raise ValueError("image_service_unavailable")
    if any(not isinstance(row, dict) or type(row.get("enabled")) is not bool for row in endpoints):
        raise ValueError("image_service_unavailable")
    enabled = [row for row in endpoints if row["enabled"]]
    if len(enabled) != 1:
        raise ValueError("image_service_unavailable")
    endpoint = enabled[0]
    plugin_module = type(service).__module__
    if not isinstance(plugin_module, str) or not plugin_module.endswith(".main"):
        raise ValueError("image_service_unavailable")
    package_name = plugin_module.rsplit(".", 1)[0]
    adapter = importlib.import_module(package_name + ".cockpit_image25")
    runtime = importlib.import_module(package_name + ".image_runtime")
    # The active owner path imported this exact alias.  A missing capability
    # or a stale module after reload cannot prove shared serialization.
    if (getattr(adapter, "SHARED_SUBMISSION_GATE_VERSION", None) != 1
            or not callable(getattr(adapter, "submit_public_once", None))
            or getattr(runtime, "submit_cockpit_image25_once", None) is not getattr(adapter, "submit_once", None)):
        raise ValueError("image_service_unavailable")
    if not adapter.is_cockpit_image25_endpoint(endpoint):
        raise ValueError("image_service_unavailable")
    size = endpoint.get("size")
    timeout = endpoint.get("timeout_seconds")
    key = endpoint.get("api_key")
    if (size not in {"1024x1024", "1024x1536", "1536x1024"}
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 1 <= timeout <= 600
            or not isinstance(key, str) or not key.strip() or len(key.strip()) > 1024
            or any(ord(char) < 33 or ord(char) > 126 for char in key.strip())
            or endpoint.get("custom_headers") not in (None, "", {})):
        raise ValueError("image_service_unavailable")
    return adapter, dict(endpoint), min(float(timeout), _POST_TIMEOUT)


def _reference_root_for_host(host):
    """Resolve the local archive root without accepting arbitrary caller paths."""
    configured = getattr(host, "_public_art_reference_root", None)
    if isinstance(configured, (str, os.PathLike)):
        return Path(configured)
    return default_archive_root(host)


def _reference_local_root_for_host(host):
    configured = getattr(host, "_public_art_reference_local_root", None)
    if isinstance(configured, (str, os.PathLike)):
        return Path(configured)
    return default_local_reference_root(host)


def _comic_request(
    host, *, profile, day, page_number, scene, character_whitelist=None,
    local_reference_root=None,
):
    """Build a bounded Image2.5 request and its *actual* verified references."""
    if profile not in _COMIC_PROFILE_PROMPTS:
        return None, "invalid_profile"
    if type(page_number) is not int or not 1 <= page_number <= _COMIC_PAGE_LIMIT:
        return None, "invalid_slot"
    try:
        # The selection is made once for the date, therefore all four pages in
        # a series use the same roster and retain visual continuity.
        selected = select_reference_ids(profile, day, character_whitelist=character_whitelist)
        refs = resolve_reference_set(
            profile, day, root=_reference_root_for_host(host),
            character_whitelist=selected,
            local_root=(Path(local_reference_root) if local_reference_root is not None
                        else _reference_local_root_for_host(host)),
        )
    except ReferenceManifestError as exc:
        return None, str(exc)
    if not 3 <= len(refs) <= 4:
        return None, "reference_manifest_invalid"
    reference_ids = tuple(item["id"] for item in refs)
    prompt = (_COMIC_PROMPT + _COMIC_PROFILE_PROMPTS[profile]
              + _reference_role_line(reference_ids)
              + f"Use all {len(refs)} supplied image references as visual identity anchors; "
              + "do not replace them with text-only descriptions. "
              + "Imagined scene: " + scene)
    return {
        "profile": profile,
        "page_number": page_number,
        "reference_manifest": _COMIC_REFERENCE_MANIFEST_ID,
        "reference_ids": reference_ids,
        "reference_paths": tuple(str(item["path"]) for item in refs),
        "reference_hashes": tuple(item["sha256"] for item in refs),
        "prompt": prompt,
    }, None


def build_comic_generation_request(
    *, profile, day, page_number, scene, reference_root=None,
    local_reference_root=None, character_whitelist=None,
):
    """Offline/test-facing request builder; never submits or contacts a model."""
    if not isinstance(scene, str) or not _valid_scene(scene):
        return _failure("invalid_scene")
    if profile not in _COMIC_PROFILE_PROMPTS:
        return _failure("invalid_profile")
    if type(page_number) is not int or not 1 <= page_number <= _COMIC_PAGE_LIMIT:
        return _failure("invalid_slot")
    try:
        selected = select_reference_ids(profile, day, character_whitelist=character_whitelist)
        refs = resolve_reference_set(
            profile, day, root=reference_root, character_whitelist=selected,
            local_root=(Path(local_reference_root) if local_reference_root is not None
                        else Path(__file__).resolve().parent / "reference-assets"),
        )
    except ReferenceManifestError as exc:
        return _failure(str(exc))
    return {
        "ok": True,
        "profile": profile,
        "page_number": page_number,
        "reference_manifest": _COMIC_REFERENCE_MANIFEST_ID,
        "reference_ids": tuple(item["id"] for item in refs),
        "reference_paths": tuple(str(item["path"]) for item in refs),
        "reference_hashes": tuple(item["sha256"] for item in refs),
        "prompt": (_COMIC_PROMPT + _COMIC_PROFILE_PROMPTS[profile]
                   + _reference_role_line(tuple(item["id"] for item in refs))
                   + f"Use all {len(refs)} supplied image references as visual identity anchors; "
                   + "do not replace them with text-only descriptions. Imagined scene: " + scene),
    }


class PublicArtGenerationMixin:
    def public_art_status(self):
        """Read a whitelist of local state without reading secrets or creating files.

        ``endpoint_profile`` covers the enabled model/address/size/timeout
        shape only. Credentials, headers, other processes and the service's
        remote execution are always unknown here.
        """
        status = {
            "endpoint_profile": "unavailable", "shared_gate": "unavailable",
            "gate_lock_state": "unknown", "owner_waiting": "unknown",
            "other_image_tasks": "unknown", "external_activity": "unknown",
            "credentials": "unknown", "dispatch_readiness": "unknown",
            "comic_reference_capability": "unavailable",
            "comic_reference_manifest": "unavailable",
            "comic_reference_roots": "unavailable",
        }
        status.update(_ledger_read_only_status(self))
        try:
            resolver = getattr(self, "plugin", None)
            service = resolver("astrbot_plugin_image_companion") if callable(resolver) else None
            setting = getattr(service, "image_setting", None)
            if (service is None or getattr(service, "enabled", None) is not True
                    or not callable(setting) or setting("photo_generation_backend", None) != "external"):
                return status
            endpoints = setting("external_image_api_endpoints", None)
            if (not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 12
                    or any(not isinstance(row, dict) or type(row.get("enabled")) is not bool for row in endpoints)):
                return status
            enabled = [row for row in endpoints if row["enabled"]]
            if len(enabled) != 1:
                return status
            endpoint = enabled[0]
            plugin_module = type(service).__module__
            if not isinstance(plugin_module, str) or not plugin_module.endswith(".main"):
                return status
            package = plugin_module.rsplit(".", 1)[0]
            adapter = sys.modules.get(package + ".cockpit_image25")
            runtime = sys.modules.get(package + ".image_runtime")
            try:
                submit_comic = getattr(adapter, "submit_public_comic_once", None)
                signature = inspect.signature(submit_comic) if callable(submit_comic) else None
                parameters = signature.parameters if signature is not None else {}
                if (getattr(adapter, "EXPLICIT_PUBLIC_REFERENCE_CAPABILITY_VERSION", None) == 1
                        and {"reference_paths", "reference_hashes"}.issubset(parameters)):
                    status["comic_reference_capability"] = "ready"
            except (TypeError, ValueError):
                pass
            try:
                manifest = load_manifest()
                if manifest.get("manifest_id") == _COMIC_REFERENCE_MANIFEST_ID:
                    status["comic_reference_manifest"] = _COMIC_REFERENCE_MANIFEST_ID
                    today = datetime.now(_TZ).date().isoformat()
                    archive = _reference_root_for_host(self)
                    local = _reference_local_root_for_host(self)
                    for profile in ("ai_friends", "dragon_helpers"):
                        refs = resolve_reference_set(profile, today, root=archive, local_root=local)
                        if not 3 <= len(refs) <= 4:
                            raise ReferenceManifestError("reference_manifest_invalid")
                    status["comic_reference_roots"] = "verified"
            except (ReferenceManifestError, OSError, ValueError, TypeError):
                pass
            checker = getattr(adapter, "is_cockpit_image25_endpoint", None)
            size, timeout = endpoint.get("size"), endpoint.get("timeout_seconds")
            if (not callable(checker) or not checker(endpoint)
                    or size not in {"1024x1024", "1024x1536", "1536x1024"}
                    or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or not 1 <= timeout <= 600):
                return status
            status["endpoint_profile"] = "configured"
            gate_status = getattr(adapter, "public_submission_status", None)
            if (type(getattr(adapter, "SHARED_SUBMISSION_GATE_VERSION", None)) is not int
                    or adapter.SHARED_SUBMISSION_GATE_VERSION != 1
                    or not callable(getattr(adapter, "submit_public_once", None))
                    or not callable(gate_status)
                    or getattr(runtime, "submit_cockpit_image25_once", None) is not getattr(adapter, "submit_once", None)):
                return status
            status["shared_gate"] = "available"
            observed = gate_status()
            if not isinstance(observed, dict):
                return status
            for key, allowed in (
                    ("gate_lock_state", {"unknown", "uninitialized", "locked", "unlocked"}),
                    ("owner_waiting", {"unknown", "yes", "no"}),
                    ("other_image_tasks", {"unknown", "observed", "not_observed"})):
                value = observed.get(key)
                if isinstance(value, str) and value in allowed:
                    status[key] = value
        except Exception:
            pass
        return status

    async def generate_public_art(
        self, *, slot_key, caption_hint, authorization, page_number=1,
        profile=None, character_whitelist=None, local_reference_root=None,
    ):
        """Generate one image for an authorized slot and page reservation.

        ``caption_hint`` is a narrowly validated English noun-phrase scene
        taken from the current public-safe creative result. The result contains
        fixed reasons, or a local path, SHA-256 and bounded image dimensions.
        No prompt, credential, response body, chat content or raw error is stored.
        """
        day = _slot_day(slot_key)
        if not day:
            return _failure("invalid_slot")
        comic = _is_comic_slot(slot_key)
        if (type(page_number) is not int or not 1 <= page_number <= (_COMIC_PAGE_LIMIT if comic else 1)):
            return _failure("invalid_slot")
        if not _valid_scene(caption_hint):
            return _failure("invalid_scene")
        expected_profile = _slot_profile(slot_key, profile)
        if comic and expected_profile is None:
            return _failure("invalid_profile")
        if comic and not _is_new_comic_slot(slot_key) and character_whitelist is not None:
            return _failure("invalid_profile")
        if not comic and (profile is not None or character_whitelist is not None):
            return _failure("invalid_profile")
        if _is_new_comic_slot(slot_key) and not _new_comic_authorization(authorization):
            return _failure("not_authorized")
        if not _same_authorization(self, authorization):
            return _failure("not_authorized")
        if slot_key.endswith(_EXTRA_TASK_SUFFIX) and not _extra_capability_ok(self, slot_key, authorization):
            return _failure("not_authorized")
        if datetime.now(_TZ).date().isoformat() != day:
            return _failure("slot_expired")
        boundary = getattr(self, "run_authorized_public_art", None)
        if not callable(boundary):
            return _failure("resource_guard_unavailable")

        invoked = False
        operation_result = None
        binding = object()

        async def operation():
            nonlocal invoked, operation_result
            if invoked:
                return _failure("resource_guard_denied")
            invoked = True
            ticket = getattr(self, "_public_art_resource_ticket", None)
            if (not _ticket_available(self, slot_key, authorization, ticket)
                    or ticket.get("page_number") != page_number):
                operation_result = _failure("resource_guard_denied")
                return operation_result
            task = asyncio.current_task()
            if task is None:
                operation_result = _failure("resource_guard_denied")
                return operation_result
            token = _PUBLIC_ART_OPERATION.set((binding, self, ticket, task, slot_key, caption_hint, page_number))
            try:
                if not _active_resource_grant(self, slot_key, authorization, ticket, caption_hint, binding):
                    operation_result = _failure("resource_guard_denied")
                    return operation_result
                operation_result = await self._generate_public_art_once(
                    slot_key=slot_key, day=day, scene=caption_hint, authorization=authorization,
                    ticket=ticket, binding=binding, page_number=page_number,
                    profile=expected_profile, character_whitelist=character_whitelist,
                    local_reference_root=local_reference_root,
                )
                return operation_result
            finally:
                _PUBLIC_ART_OPERATION.reset(token)

        try:
            result = await boundary(slot_key, authorization, operation)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _failure("resource_guard_unavailable")
        if (isinstance(result, dict) and result.get("ok") is False):
            reason = result.get("reason")
            # The operation's own fixed outcomes are also legitimate.
            if operation_result is not None and result == operation_result:
                return operation_result
            if reason == "public_art_timeout_unknown":
                return _failure("image_result_unknown")
            return _failure(reason if reason in _GUARD_REASONS else "resource_guard_denied")
        if not invoked or operation_result is None or result != operation_result:
            return _failure("resource_guard_denied")
        return operation_result

    async def _generate_public_art_once(
        self, *, slot_key, day, scene, authorization, ticket, binding=None, page_number=1,
        profile=None, character_whitelist=None, local_reference_root=None,
    ):
        if not _active_resource_grant(self, slot_key, authorization, ticket, scene, binding):
            return _failure("resource_guard_denied")
        if _slot_day(slot_key) != day or not _valid_scene(scene):
            return _failure("invalid_scene")
        expected_profile = _slot_profile(slot_key, profile) if _is_comic_slot(slot_key) else None
        if _is_comic_slot(slot_key) and expected_profile is None:
            return _failure("invalid_profile")
        if not _same_authorization(self, authorization):
            return _failure("authorization_changed")
        if _is_new_comic_slot(slot_key) and not _new_comic_authorization(authorization):
            return _failure("authorization_changed")
        if slot_key.endswith(_EXTRA_TASK_SUFFIX) and not _extra_capability_ok(self, slot_key, authorization):
            return _failure("resource_guard_denied")
        now = datetime.now(_TZ)
        if now.date().isoformat() != day:
            return _failure("slot_expired")
        try:
            adapter, endpoint, timeout = _image_endpoint(self)
        except Exception:
            return _failure("image_service_unavailable")
        comic = _is_comic_slot(slot_key)
        request = None
        if comic and _is_new_comic_slot(slot_key):
            request, reference_error = _comic_request(
                self, profile=expected_profile, day=day, page_number=page_number,
                scene=scene, character_whitelist=character_whitelist,
                local_reference_root=local_reference_root,
            )
            if request is None:
                return _failure(reference_error or "reference_manifest_invalid")
        try:
            directory = _art_root(self)
            reservation_key = slot_key if page_number == 1 else slot_key + "#comic-page-" + str(page_number)
            ledger_name = (
                "public-art-extra-ai-friends" if slot_key.endswith(_EXTRA_TASK_SUFFIX)
                else "public-art-v2" if _is_new_comic_slot(slot_key)
                else "public-art"
            )
            limit = (_EXTRA_IMAGE_DAILY_LIMIT if ledger_name == "public-art-extra-ai-friends"
                     else _SCHEDULED_IMAGE_DAILY_LIMIT if ledger_name == "public-art-v2"
                     else _LEGACY_IMAGE_DAILY_LIMIT)
            status, slot_id, existing = _reserve(
                directory, reservation_key, day, int(now.timestamp()),
                ledger_name=ledger_name, daily_limit=limit,
            )
            if status == "unknown":
                return _failure("image_result_unknown")
            if status == "daily_limit":
                return _failure("image_daily_limit")
            if status == "existing":
                return _existing_result(directory, slot_id, existing)
        except (OSError, ValueError, sqlite3.Error):
            return _failure("storage_unavailable")

        # The committed pending record is deliberately left intact on every
        # cancellation, timeout, unconfirmed response or local save failure.
        # No retry or alternate endpoint is allowed after this point.
        try:
            if not _active_resource_grant(self, slot_key, authorization, ticket, scene, binding):
                _finish(directory, slot_id, "preflight", ledger_name=ledger_name)
                return _failure("resource_guard_denied")
            if not _same_authorization(self, authorization):
                _finish(directory, slot_id, "preflight", ledger_name=ledger_name)
                return _failure("authorization_changed")
            if datetime.now(_TZ).date().isoformat() != day:
                _finish(directory, slot_id, "preflight", ledger_name=ledger_name)
                return _failure("slot_expired")
            def recheck_before_submit():
                if (not _active_resource_grant(self, slot_key, authorization, ticket, scene, binding)
                        or not _same_authorization(self, authorization)
                        or datetime.now(_TZ).date().isoformat() != day):
                    return False
                try:
                    current_adapter, current_endpoint, current_timeout = _image_endpoint(self)
                    return current_adapter is adapter and current_endpoint == endpoint and current_timeout == timeout
                except Exception:
                    return False

            submit = getattr(adapter, "submit_public_comic_once", None) if comic else adapter.submit_public_once
            if not callable(submit):
                _finish(directory, slot_id, "preflight", ledger_name=ledger_name)
                return _failure("image_service_unavailable")
            requested_size = "1024x1536" if comic else endpoint["size"]
            submit_kwargs = {
                "base_url": endpoint["base_url"], "api_key": endpoint["api_key"],
                "prompt": (request["prompt"] if request else
                           (_COMIC_PROMPT if comic else _CHARACTER_PROMPT) + "Imagined scene: " + scene),
                "size": requested_size, "timeout": timeout,
                "custom_headers": endpoint.get("custom_headers"),
                "check_authorization": recheck_before_submit,
            }
            if comic:
                # New policy-v2 comics must use an adapter with an explicit
                # reference_paths/reference_hashes contract.  Calling the old
                # three-pinned-reference function would silently violate the
                # archived allow-list, so fail closed when it cannot receive
                # the selected files.
                try:
                    parameters = inspect.signature(submit).parameters
                except (TypeError, ValueError):
                    parameters = {}
                if (_is_new_comic_slot(slot_key)
                        and getattr(adapter, "EXPLICIT_PUBLIC_REFERENCE_CAPABILITY_VERSION", None) != 1):
                    _finish(directory, slot_id, "preflight", ledger_name=ledger_name)
                    return _failure("image_reference_capability_missing")
                if _is_new_comic_slot(slot_key) and not {
                        "reference_paths", "reference_hashes"}.issubset(parameters):
                    _finish(directory, slot_id, "preflight", ledger_name=ledger_name)
                    return _failure("image_reference_capability_missing")
                if {"reference_paths", "reference_hashes"}.issubset(parameters):
                    submit_kwargs["reference_paths"] = request["reference_paths"]
                    submit_kwargs["reference_hashes"] = request["reference_hashes"]
            gate_result, result = await submit(**submit_kwargs)
            if gate_result in {"preflight_busy", "preflight_denied"}:
                _finish(directory, slot_id, "preflight", ledger_name=ledger_name)
                return _failure("image_resource_busy" if gate_result == "preflight_busy" else "resource_guard_denied")
            if gate_result != "submitted" or result is None:
                return _failure("image_result_unknown")
            if result.state == "preflight":
                _finish(directory, slot_id, "preflight", ledger_name=ledger_name)
                return _failure("image_preflight_failed")
            if result.state != "completed":
                return _failure("image_result_unknown")
            inspected = await asyncio.to_thread(adapter.inspect_png_result, result.image_b64, requested_size)
            if (not isinstance(inspected, dict) or inspected.get("valid") is not True
                    or inspected.get("reason_code") not in {"ok", "native_aspect_mismatch"}):
                return _failure("image_result_unknown")
            raw = base64.b64decode(result.image_b64, validate=True)
            if len(raw) > _IMAGE_BYTES:
                _finish(directory, slot_id, "unusable", ledger_name=ledger_name)
                return _failure("image_too_large")
            width, height = inspected.get("width"), inspected.get("height")
            if (type(width) is not int or type(height) is not int or not 256 <= width <= 4096
                    or not 256 <= height <= 4096 or width * height > 16_000_000
                    or len(raw) != inspected.get("decoded_bytes") or not raw):
                return _failure("image_result_unknown")
            image = directory / (slot_id + ".png")
            if _is_link(image):
                return _failure("storage_unavailable")
            with image.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if _is_link(image) or image.resolve(strict=True).parent != directory.resolve(strict=True):
                return _failure("storage_unavailable")
            digest = hashlib.sha256(raw).hexdigest()
            mismatch = int(inspected.get("reason_code") == "native_aspect_mismatch")
            _finish(directory, slot_id, "complete", digest=digest, width=width, height=height,
                    mismatch=mismatch, ledger_name=ledger_name)
            if not _same_authorization(self, authorization):
                return _failure("authorization_changed")
            if datetime.now(_TZ).date().isoformat() != day:
                return _failure("slot_expired")
            result_payload = {
                "ok": True, "path": str(image), "sha256": digest, "width": width,
                "height": height, "aspect_mismatch": bool(mismatch),
            }
            if request is not None:
                result_payload.update({
                    "profile": request["profile"],
                    "page_number": request["page_number"],
                    "reference_manifest": request["reference_manifest"],
                    "reference_ids": list(request["reference_ids"]),
                    "reference_hashes": list(request["reference_hashes"]),
                })
            return result_payload
        except asyncio.CancelledError:
            raise
        except Exception:
            return _failure("image_result_unknown")
