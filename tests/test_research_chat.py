"""Offline acceptance of natural-language Chat -> workflow -> cited reports."""

import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials

from src.agents.agent import Agent
from src.agents.web_research_agent import WebResearchAgent
from src.api import server
from src.configs.research import Settings
from src.schemas.errors import ResearchError
from src.schemas.extraction import CompanyIdentity
from src.services import web_auth
from src.services.research import runtime as rt
from src.services.research.backends.demo import DemoBackend
from src.services.research.backends.live import GMAIL_READ, LiveBackend
from src.services.research.search_client import tavily_search
from src.services.research.store import Store
from src.tools.research import research_chat_scope, research_emails


class ChatWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            db_path=self.temp.name + "/research.sqlite3", use_llm=False
        )
        self.store = Store(self.settings.db_path)
        self.backend = DemoBackend(self.settings, "alice@example.com", self.store)
        self.backend.list_mail = Mock(wraps=self.backend.list_mail)
        self.backend.send = Mock()
        self.backend.trash = Mock()
        for target, value in (
            ("_runtime", (self.settings, self.store)),
            ("backend_for", Mock(return_value=self.backend)),
        ):
            patcher = patch.object(rt, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request = SimpleNamespace(
            state=SimpleNamespace(
                login_session=SimpleNamespace(email="alice@example.com")
            )
        )

    def test_latest_two_run_all_roles_and_return_citations_without_mail_writes(self):
        with research_chat_scope(self.request) as context:
            result = research_emails(latest_count=2)
        self.backend.list_mail.assert_called_once_with(query="in:inbox", limit=2)
        self.assertEqual(len(result["selected_message_ids"]), 2)
        self.assertTrue(result["reports"])
        report = result["reports"][0]
        self.assertEqual(
            set(report["roles"]),
            {
                "email",
                "web_research",
                "company_info",
                "calendar",
                "memory",
                "report_generation",
                "human_approval",
            },
        )
        self.assertTrue(report["facts"])
        self.assertTrue(report["sources"])
        self.assertTrue(all(f["source_ids"] for f in report["facts"]))
        self.assertTrue(context.reports[0]["url"].startswith("/reports/"))
        self.assertNotIn("text", report["sources"][0])
        self.backend.send.assert_not_called()
        self.backend.trash.assert_not_called()

    def test_two_companies_have_independent_reports_and_owner_scopes(self):
        with research_chat_scope(self.request) as context:
            result = research_emails(message_ids=["demo-1", "demo-3"])
        self.assertEqual(len(context.reports), 2)
        self.assertEqual(len({r["id"] for r in context.reports}), 2)
        for report in result["reports"]:
            with self.assertRaises(ResearchError) as error:
                self.store.get("report", "bob@example.com", report["id"])
            self.assertEqual(error.exception.code, "NOT_FOUND")

    def test_duplicate_tool_calls_reuse_one_batch_and_scope_resets(self):
        with research_chat_scope(self.request):
            first = research_emails(latest_count=1)
            second = research_emails(latest_count=2)
        self.assertIs(first, second)
        self.backend.list_mail.assert_called_once()
        self.assertEqual(research_emails()["code"], "WEB_LOGIN_REQUIRED")
        with research_chat_scope(self.request):
            third = research_emails(latest_count=1)
        self.assertNotEqual(first["batch_id"], third["batch_id"])

    def test_invalid_count_and_guessed_website_do_not_read_gmail(self):
        with research_chat_scope(self.request):
            self.assertEqual(research_emails(latest_count=21)["code"], "INVALID_INPUT")
            self.assertEqual(
                research_emails(latest_count=True)["code"], "INVALID_INPUT"
            )
            result = research_emails(
                message_ids=["demo-1"],
                selections=[
                    {
                        "message_id": "demo-1",
                        "name": "Atlas",
                        "website": "https://guessed.example.com",
                    }
                ],
            )
            self.assertEqual(result["code"], "WEBSITE_CONFIRMATION_REQUIRED")
        self.backend.list_mail.assert_not_called()

    def test_no_emails_and_auth_failure_never_invent_reports(self):
        self.backend.list_mail.return_value = {"messages": [], "errors": []}
        with research_chat_scope(self.request) as context:
            self.assertEqual(research_emails()["code"], "NO_EMAILS")
            self.assertEqual(context.reports, [])
        self.backend.list_mail.side_effect = ResearchError(
            "AUTH_REQUIRED", "Sign in", 401
        )
        with research_chat_scope(self.request) as context:
            self.assertEqual(research_emails()["code"], "AUTH_REQUIRED")
            self.assertEqual(context.reports, [])

    def test_calendar_permission_failure_still_returns_company_research(self):
        self.backend.busy = Mock(
            side_effect=ResearchError("SCOPE_REQUIRED", "Connect Calendar", 403)
        )
        with research_chat_scope(self.request):
            result = research_emails(latest_count=1)
        self.assertEqual(result["reports"][0]["calendar"]["status"], "unavailable")
        self.assertTrue(result["reports"][0]["facts"])

    def test_natural_language_chat_tool_executes_real_workflow_and_exposes_report_links(
        self,
    ):
        usage = SimpleNamespace(input_tokens=50, output_tokens=20)
        responses = [
            SimpleNamespace(
                stop_reason="tool_use",
                usage=usage,
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="research_emails",
                        input={"latest_count": 2},
                        id="research-1",
                    )
                ],
            ),
            SimpleNamespace(
                stop_reason="end_turn",
                usage=usage,
                content=[
                    SimpleNamespace(
                        type="text", text="Báo cáo nghiên cứu từ các nguồn đã kiểm tra."
                    )
                ],
            ),
        ]
        with patch("src.agents.agent.anthropic.Anthropic") as client_factory:
            client_factory.return_value.messages.create.side_effect = responses
            agent = Agent()
            with TestClient(server.app, base_url="http://localhost:9004") as client:
                sid = web_auth.create_demo_session()
                session = web_auth._sessions[sid]
                session.demo = False
                session.email = "alice@example.com"
                session.credentials = Credentials(
                    token="fixture-token", scopes=[GMAIL_READ]
                )
                self.addCleanup(web_auth.logout, sid)
                client.cookies.set(web_auth.SESSION_COOKIE, sid)
                with patch.object(server, "get_agent", return_value=agent):
                    response = client.post(
                        "/api/chat",
                        json={
                            "message": "tôi muốn bạn nghiên cứu 2 mail gần nhất giúp tôi và viết bài báo cáo giúp tôi"
                        },
                    )
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertEqual(result["tools_used"], ["research_emails"])
                self.assertTrue(result["reports"])
                self.assertEqual(
                    client.get(result["reports"][0]["url"]).status_code, 200
                )
                self.assertEqual(
                    client.get(
                        "/api/reports/" + result["reports"][0]["id"]
                    ).status_code,
                    200,
                )
        self.backend.send.assert_not_called()
        self.backend.trash.assert_not_called()
        tool_result = json.loads(agent.conversation_history[2]["content"][0]["content"])
        self.assertTrue(tool_result["result"]["data"]["reports"][0]["sources"])


