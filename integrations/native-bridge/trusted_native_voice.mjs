/** Native PTT preparation for the protected, decoded 24 kHz producer spool.
 * No codec bootstrap, URL fetch, packet API, receive handler or send API.
 * Packet timing is valid only for our fixed 20 ms pysilk producer contract;
 * complete codec decoding and sample measurement happen in native_voice_spool.py.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { assertProtectedSpool, safeFile } from './trusted_native_manifest.mjs';

export const MAX_VOICE_BYTES = 16 * 1024 * 1024;
export const MAX_VOICE_DURATION_MS = 600000;
const MIME = 'audio/x-qq-silk', CODEC = 'silk_v3_tencent';
const HEADER = Buffer.from('\x02#!SILK_V3', 'ascii');
const SHA = /^[a-f0-9]{64}$/, MD5 = /^[a-f0-9]{32}$/;
const KEYS = ['index', 'type', 'sha256', 'md5', 'size', 'mime', 'duration_ms', 'sample_rate', 'channels', 'codec', 'packet_ms'];
const WAVE = Object.freeze([0, 18, 9, 23, 16, 17, 16, 15, 44, 17, 24, 20, 14, 15, 17]);
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const hash = (bytes, algorithm = 'sha256') => createHash(algorithm).update(bytes).digest('hex');
export class NativeVoiceError extends Error {
  constructor(code) { super(code); this.name = 'NativeVoiceError'; this.code = code; }
}
const fail = (code = 'native_voice_unverified') => { throw new NativeVoiceError(code); };

function local(file) {
  if (typeof file !== 'string' || !path.isAbsolute(file) || /^[/\\]{2}/.test(file)
      || /[\u0000-\u001f]/.test(file) || file.slice(path.parse(file).root.length).includes(':')
      || file.slice(path.parse(file).root.length).split(/[/\\]/).some(part => !part || part === '.' || part === '..')
      || process.platform === 'win32' && !/^[A-Za-z]:[/\\]/.test(file)) fail('native_voice_local_file_required');
  return path.resolve(file);
}

/** Check the entire fixed producer framing, never just a SILK magic substring. */
export function probeProducerSilk(bytes) {
  if (!Buffer.isBuffer(bytes) || bytes.length < HEADER.length + 3 || bytes.length > MAX_VOICE_BYTES
      || !bytes.subarray(0, HEADER.length).equals(HEADER)) fail('native_voice_invalid_silk');
  let position = HEADER.length, packets = 0;
  while (position < bytes.length) {
    if (position + 2 > bytes.length) fail('native_voice_invalid_silk');
    const length = bytes.readInt16LE(position); position += 2;
    if (length < 1 || length > 1250 || length > bytes.length - position) fail('native_voice_invalid_silk');
    position += length; packets++;
    if (packets * 20 > MAX_VOICE_DURATION_MS) fail('native_voice_duration_not_allowed');
  }
  if (!packets) fail('native_voice_invalid_silk');
  return { mime: MIME, duration_ms: packets * 20, sample_rate: 24000, channels: 1, codec: CODEC, packet_ms: 20 };
}

export function validateVoiceEvidence(parts) {
  if (!Array.isArray(parts) || parts.length !== 1) fail('native_voice_evidence_invalid');
  const part = parts[0];
  if (!exact(part, KEYS) || part.index !== 0 || part.type !== 'record'
      || typeof part.sha256 !== 'string' || !SHA.test(part.sha256) || typeof part.md5 !== 'string' || !MD5.test(part.md5)
      || !Number.isSafeInteger(part.size) || part.size < HEADER.length + 3 || part.size > MAX_VOICE_BYTES
      || part.mime !== MIME || part.codec !== CODEC || part.sample_rate !== 24000 || part.channels !== 1 || part.packet_ms !== 20
      || !Number.isSafeInteger(part.duration_ms) || part.duration_ms < 20 || part.duration_ms > MAX_VOICE_DURATION_MS
      || part.duration_ms % 20 !== 0) fail('native_voice_evidence_invalid');
  return parts;
}

