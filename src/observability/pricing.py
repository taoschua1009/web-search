"""Model price table (USD per 1M tokens) and cost estimation."""

from __future__ import annotations

# Update when prices or models change. Unknown models fall back to DEFAULT_PRICE.
PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    "claude-fable-5-1": {"input": 10.00, "output": 50.00},
    # 4.6 generation (dateless, pinned snapshots)
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    # pre-4.6 aliases
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
}
DEFAULT_PRICE = {"input": 3.00, "output": 15.00}


def calc_cost(model: str, input_tok: int, output_tok: int) -> float:
    """Estimated cost in USD of one call."""
    price = PRICING.get(model, DEFAULT_PRICE)
    if model == "claude-haiku-5-5" and input_tok > 100_000:
        price = {"input": 0.50, "output": 2.50}
    return (input_tok * price["input"] + output_tok * price["output"]) / 1_000_000
