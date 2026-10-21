import json
import tempfile
import unittest
from pathlib import Path

from cos import failure_context


class FailureContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'20261021-160230-8cff60'
        self.path.mkdir()
        (self.path/'meta.json').write_text(json.dumps({'engine':'claude','status':'failed'}))

    def event(self, text, kind='error'):
        with (self.path/'events.jsonl').open('a') as f:
            f.write(json.dumps({'kind':kind,'body':text})+'\n')

    def test_quota_notice_explains_monitoring_failure_and_reset(self):
        self.event("You've hit your weekly limit · resets Oct 23 at 6:30am (Asia/Calcutta)", 'result')
        text=failure_context.safe_reason(self.path)
        self.assertIn('weekly usage limit',text)
        self.assertIn('Oct 23 at 6:30am',text)
        self.assertIn('retry',text.lower())

    def test_unknown_error_does_not_echo_secrets_or_invent_cause(self):
        self.event('private raw payload sk-live-SECRET customer@example.com')
        text=failure_context.safe_reason(self.path)
        self.assertNotIn('SECRET',text)
        self.assertNotIn('customer@',text)
        self.assertIn('not identified',text)

    def test_busy_exception_is_not_a_task_failure(self):
        text=failure_context.safe_reason(self.path, RuntimeError('run is already executing a turn'))
        self.assertIn('already running',text)
        self.assertIn('not started',text)

    def test_tool_output_is_not_misclassified_as_engine_error(self):
        self.event('weekly limit',kind='tool_result')
        self.assertIn('not identified',failure_context.safe_reason(self.path))

    def test_old_turn_error_is_not_attributed_to_new_turn(self):
        (self.path/'meta.json').write_text(json.dumps({'engine':'claude','turns':2,'status':'failed'}))
        (self.path/'events.jsonl').write_text(json.dumps({'turn':1,'kind':'error','body':'weekly limit'})+'\n')
        self.assertIn('not identified',failure_context.safe_reason(self.path))
