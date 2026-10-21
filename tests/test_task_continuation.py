import json
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from cos import runner, tasks
from tests import test_waiting_continuation as waiting_fixture


class DurableContinuationTest(unittest.TestCase):
    setUp = waiting_fixture.WaitingContinuationTest.setUp
    exercise = waiting_fixture.WaitingContinuationTest.exercise

    def test_false_done_with_unmet_contract_is_not_published_as_done(self):
        run = self.exercise('codex', state='done')
        tasks.contract(run.id,'ship',[{'id':'merge','title':'PR merged','proofs':[
            {'kind':'pr','url':'https://github.com/acme/repo/pull/1'}]}])
        events=[]
        with patch('cos.proofs.verify',return_value={'verified':False,'reason':'PR still open'}):
            meta=run.execute(on_event=events.append)
        self.assertEqual(meta['status'],'failed')
        self.assertTrue(any(e.get('data',{}).get('completion_refused') for e in events))
        self.assertFalse(any(e['kind']=='result' and 'Status: done' in e['body'] for e in events))

    def test_restart_recovers_durable_wait_once(self):
        run=self.exercise('codex',state='done')
        dead=subprocess.Popen(['true']); dead.wait()
        run.update_meta(status='running',pid=None,retry_owner=dead.pid,turns=1,session_id='test-session',
                        waiting_continuations=1,continuation={
                            'kind':'waiting','prompt':runner.WAITING_CONTINUATION_PROMPT,
                            'due_at':time.time()-1,'turn':1,'attempt':1})
        events=[]
        final=runner.Run(run.id).adopt(on_event=events.append)
        self.assertIsNotNone(final)
        self.assertEqual(final['status'],'done')
        self.assertIsNone(final.get('continuation'))
        self.assertIsNone(runner.Run(run.id).adopt())
        self.assertEqual((Path(self.tmp.name)/'calls').read_text(),'1')
        self.assertTrue(any(e.get('data',{}).get('continuation_recovered') for e in events))

    def test_human_gate_prevents_auto_continuation(self):
        run=self.exercise('codex',state='waiting')
        tasks.block(run.id,'Bala must review stage video')
        meta=run.execute()
        self.assertEqual(meta['conclusion']['state'],'needs-you')
        self.assertEqual((Path(self.tmp.name)/'calls').read_text(),'1')

    def test_evidence_block_prevents_busy_retries_without_asking_approval(self):
        run=self.exercise('codex',state='waiting')
        tasks.block(run.id, 'Provider credentials unavailable', kind='access')
        meta=run.execute()
        self.assertEqual(meta['conclusion']['state'],'blocked')
        self.assertEqual((Path(self.tmp.name)/'calls').read_text(),'1')

    def test_recovered_technical_block_is_not_a_human_gate(self):
        run=self.exercise('codex',state='waiting')
        tasks.block(run.id, 'Receipt unavailable', kind='evidence')
        run.update_meta(status='running',pid=None,retry_owner=99999999,turns=1,session_id='test-session',
                        continuation={'kind':'waiting','prompt':'continue','due_at':time.time()-1,
                                      'turn':1,'attempt':1})
        events=[]
        final=run.adopt(on_event=events.append)
        self.assertEqual(final['conclusion']['state'],'blocked')
        self.assertTrue(any('Status: blocked' in e.get('body','') for e in events))
        self.assertFalse(any('Status: needs-you' in e.get('body','') for e in events))

    def test_sleep_has_a_durable_checkpoint_and_cancel_clears_it(self):
        run=self.exercise('codex')
        outcome={}
        with patch.object(runner,'WAITING_CONTINUATION_DELAYS',(30.,)):
            thread=threading.Thread(target=lambda:outcome.update(run.execute()))
            thread.start()
            try:
                deadline=time.monotonic()+5
                while not run.meta().get('retry_owner') and time.monotonic()<deadline:
                    time.sleep(.01)
                pending=run.meta()
                self.assertIsNotNone(pending.get('continuation'))
                self.assertGreater(pending['continuation']['due_at'],time.time())
            finally:
                run.cancel(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome['status'],'cancelled')
        self.assertIsNone(outcome.get('continuation'))

    def test_dashboard_does_not_fail_a_recoverable_sleep(self):
        from cos import server
        run=self.exercise('codex',state='done')
        dead=subprocess.Popen(['true']); dead.wait()
        run.update_meta(status='running',pid=None,retry_owner=dead.pid,turns=1,session_id='test-session',
                        continuation={'kind':'waiting','turn':1,'prompt':'continue','attempt':1,'due_at':time.time()-1})
        self.assertEqual(server.fresh_meta(run)['status'],'running')

    def test_startup_adopts_a_dead_owners_recorded_sleep(self):
        from cos import server
        run=self.exercise('codex',state='done')
        dead=subprocess.Popen(['true']); dead.wait()
        run.update_meta(status='running',pid=None,retry_owner=dead.pid,turns=1,session_id='test-session',
                        continuation={'kind':'waiting','turn':1,'prompt':'continue','attempt':1,'due_at':time.time()-1})
        self.assertIn(run.id,server.adopt_orphans())
        deadline=time.monotonic()+5
        while run.meta()['status']=='running' and time.monotonic()<deadline: time.sleep(.02)
        self.assertEqual(run.meta()['status'],'done')

    def test_recovery_does_not_start_engine_across_human_gate(self):
        run=self.exercise('codex',state='done')
        tasks.block(run.id,'Bala must review stage video')
        dead=subprocess.Popen(['true']); dead.wait()
        run.update_meta(status='running',pid=None,retry_owner=dead.pid,turns=1,session_id='test-session',
                        continuation={'kind':'waiting','turn':1,'prompt':'continue','attempt':1,'due_at':time.time()-1})
        meta=run.adopt()
        self.assertEqual(meta['conclusion']['state'],'needs-you')
        self.assertFalse((Path(self.tmp.name)/'calls').exists())

    def test_new_human_gate_during_sleep_prevents_dispatch(self):
        run=self.exercise('codex')
        outcome={}
        with patch.object(runner,'WAITING_CONTINUATION_DELAYS',(0.5,)):
            worker=threading.Thread(target=lambda:outcome.update(run.execute()))
            worker.start()
            deadline=time.monotonic()+5
            while not run.meta().get('retry_owner') and time.monotonic()<deadline: time.sleep(.01)
            tasks.block(run.id,'Bala must review stage video')
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome['conclusion']['state'],'needs-you')
        self.assertEqual((Path(self.tmp.name)/'calls').read_text(),'1')

    def test_missing_status_cannot_bypass_contract(self):
        run=self.exercise('codex')
        tasks.contract(run.id,'ship',[{'id':'m','title':'merge','proofs':[
            {'kind':'pr','url':'https://github.com/acme/repo/pull/1'}]}])
        events=[]
        with patch('cos.proofs.verify',return_value={'verified':False,'reason':'PR open'}):
            text=run._guard_task_claim('Everything shipped successfully.',events.append,1)
        self.assertIn('Status: waiting',text)
        self.assertTrue(events[0]['data']['completion_refused'])

    def test_two_processes_cannot_claim_the_same_pending_wait(self):
        run=self.exercise('codex',state='done')
        dead=subprocess.Popen(['true']); dead.wait()
        run.update_meta(status='running',pid=None,retry_owner=dead.pid,turns=1,session_id='test-session',
                        waiting_continuations=1,continuation={
                            'kind':'waiting','turn':1,'prompt':'continue','attempt':1,'due_at':time.time()+.3})
        code='from cos.runner import Run; Run(' + repr(run.id) + ').adopt()'
        env={**os.environ,'PYTHONPATH':str(Path(runner.__file__).resolve().parent.parent)}
        workers=[subprocess.Popen([os.sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                 for _ in range(2)]
        for worker in workers:
            out,err=worker.communicate(timeout=10)
            self.assertEqual(worker.returncode,0,err.decode())
        self.assertEqual((Path(self.tmp.name)/'calls').read_text(),'1')
        self.assertEqual(run.meta()['status'],'done')
