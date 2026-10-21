import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.test_headless_background import app
from cos import runner, task_updates


class TaskUpdateDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'COS_HOME': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.run = runner.Run.create('codex', 'watch build', self.tmp.name, 'full', source='slack',
                                     extra={'slack_channel': 'C1', 'slack_thread': '1.0'})
        self.run.update_meta(status='done')
        self.token = task_updates.begin(self.run.dir, now=100)
        task_updates.queue(self.run.dir, 'latest result', token=self.token)
        self.client = AsyncMock()
        self.client.chat_postMessage.return_value = {'ok': True, 'ts': '2.0'}

    async def test_ack_only_after_delivery_and_no_early_or_duplicate_send(self):
        with patch.object(app.pause, 'is_paused', return_value=False), patch.object(task_updates.time, 'time', return_value=999):
            await app.task_update_tick(self.client)
        self.client.chat_postMessage.assert_not_called()
        with patch.object(app.pause, 'is_paused', return_value=False), patch.object(task_updates.time, 'time', return_value=1000):
            self.client.chat_postMessage.return_value = {'ok': False}
            await app.task_update_tick(self.client)
            self.assertIsNotNone(task_updates.due(self.run.dir, active=False, now=1000))
            self.client.chat_postMessage.return_value = {'ok': True, 'ts': '2.0'}
            await app.task_update_tick(self.client)
            await app.task_update_tick(self.client)
        self.assertEqual(self.client.chat_postMessage.call_count, 2)

    async def test_pause_and_muting_retain_pending_without_delivery(self):
        with patch.object(app.pause, 'is_paused', return_value=True), patch.object(task_updates.time, 'time', return_value=1000):
            await app.task_update_tick(self.client)
        self.run.update_meta(automatic_updates=False)
        with patch.object(app.pause, 'is_paused', return_value=False), patch.object(task_updates.time, 'time', return_value=1000):
            await app.task_update_tick(self.client)
        self.client.chat_postMessage.assert_not_called()
        self.assertIsNotNone(task_updates.due(self.run.dir, active=False, now=1000))

    async def test_busy_rejection_keeps_existing_notification_generation(self):
        entry = {'runtime': 'headless', 'engine': 'codex', 'run_id': self.run.id}
        async def unchanged(req, thread):
            return req
        with patch.object(app, 'ALLOWED_USER_IDS', {'U1'}), patch.object(app, 'BUSY', {'1.0'}), \
             patch.object(app, 'HEADLESS', {}), patch.object(app, 'ANSWERED', set()), \
             patch.object(app.sessions, 'get_thread', AsyncMock(return_value=entry)), \
             patch.object(app, 'route_apply', unchanged):
            await app.handle_mention({'user': 'U1', 'channel': 'C1', 'thread_ts': '1.0',
                                      'ts': '1.1', 'text': 'how is it going'}, self.client)
        self.assertEqual(task_updates.current(self.run.dir), self.token)
        self.assertIn('Still working', self.client.chat_postMessage.call_args.kwargs['text'])

    async def test_restart_before_first_direct_answer_does_not_delay_owner_reply(self):
        import asyncio
        self.run.update_meta(status='running', turns=1, pid=os.getpid())
        token = task_updates.begin(self.run.dir, direct=True)
        with patch.object(app, '_ADOPT_POLL_SECONDS', .01):
            observers = await app.finish_adopted_slack_runs(self.client)
            await asyncio.sleep(.02)
            self.run.append_event({'kind': 'result', 'body': 'Direct answer after restart', 'data': {}}, 1)
            self.run.update_meta(status='done', pid=None)
            for observer in observers:
                await asyncio.wait_for(observer, 3)
        self.client.chat_postMessage.assert_called_once()
        self.assertEqual(self.client.chat_postMessage.call_args.kwargs['text'], 'Direct answer after restart')
        self.assertFalse(task_updates.direct_pending(self.run.dir, token))
