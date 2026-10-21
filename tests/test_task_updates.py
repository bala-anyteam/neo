import tempfile
import unittest
from pathlib import Path
from cos import task_updates

class TaskUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_first_delivery_and_repeated_waiting_are_fifteen_minutes_apart(self):
        token = task_updates.begin(self.path, now=100)
        task_updates.queue(self.path, 'waiting', token=token)
        self.assertIsNone(task_updates.due(self.path, active=True, now=999))
        item = task_updates.due(self.path, active=True, now=1000)
        self.assertEqual(item['body'], 'waiting')
        task_updates.ack(self.path, item, now=1000)
        self.assertIsNone(task_updates.due(self.path, active=True, now=1899))
        self.assertEqual(task_updates.due(self.path, active=True, now=1900)['body'], 'waiting')

    def test_terminal_is_coalesced_delayed_restart_safe_and_sent_once(self):
        token = task_updates.begin(self.path, now=100)
        task_updates.queue(self.path, 'old', token=token)
        task_updates.queue(self.path, 'done', token=token)
        self.assertIsNone(task_updates.due(self.path, active=False, now=999))
        item = task_updates.due(Path(str(self.path)), active=False, now=1000)
        self.assertEqual(item['body'], 'done')
        task_updates.ack(self.path, item, now=1000)
        self.assertIsNone(task_updates.due(self.path, active=False, now=1900))

    def test_new_owner_request_rejects_old_result_and_old_delivery_ack(self):
        old = task_updates.begin(self.path, now=100)
        task_updates.queue(self.path, 'old', token=old)
        item = task_updates.due(self.path, active=False, now=1000)
        new = task_updates.begin(self.path, now=1001)
        task_updates.queue(self.path, 'stale', token=old)
        task_updates.ack(self.path, item, now=1002)
        self.assertIsNone(task_updates.due(self.path, active=False, now=2000))
        task_updates.queue(self.path, 'new', token=new)
        self.assertEqual(task_updates.due(self.path, active=False, now=2000)['body'], 'new')

    def test_mute_and_cancellation_suppress_pending(self):
        token=task_updates.begin(self.path, now=100)
        task_updates.queue(self.path, 'done', token=token)
        self.assertIsNone(task_updates.due(self.path, active=False, enabled=False, now=1000))
        task_updates.clear(self.path)
        self.assertIsNone(task_updates.due(self.path, active=True, now=1000))

    def test_direct_terminal_answer_is_not_duplicated(self):
        token=task_updates.begin(self.path, now=100)
        task_updates.queue(self.path, 'direct answer', token=token, pending=False)
        self.assertIsNone(task_updates.due(self.path, active=False, now=1000))
