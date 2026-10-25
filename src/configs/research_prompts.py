"""Load the research agents' prompts from ``research_prompts.yaml``.

The wording lives in YAML (like ``prompts.yaml`` for the chat agent); this module
validates it once and exposes it as module constants.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PROMPTS_FILE = Path(__file__).with_name("research_prompts.yaml")


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"research_prompts.yaml: {name} must be a non-empty string.")
    return value.strip()


def _texts(value, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"research_prompts.yaml: {name} must be a non-empty mapping.")
    return {str(key): _text(item, f"{name}.{key}") for key, item in value.items()}


@lru_cache(maxsize=1)
def load_research_prompts() -> dict:
    """Load and validate the prompts once; raises ``ValueError`` on a malformed file."""
    with PROMPTS_FILE.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError("research_prompts.yaml must contain a mapping.")
    prompts = {
        "deep_agent_system": _text(data.get("deep_agent_system"), "deep_agent_system"),
        "specialist_common": _text(data.get("specialist_common"), "specialist_common"),
        "specialist_tasks": _texts(data.get("specialist_tasks"), "specialist_tasks"),
        "section_labels": _texts(data.get("section_labels"), "section_labels"),
    }
    if list(prompts["specialist_tasks"]) != list(prompts["section_labels"]):
        raise ValueError(
            "research_prompts.yaml: specialist_tasks and section_labels must list "
            "the same sections in the same order."
        )
    return prompts


_PROMPTS = load_research_prompts()
DEEP_AGENT_SYSTEM: str = _PROMPTS["deep_agent_system"]
SPECIALIST_COMMON: str = _PROMPTS["specialist_common"]
SPECIALIST_TASKS: dict[str, str] = _PROMPTS["specialist_tasks"]
SECTION_LABELS: dict[str, str] = _PROMPTS["section_labels"]
