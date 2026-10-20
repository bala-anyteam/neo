"""Isolated, tool-free structured controller calls for either supported CLI."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .config import controller_engine, controller_model
from .runner import cli_path

# Keep all capability gates off, including defaults that can change independently of shell access.
_DISABLED = ("shell_tool", "unified_exec", "view_image", "apps", "plugins", "remote_plugin",
             "browser_use", "browser_use_external", "computer_use", "in_app_browser",
             "multi_agent", "multi_agent_v2", "code_mode", "code_mode_host", "image_generation",
             "hooks", "skill_search", "skill_mcp_dependency_install", "tool_suggest", "goals",
             "current_time_reminder", "sleep_tool", "token_budget", "request_permissions_tool",
             "default_mode_request_user_input", "send_message_to_user_async", "deferred_executor",
             "standalone_web_search", "memories")


def _strict_schema(schema: dict) -> dict:
    schema = copy.deepcopy(schema)
    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema


def structured(prompt: str, schema: dict, legacy_model: str, timeout: int, name: str) -> Optional[dict]:
    engine = controller_engine()
    model = controller_model(legacy_model)
    if engine == "claude":
        cmd = [cli_path("claude"), "-p", "--output-format", "json", "--tools", "",
               "--strict-mcp-config", "--setting-sources", "", "--json-schema", json.dumps(schema)]
        if model:
            cmd += ["--model", model]
        cmd += ["--", prompt]
        cwd = Path(tempfile.gettempdir()) / name
        try:
            cwd.mkdir(exist_ok=True)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, cwd=str(cwd))
            raw = json.loads(proc.stdout)
            return raw.get("structured_output") if isinstance(raw, dict) else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
    try:
        # Isolate instructions/config as well as tools. File auth refresh writes through
        # the symlink to the original store, preserving rotated OAuth credentials.
        with tempfile.TemporaryDirectory(prefix=name + "-") as temporary:
            root = Path(temporary)
            home, cwd = root / "home", root / "cwd"
            home.mkdir(mode=0o700)
            cwd.mkdir()
            auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
            if auth.is_file():
                (home / "auth.json").symlink_to(auth.resolve())
            catalog_env = {**os.environ, "CODEX_HOME": str(home)}
            catalog = subprocess.run([cli_path("codex"), "debug", "models", "--bundled"],
                                     capture_output=True, text=True, timeout=min(timeout, 30),
                                     stdin=subprocess.DEVNULL, cwd=str(cwd), env=catalog_env)
            catalog.check_returncode()
            models = json.loads(catalog.stdout)
            if not isinstance(models.get("models"), list) or not models["models"]:
                return None
            for entry in models["models"]:
                entry.update(apply_patch_tool_type=None, shell_type="disabled", experimental_supported_tools=[])
            catalog_path = root / "models.json"
            catalog_path.write_text(json.dumps(models))
            schema_path, output_path = root / "schema.json", root / "output.json"
            schema_path.write_text(json.dumps(_strict_schema(schema)))
            cmd = [cli_path("codex"), "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--output-schema", str(schema_path),
                   "--output-last-message", str(output_path), "--json",
                   "-c", f"model_catalog_json={json.dumps(str(catalog_path))}",
                   "-c", 'cli_auth_credentials_store="file"',
                   "-c", 'web_search="disabled"', "-c", "tools.update_plan.enabled=false",
                   "-c", "tools.experimental_request_user_input.enabled=false", "-c", "project_doc_max_bytes=0",
                   "-c", "skills.bundled.enabled=false"]
            for feature in _DISABLED:
                cmd += ["--disable", feature]
            cmd += ["--enable", "skip_host_skill_discovery"]
            if model:
                cmd += ["--model", model]
            cmd += ["--", prompt]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, cwd=str(cwd), env=catalog_env)
            if proc.returncode:
                return None
            result = json.loads(output_path.read_text())
            return result if isinstance(result, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
