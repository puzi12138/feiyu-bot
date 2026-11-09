"""Bounded local image snapshots for the trusted owner-private QQ bridge.

The caller supplies the stable delivery operation separately. Content hashes
identify immutable spool bytes; they never identify or deduplicate deliveries.
The spool root must already exist with a private DACL (or POSIX mode 0700).
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import secrets
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_PARTS = 16
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_PIXELS = 32_000_000
MAX_GIF_FRAMES = 100
MAX_GIF_TOTAL_PIXELS = 64_000_000
MAX_TEXT_CHARS = 4000
MAX_TEXT_BYTES = 12000
MAX_MANIFEST_BYTES = 32 * 1024
_MIME = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
    "GIF": ("image/gif", "gif"),
}


class NativeMediaSpoolError(RuntimeError):
    """A fixed-code spool rejection that excludes private source details."""


@dataclass(frozen=True)
class NativeMediaManifest:
    """A durable manifest reference, independent of the sending operation."""

    manifest: dict
    manifest_file: Path
    manifest_sha256: str


def _assert_no_reparse(path: Path):
    """Reject symlinks, junctions, alternate streams and non-absolute paths.

    Args:
        path: Existing local path to inspect without resolving away links.

    Raises:
        NativeMediaSpoolError: The path cannot be trusted as an ordinary path.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise NativeMediaSpoolError("absolute_local_media_path_required")
    # A drive separator is allowed only in the drive prefix, never as an ADS.
    if ":" in str(path)[len(path.drive) :]:
        raise NativeMediaSpoolError("media_path_stream_not_allowed")
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise NativeMediaSpoolError("media_reparse_path_not_allowed")


