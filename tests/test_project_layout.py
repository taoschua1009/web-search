"""Verify resource loading and prompt contracts after the src migration."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from src.configs import config, prompts, tool_loader


class ProjectLayoutTests(unittest.TestCase):
    def test_required_packages_and_resources_resolve_inside_src(self):
        for name in ("tools", "observability", "registry", "services", "configs", "agents", "api", "cli", "schemas", "workflows"):
            self.assertTrue((config.SRC_DIR / name).is_dir(), name)
        for page in ("login.html", "index.html", "research.html"):
            self.assertTrue((config.FRONTEND_DIR / page).is_file(), page)
        for asset in ("research.css", "research.js"):
            self.assertTrue((config.FRONTEND_DIR / "assets" / asset).is_file(), asset)
        self.assertFalse((config.SRC_DIR / "static").exists())
        self.assertEqual(prompts.PROMPTS_FILE.parent, config.CONFIG_DIR)

    def test_runtime_tool_contracts_and_system_prompt_come_from_yaml(self):
        from src.agents.agent import ALL_TOOLS, SYSTEM_PROMPT

        data = yaml.safe_load(prompts.PROMPTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(SYSTEM_PROMPT, data["agent"]["system"])
        self.assertNotIn("tools", data)
        tools = tool_loader.load_tool_configs()
        self.assertEqual({tool.name for tool in ALL_TOOLS}, set(tools))
        for tool in ALL_TOOLS:
            path = tool_loader.TOOLS_DIR / f"{tool.name}.yaml"
            self.assertTrue(path.is_file())
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(tool.description, manifest["description"])
            self.assertEqual(tool.input_schema, manifest["input_schema"])
            self.assertEqual(tool.required_scopes, manifest["required_scopes"])

    def test_tool_schema_changes_do_not_mutate_cached_yaml(self):
        first = tool_loader.tool_config("search_and_read_gmail_messages")
        first["input_schema"]["properties"]["query"]["type"] = "fixture-mutation"
        first["required_scopes"].append("fixture-mutation")
        second = tool_loader.tool_config("search_and_read_gmail_messages")
        self.assertEqual(second["input_schema"]["properties"]["query"]["type"], "string")
        self.assertEqual(second["required_scopes"], ["gmail:read"])

    def test_invalid_yaml_prompt_configuration_fails_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.yaml"
            path.write_text("agent:\n  system: ''\ntools: {}\n")
            with patch.object(prompts, "PROMPTS_FILE", path):
                prompts.load_prompts.cache_clear()
                try:
                    with self.assertRaisesRegex(ValueError, "agent.system"):
                        prompts.load_prompts()
                finally:
                    prompts.load_prompts.cache_clear()

    def test_yaml_loader_rejects_python_object_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.yaml"
            path.write_text("!!python/object/apply:builtins.str ['fixture']\n")
            with patch.object(prompts, "PROMPTS_FILE", path):
                prompts.load_prompts.cache_clear()
                try:
                    with self.assertRaises(yaml.YAMLError):
                        prompts.load_prompts()
                finally:
                    prompts.load_prompts.cache_clear()

    def test_unrelated_working_directory_does_not_change_config_or_static_paths(self):
        script = '''
import json
from fastapi.testclient import TestClient
from src.configs import config, prompts
from src.api import server
response = TestClient(server.app).get('/login')
print('LAYOUT_CHECK ' + json.dumps({
    'model': config.LLM_MODEL,
    'config_dir': str(config.CONFIG_DIR),
    'env_file': str(config.ENV_FILE),
    'frontend_dir': str(server.FRONTEND_DIR),
    'credential_path': config.config_path('client_secret.json'),
    'prompt_loaded': bool(prompts.agent_prompt()),
    'tools_loaded': len(__import__('src.configs.tool_loader', fromlist=['load_tool_configs']).load_tool_configs()),
    'login_status': response.status_code,
    'login_text_present': 'Đăng nhập bằng Google' in response.text,
}))
'''
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text("LLM_MODEL=wrong-working-directory-model\n")
            env = dict(os.environ, PYTHONPATH=str(config.PROJECT_ROOT),
                       OTEL_EXPORTER_OTLP_ENDPOINT="", ANTHROPIC_API_KEY="fixture-key")
            result = subprocess.run([sys.executable, "-c", script], cwd=directory, env=env,
                                    capture_output=True, text=True, timeout=30, check=True)
        line = next(line for line in result.stdout.splitlines() if line.startswith("LAYOUT_CHECK "))
        data = json.loads(line.removeprefix("LAYOUT_CHECK "))
        self.assertEqual(data["model"], config.LLM_MODEL)
        self.assertEqual(data["config_dir"], str(config.CONFIG_DIR))
        self.assertEqual(data["env_file"], str(config.ENV_FILE))
        self.assertEqual(data["frontend_dir"], str(config.FRONTEND_DIR))
        self.assertEqual(data["credential_path"], str(config.SECRETS_DIR / "client_secret.json"))
        self.assertTrue(data["prompt_loaded"])
        self.assertEqual(data["tools_loaded"], len(tool_loader.load_tool_configs()))
        self.assertEqual(data["login_status"], 200)
        self.assertTrue(data["login_text_present"])

    def test_tool_filename_and_name_must_match(self):
        manifest = tool_loader.tool_config("search_and_read_gmail_messages")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "wrong_name.yaml").write_text(yaml.safe_dump(manifest))
            with patch.object(tool_loader, "TOOLS_DIR", Path(directory)):
                tool_loader.load_tool_configs.cache_clear()
                try:
                    with self.assertRaisesRegex(ValueError, "filename"):
                        tool_loader.load_tool_configs()
                finally:
                    tool_loader.load_tool_configs.cache_clear()

    def test_invalid_tool_scopes_are_rejected(self):
        manifest = tool_loader.tool_config("search_and_read_gmail_messages")
        manifest["required_scopes"] = "gmail:read"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "search_and_read_gmail_messages.yaml").write_text(yaml.safe_dump(manifest))
            with patch.object(tool_loader, "TOOLS_DIR", Path(directory)):
                tool_loader.load_tool_configs.cache_clear()
                try:
                    with self.assertRaisesRegex(ValueError, "required_scopes"):
                        tool_loader.load_tool_configs()
                finally:
                    tool_loader.load_tool_configs.cache_clear()

    def test_explicit_env_file_override_works_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory, "custom.env")
            env_file.write_text("LLM_MODEL=fixture-env-model\n")
            env = dict(os.environ, PYTHONPATH=str(config.PROJECT_ROOT), AGENT_ENV_FILE=str(env_file))
            env.pop("LLM_MODEL", None)
            result = subprocess.run(
                [sys.executable, "-c", "from src.configs.config import LLM_MODEL; print(LLM_MODEL)"],
                cwd=directory, env=env, capture_output=True, text=True, timeout=15, check=True,
            )
        self.assertEqual(result.stdout.strip(), "fixture-env-model")

    def test_default_env_file_is_in_project_root(self):
        env = dict(os.environ, PYTHONPATH=str(config.PROJECT_ROOT))
        env.pop("AGENT_ENV_FILE", None)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", "from src.configs.config import ENV_FILE; print(ENV_FILE)"],
                cwd=directory, env=env, capture_output=True, text=True, timeout=15, check=True,
            )
        self.assertEqual(Path(result.stdout.strip()), config.PROJECT_ROOT / ".env")


if __name__ == "__main__":
    unittest.main()
