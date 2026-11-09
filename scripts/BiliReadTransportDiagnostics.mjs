/** Bounded metadata for failed bridge reads. Importing this module does no I/O. */
import { mkdirSync, openSync, writeFileSync, closeSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';
import { randomUUID } from 'node:crypto';

const API_ENDPOINTS = new Map([
  ['/x/web-interface/view', 'view'],
  ['/x/web-interface/nav', 'nav'],
  ['/x/web-interface/view/conclusion/get', 'summary'],
  ['/x/player/wbi/v2', 'player'],
]);
const CODE_KINDS = new Map([
  ...['ENOTFOUND', 'EAI_AGAIN', 'EAI_FAIL'].map(code => [code, 'dns']),
  ...['ECONNRESET', 'ECONNREFUSED', 'EPIPE', 'ENETUNREACH', 'EHOSTUNREACH', 'UND_ERR_SOCKET'].map(code => [code, 'connection']),
  ...['ETIMEDOUT', 'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT'].map(code => [code, 'timeout']),
  ...['CERT_HAS_EXPIRED', 'ERR_TLS_CERT_ALTNAME_INVALID', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'DEPTH_ZERO_SELF_SIGNED_CERT', 'SELF_SIGNED_CERT_IN_CHAIN', 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY', 'ERR_SSL_WRONG_VERSION_NUMBER', 'ERR_SSL_TLSV1_ALERT_INTERNAL_ERROR'].map(code => [code, 'tls']),
  ['ABORT_ERR', 'aborted'],
]);
const hrefGetter = Object.getOwnPropertyDescriptor(URL.prototype, 'href').get;
const requestUrlGetter = Object.getOwnPropertyDescriptor(Request.prototype, 'url').get;
const requestMethodGetter = Object.getOwnPropertyDescriptor(Request.prototype, 'method').get;
const responseStatusGetter = Object.getOwnPropertyDescriptor(Response.prototype, 'status').get;
const domNameGetter = Object.getOwnPropertyDescriptor(DOMException.prototype, 'name').get;
const MAX_REQUESTS = 12;

function ownData(value, key) {
  if (!value || (typeof value !== 'object' && typeof value !== 'function')) return undefined;
  return Object.getOwnPropertyDescriptor(value, key)?.value;
}
function endpointOf(input, init) {
  // Observe only GETs of the already bounded read operation. Never inspect headers.
  const methodProperty = init && Object.getOwnPropertyDescriptor(init, 'method');
  if (methodProperty && !Object.hasOwn(methodProperty, 'value')) return null;
  let method = methodProperty?.value;
  if (method === undefined && typeof input !== 'string') {
    try { method = Reflect.apply(requestMethodGetter, input, []); } catch {}
  }
  if (method !== undefined && method !== 'GET') return null;
  let raw;
  if (typeof input === 'string') raw = input;
  else {
    try { raw = Reflect.apply(hrefGetter, input, []); }
    catch { try { raw = Reflect.apply(requestUrlGetter, input, []); } catch { return 'other'; } }
  }
  if (typeof raw !== 'string' || raw.length > 16384) return 'other';
  const url = new URL(raw);
  if (url.protocol !== 'https:') return 'other';
  if (url.hostname === 'api.bilibili.com') return API_ENDPOINTS.get(url.pathname) || 'other';
  if (['aisubtitle.hdslb.com', 'i0.hdslb.com'].includes(url.hostname)) return 'subtitle';
  if (/(^|\.)bilivideo\.com$/.test(url.hostname)) return 'media';
  return 'other';
}
function statusOf(response) {
  let status = ownData(response, 'status');
  if (status === undefined) {
    try { status = Reflect.apply(responseStatusGetter, response, []); } catch { return null; }
  }
  return Number.isInteger(status) && status >= 100 && status <= 599 ? status : null;
}
function classify(error) {
  const pending = [error], seen = new Set(), codes = [];
  let special;
  while (pending.length && seen.size < 8 && codes.length < 4) {
    const current = pending.shift();
    if (!current || (typeof current !== 'object' && typeof current !== 'function') || seen.has(current)) continue;
    seen.add(current);
    const code = ownData(current, 'code');
    if (typeof code === 'string' && CODE_KINDS.has(code) && !codes.includes(code)) codes.push(code);
    let name = ownData(current, 'name');
    if (name === undefined) { try { name = Reflect.apply(domNameGetter, current, []); } catch {} }
    if (name === 'TimeoutError') special = 'TIMEOUT';
    else if (name === 'AbortError' && !special) special = 'ABORTED';
    const cause = ownData(current, 'cause');
    if (cause) pending.push(cause);
    const errors = ownData(current, 'errors');
    if (Array.isArray(errors)) {
      for (let i = 0; i < Math.min(errors.length, 4); i++) pending.push(ownData(errors, String(i)));
    }
  }
  if (!codes.length) return { kind: special === 'TIMEOUT' ? 'timeout' : special === 'ABORTED' ? 'aborted' : 'unknown', cause_codes: [special || 'UNKNOWN'] };
  return { kind: CODE_KINDS.get(codes[0]), cause_codes: codes };
}
function failedResult(result) {
  if (result === true) return true;
  if (typeof result !== 'string') return false;
  try {
    const value = JSON.parse(result);
    return value !== null && !Array.isArray(value) && typeof value === 'object' && Object.hasOwn(value, 'error');
  } catch { return false; }
}

export function createReadTransportDiagnostics({ action, dataDir } = {}) {
  const enabled = action === 'read' && typeof dataDir === 'string' && isAbsolute(dataDir);
  const startedAt = new Date().toISOString();
  const pid = process.pid;
  const proxyEnvironment = {
    cli_enabled: process.execArgv.includes('--use-env-proxy'),
    node_env_enabled: process.env.NODE_USE_ENV_PROXY === '1',
    http_proxy_present: Boolean(process.env.http_proxy || process.env.HTTP_PROXY),
    https_proxy_present: Boolean(process.env.https_proxy || process.env.HTTPS_PROXY),
    all_proxy_present: Boolean(process.env.all_proxy || process.env.ALL_PROXY),
    no_proxy_present: Boolean(process.env.no_proxy || process.env.NO_PROXY),
    configuration_only: true,
  };
  const requests = [];
  let ordinal = 0, finished = false, truncated = false;
  function observe(args, requestOrdinal, response, error, rejected) {
    try {
      const endpoint = endpointOf(args[0], args[1]);
      if (endpoint === null) return;
      if (requests.length >= MAX_REQUESTS) { truncated = true; return; }
      requests.push({
        ordinal: requestOrdinal,
        at: new Date().toISOString(),
        endpoint,
        phase: rejected ? 'fetch_rejected' : 'response_headers',
        http_status: rejected ? null : statusOf(response),
        ...(rejected ? classify(error) : { kind: 'headers_received', cause_codes: [] }),
      });
    } catch { /* Diagnostics must not change fetch behavior. */ }
  }
  return {
    wrapFetch(original) {
      if (!enabled || typeof original !== 'function') return original;
      // Keep the original return value, this, arguments and rejection identity.
      // The production caller already awaits/catches this native fetch Promise.
      return function (...args) {
        const requestOrdinal = ordinal = Math.min(65535, ordinal + 1);
        let result;
        try { result = Reflect.apply(original, this, args); }
        catch (error) { observe(args, requestOrdinal, null, error, true); throw error; }
        try {
          Promise.prototype.then.call(result,
            response => { observe(args, requestOrdinal, response, null, false); },
            error => { observe(args, requestOrdinal, null, error, true); });
        } catch { /* Non-Promise returns remain untouched. */ }
        return result;
      };
    },
    finish(result) {
      if (finished) return;
      finished = true;
      if (!enabled || !failedResult(result) || !requests.length) return;
      let fd;
      try {
        const directory = join(dataDir, 'transport-diagnostics');
        const record = {
          schema: 1, started_at: startedAt, finished_at: new Date().toISOString(), pid,
          proxy_environment: proxyEnvironment, requests, truncated,
        };
        const body = JSON.stringify(record) + '\n';
        if (Buffer.byteLength(body) > 16384) return;
        mkdirSync(directory, { recursive: true });
        // One exclusive new file per failed read; never overwrite older evidence.
        fd = openSync(join(directory, startedAt.replace(/[:.]/g, '-') + '-' + pid + '-' + randomUUID() + '.json'), 'wx', 0o600);
        writeFileSync(fd, body, 'utf8');
      } catch { /* No failure of this sidecar may change the business result. */ }
      finally { if (fd !== undefined) { try { closeSync(fd); } catch {} } }
    },
  };
}
