"""Gmail behavior tests using fake API responses; no real mailbox or LLM calls."""

import base64
from email.message import EmailMessage
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from httplib2 import Response

from src.services import gmail_service as gmail


def api_response(message, message_id="email-1"):
    def payload(part):
        result = {
            "mimeType": part.get_content_type(),
            "filename": part.get_filename() or "",
            "headers": [{"name": name, "value": str(value)} for name, value in part.items()],
            "body": {},
        }
        if part.is_multipart():
            result["parts"] = [payload(child) for child in part.iter_parts()]
        else:
            data = part.get_payload(decode=True) or b""
            result["body"] = {"size": len(data), "data": base64.urlsafe_b64encode(data).decode().rstrip("=")}
        return result

    return {"id": message_id, "threadId": "thread-1", "payload": payload(message)}


class FakeBatch:
    """Callback fake that deliberately completes requests in reverse order."""

    def __init__(self, callback, responses, errors):
        self.callback = callback
        self.responses = responses
        self.errors = errors
        self.requests = []
        self.executions = 0

    def add(self, request, request_id):
        self.requests.append((request_id, request))

    def execute(self):
        self.executions += 1
        for message_id, request in reversed(self.requests):
            error = self.errors.get(message_id)
            self.callback(message_id, None if error else self.responses[message_id], error)


class GmailServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "gmail_token.json"
        self.token_patch = patch.object(gmail, "GOOGLE_GMAIL_TOKEN_FILE", str(self.token))
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.service = Mock()
        self.messages = self.service.users.return_value.messages.return_value
        self.service_patch = patch.object(gmail, "_get_service", return_value=self.service)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.responses, self.errors, self.batches = {}, {}, []

        def new_batch(callback):
            batch = FakeBatch(callback, self.responses, self.errors)
            self.batches.append(batch)
            return batch

        self.service.new_batch_http_request.side_effect = new_batch

    def test_missing_authorization_never_opens_browser(self):
        with patch.object(gmail.InstalledAppFlow, "from_client_secrets_file") as flow:
            with self.assertRaisesRegex(PermissionError, "src.services.gmail_service"):
                gmail._get_credentials()
            flow.assert_not_called()

    def test_calendar_token_cannot_be_used_for_gmail(self):
        self.token.write_text(json.dumps({
            "token": "fake", "refresh_token": "fake", "client_id": "fake",
            "client_secret": "fake", "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
        }))
        with self.assertRaisesRegex(PermissionError, "permission is missing"):
            gmail._get_credentials()

    def test_malformed_token_has_actionable_error(self):
        self.token.write_text("not json")
        with self.assertRaisesRegex(PermissionError, "src.services.gmail_service"):
            gmail._get_credentials()

    def test_refresh_saves_owner_only_token(self):
        self.token.write_text("{}")
        creds = Mock(valid=False, refresh_token="fake")
        creds.has_scopes.return_value = True
        creds.to_json.return_value = '{"token":"refreshed-fake"}'
        with patch.object(gmail.Credentials, "from_authorized_user_info", return_value=creds):
            self.assertIs(gmail._get_credentials(), creds)
        creds.refresh.assert_called_once()
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.token.read_text())["token"], "refreshed-fake")

    def test_revoked_token_requests_new_login(self):
        self.token.write_text("{}")
        creds = Mock(valid=False, refresh_token="fake")
        creds.has_scopes.return_value = True
        creds.refresh.side_effect = RefreshError("revoked")
        with patch.object(gmail.Credentials, "from_authorized_user_info", return_value=creds):
            with self.assertRaisesRegex(PermissionError, "revoked.*src.services.gmail_service"):
                gmail._get_credentials()

    def test_login_requires_gmail_consent_and_preserves_calendar_token(self):
        client = Path(self.directory.name) / "client.json"
        client.write_text("{}")
        calendar_token = Path(self.directory.name) / "token.json"
        calendar_token.write_text("calendar-token")
        creds = Mock(granted_scopes=gmail.SCOPES, scopes=gmail.SCOPES)
        creds.to_json.return_value = '{"token":"fake-gmail"}'
        flow = Mock()
        flow.run_local_server.return_value = creds
        with patch.object(gmail, "GOOGLE_GMAIL_CLIENT_FILE", str(client)), patch.object(
            gmail.InstalledAppFlow, "from_client_secrets_file", return_value=flow
        ) as factory:
            self.assertIn("read-only", gmail.login())
            factory.assert_called_once_with(str(client), gmail.SCOPES)
        self.assertEqual(calendar_token.read_text(), "calendar-token")
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.assertEqual(flow.run_local_server.call_args.kwargs["bind_addr"], "127.0.0.1")
        self.assertFalse(flow.run_local_server.call_args.kwargs["redirect_uri_trailing_slash"])

    def test_denied_gmail_scope_is_not_saved(self):
        client = Path(self.directory.name) / "client.json"
        client.write_text("{}")
        flow = Mock()
        flow.run_local_server.return_value = Mock(granted_scopes=["other"], scopes=gmail.SCOPES)
        with patch.object(gmail, "GOOGLE_GMAIL_CLIENT_FILE", str(client)), patch.object(
            gmail.InstalledAppFlow, "from_client_secrets_file", return_value=flow
        ):
            with self.assertRaisesRegex(PermissionError, "not granted"):
                gmail.login()
        self.assertFalse(self.token.exists())

    def test_search_preserves_pagination_and_returns_headers(self):
        self.messages.list.return_value.execute.return_value = {
            "messages": [{"id": "email-1"}], "nextPageToken": "next",
        }
        self.responses["email-1"] = {
            "id": "email-1", "labelIds": ["UNREAD"], "snippet": "A &amp; B",
            "payload": {"headers": [{"name": "SUBJECT", "value": "Invoice"}]},
        }
        result = gmail.list_messages("from:billing@example.com", 5, "previous")
        self.messages.list.assert_called_once_with(
            userId="me", q="from:billing@example.com", maxResults=5, pageToken="previous"
        )
        self.assertEqual(result["next_page_token"], "next")
        self.assertEqual(result["messages"][0]["subject"], "Invoice")
        self.assertEqual(result["messages"][0]["snippet"], "A & B")
        self.assertTrue(result["messages"][0]["unread"])
        self.messages.get.assert_called_once_with(userId="me", id="email-1", format="full")
        self.messages.get.return_value.execute.assert_not_called()
        self.assertEqual(self.batches[0].executions, 1)

    def test_empty_search(self):
        self.messages.list.return_value.execute.return_value = {}
        self.assertEqual(gmail.list_messages()["messages"], [])
        self.messages.get.assert_not_called()
        self.service.new_batch_http_request.assert_not_called()

    def test_invalid_page_sizes_fail_before_api(self):
        for value in (0, 21, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gmail.list_messages(max_results=value)
        self.service.users.assert_not_called()

    def test_multipart_email_prefers_plain_text_and_omits_attachment_body(self):
        message = EmailMessage()
        message["Subject"] = "Hóa đơn tháng này"
        message["From"] = "sender@example.com"
        message.set_content("Xin chào, đây là hóa đơn.")
        message.add_alternative("<p>Duplicate HTML</p>", subtype="html")
        message.add_attachment(b"secret attachment", maintype="application", subtype="pdf", filename="invoice.pdf")
        self.messages.get.return_value.execute.return_value = api_response(message)
        result = gmail.read_message("email-1")
        self.assertEqual(result["subject"], "Hóa đơn tháng này")
        self.assertIn("Xin chào", result["body"])
        self.assertNotIn("Duplicate", result["body"])
        self.assertNotIn("secret attachment", result["body"])
        self.assertEqual(result["attachments"], ["invoice.pdf"])
        self.messages.get.assert_called_once_with(userId="me", id="email-1", format="full")
        self.messages.attachments.assert_not_called()

    def test_html_only_email_strips_styles_and_scripts(self):
        message = EmailMessage()
        message.set_content("<style>hidden</style><p>Hello &amp; welcome</p><script>bad()</script>", subtype="html")
        self.messages.get.return_value.execute.return_value = api_response(message)
        result = gmail.read_message("email-1")
        self.assertEqual(result["body"], "Hello & welcome")

    def test_non_utf8_body_and_truncation(self):
        message = EmailMessage()
        message.set_content("café " * 5000, charset="iso-8859-1")
        self.messages.get.return_value.execute.return_value = api_response(message)
        result = gmail.read_message("email-1")
        self.assertTrue(result["body"].startswith("café"))
        self.assertEqual(len(result["body"]), gmail.MAX_BODY_CHARS)
        self.assertTrue(result["body_truncated"])

    def test_empty_message_id_fails_before_api(self):
        with self.assertRaises(ValueError):
            gmail.read_message(" ")
        self.service.users.assert_not_called()

    def test_api_disabled_error_is_actionable(self):
        self.messages.list.return_value.execute.side_effect = HttpError(
            Response({"status": "403"}), b'{"error":{"message":"API disabled"}}'
        )
        with self.assertRaisesRegex(PermissionError, "Enable Gmail API"):
            gmail.list_messages()


class AgentGmailTests(unittest.TestCase):
    def test_registry_blocks_guest_and_allows_default_agent(self):
        from src.registry.registry import USER_DB, check_scopes
        from src.tools.gmail import ALL_GMAIL_TOOLS

        for tool in ALL_GMAIL_TOOLS:
            check_scopes(USER_DB["sk-admin-001"], tool)
            with self.assertRaises(PermissionError):
                check_scopes(USER_DB["sk-guest-003"], tool)

    def test_agent_dispatches_gmail_tool_and_returns_result(self):
        from src.agents.agent import Agent

        tool_call = SimpleNamespace(type="tool_use", name="list_gmail_messages", input={"max_results": 5}, id="call-1")
        usage = SimpleNamespace(input_tokens=10, output_tokens=10)
        responses = [
            SimpleNamespace(stop_reason="tool_use", content=[tool_call], usage=usage),
            SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(text="Có một email.")], usage=usage),
        ]
        with patch("src.agents.agent.anthropic.Anthropic") as client:
            client.return_value.messages.create.side_effect = responses
            agent = Agent()
            handler = Mock(return_value={"messages": [{"id": "email-1"}]})
            with patch.object(agent.registry.get_tool("list_gmail_messages"), "handler", handler):
                self.assertEqual(agent.run("Đọc 5 email mới nhất"), "Có một email.")
        handler.assert_called_once_with(max_results=5)
        self.assertEqual(agent.get_audit_log()[-1]["tool"], "list_gmail_messages")
        self.assertIn("email-1", agent.conversation_history[-2]["content"][0]["content"])
        self.assertIn("read_gmail_message", {tool["name"] for tool in agent.get_tools_for_claude()})


if __name__ == "__main__":
    unittest.main()
