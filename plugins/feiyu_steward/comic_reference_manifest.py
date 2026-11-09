"""Offline allow-list for the archived AI character references.

The archive is input data, not a source of runtime trust.  Every use resolves
the selected files under an explicit root and re-checks the manifest digest,
byte count, dimensions, JPEG decode and link/junction boundaries.  A caller
receives paths only after all checks pass.  No network access, image fetching,
or model invocation happens in this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable


_ID_RE = re.compile(r"(?:deepseek|gpt|claude|gemini|glm|kimi|qwen|grok|minimax)-\d{2}\Z|assistant-icon-reference\Z")
_FILE_RE = re.compile(r"(?:\d{2}\.jpg|assistant-icon-reference\.png)\Z")
_SHA_RE = re.compile(r"[a-f0-9]{64}\Z")
_PROFILE_NAMES = frozenset(("ai_friends", "dragon_helpers"))
_MANIFEST_NAME = "comic_reference_manifest.json"
_ARCHIVE_RELATIVE = Path("assets") / "ai-character-references" / "heybox-0-0"


class ReferenceManifestError(ValueError):
    """Raised when an allow-listed reference cannot be proven safe."""


def _is_link(path: Path) -> bool:
    """Reject symbolic links and Windows junctions at every boundary."""
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return True


def _safe_root(root: Path) -> Path:
    if _is_link(root) or not root.is_dir():
        raise ReferenceManifestError("reference_root_unavailable")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ReferenceManifestError("reference_root_unavailable") from None
    if _is_link(resolved):
        raise ReferenceManifestError("reference_root_unavailable")
    return resolved


def _safe_child(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not _FILE_RE.fullmatch(relative):
        raise ReferenceManifestError("reference_manifest_invalid")
    child = root / relative
    if _is_link(child) or not child.is_file():
        raise ReferenceManifestError("reference_asset_unavailable")
    try:
        resolved = child.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ReferenceManifestError("reference_asset_unavailable") from None
    if _is_link(resolved) or resolved.parent != root:
        raise ReferenceManifestError("reference_asset_unavailable")
    return resolved


def manifest_path() -> Path:
    """Return the candidate's local allow-list path without creating files."""
    return Path(__file__).resolve().parent / _MANIFEST_NAME


def default_archive_root(host: Any = None) -> Path:
    """Find a deployment root using only fixed local locations.

    Production copies may set ``_public_art_reference_root`` on the host.  The
    candidate workspace fallback is intentionally deterministic and does not
    inspect arbitrary environment variables or accept a caller supplied URL.
    """
    candidates: list[Path] = []
    configured = getattr(host, "_public_art_reference_root", None) if host is not None else None
    if isinstance(configured, (str, os.PathLike)):
        configured_path = Path(configured)
        candidates.append(configured_path)
    module_root = Path(__file__).resolve().parent
    # Candidate layout: work/stage-.../ -> repository root -> assets/...
    candidates.append(module_root.parent.parent / _ARCHIVE_RELATIVE)
    # A production copy normally keeps the archive below the plugin data root.
    host_root = getattr(host, "root", None) if host is not None else None
    if isinstance(host_root, (str, os.PathLike)):
        base = Path(host_root)
        for ancestor in (base, *base.parents[:5]):
            candidates.extend((
                ancestor / _ARCHIVE_RELATIVE,
                ancestor / "assets" / "ai-character-references" / "heybox-0-0",
            ))
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = os.fspath(candidate)
            if key in seen:
                continue
            seen.add(key)
            root = _safe_root(candidate)
            if (root / "manifest.json").is_file() or (root / _MANIFEST_NAME).is_file():
                return root
        except (ReferenceManifestError, OSError, ValueError):
            continue
    raise ReferenceManifestError("reference_root_unavailable")


