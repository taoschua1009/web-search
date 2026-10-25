"""Batch request counts, MIME parsing and cache isolation regressions."""

import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import Response

from src.services import gmail_service as gmail
from test_gmail import api_response, FakeBatch


def make_email(message_id, text):
    message = EmailMessage()
    message["Subject"] = "Subject " + message_id
    message.set_content(text)
    return api_response(message, message_id)


class GmailBatchTests(unittest.TestCase):
    def setUp(self):
        self.responses = {message_id: make_email(message_id, "Body " + message_id) for message_id in ("one", "two", "three")}
        self.errors = {}
        self.service = Mock()
        self.messages = self.service.users.return_value.messages.return_value
        self.messages.list.return_value.execute.return_value = {
            "messages": [{"id": message_id} for message_id in self.responses],
        }
        self.batches = []

        def batch_factory(callback):
            batch = FakeBatch(callback, self.responses, self.errors)
            self.batches.append(batch)
            return batch

        self.service.new_batch_http_request.side_effect = batch_factory
        self.service_patch = patch.object(gmail, "_get_service", return_value=self.service)
        self.service_factory = self.service_patch.start()
        self.addCleanup(self.service_patch.stop)

    def test_search_then_three_reads_use_one_list_and_one_batch(self):
        with gmail.gmail_cache_scope():
            result = gmail.list_messages()
            self.assertEqual([item["id"] for item in result["messages"]], ["one", "two", "three"])
            self.assertNotIn("body", result["messages"][0])
            for message_id in self.responses:
                self.assertIn("Body " + message_id, gmail.read_message(message_id)["body"])
        self.messages.list.return_value.execute.assert_called_once()
        self.service_factory.assert_called_once()
        self.assertEqual(len(self.batches), 1)
        self.assertEqual(self.batches[0].executions, 1)
        self.assertEqual(len(self.batches[0].requests), 3)
        # GET objects were added to the batch, never executed separately.
        self.assertEqual(self.messages.get.call_count, 3)
        self.messages.get.return_value.execute.assert_not_called()
        self.assertIsNone(gmail._message_cache.get())

    def test_cache_is_not_reused_by_the_next_run_or_another_account(self):
        with gmail.gmail_cache_scope():
            gmail.list_messages()
            self.assertIn("Body one", gmail.read_message("one")["body"])
        other_email = make_email("one", "Other account body")
        self.messages.get.return_value.execute.return_value = other_email
        with gmail.gmail_cache_scope():
            self.assertIn("Other account body", gmail.read_message("one")["body"])
        self.messages.get.return_value.execute.assert_called_once()

    def test_nested_runs_do_not_share_message_cache(self):
        with gmail.gmail_cache_scope():
            gmail.list_messages()
            with gmail.gmail_cache_scope():
                self.assertEqual(gmail._message_cache.get(), {})
            self.assertIn("one", gmail._message_cache.get())

    def test_cache_is_cleared_when_the_agent_fails(self):
        from src.agents.agent import Agent

        with patch("src.agents.agent.anthropic.Anthropic") as client:
            client.return_value.messages.create.side_effect = RuntimeError("fixture failure")
            with self.assertRaises(RuntimeError):
                Agent().run("Read email")
        self.assertIsNone(gmail._message_cache.get())

    def test_partial_failure_is_reported_without_losing_successful_emails(self):
        self.errors["two"] = HttpError(Response({"status": "404"}), b'{}')
        with gmail.gmail_cache_scope():
            result = gmail.list_messages()
            self.assertEqual([item["id"] for item in result["messages"]], ["one", "three"])
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["errors"], [{"id": "two", "error": "Email no longer exists."}])
            self.assertNotIn("two", gmail._message_cache.get())

    def test_batch_auth_failure_is_not_reported_as_an_empty_mailbox(self):
        self.errors["two"] = HttpError(Response({"status": "403"}), b'{}')
        with self.assertRaisesRegex(PermissionError, "Enable Gmail API"):
            gmail.list_messages()

    def test_all_rate_limited_results_include_errors(self):
        self.errors.update({key: HttpError(Response({"status": "429"}), b'{}') for key in self.responses})
        result = gmail.list_messages()
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(result["errors"]), 3)

    def test_large_text_part_is_loaded_once_and_binary_attachment_is_skipped(self):
        response = self.responses["one"]
        response["payload"]["body"] = {"attachmentId": "large-text", "size": 42}
        self.messages.get.return_value.execute.return_value = response
        attachment_api = self.messages.attachments.return_value.get
        attachment_api.return_value.execute.return_value = {"data": base64.urlsafe_b64encode(b"Large text body").decode()}
        with gmail.gmail_cache_scope():
            self.assertEqual(gmail.read_message("one")["body"], "Large text body")
            self.assertEqual(gmail.read_message("one")["body"], "Large text body")
        attachment_api.assert_called_once_with(userId="me", messageId="one", id="large-text")

    def test_unknown_charset_has_utf8_fallback(self):
        response = make_email("one", "Nội dung tiếng Việt")
        for header in response["payload"]["headers"]:
            if header["name"].lower() == "content-type":
                header["value"] = 'text/plain; charset="unknown-charset"'
        self.messages.get.return_value.execute.return_value = response
        self.assertIn("Nội dung tiếng Việt", gmail.read_message("one")["body"])

    def test_quoted_printable_content_is_not_decoded_twice(self):
        message = EmailMessage()
        message.set_content("Literal =41 and café", cte="quoted-printable")
        self.messages.get.return_value.execute.return_value = api_response(message)
        self.assertEqual(gmail.read_message("email-1")["body"].strip(), "Literal =41 and café")

    def test_folded_gmail_headers_are_unfolded_without_rejecting_the_email(self):
        response = make_email("one", "Body with folded headers")
        response["payload"]["headers"].append({
            "name": "Received", "value": "from mail.example.com\r\n\tby gmail.example.com; today",
        })
        response["payload"]["headers"].append({
            "name": "From", "value": 'Example Sender\r\n\t<sender@example.com>',
        })
        self.messages.get.return_value.execute.return_value = response
        result = gmail.read_message("one")
        self.assertIn("Body with folded headers", result["body"])
        self.assertIn("sender@example.com", result["from"])

    def test_related_message_uses_declared_root_body(self):
        message = EmailMessage()
        message.set_type("multipart/related")
        message.set_param("start", "<root>")
        html = EmailMessage()
        html.set_content("<p>Actual body</p>", subtype="html")
        html["Content-ID"] = "<root>"
        unrelated = EmailMessage()
        unrelated.set_content("Unrelated inline text")
        message.attach(html)
        message.attach(unrelated)
        self.messages.get.return_value.execute.return_value = api_response(message)
        self.assertEqual(gmail.read_message("email-1")["body"], "Actual body")

    def test_agent_tool_loop_reuses_prefetch_without_changing_three_llm_turns(self):
        from src.agents.agent import Agent

        usage = SimpleNamespace(input_tokens=10, output_tokens=10)
        list_call = SimpleNamespace(type="tool_use", name="list_gmail_messages", input={}, id="list-1")
        read_calls = [SimpleNamespace(type="tool_use", name="read_gmail_message", input={"message_id": key}, id="read-"+key) for key in self.responses]
        responses = [
            SimpleNamespace(stop_reason="tool_use", content=[list_call], usage=usage),
            SimpleNamespace(stop_reason="tool_use", content=read_calls, usage=usage),
            SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(text="Summary")], usage=usage),
        ]
        with patch("src.agents.agent.anthropic.Anthropic") as client:
            client.return_value.messages.create.side_effect = responses
            agent = Agent()
            self.assertEqual(agent.run("Read today's mail"), "Summary")
            self.assertEqual(client.return_value.messages.create.call_count, 3)
        self.messages.list.return_value.execute.assert_called_once()
        self.messages.get.return_value.execute.assert_not_called()
        self.assertEqual(self.batches[0].executions, 1)
        self.assertEqual(len(agent.get_audit_log()), 4)

    def test_combined_tool_returns_bodies_and_preserves_pagination(self):
        self.messages.list.return_value.execute.return_value["nextPageToken"] = "page-two"
        result = gmail.search_and_read_messages("from:sender@example.com", 3, "previous-page")
        self.messages.list.assert_called_once_with(
            userId="me", q="from:sender@example.com", maxResults=3, pageToken="previous-page",
        )
        self.assertEqual(result["next_page_token"], "page-two")
        self.assertEqual(result["count"], 3)
        self.assertTrue(all(item["body"].startswith("Body ") for item in result["messages"]))
        self.assertTrue(all("snippet" in item and "unread" in item for item in result["messages"]))
        self.assertEqual(len(self.batches), 1)
        self.messages.get.return_value.execute.assert_not_called()
        self.assertIsNone(gmail._message_cache.get())

    def test_combined_tool_handles_empty_search_without_batch(self):
        self.messages.list.return_value.execute.return_value = {}
        result = gmail.search_and_read_messages()
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.batches, [])

    def test_combined_tool_preserves_fetch_errors_and_reports_bad_bodies(self):
        self.errors["two"] = HttpError(Response({"status": "404"}), b'{}')
        self.responses["three"]["payload"]["body"]["data"] = "a"
        result = gmail.search_and_read_messages()
        self.assertEqual([item["id"] for item in result["messages"]], ["one"])
        self.assertEqual(result["count"], 1)
        self.assertEqual({error["id"] for error in result["errors"]}, {"two", "three"})
        self.assertIsNone(gmail._message_cache.get())

    def test_combined_tool_preserves_body_truncation(self):
        self.responses["one"] = make_email("one", "x" * (gmail.MAX_BODY_CHARS + 1))
        result = gmail.search_and_read_messages()
        self.assertEqual(len(result["messages"][0]["body"]), gmail.MAX_BODY_CHARS)
        self.assertTrue(result["messages"][0]["body_truncated"])

    def test_combined_tool_still_requires_gmail_read_permission(self):
        from src.registry import ToolRegistry
        from src.tools.gmail import search_and_read_gmail_messages_tool

        registry = ToolRegistry()
        registry.register(search_and_read_gmail_messages_tool)
        result = registry.call("search_and_read_gmail_messages", {}, "sk-guest-003")
        self.assertIn("error", result)
        self.service_factory.assert_not_called()

    def test_combined_tool_provides_actual_bodies_in_two_llm_calls(self):
        from src.agents.agent import Agent

        usage = SimpleNamespace(input_tokens=10, output_tokens=10)
        call = SimpleNamespace(type="tool_use", name="search_and_read_gmail_messages", input={"max_results": 3}, id="combined-1")
        responses = [
            SimpleNamespace(stop_reason="tool_use", content=[call], usage=usage),
            SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(text="Summary from complete bodies")], usage=usage),
        ]
        with patch("src.agents.agent.anthropic.Anthropic") as client:
            client.return_value.messages.create.side_effect = responses
            agent = Agent()
            self.assertEqual(agent.run("Đọc và tóm tắt email"), "Summary from complete bodies")
            self.assertEqual(client.return_value.messages.create.call_count, 2)
        envelope = json.loads(agent.conversation_history[2]["content"][0]["content"])["result"]
        # Mail content reaches the model only as labelled, untrusted data.
        self.assertEqual(envelope["untrusted_content"]["source"], "search_and_read_gmail_messages")
        tool_result = envelope["data"]
        self.assertEqual(tool_result["count"], 3)
        for item in tool_result["messages"]:
            self.assertIn("Body " + item["id"], item["body"])
        self.assertEqual([entry["tool"] for entry in agent.get_audit_log()], ["search_and_read_gmail_messages"])
        self.assertEqual(self.batches[0].executions, 1)
        self.messages.get.return_value.execute.assert_not_called()
        self.assertIsNone(gmail._message_cache.get())


