/** Narrow native PIC preparation, copied from the observed Framework shape.
 * The caller verifies the protected spool manifest/root before using this
 * module. No URL, OneBot upload, packet API, codec or send API is exposed here.
 */
import { promises as fs } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';

export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
export const MAX_IMAGE_SIDE = 8192;
export const MAX_IMAGE_PIXELS = 32_000_000;
const SHA256 = /^[a-f0-9]{64}$/;
const MD5 = /^[a-f0-9]{32}$/;
const MIME = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
const fail = code => { throw new NativeMediaError(code); };
const digest = (bytes, algorithm = 'sha256') => createHash(algorithm).update(bytes).digest('hex');
const exactKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...keys].sort().join(',');

export class NativeMediaError extends Error {
  constructor(code) { super(code); this.name = 'NativeMediaError'; this.code = code; }
}

function dimensions(mime, width, height) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1
      || width > MAX_IMAGE_SIDE || height > MAX_IMAGE_SIDE || width * height > MAX_IMAGE_PIXELS) {
    fail('image_dimensions_not_allowed');
  }
  return { mime, width, height };
}

/** Measure the on-disk header, without any guessed 1024x1024 fallback.
 * Full decoder/frame validation remains the protected spool producer's job.
 */
export function probeImageBuffer(bytes) {
  if (!Buffer.isBuffer(bytes) || bytes.length < 10 || bytes.length > MAX_IMAGE_BYTES) fail('image_bytes_not_allowed');
  if (bytes.length >= 33 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
    if (bytes.readUInt32BE(8) !== 13 || bytes.toString('ascii', 12, 16) !== 'IHDR'
        || bytes[26] !== 0 || bytes[27] !== 0 || bytes[28] > 1) fail('invalid_png_header');
    return dimensions('image/png', bytes.readUInt32BE(16), bytes.readUInt32BE(20));
  }
  const signature = bytes.toString('ascii', 0, 6);
  if (bytes.length >= 14 && (signature === 'GIF87a' || signature === 'GIF89a')) {
    return dimensions('image/gif', bytes.readUInt16LE(6), bytes.readUInt16LE(8));
  }
  if (bytes.length >= 4 && bytes[0] === 255 && bytes[1] === 216) {
    let offset = 2;
    while (offset + 4 <= bytes.length) {
      if (bytes[offset] !== 255) fail('invalid_jpeg_header');
      while (bytes[offset] === 255) offset++;
      const marker = bytes[offset++];
      if (marker === 0xd8 || marker === 0x01 || marker >= 0xd0 && marker <= 0xd7) continue;
      if (marker === 0xd9 || marker === 0xda || offset + 2 > bytes.length) break;
      const length = bytes.readUInt16BE(offset);
      if (length < 2 || offset + length > bytes.length) fail('invalid_jpeg_header');
      if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker)) {
        if (length < 8 || bytes[offset + 7] < 1 || length !== 8 + 3 * bytes[offset + 7]) fail('invalid_jpeg_header');
        return dimensions('image/jpeg', bytes.readUInt16BE(offset + 5), bytes.readUInt16BE(offset + 3));
      }
      offset += length;
    }
    fail('invalid_jpeg_header');
  }
  if (bytes.length >= 20 && bytes.toString('ascii', 0, 4) === 'RIFF' && bytes.toString('ascii', 8, 12) === 'WEBP') {
    if (bytes.readUInt32LE(4) + 8 !== bytes.length) fail('invalid_webp_header');
    let offset = 12;
    while (offset + 8 <= bytes.length) {
      const type = bytes.toString('ascii', offset, offset + 4);
      const length = bytes.readUInt32LE(offset + 4);
      const start = offset + 8;
      if (length > bytes.length - start) fail('invalid_webp_header');
      if (type === 'VP8X') {
        if (length !== 10) fail('invalid_webp_header');
        return dimensions('image/webp', 1 + bytes.readUIntLE(start + 4, 3), 1 + bytes.readUIntLE(start + 7, 3));
      }
      if (type === 'VP8L') {
        if (length < 5 || bytes[start] !== 0x2f) fail('invalid_webp_header');
        const bits = bytes.readUInt32LE(start + 1);
        return dimensions('image/webp', 1 + (bits & 0x3fff), 1 + (bits >>> 14 & 0x3fff));
      }
      if (type === 'VP8 ') {
        if (length < 10 || (bytes[start] & 1) !== 0
            || !bytes.subarray(start + 3, start + 6).equals(Buffer.from([0x9d, 0x01, 0x2a]))) fail('invalid_webp_header');
        return dimensions('image/webp', bytes.readUInt16LE(start + 6) & 0x3fff, bytes.readUInt16LE(start + 8) & 0x3fff);
      }
      offset = start + length + (length & 1);
    }
    fail('invalid_webp_header');
  }
  fail('image_type_not_allowed');
}

