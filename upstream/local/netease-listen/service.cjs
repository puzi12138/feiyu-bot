'use strict';

// Only this bridge's fixed API allowlist is exposed. The upstream web server is never started.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

const ROOT = process.env.QQBOT_ROOT || 'D:/QQBot';
const APP = path.join(ROOT, 'apps/netease-listen');
const CONFIG = path.join(ROOT, 'config/netease-listen');
const LOG = path.join(ROOT, 'logs/netease-listen');
const TEMP = path.join(ROOT, 'temp/netease-listen');
for (const dir of [CONFIG, LOG, TEMP]) fs.mkdirSync(dir, { recursive: true });
process.env.TEMP = process.env.TMP = TEMP;

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); } catch { return fallback; }
}
function atomic(file, content) {
  const tmp = `${file}.new`;
  fs.writeFileSync(tmp, content, { mode: 0o600 });
  fs.renameSync(tmp, file);
}
const settingsPath = path.join(CONFIG, 'settings.json');
const settings = readJson(settingsPath, { auto_start: true, port: 16300 });
settings.device_id ||= crypto.randomBytes(26).toString('hex').toUpperCase();
atomic(settingsPath, JSON.stringify(settings, null, 2));
const PORT = Number(settings.port) || 16300;
if (PORT !== 16300) throw new Error('The bridge contract uses port 16300');
const ORIGIN = `http://127.0.0.1:${PORT}`;
const tokenPath = path.join(CONFIG, 'service-token');
if (!fs.existsSync(tokenPath)) atomic(tokenPath, crypto.randomBytes(32).toString('hex'));
const SERVICE_TOKEN = fs.readFileSync(tokenPath, 'utf8').trim();
if (SERVICE_TOKEN.length < 40) throw new Error('Invalid local service token');

function audit(event, fields = {}) {
  const line = JSON.stringify({ at: new Date().toISOString(), event, ...fields });
  const date = new Date().toISOString().slice(0, 10);
  fs.appendFileSync(path.join(LOG, `${date}.jsonl`), `${line}\n`, { mode: 0o600 });
}
// Upstream errors may embed HTTP cookies or request headers. Never forward arbitrary objects.
for (const key of ['log', 'info', 'warn', 'error', 'debug']) console[key] = () => {};
if (!fs.existsSync(path.join(TEMP, 'anonymous_token'))) atomic(path.join(TEMP, 'anonymous_token'), '');
global.deviceId = settings.device_id;
const UP = path.join(APP, 'upstream');
const request = require(path.join(UP, 'util/request'));
const names = ['login_qr_key', 'login_qr_create', 'login_qr_check', 'login_status',
  'listentogether_accept', 'listentogether_status', 'listentogether_room_check',
  'listentogether_heatbeat', 'listentogether_end', 'listentogether_sync_playlist_get'];
const modules = Object.fromEntries(names.map(name => [name, require(path.join(UP, 'module', `${name}.js`))]));

