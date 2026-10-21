from pathlib import Path
import unittest
import json
import re


class OperatingBriefTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.brief = (Path(__file__).resolve().parents[1] / 'prompts/slack-agent.md').read_text()

    def test_contract_commands_work_outside_nio_repository(self):
        for command in ('contract', 'check', 'show', 'release', 'block'):
            self.assertIn(f'"$NEO_TASKS_PYTHON" "$NEO_TASKS_CLI" {command}', self.brief)
        self.assertIn('COS_HOME', self.brief)
        self.assertIn('informational', self.brief)
        self.assertIn('independently', self.brief)

    def test_release_scope_and_real_approval_sources_are_explicit(self):
        for term in ('source_sha', 'native', 'migrations', 'gates', 'PMR', 'stage-video', 'Current owner instructions'):
            self.assertIn(term, self.brief)
        self.assertNotIn('or a\n    rollback', self.brief)
        self.assertIn('do not invent', self.brief)

    def test_release_example_matches_cli_and_records_gate_approval(self):
        match = re.search(r"release --run \"\$NEO_RUN_ID\" --scope '(.*?)'", self.brief)
        scope = json.loads(match[1])
        self.assertIsInstance(scope['native'], dict)
        self.assertNotIn('--replace', self.brief)
        self.assertIn('"$NEO_TASKS_PYTHON" "$NEO_TASKS_CLI" unblock', self.brief)
        self.assertIn('--approval', self.brief)
