/** Run the unchanged upstream DSH tools through a bounded AstrBot adapter. */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const root = resolve(dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
let dispose;
let diagnostics;
let restoreFetch;
try {
  let input = '';
  for await (const chunk of process.stdin) {
    input += chunk.toString('utf8');
    if (Buffer.byteLength(input) > 180000) throw new Error('INPUT_TOO_LARGE');
  }
  const request = JSON.parse(input);
  const retryVideo = request.automaticViewFailure === true && request.action === 'read'
    && request.arguments && typeof request.arguments === 'object' && !Array.isArray(request.arguments)
    && Object.keys(request.arguments).every(key => ['video', 'page'].includes(key))
    && /^BV[0-9A-Za-z]{10}$/.test(request.arguments.video)
    && (request.arguments.page === undefined || request.arguments.page === 1)
      ? request.arguments.video : undefined;
  const config = JSON.parse(readFileSync(resolve(root, 'config/bili-humanism.json'), 'utf8'));
  // Trusted bridge scope only; tool arguments cannot opt into transport reconnects.
  config._astrbotReadViewReconnect = request.action === 'read';
  // Credentials stay in private local files, never command arguments or model input.
  const existing = JSON.parse(readFileSync(resolve(root, 'data/astrbot/config/astrbot_plugin_bilibili_ai_bot_config.json'), 'utf8').replace(/^\uFEFF/, ''));
  if (existing.SESSDATA) {
    mkdirSync(config.dataDir, { recursive: true });
    writeFileSync(config.credentialPath, JSON.stringify({ sessdata: existing.SESSDATA }), { mode: 0o600 });
  } else {
    config.credentialPath = '';
  }
  if (request.action === 'read') {
    try {
      const { createReadTransportDiagnostics } = await import('./BiliReadTransportDiagnostics.mjs');
      diagnostics = createReadTransportDiagnostics({ action: request.action, dataDir: config.dataDir, retryVideo });
      const originalFetch = globalThis.fetch;
      const observedFetch = diagnostics.wrapFetch(originalFetch);
      restoreFetch = () => { if (globalThis.fetch === observedFetch) globalThis.fetch = originalFetch; };
      globalThis.fetch = observedFetch;
    } catch {
      try { restoreFetch?.(); } catch {}
      diagnostics = undefined;
      restoreFetch = undefined;
    }
  }
  const upstream = await import(pathToFileURL(resolve(root, 'apps/dsh-bili-humanism/index.js')).href);
  const tools = new Map();
  upstream.apply({ tools: { register(tool) { tools.set(tool.name, tool); } }, on(event, fn) { if (event === 'dispose') dispose = fn; } }, config);
  if (request.action === 'catalog') {
    process.stdout.write(JSON.stringify([...tools.values()].map(tool => ({ name: tool.name, description: tool.description, parameters: tool.parameters }))));
  } else {
    const name = 'bili_humanism_' + request.action;
    const tool = tools.get(name);
    if (!tool) throw new Error('UNAVAILABLE_TOOL');
    let result = await tool.execute(request.arguments || {}, {});
    try {
      const value = JSON.parse(result);
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        // Upstream output never supplies a capability. Strip reserved fields
        // before considering only this bridge's current in-memory observation.
        const reserved = Object.hasOwn(value, '_astrbot_view_failure') || Object.hasOwn(value, '_bili_view_failure');
        delete value._astrbot_view_failure;
        delete value._bili_view_failure;
        const clean = reserved ? JSON.stringify(value) : result;
        result = clean;
        const receipt = diagnostics?.takeViewFailure(clean);
        result = receipt ? JSON.stringify({ ...value, _astrbot_view_failure: receipt }) : clean;
      }
    } catch { /* A missing receipt leaves the existing pause behavior. */ }
    try { diagnostics?.finish(result); } catch {}
    process.stdout.write(result);
  }
} catch {
  try { diagnostics?.finish(true); } catch {}
  // Upstream tool errors are already redacted. Bridge failures never expose files.
  process.stdout.write(JSON.stringify({ error: 'HUMANISM_BRIDGE_FAILED' }));
  process.exitCode = 1;
} finally {
  try { restoreFetch?.(); } catch {}
  dispose?.();
}
