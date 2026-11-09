/** Disabled, send-only Framework pilot using its real QQ session.
 * No OneBot short ID, packet hook, database access, or receive subscription.
 * Only owner-private plain text is supported. All errors are fixed codes.
 */
import http from 'node:http';
import { createHash, timingSafeEqual, randomBytes } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadMediaManifest, validateMediaEvidence, orderedRawMediaEvidence } from './trusted_native_manifest.mjs';
import { prepareNativeImage, verifyPreparedNativeImage } from './trusted_native_media.mjs';
import { loadVoiceManifest, validateVoiceEvidence, prepareNativeVoice, verifyPreparedNativeVoice, orderedRawVoiceEvidence } from './trusted_native_voice.mjs';

const OWNER = '0';
const SELF = '0';
const MAX_BODY = 16384;
const SELF_KEYS = new Set(['enabled', 'expected_self_id', 'private_target', 'port', 'token', 'receipt_dir']);
const OP = /^[A-Za-z0-9_.:/-]{1,160}$/;
const DECIMAL = /^\d{1,24}$/;
const UID = /^u_[A-Za-z0-9_-]{5,100}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const hash = value => createHash('sha256').update(value).digest('hex');
const fail = code => { throw new BridgeError(code); };
const decimalValue = value => (typeof value === 'string' || (typeof value === 'number' && Number.isSafeInteger(value))) && DECIMAL.test(String(value));
const sameFields = (left, right) => left && right && exactKeys(left, Object.keys(right))
  && Object.keys(right).every(key => left[key] === right[key]);

async function bounded(callback, timeout) {
  let timer;
  try {
    return await Promise.race([Promise.resolve().then(callback), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new BridgeError('native_call_timeout')), timeout);
    })]);
  } finally { clearTimeout(timer); }
}

export class BridgeError extends Error {
  constructor(code) { super(code); this.name = 'BridgeError'; this.code = code; }
}

function exactKeys(value, keys) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
}

export function validateConfig(value) {
  if (!value || Object.keys(value).some(key => !SELF_KEYS.has(key)) || typeof value.enabled !== 'boolean') fail('invalid_bridge_config');
  // Disabled truly means no account inspection, path creation, or listener.
  if (!value.enabled) return { enabled: false };
  if (value.expected_self_id !== SELF || value.private_target !== OWNER
      || !Number.isInteger(value.port) || value.port < 1024 || value.port > 65535
      || typeof value.token !== 'string' || !/^[A-Za-z0-9_-]{43,128}$/.test(value.token)
      || typeof value.receipt_dir !== 'string' || !path.isAbsolute(value.receipt_dir)) fail('invalid_bridge_config');
  return { ...value };
}

