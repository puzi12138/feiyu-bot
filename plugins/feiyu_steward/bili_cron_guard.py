"""Bind verified cron journal stages to one live occurrence and resume generation.

This module grants no new identity and starts no work. Receipts contain only
identifiers, not source text or private notes; a reload discards them closed.
"""

import json
import hashlib
import math
import os
import re
import time
from dataclasses import dataclass
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_BV = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_LIMIT = 48
_CRON_SHARE_SCOPE = ContextVar("feiyu_bili_cron_share_scope", default=None)
_JS_SPACE = " \t\n\r\v\f\u00a0\ufeff\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
_NOTE_FIELDS = ("encounter", "facts", "author_view", "inference", "previous_judgment", "revision", "missing", "next_response")


@dataclass(frozen=True)
class CronJournalScope:
    occurrence: tuple
    generation: tuple
    started: float


class CronGuardStorageError(OSError):
    """A fixed classification with no storage path or body attached."""


class CronGuardIntentError(ValueError):
    """An invalid note is a bounded tool/model error, never a disk diagnosis."""


def _intent_hash(arguments):
    intent = arguments.get("json")
    if isinstance(intent, str):
        intent = json.loads(intent)
    if not isinstance(intent, dict):
        raise ValueError
    # Upstream interest mode deduplicates the same reading by exact at/claim
    # evidence. Rewording private reflection fields must not reopen an unknown
    # attempt, while genuinely different evidence remains a separate intent.
    evidence = intent.get("evidence") if isinstance(intent, dict) else None
    if (isinstance(evidence, list) and evidence
            and all(isinstance(item, dict) and type(item.get("at")) in (int, float)
                    and math.isfinite(item["at"]) and type(item.get("claim")) is str for item in evidence)):
        points = {(float(item["at"]) or 0.0, item["claim"].strip(_JS_SPACE)) for item in evidence}
        intent = {"evidence": sorted(points)}
    else:
        intent = {key: (intent.get(key) or "").strip(_JS_SPACE) for key in _NOTE_FIELDS}
        intent["evidence"] = evidence or []
    canonical = json.dumps([arguments.get("readingId"), intent],
                           ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def cron_journal_scope(steward, event):
    """Reverify the real running occurrence, window and durable incident state."""
    try:
        from .autonomy_budget import _verified_job
        from .bili_journal_review import _resume_snapshot
        from .peak_runtime import automatic_peak_paused
        from .policy import TZ

        if (getattr(steward, "_group_provider_terminated", True) is not False
                or getattr(steward, "_bili_cron_failed", False) is not False):
            return None
        hooks = steward._incident_hooks
        if not callable(getattr(hooks, "_report", None)):
            return None
        job = await _verified_job(steward.context, event, SimpleNamespace(contexts=[]))
        if job is None or job is False:
            return None
        now = time.time()
        if not 8 <= datetime.fromtimestamp(now, TZ).hour < 22 or automatic_peak_paused(now) is not False:
            return None
        started = getattr(job, "last_run_at", None)
        if not isinstance(started, datetime):
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        stamp = started.timestamp()
        generation = _resume_snapshot(steward)
        if (generation is None or not math.isfinite(stamp) or not 0 < stamp <= now
                or (generation[0] and stamp <= generation[0])):
            return None
        return CronJournalScope((str(job.job_id), stamp), generation, stamp)
    except Exception:  # noqa: BLE001 - Missing identity, receipt or policy closes automatic work.
        return None


async def cron_scope_allowed(steward, event, scope):
    return isinstance(scope, CronJournalScope) and await cron_journal_scope(steward, event) == scope


async def cron_dispatch_allowed(scope):
    """A queue callback captures this explicit scope before the caller resets it."""
    if scope is None:
        return True
    try:
        steward, event, generation = scope
        return await cron_scope_allowed(steward, event, generation)
    except Exception:  # noqa: BLE001 - A retired or malformed occurrence cannot dispatch.
        return False


def _receipts(steward):
    receipts = getattr(steward, "_bili_cron_readings", None)
    if not isinstance(receipts, dict):
        receipts = steward._bili_cron_readings = {}
    return receipts


def reading_receipt(steward, scope, reading_id):
    if not isinstance(scope, CronJournalScope) or not isinstance(reading_id, str) or not _ID.fullmatch(reading_id):
        return None
    row = _receipts(steward).get((scope.occurrence, reading_id))
    return row if isinstance(row, dict) and row.get("generation") == scope.generation else None


def bind_reading(steward, scope, result):
    """Accept only an actual successful bridge read from this unchanged scope."""
    row = result.get("result") if isinstance(result, dict) else None
    if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
            or not _ID.fullmatch(row["id"]) or not isinstance(row.get("bvid"), str)
            or not _BV.fullmatch(row["bvid"]) or row.get("route") not in {"A1", "A2"}
            or not isinstance(row.get("coverage"), dict)
            or row["coverage"].get("state") == "read_failed"):
        return
    receipts = _receipts(steward)
    key = (scope.occurrence, row["id"])
    # A repeated read cannot clear an attempted/unknown journal claim.
    if key not in receipts:
        while len(receipts) >= _LIMIT:
            receipts.pop(next(iter(receipts)))
        receipts[key] = {"generation": scope.generation, "bvid": row["bvid"],
                         "attempted": False, "note_id": None, "intents": {}}


