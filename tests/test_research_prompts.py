"""Research prompts live in YAML; the loader validates them and keeps sections in sync."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.agents.fast_research_agent import SECTIONS
from src.configs import research_prompts


class ResearchPromptTests(unittest.TestCase):
    def test_constants_come_from_the_yaml_file(self):
        data = yaml.safe_load(research_prompts.PROMPTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(
            research_prompts.DEEP_AGENT_SYSTEM, data["deep_agent_system"].strip()
        )
        self.assertEqual(
            research_prompts.SPECIALIST_COMMON, data["specialist_common"].strip()
        )
        self.assertEqual(research_prompts.SPECIALIST_TASKS, data["specialist_tasks"])
        self.assertIn("submit_dossier", research_prompts.DEEP_AGENT_SYSTEM)
        self.assertIn("submit_section", research_prompts.SPECIALIST_COMMON)
        self.assertNotIn("ResearchWorkflow", research_prompts.DEEP_AGENT_SYSTEM)

    def test_every_specialist_section_has_a_task_prompt_and_a_label(self):
        self.assertEqual(list(SECTIONS), list(research_prompts.SPECIALIST_TASKS))
        self.assertEqual(list(SECTIONS), list(research_prompts.SECTION_LABELS))

    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "p.yaml"
            path.write_text(text, encoding="utf-8")
            research_prompts.load_research_prompts.cache_clear()
            try:
                with patch.object(research_prompts, "PROMPTS_FILE", path):
                    return research_prompts.load_research_prompts()
            finally:
                research_prompts.load_research_prompts.cache_clear()

    def test_malformed_files_fail_clearly(self):
        base = {
            "deep_agent_system": "a",
            "specialist_common": "b",
            "specialist_tasks": {"profile": "t"},
            "section_labels": {"profile": "L"},
        }
        self.assertEqual(self.load(yaml.dump(base))["section_labels"], {"profile": "L"})
        for key, bad in (
            ("deep_agent_system", ""),
            ("specialist_common", None),
            ("specialist_tasks", {}),
            ("section_labels", {"other": "L"}),
        ):
            with (
                self.subTest(key),
                self.assertRaisesRegex(ValueError, "research_prompts.yaml"),
            ):
                self.load(yaml.dump({**base, key: bad}))
        with self.assertRaises(ValueError):
            self.load("- not a mapping")
        with self.assertRaises(yaml.YAMLError):
            self.load("!!python/object/apply:builtins.str ['x']")


if __name__ == "__main__":
    unittest.main()
