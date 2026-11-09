/** Execute only the upstream's three package-local, read-only tools. */
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve } from 'node:path';

const allowed = new Set(['layers', 'manifest', 'doctor']);
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..');
try {
  let input = '';
  for await (const chunk of process.stdin) {
    input += chunk.toString('utf8');
    if (Buffer.byteLength(input) > 1024) throw new Error('INPUT_TOO_LARGE');
  }
  const request = JSON.parse(input);
  if (!allowed.has(request.action)) throw new Error('READ_ONLY_ACTION_REQUIRED');
  const upstream = await import(pathToFileURL(resolve(root, 'apps/dsh-agent-soul/index.js')).href);
  const tools = new Map();
  upstream.apply({ tools: { register(tool) { tools.set(tool.name, tool); } } });
  const result = await tools.get('agent_soul_' + request.action).execute({});
  // Exclude arbitrary extra fields and retain the original text/checks.
  process.stdout.write(JSON.stringify({
    ok: !!result.ok, text: String(result.text || ''), checks: result.checks || [],
    upstream_tool: 'agent_soul_' + request.action,
  }));
} catch {
  process.stdout.write(JSON.stringify({ ok: false, error: 'AGENT_SOUL_READ_FAILED' }));
  process.exitCode = 1;
}
