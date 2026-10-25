"""Load the agent's static LLM instructions from YAML."""

from functools import lru_cache
from pathlib import Path

import yaml

PROMPTS_FILE = Path(__file__).with_name("prompts.yaml")


@lru_cache(maxsize=1)
def load_prompts() -> dict:
    """Load and validate prompt data once at application startup."""
    with PROMPTS_FILE.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError("prompts.yaml must contain a mapping.")
    agent = data.get("agent", {})
    if not isinstance(agent, dict) or not isinstance(agent.get("system"), str) or not agent["system"].strip():
        raise ValueError("prompts.yaml must define a non-empty agent.system prompt.")
    return data


def agent_prompt() -> str:
    return load_prompts()["agent"]["system"]