function protect(text, decrypt = false) {
  const action = decrypt ?
    "$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Security;$b=[Convert]::FromBase64String([Console]::In.ReadToEnd());$v=[Security.Cryptography.ProtectedData]::Unprotect($b,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser);[Console]::Write([Text.Encoding]::UTF8.GetString($v))" :
    "$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Security;$b=[Text.Encoding]::UTF8.GetBytes([Console]::In.ReadToEnd());$v=[Security.Cryptography.ProtectedData]::Protect($b,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser);[Console]::Write([Convert]::ToBase64String($v))";
  return execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', action],
    { input: text, encoding: 'utf8', windowsHide: true, timeout: 15000, stdio: ['pipe', 'pipe', 'pipe'] }).trim();
}
if (protect(protect('feiyu-storage-check'), true) !== 'feiyu-storage-check') throw new Error('Cookie storage self-check failed');
const cookiePath = path.join(CONFIG, 'cookie.dpapi');
let cookie = {};
if (fs.existsSync(cookiePath)) {
  try { cookie = JSON.parse(protect(fs.readFileSync(cookiePath, 'utf8'), true)); }
  catch { audit('cookie_unprotect_failed'); }
}
let user = readJson(path.join(CONFIG, 'account.json'), null);
let loginVerifiedAt = 0;
const statePath = path.join(CONFIG, 'room-state.json');
let state = readJson(statePath, { active: null, roomId: '', requests: {} });
state.requests ||= {};
function saveState() { atomic(statePath, JSON.stringify(state, null, 2)); }
function loggedIn() { return !!cookie.MUSIC_U; }
function normalizeCookies(items) {
  const out = {};
  const values = Array.isArray(items) ? items : String(items || '').split(/;(?=\s*[A-Za-z_][\w-]*=)/);
  for (const item of values) {
    const part = String(item).split(';', 1)[0];
    const i = part.indexOf('=');
    const key = part.slice(0, i).trim();
    if (i > 0 && !/^(Path|Domain|Expires|Max-Age|SameSite)$/i.test(key)) out[key] = part.slice(i + 1).trim();
  }
  return out;
}
async function call(name, data = {}, auth = true) {
  return modules[name]({ ...data, cookie: auth ? { ...cookie } : (data.cookie || {}), timeout: 12000 }, request);
}
function bodyOf(result) { return result && typeof result.body === 'object' ? result.body : {}; }
async function verifyLogin(force = false) {
  if (!loggedIn()) return { ok: true, logged_in: false };
  if (!force && user && Date.now() - loginVerifiedAt < 60000) return { ok: true, logged_in: true, user };
  try {
    const b = bodyOf(await call('login_status'));
    const profile = b.data?.profile || b.profile;
    const account = b.data?.account || b.account;
    const id = profile?.userId || account?.id;
    if (id) {
      user = { id: String(id), nickname: String(profile?.nickname || '').slice(0, 100) };
      loginVerifiedAt = Date.now();
      atomic(path.join(CONFIG, 'account.json'), JSON.stringify(user));
      return { ok: true, logged_in: true, user };
    }
    return { ok: true, logged_in: false, code: 'session_expired' };
  } catch { return { ok: false, logged_in: null, code: 'login_status_unknown' }; }
}