def _claims_root():
    return Path(__file__).resolve().parents[4] / "data/bili-cron-guard/journal-claims"


def claim_journal(steward, scope, arguments):
    reading_id = arguments.get("readingId")
    row = reading_receipt(steward, scope, reading_id)
    if row is None:
        return False
    bvid = row.get("bvid")
    if not isinstance(bvid, str) or not _BV.fullmatch(bvid):
        return False
    try:
        intent_hash = _intent_hash(arguments)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise CronGuardIntentError("cron_guard_intent_invalid") from None
    # Claim before starting the child. Unknown writes are never retried here.
    # Exclusive create is cross-process and persists across reloads/restarts.
    # A partial/unknown claim stays closed; no auto migration, expiry or reset.
    try:
        root = _claims_root()
        root.mkdir(parents=True, exist_ok=True)
        claim = {"bvid": bvid, "reading_id": reading_id, "intent_sha256": intent_hash,
                 "occurrence": scope.occurrence, "generation": scope.generation,
                 "status": "attempt_reserved_no_replay"}
        with (root / (intent_hash + ".json")).open("x", encoding="utf-8") as target:
            json.dump(claim, target, ensure_ascii=True, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError:
        try:
            existing = (root / (intent_hash + ".json")).read_text(encoding="utf-8")
            saved = json.loads(existing) if len(existing) <= 16000 else None
            if (isinstance(saved, dict) and saved.get("intent_sha256") == intent_hash
                    and saved.get("reading_id") == reading_id and saved.get("bvid") == bvid
                    and saved.get("status") == "committed"
                    and type(saved.get("note_id")) is str and _ID.fullmatch(saved["note_id"])):
                return {"status": "local_reviewable_draft", "id": saved["note_id"],
                        "readingId": reading_id, "deduplicated": True}
        except (OSError, ValueError, TypeError):
            pass
        return False
    except (OSError, ValueError, TypeError, RecursionError):
        raise CronGuardStorageError("cron_guard_storage_failed") from None
    row["attempted"] = True
    row["intents"][intent_hash] = "attempt_reserved_no_replay"
    return True


def finish_journal_claim(scope, arguments, result):
    """Save only bounded operation metadata; never the note or raw error body."""
    try:
        intent_hash = _intent_hash(arguments)
        path = _claims_root() / (intent_hash + ".json")
        with path.open("r", encoding="utf-8") as source:
            claim = json.load(source)
        if claim.get("intent_sha256") != intent_hash:
            raise ValueError
        success = (isinstance(result, dict) and result.get("status") == "local_reviewable_draft"
                   and type(result.get("deduplicated")) is bool
                   and result.get("readingId") == arguments.get("readingId")
                   and type(result.get("id")) is str and _ID.fullmatch(result["id"]))
        claim["status"] = "committed" if success else "result_unconfirmed_no_replay"
        if success:
            claim["note_id"] = result["id"]
            claim["deduplicated"] = result["deduplicated"]
        temporary = path.with_suffix(".tmp")
        with temporary.open("x", encoding="utf-8") as target:
            json.dump(claim, target, ensure_ascii=True, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        raise CronGuardStorageError("cron_guard_storage_failed") from None


def bind_journal(steward, scope, reading_id, receipt):
    row = reading_receipt(steward, scope, reading_id)
    if (row is not None and row.get("attempted") is True and isinstance(receipt, dict)
            and receipt.get("status") == "local_reviewable_draft"
            and receipt.get("deduplicated") is False and receipt.get("readingId") == reading_id
            and isinstance(receipt.get("id"), str) and _ID.fullmatch(receipt["id"])):
        row["note_id"] = receipt["id"]
        row["share_claimed"] = False


async def committed_cron_scope(steward, event, receipt, reading_id):
    scope = await cron_journal_scope(steward, event)
    row = reading_receipt(steward, scope, reading_id)
    return scope if row is not None and row.get("note_id") == receipt.get("id") else None


async def report_cron_failure(steward, action, result=None, *, error=None):
    """Report fixed categories through F060; never copy raw responses or notes."""
    category = None
    if error is not None:
        category = ("storage_failed" if isinstance(error, CronGuardStorageError) else
                    "model_failed" if isinstance(error, CronGuardIntentError) else
                    "delivery_unknown" if action == "journal" else "unexpected_exception")
    elif isinstance(result, dict) and isinstance(result.get("error"), str):
        code = result["error"]
        if code in {"CANCELLED", "TOKEN_BUDGET", "JOURNAL_DAILY_LIMIT", "CANDIDATES_BUDGET"}:
            return
        if code in {"NETWORK_FAILED", "HTTP_FAILED", "BODY_READ_FAILED", "API_UNAVAILABLE", "WALL_BUDGET"}:
            category = "request_failed"
        elif code in {"CREDENTIAL_INVALID", "CREDENTIAL_EXPIRED", "CREDENTIAL_DISABLED"}:
            category = "credential_invalid"
        elif code in {"PLATFORM_PAUSED", "PERSISTENT_PAUSE"}:
            category = "access_denied"
        elif code in {"INVALID_RESPONSE", "RESPONSE_TOO_LARGE", "OUTPUT_TOO_LARGE"}:
            category = "response_invalid" if action != "journal" else "delivery_unknown"
        elif code in {"HUMANISM_TIMEOUT", "HUMANISM_BRIDGE_FAILED", "INTERNAL_ERROR"}:
            category = "delivery_unknown" if action == "journal" else "unexpected_exception"
        elif action == "journal":
            category = "model_failed"
        elif code.startswith("INVALID_") or code in {"UNKNOWN_PARAMETER", "URL_DENIED"}:
            return
        else:
            category = "unexpected_exception"
    elif action in {"read", "source"}:
        row = result.get("result") if action == "read" and isinstance(result, dict) else result
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not _ID.fullmatch(row["id"]) or not isinstance(row.get("bvid"), str)
                or not _BV.fullmatch(row["bvid"]) or not isinstance(row.get("segments"), list)
                or not isinstance(row.get("coverage"), dict)):
            category = "response_invalid"
        elif row["coverage"].get("state") == "read_failed":
            category = "request_failed"
    elif action == "journal":
        if (not isinstance(result, dict) or result.get("status") != "local_reviewable_draft"
                or type(result.get("deduplicated")) is not bool
                or not all(type(result.get(key)) is str and _ID.fullmatch(result[key])
                           for key in ("id", "readingId"))):
            category = "delivery_unknown"
    if category is None:
        return
    try:
        await steward._incident_hooks._report("bili_journal", category, "bili_journal_once")
    except Exception:  # noqa: BLE001 - Keep this instance closed if persistent reporting is unavailable.
        steward._bili_cron_failed = True


def share_hook_ready(steward):
    """The public retry must still be the installed F060 error-reporting wrapper."""
    try:
        binding = steward._incident_hooks._bindings["bili_share"]
        return (binding["object"] is steward and binding["name"] == "retry_bili_recommendation"
                and steward.retry_bili_recommendation is binding["wrapped"])
    except (AttributeError, KeyError, TypeError):
        return False