class TavilyTests(unittest.TestCase):
    def transport(self, status=200, body=None):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [
            json.dumps(body if body is not None else {"results": []}).encode()
        ]
        session = Mock()
        session.post.return_value = response
        return session, response

    def test_request_uses_fixed_endpoint_bounded_public_query_and_never_follows_redirects(
        self,
    ):
        session, _response = self.transport(
            body={
                "results": [
                    {
                        "url": "https://atlas.example.com/about?utm_source=test",
                        "title": "Atlas",
                        "content": "Discovery only",
                    },
                    {"url": "http://127.0.0.1/private"},
                    {"url": "https://atlas.example.com/about"},
                ]
            }
        )
        with patch(
            "src.services.research.search_client.requests.Session", return_value=session
        ):
            hits = tavily_search(
                "secret-fixture",
                "Atlas",
                "company news last 30 days",
                time.monotonic() + 10,
            )
        args = session.post.call_args
        self.assertEqual(args.args[0], "https://api.tavily.com/search")
        self.assertFalse(args.kwargs["allow_redirects"])
        self.assertFalse(session.trust_env)
        self.assertEqual(
            args.kwargs["headers"]["Authorization"], "Bearer secret-fixture"
        )
        payload = args.kwargs["json"]
        self.assertEqual(payload["query"], "Atlas company news last 30 days")
        self.assertFalse(payload["include_raw_content"])
        self.assertFalse(payload["include_answer"])
        self.assertEqual(payload["time_range"], "month")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["url"], "https://atlas.example.com/about")
        session.close.assert_called_once()

    def test_missing_key_error_makes_no_request(self):
        with patch("src.services.research.search_client.requests.Session") as factory:
            with self.assertRaises(ResearchError) as error:
                tavily_search("", "Atlas", "news", time.monotonic() + 10)
            self.assertEqual(error.exception.code, "SEARCH_NOT_CONFIGURED")
            factory.assert_not_called()

    def test_auth_quota_redirect_and_server_errors_are_sanitized(self):
        for status in (401, 403, 429, 432, 433, 302, 500):
            with self.subTest(status=status):
                session, response = self.transport(status=status)
                with (
                    patch(
                        "src.services.research.search_client.requests.Session",
                        return_value=session,
                    ),
                    self.assertRaises(ResearchError) as error,
                ):
                    tavily_search(
                        "secret-fixture", "Atlas", "news", time.monotonic() + 10
                    )
                self.assertNotIn("secret-fixture", str(error.exception))
                response.iter_content.assert_not_called()

    def test_oversized_invalid_and_expired_responses_fail_safely(self):
        for chunks in ([b"x" * 1_000_001], [b"not-json"], [b'{"results":null}']):
            session, response = self.transport()
            response.iter_content.return_value = chunks
            with (
                patch(
                    "src.services.research.search_client.requests.Session",
                    return_value=session,
                ),
                self.assertRaises(ResearchError),
            ):
                tavily_search("fixture", "Atlas", "news", time.monotonic() + 10)
        with self.assertRaises(ResearchError) as error:
            tavily_search("fixture", "Atlas", "news", time.monotonic() - 1)
        self.assertEqual(error.exception.code, "DEADLINE_EXCEEDED")

    def test_provider_budget_caps_tavily_calls_at_three(self):
        settings = Settings(
            search_provider="tavily", tavily_key="fixture", max_search_queries=3
        )
        backend = LiveBackend(None, settings)
        with patch(
            "src.services.research.search_client.tavily_search", return_value=[]
        ) as search:
            for _ in range(3):
                backend.search("Atlas", "news", time.monotonic() + 10)
            with self.assertRaises(ResearchError) as error:
                backend.search("atlas", "news", time.monotonic() + 10)
        self.assertEqual(error.exception.code, "BUDGET_EXCEEDED")
        self.assertEqual(search.call_count, 3)

    def test_env_loads_tavily_key_without_exposing_it_in_settings_repr(self):
        with patch.dict(
            "os.environ",
            {"TAVILY_API_KEY": "secret-fixture", "RESEARCH_SEARCH_PROVIDER": "tavily"},
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.tavily_key, "secret-fixture")
        self.assertNotIn("secret-fixture", repr(settings))


class CompanyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.backend.search.return_value = [
            {"url": "https://atlas.example.com/about", "title": "Atlas", "snippet": ""}
        ]
        self.source = {
            "url": "https://atlas.example.com/about",
            "title": "Atlas Labs",
            "text": "Atlas Labs is the company operating this official website.",
        }
        self.backend.fetch.return_value = self.source
        self.reasoner = Mock()
        self.reasoner.company_identity.return_value = CompanyIdentity(
            official=True, evidence=self.source["text"]
        )

    def test_official_website_requires_retrieved_literal_identity_evidence(self):
        company, _candidates, errors = WebResearchAgent(self.backend).resolve_company(
            "Atlas Labs", self.reasoner, time.monotonic() + 10
        )
        self.assertEqual(company["website"], "https://atlas.example.com/")
        self.assertEqual(company["evidence"], "retrieved_official_identity")
        self.assertEqual(company["discovery_pages"], 1)
        self.assertEqual(errors, [])
        self.backend.fetch.assert_called_once()

    def test_false_match_unquoted_claim_or_missing_name_stays_unresolved(self):
        for match in (
            CompanyIdentity(official=False),
            CompanyIdentity(
                official=True, evidence="Invented evidence about Atlas Labs"
            ),
            CompanyIdentity(
                official=True, evidence="the company operating this official website."
            ),
        ):
            with self.subTest(match=match):
                self.reasoner.company_identity.return_value = match
                company, candidates, _errors = WebResearchAgent(
                    self.backend
                ).resolve_company("Atlas Labs", self.reasoner, time.monotonic() + 10)
                self.assertIsNone(company)
                self.assertTrue(candidates)

    def test_third_party_domain_cannot_become_the_official_company_site(self):
        for url in (
            "https://en.wikipedia.org/wiki/Atlas_Labs",
            "https://news.example.com/atlas",
        ):
            self.backend.search.return_value = [{"url": url}]
            self.backend.fetch.return_value = {**self.source, "url": url}
            company, _, _ = WebResearchAgent(self.backend).resolve_company(
                "Atlas Labs", self.reasoner, time.monotonic() + 10
            )
            self.assertIsNone(company)
        self.reasoner.company_identity.assert_not_called()

    def test_search_failure_is_reported_instead_of_claiming_research_completed(self):
        self.backend.search.side_effect = ResearchError(
            "SEARCH_AUTH_REQUIRED", "Invalid key"
        )
        company, _candidates, errors = WebResearchAgent(self.backend).resolve_company(
            "Atlas Labs", self.reasoner, time.monotonic() + 10
        )
        self.assertIsNone(company)
        self.assertEqual(errors[0]["code"], "SEARCH_AUTH_REQUIRED")
        self.backend.fetch.assert_not_called()

    def test_two_official_domains_require_confirmation(self):
        self.backend.search.return_value.append(
            {"url": "https://atlas.other.com/", "title": "Atlas", "snippet": ""}
        )
        self.backend.fetch.side_effect = [
            self.source,
            {**self.source, "url": "https://atlas.other.com/"},
        ]
        company, candidates, _errors = WebResearchAgent(self.backend).resolve_company(
            "Atlas Labs", self.reasoner, time.monotonic() + 10
        )
        self.assertIsNone(company)
        self.assertEqual(len(candidates), 2)
