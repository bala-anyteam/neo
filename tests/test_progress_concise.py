import os
import unittest
from unittest.mock import patch

from commands import Progress


class ConciseProgressTest(unittest.TestCase):
    def progress(self):
        p = Progress('codex', 'headless', 'full', 'run-123', 'https://trace.example')
        p.add({'kind': 'tool_call', 'title': 'exec_command', 'body': 'git status --porcelain'})
        p.add({'kind': 'text', 'body': 'Release scope verified; checking runtime health.'})
        return p

    def test_default_hides_commands_but_preserves_timeline_and_useful_status(self):
        with patch.dict(os.environ, {'NEO_PROGRESS_DEBUG': ''}):
            p = self.progress()
            rendered = p.render()
        self.assertNotIn('git status', rendered)
        self.assertNotIn('exec_command', rendered)
        self.assertEqual(len(p.steps), 1)
        self.assertEqual(p.step_count, 1)
        self.assertIn('codex', rendered)
        self.assertIn('working', rendered)
        self.assertIn('checking runtime health', rendered)
        self.assertIn('full trace', rendered)

    def test_failed_tool_is_visible_without_command_list(self):
        p = self.progress()
        p.add({'kind': 'tool_result', 'title': 'exec_command', 'body': 'permission denied', 'data': {'is_error': True}})
        self.assertIn('permission denied', p.render())
        self.assertNotIn('git status', p.render())

    def test_error_without_tool_call_is_visible(self):
        p = Progress('codex', 'headless', 'full', 'run-123')
        p.add({'kind': 'tool_result', 'title': 'health', 'body': 'upstream unavailable', 'data': {'is_error': True}})
        self.assertIn('upstream unavailable', p.render())

    def test_debug_explicitly_shows_commands(self):
        with patch.dict(os.environ, {'NEO_PROGRESS_DEBUG': '1'}):
            self.assertIn('git status', self.progress().render())
