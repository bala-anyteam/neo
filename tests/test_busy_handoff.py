import os
from unittest.mock import patch
import commands
from tests import test_headless_background as fixture
from cos.runner import Run


class BusyHandoffTest(fixture.AdoptedSlackRunTest):
    async def test_live_adopted_followup_does_not_start_or_reset_timer(self):
        app = fixture.app
        run = self._run([], pid=os.getpid(), session_id='session')
        token = app.task_updates.begin(run.dir, now=100)
        entry = {'run_id': run.id, 'runtime': 'headless', 'engine': 'claude'}
        client = fixture.FakeClient()
        with patch.object(Run, 'execute', side_effect=AssertionError('must not execute')):
            result = await app.run_headless(client, 'C1', '9.0', entry, commands.Request('run', prompt='go'), 'go')
        self.assertTrue(result)
        self.assertEqual(len(client.posts), 1)
        self.assertIn('not started', client.posts[0])
        self.assertEqual(run.meta()['status'], 'running')
        self.assertEqual(app.task_updates.current(run.dir), token)

    async def test_race_is_busy_not_failure_and_keeps_original(self):
        from cos.runner import RunBusyError
        app = fixture.app
        run = self._run([], pid=None, session_id='session')
        run.update_meta(status='done')
        entry = {'run_id': run.id, 'runtime': 'headless', 'engine': 'claude'}
        client = fixture.FakeClient()
        token = app.task_updates.begin(run.dir, now=100)
        with patch.object(app, 'agent_brief', return_value=''), patch.object(Run, 'execute', side_effect=RunBusyError('busy')), patch.object(app.sessions, 'set_thread'), patch.object(Run, 'cancel') as cancel:
            result = await app.run_headless(client, 'C1', '9.0', entry, commands.Request('run', prompt='go'), 'go')
        self.assertTrue(result)
        self.assertTrue(client.deleted)
        self.assertIn('not started', client.posts[-1])
        self.assertFalse(any('without an answer' in p for p in client.posts))
        cancel.assert_not_called()
        self.assertEqual(app.task_updates.current(run.dir), token)

    async def test_runner_busy_is_typed_and_does_not_mutate(self):
        from cos.runner import RunBusyError
        run = self._run([], pid=os.getpid(), session_id='session')
        before = run.meta()
        with self.assertRaises(RunBusyError):
            run.execute('go')
        self.assertEqual(run.meta(), before)

    async def test_adopted_retry_and_pending_are_busy_even_without_engine(self):
        import time
        app = fixture.app
        for fields in ({'retry_owner': os.getpid()},
                       {'continuation': {'kind': 'waiting', 'turn': 1, 'attempt': 8,
                                         'prompt': 'continue', 'due_at': time.time() + 300}}):
            run = self._run([], pid=None, session_id='session', conclude=True, **fields)
            client = fixture.FakeClient()
            entry = {'run_id': run.id, 'runtime': 'headless', 'engine': 'claude'}
            with patch.object(Run, 'execute', side_effect=AssertionError('must not execute')):
                self.assertTrue(await app.run_headless(client, 'C1', '9.0', entry,
                                                      commands.Request('run', prompt='go'), 'go'))
            self.assertEqual(len(client.posts), 1)
            self.assertIn('not started', client.posts[0])

    async def test_mention_busy_guard_preserves_timer_before_dispatch(self):
        from unittest.mock import AsyncMock
        app = fixture.app
        run = self._run([], pid=os.getpid(), session_id='session')
        token = app.task_updates.begin(run.dir, now=100)
        entry = {'run_id': run.id, 'runtime': 'headless', 'engine': 'claude'}
        client = fixture.FakeClient()
        req = commands.Request('run', prompt='go')
        with patch.object(app, 'ALLOWED_USER_IDS', set()), patch.object(app.sessions, 'get_thread', new=AsyncMock(return_value=entry)), patch.object(app, 'route_apply', new=AsyncMock(return_value=req)), patch.object(app, 'run_headless', new=AsyncMock()) as dispatch:
            await app.handle_mention({'user': 'U1', 'channel': 'C1', 'thread_ts': '9.0', 'ts': '9.1', 'text': 'go'}, client)
        dispatch.assert_not_called()
        self.assertEqual(app.task_updates.current(run.dir), token)
        self.assertIn('not started', client.posts[0])
