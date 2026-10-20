import os
import unittest
from unittest.mock import patch

from cos import config


class ControllerConfigTests(unittest.TestCase):
    def test_default_is_independent_of_task_engine(self):
        with patch.dict(os.environ, {"NEO_DEFAULT_ENGINE": "codex"}, clear=True):
            self.assertEqual(config.controller_engine(), "claude")

    def test_engine_and_model_are_configurable(self):
        with patch.dict(os.environ, {"NEO_CONTROLLER_ENGINE": "codex", "NEO_CONTROLLER_MODEL": "gpt-6.1-sol"}, clear=True):
            self.assertEqual(config.controller_engine(), "codex")
            self.assertEqual(config.controller_model("old-claude"), "gpt-6.1-sol")

    def test_codex_does_not_inherit_claude_model(self):
        with patch.dict(os.environ, {"NEO_CONTROLLER_ENGINE": "codex"}, clear=True):
            self.assertIsNone(config.controller_model("old-claude"))

    def test_invalid_engine_fails_explicitly(self):
        with patch.dict(os.environ, {"NEO_CONTROLLER_ENGINE": "invalid"}, clear=True):
            with self.assertRaises(ValueError):
                config.controller_engine()


class StructuredControllerTests(unittest.TestCase):
    def test_codex_is_isolated_and_uses_sanitized_catalog(self):
        import json
        import subprocess
        from pathlib import Path
        from cos.controller import structured
        seen = {}
        def run(cmd, **kwargs):
            seen["cwd"] = kwargs["cwd"]
            if "debug" in cmd:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"models": [{"slug": "test", "apply_patch_tool_type": "freeform", "shell_type": "shell_command", "experimental_supported_tools": ["clock"]}]}), "")
            seen["cmd"] = cmd
            self.assertNotEqual(kwargs["env"]["CODEX_HOME"], str(Path.home() / ".codex"))
            catalog = next(v for v in cmd if v.startswith("model_catalog_json="))
            models = json.loads(Path(json.loads(catalog.split("=", 1)[1])).read_text())
            self.assertIsNone(models["models"][0]["apply_patch_tool_type"])
            self.assertEqual(models["models"][0]["shell_type"], "disabled")
            self.assertEqual(models["models"][0]["experimental_supported_tools"], [])
            schema = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text())
            self.assertFalse(schema["additionalProperties"])
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text('{"status":"ok"}')
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.dict(os.environ, {"NEO_CONTROLLER_ENGINE": "codex"}, clear=True), patch("subprocess.run", side_effect=run):
            self.assertEqual(structured("hello", {"type":"object", "properties":{"status":{"type":"string"}}}, "claude-old", 30, "neo-test"), {"status":"ok"})
        self.assertFalse(Path(seen["cwd"]).exists())
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral"):
            self.assertIn(flag, seen["cmd"])
        self.assertNotIn("--tools", seen["cmd"])
        self.assertNotIn("claude-old", seen["cmd"])

    def test_failed_codex_does_not_return_stale_or_partial_output(self):
        import subprocess
        from cos.controller import structured
        with patch.dict(os.environ, {"NEO_CONTROLLER_ENGINE": "codex"}, clear=True), patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "failed")):
            self.assertIsNone(structured("hello", {}, "claude-old", 30, "neo-test"))


    def test_auth_refresh_writes_through_symlink_and_preserves_source(self):
        import json
        import subprocess
        import tempfile
        from pathlib import Path
        from cos.controller import structured
        with tempfile.TemporaryDirectory() as auth_home:
            auth = Path(auth_home) / "auth.json"
            auth.write_text('{"token":"old-synthetic"}')
            def run(cmd, **kwargs):
                if "debug" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"models": [{"slug":"test"}]}), "")
                linked = Path(kwargs["env"]["CODEX_HOME"]) / "auth.json"
                self.assertTrue(linked.is_symlink())
                self.assertEqual(linked.resolve(), auth.resolve())
                linked.write_text('{"token":"refreshed-synthetic"}')
                self.assertIn('cli_auth_credentials_store="file"', cmd)
                Path(cmd[cmd.index("--output-last-message") + 1]).write_text('{}')
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with patch.dict(os.environ, {"NEO_CONTROLLER_ENGINE":"codex", "CODEX_HOME":auth_home}, clear=True), patch("subprocess.run", side_effect=run):
                self.assertEqual(structured("hello", {"type":"object", "properties":{}}, "old", 30, "neo-test"), {})
            self.assertEqual(json.loads(auth.read_text()), {"token":"refreshed-synthetic"})