async function verifyFile(file, expected) {
  file = local(file);
  const ancestors = [];
  let cursor = path.parse(file).root;
  for (const component of file.slice(cursor.length).split(path.sep)) {
    cursor = path.join(cursor, component);
    const observed = await fs.lstat(cursor);
    if (observed.isSymbolicLink() || cursor !== file && !observed.isDirectory()) fail('native_voice_file_changed');
    ancestors.push({ path: cursor, dev: observed.dev, ino: observed.ino });
  }
  const before = await fs.lstat(file);
  const bytes = await safeFile(file, MAX_VOICE_BYTES);
  // safeFile checks the opened inode and read stability. Repeat pathname/ancestor
  // identity checks because the native copy API later opens this same pathname.
  const after = await fs.lstat(file);
  if (before.isSymbolicLink() || !before.isFile() || before.nlink !== 1
      || before.ino !== after.ino || before.dev !== after.dev || before.size !== after.size
      || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs || after.nlink !== 1) fail('native_voice_file_changed');
  for (const ancestor of ancestors) {
    const observed = await fs.lstat(ancestor.path);
    if (observed.isSymbolicLink() || observed.dev !== ancestor.dev || observed.ino !== ancestor.ino
        || ancestor.path !== file && !observed.isDirectory()) fail('native_voice_file_changed');
  }
  const measured = probeProducerSilk(bytes);
  if (bytes.length !== expected.size || hash(bytes) !== expected.sha256 || hash(bytes, 'md5') !== expected.md5
      || Object.entries(measured).some(([key, value]) => expected[key] !== value)) fail('native_voice_payload_mismatch');
  return { index: 0, type: 'record', sha256: hash(bytes), md5: hash(bytes, 'md5'), size: bytes.length, ...measured };
}

/** Load one standalone voice manifest from the shared private immutable spool. */
export async function loadVoiceManifest(root, digest) {
  try {
    if (typeof digest !== 'string' || !SHA.test(digest)) fail();
    root = local(root);
    await assertProtectedSpool(root);
    const filename = path.join(root, 'manifests', digest + '.json');
    const bytes = await safeFile(filename, 4096);
    if (hash(bytes) !== digest) fail();
    const manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    if (!exact(manifest, ['schema', 'parts']) || manifest.schema !== 1 || !Array.isArray(manifest.parts)
        || manifest.parts.length !== 1) fail();
    const part = manifest.parts[0];
    if (!exact(part, ['index', 'type', 'file', 'sha256', 'size', 'mime', 'duration_ms', 'sample_rate', 'channels', 'codec', 'packet_ms'])
        || typeof part.sha256 !== 'string' || !SHA.test(part.sha256)
        || part.file !== `objects/${part.sha256}.silk`) fail();
    const file = path.join(root, 'objects', part.sha256 + '.silk');
    await assertProtectedSpool(root, [filename, file]);
    const content = await safeFile(file, MAX_VOICE_BYTES);
    const evidence = { ...part, md5: hash(content, 'md5') }; delete evidence.file;
    validateVoiceEvidence([evidence]);
    await verifyFile(file, evidence);
    await assertProtectedSpool(root, [filename, file]);
    return { manifest, manifest_sha256: digest, media_evidence: [evidence], verifiedRecord: { path: file, ...evidence } };
  } catch (error) {
    if (error instanceof NativeVoiceError) throw error;
    fail('native_voice_manifest_unverified');
  }
}