function localAbsolute(file) {
  if (typeof file !== 'string' || !path.isAbsolute(file)
      || /^[/\\]{2}/.test(file) || /[\u0000-\u001f]/.test(file)
      || (process.platform === 'win32' && !/^[A-Za-z]:[/\\]/.test(file))
      || file.slice(path.parse(file).root.length).includes(':')
      || file.slice(path.parse(file).root.length).split(/[/\\]/).some(part => !part || part === '.' || part === '..')) fail('image_local_file_required');
  return path.resolve(file);
}

async function readVerifiedLocal(file, expected) {
  file = localAbsolute(file);
  // Reject symlink/junction ancestors as well as hard-linked leaf files. The
  // caller also owns the protected spool ACL; we never alter QQ cache ACLs.
  const root = path.parse(file).root;
  let current = root;
  const ancestors = [];
  for (const part of file.slice(root.length).split(path.sep)) {
    if (!part || part === '.' || part === '..') fail('image_local_file_required');
    current = path.join(current, part);
    const stat = await fs.lstat(current);
    if (stat.isSymbolicLink() || current !== file && !stat.isDirectory()) fail('image_reparse_path_not_allowed');
    ancestors.push({ path: current, dev: stat.dev, ino: stat.ino });
  }
  const before = await fs.lstat(file);
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size !== expected.size) fail('image_file_changed');
  const handle = await fs.open(file, 'r');
  try {
    const opened = await handle.stat();
    if (opened.dev !== before.dev || opened.ino !== before.ino || opened.size !== before.size
        || opened.nlink !== 1 || !opened.isFile()) fail('image_file_changed');
    const bytes = Buffer.alloc(expected.size + 1);
    let offset = 0;
    while (offset < bytes.length) {
      const read = await handle.read(bytes, offset, bytes.length - offset, offset);
      if (read.bytesRead === 0) break;
      offset += read.bytesRead;
    }
    const after = await handle.stat();
    if (offset !== expected.size || after.size !== opened.size || after.dev !== opened.dev || after.ino !== opened.ino
        || after.mtimeMs !== opened.mtimeMs || after.ctimeMs !== opened.ctimeMs || after.nlink !== 1) fail('image_file_changed');
    // fstat alone would miss a rename-replacement at the name native QQ will
    // later open. Recheck every ancestor and the final pathname as well.
    for (const ancestor of ancestors) {
      const observed = await fs.lstat(ancestor.path);
      if (observed.isSymbolicLink() || observed.dev !== ancestor.dev || observed.ino !== ancestor.ino
          || ancestor.path !== file && !observed.isDirectory()) fail('image_file_changed');
      if (ancestor.path === file && (observed.dev !== opened.dev || observed.ino !== opened.ino
          || observed.size !== opened.size || observed.nlink !== 1 || !observed.isFile())) fail('image_file_changed');
    }
    const content = bytes.subarray(0, offset);
    if (digest(content) !== expected.sha256) fail('image_sha256_mismatch');
    const measured = probeImageBuffer(content);
    if (measured.mime !== expected.mime || measured.width !== expected.width || measured.height !== expected.height) fail('image_metadata_mismatch');
    return { ...measured, sha256: expected.sha256, md5: digest(content, 'md5'), size: offset };
  } finally { await handle.close(); }
}

function validateVerified(value) {
  if (!exactKeys(value, ['path', 'sha256', 'size', 'mime', 'width', 'height', 'index'])
      || typeof value.sha256 !== 'string' || !SHA256.test(value.sha256)
      || !Number.isInteger(value.size) || value.size < 10 || value.size > MAX_IMAGE_BYTES
      || !MIME.has(value.mime) || !Number.isInteger(value.index) || value.index < 0 || value.index > 15) fail('invalid_verified_image');
  dimensions(value.mime, value.width, value.height);
  localAbsolute(value.path);
}

/** Prepare only a previously validated immutable spool object using real QQ APIs.
 * The caller reserves the stable complete message operation before invoking it.
 */
