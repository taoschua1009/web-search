"""Load individually managed YAML tool definitions with Python-bound handlers."""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import yaml

TOOLS_DIR = Path(__file__).with_name("tools")
TOOL_FIELDS = {"name", "description", "input_schema", "required_scopes", "risk"}
OPTIONAL_FIELDS = {"untrusted_output"}
RISK_TIERS = {"low", "medium", "high"}


@lru_cache(maxsize=1)
def load_tool_configs() -> dict[str, dict]:
    """Validate all manifests before the tools are registered."""
    files = sorted(TOOLS_DIR.glob("*.yaml"))
    if not files:
        raise ValueError(f"No tool YAML files found in {TOOLS_DIR}.")
    tools = {}
    for path in files:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        if not isinstance(data, dict) or not TOOL_FIELDS <= set(data) <= TOOL_FIELDS | OPTIONAL_FIELDS:
            raise ValueError(f"{path.name} must define name, description, input_schema, required_scopes and risk.")
        name = data["name"]
        if not isinstance(name, str) or name != path.stem:
            raise ValueError(f"Tool name must match its YAML filename: {path.name}.")
        if not isinstance(data["description"], str) or not data["description"].strip():
            raise ValueError(f"{path.name} must have a non-empty description.")
        schema = data["input_schema"]
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"{path.name} must have an object input_schema.")
        scopes = data["required_scopes"]
        if not isinstance(scopes, list) or not all(isinstance(scope, str) and scope.strip() for scope in scopes):
            raise ValueError(f"{path.name} required_scopes must be a list of scope names.")
        if data["risk"] not in RISK_TIERS:
            raise ValueError(f"{path.name} risk must be one of {sorted(RISK_TIERS)}.")
        if not isinstance(data.get("untrusted_output", False), bool):
            raise ValueError(f"{path.name} untrusted_output must be true or false.")
        tools[name] = data
    return tools


def tool_config(name: str) -> dict:
    """Keep mutable schemas/scopes independent for each ToolDefinition."""
    tools = load_tool_configs()
    if name not in tools:
        raise ValueError(f"Missing tool definition: {name}.yaml.")
    return deepcopy(tools[name])
