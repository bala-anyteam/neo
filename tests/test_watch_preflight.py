"""No live CLI or network calls: preflight and cooldown regression coverage."""
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.test_watches import WatchTestCase
from cos.runner import Run


class WatchPreflightTests(WatchTestCase):
    def test_missing_selected_cli_records_failure_without_thread(self):
        watch = self.watches.save({'name': 'Missing', 'instructions': 'Read logs'})
        with patch.dict('os.environ', {'CLAUDE_CLI_PATH': '/missing/claude'}), patch('cos.watches.threading.Thread') as thread:
            meta = self.watches.start(watch['id'])
        self.assertEqual(meta['status'], 'failed')
        self.assertIn('CLI', meta['watch_preflight_error'])
        thread.assert_not_called()

    def test_codex_capability_requires_repo_before_dispatch(self):
        watch = self.watches.save({'name': 'Wrong capability', 'instructions': 'Read Sentry', 'engine': 'codex'})
        with patch.dict('os.environ', {'CODEX_CLI_PATH': str(self.fake)}), patch('cos.watches.threading.Thread') as thread:
            meta = self.watches.start(watch['id'])
        self.assertEqual(meta['status'], 'failed')
        self.assertIn('issue_repo', meta['watch_preflight_error'])
        thread.assert_not_called()

    def test_quota_failure_cools_schedule_but_manual_can_retry(self):
        watch = self.watches.save({'name': 'Quota', 'instructions': 'Check logs'})
        run = Run.create('claude', 'x', str(self.home / 'work'), 'readonly', source='schedule',
                         extra={'kind': 'watch', 'watch_id': watch['id']})
        run.append_event({'kind': 'error', 'title': 'Engine error', 'body': 'Credit balance is too low', 'data': {}}, 1)
        run.update_meta(status='failed')
        self.watches._update(watch['id'], last_run_id=run.id, last_started_at=datetime.now().isoformat())
        self.watches.checked(run)
        with patch('cos.watches.threading.Thread') as thread:
            scheduled = self.watches.start(watch['id'])
            self.assertEqual(scheduled['id'], run.id)
            thread.assert_not_called()
            manual = self.watches.start(watch['id'], source='dashboard')
            self.assertNotEqual(manual['id'], run.id)
            thread.assert_called_once()

    def test_expired_cooldown_and_new_engine_can_retry(self):
        watch = self.watches.save({'name': 'Expired', 'instructions': 'Check logs'})
        self.watches._update(watch['id'], cooldown_until=(datetime.now() - timedelta(seconds=1)).isoformat(), cooldown_engine='claude')
        with patch('cos.watches.threading.Thread') as thread:
            meta = self.watches.start(watch['id'])
        self.assertEqual(meta['engine'], 'claude')
        thread.assert_called_once()

    def test_auth_failure_from_stderr_and_cooldown_deadline_is_stable(self):
        watch = self.watches.save({'name': 'Auth', 'instructions': 'Check logs'})
        run = Run.create('claude', 'x', str(self.home / 'work'), 'readonly', source='schedule',
                         extra={'kind': 'watch', 'watch_id': watch['id']})
        (run.dir / 'stderr.log').write_text('Authentication failed: token expired')
        run.update_meta(status='failed')
        self.watches._update(watch['id'], last_run_id=run.id)
        self.watches.checked(run)
        first = self.watches.get(watch['id'])['cooldown_until']
        self.watches.checked(run)
        self.assertEqual(self.watches.get(watch['id'])['cooldown_until'], first)

    def test_cooldown_does_not_switch_or_block_explicit_new_engine(self):
        watch = self.watches.save({'name': 'Changed', 'instructions': 'Check bugs', 'engine': 'codex', 'issue_repo': 'acme/design'})
        self.watches._update(watch['id'], cooldown_until=(datetime.now() + timedelta(minutes=30)).isoformat(), cooldown_engine='claude')
        with patch.dict('os.environ', {'CODEX_CLI_PATH': str(self.fake)}), patch('cos.watches.threading.Thread') as thread:
            meta = self.watches.start(watch['id'])
        self.assertEqual(meta['engine'], 'codex')
        thread.assert_called_once()
