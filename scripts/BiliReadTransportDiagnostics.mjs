/** Bounded metadata for failed bridge reads. Importing this module does no I/O. */
import { mkdirSync, openSync, writeFileSync, closeSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import { channel } from 'node:diagnostics_channel';
import { isProxy, isNativeError } from 'node:util/types';

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
const signalAbortedGetter = Object.getOwnPropertyDescriptor(AbortSignal.prototype, 'aborted').get;
const MAX_REQUESTS = 12;
const MAX_TRANSPORT_EVENTS = 24;
const transportContext = new AsyncLocalStorage();
const activeTransportScopes = new Set();
const TRANSPORT_CHANNELS = [
  ['undici:client:beforeConnect', 'before_connect'],
  ['undici:client:connected', 'connected'],
  ['undici:client:connectError', 'connect_error'],
  ['undici:proxy:connected', 'proxy_connect_200'],
];

function ownData(value, key) {
  if (!value || (typeof value !== 'object' && typeof value !== 'function') || isProxy(value)) return undefined;
  return Object.getOwnPropertyDescriptor(value, key)?.value;
}
function endpointOf(input, init) {
  // Inspecting Proxy descriptors can execute traps before the real fetch.
  if (isProxy(input) || isProxy(init)) return null;
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
// This allowlist is independent of best-effort disk diagnostics. It never
// observes error text, getters, sidecars or unfixed error codes.
function viewFailureKind(error) {
  try {
    const seen = new Set();
    let kind = null;
    while (error && typeof error === 'object' && !isProxy(error) && !seen.has(error) && seen.size < 8) {
      let domName;
      try { domName = Reflect.apply(domNameGetter, error, []); } catch {}
      if (!isNativeError(error) && domName === undefined) return null;
      seen.add(error);
      const fields = ['code', 'name', 'cause', 'errors', 'status', 'statusCode', 'response'].map(key => Object.getOwnPropertyDescriptor(error, key));
      if (fields.some(field => field && !Object.hasOwn(field, 'value'))) return null;
      const [code, ownName, cause, errors, status, statusCode, response] = fields.map(field => field?.value);
      const name = ownName ?? domName;
      if (errors !== undefined || status !== undefined || statusCode !== undefined || response !== undefined
          || name !== undefined && !['Error', 'TypeError', 'ConnectTimeoutError', 'HeadersTimeoutError'].includes(name)) return null;
      if (name === 'ConnectTimeoutError' && code !== 'UND_ERR_CONNECT_TIMEOUT'
          || name === 'HeadersTimeoutError' && code !== 'UND_ERR_HEADERS_TIMEOUT') return null;
      let current;
      if (code === 'ECONNRESET') current = 'connection';
      else if (['ETIMEDOUT', 'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT'].includes(code)) current = 'timeout';
      else if (code !== undefined) return null;

      if (current && kind && kind !== current) return null;
      kind = current || kind;
      if (cause !== undefined && cause !== null && typeof cause !== 'object') return null;
      error = cause;
    }
    return !error ? kind : null;
  } catch { return null; }
}
function exactViewRequest(args, expectedVideo) {
  if (isProxy(args[0]) || isProxy(args[1])) return false;
  const init = args[1];
  if (!init || typeof init !== 'object') return false;
  const method = Object.getOwnPropertyDescriptor(init, 'method');
  const body = Object.getOwnPropertyDescriptor(init, 'body');
  const redirect = Object.getOwnPropertyDescriptor(init, 'redirect');
  const signal = Object.getOwnPropertyDescriptor(init, 'signal');
  if (!method || !Object.hasOwn(method, 'value') || method.value !== 'GET'
      || !redirect || !Object.hasOwn(redirect, 'value') || redirect.value !== 'manual'
      || !signal || !Object.hasOwn(signal, 'value')
      || Reflect.apply(signalAbortedGetter, signal.value, []) !== false
      || body && (!Object.hasOwn(body, 'value') || body.value !== undefined)) return false;
  let raw;
  if (typeof args[0] === 'string') raw = args[0];
  else { try { raw = Reflect.apply(hrefGetter, args[0], []); } catch { return false; } }
  const url = new URL(raw);
  return url.origin === 'https://api.bilibili.com' && !url.username && !url.password
    && url.pathname === '/x/web-interface/view' && !url.hash
    && [...url.searchParams.keys()].length === 1 && url.searchParams.get('bvid') === expectedVideo;
}
function failedResult(result) {
  if (result === true) return true;
  if (typeof result !== 'string') return false;
  try {
    const value = JSON.parse(result);
    return value !== null && !Array.isArray(value) && typeof value === 'object' && Object.hasOwn(value, 'error');
  } catch { return false; }
}

export function createReadTransportDiagnostics({ action, dataDir, retryVideo } = {}) {
  const enabled = action === 'read' && typeof dataDir === 'string' && isAbsolute(dataDir);
  const startedAt = new Date().toISOString();
  const startedMs = Date.now();
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
  const retryEnabled = action === 'read' && typeof retryVideo === 'string' && /^BV[0-9A-Za-z]{10}$/.test(retryVideo);
  let retryCalls = 0, retryPending = 0, retryInvalid = false, retryConsumed = false;
  const retryKinds = [];
  function retryStart(args) {
    if (!retryEnabled || finished) return false;
    retryCalls = Math.min(3, retryCalls + 1);
    if (retryPending || retryCalls > 2) retryInvalid = true;
    retryPending++;
    try { if (!exactViewRequest(args, retryVideo)) retryInvalid = true; }
    catch { retryInvalid = true; }
    return true;
  }
  function retryEnd(started, args, error, rejected) {
    if (!started) return;
    retryPending--;
    try { if (!exactViewRequest(args, retryVideo)) retryInvalid = true; }
    catch { retryInvalid = true; }
    if (!rejected) { retryInvalid = true; return; }
    const kind = viewFailureKind(error);
    if (!kind) retryInvalid = true;
    if (retryKinds.length < 2) retryKinds.push(kind);
    else retryInvalid = true;
  }
  let ordinal = 0, finished = false, truncated = false;
  const transportEvents = [], subscriptions = [];
  let transportStarted = false, transportUnavailable = false;
  let transportAmbiguous = false, transportTruncated = false;
  const transportOwner = {
    ambiguous() { transportAmbiguous = true; transportEvents.length = 0; retryInvalid = true; },
  };
  function stopTransport() {
    for (const [source, callback] of subscriptions.splice(0)) {
      try { source.unsubscribe(callback); } catch {}
    }
    for (const scope of activeTransportScopes) {
      if (scope.owner === transportOwner) {
        scope.active = false;
        activeTransportScopes.delete(scope);
      }
    }
  }
  function startTransport() {
    if (transportStarted || finished) return;
    transportStarted = true;
    try {
      for (const [name, phase] of TRANSPORT_CHANNELS) {
        const source = channel(name);
        const callback = (message) => {
          // Async resource ancestry is only a filter. Pools/reuse/queued callbacks
          // prevent these process events from proving request or task ownership.
          try {
            const scope = transportContext.getStore();
            if (finished || transportAmbiguous || !scope?.active || !scope.observe || scope.owner !== transportOwner) return;
            const params = ownData(message, 'connectParams');
            let endpoint;
            if (phase === 'proxy_connect_200') {
              if (ownData(params, 'path') !== 'api.bilibili.com:443'
                  || !['http://127.0.0.1:7897', 'http://localhost:7897', 'http://[::1]:7897']
                    .includes(ownData(params, 'origin'))) return;
              endpoint = 'bili_api';
            } else {
              const host = ownData(params, 'hostname');
              const protocol = ownData(params, 'protocol');
              if (host === 'api.bilibili.com' && protocol === 'https:'
                  && ['', '443', 443].includes(ownData(params, 'port'))) endpoint = 'bili_api';
              else if (['127.0.0.1', 'localhost', '::1'].includes(host)
                       && protocol === 'http:' && ['7897', 7897].includes(ownData(params, 'port'))) endpoint = 'loopback_proxy';
              else return;
            }
            if (transportEvents.length >= MAX_TRANSPORT_EVENTS) { transportTruncated = true; return; }
            const elapsed = Date.now() - startedMs;
            transportEvents.push({
              phase, endpoint,
              elapsed_ms: Number.isFinite(elapsed) ? Math.max(0, Math.min(3600000, Math.floor(elapsed))) : 0,
              ...(phase === 'connect_error' ? classify(ownData(message, 'error')) : {}),
            });
          } catch { /* A diagnostics subscriber must never throw into the host. */ }
        };
        // Register before subscribing so partial initialization can be undone.
        subscriptions.push([source, callback]);
        source.subscribe(callback);
      }
    } catch {
      transportUnavailable = true;
      stopTransport();
    }
  }
  function enterTransport(args) {
    let endpoint;
    try { endpoint = endpointOf(args[0], args[1]); } catch { endpoint = null; }
    if (finished) return null;
    const eligible = endpoint !== null && endpoint !== 'other';
    if (eligible) startTransport();
    // Count every wrapped call for overlap, including POST/unknown/invalid input.
    const scope = { owner: transportOwner, active: true, observe: eligible && !transportUnavailable };
    activeTransportScopes.add(scope);
    if (activeTransportScopes.size > 1) {
      for (const active of activeTransportScopes) active.owner.ambiguous();
    }
    return scope;
  }
  function leaveTransport(scope) {
    if (scope) { scope.active = false; activeTransportScopes.delete(scope); }
  }
  function observe(args, requestOrdinal, response, error, rejected) {
    try {
      if (finished) return;
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
      if (!enabled || finished || typeof original !== 'function') return original;
      // Keep the original return value, this, arguments and rejection identity.
      // The production caller already awaits/catches this native fetch Promise.
      return function (...args) {
        const requestOrdinal = ordinal = Math.min(65535, ordinal + 1);
        const retryStarted = retryStart(args);
        const scope = enterTransport(args);
        let result;
        try {
          result = scope ? transportContext.run(scope, () => Reflect.apply(original, this, args))
            : Reflect.apply(original, this, args);
        } catch (error) {
          // A local synchronous throw is not a completed network fetch attempt.
          retryInvalid = true;
          retryEnd(retryStarted, args, error, true);
          leaveTransport(scope);
          observe(args, requestOrdinal, null, error, true);
          throw error;
        }
        try {
          Promise.prototype.then.call(result,
            response => { retryEnd(retryStarted, args, null, false); leaveTransport(scope); observe(args, requestOrdinal, response, null, false); },
            error => { retryEnd(retryStarted, args, error, true); leaveTransport(scope); observe(args, requestOrdinal, null, error, true); });
        } catch { retryInvalid = true; retryEnd(retryStarted, args, null, false); leaveTransport(scope); /* Non-Promise returns remain untouched. */ }
        return result;
      };
    },
    takeViewFailure(result) {
      if (!retryEnabled || retryConsumed || finished) return null;
      retryConsumed = true;
      let value;
      try { value = typeof result === 'string' ? JSON.parse(result) : null; } catch { return null; }
      if (retryInvalid || retryPending !== 0 || retryCalls !== 2 || retryKinds.length !== 2
          || retryKinds[0] !== 'connection' || !['connection', 'timeout'].includes(retryKinds[1])
          || !value || Array.isArray(value) || Object.keys(value).some(key => !['error', 'message'].includes(key))
          || value.error !== 'NETWORK_FAILED') return null;
      // These flags describe wrapped fetch observations through this tool result.
      // They do not deny proxy CONNECT HTTP or independent asynchronous I/O.
      return { version: 1, endpoint: 'view', page: 1, attempts: 2,
        kind: retryKinds[1], no_http_response: true, no_followup: true };
    },
    finish(result) {
      if (finished) return;
      finished = true;
      stopTransport();
      if (!enabled || !failedResult(result) || !requests.length) return;
      let fd;
      try {
        const directory = join(dataDir, 'transport-diagnostics');
        const record = {
          schema: 1, started_at: startedAt, finished_at: new Date().toISOString(), pid,
          proxy_environment: proxyEnvironment, requests, truncated,
          transport_observations: {
            attribution: 'unattributed_process_events',
            request_or_task_association: 'unknown',
            absence_means: 'unknown',
            collection: transportUnavailable ? 'unavailable' : 'presence_only',
            ambiguous_overlap: transportAmbiguous, truncated: transportTruncated,
            events: transportEvents,
          },
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
