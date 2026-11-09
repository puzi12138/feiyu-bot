"""One complete offline WOFF2, with a verified SFNT cache for Pillow."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import re
import struct
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path

from PIL import ImageFont

BUNDLED_FONT_FILENAME = "LXGWWenKaiScreen.woff2"
# Created with WOFF2FlavorData(transformedTables=set()): retaining glyf/loca
# avoids rebuilding 47,871 outlines in Python during the first offline decode.
BUNDLED_FONT_SHA256 = "4657e8df0a103f3c2d48b84632d6038aa2b3febddff5ba81ba27f97dbb2737e8"
_MAX_FONT_BYTES = 32 * 1024 * 1024
_THREAD_LOCK = threading.Lock()
_SUBSET_LOCK = threading.Lock()
_SUBSET_CACHE: OrderedDict[str, bytes] = OrderedDict()
_SUBSET_CACHE_BYTES = 0
_SUBSET_CACHE_MAX_BYTES = 8 * 1024 * 1024
_SUBSET_CACHE_MAX_ENTRIES = 16
# FreeType's auto-hinter measures other glyphs in the same script. Retaining
# its CJK blue-zone/standard characters prevents unchanged outlines from moving
# by a pixel after subsetting. The small Greek/Cyrillic range also preserves
# script classification for shared/ligature glyphs in this font (including PUA).
# Reference character data: FreeType src/autofit/{afblue.dat,afscript.h}.
_CJK_HINT_CHARACTERS = (
    "田囗他们你來們到和地对對就席我时時會来為能舰說说这這齊"
    "军同已愿既星是景民照现現理用置要軍那配里開雷露面顾"
    "个为人他以们你來個們到和大对對就我时時有来為要說说"
    "主些因它想意理生當看着置者自著裡过还进進過道還里面"
    "些们你來們到和地她将將就年得情最样樣理能說说这這通"
    "即吗吧听呢品响嗎师師收断斷明眼間间际陈限除陳随際隨"
    "事前學将將情想或政斯新样樣民沒没然特现現球第經谁起"
    "例別别制动動吗嗎增指明朝期构物确种調调費费那都間间"
)
_HINT_CHARACTERS = frozenset(range(0x20, 0x7F)) | frozenset(range(0x370, 0x530)) | frozenset(map(ord, _CJK_HINT_CHARACTERS))


def collect_font_characters(template: str, context: dict) -> frozenset[int]:
    """Keep every possible template label and every job text, without media."""
    characters = set(range(0x20, 0x7F))
    characters.update(map(ord, template))
    seen: set[int] = set()
    media_keys = {"src", "asset", "avatar", "images", "theme_art_src",
                  "font_face_src", "fallback_font_face_src"}

    def visit(value):
        if isinstance(value, str):
            characters.update(map(ord, value))
        elif isinstance(value, (dict, list, tuple)):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            if isinstance(value, dict):
                for key, child in value.items():
                    if key not in media_keys:
                        visit(child)
            else:
                for child in value:
                    visit(child)

    visit(context)
    return frozenset(characters)


def read_bundled_woff2(path: Path, expected_sha256: str = BUNDLED_FONT_SHA256) -> bytes:
    """Check the shipped resource before HTML delivery or decompression."""
    with Path(path).open("rb") as source:
        raw = source.read(_MAX_FONT_BYTES + 1)
    if (len(raw) > _MAX_FONT_BYTES or len(raw) < 48 or raw[:4] != b"wOF2"
            or hashlib.sha256(raw).hexdigest() != expected_sha256
            or not 0 < struct.unpack(">I", raw[16:20])[0] <= _MAX_FONT_BYTES):
        raise ValueError("内置霞鹜文楷字体损坏，请重新安装插件")
    return raw


@contextlib.contextmanager
def _process_lock(path: Path):
    """OS locks are released after crashes; no stale lock-file timeout needed."""
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()

            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + 60
        while True:
            try:
                acquire()
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待内置字体缓存锁超时") from error
                time.sleep(0.05)
        try:
            yield
        finally:
            release()


class BundledFontCache:
    def __init__(self, data_root: Path, source: Path | None = None,
                 source_sha256: str = BUNDLED_FONT_SHA256):
        if not re.fullmatch("[0-9a-f]{64}", source_sha256):
            raise ValueError("Invalid bundled font digest")
        self.source = Path(source) if source is not None else Path(__file__).parent / "templates" / "fonts" / BUNDLED_FONT_FILENAME
        self.source_sha256 = source_sha256
        # FontStore only owns files directly inside fonts/, so custom-font
        # upload/reset cannot remove a bundled font used by another render.
        self.root = Path(data_root) / "fonts" / "bundled"
        self.path = self.root / (source_sha256 + ".sfnt")
        self.manifest = self.path.with_suffix(".json")

    @staticmethod
    def _make_subset(source: Path, unicodes: frozenset[int]) -> bytes:
        from fontTools import subset
        from fontTools.ttLib import TTFont
        from fontTools.ttLib.woff2 import WOFF2FlavorData

        options = subset.Options()
        options.layout_features = ["*"]
        options.hinting = True
        options.glyph_names = True
        options.name_IDs = ["*"]
        options.name_languages = ["*"]
        options.notdef_outline = True
        options.recommended_glyphs = True
        with TTFont(source, lazy=True, recalcBBoxes=False, recalcTimestamp=False) as font:
            cmap = font.getBestCmap() or {}
            # HarfBuzz can compose/decompose canonically equivalent input (for
            # example e + acute -> é), and adds a dotted circle to isolated
            # combining marks. Keep those shaping inputs and output glyphs too.
            characters = set(unicodes)
            characters.add(0x25CC)
            for point in tuple(characters):
                characters.update(map(ord, unicodedata.normalize("NFD", chr(point))))
            characters.update(point for point in cmap
                              if set(map(ord, unicodedata.normalize("NFD", chr(point)))) <= characters)
            # Preserve every Unicode alias of a requested glyph; removing its
            # primary script mapping can change the auto-hinter's classification.
            glyphs = {cmap[point] for point in characters if point in cmap}
            aliases = {point for point, glyph in cmap.items() if glyph in glyphs}
            worker = subset.Subsetter(options=options)
            worker.populate(unicodes=characters | aliases)
            worker.subset(font)
            font.flavor = "woff2"
            font.flavorData = WOFF2FlavorData(transformedTables=set())
            output = io.BytesIO()
            font.save(output)
            return output.getvalue()

    def subset_woff2(self, unicodes: Iterable[int]) -> bytes:
        """Subset the complete source; cache only bounded bytes and hash keys."""
        global _SUBSET_CACHE_BYTES
        characters = frozenset(unicodes)
        if any(type(point) is not int or not 0 <= point <= 0x10FFFF for point in characters):
            raise ValueError("Invalid font character set")
        characters |= _HINT_CHARACTERS
        digest = hashlib.sha256(self.source_sha256.encode("ascii"))
        for point in sorted(characters):
            digest.update(struct.pack(">I", point))
        key = digest.hexdigest()
        with _SUBSET_LOCK:
            cached = _SUBSET_CACHE.get(key)
            if cached is not None:
                _SUBSET_CACHE.move_to_end(key)
                return cached
            raw = self._make_subset(self.sfnt_path(), characters)
            # Very large documents still retain their entire character set;
            # an over-budget result is returned but never kept in this LRU.
            if len(raw) <= _SUBSET_CACHE_MAX_BYTES:
                while _SUBSET_CACHE and (len(_SUBSET_CACHE) >= _SUBSET_CACHE_MAX_ENTRIES
                                        or _SUBSET_CACHE_BYTES + len(raw) > _SUBSET_CACHE_MAX_BYTES):
                    _, removed = _SUBSET_CACHE.popitem(last=False)
                    _SUBSET_CACHE_BYTES -= len(removed)
                _SUBSET_CACHE[key] = raw
                _SUBSET_CACHE_BYTES += len(raw)
            return raw

    def subset_for_context(self, template: str, context: dict) -> bytes:
        return self.subset_woff2(collect_font_characters(template, context))

    def _signature(self) -> tuple:
        return tuple((stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                     for stat in (self.path.stat(), self.manifest.stat()))

    def _valid_cache(self) -> bool:
        try:
            signature = self._signature()
            if not 0 < signature[0][0] <= _MAX_FONT_BYTES or not 0 < signature[1][0] <= 1024:
                return False
            meta = json.loads(self.manifest.read_text(encoding="utf-8"))
            if (not isinstance(meta, dict) or meta.get("version") != 1
                    or meta.get("source_sha256") != self.source_sha256
                    or type(meta.get("size")) is not int or meta["size"] != signature[0][0]
                    or not isinstance(meta.get("sha256"), str)
                    or not re.fullmatch("[0-9a-f]{64}", meta["sha256"])):
                return False
            with self.path.open("rb") as cached:
                raw = cached.read(_MAX_FONT_BYTES + 1)
            if (len(raw) != meta["size"] or raw[:4] not in {b"\0\1\0\0", b"OTTO", b"true"}
                    or hashlib.sha256(raw).hexdigest() != meta["sha256"]):
                return False
            # Recheck in case an external modification happened during hashing.
            return self._signature() == signature
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _decode(raw: bytes) -> bytes:
        try:
            from fontTools.ttLib import TTFont
            with TTFont(io.BytesIO(raw), recalcBBoxes=False, recalcTimestamp=False) as font:
                font.flavor = None
                output = io.BytesIO()
                font.save(output)
                sfnt = output.getvalue()
            if not 0 < len(sfnt) <= _MAX_FONT_BYTES:
                raise ValueError("内置字体解压大小无效")
            ImageFont.truetype(io.BytesIO(sfnt), 16).getmask("字体 Font")
            return sfnt
        except ImportError as error:
            raise RuntimeError("字体处理依赖尚未安装，请安装 requirements.txt 后重载插件") from error

    @staticmethod
    def _replace(target: Path, raw: bytes):
        descriptor, name = tempfile.mkstemp(prefix=target.name + ".tmp-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def sfnt_path(self) -> Path:
        """Decode at most once; restarts verify the cached bytes before reuse."""
        with _THREAD_LOCK:
            if self._valid_cache():
                return self.path
            self.root.mkdir(parents=True, exist_ok=True)
            with _process_lock(self.root / (self.source_sha256 + ".lock")):
                # A process killed during a write cannot run its finally block.
                # This hash's exclusive lock makes abandoned temp cleanup safe.
                for target in (self.path, self.manifest):
                    for temporary in self.root.glob(target.name + ".tmp-*"):
                        with contextlib.suppress(OSError):
                            temporary.unlink()
                # Another process may have finished while this one waited.
                if not self._valid_cache():
                    raw = read_bundled_woff2(self.source, self.source_sha256)
                    sfnt = self._decode(raw)
                    meta = {"version": 1, "source_sha256": self.source_sha256,
                            "sha256": hashlib.sha256(sfnt).hexdigest(), "size": len(sfnt)}
                    self._replace(self.path, sfnt)
                    self._replace(self.manifest, json.dumps(meta, sort_keys=True).encode("utf-8"))
            return self.path
