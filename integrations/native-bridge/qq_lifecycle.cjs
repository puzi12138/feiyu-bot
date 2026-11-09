'use strict';
/** Metadata-only diagnostics. No QQ API calls, token reads, sends or recovery. */
const fs = require('node:fs');
const path = require('node:path');
const { randomBytes } = require('node:crypto');
const { execFile } = require('node:child_process');

const STAGE = 'native_framework_lifecycle_candidate_20261026';
const EVENT_NAMES = new Set([
  'initialization_observed', 'initialization_reentry_observed', 'process_identity_observed',
  'process_identity_unavailable', 'loader_init_requested', 'loader_init_completed', 'loader_init_failed',
  'loader_import_begin', 'loader_import_completed', 'loader_framework_await_begin', 'loader_framework_await_completed',
  'framework_init_entered', 'wrapper_load_begin', 'wrapper_load_completed', 'login_listener_registered',
  'login_connected', 'login_disconnected', 'login_succeeded', 'login_failed', 'qr_session_failed',
  'kicked_offline', 'profile_status', 'core_ready', 'bridge_start_requested', 'bridge_started',
  'bridge_closed', 'bridge_close_failed', 'process_before_exit', 'process_exit',
  'uncaught_exception_observed', 'unhandled_rejection_observed',
]);
const ownValue = (value, key) => {
  try {
    if (!value || !['object', 'function'].includes(typeof value)) return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    return descriptor && Object.hasOwn(descriptor, 'value') ? descriptor.value : undefined;
  } catch { return undefined; }
};
const safeInteger = value => typeof value === 'number' && Number.isSafeInteger(value)
  && value >= -2147483648 && value <= 2147483647 ? value : null;
const objectIdentity = value => value !== null && ['object', 'function'].includes(typeof value);

function classifyKick(value) {
  // Inspect only the two text fields established in this exact Framework source.
  // These strings never leave this function, even when they contain private data.
  const parts = ['tipsTitle', 'tipsDesc'].map(key => ownValue(value, key))
    .filter(item => typeof item === 'string').map(item => item.slice(0, 512));
  const text = parts.join(' ');
  if (/异地|其他(?:设备|地点)|另一(?:设备|地点)|别处|other device|another device/i.test(text)) return 'account_elsewhere';
  if (/风控|验证|安全(?:检查|校验)|异常设备|风险|captcha|verification|security check/i.test(text)) return 'security_verification';
  if (/登录.{0,8}(?:失效|过期)|重新登录|身份.{0,8}(?:失效|过期)|会话.{0,8}过期|session expired|login expired/i.test(text)) return 'login_invalid';
  if (/连接.{0,8}(?:断开|中断|失败)|网络.{0,8}(?:断开|中断|异常)|断线|disconnected|connection lost/i.test(text)) return 'connection_disconnected';
  return 'other';
}

function eventData(event, payload) {
  if (event === 'kicked_offline') {
    const fields = [], types = {};
    for (const key of ['tipsTitle', 'tipsDesc']) {
      const value = ownValue(payload, key);
      if (value === undefined) continue;
      fields.push(key); types[key] = value === null ? 'null' : typeof value;
    }
    return { classification: classifyKick(payload), known_fields: fields, field_types: types,
      numeric_codes: {}, numeric_codes_available: false };
  }
  if (event === 'qr_session_failed') return {
    numeric_codes: { err_type: safeInteger(ownValue(payload, 'errType')), err_code: safeInteger(ownValue(payload, 'errCode')) },
  };
  if (event === 'profile_status') return { status: safeInteger(ownValue(payload, 'status')) };
  return {};
}