function roomIdOf(b) {
  const candidates = [b.roomId, b.data?.roomId, b.roomInfo?.roomId, b.data?.roomInfo?.roomId,
    b.data?.room?.roomId, b.data?.room?.id, b.room?.roomId];
  const ids = [...new Set(candidates.filter(v => v !== null && v !== undefined && String(v) !== '').map(String))];
  return ids.length === 1 ? ids[0] : '';
}
function playbackOf(b) {
  // Only copy a complete observed state. Never invent an empty song or a playback mode.
  for (const root of [b.data, b]) {
    if (!root || typeof root !== 'object') continue;
    const choices = [root.playInfo, root.playback, root.current, root.roomInfo?.playInfo, root.roomInfo, root];
    for (const value of choices) {
      if (!value || typeof value !== 'object') continue;
      const songId = String(value.songId ?? value.currentSongId ?? value.targetSongId ?? '');
      const playStatus = value.playStatus ?? value.status;
      const progress = value.progress;
      if (/^[1-9]\d{0,24}$/.test(songId) && ['PLAY', 'PAUSE'].includes(playStatus)
          && typeof progress === 'number' && Number.isFinite(progress) && progress >= 0) {
        return { songId, playStatus, progress: Math.floor(progress) };
      }
    }
  }
  return null;
}
function validRoom(value) { return typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value); }
function validUid(value) { return typeof value === 'string' && /^[1-9]\d{0,24}$/.test(value); }
function partnerPresence(b, expected, inviterHint = '') {
  const unknown = { partner_present: null, partner_evidence: 'unknown' };
  const selfId = String(user?.id || '');
  const inviterId = String(inviterHint || state.inviterId || '');
  // These are NetEase UIDs from the login profile and approved invitation, never QQ IDs.
  if (state.roomId !== expected || !validUid(selfId) || !validUid(inviterId) || selfId === inviterId) return unknown;
  if (inviterHint && state.inviterId && state.inviterId !== inviterHint) return unknown;
  if (String(b.data?.roomInfo?.roomId || '') !== expected) return unknown;
  const users = b.data?.roomInfo?.roomUsers;
  if (!Array.isArray(users) || users.length === 0) return unknown;
  const ids = [];
  for (const item of users) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return unknown;
    const id = item.userId;
    if ((typeof id !== 'string' && typeof id !== 'number')
        || (typeof id === 'number' && !Number.isSafeInteger(id)) || !validUid(String(id))) return unknown;
    ids.push(String(id));
  }
  // A missing self entry may be an incomplete projection, and cannot prove a peer left.
  if (!ids.includes(selfId) || new Set(ids).size !== ids.length) return unknown;
  return { partner_present: ids.includes(inviterId), partner_evidence: 'room_users' };
}
function response(ok, code, roomId, active, extra = {}) {
  const out = { ok, code, ...extra };
  if (roomId !== undefined) out.roomId = roomId;
  if (active !== undefined) out.state = { active };
  return out;
}
function managedRequestId(roomId) {
  if (state.requestId && state.requests[state.requestId]?.roomId === roomId
      && ['joined', 'unknown', 'attempting'].includes(state.requests[state.requestId].outcome)) return state.requestId;
  const rows = Object.entries(state.requests).filter(([, row]) => row?.roomId === roomId
    && ['joined', 'unknown', 'attempting'].includes(row.outcome));
  rows.sort((a, b) => String(b[1].at || '').localeCompare(String(a[1].at || '')));
  return rows[0]?.[0] || '';
}
async function currentRoom(expected = '', inviterHint = '') {
  const b = bodyOf(await call('listentogether_status'));
  if (b.code !== 200) return response(false, 'status_unknown', expected, undefined);
  const roomId = roomIdOf(b);
  // Only an explicit boolean or an actual matching room is accepted as evidence.
  const inRoom = b.inRoom ?? b.data?.inRoom ?? b.isInRoom ?? b.data?.isInRoom;
  if (inRoom === false) {
    state.active = false; saveState();
    const out = response(true, 'ok', expected, false);
    Object.assign(out.state, { partner_present: null, partner_evidence: 'not_in_room' });
    return out;
  }
  if (roomId && validRoom(roomId) && (!expected || roomId === expected) && inRoom === true) {
    if (state.roomId !== roomId) delete state.inviterId;
    const requestId = managedRequestId(roomId);
    if (!state.inviterId && validUid(inviterHint) && inviterHint !== String(user?.id || '')) state.inviterId = inviterHint;
    state.active = true; state.roomId = roomId; saveState();
    const out = response(true, 'ok', roomId, true, { requestId, playback: playbackOf(b) });
    Object.assign(out.state, partnerPresence(b, roomId, inviterHint));
    return out;
  }
  if (expected && roomId && roomId !== expected) return response(false, 'different_room', roomId, true);
  return response(false, 'status_unknown', expected, undefined);
}
let mutationBusy = false;
async function together(action, data) {
  const roomId = String(data.roomId || '');
  if (!validRoom(roomId)) return response(false, 'invalid_request', roomId, undefined, { attempted: false });
  if (!loggedIn()) return response(false, 'login_required', roomId, undefined, { attempted: false });
  if (action === 'status') {
    const inviterHint = data.inviterId === undefined ? '' : String(data.inviterId);
    if (inviterHint && !validUid(inviterHint)) return response(false, 'invalid_request', roomId, undefined, { attempted: false });
    try { return await currentRoom(roomId, inviterHint); } catch { return response(false, 'status_unknown', roomId, undefined); }
  }
  if (mutationBusy) return response(false, 'busy', roomId, undefined, { attempted: false });
  mutationBusy = true;
  try {
    if (action === 'accept') {
      const inviterId = String(data.inviterId || '');
      const requestId = String(data.requestId || '');
      if (!validUid(inviterId) || !/^[A-Za-z0-9_-]{8,128}$/.test(requestId))
        return response(false, 'invalid_request', roomId, undefined, { attempted: false });
      if (state.requests[requestId]) return response(false, 'request_already_attempted', roomId, undefined, { attempted: false });
      if (state.active !== false && state.roomId && state.roomId !== roomId)
        return response(false, 'existing_room_requires_end', state.roomId, state.active, { attempted: false });
      const login = await verifyLogin(true);
      if (!login.logged_in) return response(false, login.ok ? 'login_required' : 'login_status_unknown', roomId, undefined, { attempted: false });
      state.requests[requestId] = { roomId, at: new Date().toISOString(), outcome: 'attempting' };
      state.roomId = roomId; state.inviterId = inviterId; state.requestId = requestId; state.active = null; saveState();
      audit('accept_attempt', { requestId, roomId });
      try {
        const b = bodyOf(await call('listentogether_accept', { roomId, inviterId }));
        if (b.code !== 200) {
          state.requests[requestId].outcome = 'rejected'; saveState();
          return response(false, 'accept_rejected', roomId, undefined, { attempted: true });
        }
        // The join write is never retried. Read-back determines whether it really joined.
        const checked = await currentRoom(roomId);
        if (checked.ok && checked.state?.active === true) {
          state.requests[requestId].outcome = 'joined'; state.joinedAt = new Date().toISOString(); saveState();
          audit('joined', { roomId, requestId });
          const joined = response(true, 'joined', roomId, true, { attempted: true });
          Object.assign(joined.state, {
            partner_present: checked.state.partner_present,
            partner_evidence: checked.state.partner_evidence,
          });
          return joined;
        }
      } catch {}
      state.requests[requestId].outcome = 'unknown'; saveState();
      audit('accept_unknown', { roomId, requestId });
      return response(false, 'accept_unknown', roomId, undefined, { attempted: true });
    }
    if (action === 'heartbeat') {
      if (state.active !== true || state.roomId !== roomId)
        return response(false, 'room_not_verified', roomId, undefined, { attempted: false });
      const observed = await currentRoom(roomId);
      if (!observed.ok || observed.state?.active !== true)
        return response(false, 'room_not_verified', roomId, undefined, { attempted: false });
      let playback = observed.playback;
      if (!playback) {
        const sync = bodyOf(await call('listentogether_sync_playlist_get', { roomId }));
        // Observed classic API schema: data.playCommand contains targetSongId/progress/playStatus.
        if (sync.code === 200) playback = playbackOf({ data: { playInfo: sync.data?.playCommand } });
        if (!playback)
          return response(false, 'heartbeat_state_unavailable', roomId, undefined, { attempted: false });
        const stillHere = await currentRoom(roomId);
        if (!stillHere.ok || stillHere.state?.active !== true)
          return response(false, 'room_not_verified', roomId, undefined, { attempted: false });
      }
      // Presence only: repeat observed values without sending any playback-control command.
      const b = bodyOf(await call('listentogether_heatbeat', { roomId, ...playback }));
      // The live classic API acknowledges heartbeats with data.result, not data.success.
      if (b.code === 200 && (b.data?.result === false || b.data?.success === false || b.success === false))
        return response(false, 'heartbeat_rejected', roomId, undefined);
      if (b.code === 200 && (b.data?.result === true || b.data?.success === true || b.success === true)) {
        state.lastHeartbeatConfirmedAt = new Date().toISOString();
        state.heartbeatConfirmedCount = Number.isSafeInteger(state.heartbeatConfirmedCount)
          ? state.heartbeatConfirmedCount + 1 : 1;
        saveState();
        return response(true, 'heartbeat', roomId, true, { heartbeat_confirmed: true });
      }
      if (b.code === 200 && b.data?.success !== false && b.success !== false) {
        const checked = await currentRoom(roomId);
        if (checked.ok && checked.state?.active === true)
          return response(false, 'heartbeat_unconfirmed', roomId, true, { heartbeat_confirmed: false });
        return response(false, 'heartbeat_unknown', roomId, undefined);
      }
      return response(false, 'heartbeat_rejected', roomId, undefined);
    }
    if (action === 'end') {
      const b = bodyOf(await call('listentogether_end', { roomId }));
      if (b.code === 200 && (b.data?.success === true || b.success === true)) {
        const checked = await currentRoom(roomId);
        if (checked.ok && checked.state?.active === false) {
          audit('ended', { roomId });
          return response(true, 'ended', roomId, false);
        }
      }
      return response(false, 'end_unknown', roomId, undefined);
    }
    return response(false, 'invalid_request', roomId, undefined, { attempted: false });
  } catch {
    return response(false, `${action}_unknown`, roomId, undefined);
  } finally { mutationBusy = false; }
}

