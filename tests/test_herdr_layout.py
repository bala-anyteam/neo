import unittest
from unittest.mock import Mock, patch

from cos.herdr import Herdr
from cos.layout import group_for_task, suggest_task_label, task_label


class TaskLayoutTest(unittest.TestCase):
    def test_groups_release_operations_agent_and_product_work(self):
        self.assertEqual(group_for_task("deploy Deskie v3.15.9 to production", "/work/anyteam-deskie"), "Nio · Releases")
        self.assertEqual(group_for_task("investigate the Sentry alert", "/work/anyteam-backie"), "Nio · Operations")
        self.assertEqual(group_for_task("fix Nio Herdr task routing", "/work/neo"), "Nio · Agent operations")
        self.assertEqual(group_for_task("fix the auth handler", "/work/anyteam-backie"), "Nio · Backend fixes")
        self.assertEqual(group_for_task("fix the tray", "/work/anyteam-deskie"), "Nio · Desktop fixes")
        self.assertEqual(group_for_task("fix issue #2739", "/work/anyteam-one"), "Nio · Issue & PR fixes")

    def test_task_label_surfaces_issue_and_action(self):
        self.assertEqual(task_label("Fix issue #2739: recording sync leaves a meeting orphaned"),
                         "#2739 Fix recording sync leaves a meeting orphaned")

    def test_suggest_task_label_uses_a_short_meaningful_controller_title(self):
        with patch("cos.layout.structured", return_value={"title": "Find missing Herdr tasks"}) as controller:
            self.assertEqual(
                suggest_task_label("i don't see you there, i only see 2 spaces with only one codex session running. where are the rest?"),
                "Find missing Herdr tasks",
            )
        prompt = controller.call_args.args[0]
        self.assertIn("3-7 words", prompt)
        self.assertIn("where are the rest?", prompt)

    def test_task_label_falls_back_without_exposing_a_long_message(self):
        label = task_label("Can you please check why duplicate Slack messages are being sent and recover the service?")
        self.assertEqual(label, "Investigate duplicate Slack message delivery")

    def test_suggest_task_label_rejects_invalid_controller_output(self):
        with patch("cos.layout.structured", return_value={"title": "This title is far too verbose because it repeats the complete owner request instead of summarizing it"}):
            label = suggest_task_label("Please investigate the duplicate Slack messages")
        self.assertEqual(label, "Investigate the duplicate Slack messages")

    def test_suggest_task_label_preserves_pr_number_forms(self):
        for prompt in ("Fix PR 5061", "Fix pull request 5061", "Fix https://github.com/acme/repo/pull/5061"):
            with self.subTest(prompt=prompt), patch("cos.layout.structured", return_value={"title": "Fix malformed Library artifacts"}):
                self.assertEqual(suggest_task_label(prompt), "#5061 Fix malformed Library artifacts")

    def test_acquire_lane_reuses_group_as_new_tab(self):
        herdr = Herdr("nio", "herdr")
        herdr.call = Mock(side_effect=[
            {"type": "workspace_list", "workspaces": [{"workspace_id": "w2", "label": "Backend fixes"}]},
            {"tab": {"tab_id": "w2:t3"}, "root_pane": {"pane_id": "w2:p3"}},
            {},
        ])
        self.assertEqual(herdr.acquire_lane("/work/backie", "Backend fixes", "#2739 Fix recording sync"),
                         ("w2", "w2:t3", "w2:p3"))
        self.assertEqual(herdr.call.call_args_list[1].args[:2], ("tab", "create"))

    def test_close_lane_closes_only_task_tab_in_shared_group(self):
        herdr = Herdr("nio", "herdr")
        herdr.call = Mock(side_effect=[
            {"type": "tab_list", "tabs": [{"tab_id": "w2:t1"}, {"tab_id": "w2:t2"}]},
            {},
        ])
        herdr.close_lane("w2", "w2:t2")
        herdr.call.assert_called_with("tab", "close", "w2:t2")

    def test_rename_lane_updates_tab_and_pane(self):
        herdr = Herdr("nio", "herdr")
        herdr.call = Mock(return_value={})
        herdr.rename_lane("w2:t3", "w2:p3", "Fix malformed Library artifacts")
        self.assertEqual(
            [call.args for call in herdr.call.call_args_list],
            [("tab", "rename", "w2:t3", "Fix malformed Library artifacts"),
             ("pane", "rename", "w2:p3", "Fix malformed Library artifacts")],
        )


if __name__ == "__main__":
    unittest.main()
