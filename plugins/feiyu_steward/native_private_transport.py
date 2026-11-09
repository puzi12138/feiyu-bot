"""Explicit, durable owner-private text delivery through the trusted QQ bridge.

This module installs nothing on import. Producers provide a stable source/slot
operation; unsupported or unscoped calls keep the original protected transport.
Native acknowledgements deliberately contain no OneBot ``message_id``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import functools
import hashlib
import inspect
import json
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SELF = "0"
OWNER = "0"
OWNER_UMO = "snowluma-qq:FriendMessage:" + OWNER
_MESSAGE_ID = re.compile(r"-?[1-9]\d{0,23}\Z")
_SOURCE_ID = re.compile(r"-?[1-9]\d{0,23}:[1-9]\d{8,11}:[1-9]\d{0,23}\Z")
_SLOT = re.compile(r"[A-Za-z0-9_.:/-]{1,64}\Z")
_PRODUCER = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z")
_OCCURRENCE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_DECIMAL = re.compile(r"[1-9]\d{0,23}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_OPERATION: ContextVar[NativeOperation | None] = ContextVar(
    "feiyu_native_private_operation", default=None
)


class NativeTransportError(RuntimeError):
    """A fixed-code contract or storage failure, without private details."""


class NativeDeliveryUnknown(NativeTransportError):
    """A native request may have sent; retain the operation and never resend."""


@dataclass(frozen=True)
class NativeOperation:
    """A producer's logical output, independent of its text contents."""

    operation_id: str
    source_id: str
    slot: str
    producer: str = "reply"

    def __post_init__(self):
        expected = (
            f"reply/v1/{SELF}/{OWNER}/{self.source_id}/{self.slot}"
            if self.producer == "reply"
            else f"out/v1/{SELF}/{OWNER}/{self.producer}/{self.source_id}/{self.slot}"
        )
        if (
            not isinstance(self.source_id, str)
            or not isinstance(self.producer, str)
            or not _PRODUCER.fullmatch(self.producer)
            or not (_SOURCE_ID if self.producer == "reply" else _OCCURRENCE).fullmatch(
                self.source_id
            )
            or not isinstance(self.slot, str)
            or not _SLOT.fullmatch(self.slot)
            or self.operation_id != expected
            or len(self.operation_id) > 160
        ):
            raise NativeTransportError("invalid_native_operation")


def owner_reply_operation(event: Any, *, slot: str) -> NativeOperation:
    """Bind one producer-defined reply slot to a verified owner QQ event.

    Args:
        event: The original AstrBot event, with matching raw/message identities.
        slot: Stable output slot, including a deterministic part index if split.

    Returns:
        The operation reused for that exact logical output after a retry.

    Raises:
        NativeTransportError: The input is not a verified real owner event.
    """
    message = getattr(event, "message_obj", None)
    raw = getattr(message, "raw_message", None)
    source = getattr(message, "message_id", None)
    sender = raw.get("sender") if isinstance(raw, dict) else None
    if (
        str(getattr(event, "unified_msg_origin", "")) != OWNER_UMO
        or not isinstance(raw, dict)
        or raw.get("post_type") != "message"
        or raw.get("message_type") != "private"
        or str(raw.get("self_id", "")) != SELF
        or str(raw.get("user_id", "")) != OWNER
        or not isinstance(sender, dict)
        or str(sender.get("user_id", "")) != OWNER
        or str(raw.get("target_id", SELF)) != SELF
        or isinstance(source, bool)
        or not isinstance(source, (str, int))
        or str(source) != str(raw.get("message_id"))
        or not _MESSAGE_ID.fullmatch(str(source))
        or isinstance(raw.get("time"), bool)
        or not isinstance(raw.get("time"), int)
        or not 1000000000 <= raw["time"] <= int(time.time()) + 300
        or isinstance(raw.get("message_seq"), bool)
        or not isinstance(raw.get("message_seq"), int)
        or not 1 <= raw["message_seq"] < 10**24
    ):
        raise NativeTransportError("verified_owner_event_required")
    # Snow's public ID is only a 32-bit hash. Time and native source sequence
    # prevent an unrelated future event with that short ID sharing this slot.
    source = f"{source}:{raw['time']}:{raw['message_seq']}"
    return NativeOperation(f"reply/v1/{SELF}/{OWNER}/{source}/{slot}", source, slot)


