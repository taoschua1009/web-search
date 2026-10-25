"""
Agent Core - Connects Claude LLM with the Tool Registry.
Handles the full conversation loop with tool use.
"""

import time
import json
import uuid
import anthropic
from opentelemetry import trace
from src.configs.config import LLM_EFFORT, LLM_MODEL
from src.configs.prompts import agent_prompt
from src.registry import ToolRegistry, ToolDefinition
from src.observability import telemetry  # noqa: F401 – initialises providers on import
from src.observability.telemetry import (
    tracer,
    agent_run_counter,
    llm_call_counter,
    llm_input_tokens,
    llm_output_tokens,
    llm_latency_ms,
    llm_cost_usd,
)

# Pricing per 1M tokens (USD) — update when model changes
# Pricing per 1M tokens — dateless IDs (4.6+) and aliases (pre-4.6)
_PRICING = {
    "claude-haiku-5-5":          {"input": 0.10,  "output": 0.50},
    # 4.6 generation (dateless, pinned snapshots)
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
    # pre-4.6 aliases
    "claude-sonnet-4-5":          {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00,  "output": 15.00},
}
_DEFAULT_PRICE = {"input": 3.00, "output": 15.00}

def _calc_cost(model: str, input_tok: int, output_tok: int) -> float:
    """Return estimated cost in USD."""
    price = _PRICING.get(model, _DEFAULT_PRICE)
    if model == "claude-haiku-5-5" and input_tok > 100_000:
        price = {"input": 0.50, "output": 2.50}
    return (input_tok * price["input"] + output_tok * price["output"]) / 1_000_000

from src.tools.google_drive import ALL_DRIVE_TOOLS
from src.tools.google_calendar import ALL_CALENDAR_TOOLS
from src.tools.gmail import ALL_GMAIL_TOOLS
from src.tools.read_file import ALL_READ_FILE_TOOLS
from src.tools.memory import ALL_MEMORY_TOOLS
from src.services.gmail_service import gmail_cache_scope

ALL_TOOLS: list[ToolDefinition] = ALL_DRIVE_TOOLS + ALL_CALENDAR_TOOLS + ALL_GMAIL_TOOLS + ALL_READ_FILE_TOOLS + ALL_MEMORY_TOOLS

SYSTEM_PROMPT = agent_prompt()


