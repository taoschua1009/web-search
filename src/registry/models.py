from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict


class ToolDefinition(BaseModel):
    """Definition of a tool in the registry.

    ``risk`` classifies the worst thing the tool can do: ``low`` (read-only, no side
    effects), ``medium`` (writes private data or only *proposes* an action that a human
    approves) or ``high`` (acts on the outside world). Unclassified tools default to
    ``high`` and are refused: the policy fails closed. ``untrusted_output`` marks tools
    whose results contain external content (mail, web pages, files, memory).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_schema: dict
    required_scopes: list[str]
    handler: Callable
    risk: Literal["low", "medium", "high"] = "high"
    untrusted_output: bool = False
