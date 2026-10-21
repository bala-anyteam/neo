import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_watches import WatchTestCase, ROOT

FAKE_CODEX = '''#!/usr/bin/env python3
import json, sys
from pathlib import Path
a = sys.argv[1:]
assert a[a.index('-s') + 1] == 'read-only', a
assert '--allowedTools' not in a and '--json-schema' not in a, a
s = json.loads(Path(a[a.index('--output-schema') + 1]).read_text())
assert s['additionalProperties'] is False
assert s['properties']['findings']['items']['additionalProperties'] is False
print(json.dumps({'type': 'thread.started', 'thread_id': 'codex-watch'}))
print(json.dumps({'type': 'item.completed', 'item': {'type':'agent_message', 'text': json.dumps({'summary':'Design checked.', 'findings':[{'key':'design#1','title':'Bug','severity':'high','what':'broken','evidence':'repro','link':'https://github.com/AnyTeamAI/anyteam-design/issues/1','repo':'AnyTeamAI/anyteam-design','next_step':'Investigate'}]})}}))
print(json.dumps({'type':'turn.completed','usage':{}}))
'''

class CodexWatchTest(WatchTestCase):
    def test_codex_watch_records_structured_findings(self):
        fake = self.home / 'codex'
        fake.write_text(FAKE_CODEX)
        fake.chmod(0o755)
        w = self.watches.save({'name':'Triage','instructions':'Check design','engine':'codex','issue_repo':'AnyTeamAI/anyteam-design'})
        self.assertEqual(w.get('engine'), 'codex')
        with patch.dict('os.environ', {'CODEX_CLI_PATH':str(fake)}), patch('cos.watch_snapshot.collect', return_value={'issues':[]}):
            self.watches.start(w['id'])
            m = self.wait_for_check(w['id'])
        self.assertEqual(m['engine'], 'codex')
        self.assertEqual(m['watch_found'], 1)
        self.assertEqual(m['watch_summary'], 'Design checked.')
        self.assertIsNotNone(self.watches.get(w['id']).get('last_ok_at'))

    def test_scoped_prompt_excludes_old_foreign_findings(self):
        w = {'id':'bugs-triage','name':'Triage','instructions':'Check bugs','issue_repo':'AnyTeamAI/anyteam-design'}
        self.watches._write('findings.json', {'x':{'watch_id':w['id'],'key':'backie#1','title':'foreign backlog','repo':'AnyTeamAI/anyteam-backie','status':'new','last_seen':'2026-10-20'}})
        p = self.watches.prompt(w)
        self.assertNotIn('foreign backlog', p)
        self.assertIn('--repo AnyTeamAI/anyteam-design', p)

    def test_foreign_findings_are_rejected_before_persistence(self):
        from cos.runner import Run
        w = self.watches.save({'name':'Triage','instructions':'Check bugs','issue_repo':'AnyTeamAI/anyteam-design'})
        r = Run.create('claude','x',str(self.home/'work'),'readonly')
        f = {'key':'backie#1','title':'foreign','severity':'high','repo':'AnyTeamAI/anyteam-backie'}
        (r.dir/'raw.jsonl').write_text(json.dumps({'type':'result','structured_output':{'summary':'Checked','findings':[f]}})+'\n')
        self.assertEqual(self.watches.finish(w,r,r.meta()['created_at']), [])
        self.assertEqual(self.watches._findings(), {})

    def test_invalid_engine_is_rejected(self):
        with self.assertRaises(ValueError):
            self.watches.save({'name':'Triage','instructions':'Check bugs','engine':'invalid'})

    def test_codex_uses_isolated_snapshot_without_remote_tools(self):
        fake = self.home / 'codex'
        fake.write_text(FAKE_CODEX)
        fake.chmod(0o755)
        w = self.watches.save({'name':'Triage','instructions':'Check design','engine':'codex','issue_repo':'AnyTeamAI/anyteam-design'})
        with patch.dict('os.environ', {'CODEX_CLI_PATH':str(fake)}), patch('cos.watch_snapshot.collect', return_value={'issues':[]}) as collect:
            self.watches.start(w['id'])
            m = self.wait_for_check(w['id'])
        collect.assert_called_once_with('AnyTeamAI/anyteam-design')
        self.assertIn('--ignore-user-config', m['cli_args'])
        self.assertIn('apps', m['cli_args'])
        self.assertNotIn(m['cwd'], (str(self.home/'work'), str(ROOT)))
        self.assertTrue((Path(m['cwd'])/'github-snapshot.json').is_file())

    def test_daily_draft_cannot_embed_foreign_issues(self):
        w = {'issue_repo':'AnyTeamAI/anyteam-design'}
        self.assertFalse(self.watches.in_scope(w, {'repo':'AnyTeamAI/anyteam-design','key':'daily-update:2026-10-21','what':'Fix https://github.com/AnyTeamAI/anyteam-backie/issues/1'}))

    def test_snapshot_failure_does_not_advance_last_success(self):
        import time
        w = self.watches.save({'name':'Triage','instructions':'Check design','engine':'codex','issue_repo':'AnyTeamAI/anyteam-design'})
        with patch('cos.watch_snapshot.collect', side_effect=RuntimeError('GitHub unavailable')):
            m = self.watches.start(w['id'])
            from cos.runner import Run
            r = Run(m['id'])
            for _ in range(100):
                if r.meta()['status'] == 'failed':
                    break
                time.sleep(.02)
        self.assertEqual(r.meta()['status'], 'failed')
        self.assertIsNone(self.watches.get(w['id']).get('last_ok_at'))


class SnapshotTests(unittest.TestCase):
    def test_issue_comment_and_timeline_pagination(self):
        from cos.watch_snapshot import collect
        end = {'hasNextPage':False, 'endCursor':None}
        issue = {'number':1,'comments':{'nodes':[{'body':'first'}],'pageInfo':{'hasNextPage':True,'endCursor':'c1'}},'timelineItems':{'nodes':[],'pageInfo':{'hasNextPage':True,'endCursor':'t1'}}}
        pages = [
            {'issues':{'nodes':[issue],'pageInfo':{'hasNextPage':True,'endCursor':'i1'}}},
            {'issues':{'nodes':[], 'pageInfo':end}},
            {'issue':{'comments':{'nodes':[{'body':'second'}],'pageInfo':end}}},
            {'issue':{'timelineItems':{'nodes':[{'source':{'state':'MERGED'}}],'pageInfo':end}}},
        ]
        with patch('cos.watch_snapshot.graphql', side_effect=pages) as query, patch('cos.watch_snapshot.gh', return_value=[]):
            s = collect('acme/design')
        self.assertEqual(len(s['issues'][0]['comments']['nodes']),2)
        self.assertEqual(len(s['issues'][0]['timelineItems']['nodes']),1)
        self.assertEqual(query.call_args_list[1].kwargs['cursor'],'i1')
        self.assertEqual(query.call_args_list[2].kwargs['cursor'],'c1')
        self.assertEqual(query.call_args_list[3].kwargs['cursor'],'t1')

    def test_collector_only_executes_fixed_read_query(self):
        from cos.watch_snapshot import collect
        import subprocess
        reply = {'data':{'repository':{'issues':{'nodes':[],'pageInfo':{'hasNextPage':False}}}}}
        with patch('cos.watch_snapshot.subprocess.run', return_value=subprocess.CompletedProcess([],0,json.dumps(reply),'')) as proc:
            collect('acme/design')
        args = proc.call_args_list[0].args[0]
        self.assertEqual(args[:3], ['gh','api','graphql'])
        self.assertIn('owner=acme',args)
        self.assertNotIn('mutation', ' '.join(args))