/** Copy previously decoded bytes into QQ's native media path and build PTT. */
export async function prepareNativeVoice(core, verified) {
  try {
    if (!exact(verified, ['path', ...KEYS])) fail('native_voice_evidence_invalid');
    const expected = { ...verified }; delete expected.path; validateVoiceEvidence([expected]);
    const sourcePath = local(verified.path);
    const source = await verifyFile(sourcePath, expected);
    if (typeof core?.apis?.FileApi?.uploadFile !== 'function') fail('native_voice_api_missing');
    const copied = await core.apis.FileApi.uploadFile(sourcePath, 4, 0);
    if (!copied || copied.md5 !== source.md5 || !['number', 'string', 'bigint'].includes(typeof copied.fileSize)
        || !/^\d{1,12}$/.test(String(copied.fileSize)) || Number(copied.fileSize) !== source.size
        || copied.fileName !== path.basename(sourcePath)) fail('native_voice_copy_unverified');
    const copyPath = local(copied.path);
    if ((process.platform === 'win32' ? copyPath.toLowerCase() : copyPath)
        === (process.platform === 'win32' ? sourcePath.toLowerCase() : sourcePath)) fail('native_voice_copy_unverified');
    await verifyFile(copyPath, source); await verifyFile(sourcePath, source);
    return {
      element: { elementType: 4, elementId: '', pttElement: {
        fileName: copied.fileName, filePath: copyPath, md5HexStr: source.md5, fileSize: String(source.size),
        duration: source.duration_ms / 1000, formatType: 1, voiceType: 1, voiceChangeType: 0, canConvert2Text: true,
        // Upstream static presentation placeholder; never claimed as audio proof.
        waveAmplitudes: [...WAVE], fileSubId: '', playState: 1, autoConvertText: 0, storeID: 0,
        otherBusinessInfo: { aiVoiceType: 0 },
      } },
      evidence: source,
    };
  } catch (error) {
    if (error instanceof NativeVoiceError) throw error;
    fail('native_voice_preparation_unavailable');
  }
}

export async function verifyPreparedNativeVoice(prepared) {
  if (!exact(prepared, ['element', 'evidence']) || !exact(prepared.element, ['elementType', 'elementId', 'pttElement'])
      || prepared.element.elementType !== 4 || prepared.element.elementId !== '') fail('native_voice_prepared_invalid');
  validateVoiceEvidence([prepared.evidence]);
  const ptt = prepared.element.pttElement, part = prepared.evidence;
  if (!exact(ptt, ['fileName', 'filePath', 'md5HexStr', 'fileSize', 'duration', 'formatType', 'voiceType', 'voiceChangeType',
    'canConvert2Text', 'waveAmplitudes', 'fileSubId', 'playState', 'autoConvertText', 'storeID', 'otherBusinessInfo'])
      || typeof ptt.fileName !== 'string' || ptt.fileName !== part.sha256 + '.silk'
      || ptt.md5HexStr !== part.md5 || ptt.fileSize !== String(part.size) || ptt.duration !== part.duration_ms / 1000
      || ptt.formatType !== 1 || ptt.voiceType !== 1 || ptt.voiceChangeType !== 0 || ptt.canConvert2Text !== true
      || JSON.stringify(ptt.waveAmplitudes) !== JSON.stringify(WAVE) || ptt.fileSubId !== '' || ptt.playState !== 1
      || ptt.autoConvertText !== 0 || ptt.storeID !== 0 || !exact(ptt.otherBusinessInfo, ['aiVoiceType'])
      || ptt.otherBusinessInfo.aiVoiceType !== 0) fail('native_voice_prepared_invalid');
  return verifyFile(ptt.filePath, part);
}

/** Compare the real raw native message with this exact standalone audio. */
export function orderedRawVoiceEvidence(raw, expected) {
  validateVoiceEvidence(expected);
  if (!raw || !Array.isArray(raw.elements) || raw.elements.length !== 1) fail('native_voice_raw_mismatch');
  const element = raw.elements[0], part = expected[0], ptt = element?.pttElement;
  if (!element || element.elementType !== 4 || !ptt || ['textElement', 'picElement', 'replyElement', 'arkElement', 'fileElement', 'videoElement']
    .some(key => element[key]) || typeof ptt.md5HexStr !== 'string' || ptt.md5HexStr.toLowerCase() !== part.md5
      || !['number', 'string', 'bigint'].includes(typeof ptt.fileSize) || !/^\d{1,12}$/.test(String(ptt.fileSize))
      || Number(ptt.fileSize) !== part.size || typeof ptt.duration !== 'number' || !Number.isFinite(ptt.duration)
      || Math.abs(ptt.duration * 1000 - part.duration_ms) > 0.001 || ptt.formatType !== 1 || ptt.voiceType !== 1) fail('native_voice_raw_mismatch');
  return expected;
}