def producer_operation(producer: str, occurrence: str, *, slot: str) -> NativeOperation:
    """Identify one output of a persisted proactive job or source occurrence.

    Args:
        producer: Stable producer namespace, for example ``daily-billing``.
        occurrence: Durable occurrence identity, for example a job row UUID.
        slot: Stable output/part index for this occurrence.

    Returns:
        A reusable operation independent of text content and wall-clock retries.

    Raises:
        NativeTransportError: The producer/occurrence cannot form a safe identity.
    """
    if producer == "reply":
        raise NativeTransportError("reserved_native_producer")
    return NativeOperation(
        f"out/v1/{SELF}/{OWNER}/{producer}/{occurrence}/{slot}",
        occurrence,
        slot,
        producer,
    )


@contextmanager
def native_delivery(operation: NativeOperation):
    """Scope exactly one logical native output, resetting context on failure.

    Args:
        operation: A stable producer/source/part identity, never a text hash.

    Yields:
        The operation provided by the caller.
    """
    if not isinstance(operation, NativeOperation):
        raise NativeTransportError("explicit_native_operation_required")
    token = _OPERATION.set(operation)
    try:
        yield operation
    finally:
        _OPERATION.reset(token)


class NativeOperationStore:
    """Atomically reserve operations before network dispatch, across processes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise NativeTransportError("absolute_native_store_required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_private_operations ("
                "operation_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL, "
                "reserved_at INTEGER NOT NULL, state TEXT NOT NULL, "
                "receipt_json TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_private_exclusions ("
                "operation_id TEXT PRIMARY KEY, reserved_at INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_private_media_payloads ("
                "operation_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim(
        self,
        operation: NativeOperation,
        payload_hash: str,
        *,
        media_evidence: list | None = None,
    ):
        """Reserve once, or return the existing operation without resending.

        Args:
            operation: Stable logical output identity.
            payload_hash: Exact UTF-8 text digest for detecting contract reuse.

        Returns:
            A stored row and whether this call created its reservation.
        """
        if (
            not isinstance(operation, NativeOperation)
            or not isinstance(payload_hash, str)
            or not _DIGEST.fullmatch(payload_hash)
        ):
            raise NativeTransportError("invalid_native_reservation")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM native_private_exclusions WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone():
                raise NativeTransportError("native_operation_uses_original_transport")
            row = db.execute(
                "SELECT * FROM native_private_operations WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone()
            created = row is None
            if row is None:
                db.execute(
                    "INSERT INTO native_private_operations (operation_id,payload_sha256,reserved_at,state,receipt_json) VALUES (?,?,?,'pending',NULL)",
                    (operation.operation_id, payload_hash, int(time.time())),
                )
                row = db.execute(
                    "SELECT * FROM native_private_operations WHERE operation_id=?",
                    (operation.operation_id,),
                ).fetchone()
            if row["payload_sha256"] != payload_hash:
                db.rollback()
                raise NativeTransportError("native_operation_content_changed")
            if (
                row["state"] not in {"pending", "unknown", "confirmed"}
                or not isinstance(row["reserved_at"], int)
                or row["reserved_at"] <= 0
                or (row["state"] == "confirmed") != (row["receipt_json"] is not None)
            ):
                raise NativeTransportError("native_reservation_corrupt")
            if row["receipt_json"] is not None:
                try:
                    receipt = json.loads(row["receipt_json"])
                except (TypeError, ValueError):
                    raise NativeTransportError("native_reservation_corrupt") from None
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("native_operation_id") != operation.operation_id
                    or receipt.get("payload_sha256") != payload_hash
                ):
                    raise NativeTransportError("native_reservation_corrupt")
            saved_media = db.execute(
                "SELECT payload_sha256,evidence_json FROM native_private_media_payloads WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone()
            if media_evidence is not None:
                encoded = json.dumps(
                    media_evidence, sort_keys=True, separators=(",", ":")
                )
                if saved_media is None and created:
                    db.execute(
                        "INSERT INTO native_private_media_payloads VALUES (?,?,?)",
                        (operation.operation_id, payload_hash, encoded),
                    )
                elif (
                    saved_media is None
                    or saved_media["payload_sha256"] != payload_hash
                    or saved_media["evidence_json"] != encoded
                ):
                    raise NativeTransportError("native_media_reservation_changed")
            elif saved_media is not None:
                raise NativeTransportError("native_media_route_already_reserved")
            db.commit()
            return dict(row), created

    def media_payload(self, operation: NativeOperation) -> tuple[dict, list] | None:
        """Read saved media evidence without reopening the original source.

        Args:
            operation: Stable producer occurrence and output slot.

        Returns:
            Saved operation/evidence, or None if it has no media reservation.

        Raises:
            NativeTransportError: Sidecar and operation evidence do not agree.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM native_private_operations WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone()
            media = db.execute(
                "SELECT * FROM native_private_media_payloads WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone()
            excluded = db.execute(
                "SELECT 1 FROM native_private_exclusions WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone()
        if media is None:
            return None
        if (
            row is None
            or excluded
            or media["payload_sha256"] != row["payload_sha256"]
            or (
                row["state"] not in {"pending", "unknown", "confirmed"}
                or not isinstance(row["reserved_at"], int)
                or row["reserved_at"] <= 0
                or (row["state"] == "confirmed") != (row["receipt_json"] is not None)
            )
        ):
            raise NativeTransportError("native_media_reservation_corrupt")
        try:
            evidence = json.loads(media["evidence_json"])
        except (ValueError, TypeError):
            raise NativeTransportError("native_media_reservation_corrupt") from None
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(part, dict) for part in evidence)
        ):
            raise NativeTransportError("native_media_reservation_corrupt")
        if row["receipt_json"] is not None:
            try:
                receipt = json.loads(row["receipt_json"])
            except (TypeError, ValueError):
                raise NativeTransportError("native_media_reservation_corrupt") from None
            if (
                not isinstance(receipt, dict)
                or receipt.get("native_operation_id") != operation.operation_id
                or receipt.get("payload_sha256") != row["payload_sha256"]
                or receipt.get("media_evidence") != evidence
            ):
                raise NativeTransportError("native_media_reservation_corrupt")
        return dict(row), evidence

    def finish(self, operation_id: str, receipt: dict | None):
        """Persist a verified receipt or retain an unknown native outcome.

        Args:
            operation_id: Previously reserved output identity.
            receipt: Verified receipt, or None if dispatch is uncertain.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT receipt_json FROM native_private_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise NativeTransportError("native_reservation_missing")
            encoded = json.dumps(receipt, sort_keys=True) if receipt else None
            if row["receipt_json"] and encoded and row["receipt_json"] != encoded:
                raise NativeTransportError("native_receipt_identity_changed")
            # A delayed failed reader must not downgrade another confirmed reader.
            if encoded:
                db.execute(
                    "UPDATE native_private_operations SET state='confirmed',receipt_json=? WHERE operation_id=?",
                    (encoded, operation_id),
                )
            elif not row["receipt_json"]:
                db.execute(
                    "UPDATE native_private_operations SET state='unknown' WHERE operation_id=?",
                    (operation_id,),
                )
            db.commit()

    def state(self, operation: NativeOperation) -> str | None:
        """Read a durable route reservation before considering any fallback.

        Args:
            operation: Exact source occurrence and output slot to inspect.

        Returns:
            Stored delivery state, or None when no native route was reserved.

        Raises:
            NativeTransportError: The saved reservation is inconsistent.
        """
        with self._connect() as db:
            if db.execute(
                "SELECT 1 FROM native_private_exclusions WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone():
                return "excluded"
            row = db.execute(
                "SELECT state,receipt_json FROM native_private_operations WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row["state"] not in {"pending", "unknown", "confirmed"} or (
            (row["state"] == "confirmed") != (row["receipt_json"] is not None)
        ):
            raise NativeTransportError("native_reservation_corrupt")
        return row["state"]

    def exclude(self, operation: NativeOperation) -> bool:
        """Reserve an unsupported output for the original transport once.

        Args:
            operation: Logical output whose route must survive event rebuilding.

        Returns:
            True only for the first caller allowed to dispatch via Snow.

        Raises:
            NativeTransportError: This output already reserved the native route.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM native_private_operations WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone():
                raise NativeTransportError("native_operation_route_already_reserved")
            result = db.execute(
                "INSERT OR IGNORE INTO native_private_exclusions VALUES (?,?)",
                (operation.operation_id, int(time.time())),
            )
            db.commit()
            return result.rowcount == 1


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TrustedNativeClient:
    """Read only the explicit protected bridge config; never use proxies/retries."""

    def __init__(self, config_path: Path, *, timeout: float = 60):
        self.config_path = Path(config_path)
        if not self.config_path.is_absolute() or timeout <= 0 or timeout > 90:
            raise NativeTransportError("invalid_native_client_config")
        self.timeout = timeout

    async def __call__(self, route: str, payload: dict):
        if route not in {"send-text", "send-message", "read-operation", "status"}:
            raise NativeTransportError("native_route_not_allowed")
        return await asyncio.to_thread(self._request, route, payload)

    def _request(self, route: str, payload: dict):
        try:
            if self.config_path.is_symlink() or self.config_path.stat().st_size > 8192:
                raise ValueError
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            port, token = config.get("port"), config.get("token")
            if (
                config.get("enabled") is not True
                or config.get("expected_self_id") != SELF
                or config.get("private_target") != OWNER
                or isinstance(port, bool)
                or not isinstance(port, int)
                or not 1024 <= port <= 65535
                or not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token)
            ):
                raise ValueError
            request = Request(
                f"http://127.0.0.1:{port}/v1/{route}",
                data=None
                if route == "status"
                else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
                method="GET" if route == "status" else "POST",
            )
            with build_opener(ProxyHandler({}), _NoRedirect()).open(
                request, timeout=self.timeout
            ) as response:
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536:
                    raise ValueError
                return json.loads(body.decode("utf-8"))
        except (OSError, ValueError, TypeError, AttributeError, HTTPError, URLError):
            raise NativeTransportError("native_bridge_request_unconfirmed") from None


