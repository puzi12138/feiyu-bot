/** Immutable local ordered-media manifest validation. Never fetches URLs. */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { probeImageBuffer, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, MAX_IMAGE_SIDE } from './trusted_native_media.mjs';

const SHA = /^[a-f0-9]{64}$/;
const MD5 = /^[a-f0-9]{32}$/;
const TYPES = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp' };
const hash = (bytes, algorithm = 'sha256') => createHash(algorithm).update(bytes).digest('hex');
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const fail = () => { throw new Error('native_media_manifest_unverified'); };

export async function safeFile(file, maximum) {
  const resolved = path.resolve(file), root = path.parse(resolved).root;
  if (!path.isAbsolute(file) || /^[/\\]{2}/.test(file) || resolved.slice(root.length).includes(':')) fail();
  let current = root;
  for (const item of resolved.slice(root.length).split(path.sep)) {
    if (!item || item === '.' || item === '..') fail();
    current = path.join(current, item);
    const stat = await fs.lstat(current);
    if (stat.isSymbolicLink() || current !== resolved && !stat.isDirectory()) fail();
  }
  const before = await fs.lstat(resolved);
  if (!before.isFile() || before.nlink !== 1 || before.size < 1 || before.size > maximum) fail();
  const handle = await fs.open(resolved, 'r');
  try {
    const opened = await handle.stat();
    if (opened.dev !== before.dev || opened.ino !== before.ino || opened.size !== before.size || opened.nlink !== 1) fail();
    const buffer = Buffer.alloc(opened.size + 1);
    let length = 0;
    while (length < buffer.length) {
      const result = await handle.read(buffer, length, buffer.length - length, length);
      if (!result.bytesRead) break;
      length += result.bytesRead;
    }
    const after = await handle.stat();
    if (length !== opened.size || after.size !== opened.size || after.mtimeMs !== opened.mtimeMs
        || after.ctimeMs !== opened.ctimeMs || after.nlink !== 1) fail();
    return buffer.subarray(0, length);
  } finally { await handle.close(); }
}

export async function assertProtectedSpool(root, files = []) {
  try {
    if (!path.isAbsolute(root) || /^[/\\]{2}/.test(root)) fail();
    const stat = await fs.lstat(root);
    if (!stat.isDirectory() || stat.isSymbolicLink()) fail();
    if (process.platform === 'win32') {
      const script = fileURLToPath(new URL('./Assert-NativeMediaSpool.ps1', import.meta.url));
      const { stdout } = await promisify(execFile)(
        'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script,
          '-LiteralRoot', root, '-ItemsBase64', Buffer.from(JSON.stringify(files)).toString('base64')],
        { timeout: 10000, maxBuffer: 2048, windowsHide: true },
      );
      if (JSON.parse(stdout.trim()).ok !== true) fail();
    } else {
      for (const item of [root, ...files]) {
        const observed = await fs.lstat(item);
        if (observed.isSymbolicLink() || observed.uid !== process.getuid() || (observed.mode & 0o077) !== 0) fail();
      }
    }
  } catch { fail(); }
}

export function validateMediaEvidence(parts) {
  if (!Array.isArray(parts) || parts.length < 1 || parts.length > 16) fail();
  let images = 0, totalImage = 0, totalText = 0;
  for (const [index, part] of parts.entries()) {
    if (!part || typeof part !== 'object' || part.index !== index || typeof part.sha256 !== 'string' || !SHA.test(part.sha256)) fail();
    if (part.type === 'text') {
      if (!exact(part, ['index', 'type', 'sha256', 'size']) || !Number.isSafeInteger(part.size) || part.size < 1 || part.size > 12000) fail();
      if (index && parts[index - 1].type === 'text') fail();
      totalText += part.size;
    } else if (part.type === 'image') {
      if (!exact(part, ['index', 'type', 'sha256', 'md5', 'size', 'mime', 'width', 'height'])
          || typeof part.md5 !== 'string' || !MD5.test(part.md5) || !Object.hasOwn(TYPES, part.mime)
          || !Number.isSafeInteger(part.size) || part.size < 10 || part.size > MAX_IMAGE_BYTES
          || !Number.isInteger(part.width) || !Number.isInteger(part.height)
          || part.width < 1 || part.height < 1 || part.width > MAX_IMAGE_SIDE || part.height > MAX_IMAGE_SIDE
          || part.width * part.height > MAX_IMAGE_PIXELS) fail();
      images++; totalImage += part.size;
    } else fail();
  }
  if (images < 1 || images > 6 || totalImage > 32 * 1024 * 1024 || totalText > 12000) fail();
  return parts;
}