def _assert_private_path(path: Path, *, protected: bool = False):
    """Check private ownership and grants without changing an existing ACL.

    Args:
        path: Existing spool path with no symlinks or reparse ancestors.
        protected: Require a protected DACL for the pre-provisioned root.

    Raises:
        NativeMediaSpoolError: Ownership or access grants are not private.
    """
    _assert_no_reparse(path)
    info = path.lstat()
    if os.name != "nt":
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NativeMediaSpoolError("private_media_spool_required")
        return
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    void = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(void),
        void,
        ctypes.POINTER(void),
        void,
        ctypes.POINTER(void),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [
        void,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [void, void, wintypes.DWORD, ctypes.c_int]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [void, wintypes.DWORD, ctypes.POINTER(void)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [void, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [void]
    kernel.LocalFree.restype = void
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        void,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL

    class TokenUser(ctypes.Structure):
        _fields_ = [("sid", void), ("attributes", wintypes.DWORD)]

    class AclSize(ctypes.Structure):
        _fields_ = [
            ("count", wintypes.DWORD),
            ("used", wintypes.DWORD),
            ("free", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("kind", ctypes.c_ubyte),
            ("flags", ctypes.c_ubyte),
            ("size", wintypes.WORD),
        ]

    # Native security allocations are released even when a malformed ACL is
    # rejected. No localized command output, account names or tokens are used.
    descriptor = void()
    owner = void()
    dacl = void()
    token = wintypes.HANDLE()
    converted = wintypes.LPWSTR()
    try:
        if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(), 8, ctypes.byref(token)
        ):
            raise NativeMediaSpoolError("private_media_acl_unavailable")
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value < 4096:
            raise NativeMediaSpoolError("private_media_acl_unavailable")
        token_info = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(
            token, 1, token_info, needed, ctypes.byref(needed)
        ):
            raise NativeMediaSpoolError("private_media_acl_unavailable")
        token_user = ctypes.cast(token_info, ctypes.POINTER(TokenUser)).contents
        if not advapi.ConvertSidToStringSidW(token_user.sid, ctypes.byref(converted)):
            raise NativeMediaSpoolError("private_media_acl_unavailable")
        allowed = {converted.value, "S-1-5-18", "S-1-5-32-544"}
        kernel.LocalFree(ctypes.cast(converted, void))
        converted = wintypes.LPWSTR()
        if (
            advapi.GetNamedSecurityInfoW(
                str(path),
                1,
                5,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
            != 0
            or not dacl.value
        ):
            raise NativeMediaSpoolError("private_media_acl_unavailable")
        if not advapi.ConvertSidToStringSidW(owner, ctypes.byref(converted)):
            raise NativeMediaSpoolError("private_media_acl_unavailable")
        if converted.value not in allowed:
            raise NativeMediaSpoolError("private_media_spool_required")
        kernel.LocalFree(ctypes.cast(converted, void))
        converted = wintypes.LPWSTR()
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if (
            not advapi.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            )
            or protected
            and not control.value & 0x1000
        ):
            raise NativeMediaSpoolError("private_media_spool_required")
        acl_size = AclSize()
        if (
            not advapi.GetAclInformation(
                dacl, ctypes.byref(acl_size), ctypes.sizeof(acl_size), 2
            )
            or acl_size.count != 3
        ):
            raise NativeMediaSpoolError("private_media_spool_required")
        seen = set()
        expected_flags = 3 if stat.S_ISDIR(info.st_mode) else 0
        for index in range(acl_size.count):
            ace = void()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise NativeMediaSpoolError("private_media_acl_unavailable")
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            ace_type, ace_flags, ace_size = header.kind, header.flags, header.size
            if (
                ace_type != 0
                or ace_flags not in {expected_flags, expected_flags | 16}
                or protected
                and ace_flags & 16
                or ace_size < 12
            ):
                raise NativeMediaSpoolError("private_media_spool_required")
            if ctypes.c_uint32.from_address(ace.value + 4).value != 0x1F01FF:
                raise NativeMediaSpoolError("private_media_spool_required")
            if not advapi.ConvertSidToStringSidW(
                void(ace.value + 8), ctypes.byref(converted)
            ):
                raise NativeMediaSpoolError("private_media_acl_unavailable")
            if converted.value not in allowed or converted.value in seen:
                raise NativeMediaSpoolError("private_media_spool_required")
            seen.add(converted.value)
            kernel.LocalFree(ctypes.cast(converted, void))
            converted = wintypes.LPWSTR()
        if seen != allowed:
            raise NativeMediaSpoolError("private_media_spool_required")
    finally:
        if converted:
            kernel.LocalFree(ctypes.cast(converted, void))
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if token.value:
            kernel.CloseHandle(token)


def _read_local(path: Path, limit: int, *, private: bool = False) -> bytes:
    """Snapshot one bounded regular file and detect identity changes.

    Args:
        path: Absolute existing local file, never an URL or alternate stream.
        limit: Maximum accepted file size.
        private: Whether spool ownership and single-link identity are required.

    Returns:
        Exact captured file bytes.

    Raises:
        NativeMediaSpoolError: The path, size or identity is untrusted.
    """
    _assert_no_reparse(path)
    if private:
        _assert_private_path(path)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or not 0 < before.st_size <= limit
        or private
        and before.st_nlink != 1
    ):
        raise NativeMediaSpoolError("invalid_local_media_file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    _assert_no_reparse(path)
    current = path.lstat()
    identities = {
        (entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns)
        for entry in (before, opened, after, current)
    }
    if len(identities) != 1 or len(data) != before.st_size or len(data) > limit:
        raise NativeMediaSpoolError("local_media_changed_during_snapshot")
    return data


def _inspect_image(data: bytes) -> tuple[str, str, int, int]:
    """Decode accepted raster bytes under fixed frame and pixel limits.

    Args:
        data: Bounded image snapshot whose original bytes will be preserved.

    Returns:
        MIME, canonical suffix, width and height.

    Raises:
        NativeMediaSpoolError: The image is corrupt, unsupported or too large.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in _MIME:
                    raise NativeMediaSpoolError("native_media_format_not_allowed")
                width, height = image.size
                if (
                    not 1 <= width <= MAX_DIMENSION
                    or not 1 <= height <= MAX_DIMENSION
                    or width * height > MAX_PIXELS
                ):
                    raise NativeMediaSpoolError("native_media_dimensions_exceeded")
                is_gif = image.format == "GIF"
                frames = MAX_GIF_FRAMES + 1 if is_gif else getattr(image, "n_frames", 1)
                if not is_gif and frames != 1:
                    raise NativeMediaSpoolError("native_media_animation_not_allowed")
                cumulative = 0
                for index in range(frames):
                    try:
                        image.seek(index)
                    except EOFError:
                        if not is_gif:
                            raise NativeMediaSpoolError(
                                "native_media_image_invalid"
                            ) from None
                        break
                    if is_gif and index == MAX_GIF_FRAMES:
                        raise NativeMediaSpoolError("native_media_gif_frames_exceeded")
                    frame_width, frame_height = image.size
                    cumulative += frame_width * frame_height
                    if (
                        frame_width > MAX_DIMENSION
                        or frame_height > MAX_DIMENSION
                        or frame_width * frame_height > MAX_PIXELS
                        or cumulative > MAX_GIF_TOTAL_PIXELS
                    ):
                        raise NativeMediaSpoolError(
                            "native_media_frame_pixels_exceeded"
                        )
                    image.load()
                mime, suffix = _MIME[image.format]
                return mime, suffix, width, height
    except NativeMediaSpoolError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        raise NativeMediaSpoolError("native_media_image_invalid") from None


class NativeMediaSpool:
    """Write content-addressed objects without replacing existing spool bytes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        try:
            _assert_private_path(self.root, protected=True)
            if not self.root.is_dir():
                raise NativeMediaSpoolError("private_media_spool_directory_required")
            for folder in ("objects", "manifests"):
                target = self.root / folder
                # On current Windows Python, mkdir(0700) synthesizes an OWNER
                # RIGHTS ACE. Inherit the already verified explicit SID DACL.
                target.mkdir(mode=0o777 if os.name == "nt" else 0o700, exist_ok=True)
                _assert_private_path(target)
                if not target.is_dir():
                    raise NativeMediaSpoolError(
                        "private_media_spool_directory_required"
                    )
        except NativeMediaSpoolError:
            raise
        except (OSError, ValueError, TypeError):
            raise NativeMediaSpoolError("private_media_spool_unavailable") from None

    def _store(self, folder: str, filename: str, data: bytes) -> Path:
        target = self.root / folder / filename
        _assert_private_path(self.root, protected=True)
        _assert_private_path(target.parent)
        temporary = target.parent / ("." + secrets.token_hex(16) + ".part")
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if _read_local(temporary, len(data), private=True) != data:
                raise NativeMediaSpoolError("native_media_write_verification_failed")
            try:
                # Windows rename fails if the target exists. POSIX needs link
                # for the same no-replace contract. Neither path overwrites.
                if os.name == "nt":
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
            except FileExistsError:
                if _read_local(target, len(data), private=True) != data:
                    raise NativeMediaSpoolError(
                        "native_media_existing_object_changed"
                    ) from None
            finally:
                temporary.unlink(missing_ok=True)
            if _read_local(target, len(data), private=True) != data:
                raise NativeMediaSpoolError("native_media_write_verification_failed")
            os.chmod(target, stat.S_IREAD if os.name == "nt" else 0o400)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def build(self, parts: list[dict]) -> NativeMediaManifest:
        """Snapshot local images and persist one ordered immutable manifest.

        Args:
            parts: Ordered text parts (type/text) or image parts (type/path or
                type/data). A path must be an absolute local Path; data is bytes.

        Returns:
            Manifest data and its durable content-addressed local reference.

        Raises:
            NativeMediaSpoolError: The input, protected spool or image is invalid.
        """
        try:
            _assert_private_path(self.root, protected=True)
            # Each nonempty raw text part consumes at least one allowed
            # character. Normalize before applying the final part bound.
            if (
                not isinstance(parts, list)
                or not 1 <= len(parts) <= MAX_TEXT_CHARS + MAX_IMAGES
            ):
                raise NativeMediaSpoolError("native_media_parts_exceeded")
            ordered, images, total_bytes, text_chars, text_bytes = [], 0, 0, 0, 0
            for part in parts:
                if not isinstance(part, dict):
                    raise NativeMediaSpoolError("native_media_part_invalid")
                if part.get("type") == "text" and set(part) == {"type", "text"}:
                    text = part["text"]
                    if (
                        not isinstance(text, str)
                        or not text
                        or re.search(
                            r"\[CQ:|[\x00-\x08\x0b\x0c\x0e-\x1f]", text, re.IGNORECASE
                        )
                    ):
                        raise NativeMediaSpoolError("native_media_text_invalid")
                    text_chars += len(text)
                    text_bytes += len(text.encode("utf-8"))
                    if text_chars > MAX_TEXT_CHARS or text_bytes > MAX_TEXT_BYTES:
                        raise NativeMediaSpoolError("native_media_text_exceeded")
                    if ordered and ordered[-1]["type"] == "text":
                        ordered[-1]["text"] += text
                        if re.search(r"\[CQ:", ordered[-1]["text"], re.IGNORECASE):
                            raise NativeMediaSpoolError("native_media_text_invalid")
                    else:
                        ordered.append(
                            {"index": len(ordered), "type": "text", "text": text}
                        )
                    continue
                if part.get("type") != "image" or set(part) not in (
                    {"type", "path"},
                    {"type", "data"},
                ):
                    raise NativeMediaSpoolError("native_media_part_invalid")
                images += 1
                if images > MAX_IMAGES:
                    raise NativeMediaSpoolError("native_media_image_count_exceeded")
                if "path" in part:
                    if not isinstance(part["path"], Path):
                        raise NativeMediaSpoolError(
                            "absolute_local_media_path_required"
                        )
                    data = _read_local(part["path"], MAX_IMAGE_BYTES)
                else:
                    data = part["data"]
                if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
                    raise NativeMediaSpoolError("native_media_bytes_exceeded")
                total_bytes += len(data)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise NativeMediaSpoolError("native_media_total_bytes_exceeded")
                mime, suffix, width, height = _inspect_image(data)
                digest = hashlib.sha256(data).hexdigest()
                relative = f"objects/{digest}.{suffix}"
                self._store("objects", f"{digest}.{suffix}", data)
                ordered.append(
                    {
                        "index": len(ordered),
                        "type": "image",
                        "file": relative,
                        "sha256": digest,
                        "size": len(data),
                        "mime": mime,
                        "width": width,
                        "height": height,
                    }
                )
            if not images:
                raise NativeMediaSpoolError("native_media_image_required")
            if len(ordered) > MAX_PARTS:
                raise NativeMediaSpoolError("native_media_parts_exceeded")
            manifest = {"schema": 1, "parts": ordered}
            encoded = json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise NativeMediaSpoolError("native_media_manifest_exceeded")
            manifest_hash = hashlib.sha256(encoded).hexdigest()
            filename = self._store("manifests", manifest_hash + ".json", encoded)
            return NativeMediaManifest(manifest, filename, manifest_hash)
        except NativeMediaSpoolError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            raise NativeMediaSpoolError("native_media_spool_failed") from None
