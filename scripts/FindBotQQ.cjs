// Identify only the configured bot process by the account database it holds.
// This does not read messages or load the hook into any process.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

async function main() {
  const root = path.resolve(__dirname, '..');
  const client = JSON.parse(fs.readFileSync(path.join(root, 'config', 'qq-client.json'), 'utf8').replace(/^\uFEFF/, ''));
  const uin = String(client.expectedUin);
  if (!/^\d{5,12}$/.test(uin)) throw new Error('Invalid configured bot account');
  let dataRoot = path.join(os.homedir(), 'Documents', 'Tencent Files');
  const iniPath = path.join(path.dirname(os.homedir()), 'Public', 'Documents', 'Tencent', 'QQ', 'UserDataInfo.ini');
  if (fs.existsSync(iniPath)) {
    const bytes = fs.readFileSync(iniPath);
    const iniText = bytes[0] === 0xff && bytes[1] === 0xfe ? bytes.toString('utf16le') : bytes.toString('utf8');
    let inSection = false;
    for (const raw of iniText.replace(/^\uFEFF/, '').split(/\r?\n/)) {
      const line = raw.trim();
      if (line.startsWith('[')) { inSection = line.toLowerCase() === '[userdataset]'; continue; }
      const eq = line.indexOf('=');
      if (inSection && eq > 0 && line.slice(0, eq).trim().toLowerCase() === 'userdatasavepath') {
        const value = line.slice(eq + 1).trim();
        if (value) dataRoot = path.basename(value).toLowerCase() === 'tencent files' ? value : path.join(value, 'Tencent Files');
      }
    }
  }
  const db = path.join(dataRoot, uin, 'nt_qq', 'nt_db', 'nt_msg.db');
  if (!fs.existsSync(db)) { console.log('[]'); return; }
  const addon = require(path.join(root, 'apps', 'SnowLuma', 'native', 'snowluma-win32-x64.node'));
  const holders = await addon.getFileHoldersAsync(db);
  console.log(JSON.stringify([...new Set(holders.filter(h => /^qq(?:\.exe)?$/i.test(h.name)).map(h => h.pid))]));
}
main().catch(e => { console.error(e.message); process.exitCode = 1; });
