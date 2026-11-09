"""Official-period report delivery with durable intent and no unknown retries."""
import asyncio
import hashlib
import re
from contextlib import nullcontext
from datetime import datetime

from .native_private_transport import native_delivery, producer_operation
from .official_token_query import MODE, unavailable
from .admin_info import owner_private
from .cockpit_token_report import MODE as COCKPIT_MODE, report_readiness
from .policy import TZ
from .peak_policy import automatic_peak_paused
from .local_feature_query import MODE as FEATURE_MODE, report_ready as feature_report_ready
from .combined_token_report import MODE as COMBINED_MODE, report_ready as combined_report_ready
from .token_report_delivery import LEGACY_QUEUE_STATE, QUEUE_STATE, _body, _receipt_response
from .token_report_queue import queue_key
from .bill_display_policy import display_bill_text
from .peak_runtime import owner_scope_active

OWNER = '0'
BOT_QQ = '0'


class OfficialTokenDelivery:
    async def send_official_token_report(self, event, result, *, source='manual', daily_day=None, dry_run=False):
        if source not in ('manual', 'daily') or source == 'manual' and not owner_private(event):
            return {'ok': False, 'sent': False, 'queued': False, 'status': 'owner_private_only'}
        if source == 'daily' and automatic_peak_paused():
            return {'ok': True, 'sent': False, 'queued': False, 'status': 'peak_tariff'}
        mode = result.get('report_mode')
        if result.get('ok') is not True or mode not in (MODE, COCKPIT_MODE, FEATURE_MODE, COMBINED_MODE):
            return result
        summary = result.get('summary')
        if mode == MODE and (not isinstance(summary, dict) or summary.get('key_filter') != 'all' or summary.get('fresh') is not True):
            return unavailable()
        if mode == COCKPIT_MODE and (source != 'manual' or not report_readiness(summary)['ready']):
            return {'ok': False, 'sent': False, 'queued': False, 'status': 'local_report_unavailable'}
        if mode == FEATURE_MODE and not feature_report_ready(result):
            return {'ok': False, 'sent': False, 'queued': False, 'status': 'local_report_unavailable'}
        if mode == COMBINED_MODE and (source != 'manual' or not combined_report_ready(result)):
            return {'ok': False, 'sent': False, 'queued': False, 'status': 'local_report_unavailable'}
        path = await asyncio.to_thread(self.token_usage_report_image, result)
        if source == 'daily' and automatic_peak_paused():
            return {'ok': True, 'sent': False, 'queued': False, 'status': 'peak_tariff'}
        if dry_run:
            return {**result, 'sent': False, 'status': 'preview', 'image_path': str(path) if path else None}
        if source == 'daily':
            identity = daily_day
            table = 'token_usage_reports'
            match = re.fullmatch(r'feiyu_local_v1:(\d{4}-\d{2}-\d{2})', str(identity))
            legacy_identity = match.group(1) if match else identity
            legacy_receipt = legacy_identity
        else:
            identity = getattr(event, 'get_message_id', lambda: '')()
            identity = identity or getattr(getattr(event, 'message_obj', None), 'message_id', '')
            if not identity:
                return {'ok': False, 'sent': False, 'queued': False, 'status': 'message_identity_unavailable',
                        'note': '这次请求缺少可核对的消息标识，报告暂未发送。'}
            legacy_identity = str(identity)
            identity = hashlib.sha256(f'{legacy_identity}:token_report_image'.encode()).hexdigest()
            legacy_receipt = identity
            table = 'token_usage_manual_images'
        try:
            legacy_key = queue_key(source, legacy_identity, 'image')
        except (TypeError, ValueError):
            return {'ok': False, 'sent': False, 'queued': False, 'status': 'message_identity_unavailable',
                    'note': '这次请求缺少可核对的消息标识，报告暂未发送。'}
        async with self.token_usage_lock:
            receipts = self.state.setdefault(table, {})
            if not isinstance(receipts, dict):
                return {'ok': False, 'sent': False, 'queued': False, 'status': 'receipt_store_unavailable',
                        'note': '报告记录暂时无法核对，本次报告暂未发送。'}
            previous = receipts.get(identity)
            resume_blocked = bool(source == 'daily' and isinstance(previous, dict)
                                  and previous.get('status') == 'blocked_before_send'
                                  and previous.get('peak_guard_v1') is True)
            if isinstance(previous, dict) and not resume_blocked:
                return _receipt_response(previous, duplicate=True)
            # A source change must not turn an old queued request into a new
            # send. Preserve every old row, including waiting and unknown.
            if source == 'daily' and legacy_receipt != identity and isinstance(receipts.get(legacy_receipt), dict):
                return _receipt_response(receipts[legacy_receipt], duplicate=True)
            for name in (LEGACY_QUEUE_STATE, QUEUE_STATE):
                queue = self.state.get(name, {})
                if not isinstance(queue, dict):
                    return {'ok': False, 'sent': False, 'queued': False, 'status': 'legacy_queue_unavailable',
                            'note': '旧报告记录暂时无法核对，本次报告暂未发送。'}
                if legacy_key in queue or any(isinstance(row, dict) and row.get('receipt_key') == legacy_receipt
                                              for row in queue.values()):
                    return {'ok': False, 'sent': False, 'queued': False, 'status': 'legacy_request_paused',
                            'duplicate': True, 'note': '原报告请求已保留，暂不重新发送。'}
            if source == 'daily' and automatic_peak_paused():
                return {'ok': True, 'sent': False, 'queued': False, 'status': 'peak_tariff'}
            try:
                adapter = self.context.get_platform_inst('snowluma-qq')
                client = adapter.get_client()
                login = _body(await asyncio.wait_for(client.call_action('get_login_info'), 20))
                if str(login.get('user_id')) != BOT_QQ:
                    return {'ok': False, 'sent': False, 'queued': False, 'status': 'qq_identity_unavailable',
                            'note': '报告已生成，QQ当前不可发送，请恢复后重新查询。'}
            except asyncio.CancelledError:
                raise
            except Exception:
                return {'ok': False, 'sent': False, 'queued': False, 'status': 'qq_unavailable',
                        'note': '报告已生成，QQ当前不可发送，请恢复后重新查询。'}
            if source == 'daily' and automatic_peak_paused():
                return {'ok': True, 'sent': False, 'queued': False, 'status': 'peak_tariff'}
            receipt = dict(previous) if resume_blocked else {}
            receipt.update({'status': 'intent', 'at': datetime.now(TZ).timestamp(),
                            'format': 'image' if path else 'text', 'report_mode': mode,
                            'period': summary['period'], 'snapshot_at': summary['observed_at']})
            # Only a newly created manual intent in the live owner scope may
            # continue through the native route during a later peak window.
            # Never upgrade a pre-existing receipt while resuming it.
            if source == 'manual' and not resume_blocked and owner_scope_active(event):
                receipt['owner_direct_peak_v1'] = True
            receipts[identity] = receipt
            try:
                self.save()
            except Exception:
                if resume_blocked:
                    receipts[identity] = previous
                else:
                    receipts.pop(identity, None)
                return {'ok': False, 'sent': False, 'queued': False, 'status': 'report_intent_persist_failed'}
            try:
                if source == 'daily' and automatic_peak_paused():
                    receipt.update(status='blocked_before_send', peak_guard_v1=True)
                    self.save()
                    return {'ok': True, 'sent': False, 'queued': False, 'status': 'peak_tariff'}
                transport = getattr(self, '_native_private_transport', None)
                use_native = bool(transport is not None and transport.enabled
                                  and (path is None or getattr(transport, 'enabled_media', False)))
                producer = 'token-report-daily' if source == 'daily' else 'token-report-manual'
                scope = (native_delivery(producer_operation(producer, identity, slot='report')) if use_native else nullcontext())
                message = ([{'type': 'image', 'data': {'file': path.as_uri()}}] if path
                           else [{'type': 'text', 'data': {'text': display_bill_text(result['text'])}}])
                with scope:
                    if source == 'daily' and automatic_peak_paused():
                        receipt.update(status='blocked_before_send', peak_guard_v1=True)
                        self.save()
                        return {'ok': True, 'sent': False, 'queued': False, 'status': 'peak_tariff'}
                    response = _body(await asyncio.wait_for(client.call_action('send_private_msg', user_id=int(OWNER), message=message), 40))
                message_id = response.get('message_id')
                valid = type(message_id) in (str, int) and re.fullmatch(r'-?[1-9]\d{0,19}', str(message_id))
                native_id = response.get('native_message_id') if use_native else None
                native_confirmed = bool(use_native and response.get('native_delivery_confirmed') is True
                                        and isinstance(native_id, str) and re.fullmatch(r'[1-9]\d{0,23}', native_id))
                receipt['status'] = 'sent' if valid or native_confirmed else 'unknown'
                if valid:
                    receipt['message_id'] = str(message_id)
                if native_confirmed:
                    receipt.update(native_message_id=native_id, native_confirmed=True)
            except BaseException as error:
                receipt['status'] = 'unknown'
                self.save()
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
            self.save()
            return _receipt_response(receipt)