class Agent:
    def __init__(self, service_api_key: str = "sk-admin-001"):
        self.client = anthropic.Anthropic()
        self.model = LLM_MODEL
        self.effort = LLM_EFFORT
        self.service_api_key = service_api_key
        self.conversation_history: list[dict] = []

        # Initialize Tool Registry with all tools
        self.registry = ToolRegistry()
        for tool in ALL_TOOLS:
            self.registry.register(tool)

        # Pre-create counter series at 0 so Prometheus rate()/increase() sees the first real call
        agent_run_counter.add(0, {"model": self.model})
        for stop_reason in ("end_turn", "tool_use"):
            attrs = {"model": self.model, "stop_reason": stop_reason}
            for counter in (llm_call_counter, llm_input_tokens, llm_output_tokens, llm_cost_usd):
                counter.add(0, attrs)

    def get_tools_for_claude(self) -> list[dict]:
        return self.registry.list_tools()

    def run(self, user_message: str) -> str:
        """
        Run the full agent loop:
        1. Add user message to history
        2. Send to Claude with tools
        3. If Claude uses tools → run through Registry pipeline
        4. Loop until Claude gives final text response
        """
        print(f"\n{'#'*60}")
        print(f"  USER: {user_message}")
        print(f"{'#'*60}")

        agent_run_counter.add(1, {"model": self.model})

        self.conversation_history.append({
            "role": "user",
            "content": user_message,
        })

        tools = self.get_tools_for_claude()

        run_id = str(uuid.uuid4())
        with gmail_cache_scope(), tracer.start_as_current_span("agent.run") as agent_span:
            agent_span.set_attribute("agent.run_id", run_id)
            agent_span.set_attribute("agent.model", self.model)
            agent_span.set_attribute("agent.effort", self.effort)
            agent_span.set_attribute("agent.session", self.service_api_key[:8])
            agent_span.set_attribute("user.message_length", len(user_message))

            llm_turn = 0
            while True:
                llm_turn += 1
                print(f"\n>>> Calling Claude LLM...")

                t0 = time.perf_counter()
                with tracer.start_as_current_span("llm.call") as llm_span:
                    llm_span.set_attribute("llm.model", self.model)
                    llm_span.set_attribute("llm.effort", self.effort)
                    llm_span.set_attribute("llm.turn", llm_turn)

                    response = self.client.messages.create(
                        model=self.model,
                        output_config={"effort": self.effort},
                        max_tokens=4096,
                        system=SYSTEM_PROMPT,
                        tools=tools,
                        messages=self.conversation_history,
                    )

                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    in_tok = response.usage.input_tokens
                    out_tok = response.usage.output_tokens
                    cost = _calc_cost(self.model, in_tok, out_tok)

                    llm_span.set_attribute("llm.stop_reason", response.stop_reason)
                    llm_span.set_attribute("llm.input_tokens", in_tok)
                    llm_span.set_attribute("llm.output_tokens", out_tok)
                    llm_span.set_attribute("llm.cost_usd", round(cost, 6))
                    llm_span.set_attribute("llm.latency_ms", round(elapsed_ms))

                attrs = {"model": self.model, "stop_reason": response.stop_reason}
                llm_call_counter.add(1, attrs)
                llm_input_tokens.add(in_tok, attrs)
                llm_output_tokens.add(out_tok, attrs)
                llm_latency_ms.record(elapsed_ms, attrs)
                llm_cost_usd.add(int(cost * 1_000_000), attrs)  # store as micro-USD integer

                print(f">>> Claude stop_reason: {response.stop_reason}")

                # Final response - no more tool calls
                if response.stop_reason in {"end_turn", "refusal", "max_tokens", "model_context_window_exceeded"}:
                    text_parts = [
                        block.text for block in response.content
                        if hasattr(block, "text")
                    ]
                    final_response = "\n".join(text_parts)
                    if response.stop_reason in {"max_tokens", "model_context_window_exceeded"}:
                        final_response = (final_response + "\n\nPhản hồi đã đạt giới hạn độ dài. Hãy chia yêu cầu thành phần nhỏ hơn.").strip()
                    elif not final_response:
                        final_response = "Model không thể trả lời yêu cầu này. Hãy thử diễn đạt lại."
                    # Truncation may contain an incomplete tool call. Keep only
                    # text for that turn so the next request remains valid.
                    content = final_response if response.stop_reason in {"max_tokens", "model_context_window_exceeded"} else response.content
                    self.conversation_history.append({"role": "assistant", "content": content})

                    agent_span.set_attribute("agent.llm_turns", llm_turn)
                    agent_span.set_attribute("agent.response_length", len(final_response))

                    print(f"\n{'#'*60}")
                    print(f"  ASSISTANT: {final_response[:500]}")
                    if len(final_response) > 500:
                        print(f"  ... [truncated, total {len(final_response)} chars]")
                    print(f"{'#'*60}\n")

                    return final_response

                # Process tool calls
                if response.stop_reason == "tool_use":
                    self.conversation_history.append({
                        "role": "assistant",
                        "content": response.content,
                    })

                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            tool_name = block.name
                            tool_input = block.input
                            tool_use_id = block.id

                            # Run through the 6-step Registry pipeline
                            result = self.registry.call(
                                tool_name=tool_name,
                                arguments=tool_input,
                                api_key=self.service_api_key,
                            )

                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": json.dumps(result, ensure_ascii=False),
                            })

                    self.conversation_history.append({
                        "role": "user",
                        "content": tool_results,
                    })
                else:
                    raise RuntimeError(f"Unexpected LLM stop reason: {response.stop_reason}")

    def clear_history(self):
        self.conversation_history = []
        print("[Agent] Conversation history cleared.")

    def get_audit_log(self) -> list[dict]:
        return self.registry.get_audit_log()
