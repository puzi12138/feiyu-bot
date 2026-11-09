"""Validate and persist one user-supplied font independently of plugin updates."""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import io
import json
import os
import re
import struct
import time
import uuid
from pathlib import Path

from PIL import ImageFont

MAX_FONT_BYTES = 32 * 1024 * 1024
FONT_FORMATS = ('ttf', 'otf', 'woff', 'woff2')


class FontStore:
    def __init__(self, data_root: Path):
        self.root = Path(data_root) / 'fonts'
        self.manifest = self.root / 'custom.json'

    def load(self) -> tuple[dict, bytes] | None:
        for _ in range(2):
            try:
                meta = json.loads(self.manifest.read_text(encoding='utf-8'))
                if not isinstance(meta, dict) or meta.get('format') not in {'truetype', 'opentype'}:
                    return None
                digest = meta['sha256']
                if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                    return None
                path = self.root / (digest + '.sfnt')
                if path.stat().st_size > MAX_FONT_BYTES:
                    return None
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    return None
                return meta, raw
            except (OSError, ValueError, KeyError, TypeError):
                # Replacing/resetting a font may race a render snapshot.
                continue
        return None

    def info(self) -> dict:
        meta = {}
        try:
            candidate = json.loads(self.manifest.read_text(encoding='utf-8'))
            if not isinstance(candidate, dict) or candidate.get('format') not in {'truetype', 'opentype'}:
                raise ValueError('Invalid font metadata')
            digest = candidate.get('sha256', '')
            if re.fullmatch('[0-9a-f]{64}', digest) and 0 < (self.root / (digest + '.sfnt')).stat().st_size <= MAX_FONT_BYTES:
                meta = candidate
        except (OSError, ValueError, TypeError):
            pass
        return {'uploaded': bool(meta), 'name': meta.get('name', ''),
                'size': meta.get('size', 0), 'sha256': meta.get('sha256', ''),
                'max_bytes': MAX_FONT_BYTES, 'formats': list(FONT_FORMATS)}

    def upload(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError('请提交字体文件')
        name, encoded = payload.get('name'), payload.get('data')
        if not isinstance(name, str) or not isinstance(encoded, str):
            raise ValueError('字体名称或数据无效')
        name = name.replace('\\', '/').rsplit('/', 1)[-1]
        name = ''.join(c for c in name if c.isprintable())[:160]
        if not name or Path(name).suffix.lower().lstrip('.') not in FONT_FORMATS:
            raise ValueError('支持 TTF、OTF、WOFF 和 WOFF2 字体')
        if not encoded or len(encoded) > 4 * ((MAX_FONT_BYTES + 2) // 3):
            raise ValueError('字体文件须为 1 字节至 32 MB')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('字体数据无效，请重新选择文件') from None
        if not raw or len(raw) > MAX_FONT_BYTES:
            raise ValueError('字体文件须为 1 字节至 32 MB')
        return self._install(name, raw)

    def _install(self, name: str, raw: bytes) -> dict:
        if raw[:4] not in {b'\x00\x01\x00\x00', b'OTTO', b'true', b'wOFF', b'wOF2'}:
            raise ValueError('文件不是有效的字体，请选择 TTF、OTF、WOFF 或 WOFF2')
        if raw[:4] in {b'wOFF', b'wOF2'} and (len(raw) < 20 or struct.unpack('>I', raw[16:20])[0] > MAX_FONT_BYTES):
            raise ValueError('字体声明的解压大小超过 32 MB')
        try:
            from fontTools.ttLib import TTFont
            with TTFont(io.BytesIO(raw), recalcBBoxes=False, recalcTimestamp=False) as font:
                if not font.getBestCmap() or not 0 < font['maxp'].numGlyphs <= 65535:
                    raise ValueError('字体缺少有效字符表')
                font.flavor = None
                output = io.BytesIO()
                font.save(output)
                normalized = output.getvalue()
            if len(normalized) > MAX_FONT_BYTES:
                raise ValueError('字体解压后超过 32 MB，请使用较小字体')
            ImageFont.truetype(io.BytesIO(normalized), 16).getmask('字体 Font')
        except ImportError:
            raise ValueError('字体处理依赖尚未安装，请安装 requirements.txt 后重载插件') from None
        except ValueError:
            raise
        except Exception:
            raise ValueError('无法读取该字体，文件可能损坏或格式不受支持') from None
        digest = hashlib.sha256(normalized).hexdigest()
        meta = {'name': name, 'size': len(raw), 'sha256': digest,
                'format': 'opentype' if normalized[:4] == b'OTTO' else 'truetype'}
        self.root.mkdir(parents=True, exist_ok=True)
        self._replace(self.root / (digest + '.sfnt'), normalized)
        self._replace(self.manifest, json.dumps(meta, ensure_ascii=False).encode('utf-8'))
        for path in self.root.glob('*.sfnt'):
            if path.name != digest + '.sfnt':
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
        return self.info()

    def upload_chunk(self, payload: dict) -> dict:
        """Small JSON bodies also work with older dashboard request limits."""
        if not isinstance(payload, dict):
            raise ValueError('请提交字体分块')
        name, encoded = payload.get('name'), payload.get('data')
        total, offset = payload.get('total_size'), payload.get('offset')
        if (isinstance(total, bool) or not isinstance(total, int) or not 0 < total <= MAX_FONT_BYTES
                or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < total):
            raise ValueError('字体大小或分块偏移无效')
        if not isinstance(name, str) or not name or len(name) > 240 or Path(name.replace('\\','/')).suffix.lower().lstrip('.') not in FONT_FORMATS:
            raise ValueError('支持 TTF、OTF、WOFF 和 WOFF2 字体')
        name = ''.join(c for c in name.replace('\\','/').rsplit('/',1)[-1] if c.isprintable())[:160]
        if not isinstance(encoded, str) or not 0 < len(encoded) <= 1024 * 1024:
            raise ValueError('字体分块过大或为空，请重新上传')
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('字体分块数据无效') from None
        if not chunk or offset + len(chunk) > total:
            raise ValueError('字体分块超出声明大小')
        uploads = self.root / 'uploads'
        uploads.mkdir(parents=True, exist_ok=True)
        for path in uploads.iterdir():
            if path.is_file() and path.stat().st_mtime < time.time() - 900:
                with contextlib.suppress(OSError):
                    path.unlink()
        upload_id = payload.get('upload_id')
        if not upload_id:
            if offset != 0:
                raise ValueError('请从第一块重新上传字体')
            if len(list(uploads.glob('*.json'))) >= 4:
                raise ValueError('待完成的字体上传过多，请稍后再试')
            upload_id = uuid.uuid4().hex
            session = {'name': name, 'total_size': total, 'received': 0}
        else:
            if not isinstance(upload_id, str) or not re.fullmatch('[0-9a-f]{32}', upload_id):
                raise ValueError('上传会话无效')
            try:
                session = json.loads((uploads / (upload_id + '.json')).read_text())
            except (OSError, ValueError):
                raise ValueError('上传会话已过期，请重新选择字体') from None
            if session.get('name') != name or session.get('total_size') != total:
                raise ValueError('字体上传会话不匹配')
        part = uploads / (upload_id + '.part')
        manifest = uploads / (upload_id + '.json')
        if session['received'] != offset or (part.stat().st_size if part.exists() else 0) != offset:
            raise ValueError('字体分块顺序不一致，请重新上传')
        with part.open('ab') as output:
            output.write(chunk)
        received = offset + len(chunk)
        session['received'] = received
        self._replace(manifest, json.dumps(session,ensure_ascii=False).encode())
        if received < total:
            return {'complete': False, 'upload_id': upload_id, 'received': received}
        try:
            font = self._install(name, part.read_bytes())
            return {'complete': True, 'upload_id': upload_id, 'received': received, 'font': font}
        finally:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                manifest.unlink(missing_ok=True)

    @staticmethod
    def _replace(target: Path, raw: bytes):
        temporary = target.with_name(target.name + '.tmp-' + uuid.uuid4().hex)
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def reset(self) -> dict:
        self.manifest.unlink(missing_ok=True)
        for path in self.root.glob('*.sfnt'):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        for path in (self.root / 'uploads').glob('*'):
            if path.is_file():
                with contextlib.suppress(OSError):
                    path.unlink()
        return self.info()

    def restore(self, snapshot: tuple[dict, bytes] | None):
        if snapshot is None:
            self.reset()
            return
        meta, raw = snapshot
        self.root.mkdir(parents=True, exist_ok=True)
        self._replace(self.root / (meta['sha256'] + '.sfnt'), raw)
        self._replace(self.manifest, json.dumps(meta,ensure_ascii=False).encode())
        for path in self.root.glob('*.sfnt'):
            if path.stem != meta['sha256']:
                with contextlib.suppress(OSError):
                    path.unlink()
