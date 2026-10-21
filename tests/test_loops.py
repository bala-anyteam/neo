"""Loops: issues and PRs followed from detection to a verified fix."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
H = 3600.0
T0 = 1_790_000_000.0


class LoopsTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()
        from cos import loops
        self.loops = loops
        import subprocess
        self.gh = patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"state":"MERGED","mergedAt":"2026-10-21T00:00:00Z","mergeCommit":{"oid":"abc"}}', ''))
        self.gh.start()
        self.addCleanup(self.gh.stop)
        self.health = patch("cos.proofs.urllib.request.urlopen")
        response = self.health.start()
        response.return_value.__enter__.return_value.read.return_value = b'{"version":"1"}'
        self.addCleanup(self.health.stop)

    def tearDown(self):
        self.env.stop()

    def up(self, lid, stage, at, evidence="seen it", **kw):
        if stage in ("merged", "deployed", "verified") and lid in self.loops.STORE.read():
            self.loops.STORE.update(lambda rows: rows[lid]["links"].update(prs=rows[lid]["links"].get("prs") or ["https://github.com/o/r/pull/7"]))
            kw.setdefault("links", {"prs": self.loops.STORE.read()[lid]["links"]["prs"]})
            kw.setdefault("proof", {"kind": "health", "url": "https://service/health", "expected": {"version": "1"}})
        return self.loops.apply_update(lid, {"stage": stage, "note": kw.pop("note", ""), "evidence": evidence, **kw}, now=at)

    def test_open_pr_cannot_be_claimed_merged(self):
        l = self.loops.open_loop("pr", "pr:o/r#7", "t", {"prs": ["https://github.com/o/r/pull/7"]}, stage="fix_pr")
        import subprocess
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"state":"OPEN","mergedAt":null,"mergeCommit":null}', '')):
            ok, _ = self.up(l["id"], "merged", T0, evidence="MERGED")
        self.assertFalse(ok)

    def test_canonical_pr_identity_cannot_be_replaced_by_model_link(self):
        l = self.loops.open_loop("pr", "pr:o/r#7", "t", {"prs": ["https://github.com/o/r/pull/8"]}, stage="fix_pr")
        ok, _ = self.loops.apply_update(l["id"], {"stage": "merged", "evidence": "PR 8 merged", "links": {"prs": ["https://github.com/o/r/pull/8"]}})
        self.assertTrue(ok)
        from cos import proofs
        self.assertIn("https://github.com/o/r/pull/7", proofs.subprocess.run.call_args.args[0])

    def test_workflow_proof_cannot_claim_runtime_deployment(self):
        l = self.loops.open_loop("pr", "pr:o/r#7", "t", {}, stage="merged")
        ok, why = self.loops.apply_update(l["id"], {"stage": "deployed", "evidence": "workflow success", "proof": {"kind": "workflow", "url": "https://github.com/o/r/actions/runs/1"}})
        self.assertFalse(ok)
        self.assertIn("health", why)

    def test_superseded_alert_pr_does_not_block_selected_merged_fix(self):
        old, fix = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"
        l = self.loops.open_loop("alert", "k", "t", {"prs": [old, fix]}, stage="fix_pr")
        with patch("cos.proofs.verify", side_effect=lambda spec: {"verified": spec["url"] == fix}):
            self.assertTrue(self.loops.apply_update(l["id"], {"stage": "merged", "evidence": "replacement merged", "links": {"prs": [fix]}})[0])

    def test_same_stage_close_does_not_require_live_deployment_proofs(self):
        l = self.loops.open_loop("alert", "k", "t", {}, stage="deployed")
        with patch("cos.proofs.verify", side_effect=AssertionError("no proof fetch for informational close")):
            self.assertTrue(self.loops.apply_update(l["id"], {"stage": "deployed", "close": True, "evidence": "issue no longer applies"})[0])
        self.assertEqual(self.loops.STORE.read()[l["id"]]["status"], "closed")

    def test_forward_close_cannot_bypass_proofs(self):
        l = self.loops.open_loop("alert", "k", "t", {}, stage="fix_pr")
        self.assertFalse(self.loops.apply_update(l["id"], {"stage": "deployed", "close": True, "evidence": "claimed"})[0])

    def test_refusal_ledger_contains_no_model_evidence(self):
        l = self.loops.open_loop("alert", "k", "t", {}, stage="fix_pr")
        self.loops.apply_update(l["id"], {"stage": "merged", "evidence": "secret-token"}, now=T0)
        path = Path(os.environ["COS_HOME"]) / "evidence-refusals.jsonl"
        self.assertTrue(path.exists())
        row = json.loads(path.read_text())
        self.assertEqual(row["loop_id"], l["id"])
        self.assertIn("ts", row)
        self.assertNotIn("secret-token", path.read_text())

    def test_alert_progress_needs_explicit_linked_pr_selection(self):
        linked = "https://github.com/o/r/pull/2"
        l = self.loops.open_loop("alert", "k", "t", {"prs": [linked]}, stage="fix_pr")
        for links in ({}, {"prs": ["https://github.com/o/r/pull/99"]}):
            self.assertFalse(self.loops.apply_update(l["id"], {"stage": "merged", "evidence": "claimed", "links": links})[0])

    def test_open_is_idempotent_by_key(self):
        a = self.loops.open_loop("alert", "slack:C1:1.1", "HighErrorRate api", {"source": "u"}, now=T0)
        b = self.loops.open_loop("alert", "slack:C1:1.1", "HighErrorRate api", {}, now=T0 + 5)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.loops.listing(now=T0 + 5)), 1)

    def test_forward_update_records_history(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        ok, _ = self.up(l["id"], "fix_pr", T0 + H, note="PR opened", links={"prs": ["https://github.com/o/r/pull/1"]})
        self.assertTrue(ok)
        got = self.loops.listing(now=T0 + H)[0]
        self.assertEqual(got["stage"], "fix_pr")
        self.assertEqual(got["links"]["prs"], ["https://github.com/o/r/pull/1"])
        self.assertEqual(got["history"][-1]["note"], "PR opened")

    def test_no_backwards_without_regression(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.up(l["id"], "merged", T0 + H)
        ok, why = self.up(l["id"], "fix_pr", T0 + 2 * H)
        self.assertFalse(ok)
        self.assertIn("backwards", why)

    def test_update_without_evidence_or_unknown_stage_is_refused(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.assertFalse(self.up(l["id"], "merged", T0 + H, evidence="  ")[0])
        self.assertFalse(self.up(l["id"], "shipped", T0 + H)[0])
        self.assertFalse(self.up("nope", "merged", T0 + H)[0])

    def test_verified_needs_quiet_window(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.up(l["id"], "deployed", T0)
        self.assertFalse(self.up(l["id"], "verified", T0 + 2 * H)[0])
        self.assertFalse(self.up(l["id"], "verified", T0 + 2 * H)[0])
        self.assertTrue(self.up(l["id"], "verified", T0 + 25 * H)[0])

    def test_verified_cannot_skip_deployed(self):
        l = self.loops.open_loop("pr", "k", "t", {}, now=T0, stage="fix_pr")
        ok, why = self.up(l["id"], "verified", T0 + 100 * H)
        self.assertFalse(ok)
        self.assertIn("deployed", why)

    def test_regression_reopens_and_flags(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.up(l["id"], "deployed", T0)
        self.up(l["id"], "verified", T0 + 25 * H)
        ok, _ = self.up(l["id"], "rca", T0 + 30 * H, regressed=True, note="back")
        got = self.loops.listing(now=T0 + 30 * H)[0]
        self.assertTrue(ok)
        self.assertTrue(got["regressed"])
        self.assertEqual(got["status"], "open")

    def test_a_later_forward_update_without_regression_clears_the_flag(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.up(l["id"], "deployed", T0)
        self.up(l["id"], "rca", T0 + 5 * H, regressed=True, note="fired after the deploy")
        self.up(l["id"], "deployed", T0 + 7 * H, note="fix is in prod; the other PR was closed")
        got = self.loops.listing(now=T0 + 7 * H)[0]
        self.assertFalse(got["regressed"])
        self.assertEqual(got["stage"], "deployed")

    def test_same_alert_firing_after_the_fix_is_in_prod_is_a_regression(self):
        old = self.loops.open_loop("alert", "slack:C1:1.0", "[PROD][FIRING] X (1)", {}, now=T0)
        self.up(old["id"], "deployed", T0 + H)
        self.loops.open_loop("alert", "slack:C1:9.0", "[PROD][FIRING]  X (1)", {}, now=T0 + 5 * H)
        hit = self.loops.note_refire("slack:C1:9.0", "[PROD][FIRING]  X (1)", "https://s/p9", now=T0 + 5 * H)
        self.assertEqual(hit, [old["id"]])
        got = {l["id"]: l for l in self.loops.listing(now=T0 + 5 * H)}[old["id"]]
        self.assertTrue(got["regressed"])
        self.assertIn("Fired again", got["history"][-1]["note"])
        self.assertIn("https://s/p9", got["history"][-1]["evidence"])

    def test_a_firing_before_the_fix_reached_prod_or_on_a_muted_loop_is_not_a_regression(self):
        merged = self.loops.open_loop("alert", "slack:C1:1.0", "A", {}, now=T0)
        self.up(merged["id"], "merged", T0 + H)
        muted = self.loops.open_loop("alert", "slack:C1:2.0", "B", {}, now=T0)
        self.up(muted["id"], "deployed", T0 + H)
        self.loops.set_status(muted["id"], "muted")
        self.assertEqual(self.loops.note_refire("slack:C1:3.0", "A", now=T0 + 5 * H), [])
        self.assertEqual(self.loops.note_refire("slack:C1:4.0", "B", now=T0 + 5 * H), [])

    def test_mark_checked_stamps_the_loops_a_check_covered(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.loops.mark_checked([l["id"], "gone"], T0 + H)
        self.assertEqual(self.loops.listing(now=T0 + H)[0]["checked_at"], T0 + H)

    def test_stalled_rules(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.assertIsNone(self.loops.stalled(l, T0 + H))
        self.assertIn("No RCA", self.loops.stalled(l, T0 + 5 * H))
        self.up(l["id"], "merged", T0)
        got = self.loops.listing(now=T0 + 25 * H)[0]
        self.assertIn("not deployed", got["stalled"])
        self.loops.set_status(l["id"], "muted")
        self.assertIsNone(self.loops.listing(now=T0 + 25 * H)[0]["stalled"])

    def test_closed_loops_drop_off_after_a_day(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.loops.set_status(l["id"], "closed")
        self.assertEqual(len(self.loops.listing()), 1)
        self.assertEqual(len(self.loops.listing(now=self.loops.STORE.read()[l["id"]]["updated_at"] + 2 * 86400)), 0)

    def test_sync_prs_creates_one_loop_per_pr(self):
        mine = [{"repo": "o/r", "number": 7, "title": "fix: x", "url": "https://github.com/o/r/pull/7"}]
        self.loops.sync_prs(mine)
        self.loops.sync_prs(mine)
        rows = self.loops.listing()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["stage"], rows[0]["kind"]), ("fix_pr", "pr"))

    def test_finish_check_applies_structured_updates(self):
        from cos.runner import Run, runs_dir
        runs_dir()
        l = self.loops.open_loop("pr", "pr:o/r#7", "fix: x", {"prs": ["https://github.com/o/r/pull/7"]}, stage="fix_pr")
        run = Run.create("claude", "check", os.environ["COS_HOME"], "readonly", extra={"kind": "loops"})
        answer = {"updates": [{"id": l["id"], "stage": "merged", "note": "merged", "evidence": "gh pr view: MERGED"},
                              {"id": l["id"], "stage": "fix_pr", "note": "", "evidence": "x"}]}
        (run.dir / "raw.jsonl").write_text(json.dumps({"type": "result", "structured_output": answer}) + "\n")
        out = self.loops.finish_check(run)
        self.assertEqual([o["applied"] for o in out], [True, False])
        self.assertEqual(self.loops.listing()[0]["stage"], "merged")
        self.assertEqual(run.meta()["loops_applied"], 1)

    def test_check_not_blocked_by_a_dead_running_check(self):
        rows = [{"kind": "loops", "status": "running", "pid": 999999}]
        with patch.object(self.loops, "list_runs", return_value=rows):
            self.assertFalse(self.loops.check_running())
        with patch.object(self.loops, "list_runs", return_value=[{**rows[0], "pid": os.getpid()}]):
            self.assertTrue(self.loops.check_running())
        # waiting to auto-restart after an API error: no pid, but its owner is alive
        with patch.object(self.loops, "list_runs", return_value=[{**rows[0], "pid": None, "retry_owner": os.getpid()}]):
            self.assertTrue(self.loops.check_running())


    def test_repeat_regression_report_does_not_reset_the_clock(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.up(l["id"], "deployed", T0)
        self.up(l["id"], "rca", T0 + H, regressed=True)
        before = self.loops.listing(now=T0 + 2 * H)[0]
        self.up(l["id"], "rca", T0 + 2 * H, regressed=True)
        after = self.loops.listing(now=T0 + 2 * H)[0]
        self.assertEqual(after["stage_at"], before["stage_at"])
        self.assertEqual(len(after["history"]), len(before["history"]))

    def test_only_web_links_are_kept(self):
        l = self.loops.open_loop("alert", "k", "t", {}, now=T0)
        self.up(l["id"], "rca", T0 + H, links={"rca_doc": "javascript:alert(1)", "source": "https://ok", "prs": ["javascript:x", "https://github.com/o/r/pull/2"]})
        got = self.loops.listing(now=T0 + H)[0]["links"]
        self.assertNotIn("rca_doc", got)
        self.assertEqual(got["source"], "https://ok")
        self.assertEqual(got["prs"], ["https://github.com/o/r/pull/2"])
        bad = self.loops.open_loop("alert", "k2", "t", {"source": "javascript:alert(1)"}, now=T0)
        self.assertEqual(bad["links"]["source"], "")

    def test_abandoned_pr_is_closed_by_the_checker(self):
        l = self.loops.open_loop("pr", "pr:o/r#9", "t", {}, now=T0, stage="fix_pr")
        ok, _ = self.loops.apply_update(l["id"], {"stage": "fix_pr", "close": True, "note": "closed unmerged", "evidence": "gh: CLOSED"}, now=T0 + H)
        self.assertTrue(ok)
        self.assertEqual(self.loops.listing(now=T0 + H)[0]["status"], "closed")

    def test_sync_prs_skips_drafts(self):
        self.loops.sync_prs([{"repo": "o/r", "number": 8, "title": "wip", "url": "https://x", "draft": True}])
        self.assertEqual(self.loops.listing(), [])

    def test_check_skips_prs_that_are_still_open(self):
        mine = [{"repo": "o/r", "number": 7, "title": "a", "url": "https://x/7"}, {"repo": "o/r", "number": 8, "title": "b", "url": "https://x/8"}]
        self.loops.sync_prs(mine)
        self.assertEqual(self.loops._open_for_check(T0), [])
        self.loops.sync_prs(mine[:1])  # #8 left the open list: merged or closed, so check it
        self.assertEqual([r["title"] for r in self.loops._open_for_check(T0)], ["b"])

class RcaFromRunTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()
        from cos import loops
        self.loops = loops
        import subprocess
        self.gh = patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"state":"MERGED","mergedAt":"2026-10-21T00:00:00Z","mergeCommit":{"oid":"abc"}}', ''))
        self.gh.start()
        self.addCleanup(self.gh.stop)
        self.health = patch("cos.proofs.urllib.request.urlopen")
        response = self.health.start()
        response.return_value.__enter__.return_value.read.return_value = b'{"version":"1"}'
        self.addCleanup(self.health.stop)

    def tearDown(self):
        self.env.stop()

    def test_neo_reply_in_the_alert_thread_is_the_rca(self):
        l = self.loops.open_loop("alert", "slack:C1:1.5", "LagHigh", {}, now=T0)
        ok, _ = self.loops.rca_from_run({"id": "r1", "status": "done", "slack_channel": "C1", "slack_thread": "1.5"}, now=T0 + H)
        self.assertTrue(ok)
        got = self.loops.listing(now=T0 + 5 * H)[0]
        self.assertEqual(got["stage"], "rca")
        self.assertIsNone(got["stalled"])

    def test_other_threads_failed_runs_and_later_stages_are_left_alone(self):
        l = self.loops.open_loop("alert", "slack:C1:1.5", "LagHigh", {}, now=T0)
        self.assertFalse(self.loops.rca_from_run({"id": "r", "status": "failed", "slack_channel": "C1", "slack_thread": "1.5"})[0])
        self.assertFalse(self.loops.rca_from_run({"id": "r", "status": "done", "slack_channel": "C1", "slack_thread": "9.9"})[0])
        self.loops.apply_update(l["id"], {"stage": "fix_pr", "evidence": "pr"}, now=T0 + H)
        self.assertFalse(self.loops.rca_from_run({"id": "r", "status": "done", "slack_channel": "C1", "slack_thread": "1.5"})[0])
        self.assertEqual(self.loops.listing(now=T0 + H)[0]["stage"], "fix_pr")



if __name__ == "__main__":
    unittest.main()