def default_local_reference_root(host: Any = None) -> Path:
    """Find checked local helper references copied with the candidate."""
    candidates: list[Path] = []
    configured = getattr(host, "_public_art_reference_local_root", None) if host is not None else None
    if isinstance(configured, (str, os.PathLike)):
        candidates.append(Path(configured))
    candidates.append(Path(__file__).resolve().parent / "reference-assets")
    host_root = getattr(host, "root", None) if host is not None else None
    if isinstance(host_root, (str, os.PathLike)):
        base = Path(host_root)
        for ancestor in (base, *base.parents[:5]):
            candidates.extend((
                ancestor / "assets" / "noon-comic",
                ancestor / "plugins" / "feiyu_steward" / "assets" / "noon-comic",
                ancestor / "plugins" / "feiyu_steward" / "reference-assets",
            ))
    for candidate in candidates:
        try:
            root = _safe_root(candidate)
            if (root / "assistant-icon-reference.png").is_file():
                return root
        except (ReferenceManifestError, OSError, ValueError):
            continue
    raise ReferenceManifestError("reference_local_root_unavailable")


def _load_json(path: Path) -> dict[str, Any]:
    if _is_link(path) or not path.is_file() or path.stat().st_size > 128_000:
        raise ReferenceManifestError("reference_manifest_invalid")
    try:
        raw = path.read_text(encoding="utf-8-sig")
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        raise ReferenceManifestError("reference_manifest_invalid") from None
    if not isinstance(value, dict):
        raise ReferenceManifestError("reference_manifest_invalid")
    return value


def load_manifest(root: str | os.PathLike[str] | Path | None = None) -> dict[str, Any]:
    """Load and structurally validate the static candidate manifest."""
    if root is None:
        manifest_file = manifest_path()
        archive = None
    else:
        archive = _safe_root(Path(root))
        manifest_file = archive / _MANIFEST_NAME
        if not manifest_file.is_file():
            # The copied archive may retain its original manifest name.  The
            # candidate manifest is still required for role/profile policy.
            manifest_file = archive / "manifest.json"
    value = _load_json(manifest_file)
    if (value.get("schema") != 2 or not isinstance(value.get("manifest_id"), str)
            or value.get("source_post_id") != "0"
            or not isinstance(value.get("entries"), list)
            or not isinstance(value.get("profiles"), dict)):
        raise ReferenceManifestError("reference_manifest_invalid")
    rows: dict[str, dict[str, Any]] = {}
    for row in value["entries"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not _ID_RE.fullmatch(row["id"]) or row["id"] in rows
                or not _FILE_RE.fullmatch(str(row.get("file", "")))
                or not _SHA_RE.fullmatch(str(row.get("sha256", "")))
                or type(row.get("bytes")) is not int or not 1 <= row["bytes"] <= 10_000_000
                or type(row.get("width")) is not int or type(row.get("height")) is not int
                or not 1 <= row["width"] <= 4096 or not 1 <= row["height"] <= 4096
                or row.get("decoded_format") not in {"JPEG", "PNG"}
                or not isinstance(row.get("root", "archive"), str)
                or row.get("root", "archive") not in {"archive", "local"}
                or (row.get("source_post_id") != "0"
                    and row.get("source_post_id") != "local-approved-reference")):
            raise ReferenceManifestError("reference_manifest_invalid")
        rows[row["id"]] = dict(row)
    for profile in _PROFILE_NAMES:
        profile_row = value["profiles"].get(profile)
        if (not isinstance(profile_row, dict) or not isinstance(profile_row.get("reference_ids"), list)
                or not 3 <= len(profile_row["reference_ids"]) <= 9
                or len(set(profile_row["reference_ids"])) != len(profile_row["reference_ids"])
                or any(item not in rows for item in profile_row["reference_ids"])):
            raise ReferenceManifestError("reference_manifest_invalid")
    value["_entries"] = rows
    return value


def _decode_dimensions(path: Path, expected_format: str) -> tuple[int, int]:
    """Fully decode one JPEG using the bundled Pillow runtime when available."""
    try:
        from PIL import Image
    except ImportError:
        raise ReferenceManifestError("reference_decoder_unavailable") from None
    try:
        with Image.open(path) as image:
            if image.format != expected_format:
                raise ReferenceManifestError("reference_decode_failed")
            image.verify()
        with Image.open(path) as image:
            image.load()
            return int(image.width), int(image.height)
    except ReferenceManifestError:
        raise
    except Exception:
        raise ReferenceManifestError("reference_decode_failed") from None


