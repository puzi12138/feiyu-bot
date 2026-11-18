"""Only strong read evidence can resolve an uncertain write; no resend path."""
import copy
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'plugins'))

from astrbot_plugin_xiaoheihe_adapter.xiaoheihe.api_client import ApiResponse, XiaoheiheApiClient
from astrbot_plugin_xiaoheihe_adapter.xiaoheihe.config_service import DEFAULT_CONFIG
from astrbot_plugin_xiaoheihe_adapter.xiaoheihe.models import Credentials, RoutingTarget, Notification, NotificationType, EventState
from astrbot_plugin_xiaoheihe_adapter.xiaoheihe.runtime import RuntimeServices
from astrbot_plugin_xiaoheihe_adapter.xiaoheihe import endpoints
from feiyu_steward.heybox_reconciliation import (
    PROFILE_READ, install_uncertain_reconciliation, matching_comment_id, normalize_profile_comments,
)
from astrbot_plugin_xiaoheihe_adapter.xiaoheihe.unknown_receipts import (
    matching_comment_id as live_matching_comment_id,
)


class Native:
    __module__ = 'astrbot_plugin_xiaoheihe_adapter.main'


def payload(*, comment='200', root='200', parent=None, text='有趣的公开评论', when=1700000005):
    item = {'comment_id': comment, 'root_comment_id': root, 'text': text,
            'create_at': when, 'link': {'id': '12345'}}
    if parent:
        item['parent_comment'] = {'id': parent}
    return {'status': 'ok', 'user': {'userid': '10'}, 'result': [item]}


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.route = RoutingTarget('default', '12345')
        self.raw = payload()
        self.rows = normalize_profile_comments(self.raw, '10')

    def match(self, rows=None, **kwargs):
        return matching_comment_id(self.rows if rows is None else rows, self.route, '10',
                                   '有趣的公开评论', 1700000000, now=1700000900, **kwargs)

    def test_actual_profile_shape_and_top_floor_match(self):
        self.assertEqual(self.match(), '200')
        self.assertNotIn('user', self.raw['result'][0])

    def test_returned_foreign_or_missing_profile_never_inherits_request_identity(self):
        for user in ({'userid': '11'}, {}, None):
            with self.subTest(user=user), self.assertRaises(ValueError):
                normalize_profile_comments({**self.raw, 'user': user}, '10')
        contradictory = copy.deepcopy(self.raw)
        contradictory['result'][0]['user'] = {'userid': '11'}
        self.assertEqual(normalize_profile_comments(contradictory, '10'), [])

    def test_wrong_post_author_root_or_parent_never_matches(self):
        for patch in ({'post_id': '67890'}, {'user': {'uid': '11'}},
                      {'root_comment_id': '201'}, {'parent_comment_id': '222'}):
            with self.subTest(patch=patch):
                self.assertIsNone(self.match([{**self.rows[0], **patch}]))

    def test_replies_require_both_root_and_direct_parent(self):
        self.route = RoutingTarget('default', '12345', '100', '101')
        rows = normalize_profile_comments(payload(root='100', parent='101'), '10')
        self.assertEqual(self.match(rows), '200')
        self.assertIsNone(self.match([{**rows[0], 'parent_comment_id': '100'}]))

    def test_full_text_only_no_partial_matching(self):
        for text in ('有趣', '有趣的公开评论 extra', '', None):
            self.assertIsNone(self.match([{**self.rows[0], 'content': text}]))

    def test_fixed_send_window_never_expands_as_old_unknown_ages(self):
        for stamp in (1699999984, 1700000181, float('nan'), None, True):
            self.assertIsNone(self.match([{**self.rows[0], 'created_at': stamp}]))
        self.assertEqual(self.match([{**self.rows[0], 'created_at': 1700000005000}]), '200')

    def test_distinct_same_text_comments_are_ambiguous(self):
        self.assertIsNone(self.match(self.rows + [{**self.rows[0], 'comment_id': '201', 'root_comment_id': '201'}]))
        self.assertEqual(self.match(self.rows + self.rows), '200')

    def test_malformed_rows_and_missing_floor_evidence_fail_closed(self):
        for changed in ({'root_comment_id': ''}, {'link': {}}, {'comment_id': None}, {'parent_comment': {'text': 'only prose'}}):
            data = copy.deepcopy(self.raw)
            data['result'][0].update(changed)
            self.assertEqual(normalize_profile_comments(data, '10'), [])

    def test_compatibility_export_matches_live_guard_including_whitespace(self):
        examples = [self.rows, [], [{**self.rows[0], 'content': ' 有趣的公开评论'}],
                    [{**self.rows[0], 'user': {'uid': '11'}}],
                    [{**self.rows[0], 'post_id': '999'}],
                    [{**self.rows[0], 'root_comment_id': '201'}],
                    [{**self.rows[0], 'created_at': 1699999984}],
                    self.rows + [{**self.rows[0], 'comment_id': '201', 'root_comment_id': '201'}]]
        for rows in examples:
            with self.subTest(rows=rows):
                args = (rows, self.route, '10', '有趣的公开评论', 1700000000)
                self.assertEqual(matching_comment_id(*args, now=1700000900),
                                 live_matching_comment_id(*args, now=1700000900))


class NativeReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connect_guard = patch('socket.socket.connect', side_effect=AssertionError('network blocked'))
        self.connect_guard.start()
        self.connect_ex_guard = patch('socket.socket.connect_ex', side_effect=AssertionError('network blocked'))
        self.connect_ex_guard.start()
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = RuntimeServices(copy.deepcopy(DEFAULT_CONFIG), Path(self.temp.name))
        await self.runtime.ensure_started()
        self.now = time.time()
        self.route = RoutingTarget('default', '12345')
        self.rowid = await self.runtime.repository.record_outgoing_attempt('default', None, self.route, '有趣的公开评论', 'send_unknown')
        await self.runtime.repository.db.execute('UPDATE outgoing_replies SET attempted_at=? WHERE id=?', (self.now - 90, self.rowid))
        self.client = XiaoheiheApiClient('default', credentials=Credentials('default', '10', '', {}))
        self.client._request = AsyncMock(return_value=ApiResponse(payload(when=self.now - 88), {}, {}))
        self.client.send_comment = AsyncMock()
        self.runtime.get_client = AsyncMock(return_value=self.client)
        self.plugin = Native()
        self.plugin.runtime = self.runtime
        self.original_check = self.runtime._check_uncertain_send
        self.original_recent = XiaoheiheApiClient.recent_comments
        self.restore = install_uncertain_reconciliation(self.plugin, start_monitor=False)

    async def asyncTearDown(self):
        self.restore()
        await self.client.close()
        await self.runtime.close()
        self.temp.cleanup()
        self.connect_ex_guard.stop()
        self.connect_guard.stop()

    async def test_read_only_reconcile_confirms_original_unknown_without_send(self):
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        self.assertEqual(result, {'checked': 1, 'confirmed': 1, 'repaired_events': 0, 'writes_attempted': 0})
        row = await self.runtime.repository.db.fetchone('SELECT status,external_comment_id FROM outgoing_replies WHERE id=?', (self.rowid,))
        self.assertEqual((row['status'], row['external_comment_id']), ('sent', '200'))
        self.client.send_comment.assert_not_awaited()
        self.assertEqual(self.client._request.call_args.args, (PROFILE_READ,))
        self.assertEqual(endpoints.ENDPOINTS[PROFILE_READ].method, 'GET')
        self.assertTrue(endpoints.ENDPOINTS[PROFILE_READ].retry_safe)

    async def test_missing_or_failed_read_stays_unknown_and_does_not_block_later_sends(self):
        self.client._request.side_effect = OSError('private upstream detail')
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        self.assertEqual(result['confirmed'], 0)
        row = await self.runtime.repository.db.fetchone('SELECT status FROM outgoing_replies WHERE id=?', (self.rowid,))
        self.assertEqual(row['status'], 'send_unknown')
        self.client.send_comment.assert_not_awaited()
        state = (Path(self.temp.name) / 'feiyu-uncertain-rechecks.json').read_text()
        self.assertNotIn('private', state)
        self.assertNotIn('公开评论', state)

    async def test_persistent_read_cooldown_survives_restore_and_reinstall(self):
        self.client._request.return_value = ApiResponse(payload(text='different'), {}, {})
        await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        self.restore()
        self.restore = install_uncertain_reconciliation(self.plugin, start_monitor=False)
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now + 60)
        self.assertEqual(result['checked'], 0)
        self.assertEqual(self.client._request.await_count, 1)
        self.client.send_comment.assert_not_awaited()

    async def test_foreign_profile_cannot_confirm_and_restoration_is_idempotent(self):
        body = payload(when=self.now - 88)
        body['user']['userid'] = '999'
        self.client._request.return_value = ApiResponse(body, {}, {})
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        self.assertEqual(result['confirmed'], 0)
        self.restore()
        self.restore()
        self.assertEqual(self.runtime._check_uncertain_send, self.original_check)
        self.assertIs(XiaoheiheApiClient.recent_comments, self.original_recent)
        self.assertNotIn(PROFILE_READ, endpoints.ENDPOINTS)

    async def test_interruption_after_outgoing_confirmation_repairs_event_after_reload_without_read_or_send(self):
        event = Notification('default', 'fixture-event', '', '', NotificationType.REPLY,
                             '15', 'public author', '12345', '', '', 'public source', self.now - 100)
        event_id = await self.runtime.repository.claim_event(event)
        self.assertIsNotNone(event_id)
        await self.runtime.repository.mark_event(event_id, EventState.SEND_UNKNOWN)
        await self.runtime.repository.db.execute('UPDATE outgoing_replies SET incoming_event_id=? WHERE id=?', (event_id, self.rowid))
        original_mark = self.runtime.repository.mark_event
        self.runtime.repository.mark_event = AsyncMock(side_effect=__import__('asyncio').CancelledError())
        with self.assertRaises(__import__('asyncio').CancelledError):
            await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        row = await self.runtime.repository.db.fetchone('SELECT status FROM outgoing_replies WHERE id=?', (self.rowid,))
        self.assertEqual(row['status'], 'sent')
        self.restore()
        self.runtime.repository.mark_event = original_mark
        self.restore = install_uncertain_reconciliation(self.plugin, start_monitor=False)
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now + 1)
        self.assertEqual(result['repaired_events'], 1)
        incoming = await self.runtime.repository.db.fetchone('SELECT status FROM incoming_events WHERE id=?', (event_id,))
        self.assertEqual(incoming['status'], 'sent')
        self.assertEqual(self.client._request.await_count, 1)
        self.client.send_comment.assert_not_awaited()

    async def test_closed_native_runtime_does_not_start_background_reads(self):
        import asyncio
        self.restore()
        await self.runtime.close()
        self.restore = install_uncertain_reconciliation(self.plugin)
        await asyncio.sleep(0)
        self.runtime.get_client.assert_not_awaited()
        self.client.send_comment.assert_not_awaited()

    async def test_shorter_current_limit_cannot_confirm_truncated_comment(self):
        await self.runtime.repository.db.execute(
            'UPDATE outgoing_replies SET content=?,content_hash=? WHERE id=?',
            ('ABCDE', __import__('hashlib').sha256(b'ABCDE').hexdigest(), self.rowid))
        self.runtime.config.snapshot = Mock(return_value={'reply': {'max_reply_chars': 3}})
        self.client._request.return_value = ApiResponse(payload(text='ABC', when=self.now - 88), {}, {})
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        self.assertEqual(result['confirmed'], 0)
        row = await self.runtime.repository.db.fetchone('SELECT status FROM outgoing_replies WHERE id=?', (self.rowid,))
        self.assertEqual(row['status'], 'send_unknown')
        self.client.send_comment.assert_not_awaited()

    async def test_shorter_current_limit_does_not_block_full_text_match(self):
        await self.runtime.repository.db.execute(
            'UPDATE outgoing_replies SET content=?,content_hash=? WHERE id=?',
            ('ABCDE', __import__('hashlib').sha256(b'ABCDE').hexdigest(), self.rowid))
        self.runtime.config.snapshot = Mock(return_value={'reply': {'max_reply_chars': 3}})
        self.client._request.return_value = ApiResponse(payload(text='ABCDE', when=self.now - 88), {}, {})
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        self.assertEqual(result['confirmed'], 1)
        self.client.send_comment.assert_not_awaited()

    async def test_corrupt_stored_text_digest_fails_closed_without_network(self):
        await self.runtime.repository.db.execute(
            'UPDATE outgoing_replies SET content_hash=? WHERE id=?', ('broken', self.rowid))
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        self.assertEqual(result['confirmed'], 0)
        self.client._request.assert_not_awaited()
        self.client.send_comment.assert_not_awaited()

    async def test_status_separates_outside_window_and_read_failure(self):
        await self.runtime.repository.record_outgoing_attempt('default', None, self.route, 'older', 'send_unknown')
        await self.runtime.repository.db.execute(
            "UPDATE outgoing_replies SET attempted_at=? WHERE content=?", (self.now - 8 * 86400, 'older'))
        self.client._request.side_effect = OSError('never expose this detail')
        await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        state = self.runtime.feiyu_unknown_reconciliation_status()
        self.assertEqual((state['unknown_count'], state['outside_window_count']), (2, 1))
        self.assertEqual(state['last_read_failure_kind'], 'platform_read_failed')
        self.assertEqual(state['consecutive_read_failures'], 1)
        self.assertNotIn('never expose', str(state))
        reads_before = self.client._request.await_count
        self.runtime.feiyu_unknown_reconciliation_status()
        self.assertEqual(self.client._request.await_count, reads_before)
        self.client.send_comment.assert_not_awaited()

    async def test_changed_record_during_read_is_not_confirmed(self):
        async def mutate_during_read(*args, **kwargs):
            await self.runtime.repository.db.execute(
                'UPDATE outgoing_replies SET content_hash=? WHERE id=?', ('changed', self.rowid))
            return ApiResponse(payload(when=self.now - 88), {}, {})
        self.client._request.side_effect = mutate_during_read
        result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
        self.assertEqual(result['confirmed'], 0)
        row = await self.runtime.repository.db.fetchone(
            'SELECT status FROM outgoing_replies WHERE id=?', (self.rowid,))
        self.assertEqual(row['status'], 'send_unknown')
        self.client.send_comment.assert_not_awaited()

    async def test_competing_update_before_confirmation_is_atomic(self):
        repository = self.runtime.repository
        original = repository.confirm_outgoing
        event = Notification('default', 'another-event', '', '', NotificationType.REPLY,
                             '15', 'public author', '12345', '', '', 'public source', self.now - 100)
        event_id = await repository.claim_event(event)
        self.assertIsNotNone(event_id)
        fields = {'status': 'sent', 'content': 'changed', 'content_hash': 'changed',
                  'profile_id': 'other', 'post_id': '99999', 'root_comment_id': '999',
                  'parent_comment_id': '999', 'attempted_at': self.now - 100,
                  'incoming_event_id': event_id, 'external_comment_id': '333', 'confirmed_at': self.now}
        for field, value in fields.items():
            with self.subTest(field=field):
                await repository.db.execute(
                    'UPDATE outgoing_replies SET status=?,content=?,content_hash=?,profile_id=?,post_id=?,'
                    'root_comment_id=?,parent_comment_id=?,attempted_at=?,incoming_event_id=NULL,'
                    'external_comment_id=?,confirmed_at=NULL WHERE id=?',
                    ('send_unknown', '有趣的公开评论', __import__('hashlib').sha256('有趣的公开评论'.encode()).hexdigest(),
                     'default', '12345', '', '', self.now - 90, '', self.rowid))
                async def race(*args, **kwargs):
                    await repository.db.execute(f'UPDATE outgoing_replies SET {field}=? WHERE id=?', (value, self.rowid))
                    return await original(*args, **kwargs)
                repository.confirm_outgoing = AsyncMock(side_effect=race)
                cid = await self.runtime._check_uncertain_send(
                    self.client, self.route, '有趣的公开评论', self.rowid, attempted_at=self.now - 90)
                self.assertIsNone(cid)
                row = await repository.db.fetchone('SELECT external_comment_id FROM outgoing_replies WHERE id=?', (self.rowid,))
                self.assertEqual(row['external_comment_id'], value if field == 'external_comment_id' else '')
                bindings = await repository.db.fetchone('SELECT COUNT(*) AS n FROM self_comment_ids')
                self.assertEqual(bindings['n'], 0)
        self.client.send_comment.assert_not_awaited()

    async def test_direct_send_confirmation_remains_compatible(self):
        result = await self.runtime.repository.confirm_outgoing(self.rowid, '200')
        self.assertIsInstance(result, dict)
        row = await self.runtime.repository.db.fetchone(
            'SELECT status,external_comment_id FROM outgoing_replies WHERE id=?', (self.rowid,))
        self.assertEqual((row['status'], row['external_comment_id']), ('sent', '200'))

    async def test_native_fallback_preserves_unknown_after_limit_change(self):
        self.restore()
        await self.runtime.repository.db.execute(
            'UPDATE outgoing_replies SET content=?,content_hash=? WHERE id=?',
            ('ABCDE', __import__('hashlib').sha256(b'ABCDE').hexdigest(), self.rowid))
        self.runtime.config.snapshot = Mock(return_value={'reply': {'max_reply_chars': 3}})
        row = {'comment_id': '200', 'post_id': '12345', 'root_comment_id': '200',
               'parent_comment_id': '', 'user': {'uid': '10'}, 'content': 'ABC',
               'created_at': self.now - 88}
        self.client.recent_comments = AsyncMock(return_value=[row])
        result = await self.runtime._check_uncertain_send(
            self.client, self.route, 'ABCDE', self.rowid, attempted_at=self.now - 90)
        self.assertIsNone(result)
        self.client.send_comment.assert_not_awaited()

    async def test_native_fallback_requires_full_post_and_floor_evidence(self):
        self.restore()
        valid = {'comment_id': '200', 'post_id': '12345', 'root_comment_id': '200',
                 'parent_comment_id': '', 'user': {'uid': '10'}, 'content': '有趣的公开评论',
                 'created_at': self.now - 88}
        self.client.recent_comments = AsyncMock(return_value=[{**valid, 'post_id': '999'}])
        result = await self.runtime._check_uncertain_send(
            self.client, self.route, '有趣的公开评论', self.rowid, attempted_at=self.now - 90)
        self.assertIsNone(result)
        for changed in ({'user': {'uid': '11'}}, {'root_comment_id': '201'},
                        {'created_at': self.now - 500}, {'content': '有趣'}):
            with self.subTest(changed=changed):
                self.client.recent_comments.return_value = [{**valid, **changed}]
                result = await self.runtime._check_uncertain_send(
                    self.client, self.route, '有趣的公开评论', self.rowid, attempted_at=self.now - 90)
                self.assertIsNone(result)
        self.client.recent_comments.return_value = [valid, {**valid, 'comment_id': '201', 'root_comment_id': '201'}]
        result = await self.runtime._check_uncertain_send(
            self.client, self.route, '有趣的公开评论', self.rowid, attempted_at=self.now - 90)
        self.assertIsNone(result)
        self.client.recent_comments.return_value = [valid]
        result = await self.runtime._check_uncertain_send(
            self.client, self.route, '有趣的公开评论', self.rowid, attempted_at=self.now - 90)
        self.assertEqual(result, '200')
        self.client.send_comment.assert_not_awaited()

    async def test_installer_uses_live_plugin_module_namespace(self):
        import importlib
        self.restore()
        prefix = 'data.plugins.astrbot_plugin_xiaoheihe_adapter.xiaoheihe'
        actual = 'astrbot_plugin_xiaoheihe_adapter.xiaoheihe'
        aliases = {f'{prefix}.{name}': importlib.import_module(f'{actual}.{name}')
                   for name in ('api_client', 'endpoints', 'models', 'security', 'unknown_receipts')}
        class DataPlugin:
            __module__ = 'data.plugins.astrbot_plugin_xiaoheihe_adapter.main'
        plugin = DataPlugin()
        plugin.runtime = self.runtime
        with patch.dict(sys.modules, aliases):
            self.restore = install_uncertain_reconciliation(plugin, start_monitor=False)
            result = await self.runtime.feiyu_reconcile_unknown_comments(now=self.now)
            self.assertEqual(result['confirmed'], 1)
            self.client.send_comment.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
