import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cos import bug_dms as dms

NOW = dt.datetime.now(dt.timezone.utc)
def ago(days):return (NOW-dt.timedelta(days=days)).isoformat()
def issue(n, days, bot=False):
 return {'number':n,'title':'[HIGH] Example bug','state':'OPEN','url':dms.url(n),'createdAt':ago(90),'labels':[{'name':'bug'},{'name':'high-priority'}],'assignees':[{'login':'praveen-anyteam'}],'comments':[{'author':{'login':'anyteam-devsvcs' if bot else 'praveen-anyteam','__typename':'User'},'createdAt':ago(days),'body':'update'}]}

class FollowupTests(unittest.TestCase):
 def dry_run(self, issues):
  home=Path(tempfile.mkdtemp())
  with patch.object(dms,'HERE',home),patch.object(dms,'STATE',home/'state.json'),patch('cos.watch_snapshot.collect',return_value={'issues':issues}),patch('sys.argv',['bug_dms','--dry-run']),contextlib.redirect_stdout(io.StringIO()):dms.main()
  return json.loads((home/('dms-'+dt.date.today().isoformat()+'.json')).read_text())
 def test_old_high_with_recent_human_update_is_not_messaged(self):
  self.assertEqual(self.dry_run([issue(1,1)]),{})
 def test_only_idle_issues_appear_in_owner_message(self):
  output=self.dry_run([issue(1,21),issue(2,1)])
  self.assertIn('[#1]',output['praveen-anyteam']['text'])
  self.assertNotIn('[#2]',output['praveen-anyteam']['text'])
 def test_bot_update_does_not_reset_human_inactivity(self):
  i=issue(1,21);i['comments'].append(issue(2,1,True)['comments'][0])
  self.assertIn('21',self.dry_run([i])['praveen-anyteam']['text'])
 def test_exactly_fourteen_days_is_not_eligible(self):
  i=issue(1,14)
  self.assertFalse(dms.eligible(i,NOW))
 def test_closed_issues_are_not_eligible(self):
  i=issue(1,21);i['state']='CLOSED'
  self.assertFalse(dms.eligible(i,NOW))
 def test_human_edit_resets_idle_clock(self):
  i=issue(1,21);i['lastEditedAt']=ago(1);i['editor']={'login':'praveen-anyteam','__typename':'User'}
  self.assertFalse(dms.eligible(i,NOW))
 def test_success_is_deduplicated_and_failure_can_retry(self):
  home=Path(tempfile.mkdtemp());state=home/'state.json'
  with patch.object(dms,'HERE',home),patch.object(dms,'STATE',state),patch('cos.watch_snapshot.collect',return_value={'issues':[issue(1,21)]}),patch.object(dms,'bot_token',return_value='test'),patch.object(dms,'send',side_effect=[{'ok':False,'error':'ratelimited'},{'ok':True,'channel':'D1','ts':'1'}]) as send,patch('sys.argv',['bug_dms']),contextlib.redirect_stdout(io.StringIO()):
   dms.main();self.assertFalse(state.exists())
   dms.main();self.assertTrue(state.exists())
   dms.main();self.assertEqual(send.call_count,2)
 def test_overlapping_invocation_does_not_collect_or_send(self):
  with patch.object(dms,'HERE',Path(tempfile.mkdtemp())),patch.object(dms.fcntl,'flock',side_effect=BlockingIOError),patch('cos.watch_snapshot.collect') as collect,contextlib.redirect_stdout(io.StringIO()):
   dms.main()
  collect.assert_not_called()