export function orderedRawMediaEvidence(raw, expected) {
  validateMediaEvidence(expected);
  if (!Array.isArray(raw.elements) || raw.elements.length !== expected.length) fail();
  for (const [index, part] of expected.entries()) {
    const element = raw.elements[index];
    if (!element || Object.entries(element).some(([key, value]) => /Element$/.test(key)
        && !['textElement', 'picElement'].includes(key) && value != null)) fail();
    if (part.type === 'text') {
      if (element.elementType !== 1 || element.picElement || !element.textElement
          || element.textElement.atType !== 0 || typeof element.textElement.content !== 'string'
          || hash(element.textElement.content) !== part.sha256 || Buffer.byteLength(element.textElement.content) !== part.size) fail();
    } else {
      const pic = element.picElement;
      if (element.elementType !== 2 || element.textElement || !pic
          || typeof pic.md5HexStr !== 'string' || pic.md5HexStr.toLowerCase() !== part.md5
          || !['string', 'number', 'bigint'].includes(typeof pic.fileSize)
          || !/^\d{1,12}$/.test(String(pic.fileSize)) || Number(pic.fileSize) !== part.size
          || pic.picWidth !== part.width || pic.picHeight !== part.height
          || pic.picType !== (part.mime === 'image/gif' ? 2000 : 1000) || pic.picSubType !== 0) fail();
    }
  }
  return expected;
}

export async function loadMediaManifest(root, digest) {
  try {
    if (typeof digest !== 'string' || !SHA.test(digest)) fail();
    if (typeof root !== 'string' || !path.isAbsolute(root) || /^[/\\]{2}/.test(root)) fail();
    root = path.resolve(root);
    await assertProtectedSpool(root);
    const filename = path.join(root, 'manifests', digest + '.json');
    const bytes = await safeFile(filename, 32768);
    if (hash(bytes) !== digest) fail();
    const manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    if (!exact(manifest, ['schema', 'parts']) || manifest.schema !== 1
        || !Array.isArray(manifest.parts) || manifest.parts.length < 1 || manifest.parts.length > 16) fail();
    const verifiedImages = new Map(), evidence = [], files = [filename];
    let totalCharacters = 0;
    for (const [index, part] of manifest.parts.entries()) {
      if (!part || part.index !== index) fail();
      if (part.type === 'text') {
        if (!exact(part, ['index', 'type', 'text']) || typeof part.text !== 'string'
            || !part.text.length || [...part.text].length > 4000 || Buffer.byteLength(part.text) > 12000
            || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(part.text)
            || /\[CQ:/i.test(part.text) || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(part.text)) fail();
        totalCharacters += [...part.text].length;
        evidence.push({ index, type: 'text', sha256: hash(part.text), size: Buffer.byteLength(part.text) });
      } else if (part.type === 'image') {
        if (!exact(part, ['index', 'type', 'file', 'sha256', 'size', 'mime', 'width', 'height'])
            || typeof part.sha256 !== 'string' || !SHA.test(part.sha256)
            || !Object.hasOwn(TYPES, part.mime) || part.file !== `objects/${part.sha256}.${TYPES[part.mime]}`
            || !Number.isSafeInteger(part.size) || part.size < 10 || part.size > MAX_IMAGE_BYTES) fail();
        const file = path.join(root, 'objects', `${part.sha256}.${TYPES[part.mime]}`);
        const content = await safeFile(file, MAX_IMAGE_BYTES);
        const measured = probeImageBuffer(content);
        if (content.length !== part.size || hash(content) !== part.sha256
            || measured.mime !== part.mime || measured.width !== part.width || measured.height !== part.height) fail();
        const md5 = hash(content, 'md5');
        verifiedImages.set(index, { path: file, sha256: part.sha256, md5,
          size: part.size, mime: part.mime, width: part.width, height: part.height, index });
        evidence.push({ index, type: 'image', sha256: part.sha256, md5, size: part.size,
          mime: part.mime, width: part.width, height: part.height });
        files.push(file);
      } else fail();
    }
    if (totalCharacters > 4000) fail();
    validateMediaEvidence(evidence);
    await assertProtectedSpool(root, files);
    return { manifest, manifest_sha256: digest, verifiedImages, media_evidence: evidence };
  } catch { fail(); }
}
