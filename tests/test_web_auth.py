"""Exercise the login gate and OAuth callbacks without contacting Google."""

from datetime import datetime, timedelta
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from httplib2 import Response
from oauthlib.oauth2.rfc6749.errors import InvalidClientError, InvalidGrantError
from requests.exceptions import Timeout

from src.api import server
from src.services import gmail_service as gmail
from src.services import web_auth


def fake_credentials(token="fake-user-token", scopes=None):
    return Credentials(
        token=token, refresh_token="fake-refresh", client_id="fake-client",
        client_secret="fake-secret", scopes=gmail.SCOPES if scopes is None else scopes,
        expiry=datetime.utcnow() + timedelta(hours=1),
    )


class WebLoginTests(unittest.TestCase):
    def setUp(self):
        web_auth._sessions.clear()
        web_auth._pending.clear()
        self.addCleanup(web_auth._sessions.clear)
        self.addCleanup(web_auth._pending.clear)
        self.client = TestClient(server.app, base_url="http://localhost:9004", follow_redirects=False)
        self.addCleanup(self.client.close)
        self.flow = Mock()
        self.flow.authorization_url.return_value = ("https://accounts.google.com/authorize?state=test-state", "test-state")
        self.flow.credentials = fake_credentials()
        self.flow_patch = patch.object(web_auth.Flow, "from_client_secrets_file", return_value=self.flow)
        self.flow_factory = self.flow_patch.start()
        self.addCleanup(self.flow_patch.stop)
        profile_patch = patch.object(web_auth, "_gmail_email", return_value="owner@example.com")
        self.profile = profile_patch.start()
        self.addCleanup(profile_patch.stop)

    def sign_in(self, client=None):
        client = client or self.client
        start = client.get("/auth/login")
        self.assertEqual(start.status_code, 303)
        return client.get("/", params={"code": "fake-code", "state": "test-state"})

    def test_root_requires_login_and_login_page_has_google_button(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        page = self.client.get("/login")
        self.assertIn("Đăng nhập bằng Google", page.text)
        self.assertNotIn('id="chatContainer"', page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")

    def test_apis_and_docs_cannot_bypass_login(self):
        for path in ("/api/chat", "/api/clear"):
            with self.subTest(path=path):
                response = self.client.post(path, json={"message": "read mail"})
                self.assertEqual(response.status_code, 401)
        for path in ("/api/audit", "/api/me"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/docs").status_code, 303)
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")

    def test_login_cookie_and_pkce_flow(self):
        response = self.client.get("/auth/login")
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("fake-user-token", cookie)
        self.assertNotIn("fake-secret", cookie)
        self.assertTrue(self.flow_factory.call_args.kwargs["autogenerate_code_verifier"])
        # One sign-in grants Gmail and Calendar; no separate "connect" step is needed.
        self.assertEqual(self.flow_factory.call_args.kwargs["scopes"], web_auth.RESEARCH_SCOPES)

    def test_successful_callback_opens_workflow_and_keeps_chat_available(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.flow.fetch_token.assert_called_once_with(code="fake-code", timeout=20)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertNotIn("fake-user-token", response.headers["set-cookie"])
        self.assertIsNone(self.client.cookies.get(web_auth.OAUTH_COOKIE))
        self.assertEqual(self.client.get("/api/me").json(), {"email": "owner@example.com"})
        self.assertEqual(self.client.get("/").headers["location"], "/research")
        self.assertIn('id="researchForm"', self.client.get("/research").text)
        self.assertIn('href="/chat"', self.client.get("/research").text)
        self.assertIn('id="chatContainer"', self.client.get("/chat").text)
        self.assertEqual(self.client.get("/login").headers["location"], "/")

    def test_mismatched_state_cannot_exchange_code(self):
        self.client.get("/auth/login")
        response = self.client.get("/?state=attacker-state&code=fake-code")
        self.assertEqual(response.headers["location"], "/login?error=expired")
        self.flow.fetch_token.assert_not_called()
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_callback_from_another_browser_is_rejected(self):
        self.client.get("/auth/login")
        with TestClient(server.app, base_url="http://localhost:9004") as other:
            response = other.get("/?state=test-state&code=fake-code", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login?error=expired")
        self.flow.fetch_token.assert_not_called()

    def test_expired_login_and_callback_replay_are_rejected(self):
        self.client.get("/auth/login")
        nonce = self.client.cookies.get(web_auth.OAUTH_COOKIE)
        web_auth._pending[nonce].expires_at = time.time() - 1
        response = self.client.get("/?state=test-state&code=fake-code")
        self.assertEqual(response.headers["location"], "/login?error=expired")
        self.flow.fetch_token.assert_not_called()
        self.sign_in()
        self.flow.fetch_token.reset_mock()
        self.client.get("/?state=test-state&code=fake-code")
        self.flow.fetch_token.assert_not_called()

    def test_denied_consent_returns_login(self):
        self.client.get("/auth/login")
        response = self.client.get("/?error=access_denied&state=test-state")
        self.assertEqual(response.headers["location"], "/login?error=cancelled")
        self.assertEqual(web_auth._pending, {})
        self.flow.fetch_token.assert_not_called()

    def test_missing_scope_never_creates_login_session(self):
        self.flow.credentials = fake_credentials(scopes=["https://www.googleapis.com/auth/calendar.readonly"])
        response = self.sign_in()
        self.assertEqual(response.headers["location"], "/login?error=gmail_permission")
        self.assertEqual(web_auth._sessions, {})
        self.profile.assert_not_called()

    def test_token_exchange_error_does_not_leak_secrets(self):
        self.flow.fetch_token.side_effect = ValueError("fake-secret-access-token")
        with self.assertLogs(web_auth.logger, level="WARNING") as logs:
            response = self.sign_in()
        self.assertEqual(response.headers["location"], "/login?error=google")
        self.assertNotIn("fake-secret-access-token", response.text + str(response.headers))
        self.assertNotIn("fake-secret-access-token", " ".join(logs.output))
        self.assertEqual(web_auth._sessions, {})

    def test_oauth_errors_have_specific_actions(self):
        for error, expected in (
            (InvalidClientError(), "google_client"),
            (InvalidGrantError(), "google_code"),
            (Timeout(), "google_network"),
        ):
            with self.subTest(expected=expected):
                self.flow.fetch_token.side_effect = error
                response = self.sign_in()
                self.assertEqual(response.headers["location"], "/login?error=" + expected)

    def test_disabled_gmail_api_is_identified_and_links_setup(self):
        self.profile.side_effect = HttpError(
            Response({"status": "403"}),
            b'{"error":{"errors":[{"reason":"accessNotConfigured"}],"details":[{"reason":"SERVICE_DISABLED"}]}}',
        )
        response = self.sign_in()
        self.assertEqual(response.headers["location"], "/login?error=gmail_api_disabled")
        with patch.object(web_auth, "gmail_api_setup_url", return_value="https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=test-project"):
            page = self.client.get(response.headers["location"])
        self.assertIn("Gmail API chưa được bật", page.text)
        self.assertIn("gmail.googleapis.com?project=test-project", page.text)
        self.assertEqual(web_auth._sessions, {})

    def test_gmail_scope_and_policy_errors_are_distinct(self):
        for content, expected in (
            (b'{"error":{"errors":[{"reason":"insufficientPermissions"}]}}', "gmail_permission"),
            (b'{"error":{"errors":[{"reason":"domainPolicy"}]}}', "gmail_access"),
        ):
            with self.subTest(expected=expected):
                self.profile.side_effect = HttpError(Response({"status": "403"}), content)
                response = self.sign_in()
                self.assertEqual(response.headers["location"], "/login?error=" + expected)

    def test_callback_codes_are_not_exported_in_traces(self):
        from src.observability import telemetry
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        telemetry._tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.client.get("/?state=not-real&code=private-diagnostic-code")
        attributes = str([dict(span.attributes) for span in exporter.get_finished_spans()])
        self.assertNotIn("private-diagnostic-code", attributes)

    def test_logout_invalidates_server_side_session(self):
        self.sign_in()
        session_id = self.client.cookies.get(web_auth.SESSION_COOKIE)
        response = self.client.post("/auth/logout")
        self.assertEqual(response.headers["location"], "/login")
        self.assertIsNone(web_auth.get_session(session_id))
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        # Replaying the old cookie also fails after server-side invalidation.
        self.client.cookies.set(web_auth.SESSION_COOKIE, session_id)
        self.assertEqual(self.client.get("/api/audit").status_code, 401)

    def test_expired_session_and_forged_cookie_are_rejected(self):
        self.client.cookies.set(web_auth.SESSION_COOKIE, "forged")
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.client.cookies.clear()
        self.sign_in()
        web_auth._sessions[self.client.cookies.get(web_auth.SESSION_COOKIE)].expires_at = time.time() - 1
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_cross_origin_write_is_blocked(self):
        self.sign_in()
        response = self.client.post("/api/clear", json={"message": ""}, headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)

    def test_other_loopback_host_starts_login_on_callback_host(self):
        with TestClient(server.app, base_url="http://127.0.0.1:9004", follow_redirects=False) as client:
            response = client.get("/auth/login")
        self.assertEqual(response.headers["location"], "http://localhost:9004/auth/login")
        self.flow_factory.assert_not_called()

    def test_two_browsers_use_their_own_gmail_credentials_and_chat(self):
        self.sign_in()
        first_credentials = self.flow.credentials
        with TestClient(server.app, base_url="http://localhost:9004", follow_redirects=False) as other:
            self.flow.credentials = fake_credentials(token="second-user-token")
            self.profile.return_value = "other@example.com"
            self.sign_in(other)
            first_agent, second_agent = Mock(), Mock()
            for agent in (first_agent, second_agent):
                agent.audit_mark.return_value = 0
                agent.audit_since.return_value = []
            first_agent.run.side_effect = lambda message: gmail._get_credentials().token
            second_agent.run.side_effect = lambda message: gmail._get_credentials().token
            with patch("src.api.server.Agent", side_effect=[first_agent, second_agent]):
                first_response = self.client.post("/api/chat", json={"session_id": "same-id", "message": "read"})
                second_response = other.post("/api/chat", json={"session_id": "same-id", "message": "read"})
            self.assertEqual(first_response.json()["response"], first_credentials.token)
            self.assertEqual(second_response.json()["response"], "second-user-token")
            self.client.post("/api/clear", json={"session_id": "same-id", "message": ""})
            first_agent.clear_history.assert_called_once()
            second_agent.clear_history.assert_not_called()
        self.assertIsNone(gmail.request_credentials.get())

    def test_missing_client_config_returns_useful_error(self):
        self.flow_factory.side_effect = FileNotFoundError("client missing")
        response = self.client.get("/auth/login")
        self.assertEqual(response.headers["location"], "/login?error=config")
        self.assertIn("file OAuth", self.client.get(response.headers["location"]).text)


class CredentialIsolationTests(unittest.TestCase):
    def test_web_credentials_do_not_read_or_write_cli_token(self):
        credentials = fake_credentials()
        token = gmail.request_credentials.set(credentials)
        try:
            with patch.object(gmail.Path, "is_file", side_effect=AssertionError("must not read disk")):
                self.assertIs(gmail._get_credentials(), credentials)
        finally:
            gmail.request_credentials.reset(token)

    def test_web_refresh_does_not_replace_cli_token(self):
        credentials = Mock(valid=False, refresh_token="fake-refresh")
        credentials.has_scopes.return_value = True
        token = gmail.request_credentials.set(credentials)
        try:
            with patch.object(gmail, "_save_credentials") as save:
                self.assertIs(gmail._get_credentials(), credentials)
                credentials.refresh.assert_called_once()
                save.assert_not_called()
        finally:
            gmail.request_credentials.reset(token)

    def test_audit_entries_belong_to_the_agent_that_called_the_tool(self):
        from src.registry import ToolRegistry
        from src.registry.models import ToolDefinition

        first, second = ToolRegistry(), ToolRegistry()
        first.register(ToolDefinition(
            name="fake_read", description="Read fixture", input_schema={"type": "object"},
            required_scopes=["gmail:read"], handler=lambda: {"subject": "private email"},
        ))
        first.call("fake_read", {}, "sk-admin-001")
        self.assertEqual(len(first.get_audit_log()), 1)
        self.assertEqual(second.get_audit_log(), [])


if __name__ == "__main__":
    unittest.main()