/** Create a recorder with an injectable sink for offline tests. Never throws. */
function createLifecycleRecorder(options = {}) {
  const sessions = new WeakMap(), services = new WeakMap();
  let attempts = 0, sessionCount = 0, serviceCount = 0, sequence = 0, writeFailures = 0;
  const pid = Number.isSafeInteger(options.pid) ? options.pid : process.pid;
  const parent = Number.isSafeInteger(options.parentPid) ? options.parentPid : process.ppid;
  const runId = typeof options.runId === 'string' && /^[a-f0-9]{16}$/.test(options.runId) ? options.runId : randomBytes(8).toString('hex');
  let creationUtc = null, identitySource = 'not_observed';
  const sink = typeof options.sink === 'function' ? options.sink : () => {};
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  const handleFor = session => objectIdentity(session) ? sessions.get(session) : undefined;
  const recorder = {
    begin(session, loginService) {
      try {
        attempts++;
        let handle = handleFor(session);
        const repeated = !!handle;
        if (!handle && objectIdentity(session)) {
          handle = { session_generation: ++sessionCount, session_init_attempts: 0, login_service_generation: 0 };
          sessions.set(session, handle);
        }
        if (handle) {
          handle.session_init_attempts++;
          if (objectIdentity(loginService)) {
            if (!services.has(loginService)) services.set(loginService, ++serviceCount);
            handle.login_service_generation = services.get(loginService);
          }
        }
        recorder.record(session, repeated ? 'initialization_reentry_observed' : 'initialization_observed');
        return handle ? { ...handle, initialization_attempt: attempts } : null;
      } catch { writeFailures++; return null; }
    },
    record(session, event, payload) {
      try {
        if (!EVENT_NAMES.has(event) || sequence >= 8192) return false;
        const handle = handleFor(session);
        const record = {
          schema: 1, stage: STAGE, run_id: runId, sequence: ++sequence, timestamp_utc: now(),
          process_id: pid, parent_process_id: parent, process_created_utc: creationUtc,
          process_identity_source: identitySource, initialization_attempts: attempts,
          session_generation: handle?.session_generation || 0,
          session_initialization_attempts: handle?.session_init_attempts || 0,
          login_service_generation: handle?.login_service_generation || 0,
          distinct_sessions: sessionCount, guard_mode: 'observe_only', event,
          ...eventData(event, payload),
        };
        // Serialize only our freshly constructed fixed-shape object. Never the
        // native payload, session, login service, Error, callback or text body.
        sink(JSON.stringify(record) + '\n');
        return true;
      } catch { writeFailures++; return false; }
    },
    async observeProcessIdentity(query) {
      try {
        const identity = await query(pid);
        const createdUtc = ownValue(identity, 'created_utc');
        if (!identity || ownValue(identity, 'pid') !== pid || ownValue(identity, 'parent_pid') !== parent
            || typeof createdUtc !== 'string'
            || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?Z$/.test(createdUtc)
            || !Number.isFinite(Date.parse(createdUtc))) throw new Error('identity_unverified');
        creationUtc = createdUtc; identitySource = 'windows_cim_verified';
        recorder.record(null, 'process_identity_observed');
      } catch {
        creationUtc = null; identitySource = 'unavailable'; recorder.record(null, 'process_identity_unavailable');
      }
    },
    snapshot() { return { initialization_attempts: attempts, distinct_sessions: sessionCount, records: sequence,
      write_failures: writeFailures, process_identity_source: identitySource }; },
  };
  return recorder;
}

function queryWindowsProcessIdentity(pid) {
  return new Promise((resolve, reject) => {
    if (process.platform !== 'win32') return reject(new Error('windows_identity_required'));
    // Fixed read-only query; PID is this current process's integer, never input.
    const command = `$ErrorActionPreference='Stop'; $p=Get-CimInstance Win32_Process -Filter 'ProcessId=${pid}'; `
      + `[pscustomobject]@{pid=[int]$p.ProcessId;parent_pid=[int]$p.ParentProcessId;created_utc=$p.CreationDate.ToUniversalTime().ToString('o')}|ConvertTo-Json -Compress`;
    const encoded = Buffer.from(command, 'utf16le').toString('base64');
    execFile('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe',
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
      { windowsHide: true, timeout: 3000, maxBuffer: 2048, encoding: 'utf8' },
      (error, stdout) => {
        if (error) return reject(new Error('process_identity_unavailable'));
        try { resolve(JSON.parse(stdout.replace(/^\uFEFF/, '').trim())); }
        catch { reject(new Error('process_identity_unavailable')); }
      });
  });
}

function boundedFileSink(directory) {
  let descriptor = null, bytes = 0, failed = false;
  return {
    append(line) {
      if (failed || bytes + Buffer.byteLength(line) > 1024 * 1024) return;
      try {
        const absolute = path.resolve(directory), root = path.parse(absolute).root;
        let cursor = root;
        for (const part of absolute.slice(root.length).split(path.sep)) {
          cursor = path.join(cursor, part);
          if (!fs.existsSync(cursor)) fs.mkdirSync(cursor, { mode: 0o700 });
          const stat = fs.lstatSync(cursor);
          if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error('diagnostic_path_invalid');
        }
        if (descriptor === null) {
          if (fs.readdirSync(absolute).filter(name => /^qq-lifecycle-\d+-[a-f0-9]{16}\.jsonl$/.test(name)).length >= 128) return;
          const file = path.join(absolute, `qq-lifecycle-${process.pid}-${randomBytes(8).toString('hex')}.jsonl`);
          descriptor = fs.openSync(file, 'ax', 0o600);
        }
        fs.writeSync(descriptor, line, null, 'utf8'); fs.fsyncSync(descriptor);
        bytes += Buffer.byteLength(line);
      } catch { failed = true; }
    },
    close() { try { if (descriptor !== null) fs.closeSync(descriptor); } catch {} descriptor = null; },
  };
}

function getLifecycle() {
  const key = Symbol.for('feiyu.native.lifecycle.candidate.20261026');
  if (!globalThis[key]) {
    const file = boundedFileSink(path.join(__dirname, 'lifecycle-diagnostics'));
    const recorder = createLifecycleRecorder({ sink: line => file.append(line) });
    globalThis[key] = recorder;
    void recorder.observeProcessIdentity(queryWindowsProcessIdentity);
    process.once('exit', () => { recorder.record(null, 'process_exit'); file.close(); });
  }
  return globalThis[key];
}

module.exports = { STAGE, createLifecycleRecorder, classifyKick, getLifecycle };
