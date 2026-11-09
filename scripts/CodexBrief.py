"""Codex completion -> Feiyu owner-only QQ brief. No model/API key required."""
from __future__ import annotations

import contextlib
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path('D:/QQBot')
SETTINGS = ROOT / 'config/codex-brief.json'
DATA = ROOT / 'data/codex-brief'
AUDIT = ROOT / 'logs/codex-brief.jsonl'
OWNER = '0'
BOT = '0'
LOCAL_HTTP = build_opener(ProxyHandler({}))


def config():
    return json.loads(SETTINGS.read_text(encoding='utf-8-sig'))


def audit(event, **fields):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    # No raw input, response body, credentials, URLs, or message text in logs.
    row = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': event, **fields}
    with AUDIT.open('a', encoding='utf-8') as file:
        file.write(json.dumps(row, ensure_ascii=False) + '\n')


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'queue.sqlite3', timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE IF NOT EXISTS events (
        event_key TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
        title TEXT NOT NULL, brief TEXT NOT NULL, state TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, next_attempt REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, message_id TEXT, reason TEXT
    )''')
    db.commit()
    return db


def root_session(thread_id, settings):
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', thread_id):
        return False, 'invalid_thread'
    base = Path(settings['sessions_path'])
    for path in base.glob(f'**/*{thread_id}*.jsonl'):
        try:
            with path.open('r', encoding='utf-8') as file:
                raw = file.readline(2_000_000)
            meta = json.loads(raw)
            payload = meta.get('payload', {})
            if meta.get('type') != 'session_meta' or payload.get('id') != thread_id:
                continue
            source = payload.get('source')
            if (isinstance(source, dict) and 'subagent' in source
                    or payload.get('parent_thread_id') or payload.get('agent_path')):
                return False, 'subagent'
            return True, 'root'
        except (OSError, ValueError):
            continue
    # Fail closed rather than notify for unknown/background agents.
    return False, 'session_not_found'


def clean(text, limit=380):
    text = str(text or '')
    text = re.sub(r'```[\s\S]*?(?:```|$)', '[代码略]', text)
    text = re.sub(r'<(?:environment_context|in-app-browser-context|send_user_message_question_reply)\b[^>]*>[\s\S]*?</(?:environment_context|in-app-browser-context|send_user_message_question_reply)>', '', text)
    # Do not send keys/cookies/authorization even if a final answer quotes them.
    text = re.sub(r'(?im)^.*(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|authorization|cookie|password|密码|密钥|令牌)\s*[:=：].*$', '[敏感配置已省略]', text)
    text = re.sub(r'(?i)\b[a-z0-9_-]*(?:token|secret|api[_-]?key|password|cookie)[a-z0-9_-]*["\x27]?\s*[:=]\s*["\x27]?[^\s,;，；"\x27]+', '[敏感配置已省略]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9_.~+/-]+=*', '[凭据已省略]', text)
    text = re.sub(r'\b(?:agt_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]{15,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b', '[凭据已省略]', text)
    text = re.sub(r'\[([^\]]+)\]\([^\n]*\)', r'\1', text)
    text = re.sub(r'https?://[^\s<>]+', '[链接]', text)
    text = re.sub(r'(?i)[A-Z]:[/\\][^\s<>，。；]+', '[本机文件]', text)
    text = re.sub(r'<[^>]*>', '', text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r'^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)、]\s*)', '', line)
        line = re.sub(r'[*`_]', '', line).strip()
        if line and not line.startswith(('::', '|', '# Files', 'Image attachment:')):
            lines.append(line)
    result = '\n'.join(lines[:6]).strip()
    if len(result) > limit:
        result = result[:limit - 1].rstrip() + '…'
    return result


def build_brief(event):
    raw = str(event.get('last-assistant-message') or '')
    summary = clean(raw)
    if not summary:
        return None, 'empty_result'
    status = str(event.get('status') or event.get('turn-status') or 'completed').lower()
    if status not in ('completed', 'complete', 'success', 'succeeded'):
        return None, 'not_completed'
    # Do not call a request to log in/approve/provide missing data a finished job.
    pending = re.search(r'(?:请|需要你|等你|等待你|还需你).{0,24}(?:扫码|登录|授权|批准|确认后|提供.{0,8}(?:账号|密钥|文件)|选择.{0,6}(?:后|再))', summary)
    if pending:
        return None, 'waiting_for_user'
    inputs = event.get('input-messages') or []
    if isinstance(inputs, str):
        inputs = [inputs]
    candidate = str(inputs[-1]) if inputs else ''
    if '## My request:' in candidate:
        candidate = candidate.rsplit('## My request:', 1)[-1]
    title = clean(candidate, 48).split('\n')[0].strip() if clean(candidate, 48) else ''
    if not title or title.startswith(('<', '#', 'Another language model')):
        title = '任务结果'
    return {'title': title, 'brief': summary}, None


def enqueue(event, settings=None):
    settings = settings or config()
    if not settings.get('enabled', True) or event.get('type') != 'agent-turn-complete':
        return 'ignored'
    thread_id = str(event.get('thread-id') or '')
    turn_id = str(event.get('turn-id') or '')
    if not turn_id or len(turn_id) > 160:
        return 'missing_turn'
    allowed, reason = root_session(thread_id, settings)
    if not allowed:
        audit('filtered', reason=reason, thread=thread_id)
        return reason
    item, reason = build_brief(event)
    if item is None:
        audit('filtered', reason=reason, thread=thread_id, turn=turn_id)
        return reason
    event_key = hashlib.sha256(f'{thread_id}:{turn_id}'.encode()).hexdigest()
    now = time.time()
    with contextlib.closing(connect()) as db, db:
        changed = db.execute('''INSERT OR IGNORE INTO events
            (event_key,thread_id,turn_id,title,brief,state,created,updated,next_attempt)
            VALUES(?,?,?,?,?,'pending',?,?,?)''',
            (event_key, thread_id, turn_id, item['title'], item['brief'], now, now, now)).rowcount
    audit('queued' if changed else 'duplicate', key=event_key[:16])
    return 'queued' if changed else 'duplicate'


def http(path, payload, token=None, timeout=15):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request('http://127.0.0.1:5099' + path, method='POST',
                      data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
    with LOCAL_HTTP.open(request, timeout=timeout) as response:
        return json.load(response)


class NotSent(Exception):
    pass


def login():
    credentials = json.loads((ROOT / 'config/bootstrap.json').read_text(encoding='utf-8-sig'))
    response = http('/api/login', {'password': credentials['snowluma_password']})
    token = response.get('token')
    if not token:
        raise NotSent('login_unavailable')
    response = http('/api/debug/invoke', {'uin': BOT, 'action': 'get_status', 'params': {}}, token)
    if response.get('retcode') != 0 or not response.get('data', {}).get('online'):
        raise NotSent('qq_offline')
    return token


def render(row):
    return f"主人，Codex 刚忙完一项，我来报个信～\n【{row['title']}】\n{row['brief']}"


def send(row, token):
    response = http('/api/debug/invoke', {
        'uin': BOT, 'action': 'send_private_msg', 'params': {
            'user_id': int(OWNER),
            'message': [{'type': 'text', 'data': {'text': render(row)}}],
        }}, token, timeout=35)
    message_id = (response.get('data') or {}).get('message_id')
    if response.get('retcode') == 0 and message_id is not None:
        return str(message_id)
    # An explicit offline rejection is safe to retry; unknown receipts are not.
    raise RuntimeError('send_receipt_unconfirmed')


@contextlib.contextmanager
def worker_lock(name='worker.lock'):
    import msvcrt
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / name).open('a+b') as file:
        file.seek(0)
        if not file.read(1):
            file.write(b'0')
            file.flush()
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def flush():
    settings = config()
    if not settings.get('enabled', True):
        return
    with worker_lock() as acquired:
        if not acquired:
            return
        with contextlib.closing(connect()) as db:
            now = time.time()
            # A process may have died after QQ accepted the message. Never resend it blindly.
            db.execute("UPDATE events SET state='uncertain',reason='worker_interrupted',updated=? WHERE state='sending'", (now,))
            db.commit()
            rows = db.execute("SELECT * FROM events WHERE state='pending' AND next_attempt<=? ORDER BY created LIMIT 10", (now,)).fetchall()
            for row in rows:
                if now - row['created'] > 86400:
                    db.execute("UPDATE events SET state='expired',updated=?,reason='older_than_24h' WHERE event_key=?", (now, row['event_key']))
                    db.commit()
                    continue
                try:
                    token = login()
                except Exception as error:
                    retry(db, row, type(error).__name__)
                    break
                db.execute("UPDATE events SET state='sending',updated=?,attempts=attempts+1 WHERE event_key=?", (time.time(), row['event_key']))
                db.commit()
                try:
                    message_id = send(row, token)
                except NotSent as error:
                    retry(db, row, str(error))
                    continue
                except Exception as error:
                    # Timeout/connection loss after sending is ambiguous. Avoid double notices.
                    db.execute("UPDATE events SET state='uncertain',updated=?,reason=? WHERE event_key=?", (time.time(), type(error).__name__, row['event_key']))
                    db.commit()
                    audit('uncertain', key=row['event_key'][:16], reason=type(error).__name__)
                    continue
                db.execute("UPDATE events SET state='sent',updated=?,message_id=?,reason=NULL WHERE event_key=?", (time.time(), message_id, row['event_key']))
                db.commit()
                audit('sent', key=row['event_key'][:16], message_id=message_id)
            # Keep only the brief locally; dedupe ledger survives for 90 days.
            db.execute("UPDATE events SET title='',brief='' WHERE state!='pending' AND updated<?", (time.time() - 7 * 86400,))
            db.execute('DELETE FROM events WHERE updated<?', (time.time() - 90 * 86400,))
            db.commit()


def retry(db, row, reason):
    attempts = row['attempts'] + 1
    delay = (30, 120, 300, 900, 3600, 10800)[min(attempts - 1, 5)]
    state = 'failed' if attempts >= 8 else 'pending'
    db.execute('UPDATE events SET state=?,updated=?,next_attempt=?,attempts=?,reason=? WHERE event_key=?',
               (state, time.time(), time.time() + delay, attempts, reason[:80], row['event_key']))
    db.commit()
    audit('deferred' if state == 'pending' else 'failed', key=row['event_key'][:16], reason=reason[:80])


def start_flush():
    executable = Path(sys.executable).with_name('pythonw.exe')
    subprocess.Popen([str(executable if executable.exists() else sys.executable), str(Path(__file__).resolve()), '--flush'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def file_meta(path):
    try:
        with path.open('rb') as file:
            meta = json.loads(file.readline(2_000_000))
        if meta.get('type') != 'session_meta':
            return None
        payload = meta.get('payload', {})
        source = payload.get('source')
        child = (isinstance(source, dict) and 'subagent' in source
                 or payload.get('parent_thread_id') or payload.get('agent_path'))
        return {'thread': payload['id'], 'root': not bool(child)}
    except (OSError, ValueError, KeyError):
        return None


def scan(settings):
    """Incremental desktop fallback for sessions that predate notify setup.

    A persistent EOF baseline prevents historical replay. Only event_msg rows
    are interpreted; tool inputs/outputs and assistant reasoning are ignored.
    """
    with contextlib.closing(connect()) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS cursors (
            path TEXT PRIMARY KEY, offset INTEGER NOT NULL, thread_id TEXT NOT NULL,
            is_root INTEGER NOT NULL, title TEXT NOT NULL DEFAULT ''
        )''')
        db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT)')
        first_run = db.execute("SELECT value FROM metadata WHERE key='baseline_at'").fetchone() is None
        for path in Path(settings['sessions_path']).glob('**/*.jsonl'):
            key = str(path)
            row = db.execute('SELECT * FROM cursors WHERE path=?', (key,)).fetchone()
            if row is None:
                meta = file_meta(path)
                if meta is None:
                    continue
                size = path.stat().st_size
                offset = size if first_run else 0
                db.execute('INSERT INTO cursors(path,offset,thread_id,is_root) VALUES(?,?,?,?)',
                           (key, offset, meta['thread'], int(meta['root'])))
                db.commit()
                row = db.execute('SELECT * FROM cursors WHERE path=?', (key,)).fetchone()
            if not row['is_root']:
                continue
            size = path.stat().st_size
            offset = row['offset']
            if offset >= size:
                continue
            title = row['title']
            start = offset
            with path.open('rb') as file:
                file.seek(offset)
                while file.tell() - start < 4_000_000:
                    line = file.readline(16_000_001)
                    if not line:
                        break
                    if not line.endswith(b'\n'):
                        if len(line) >= 16_000_001:
                            # Oversized tool/media rows are irrelevant; skip with bounded memory.
                            while True:
                                tail = file.readline(1_000_000)
                                if not tail or tail.endswith(b'\n'):
                                    break
                            if tail.endswith(b'\n'):
                                offset = file.tell()
                                continue
                        # An unfinished append must be read again next scan.
                        break
                    offset = file.tell()
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get('type') != 'event_msg':
                        continue
                    payload = record.get('payload') or {}
                    if payload.get('type') == 'user_message':
                        message = str(payload.get('message') or '')
                        if '## My request:' in message:
                            message = message.rsplit('## My request:', 1)[-1]
                        title = clean(message, 48).split('\n')[0]
                    elif payload.get('type') == 'task_complete':
                        attribution = payload.get('turn_attribution') or {}
                        if attribution.get('initiating_agent_path'):
                            continue
                        try:
                            # Precise ISO time avoids losing a finish in the installation second.
                            completed = datetime.fromisoformat(str(record.get('timestamp', '')).replace('Z', '+00:00')).timestamp()
                            precision = 0
                        except ValueError:
                            completed = payload.get('completed_at', 0)
                            precision = 1
                        if not completed:
                            continue
                        # A newly discovered old rollover is not new work.
                        baseline = db.execute("SELECT value FROM metadata WHERE key='baseline_at'").fetchone()
                        if baseline and float(completed) + precision < float(baseline['value']):
                            continue
                        enqueue({
                            'type': 'agent-turn-complete', 'thread-id': row['thread_id'],
                            'turn-id': payload.get('turn_id') or payload.get('root_turn_id'),
                            'last-assistant-message': payload.get('last_agent_message', ''),
                            'input-messages': [title],
                        }, settings)
            db.execute('UPDATE cursors SET offset=?,title=? WHERE path=?', (offset, title, key))
            db.commit()
        if first_run:
            db.execute("INSERT INTO metadata VALUES('baseline_at',?)", (str(time.time()),))
            db.commit()
            audit('watch_baseline', files=db.execute('SELECT count(*) FROM cursors').fetchone()[0])