function validateRequest(value) {
  if (!exactKeys(value, ['operation_id', 'target_id', 'text']) || typeof value.operation_id !== 'string' || !OP.test(value.operation_id)
      || value.target_id !== OWNER || typeof value.text !== 'string'
      || value.text.length < 1 || value.text.length > 4000 || Buffer.byteLength(value.text) > 12000
      || /\[CQ:/i.test(value.text) || /[\u0000\u0001-\u0008\u000b\u000c\u000e-\u001f]/.test(value.text)) fail('unsupported_plain_text_request');
  return value;
}

function rawText(raw) {
  if (!Array.isArray(raw.elements) || raw.elements.length < 1 || raw.elements.length > 64) fail('raw_text_evidence_missing');
  let content = '';
  for (const element of raw.elements) {
    if (element.elementType !== 1 || !element.textElement || element.textElement.atType !== 0
        || typeof element.textElement.content !== 'string'
        || ['picElement', 'replyElement', 'arkElement', 'pttElement', 'fileElement'].some(k => element[k])) fail('raw_text_evidence_missing');
    content += element.textElement.content;
  }
  return content;
}

function validateReceipt(value, operation) {
  const keys = ['schema', 'operation_id', 'self_id', 'target_id', 'peer', 'fingerprint',
    'payload_sha256', 'reserved_at', 'state', 'native_identity'];
  if (value?.schema === 2) keys.push('media_evidence');
  if (!exactKeys(value, keys) || ![1, 2].includes(value.schema)
      || value.operation_id !== operation || typeof operation !== 'string' || !OP.test(operation) || value.self_id !== SELF || value.target_id !== OWNER
      || !exactKeys(value.peer, ['chatType', 'peerUid', 'guildId']) || value.peer.chatType !== 1
      || typeof value.peer.peerUid !== 'string' || !UID.test(value.peer.peerUid) || value.peer.guildId !== ''
      || typeof value.fingerprint !== 'string' || !DIGEST.test(value.fingerprint)
      || typeof value.payload_sha256 !== 'string' || !DIGEST.test(value.payload_sha256)
      || !Number.isSafeInteger(value.reserved_at) || value.reserved_at < 1
      || !['pending', 'unknown', 'acknowledged', 'confirmed'].includes(value.state)) fail('receipt_store_unavailable');
  if (value.schema === 2) {
    try {
      if (value.media_evidence?.[0]?.type === 'record') validateVoiceEvidence(value.media_evidence);
      else validateMediaEvidence(value.media_evidence);
    } catch { fail('receipt_store_unavailable'); }
  }
  const identity = value.native_identity;
  if (identity === null) {
    if (!['pending', 'unknown'].includes(value.state)) fail('receipt_store_unavailable');
  } else if (!exactKeys(identity, ['chat_type', 'peer_uid', 'peer_uin', 'self_uin', 'msg_id', 'msg_seq', 'msg_time', 'msg_random'])
      || !['acknowledged', 'confirmed'].includes(value.state) || identity.chat_type !== 1
      || identity.peer_uid !== value.peer.peerUid || identity.peer_uin !== OWNER || identity.self_uin !== SELF
      || typeof identity.msg_id !== 'string' || !DECIMAL.test(identity.msg_id) || BigInt(identity.msg_id) < 1n
      || typeof identity.msg_seq !== 'string' || !DECIMAL.test(identity.msg_seq) || BigInt(identity.msg_seq) < 1n
      || !Number.isSafeInteger(identity.msg_time) || identity.msg_time < value.reserved_at - 5 || identity.msg_time > value.reserved_at + 300
      || !(identity.msg_random === null || (typeof identity.msg_random === 'string' && /^-?\d{1,24}$/.test(identity.msg_random)))) fail('receipt_store_unavailable');
  return value;
}

export class JsonReceiptStore {
  constructor(directory) { this.directory = path.resolve(directory); }
  async initialize() {
    await fs.mkdir(this.directory, { recursive: true });
    const stat = await fs.lstat(this.directory);
    if (stat.isSymbolicLink() || !stat.isDirectory()) fail('receipt_store_unavailable');
    // Never guess whether a stale process sent a message. Crash-left locks need
    // manual review; an active or stale lock blocks a second bridge instance.
    this.lockPath = path.join(this.directory, '.bridge-lock');
    this.lockValue = JSON.stringify({ pid: process.pid, nonce: randomBytes(16).toString('hex') });
    try {
      this.lockHandle = await fs.open(this.lockPath, 'wx', 0o600);
      await this.lockHandle.writeFile(this.lockValue); await this.lockHandle.sync();
    } catch { await this.lockHandle?.close().catch(() => {}); this.lockHandle = null; fail('receipt_store_locked'); }
  }
  async close() {
    if (!this.lockHandle) return;
    await this.lockHandle.close(); this.lockHandle = null;
    try { if (await fs.readFile(this.lockPath, 'utf8') === this.lockValue) await fs.unlink(this.lockPath); } catch {}
  }
  filename(operation) { return path.join(this.directory, hash(operation) + '.json'); }
  async get(operation) {
    if (!this.lockHandle) fail('receipt_store_unavailable');
    const filename = this.filename(operation);
    try {
      const stat = await fs.lstat(filename);
      if (stat.isSymbolicLink() || !stat.isFile() || stat.size > 8192) fail('receipt_store_unavailable');
      const value = JSON.parse(await fs.readFile(filename, 'utf8'));
      return validateReceipt(value, operation);
    } catch (error) {
      if (error.code === 'ENOENT') return null;
      fail('receipt_store_unavailable');
    }
  }
  async put(value) {
    if (!this.lockHandle) fail('receipt_store_unavailable');
    validateReceipt(value, value.operation_id);
    const filename = this.filename(value.operation_id);
    const temporary = path.join(this.directory, '.pending-' + randomBytes(12).toString('hex'));
    let handle;
    try {
      // Sync file before it becomes the durable dispatch intent. Files contain
      // identities/digests only, never text, image paths, or authentication.
      handle = await fs.open(temporary, 'wx', 0o600);
      await handle.writeFile(JSON.stringify(value));
      await handle.sync();
      await handle.close(); handle = null;
      await fs.rename(temporary, filename);
    } catch {
      fail('receipt_store_unavailable');
    } finally {
      await handle?.close().catch(() => {});
      await fs.unlink(temporary).catch(() => {});
    }
  }
}

export class TrustedNativeEngine {
  constructor(core, config, store, clock = () => Math.floor(Date.now() / 1000), nativeTimeout = 12000, media = {}) {
    this.core = core; this.config = validateConfig(config); this.store = store; this.clock = clock;
    if (!Number.isInteger(nativeTimeout) || nativeTimeout < 1 || nativeTimeout > 12000) fail('invalid_native_timeout');
    this.nativeTimeout = nativeTimeout;
    this.tail = Promise.resolve(); this.activeNativeSends = 0; this.activeNativePreparations = 0;
    this.loadManifest = media.loadManifest || (digest => loadMediaManifest(path.join(this.config.receipt_dir, 'media-spool'), digest));
    this.prepareImage = media.prepareImage || prepareNativeImage;
    this.verifyPrepared = media.verifyPrepared || verifyPreparedNativeImage;
    this.loadVoice = media.loadVoice || (digest => loadVoiceManifest(path.join(this.config.receipt_dir, 'media-spool'), digest));
    this.prepareVoice = media.prepareVoice || prepareNativeVoice;
    this.verifyVoice = media.verifyVoice || verifyPreparedNativeVoice;
  }
  serial(callback) {
    const pending = this.tail.then(callback);
    this.tail = pending.catch(() => {});
    return pending;
  }
  async identity() {
    if (!this.config.enabled) fail('bridge_disabled');
    const self = this.core?.selfInfo;
    if (!self || String(self.uin) !== SELF || !UID.test(String(self.uid)) || self.online !== true) fail('native_account_not_ready');
    if (typeof this.core?.apis?.MsgApi?.sendMsg !== 'function'
        || typeof this.core?.apis?.MsgApi?.getMsgsByMsgId !== 'function'
        || typeof this.core?.apis?.FriendApi?.isBuddy !== 'function'
        || typeof this.core?.apis?.UserApi?.getUidByUinV2 !== 'function'
        || typeof this.core?.apis?.UserApi?.getUinByUidV2 !== 'function') fail('native_api_missing');
    return { self_id: SELF, online: true, transport: 'framework_real_session', receive_subscriptions: 0, supported: 'owner_private_plain_text',
      protocol_version: 2, media_manifest_schema: 1,
      capabilities: ['owner_private_plain_text', ...(typeof this.core?.apis?.FileApi?.uploadFile === 'function' ? ['owner_private_ordered_text_image', 'owner_private_prepared_silk_backend'] : [])],
      enabled_voice: false };
  }
  async resolvePeer() {
    const uid = await bounded(() => this.core.apis.UserApi.getUidByUinV2(OWNER), this.nativeTimeout);
    if (typeof uid !== 'string' || !UID.test(uid)
        || String(await bounded(() => this.core.apis.UserApi.getUinByUidV2(uid), this.nativeTimeout)) !== OWNER) fail('native_peer_unverified');
    if (await bounded(() => this.core.apis.FriendApi.isBuddy(uid), this.nativeTimeout) !== true) fail('native_friend_unverified');
    return { chatType: 1, peerUid: uid, guildId: '' };
  }
  async rawIdentity(raw, record) {
    if (!raw || typeof raw !== 'object' || raw.chatType !== 1 || raw.peerUid !== record.peer.peerUid
        || String(raw.senderUin) !== SELF || raw.senderUid !== this.core.selfInfo.uid
        || raw.sendStatus !== 2 || !decimalValue(raw.msgId) || BigInt(raw.msgId) === 0n
        || !decimalValue(raw.msgSeq) || BigInt(raw.msgSeq) < 1n
        || !decimalValue(raw.msgTime)) fail('native_raw_identity_unverified');
    if (raw.peerUin != null && String(raw.peerUin) !== OWNER) fail('native_raw_peer_mismatch');
    if (String(await bounded(() => this.core.apis.UserApi.getUinByUidV2(raw.peerUid), this.nativeTimeout)) !== OWNER) fail('native_raw_peer_mismatch');
    const timestamp = Number(raw.msgTime);
    if (!Number.isSafeInteger(timestamp) || timestamp < record.reserved_at - 5 || timestamp > record.reserved_at + 300) fail('native_raw_time_mismatch');
    const identity = {
      chat_type: 1, peer_uid: raw.peerUid, peer_uin: OWNER, self_uin: SELF,
      msg_id: String(raw.msgId), msg_seq: String(raw.msgSeq), msg_time: timestamp,
      // Random is additional native evidence, never invented when omitted.
      msg_random: raw.msgRandom == null ? null : String(raw.msgRandom),
    };
    if (identity.msg_random !== null && (!/^-?\d{1,24}$/.test(identity.msg_random)
        || !(typeof raw.msgRandom === 'string' || (typeof raw.msgRandom === 'number' && Number.isSafeInteger(raw.msgRandom))))) fail('native_raw_identity_unverified');
    return identity;
  }
  async readVerified(record) {
    if (!record.native_identity) fail('native_outcome_unknown_do_not_resend');
    await this.identity();
    const result = await bounded(() => this.core.apis.MsgApi.getMsgsByMsgId({ ...record.peer }, [record.native_identity.msg_id]), this.nativeTimeout);
    const list = result?.msgList;
    if (!Array.isArray(list)) fail('native_readback_unavailable');
    const matches = list.filter(raw => String(raw.msgId) === record.native_identity.msg_id);
    if (matches.length !== 1) fail('native_readback_unavailable');
    const raw = matches[0];
    const observed = await this.rawIdentity(raw, record);
    if (JSON.stringify(observed) !== JSON.stringify(record.native_identity)) fail('native_identity_changed');
    if (record.schema === 2) {
      try {
        if (record.media_evidence[0].type === 'record') orderedRawVoiceEvidence(raw, record.media_evidence);
        else orderedRawMediaEvidence(raw, record.media_evidence);
      } catch { fail('native_media_payload_mismatch'); }
    } else {
      const text = rawText(raw);
      if (hash(text) !== record.payload_sha256) fail('native_payload_mismatch');
    }
    record.state = 'confirmed';
    await this.store.put(record);
    return { state: 'confirmed', operation_id: record.operation_id, native_identity: observed,
      payload_sha256: record.payload_sha256, evidence: 'native_kernel_getMsgsByMsgId',
      desktop_display_verified: false, snow_history_integrated: false,
      ...(record.schema === 2 ? { media_evidence: structuredClone(record.media_evidence),
        payload_evidence: record.media_evidence[0].type === 'record'
          ? 'native_ptt_md5_size_duration_local_sha256_fixed20ms_decoded_producer'
          : 'ordered_native_text_sha256_image_md5_size_dimensions_local_sha256' } : {}) };
  }
  async sendText(request) {
    if (!this.config.enabled) fail('bridge_disabled');
    validateRequest(request);
    return this.serial(async () => {
      const fingerprint = hash(JSON.stringify([SELF, OWNER, request.text]));
      const old = await this.store.get(request.operation_id);
      if (old) {
        if (old.fingerprint !== fingerprint) fail('operation_reused_with_different_content');
        // Existing operations are ONLY read; pending/unknown never dispatch.
        return this.readVerified(old);
      }
      await this.identity();
      const peer = await this.resolvePeer();
      const record = { schema: 1, operation_id: request.operation_id, self_id: SELF, target_id: OWNER,
        peer: { ...peer }, fingerprint, payload_sha256: hash(request.text),
        reserved_at: this.clock(), state: 'pending', native_identity: null };
      await this.store.put(record);
      try {
        // Exact converter shape and enum values observed in v4.18.41 source.
        const raw = await bounded(() => {
          this.activeNativeSends++;
          return Promise.resolve().then(() => this.core.apis.MsgApi.sendMsg({ ...peer }, [{ elementType: 1, elementId: '',
            textElement: { content: request.text, atType: 0, atUid: '', atTinyId: '', atNtUid: '' } }], 10000))
            .finally(() => { this.activeNativeSends--; });
        }, this.nativeTimeout);
        record.native_identity = await this.rawIdentity(raw, record);
        record.state = 'acknowledged';
        await this.store.put(record);
        return await this.readVerified(record);
      } catch {
        record.state = record.native_identity ? 'acknowledged' : 'unknown';
        await this.store.put(record);
        fail('native_outcome_unknown_do_not_resend');
      }
    });
  }
  async readOperation(request) {
    if (!this.config.enabled) fail('bridge_disabled');
    if (!exactKeys(request, ['operation_id']) || typeof request.operation_id !== 'string' || !OP.test(request.operation_id)) fail('invalid_operation');
    return this.serial(async () => {
      const record = await this.store.get(request.operation_id);
      if (!record) fail('unknown_operation');
      return this.readVerified(record);
    });
  }

  async sendMessage(request) {
    if (!this.config.enabled) fail('bridge_disabled');
    if (!exactKeys(request, ['operation_id', 'target_id', 'manifest_sha256'])
        || typeof request.operation_id !== 'string' || !OP.test(request.operation_id)
        || request.target_id !== OWNER || typeof request.manifest_sha256 !== 'string'
        || !DIGEST.test(request.manifest_sha256)) fail('unsupported_media_request');
    return this.serial(async () => {
      const fingerprint = hash(JSON.stringify([SELF, OWNER, 'message/v1', request.manifest_sha256]));
      const old = await this.store.get(request.operation_id);
      if (old) {
        if (old.fingerprint !== fingerprint || old.schema !== 2) fail('operation_reused_with_different_content');
        return this.readVerified(old);
      }
      const status = await this.identity();
      if (!status.capabilities.includes('owner_private_ordered_text_image')) fail('native_image_api_missing');
      let loaded;
      try { loaded = await this.loadManifest(request.manifest_sha256); }
      catch { fail('native_media_manifest_unverified'); }
      if (!loaded || loaded.manifest_sha256 !== request.manifest_sha256 || loaded.manifest?.schema !== 1
          || !Array.isArray(loaded.manifest.parts) || !(loaded.verifiedImages instanceof Map)) fail('native_media_manifest_unverified');
      const evidence = loaded.manifest.parts.map((part, index) => {
        if (part.index !== index) fail('native_media_manifest_unverified');
        if (part.type === 'text') return { index, type: 'text', sha256: hash(part.text), size: Buffer.byteLength(part.text) };
        const verified = loaded.verifiedImages.get(index);
        if (!verified || verified.index !== index || verified.sha256 !== part.sha256 || verified.size !== part.size
            || verified.mime !== part.mime || verified.width !== part.width || verified.height !== part.height) fail('native_media_manifest_unverified');
        return { index, type: 'image', sha256: verified.sha256, md5: verified.md5, size: verified.size,
          mime: verified.mime, width: verified.width, height: verified.height };
      });
      try { validateMediaEvidence(evidence); } catch { fail('native_media_manifest_unverified'); }
      const peer = await this.resolvePeer();
      const record = { schema: 2, operation_id: request.operation_id, self_id: SELF, target_id: OWNER,
        peer: { ...peer }, fingerprint, payload_sha256: request.manifest_sha256,
        reserved_at: this.clock(), state: 'pending', native_identity: null, media_evidence: evidence };
      await this.store.put(record);
      try {
        const elements = [], prepared = [];
        for (const part of loaded.manifest.parts) {
          if (part.type === 'text') {
            elements.push({ elementType: 1, elementId: '', textElement: { content: part.text,
              atType: 0, atUid: '', atTinyId: '', atNtUid: '' } });
          } else {
            const verified = { ...loaded.verifiedImages.get(part.index) }; delete verified.md5;
            const image = await bounded(() => {
              this.activeNativePreparations++;
              return Promise.resolve().then(() => this.prepareImage(this.core, verified))
                .finally(() => { this.activeNativePreparations--; });
            }, this.nativeTimeout);
            if (!sameFields({ type: 'image', ...image.evidence }, evidence[part.index])) fail('native_media_preparation_mismatch');
            prepared.push(image); elements.push(image.element);
          }
        }
        for (const image of prepared) {
          const observed = await bounded(() => this.verifyPrepared(image), this.nativeTimeout);
          if (!sameFields({ type: 'image', ...observed }, evidence[observed.index])) fail('native_media_preparation_mismatch');
        }
        const raw = await bounded(() => {
          this.activeNativeSends++;
          return Promise.resolve().then(() => this.core.apis.MsgApi.sendMsg({ ...peer }, elements, 10000))
            .finally(() => { this.activeNativeSends--; });
        }, this.nativeTimeout);
        record.native_identity = await this.rawIdentity(raw, record);
        record.state = 'acknowledged';
        await this.store.put(record);
        return await this.readVerified(record);
      } catch {
        record.state = record.native_identity ? 'acknowledged' : 'unknown';
        await this.store.put(record);
        fail('native_outcome_unknown_do_not_resend');
      }
    });
  }

  async sendVoice(request) {
    if (!this.config.enabled) fail('bridge_disabled');
    if (!exactKeys(request, ['operation_id', 'target_id', 'manifest_sha256'])
        || typeof request.operation_id !== 'string' || !OP.test(request.operation_id)
        || request.target_id !== OWNER || typeof request.manifest_sha256 !== 'string'
        || !DIGEST.test(request.manifest_sha256)) fail('unsupported_voice_request');
    return this.serial(async () => {
      const fingerprint = hash(JSON.stringify([SELF, OWNER, 'voice/v1', request.manifest_sha256]));
      const old = await this.store.get(request.operation_id);
      if (old) {
        if (old.fingerprint !== fingerprint || old.schema !== 2) fail('operation_reused_with_different_content');
        return this.readVerified(old);
      }
      const status = await this.identity();
      if (!status.capabilities.includes('owner_private_prepared_silk_backend')) fail('native_voice_api_missing');
      let loaded;
      try { loaded = await this.loadVoice(request.manifest_sha256); validateVoiceEvidence(loaded.media_evidence); }
      catch { fail('native_voice_manifest_unverified'); }
      if (loaded.manifest_sha256 !== request.manifest_sha256 || !loaded.verifiedRecord
          || !sameFields(loaded.media_evidence[0], Object.fromEntries(Object.entries(loaded.verifiedRecord).filter(([key]) => key !== 'path')))) fail('native_voice_manifest_unverified');
      const peer = await this.resolvePeer();
      const record = { schema: 2, operation_id: request.operation_id, self_id: SELF, target_id: OWNER,
        peer: { ...peer }, fingerprint, payload_sha256: request.manifest_sha256,
        reserved_at: this.clock(), state: 'pending', native_identity: null,
        media_evidence: structuredClone(loaded.media_evidence) };
      await this.store.put(record);
      try {
        const prepared = await bounded(() => {
          this.activeNativePreparations++;
          return Promise.resolve().then(() => this.prepareVoice(this.core, loaded.verifiedRecord))
            .finally(() => { this.activeNativePreparations--; });
        }, this.nativeTimeout);
        if (!sameFields(prepared.evidence, record.media_evidence[0])) fail('native_voice_preparation_mismatch');
        const observed = await bounded(() => this.verifyVoice(prepared), this.nativeTimeout);
        if (!sameFields(observed, record.media_evidence[0])) fail('native_voice_preparation_mismatch');
        const raw = await bounded(() => {
          this.activeNativeSends++;
          return Promise.resolve().then(() => this.core.apis.MsgApi.sendMsg({ ...peer }, [prepared.element], 10000))
            .finally(() => { this.activeNativeSends--; });
        }, this.nativeTimeout);
        record.native_identity = await this.rawIdentity(raw, record);
        record.state = 'acknowledged'; await this.store.put(record);
        return await this.readVerified(record);
      } catch {
        record.state = record.native_identity ? 'acknowledged' : 'unknown'; await this.store.put(record);
        fail('native_outcome_unknown_do_not_resend');
      }
    });
  }
}

async function readConfig(configPath) {
  try {
    const stat = await fs.lstat(configPath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 8192) fail('invalid_bridge_config');
    return validateConfig(JSON.parse(await fs.readFile(configPath, 'utf8')));
  } catch (error) {
    if (error.code === 'ENOENT') return { enabled: false };
    fail('invalid_bridge_config');
  }
}

function equalToken(provided, expected) {
  const wanted = Buffer.from('Bearer ' + expected);
  const actual = Buffer.from(typeof provided === 'string' ? provided : '');
  return actual.length === wanted.length && timingSafeEqual(actual, wanted);
}

async function requestJson(request) {
  if (request.headers['content-type']?.split(';')[0] !== 'application/json'
      || request.headers['transfer-encoding'] || !/^\d{1,5}$/.test(request.headers['content-length'] || '')
      || Number(request.headers['content-length']) > MAX_BODY) fail('invalid_request');
  const parts = []; let bytes = 0;
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > MAX_BODY) fail('invalid_request');
    parts.push(chunk);
  }
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(parts))); }
  catch { fail('invalid_request'); }
}

