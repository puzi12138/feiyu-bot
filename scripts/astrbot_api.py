import json
import urllib.request
from pathlib import Path

base = 'http://127.0.0.1:6185/api/v1'
creds = json.loads(Path('D:/QQBot/config/bootstrap.json').read_text(encoding='utf-8-sig'))
request = urllib.request.Request(base + '/auth/login', data=json.dumps({'username':'astrbot','password':creds['astrbot_password']}).encode(), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request, timeout=15) as response:
    token = json.load(response)['data']['token']


def api(method, path, payload=None, timeout=90):
    request = urllib.request.Request(base + path, method=method, data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None, headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=timeout) as response:
        return json.load(response)