def _checked_row(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    path = _safe_child(root, row["file"])
    try:
        size = path.stat().st_size
        raw = path.read_bytes()
    except OSError:
        raise ReferenceManifestError("reference_asset_unavailable") from None
    if size != row["bytes"] or len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise ReferenceManifestError("reference_asset_changed")
    width, height = _decode_dimensions(path, row["decoded_format"])
    if (width, height) != (row["width"], row["height"]):
        raise ReferenceManifestError("reference_dimensions_changed")
    return {
        "id": row["id"], "file": row["file"], "path": path,
        "sha256": row["sha256"], "bytes": row["bytes"],
        "width": width, "height": height,
        "source_post_id": row["source_post_id"],
        "root": row.get("root", "archive"),
    }


def select_reference_ids(profile: str, day: str, *, character_whitelist: Iterable[str] | None = None) -> tuple[str, ...]:
    """Select one deterministic four-image set for the whole daily series."""
    if profile not in _PROFILE_NAMES:
        raise ReferenceManifestError("invalid_profile")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except (TypeError, ValueError):
        raise ReferenceManifestError("invalid_day") from None
    manifest = load_manifest()
    allowed = tuple(manifest["profiles"][profile]["reference_ids"])
    if character_whitelist is not None:
        selected = tuple(character_whitelist)
        if (not 3 <= len(selected) <= 4 or len(set(selected)) != len(selected)
                or any(item not in allowed for item in selected)):
            raise ReferenceManifestError("invalid_character_whitelist")
        if profile == "ai_friends" and (len(selected) != 4 or selected[0] != "deepseek-01"):
            raise ReferenceManifestError("invalid_character_whitelist")
        if profile == "dragon_helpers" and selected != (
                "gpt-03", "deepseek-01", "assistant-icon-reference"):
            raise ReferenceManifestError("invalid_character_whitelist")
        # Preserve the caller's verified order; never add unrequested assets.
        return selected
    if len(allowed) < 4:
        # dragon_helpers deliberately uses three ordered references: GPT03,
        # the fixed DeepSeek01/Feiyu anchor and the six-color sprite.
        if profile == "dragon_helpers" and len(allowed) == 3:
            return allowed
        raise ReferenceManifestError("reference_manifest_invalid")
    if profile == "ai_friends":
        # DeepSeek01 is the archived Feiyu anchor in slot one; rotate only the
        # three AI friends that follow it, preserving series continuity.
        anchor = "deepseek-01"
        friends = tuple(item for item in allowed if item != anchor)
        offset = int(day.replace("-", "")) % len(friends)
        return (anchor,) + tuple(friends[(offset + index) % len(friends)] for index in range(3))
    if profile == "dragon_helpers":
        return allowed
    offset = int(day.replace("-", "")) % len(allowed)
    return tuple(allowed[(offset + index) % len(allowed)] for index in range(4))


def resolve_reference_set(
    profile: str,
    day: str,
    *,
    root: str | os.PathLike[str] | Path | None = None,
    character_whitelist: Iterable[str] | None = None,
    local_root: str | os.PathLike[str] | Path | None = None,
) -> tuple[dict[str, Any], ...]:
    """Resolve and verify the profile's fixed three-or-four reference set."""
    manifest = load_manifest()
    selected = select_reference_ids(profile, day, character_whitelist=character_whitelist)
    rows = manifest["_entries"]
    archive = _safe_root(Path(root)) if root is not None else default_archive_root()
    needs_local = any(rows[item].get("root") == "local" for item in selected)
    local = (_safe_root(Path(local_root)) if local_root is not None else default_local_reference_root()) if needs_local else None
    resolved = []
    for item in selected:
        row = rows[item]
        resolved.append(_checked_row(local if row.get("root") == "local" else archive, row))
    return tuple(resolved)


def profile_reference_ids(profile: str) -> tuple[str, ...]:
    """Return the approved roster (without touching image bytes)."""
    if profile not in _PROFILE_NAMES:
        raise ReferenceManifestError("invalid_profile")
    return tuple(load_manifest()["profiles"][profile]["reference_ids"])


__all__ = [
    "ReferenceManifestError", "default_archive_root", "default_local_reference_root", "load_manifest",
    "manifest_path", "profile_reference_ids", "resolve_reference_set",
    "select_reference_ids",
]
