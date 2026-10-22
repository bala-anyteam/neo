import unittest
from unittest.mock import patch
from datetime import datetime, timezone

class SourceSnapshotTest(unittest.TestCase):
    def test_temporary_engine_expires_without_mutating(self):
        from cos.watches import effective_engine
        w={'engine':'claude','engine_override':{'engine':'codex','expires_at':'2026-10-26T00:00:00+05:30'}}
        self.assertEqual(effective_engine(w, datetime(2026,10,22,tzinfo=timezone.utc)), 'codex')
        self.assertEqual(effective_engine(w, datetime(2026,10,26,tzinfo=timezone.utc)), 'claude')
        self.assertEqual(w['engine'], 'claude')
    def test_unknown_source_rejected(self):
        from cos.watch_source_snapshot import collect
        with self.assertRaises(ValueError): collect({'id':'unknown'})
    def test_source_failure_explicit_and_sanitized(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._sentry', side_effect=RuntimeError('secret')):
            s=collect({'id':'sentry'})
        self.assertTrue(s['source_errors'])
        self.assertNotIn('secret', str(s))
    def test_ci_collects_read_only_bounded_commands(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._read', return_value=[]) as read, patch('cos.watch_source_snapshot._slack_history', return_value={'messages':[]}):
            s=collect({'id':'desk-ci-fixes'})
        self.assertFalse(s['source_errors'])
        self.assertEqual(read.call_count, 2)
        self.assertIn('--repo', read.call_args_list[0].args[0])
        self.assertIn('AnyTeamAI/anyteam-deskie', read.call_args_list[0].args[0])

from tests.test_watches import WatchTestCase
class PartialSourceTest(WatchTestCase):
    def test_partial_sources_cannot_record_clean_success(self):
        import json
        from cos.runner import Run
        w=self.watches.save({'name':'Partial','instructions':'Check'})
        r=Run.create('codex','x',str(self.home/'work'),'readonly',extra={'watch_source_errors':['logs unavailable']})
        (r.dir/'raw.jsonl').write_text(json.dumps({'type':'result','structured_output':{'summary':'Everything clean','findings':[]}})+'\n')
        self.watches.finish(w,r,r.meta()['created_at'])
        self.assertIn('incomplete',r.meta()['watch_summary'].lower())
        self.assertIsNone(self.watches.get(w['id']).get('last_ok_at'))
    def test_builtin_codex_uses_isolated_source_collector(self):
        import time
        from cos.runner import Run
        self.watches._write('watches.json', [{'id':'desk-ci-fixes','name':'Desk CI','instructions':'Check Deskie CI',
            'every':60,'hours':'always','enabled':True,'engine':'claude',
            'engine_override':{'engine':'codex','expires_at':'2099-01-01T00:00:00+00:00'}}])
        with patch.dict('os.environ', {'CODEX_CLI_PATH':str(self.fake)}), \
             patch('cos.watch_source_snapshot.collect', return_value={'sources':{},'source_errors':['test missing']}) as collect, \
             patch.object(Run,'execute',lambda r:r.update_meta(status='done')):
            m=self.watches.start('desk-ci-fixes')
            for _ in range(100):
                meta=Run(m['id']).meta()
                if (__import__('pathlib').Path(meta['cwd'])/'github-snapshot.json').is_file(): break
                time.sleep(.01)
            collect.assert_called_once()
        self.assertEqual(meta['engine'],'codex')
        self.assertNotEqual(meta['cwd'],str(self.home/'work'))
        self.assertTrue((__import__('pathlib').Path(meta['cwd'])/'github-snapshot.json').is_file())
    def test_sentry_api_keeps_org_scope_and_latest_event(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._sentry', side_effect=[[{'id':'123','count':'9','userCount':4}], {'release':'v1'}]) as read:
            snapshot=collect({'id':'sentry'})
        self.assertEqual(read.call_count,2)
        self.assertTrue(read.call_args_list[0].args[0].startswith('organizations/anyteam-ai/issues/?'))
        self.assertEqual(read.call_args_list[1].args[0],'issues/123/events/latest/')
        self.assertFalse(snapshot['source_errors'])
    def test_empty_sentry_source_is_valid_clean_sample(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._sentry', return_value=[]):
            self.assertFalse(collect({'id':'sentry'})['source_errors'])

    def test_ci_slack_failure_stays_explicit(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._read', return_value=[]), patch('cos.watch_source_snapshot._slack_history', side_effect=RuntimeError('token-secret')):
            snapshot=collect({'id':'desk-ci-fixes'})
        self.assertTrue(snapshot['source_errors'])
        self.assertNotIn('token-secret', str(snapshot))
    def test_prod_window_filter_and_saturation(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._read',return_value=[{}]*100) as read:
            s=collect({'id':'prod-logs','instructions':'--project anyteam-infrie-prod','last_ok_at':'2026-10-20T01:00:00+00:00'})
        query=read.call_args.args[0][3]
        self.assertIn('2026-10-20T01:00:00+00:00',query)
        self.assertIn('jsonPayload.message',query)
        self.assertTrue(s['source_errors'])
    def test_sentry_missing_details_incomplete(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._sentry',side_effect=[[{'id':str(i)} for i in range(4)],{}, {}, {}]):
            self.assertTrue(collect({'id':'sentry'})['source_errors'])
    def test_slack_old_run_link_gets_fetched(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._read',return_value=[]) as read, patch('cos.watch_source_snapshot._slack_history',return_value={'messages':[{'text':'https://github.com/AnyTeamAI/anyteam-deskie/actions/runs/123456'}]}):
            collect({'id':'desk-ci-fixes'})
        self.assertTrue(any('123456' in call.args[0] for call in read.call_args_list))

class PRSampleTest(unittest.TestCase):
    def test_saturated_pr_sample_is_incomplete(self):
        from cos.watch_source_snapshot import collect
        with patch('cos.watch_source_snapshot._read', side_effect=[[],[{}]*30]), \
             patch('cos.watch_source_snapshot._slack_history', return_value={'messages':[]}):
            snapshot=collect({'id':'desk-ci-fixes'})
        self.assertTrue(any('PR sample' in e for e in snapshot['source_errors']))
