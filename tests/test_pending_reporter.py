import asyncio
import os
import time
import unittest
from unittest.mock import patch

from tests import test_headless_background as fixture


class PendingReporterTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdoptedSlackRunTest.asyncSetUp
    asyncTearDown = fixture.AdoptedSlackRunTest.asyncTearDown
    _run = fixture.AdoptedSlackRunTest._run

    async def test_pending_old_sleep_is_not_closed_and_new_turn_is_posted(self):
        app=fixture.app
        run=self._run([],pid=None,retry_owner=None,session_id='s1',conclude=True,
                      continuation={'kind':'waiting','turn':1,'attempt':1,'prompt':'continue','due_at':time.time()-1})
        client=fixture.FakeClient()
        with patch.object(app,'_age_hours',return_value=48.):
            observers=await app.finish_adopted_slack_runs(client)
            await asyncio.sleep(.03)
            self.assertEqual(run.meta()['status'],'running')
            run.append_event({'kind':'result','body':'Status: done — verified new turn','data':{}},2)
            run.update_meta(status='done',turns=2,continuation=None)
            for observer in observers: await asyncio.wait_for(observer,5)
        self.assertEqual(client.posts,['Status: done — verified new turn'])
        self.assertEqual(run.meta()['slack_posted_seq'],0)

    async def test_task_update_opt_out_survives_adoption_and_suppresses_posts(self):
        from cos import tasks
        app=fixture.app
        run=self._run([],pid=os.getpid(),session_id='s1',conclude=True)
        tasks.updates(run.id, False, 'Owner: stop automatic updates for this task')
        client=fixture.FakeClient()
        observers=await app.finish_adopted_slack_runs(client)
        await asyncio.sleep(.03)
        run.append_event({'kind':'result','body':'Status: done — background completion','data':{}},2)
        run.update_meta(status='done',turns=2)
        for observer in observers: await asyncio.wait_for(observer,5)
        self.assertEqual(client.posts,[])
        self.assertFalse(run.meta()['automatic_updates'])
