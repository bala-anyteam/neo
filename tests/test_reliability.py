import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cos import reliability

# app keeps an AlertGate path for process lifetime; import under a process-lived
# temporary home so these tests neither touch production state nor leave a dead path.
_APP_HOME = tempfile.TemporaryDirectory()
with patch.dict(os.environ, COS_HOME=_APP_HOME.name, SLACK_BOT_TOKEN='xoxb-test'):
    import app


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, COS_HOME=self.tmp.name)
        self.env.start()
        self.now = datetime(2026, 10, 21, 20, tzinfo=timezone.utc)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_data(self, rid, meta, events):
        folder = Path(self.tmp.name) / 'runs' / rid
        folder.mkdir(parents=True)
        (folder / 'meta.json').write_text(json.dumps(meta))
        (folder / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))

    def test_register_and_manual_heuristic(self):
        from cos.config import OWNER_MESSAGE_MARK
        self.run_data('one', {'source': 'slack', 'created_at': '2026-10-21T19:00:00+00:00'}, [
            {'ts': '2026-10-21T19:01:00+00:00', 'kind': 'prompt', 'body': 'old status?\n' + OWNER_MESSAGE_MARK + '\nplease continue'},
            {'ts': '2026-10-21T19:02:00+00:00', 'kind': 'prompt', 'body': 'continue'},
            {'ts': '2026-10-21T19:03:00+00:00', 'kind': 'status', 'data': {'waiting_continuation': 1}},
        ])
        state = reliability.configure('C1', '1.2', now=self.now, commit='abc')
        self.assertEqual(len(state['improvements']), 6)
        self.assertEqual(state['baseline']['metrics']['manual_status_nudges'], 1)
        self.assertEqual(state['baseline']['metrics']['automatic_recoveries'], 1)
        again = reliability.configure('C1', '1.2', now=self.now, commit='def')
        self.assertEqual(again['activation_commit'], 'abc')

    def test_watch_failure_notice_contains_cause_action_and_trace(self):
        from datetime import timedelta
        reliability.configure('C1','1.2',at='23:59',now=self.now,commit='abc')
        self.run_data('20261021-160230-8cff60', {
            'kind':'watch','source':'schedule','status':'failed','engine':'claude','turns':1,
            'title':'Sentry','updated_at':(self.now+timedelta(seconds=60)).isoformat()}, [
            {'turn':1,'kind':'result','body':"You've hit your weekly limit · resets Oct 23 at 6:30am (Asia/Calcutta)",
             'ts':(self.now+timedelta(seconds=60)).isoformat()}])
        client=type('Client',(),{})()
        client.chat_postMessage=AsyncMock(return_value={'ok':True,'ts':'2.0'})
        with patch('cos.reliability.pause.is_paused',return_value=False), \
             patch.dict(os.environ,{'NEO_DASHBOARD_URL':'http://mini:8789'}):
            asyncio.run(reliability.tick(client,now=self.now+timedelta(seconds=120)))
        text=client.chat_postMessage.call_args.kwargs['text']
        self.assertIn('weekly usage limit',text)
        self.assertIn('Retry after reset',text)
        self.assertIn('http://mini:8789/#20261021-160230-8cff60',text)
        self.assertIn('not a confirmed product incident',text)

    def test_failed_send_retries_and_pause(self):
        reliability.configure('C1', '1.2', at='00:00', now=self.now, commit='abc')
        client = type('Client', (), {})()
        client.chat_postMessage = AsyncMock(side_effect=RuntimeError('temporary'))
        with patch('cos.reliability.pause.is_paused', return_value=False):
            with self.assertRaises(RuntimeError):
                asyncio.run(reliability.tick(client, self.now))
            self.assertNotIn('daily:2026-10-22', reliability.status().get('sent', {}))
            client.chat_postMessage.side_effect = None
            client.chat_postMessage.return_value = {'ok': True, 'ts': '1'}
            asyncio.run(reliability.tick(client, self.now))
            asyncio.run(reliability.tick(client, self.now))
        self.assertEqual(client.chat_postMessage.await_count, 2)
        with patch('cos.reliability.pause.is_paused', return_value=True):
            asyncio.run(reliability.tick(client, self.now))
        self.assertEqual(client.chat_postMessage.await_count, 2)

    def test_large_tool_line_is_skipped_without_losing_later_events(self):
        self.run_data('one', {'source': 'slack', 'created_at': '2026-10-21T19:00:00+00:00'}, [
            {'ts': '2026-10-21T19:01:00+00:00', 'kind': 'tool', 'body': 'x' * 200000},
            {'ts': '2026-10-21T19:02:00+00:00', 'kind': 'result', 'body': 'done'},
        ])
        sample = reliability.measure(self.now.timestamp() - 86400, self.now.timestamp())
        self.assertEqual(sample['metrics']['final_replies'], 1)
        self.assertEqual(sample['oversized_lines'], 1)

    def test_watch_notice_is_deduplicated_only_after_delivery(self):
        reliability.configure('C1', '1.2', at='23:59', now=self.now, commit='abc')
        self.run_data('failed-watch', {'kind': 'watch', 'source': 'schedule', 'status': 'failed',
                      'created_at': self.now.isoformat(), 'title': 'CI watch'}, [])
        client = type('Client', (), {})()
        client.chat_postMessage = AsyncMock(return_value={'ok': True, 'ts': '1'})
        from datetime import timedelta
        later = self.now + timedelta(seconds=1)
        with patch('cos.reliability.pause.is_paused', return_value=False):
            asyncio.run(reliability.tick(client, later))
            asyncio.run(reliability.tick(client, later))
        self.assertEqual(client.chat_postMessage.await_count, 1)
        self.assertIn('watch:failed-watch', reliability.status()['sent'])

    def test_equal_exposure_and_insufficient_samples(self):
        reliability.configure('C1', '1.2', now=self.now, commit='abc')
        from datetime import timedelta
        result = reliability.report(self.now + timedelta(hours=2))
        self.assertEqual(result['exposure_seconds'], 7200)
        self.assertEqual(result['baseline']['end'] - result['baseline']['start'], 7200)
        self.assertEqual(result['assessment'], 'insufficient data')

    def test_exact_refusal_recovery_and_contract_coverage(self):
        self.run_data('contract', {'source': 'slack', 'created_at': '2026-10-21T19:00:00+00:00'}, [
            {'ts': '2026-10-21T19:01:00+00:00', 'kind': 'status', 'data': {'completion_refused': True}},
            {'ts': '2026-10-21T19:02:00+00:00', 'kind': 'status', 'data': {'continuation_recovered': True}},
        ])
        (Path(self.tmp.name) / 'runs' / 'contract' / 'task.json').write_text('{}')
        sample = reliability.measure(self.now.timestamp() - 86400, self.now.timestamp())
        self.assertEqual(sample['metrics']['completion_refusals'], 1)
        self.assertEqual(sample['metrics']['automatic_recoveries'], 1)
        self.assertEqual(sample['metrics']['declared_contracts'], 1)

    def test_twenty_first_failure_not_starved_by_acknowledged_first_twenty(self):
        reliability.configure('C1', '1.2', at='23:59', now=self.now, commit='abc')
        from datetime import timedelta
        for n in range(21):
            self.run_data(f'w{n:02}', {'kind': 'watch', 'source': 'schedule', 'status': 'failed',
                          'created_at': self.now.isoformat()}, [])
        client = type('Client', (), {})()
        client.chat_postMessage = AsyncMock(return_value={'ok': True, 'ts': '1'})
        with patch('cos.reliability.pause.is_paused', return_value=False):
            asyncio.run(reliability.tick(client, self.now + timedelta(seconds=1)))
            asyncio.run(reliability.tick(client, self.now + timedelta(seconds=1)))
        self.assertEqual(client.chat_postMessage.await_count, 2)
        self.assertEqual(len(reliability.status()['sent']), 21)

    def test_missing_delivery_receipt_is_not_acknowledged(self):
        reliability.configure('C1', '1.2', at='00:00', now=self.now, commit='abc')
        client = type('Client', (), {})()
        client.chat_postMessage = AsyncMock(return_value={})
        with patch('cos.reliability.pause.is_paused', return_value=False):
            with self.assertRaises(RuntimeError):
                asyncio.run(reliability.tick(client, self.now))
        self.assertFalse(reliability.status()['sent'])

    def test_exhausted_retry_not_counted_and_watch_denominator(self):
        self.run_data('watch', {'kind': 'watch', 'source': 'schedule', 'status': 'done',
                      'created_at': '2026-10-21T19:00:00+00:00'}, [
            {'ts': '2026-10-21T19:01:00+00:00', 'kind': 'status', 'data': {'auto_restart': 'exhausted'}}])
        (Path(self.tmp.name) / 'evidence-refusals.jsonl').write_text(json.dumps({
            'ts': '2026-10-21T19:00:00+00:00', 'loop_id': 'L1', 'reason': 'refused'}) + '\n')
        result = reliability.measure(self.now.timestamp() - 86400, self.now.timestamp())
        self.assertEqual(result['metrics']['automatic_recoveries'], 0)
        self.assertEqual(result['metrics']['scheduled_watch_runs'], 1)
        self.assertEqual(result['metrics']['evidence_refusals'], 1)

    def test_post_window_rolls_after_first_week(self):
        reliability.configure('C1', '1.2', now=self.now, commit='abc')
        from datetime import timedelta
        later = self.now + timedelta(days=14)
        result = reliability.report(later)
        self.assertEqual(result['post']['end'], later.timestamp())
        self.assertEqual(result['post']['start'], (later - timedelta(days=7)).timestamp())

    def test_waiting_then_automatic_resume_is_not_abandoned(self):
        self.run_data('resumed', {'source': 'slack', 'status': 'done', 'created_at': '2026-10-21T18:00:00+00:00'}, [
            {'ts': '2026-10-21T18:00:00+00:00', 'kind': 'result', 'body': 'Status: waiting'},
            {'ts': '2026-10-21T18:00:01+00:00', 'kind': 'status', 'data': {'waiting_continuation': 1}},
            {'ts': '2026-10-21T18:00:02+00:00', 'kind': 'prompt', 'body': 'continue'},
        ])
        sample = reliability.measure(self.now.timestamp() - 86400, self.now.timestamp())
        self.assertEqual(sample['metrics']['abandoned_waiting_exits'], 0)

    def test_terminal_waiting_without_followup_is_abandoned_after_grace(self):
        self.run_data('stranded', {'source': 'slack', 'status': 'done', 'created_at': '2026-10-21T18:00:00+00:00'}, [
            {'ts': '2026-10-21T18:00:00+00:00', 'kind': 'result', 'body': 'Status: waiting'},
        ])
        sample = reliability.measure(self.now.timestamp() - 86400, self.now.timestamp())
        self.assertEqual(sample['metrics']['abandoned_waiting_exits'], 1)

    def test_contract_uses_contract_timestamp_not_reused_thread_creation(self):
        self.run_data('oldthread', {'source': 'slack', 'created_at': '2026-09-01T18:00:00+00:00'}, [])
        (Path(self.tmp.name) / 'runs' / 'oldthread' / 'task.json').write_text(json.dumps({
            'created_at': self.now.timestamp() - 3600, 'goal': 'Finish release', 'steps': [{'id': 'release'}]}))
        sample = reliability.measure(self.now.timestamp() - 86400, self.now.timestamp())
        self.assertEqual(sample['metrics']['declared_contracts'], 1)

    def test_recent_or_pending_waiting_not_abandoned(self):
        for name, ts, pending in [('recent', '2026-10-21T19:59:00+00:00', None),
                                  ('pending', '2026-10-21T18:00:00+00:00', {'kind': 'waiting'})]:
            self.run_data(name, {'source': 'slack', 'status': 'done', 'continuation': pending,
                          'created_at': '2026-10-21T18:00:00+00:00'}, [
                {'ts': ts, 'kind': 'result', 'body': 'Status: waiting'}])
        sample = reliability.measure(self.now.timestamp() - 86400, self.now.timestamp())
        self.assertEqual(sample['metrics']['abandoned_waiting_exits'], 0)

    def test_configured_scheduled_failure_uses_only_reliability_delivery(self):
        reliability.configure('C1', '1.2', now=self.now, commit='abc')
        def close_coro(coro, loop):
            coro.close()
        with patch.object(app, 'NOTIFY_CHANNEL', 'C1'), patch.object(app.chief, 'finding_loops'), \
             patch.object(app, 'list_runs', return_value=[]), patch('cos.pause.is_paused', return_value=False), \
             patch('app.asyncio.run_coroutine_threadsafe', side_effect=close_coro) as send:
            app.watch_checked(object(), object())({'id': 'w', 'name': 'Watch'},
                {'id': 'run', 'status': 'failed', 'source': 'schedule'}, [])
        send.assert_not_called()

    def test_unconfigured_scheduled_failure_keeps_immediate_fallback(self):
        def close_coro(coro, loop):
            coro.close()
        with patch.object(app, 'NOTIFY_CHANNEL', 'C1'), patch.object(app.chief, 'finding_loops'), \
             patch.object(app, 'list_runs', return_value=[]), patch('cos.pause.is_paused', return_value=False), \
             patch('app.asyncio.run_coroutine_threadsafe', side_effect=close_coro) as send:
            app.watch_checked(object(), object())({'id': 'w', 'name': 'Watch'},
                {'id': 'run', 'status': 'failed', 'source': 'schedule'}, [])
        send.assert_called_once()

    def test_configured_tick_heartbeat_even_paused_or_not_due(self):
        reliability.configure('C1', '1.2', at='23:59', now=self.now, commit='abc')
        client = type('Client', (), {})()
        client.chat_postMessage = AsyncMock()
        with patch('cos.reliability.pause.is_paused', return_value=False):
            asyncio.run(reliability.tick(client, self.now))
        self.assertEqual(reliability.status()['last_tick_at'], self.now.timestamp())
        self.assertFalse(reliability.status()['last_tick_paused'])
        from datetime import timedelta
        later = self.now + timedelta(seconds=1)
        with patch('cos.reliability.pause.is_paused', return_value=True):
            asyncio.run(reliability.tick(client, later))
        self.assertEqual(reliability.status()['last_tick_at'], later.timestamp())
        self.assertTrue(reliability.status()['last_tick_paused'])
        self.assertEqual(reliability.status()['activation_commit'], 'abc')
        self.assertFalse(reliability.status()['sent'])
        client.chat_postMessage.assert_not_awaited()