class RecordingTransport:
    """Actual Google client serialization against a local, fake HTTP transport."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.calls.append((method, uri))
        if method == "GET":
            result = {"messages": [{"id": key} for key in self.responses]}
            return Response({"status": "200", "content-type": "application/json"}), json.dumps(result).encode()
        content_type = next(value for key, value in headers.items() if key.lower() == "content-type")
        body_bytes = body.encode() if isinstance(body, str) else body
        envelope = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + content_type + "\r\n\r\n").encode() + body_bytes
        )
        parts = []
        for part in envelope.iter_parts():
            content_id = str(part["Content-ID"]).strip("<>")
            request_line = part.get_payload().splitlines()[0]
            path = request_line.split()[1]
            self_check = urlsplit(path)
            message_id = unquote(self_check.path.rsplit("/", 1)[-1])
            assert "format=full" in self_check.query
            parts.append(
                "--response_boundary\r\nContent-Type: application/http\r\n"
                f"Content-ID: <response-{content_id}>\r\n\r\n"
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
                + json.dumps(self.responses[message_id]) + "\r\n"
            )
        content = "".join(parts) + "--response_boundary--\r\n"
        return Response({"status": "200", "content-type": "multipart/mixed; boundary=response_boundary"}), content.encode()


class GoogleClientBatchTransportTests(unittest.TestCase):
    def test_combined_tool_uses_two_http_requests_with_the_real_google_client(self):
        responses = {key: make_email(key, "Combined body " + key) for key in ("one", "two", "three")}
        transport = RecordingTransport(responses)
        service = build("gmail", "v1", http=transport, cache_discovery=False)
        with patch.object(gmail, "_get_service", return_value=service):
            result = gmail.search_and_read_messages(max_results=3)
        self.assertTrue(all("Combined body" in message["body"] for message in result["messages"]))
        self.assertEqual(len(transport.calls), 2)
        self.assertIsNone(gmail._message_cache.get())

    def test_real_client_makes_two_http_requests_for_search_and_three_reads(self):
        responses = {key: make_email(key, "Transport body " + key) for key in ("one", "two", "three")}
        transport = RecordingTransport(responses)
        service = build("gmail", "v1", http=transport, cache_discovery=False)
        with patch.object(gmail, "_get_service", return_value=service), gmail.gmail_cache_scope():
            result = gmail.list_messages()
            for message in result["messages"]:
                self.assertIn("Transport body", gmail.read_message(message["id"])["body"])
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][0], "GET")
        self.assertEqual(transport.calls[1][0], "POST")
        self.assertEqual(urlsplit(transport.calls[1][1]).netloc, "gmail.googleapis.com")
        self.assertEqual(urlsplit(transport.calls[1][1]).path, "/batch")


if __name__ == "__main__":
    unittest.main()
