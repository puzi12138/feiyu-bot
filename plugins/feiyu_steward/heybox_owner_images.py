"""Bind a quoted owner image to the file selected for a post draft."""

import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from astrbot.api.message_components import Image, Reply

from .admin_info import OWNER, owner_private

_MAX_IMAGE_BYTES = 10_000_000
_DIGEST_TOKEN = re.compile(r"(?<![a-fA-F0-9])([a-fA-F0-9]{32})(?![a-fA-F0-9])")


def _reference_digest(value):
    """Extract an unambiguous image checksum from a trusted component field."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("", "file", "https"):
            return None
        if parsed.scheme == "https" and not parsed.hostname:
            return None
        # QQ image references may put the checksum in a path segment, a file
        # name, or fname. Ignore unrelated query fields such as access tokens.
        parts = [unquote(parsed.path)]
        if parsed.scheme == "https":
            query = parse_qs(parsed.query, keep_blank_values=False, max_num_fields=64)
            parts.extend(query.get("fname", []))
        hashes = {match.lower() for part in parts for match in _DIGEST_TOKEN.findall(part)}
        return next(iter(hashes)) if len(hashes) == 1 else None
    except (TypeError, ValueError):
        return None


def owner_reply_image_sha256(event, paths, allowed_roots):
    """Return a source SHA-256 only for the owner's one matching reply image.

    Paths are candidate files, not evidence of the owner's intent. The MD5
    checksum comes from the adapter's typed image component; the SHA-256 of
    the same bytes must subsequently be checked when staging the image.
    """
    if not owner_private(event) or not isinstance(paths, list) or len(paths) != 1:
        return None
    message = getattr(getattr(event, "message_obj", None), "message", None)
    if not isinstance(message, (list, tuple)):
        return None
    replies = [part for part in message if isinstance(part, Reply)]
    # The existing direct-image policy stays independent of this reply-only
    # proof. Do not infer which image is meant if both sources are present.
    if len(replies) != 1 or any(isinstance(part, Image) for part in message):
        return None
    reply = replies[0]
    if str(getattr(reply, "sender_id", "")) != OWNER:
        return None
    chain = getattr(reply, "chain", None)
    if not isinstance(chain, (list, tuple)):
        return None
    images = [part for part in chain if isinstance(part, Image)]
    if len(images) != 1 or any(isinstance(part, Reply) for part in chain):
        return None
    values = {_reference_digest(getattr(images[0], name, None)) for name in ("file", "url")}
    values.discard(None)
    if len(values) != 1:
        return None
    try:
        source = Path(paths[0]).resolve(strict=True)
        if not any(source.is_relative_to(root) for root in allowed_roots):
            return None
        if not source.is_file() or not 0 < source.stat().st_size <= _MAX_IMAGE_BYTES:
            return None
        with source.open("rb") as handle:
            data = handle.read(_MAX_IMAGE_BYTES + 1)
        if not 0 < len(data) <= _MAX_IMAGE_BYTES:
            return None
        if hashlib.md5(data, usedforsecurity=False).hexdigest() != next(iter(values)):
            return None
        return hashlib.sha256(data).hexdigest()
    except (OSError, TypeError, ValueError):
        return None