def watch():
    with worker_lock('watch.lock') as acquired:
        if not acquired:
            return
        audit('watch_started', pid=os.getpid())
        last_health = 0
        while True:
            try:
                settings = config()
                if settings.get('enabled', True):
                    scan(settings)
                    flush()
                if time.time() - last_health >= 30:
                    health = ROOT / 'run/codex-brief-watch-health.json'
                    health.write_text(json.dumps({'pid': os.getpid(), 'heartbeat': time.time()}), encoding='utf-8')
                    last_health = time.time()
            except Exception as error:
                audit('watch_error', reason=type(error).__name__)
            time.sleep(5)


def main():
    if len(sys.argv) == 2 and sys.argv[1] == '--watch':
        watch()
        return
    if len(sys.argv) == 2 and sys.argv[1] == '--baseline':
        scan(config())
        return
    if len(sys.argv) == 2 and sys.argv[1] == '--flush':
        flush()
        return
    if len(sys.argv) != 2:
        return
    payload = json.loads(sys.argv[1])
    previous = config().get('chained_notify') or []
    if previous:
        try:
            subprocess.Popen([*previous, sys.argv[1]], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as error:
            audit('previous_notify_error', reason=type(error).__name__)
    if enqueue(payload) in ('queued', 'duplicate'):
        start_flush()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Notification errors must not fail or block the Codex task.
        try:
            audit('bridge_error', reason=type(error).__name__)
        except Exception:
            pass
