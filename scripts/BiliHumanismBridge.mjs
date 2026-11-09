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
  const config = JSON.parse(readFileSync(resolve(root, 'config/bili-humanism.json'), 'utf8'));
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
      diagnostics = createReadTransportDiagnostics({ action: request.action, dataDir: config.dataDir });
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
    const result = await tool.execute(request.arguments || {}, {});
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
