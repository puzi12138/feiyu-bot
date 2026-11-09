"""Bridge dsh-bili-scroll's vendored MCP into AstrBot without a DSH host."""

import asyncio
import importlib.util
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
HOME = ROOT / 'data/dsh-bili-scroll/bili-duty'
SOURCE_CONFIG = ROOT / 'data/astrbot/config/astrbot_plugin_bilibili_ai_bot_config.json'
READ_TOOLS = ('bili_search', 'bili_subtitle', 'bili_comments', 'bili_video_info')
os.environ['BILI_MCP_TOOLS'] = ','.join(READ_TOOLS)
os.environ['DSH_HOME'] = str(HOME.parent)
spec = importlib.util.spec_from_file_location('dsh_bili_vendor', HOME / 'mcp_server.py')
vendor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vendor)
credential_stamp = None
credential_cache = None
report_lock = asyncio.Lock()


def load_shared_credential():
    """Reuse the existing local Bilibili login and reload it after refresh."""
    global credential_stamp, credential_cache
    stamp = SOURCE_CONFIG.stat().st_mtime_ns
    if credential_stamp != stamp:
        data = json.loads(SOURCE_CONFIG.read_text(encoding='utf-8-sig'))
        credential_cache = vendor.Credential(
            sessdata=data.get('SESSDATA') or '', bili_jct=data.get('BILI_JCT') or '',
            buvid3=data.get('BUVID3') or '', buvid4=data.get('BUVID4') or '',
            dedeuserid=str(data.get('DEDE_USER_ID') or ''),
            ac_time_value=data.get('REFRESH_TOKEN') or '',
        ) if data.get('SESSDATA') else None
        credential_stamp = stamp
    return credential_cache


vendor.load_credential = load_shared_credential


@vendor.mcp.tool()
async def bili_scroll_report(action: str = 'status', markdown: str = '') -> str:
    """Manage the local Bilibili curation report and seven-day recommendation deduplication.

    Args:
        action: status returns remaining daily quota and seen BV IDs; latest reads
            the latest report; save archives a completed recommendation report.
        markdown: For save only, give a report with one heading per selected video,
            exactly '### BVxxxxxxxxxx', followed by title, URL and a reason based
            on the actual retrieved subtitle. Do not include cookies or private chats.

    Returns:
        JSON containing report text, filename or quota/deduplication information.
        Missing subtitles should be skipped and counted in the report, not invented.
    """
    async with report_lock:
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        today = now.date().isoformat()
        config = json.loads((HOME / 'config.json').read_text(encoding='utf-8'))
        limit = max(0, int(config.get('per_day', 5)))
        state_path = HOME / 'report-state.json'
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'reports': []}
        records = state['reports']
        daily = [r for r in records if r['date'] == today]
        used = sum(len(r['bvids']) for r in daily)
        cutoff = (now.date() - timedelta(days=6)).isoformat()
        seen = {bv for r in records if r['date'] >= cutoff for bv in r['bvids']}
        if action == 'status':
            return json.dumps({'daily_limit': limit, 'selected_today': used,
                               'remaining': max(0, limit - used), 'seen_bvids': sorted(seen),
                               'reports_today': [r['filename'] for r in daily]}, ensure_ascii=False)
        if action == 'latest':
            if not records:
                return json.dumps({'message': '还没有生成过刷B站报告。'}, ensure_ascii=False)
            last = records[-1]
            return json.dumps({'filename': last['filename'],
                               'markdown': (HOME / 'out' / last['filename']).read_text(encoding='utf-8')}, ensure_ascii=False)
        if action != 'save':
            return json.dumps({'error': 'action must be status, latest or save'})
        if len(markdown) > 24000:
            return json.dumps({'error': '报告过长，请控制在 24000 字符以内。'}, ensure_ascii=False)
        bvids = re.findall(r'^###\s+(BV[0-9A-Za-z]{10})\s*$', markdown, flags=re.MULTILINE)
        if not bvids:
            return json.dumps({'error': '每条入选视频必须有单独的 ### BV号 标题。'}, ensure_ascii=False)
        if len(set(bvids)) != len(bvids) or seen.intersection(bvids):
            return json.dumps({'error': '报告含重复推荐，请根据 status 返回的 seen_bvids 去重。'}, ensure_ascii=False)
        if used + len(bvids) > limit:
            return json.dumps({'error': '超过每日入选上限。', 'remaining': max(0, limit - used)}, ensure_ascii=False)
        if re.search(r'(?i)(SESSDATA|bili_jct|buvid3|REFRESH_TOKEN)\s*[:=]', markdown):
            return json.dumps({'error': '报告包含凭据字段，拒绝保存。'}, ensure_ascii=False)
        filename = now.strftime('%Y-%m-%d-%H%M%S-%f') + '.md'
        target = HOME / 'out' / filename
        temporary = target.with_suffix('.tmp')
        temporary.write_text(markdown.rstrip() + '\n', encoding='utf-8')
        temporary.replace(target)
        records.append({'date': today, 'filename': filename, 'bvids': bvids})
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(state_path)
        return json.dumps({'saved': True, 'filename': filename, 'selected': len(bvids),
                           'remaining': limit - used - len(bvids)}, ensure_ascii=False)


if __name__ == '__main__':
    vendor.mcp.run(transport='stdio')
