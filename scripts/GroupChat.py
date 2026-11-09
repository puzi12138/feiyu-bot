"""Enable, disable, or inspect the configured group speaking task."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

root = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument('action', choices=('status', 'on', 'off'), default='status', nargs='?')
args = parser.parse_args()
try:
    credentials = json.loads((root / 'config/bootstrap.json').read_text(encoding='utf-8-sig'))
    record = json.loads((root / 'config/group-0.json').read_text(encoding='utf-8'))
    base = 'http://127.0.0.1:6185/api/v1'
    request = Request(base + '/auth/login', data=json.dumps({'username':'astrbot', 'password':credentials['astrbot_password']}).encode(),
                      headers={'Content-Type':'application/json'})
    with urlopen(request, timeout=15) as response:
        token = json.load(response)['data']['token']

    def call(method, path, data=None):
        request = Request(base + path, method=method,
                          data=json.dumps(data).encode() if data is not None else None,
                          headers={'Authorization':'Bearer ' + token, 'Content-Type':'application/json'})
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    if args.action in ('on', 'off'):
        result = call('PATCH', '/cron/jobs/' + record['job_id'], {'enabled':args.action == 'on'})
        if result.get('status') != 'ok':
            raise RuntimeError(result.get('message'))
    jobs = call('GET', '/cron/jobs')['data']
    job = next(j for j in jobs if j['job_id'] == record['job_id'])
    print('群 0 定时发言：' + ('开启' if job['enabled'] else '关闭'))
    print('频率：每小时整点和半点各一条，不受群消息重置。')
    if job.get('next_run_time') and job['enabled']:
        next_run = datetime.fromisoformat(job['next_run_time']).astimezone(timezone(timedelta(hours=8)))
        print('下一次：' + next_run.strftime('%Y-%m-%d %H:%M:%S'))
    print('被艾特回复不受这个定时发言开关影响。')
except Exception as error:
    print('管理失败，请先启动机器人。错误：' + str(error))
    raise SystemExit(1)
