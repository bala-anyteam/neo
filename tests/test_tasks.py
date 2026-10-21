import json
import os
import tempfile
import unittest
from unittest.mock import patch

from cos import runner


class TaskContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"COS_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.run = runner.Run.create('codex', 'ship requested fix', self.tmp.name, 'full',
                                     source='slack', extra={'conclude': True})

    def test_contract_survives_reloading_and_refuses_unproven_completion(self):
        from cos import tasks
        tasks.contract(self.run.id, 'ship requested fix', [
            {'id': 'merge', 'title': 'PR merged', 'proofs': [{'kind': 'pr', 'url': 'https://github.com/acme/repo/pull/1'}]}])
        with patch('cos.proofs.verify', return_value={'verified': False, 'reason': 'PR open'}):
            self.assertEqual(tasks.unmet(self.run.id), ['merge: PR open'])
        self.assertEqual(tasks.show(self.run.id)['goal'], 'ship requested fix')

    def test_proof_verification_is_independent_and_rechecked(self):
        from cos import tasks
        tasks.contract(self.run.id, 'ship', [{'id':'health','title':'exact runtime','proofs':[
            {'kind':'health','url':'https://app.test/health','expected':{'version':'v1'}}]}])
        with patch('cos.proofs.verify', return_value={'verified': True, 'reason': 'ok'}):
            self.assertTrue(tasks.check(self.run.id, 'health')['verified'])
            self.assertEqual(tasks.unmet(self.run.id), [])
        with patch('cos.proofs.verify', return_value={'verified': False, 'reason': 'version differs'}):
            self.assertEqual(tasks.unmet(self.run.id), ['health: version differs'])

    def test_release_scope_cannot_silently_drift(self):
        from cos import tasks
        tasks.release(self.run.id, {'tag':'v1','source_sha':'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','native':{},'migrations':[], 'gates':[]})
        with self.assertRaises(ValueError):
            tasks.release(self.run.id, {'tag':'v2','source_sha':'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','native':{},'migrations':[], 'gates':[]})
        tasks.release(self.run.id, {'tag':'v2','source_sha':'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','native':{},'migrations':[], 'gates':[]}, approval='Owner approved expanded v2 scope')
        self.assertEqual(tasks.show(self.run.id)['release']['tag'], 'v2')

    def test_contract_replacement_requires_explicit_authorization(self):
        from cos import tasks
        steps=[{'id':'m','title':'merge','proofs':[{'kind':'pr','url':'https://github.com/acme/repo/pull/1'}]}]
        tasks.contract(self.run.id,'ship',steps)
        with self.assertRaises(ValueError):
            tasks.contract(self.run.id,'forget it',steps)

    def test_human_block_is_preserved(self):
        from cos import tasks
        tasks.block(self.run.id,'Bala must review stage video')
        self.assertEqual(tasks.show(self.run.id)['block'], 'Bala must review stage video')
        self.assertIn('Bala', tasks.unmet(self.run.id)[0])

    def test_steps_without_evidence_are_rejected(self):
        from cos import tasks
        with self.assertRaises(ValueError):
            tasks.contract(self.run.id,'ship',[{'id':'m','title':'merge','proofs':[]}])

    def test_mutable_source_ref_is_not_a_release_pin(self):
        from cos import tasks
        with self.assertRaises(ValueError):
            tasks.release(self.run.id, {'tag':'v1','source_sha':'main','native':{},'migrations':[], 'gates':[]})

    def test_changed_release_reopens_a_completed_contract(self):
        from cos import tasks
        scope={'tag':'v1','source_sha':'a'*40,'native':{},'migrations':[], 'gates':[]}
        tasks.contract(self.run.id,'ship',[{'id':'h','title':'runtime','proofs':[
            {'kind':'health','url':'https://test/health','expected':{'version':'v1'}}]}])
        tasks.release(self.run.id,scope)
        tasks.complete(self.run.id,tasks.show(self.run.id)['revision'])
        tasks.release(self.run.id,{**scope,'tag':'v2'},approval='owner approved v2')
        self.assertEqual(tasks.show(self.run.id)['status'],'open')

    def test_generic_health_is_not_release_version_proof(self):
        from cos import tasks
        tasks.contract(self.run.id,'ship',[{'id':'h','title':'runtime','proofs':[
            {'kind':'health','url':'https://test/health','expected':{'status':'ok'}}]}])
        tasks.release(self.run.id,{'tag':'v1','source_sha':'a'*40,'native':{},'migrations':[], 'gates':[]})
        with patch('cos.proofs.verify',return_value={'verified':True,'reason':'ok'}):
            self.assertIn('health proof differs from pinned release tag',tasks.unmet(self.run.id)[0])
            self.assertFalse(tasks.check(self.run.id,'h')['verified'])