const browserSessions = new Map();
function browserAllowed(req) {
  if (req.headers.origin !== ORIGIN || req.headers['content-type']?.split(';', 1)[0] !== 'application/json') return null;
  const token = req.headers['x-login-nonce'];
  const s = browserSessions.get(token);
  if (!s || Date.now() - s.created > 30 * 60 * 1000) return null;
  return s;
}
function authorized(req) {
  const value = req.headers.authorization || '';
  const expected = `Bearer ${SERVICE_TOKEN}`;
  return value.length === expected.length && crypto.timingSafeEqual(Buffer.from(value), Buffer.from(expected));
}
function send(res, status, data) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store',
    'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer' });
  res.end(JSON.stringify(data));
}
function jsonBody(req) {
  return new Promise((resolve, reject) => {
    let text = '';
    req.on('data', chunk => { text += chunk; if (text.length > 8192) { reject(new Error('size')); req.destroy(); } });
    req.on('end', () => { try { resolve(JSON.parse(text || '{}')); } catch { reject(new Error('json')); } });
    req.on('error', reject);
  });
}
function loginHtml(nonce) {
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>肥鱼 · 网易云一起听登录</title>
<style>body{font:16px/1.8 system-ui,sans-serif;color:#263141;background:#f5f8fc;max-width:640px;margin:8vh auto;padding:24px}main{background:white;border-radius:20px;padding:32px;box-shadow:0 5px 40px #20304010}h1{font-size:25px}img{display:block;width:260px;height:260px;margin:20px auto}button{background:#f05361;color:white;border:0;border-radius:10px;padding:12px 20px;cursor:pointer}small{color:#68788c}#status{font-weight:600}</style>
<main><h1>肥鱼的网易云一起听</h1><p>此服务仅供已授权用户使用。</p><p id="status">正在检查登录…</p><img id="qr" hidden alt="网易云登录二维码"><button id="refresh">刷新二维码</button><p id="account"></p><small>本页仅在这台电脑打开。登录凭据使用 Windows 当前用户保护，网页和 QQ 均不会收到登录凭据。二维码会自动刷新。</small></main>
<script nonce="${nonce}">const nonce=${JSON.stringify(nonce)};let timer;async function post(p){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json','X-Login-Nonce':nonce},body:'{}'});return r.json()}async function refresh(){clearTimeout(timer);document.querySelector('#status').textContent='正在生成二维码…';try{const r=await post('/login/qr');if(r.qrimg){document.querySelector('#qr').src=r.qrimg;document.querySelector('#qr').hidden=false;document.querySelector('#status').textContent='请用网易云音乐 App 扫码并确认';timer=setTimeout(poll,2500)}else{document.querySelector('#status').textContent='暂时无法生成，请稍后刷新';timer=setTimeout(refresh,15000)}}catch{document.querySelector('#status').textContent='服务连接失败，请刷新本页'}}async function poll(){try{const r=await post('/login/poll');if(r.logged_in){document.querySelector('#qr').hidden=true;document.querySelector('#status').textContent='登录成功，一起听已就绪';document.querySelector('#account').textContent=r.user?'当前账号：'+r.user.nickname+'（'+r.user.id+'）':'';return}if(r.code==='expired'){return refresh()}document.querySelector('#status').textContent=r.code==='scanned'?'已扫码，请在手机上确认':'等待扫码确认…';timer=setTimeout(poll,2500)}catch{timer=setTimeout(poll,5000)}}document.querySelector('#refresh').onclick=refresh;post('/login/page-status').then(r=>{if(r.logged_in){document.querySelector('#status').textContent='已登录，一起听已就绪';document.querySelector('#account').textContent=r.user?'当前账号：'+r.user.nickname+'（'+r.user.id+'）':''}else{refresh()}}).catch(refresh);</script></html>`;
}
const server = http.createServer(async (req, res) => {
  try {
    if (req.headers.host !== `127.0.0.1:${PORT}`) return send(res, 403, { ok: false, code: 'invalid_host' });
    const route = new URL(req.url, ORIGIN).pathname;
    if (req.method === 'GET' && route === '/health') return send(res, 200, { ok: true, service: 'feiyu-netease-listen', version: '1.0.0', logged_in: loggedIn() });
    if (req.method === 'GET' && (route === '/' || route === '/login')) {
      const nonce = crypto.randomBytes(24).toString('hex');
      for (const [key, s] of browserSessions) if (Date.now() - s.created > 30 * 60 * 1000) browserSessions.delete(key);
      browserSessions.set(nonce, { created: Date.now(), key: '', loginCookie: [] });
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store',
        'content-security-policy': `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`,
        'referrer-policy': 'no-referrer', 'x-content-type-options': 'nosniff' });
      return res.end(loginHtml(nonce));
    }
    if (route === '/login/status' && req.method === 'GET') {
      if (!authorized(req)) return send(res, 401, { ok: false, code: 'unauthorized' });
      return send(res, 200, await verifyLogin());
    }
    if (route === '/together/current' && req.method === 'GET') {
      if (!authorized(req)) return send(res, 401, { ok: false, code: 'unauthorized' });
      if (!loggedIn()) return send(res, 200, { ok: false, code: 'login_required' });
      try { return send(res, 200, await currentRoom()); }
      catch { return send(res, 200, { ok: false, code: 'status_unknown' }); }
    }
    if (route.startsWith('/together/') && req.method === 'POST') {
      if (!authorized(req)) return send(res, 401, { ok: false, code: 'unauthorized' });
      if (req.headers['content-type']?.split(';', 1)[0] !== 'application/json') return send(res, 415, { ok: false, code: 'json_required' });
      const action = route.slice('/together/'.length);
      if (!['accept', 'status', 'heartbeat', 'end'].includes(action)) return send(res, 404, { ok: false, code: 'not_found' });
      return send(res, 200, await together(action, await jsonBody(req)));
    }
    if (route.startsWith('/login/') && req.method === 'POST') {
      const s = browserAllowed(req);
      if (!s) return send(res, 403, { ok: false, code: 'invalid_login_session' });
      if (route === '/login/page-status') return send(res, 200, await verifyLogin());
      if (route === '/login/qr') {
        const keyResult = await call('login_qr_key', {}, false);
        const key = bodyOf(keyResult).data?.unikey;
        if (!key) return send(res, 502, { ok: false, code: 'qr_unavailable' });
        const image = bodyOf(await call('login_qr_create', { key, qrimg: true }, false)).data?.qrimg;
        s.key = key; s.qrAt = Date.now(); s.loginCookie = keyResult.cookie || [];
        return send(res, 200, { ok: true, qrimg: image });
      }
      if (route === '/login/poll') {
        if (!s.key || Date.now() - s.qrAt > 175000) return send(res, 200, { ok: false, code: 'expired' });
        const result = await call('login_qr_check', { key: s.key, cookie: normalizeCookies(s.loginCookie) }, false);
        const b = bodyOf(result);
        if (b.code === 803) {
          const nextCookie = { ...normalizeCookies(s.loginCookie), ...normalizeCookies(result.cookie), ...normalizeCookies(b.cookie) };
          if (!nextCookie.MUSIC_U) return send(res, 200, { ok: false, code: 'login_unconfirmed' });
          const encrypted = protect(JSON.stringify(nextCookie));
          if (protect(encrypted, true) !== JSON.stringify(nextCookie)) throw new Error('Cookie storage verification failed');
          atomic(cookiePath, encrypted);
          cookie = nextCookie;
          s.key = ''; loginVerifiedAt = 0;
          const status = await verifyLogin(true);
          audit('login_saved', { verified: status.logged_in === true });
          return send(res, 200, status);
        }
        return send(res, 200, { ok: true, logged_in: false, code: b.code === 800 ? 'expired' : b.code === 802 ? 'scanned' : 'waiting' });
      }
    }
    return send(res, 404, { ok: false, code: 'not_found' });
  } catch { audit('request_failed'); if (!res.headersSent) send(res, 502, { ok: false, code: 'upstream_unavailable' }); }
});
server.headersTimeout = 10000;
server.requestTimeout = 20000;
if (require.main === module) server.listen(PORT, '127.0.0.1', () => audit('started', { port: PORT }));
module.exports = { server, together, roomIdOf, playbackOf, normalizeCookies, verifyLogin };
