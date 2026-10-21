import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock


class ProofsTest(unittest.TestCase):
    def test_open_pr_is_not_merged(self):
        from cos import proofs
        with patch.object(proofs.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps({'state': 'OPEN', 'mergedAt': None, 'mergeCommit': None}), '')):
            self.assertFalse(proofs.verify({'kind': 'pr', 'url': 'https://github.com/o/r/pull/1'})['verified'])

    def test_exact_health_version(self):
        from cos import proofs
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"version":"old"}'
        with patch.object(proofs.urllib.request, 'urlopen', return_value=response):
            self.assertFalse(proofs.verify({'kind': 'health', 'url': 'https://service/health', 'expected': {'version': 'new'}})['verified'])

    def test_merged_pr_requires_merge_commit_and_expected_head(self):
        from cos import proofs
        for row, expected in [({'state': 'MERGED', 'mergedAt': 'today', 'mergeCommit': None}, False),
                              ({'state': 'MERGED', 'mergedAt': 'today', 'mergeCommit': {'oid': 'abc'}, 'headRefOid': 'wrong'}, False),
                              ({'state': 'MERGED', 'mergedAt': 'today', 'mergeCommit': {'oid': 'abc'}, 'headRefOid': 'wanted'}, True)]:
            with patch.object(proofs.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(row), '')) as run:
                result = proofs.verify({'kind': 'pr', 'url': 'https://github.com/o/r/pull/1', 'expected_head': 'wanted'})
                self.assertEqual(result['verified'], expected)
                self.assertIsInstance(result['checked_at'], float)
                self.assertNotIn('shell', run.call_args.kwargs)

    def test_workflow_requires_completed_success(self):
        from cos import proofs
        for status, conclusion, expected in [('in_progress', 'success', False), ('completed', 'failure', False), ('completed', 'success', True)]:
            with patch.object(proofs.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps({'status': status, 'conclusion': conclusion}), '')):
                result = proofs.verify({'kind': 'workflow', 'url': 'https://github.com/o/r/actions/runs/1'})
                self.assertEqual(result['verified'], expected)
                if expected:
                    self.assertIn('not runtime health', result['reason'])

    def test_errors_fail_closed_without_secrets(self):
        from cos import proofs
        with patch.object(proofs.urllib.request, 'urlopen', side_effect=TimeoutError('secret-token')):
            result = proofs.verify({'kind': 'health', 'url': 'https://service/health', 'expected': {'version': 'new'}})
            self.assertFalse(result['verified'])
            self.assertNotIn('secret', str(result))
        with patch.object(proofs.subprocess, 'run') as run:
            self.assertFalse(proofs.verify({'kind': 'pr', 'url': 'https://github.com/o/r/pull/1; touch /tmp/x'})['verified'])
            run.assert_not_called()

    def test_health_requires_nonempty_expected_fields(self):
        from cos import proofs
        with patch.object(proofs.urllib.request, 'urlopen') as get:
            self.assertFalse(proofs.verify({'kind': 'health', 'url': 'https://service/health', 'expected': {}})['verified'])
            get.assert_not_called()
