"""Verify Haiku effort, tool history and terminal stop behavior without API calls."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.agents.agent import Agent, _calc_cost


class HaikuModelTests(unittest.TestCase):
    def test_low_effort_is_sent_on_every_turn_and_thinking_is_preserved(self):
        thinking = SimpleNamespace(type="thinking", thinking="", signature="fake-signature")
        tool = SimpleNamespace(type="tool_use", name="list_gmail_messages", input={}, id="call-1")
        usage = SimpleNamespace(input_tokens=100, output_tokens=20)
        responses = [
            SimpleNamespace(stop_reason="tool_use", content=[thinking, tool], usage=usage),
            SimpleNamespace(stop_reason="end_turn", content=[thinking, SimpleNamespace(type="text", text="Đã đọc email.")], usage=usage),
        ]
        with patch("src.agents.agent.anthropic.Anthropic") as client:
            client.return_value.messages.create.side_effect = responses
            agent = Agent()
            with patch.object(agent.registry.get_tool("list_gmail_messages"), "handler", Mock(return_value={"messages": []})):
                self.assertEqual(agent.run("Đọc email"), "Đã đọc email.")
            for call in client.return_value.messages.create.call_args_list:
                self.assertEqual(call.kwargs["model"], "claude-haiku-5-5")
                self.assertEqual(call.kwargs["output_config"], {"effort": "low"})
            self.assertIs(agent.conversation_history[1]["content"][0], thinking)

    def test_terminal_stops_do_not_start_another_billable_call(self):
        for stop_reason in ("refusal", "max_tokens", "model_context_window_exceeded"):
            with self.subTest(stop_reason=stop_reason), patch("src.agents.agent.anthropic.Anthropic") as client:
                client.return_value.messages.create.return_value = SimpleNamespace(
                    stop_reason=stop_reason,
                    content=[SimpleNamespace(type="thinking", signature="fake")],
                    usage=SimpleNamespace(input_tokens=10, output_tokens=10),
                )
                result = Agent().run("Read email")
                self.assertTrue(result)
                client.return_value.messages.create.assert_called_once()

    def test_truncated_tool_call_is_not_replayed_as_a_pending_tool(self):
        with patch("src.agents.agent.anthropic.Anthropic") as client:
            client.return_value.messages.create.return_value = SimpleNamespace(
                stop_reason="max_tokens",
                content=[SimpleNamespace(type="tool_use", name="list_gmail_messages", input={}, id="partial")],
                usage=SimpleNamespace(input_tokens=10, output_tokens=10),
            )
            agent = Agent()
            agent.run("Read email")
            self.assertIsInstance(agent.conversation_history[-1]["content"], str)
            self.assertEqual(agent.get_audit_log(), [])

    def test_haiku_pricing_changes_above_100k_input_tokens(self):
        self.assertAlmostEqual(_calc_cost("claude-haiku-5-5", 100_000, 1_000), 0.0105)
        self.assertAlmostEqual(_calc_cost("claude-haiku-5-5", 100_001, 1_000), 0.0525005)


if __name__ == "__main__":
    unittest.main()