export async function prepareNativeImage(core, verified) {
  try {
    validateVerified(verified);
    if (typeof core?.apis?.FileApi?.uploadFile !== 'function') fail('native_image_api_missing');
    const source = await readVerifiedLocal(verified.path, verified);
    const uploaded = await core.apis.FileApi.uploadFile(verified.path, 2, 0);
    if (!uploaded || typeof uploaded !== 'object' || typeof uploaded.md5 !== 'string' || !MD5.test(uploaded.md5)
        || uploaded.md5 !== source.md5 || !['number', 'string', 'bigint'].includes(typeof uploaded.fileSize)
        || !/^\d{1,12}$/.test(String(uploaded.fileSize)) || Number(uploaded.fileSize) !== verified.size
        || typeof uploaded.fileName !== 'string' || uploaded.fileName !== path.basename(verified.path)
        || typeof uploaded.path !== 'string') fail('native_image_copy_unverified');
    const copyPath = localAbsolute(uploaded.path), sourcePath = localAbsolute(verified.path);
    if ((process.platform === 'win32' ? copyPath.toLowerCase() : copyPath)
        === (process.platform === 'win32' ? sourcePath.toLowerCase() : sourcePath)) fail('native_image_copy_unverified');
    const copy = await readVerifiedLocal(copyPath, verified);
    const secondSource = await readVerifiedLocal(verified.path, verified);
    if (copy.md5 !== source.md5 || secondSource.md5 !== source.md5) fail('native_image_copy_unverified');
    const result = {
      element: {
        elementType: 2,
        elementId: '',
        picElement: {
          md5HexStr: source.md5,
          fileSize: String(verified.size),
          picWidth: verified.width,
          picHeight: verified.height,
          fileName: uploaded.fileName,
          sourcePath: copyPath,
          original: true,
          // Match upstream getFileTypeForSendType rather than invent an enum.
          picType: verified.mime === 'image/gif' ? 2000 : 1000,
          picSubType: 0,
          fileUuid: '',
          fileSubId: '',
          thumbFileSize: 0,
          summary: '',
        },
      },
      evidence: { index: verified.index, sha256: source.sha256, md5: source.md5,
        size: source.size, mime: source.mime, width: source.width, height: source.height },
    };
    return result;
  } catch (error) {
    if (error instanceof NativeMediaError) throw error;
    fail('native_image_preparation_unavailable');
  }
}

/** Revalidate the actual QQ copy immediately before the one native send call. */
export async function verifyPreparedNativeImage(prepared) {
  try {
    if (!exactKeys(prepared, ['element', 'evidence'])
        || !exactKeys(prepared.evidence, ['index', 'sha256', 'md5', 'size', 'mime', 'width', 'height'])
        || !exactKeys(prepared.element, ['elementType', 'elementId', 'picElement'])
        || !MD5.test(prepared.evidence.md5) || prepared.element?.elementType !== 2
        || prepared.element?.elementId !== '') fail('invalid_prepared_image');
    const pic = prepared.element.picElement;
    if (!exactKeys(pic, ['md5HexStr', 'fileSize', 'picWidth', 'picHeight', 'fileName', 'sourcePath', 'original',
      'picType', 'picSubType', 'fileUuid', 'fileSubId', 'thumbFileSize', 'summary'])
        || typeof pic.fileName !== 'string' || pic.fileName.length < 1 || pic.fileName.length > 128
        || /[/\\:\u0000-\u001f]/.test(pic.fileName) || pic.fileUuid !== '' || pic.fileSubId !== ''
        || pic.thumbFileSize !== 0 || pic.summary !== '') fail('invalid_prepared_image');
    const expected = { path: pic?.sourcePath, ...prepared.evidence };
    delete expected.md5;
    validateVerified(expected);
    if (pic.md5HexStr !== prepared.evidence.md5 || pic.fileSize !== String(expected.size)
        || pic.picWidth !== expected.width || pic.picHeight !== expected.height
        || pic.original !== true || pic.picSubType !== 0
        || pic.picType !== (expected.mime === 'image/gif' ? 2000 : 1000)) fail('invalid_prepared_image');
    const actual = await readVerifiedLocal(pic.sourcePath, expected);
    if (actual.md5 !== prepared.evidence.md5) fail('native_image_copy_unverified');
    return { index: expected.index, ...actual };
  } catch (error) {
    if (error instanceof NativeMediaError) throw error;
    fail('native_image_preparation_unavailable');
  }
}