class NativePrivateTransport:
    """Route only explicitly scoped owner plain text, preserving other calls."""

    def __init__(
        self,
        native: Callable,
        store: NativeOperationStore,
        *,
        enabled: bool = False,
        enabled_media: bool = False,
        media_spool: Any = None,
        authorize: Callable | None = None,
    ):
        self.native, self.store = native, store
        self.enabled, self.authorize = enabled, authorize
        self.enabled_media, self.media_spool = enabled_media, media_spool

    @staticmethod
    def message_candidate(action: str, params: dict) -> list[dict] | None:
        """Parse a bounded ordered owner message without fetching media URLs.

        Args:
            action: Exact private send action.
            params: OneBot parameters with only text and local/base64 images.

        Returns:
            Ordered text/image spool input, or None for unsupported shapes.
        """
        if (
            action != "send_private_msg"
            or set(params) - {"user_id", "message", "self_id", "auto_escape"}
            or isinstance(params.get("user_id"), bool)
            or str(params.get("user_id")) != OWNER
            or str(params.get("self_id", SELF)) != SELF
            or not isinstance(params.get("auto_escape", False), bool)
        ):
            return None
        message = params.get("message")
        if not isinstance(message, list) or not 1 <= len(message) <= 16:
            return None
        parts, images, image_bytes, text_chars, text_bytes = [], 0, 0, 0, 0
        for segment in message:
            if (
                not isinstance(segment, dict)
                or set(segment) != {"type", "data"}
                or not isinstance(segment["data"], dict)
            ):
                return None
            data = segment["data"]
            if segment["type"] == "text" and set(data) == {"text"}:
                text = data["text"]
                if (
                    not isinstance(text, str)
                    or not text
                    or re.search(
                        r"\[CQ:|[\x00-\x08\x0b\x0c\x0e-\x1f]", text, re.IGNORECASE
                    )
                ):
                    return None
                text_chars += len(text)
                text_bytes += len(text.encode("utf-8"))
                if text_chars > 4000 or text_bytes > 12000:
                    return None
                if parts and parts[-1]["type"] == "text":
                    parts[-1]["text"] += text
                else:
                    parts.append({"type": "text", "text": text})
            elif (
                segment["type"] == "image"
                and set(data) == {"file"}
                and isinstance(data["file"], str)
            ):
                source = data["file"]
                images += 1
                if images > 6:
                    return None
                if source.startswith("base64://"):
                    encoded = source[9:]
                    if not encoded or len(encoded) > ((8 * 1024 * 1024 + 2) // 3) * 4:
                        return None
                    try:
                        content = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError):
                        return None
                    image_bytes += len(content)
                    if (
                        not 0 < len(content) <= 8 * 1024 * 1024
                        or image_bytes > 32 * 1024 * 1024
                    ):
                        return None
                    parts.append({"type": "image", "data": content})
                else:
                    parsed = urlsplit(source)
                    if parsed.scheme == "file":
                        if parsed.netloc or parsed.query or parsed.fragment:
                            return None
                        local = unquote(parsed.path)
                        if re.match(r"^/[A-Za-z]:/", local):
                            local = local[1:]
                    elif parsed.scheme or source.startswith(("//", "\\\\")):
                        # Windows absolute drive paths have a one-letter scheme.
                        if not re.match(r"^[A-Za-z]:[/\\]", source):
                            return None
                        local = source
                    else:
                        local = source
                    local_path = Path(local)
                    if not local_path.is_absolute():
                        return None
                    parts.append({"type": "image", "path": local_path})
            else:
                return None
        return parts if images else None

    @staticmethod
    def text_candidate(action: str, params: dict) -> str | None:
        if (
            action != "send_private_msg"
            or set(params) - {"user_id", "message", "self_id", "auto_escape"}
            or isinstance(params.get("user_id"), bool)
            or str(params.get("user_id")) != OWNER
            or str(params.get("self_id", SELF)) != SELF
            or not isinstance(params.get("auto_escape", False), bool)
        ):
            return None
        message = params.get("message")
        if isinstance(message, str):
            text = message
        elif isinstance(message, list) and message:
            parts = []
            for part in message:
                if (
                    not isinstance(part, dict)
                    or set(part) != {"type", "data"}
                    or part["type"] != "text"
                    or not isinstance(part["data"], dict)
                    or set(part["data"]) != {"text"}
                    or not isinstance(part["data"]["text"], str)
                ):
                    return None
                parts.append(part["data"]["text"])
            text = "".join(parts)
        else:
            return None
        if (
            not 1 <= len(text) <= 4000
            or len(text.encode("utf-8")) > 12000
            or re.search(r"\[CQ:", text, re.IGNORECASE)
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text)
        ):
            return None
        return text

    @staticmethod
    def verified_receipt(
        value: Any, row: dict, media_evidence: list | None = None
    ) -> dict:
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise NativeTransportError("native_receipt_unconfirmed")
        data = value.get("data")
        if (
            not isinstance(data, dict)
            or data.get("state") != "confirmed"
            or data.get("operation_id") != row["operation_id"]
            or data.get("payload_sha256") != row["payload_sha256"]
            or not _DIGEST.fullmatch(str(data.get("payload_sha256")))
            or data.get("evidence") != "native_kernel_getMsgsByMsgId"
            or data.get("snow_history_integrated") is not False
            or data.get("desktop_display_verified") is not False
        ):
            raise NativeTransportError("native_receipt_unconfirmed")
        identity = data.get("native_identity")
        if (
            not isinstance(identity, dict)
            or set(identity)
            != {
                "chat_type",
                "peer_uid",
                "peer_uin",
                "self_uin",
                "msg_id",
                "msg_seq",
                "msg_time",
                "msg_random",
            }
            or identity["chat_type"] != 1
            or isinstance(identity["chat_type"], bool)
            or identity["peer_uin"] != OWNER
            or identity["self_uin"] != SELF
            or not isinstance(identity["peer_uid"], str)
            or not re.fullmatch(r"u_[A-Za-z0-9_-]{5,100}", identity["peer_uid"])
            or any(
                not isinstance(identity[key], str)
                or not _DECIMAL.fullmatch(identity[key])
                for key in ("msg_id", "msg_seq")
            )
            or isinstance(identity["msg_time"], bool)
            or not isinstance(identity["msg_time"], int)
            or not row["reserved_at"] - 5
            <= identity["msg_time"]
            <= row["reserved_at"] + 300
            or not (
                identity["msg_random"] is None
                or isinstance(identity["msg_random"], str)
                and re.fullmatch(r"-?\d{1,24}", identity["msg_random"])
            )
        ):
            raise NativeTransportError("native_identity_unconfirmed")
        result = {
            "native_operation_id": row["operation_id"],
            "native_message_id": identity["msg_id"],
            "native_identity": copy.deepcopy(identity),
            "payload_sha256": row["payload_sha256"],
            "transport": "framework_real_session",
            "native_delivery_confirmed": True,
            "snow_history_integrated": False,
            "desktop_display_verified": False,
        }
        if media_evidence is not None:
            if (
                data.get("media_evidence") != media_evidence
                or data.get("payload_evidence")
                != "ordered_native_text_sha256_image_md5_size_dimensions_local_sha256"
            ):
                raise NativeTransportError("native_media_evidence_unconfirmed")
            result["media_evidence"] = copy.deepcopy(media_evidence)
            result["payload_evidence"] = data["payload_evidence"]
        elif "media_evidence" in data:
            raise NativeTransportError("native_media_evidence_unexpected")
        return result

    async def call(self, original: Callable, action: str, **params):
        operation = _OPERATION.get()
        if operation is None:
            return await original(action, **params)
        # A scope names one intended native action. Never escape to Snow after
        # a configuration or payload-shape change on a repeated operation.
        if not self.enabled:
            raise NativeTransportError("native_transport_disabled")
        text = self.text_candidate(action, params)
        parts = (
            self.message_candidate(action, params)
            if self.enabled_media and text is None
            else None
        )
        if text is None and parts is None:
            raise NativeTransportError("native_scope_requires_owner_plain_text")
        if self.authorize is None:
            raise NativeTransportError("native_authorizer_required")
        allowed = self.authorize(action, copy.deepcopy(params), operation)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if allowed is not True:
            raise PermissionError("native_action_not_authorized")
        media_evidence = None
        saved_media = self.store.media_payload(operation) if parts is not None else None
        if saved_media is not None:
            row, media_evidence = saved_media
            payload_hash = row["payload_sha256"]
        elif parts is not None:
            if self.media_spool is None:
                raise NativeTransportError("native_media_spool_required")
            status = await self.native("status", {})
            status = (
                status.get("data")
                if isinstance(status, dict) and status.get("ok") is True
                else None
            )
            if (
                not isinstance(status, dict)
                or status.get("self_id") != SELF
                or status.get("protocol_version") != 2
                or status.get("media_manifest_schema") != 1
                or not isinstance(status.get("capabilities"), list)
                or "owner_private_ordered_text_image"
                not in status.get("capabilities", [])
            ):
                raise NativeTransportError("native_media_capability_unavailable")
            manifest = await asyncio.to_thread(self.media_spool.build, parts)
            payload_hash = manifest.manifest_sha256
            media_evidence = []
            for part in manifest.manifest["parts"]:
                if part["type"] == "text":
                    content = part["text"].encode("utf-8")
                    media_evidence.append(
                        {
                            "index": part["index"],
                            "type": "text",
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                        }
                    )
                else:
                    from .native_media_spool import _read_local

                    content = _read_local(
                        self.media_spool.root / part["file"],
                        8 * 1024 * 1024,
                        private=True,
                    )
                    if (
                        len(content) != part["size"]
                        or hashlib.sha256(content).hexdigest() != part["sha256"]
                    ):
                        raise NativeTransportError("native_media_spool_changed")
                    media_evidence.append(
                        {
                            "index": part["index"],
                            "type": "image",
                            "sha256": part["sha256"],
                            "md5": hashlib.md5(
                                content, usedforsecurity=False
                            ).hexdigest(),
                            "size": part["size"],
                            "mime": part["mime"],
                            "width": part["width"],
                            "height": part["height"],
                        }
                    )
        else:
            payload_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # This commit is before any network dispatch. Its unique primary key
        # serializes even independent AstrBot processes using the same ledger.
        if saved_media is not None:
            created = False
        elif media_evidence is not None:
            row, created = self.store.claim(
                operation, payload_hash, media_evidence=media_evidence
            )
        else:
            row, created = self.store.claim(operation, payload_hash)
        route = (
            ("send-message" if parts is not None else "send-text")
            if created
            else "read-operation"
        )
        request = {"operation_id": operation.operation_id}
        if created:
            request.update(target_id=OWNER)
            request.update(
                manifest_sha256=payload_hash
            ) if parts is not None else request.update(text=text)
        try:
            value = await self.native(route, request)
            receipt = self.verified_receipt(value, row, media_evidence)
            self.store.finish(operation.operation_id, receipt)
            return receipt
        except BaseException as error:
            # Cancellation cannot cancel a to_thread HTTP send. Its reservation
            # survives; a later call only reads. Never invoke Snow after this.
            try:
                self.store.finish(operation.operation_id, None)
            except (OSError, sqlite3.Error, NativeTransportError):
                # The pre-dispatch reservation still prevents a later send.
                receipt = None
            if isinstance(
                error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            raise NativeDeliveryUnknown(
                "native_delivery_unknown_do_not_resend"
            ) from None


def install_native_private_transport(bot: Any, transport: NativePrivateTransport):
    """Wrap a client without weakening the existing producer/permission gates.

    Args:
        bot: CQHttp client whose call_action is already protected.
        transport: Explicitly configured router with its mandatory authorizer.

    Returns:
        An identity-safe restoration callback for normal plugin termination.

    Raises:
        NativeTransportError: The client lacks call_action.
    """
    original = getattr(bot, "call_action", None)
    if not callable(original):
        raise NativeTransportError("native_adapter_unsupported")
    active = True

    @functools.wraps(original)
    async def call_action(action, **params):
        if not active:
            # No scoped operation may escape through a buried old router.
            # An outer active router consumes eligible operations itself.
            if _OPERATION.get() is not None:
                raise NativeTransportError("native_retired_transport_requires_current_route")
            return await original(action, **params)
        return await transport.call(original, action, **params)

    bot.call_action = call_action

    def restore():
        nonlocal active
        active = False
        if getattr(bot, "call_action", None) is call_action:
            bot.call_action = original

    restore.wrapped = call_action
    return restore
