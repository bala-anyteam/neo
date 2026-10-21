"""Clean waiting exits must not silently abandon Slack work (both engine adapters)."""
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cos import runner

FAKE_ENGINE = '''#!{python}
import json, pathlib, sys
root = pathlib.Path({root!r})
n = int((root / "calls").read_text()) if (root / "calls").exists() else 0
(root / "calls").write_text(str(n + 1))
(root / ("args-" + str(n))).write_text(json.dumps(sys.argv[1:]))
state = (root / "state").read_text()
if state == "transient":
    state = "waiting" if n == 0 else "done"
    if n == 1:
        print(json.dumps({{"type": "error", "message": "stream disconnected before completion"}}))
        sys.exit(1)
text = "Status: " + (state if n == 0 or state != "waiting" else "done") + " — scoped test"
if {engine!r} == "claude":
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "test-session"}}))
    print(json.dumps({{"type": "result", "is_error": False, "session_id": "test-session", "result": text}}))
else:
    print(json.dumps({{"type": "thread.started", "thread_id": "test-session"}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": text}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{}}}}))
'''


class WaitingContinuationTest(unittest.TestCase):
    def exercise(self, engine, state="waiting", source="slack", conclude=True):
        root = Path(self.tmp.name)
        fake = root / engine
        fake.write_text(FAKE_ENGINE.format(python=os.sys.executable, root=str(root), engine=engine))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        (root / "state").write_text(state)
        self.env = patch.dict(os.environ, {"COS_HOME": str(root), engine.upper() + "_CLI_PATH": str(fake)})
        self.env.start()
        self.addCleanup(self.env.stop)
        return runner.Run.create(engine, "finish authorized deployment", str(root), "full", source=source,
                                 extra={"conclude": conclude})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        delays = patch.object(runner, "WAITING_CONTINUATION_DELAYS", (0.01, 0.01))
        delays.start()
        self.addCleanup(delays.stop)

    def test_codex_waiting_resumes_same_session(self):
        self.assert_continuation("codex")

    def test_claude_waiting_resumes_same_session(self):
        self.assert_continuation("claude")

    def assert_continuation(self, engine):
        run = self.exercise(engine)
        events = []
        meta = run.execute(on_event=events.append)
        self.assertEqual((Path(self.tmp.name) / "calls").read_text(), "2")
        args = json.loads((Path(self.tmp.name) / "args-1").read_text())
        self.assertIn("test-session", args)
        self.assertEqual(meta["conclusion"]["state"], "done")
        self.assertTrue(any(e["title"] == "Continue unfinished task" for e in events))

    def test_human_gate_is_not_resumed(self):
        run = self.exercise("codex", state="needs-you")
        self.assertEqual(run.execute()["conclusion"]["state"], "needs-you")
        self.assertEqual((Path(self.tmp.name) / "calls").read_text(), "1")

    def test_scheduled_job_is_not_resumed(self):
        run = self.exercise("codex", source="cli", conclude=False)
        run.execute()
        self.assertEqual((Path(self.tmp.name) / "calls").read_text(), "1")

    def test_repeated_waiting_is_bounded_and_reports_failure(self):
        run = self.exercise("codex", state="waiting — still pending\nStatus: waiting")
        events = []
        meta = run.execute(on_event=events.append)
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["conclusion"]["state"], "blocked")
        self.assertTrue(any(e["kind"] == "error" and e["title"] == "Continuation limit reached" for e in events))
        self.assertIsNone(meta.get("retry_owner"))

    def test_cancellation_interrupts_the_continuation_wait(self):
        run = self.exercise("codex")
        result = {}
        with patch.object(runner, "WAITING_CONTINUATION_DELAYS", (30.0,)):
            worker = threading.Thread(target=lambda: result.update(run.execute()))
            worker.start()
            deadline = time.monotonic() + 5
            while not run.meta().get("retry_owner") and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(run.meta()["status"], "running")
            self.assertTrue(runner.Run(run.id).cancel())
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual((Path(self.tmp.name) / "calls").read_text(), "1")

    def test_adopted_engine_keeps_the_continuation_cap(self):
        run = self.exercise("codex")
        run.update_meta(status="running", pid=os.getpid(), turns=3,
                        waiting_continuations=2, session_id="test-session")
        finished = {**run.meta(), "status": "done", "conclusion": {"state": "waiting"}}
        def finish(*args, **kwargs):
            return run.update_meta(**finished)
        with patch.object(run, "_follow", side_effect=finish):
            meta = run.adopt()
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["conclusion"]["state"], "blocked")
        self.assertFalse((Path(self.tmp.name) / "calls").exists())

    def test_transient_failure_during_continuation_is_retried(self):
        run = self.exercise("codex", state="transient")
        with patch.object(runner, "AUTO_RESTART_DELAYS", (0.01,)):
            meta = run.execute()
        self.assertEqual(meta["status"], "done")
        self.assertEqual((Path(self.tmp.name) / "calls").read_text(), "3")

    def test_exhausted_old_turn_cannot_overwrite_a_new_turn(self):
        run = self.exercise("codex")
        old = run.update_meta(status="done", turns=1, session_id="test-session",
                              waiting_continuations=2, conclusion={"state": "waiting"})
        run.update_meta(status="running", turns=2)
        meta = run._continue_waiting(old)
        self.assertEqual(meta["status"], "running")
        self.assertEqual(meta["turns"], 2)