export async function startTrustedNativeBridge({ core, configPath = fileURLToPath(new URL('./config/feiyu-native-bridge.json', import.meta.url)), store: injectedStore } = {}) {
  const config = await readConfig(configPath);
  if (!config.enabled) return { enabled: false, listener: null };
  const store = injectedStore || new JsonReceiptStore(config.receipt_dir);
  await store.initialize();
  const engine = new TrustedNativeEngine(core, config, store);
  try { await engine.identity(); } catch (error) { await store.close?.(); throw error; }
  const server = http.createServer(async (request, response) => {
    const send = (status, value) => {
      const body = Buffer.from(JSON.stringify(value));
      response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length,
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Connection': 'close' });
      response.end(body);
    };
    try {
      if (request.headers.host !== `127.0.0.1:${config.port}` || Object.hasOwn(request.headers, 'origin')
          || request.rawHeaders.filter((v, i) => i % 2 === 0 && v.toLowerCase() === 'authorization').length !== 1
          || !equalToken(request.headers.authorization, config.token)) return send(403, { ok: false, reason: 'request_not_authorized' });
      let result;
      if (request.method === 'GET' && request.url === '/v1/status') {
        if (request.headers['transfer-encoding'] || Number(request.headers['content-length'] || 0) !== 0) fail('invalid_request');
        result = await engine.identity();
      }
      else if (request.method === 'POST' && request.url === '/v1/send-text') result = await engine.sendText(await requestJson(request));
      else if (request.method === 'POST' && request.url === '/v1/send-message') result = await engine.sendMessage(await requestJson(request));
      else if (request.method === 'POST' && request.url === '/v1/send-voice') result = await engine.sendVoice(await requestJson(request));
      else if (request.method === 'POST' && request.url === '/v1/read-operation') result = await engine.readOperation(await requestJson(request));
      else return send(404, { ok: false, reason: 'unsupported_endpoint' });
      send(200, { ok: true, data: result });
    } catch (error) {
      // Do not expose native errors, message bodies, account names, or keys.
      send(error instanceof BridgeError ? 409 : 503, { ok: false, reason: error instanceof BridgeError ? error.code : 'native_bridge_unavailable' });
    }
  });
  server.requestTimeout = 20000; server.headersTimeout = 10000; server.maxHeadersCount = 20;
  server.maxConnections = 4;
  try { await new Promise((resolve, reject) => { server.once('error', reject); server.listen(config.port, '127.0.0.1', resolve); }); }
  catch (error) { await store.close?.(); throw error; }
  return { enabled: true, engine, listener: server, close: async () => {
    await new Promise(resolve => server.close(resolve));
    await engine.tail;
    // Timeout is not native cancellation: sendMsg can still dispatch after its
    // inner ID-generation promise resumes. Keep the crash-sticky lock if any
    // native send is unresolved. Closing the HTTP server never cancels a send.
    const unresolved = engine.activeNativeSends > 0 || engine.activeNativePreparations > 0;
    if (!unresolved) await store.close?.();
    return { outstanding_native_send: unresolved, receipt_lock_retained: unresolved };
  } };
}
