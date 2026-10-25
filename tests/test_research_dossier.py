"""Offline acceptance of the search agent, dossier validation and report page data."""

import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.agents.search_agent import SearchAgent
from src.api import research_routes as api
from src.configs.research import Settings
from src.schemas.errors import ResearchError
from src.services.research import runtime as rt
from src.services.research.backends.demo import DemoBackend
from src.services.research.dossier_service import (
    dossier_from_facts,
    dossier_source_ids,
    scorecard,
)
from src.services.research.store import Store
from src.tools.research import research_chat_scope, research_emails

PAGE = {
    "id": "src-home",
    "url": "https://acme.example.com/",
    "title": "Acme",
    "kind": "website",
    "retrieved_at": "2026-10-25T00:00:00+00:00",
    "published_at": None,
    "text": "Acme builds OCR for banks. Founded by Lan Tran. Plans start at 1.200.000 VND.",
    "access": "full_text",
    "demo": False,
}
HIT = {
    "url": "https://news.example.org/acme-raises",
    "title": "Acme raises seed",
    "snippet": "Acme raised a seed round on 2026-10-20.",
}


def tool_use(name, payload, call_id):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=payload)


def response(*blocks, stop="tool_use"):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


SUBMISSION = {
    "headline": {
        "name": "Acme",
        "website": "https://acme.example.com/",
        "industry": "OCR",
    },
    "summary": {
        "text": "Acme làm OCR cho ngân hàng.",
        "urls": ["https://acme.example.com/"],
        "evidence": "Acme builds OCR for banks",
    },
    "kpis": [
        {
            "value": "1.2",
            "unit": "triệu ₫",
            "label": "Giá gói cơ bản",
            "urls": ["https://acme.example.com/"],
        },
        {
            "value": "99",
            "label": "Bịa đặt",
            "urls": ["https://never-retrieved.example.net/"],
        },
    ],
    "founders": [
        {
            "name": "Lan Tran",
            "achievements": [
                {
                    "text": "Sáng lập Acme.",
                    "urls": ["https://acme.example.com/"],
                    "evidence": "Lan Tran gave a talk",
                },
                {"text": "Không nguồn.", "urls": []},
            ],
            "profiles": [
                {
                    "platform": "Web",
                    "handle": "acme",
                    "link": "https://acme.example.com/",
                    "urls": ["https://acme.example.com/"],
                },
                {
                    "platform": "Web",
                    "link": "https://elsewhere.example.net/",
                    "urls": ["https://acme.example.com/"],
                },
            ],
        }
    ],
    "news": [
        {"text": "Acme gọi vốn hạt giống.", "date": "2026-10-20", "urls": [HIT["url"]]}
    ],
    "meeting_prep": ["Hỏi về giá."],
    "gaps": ["Doanh thu"],
}


class FakeBackend:
    mode = "live"

    def __init__(self):
        self.web_search = Mock(return_value=[HIT])
        self.fetch = Mock(return_value=dict(PAGE))


class SearchAgentTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            use_llm=True, agent_search_limit=1, agent_fetch_limit=1
        )
        env = patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fixture"})
        env.start()
        self.addCleanup(env.stop)

    def run_agent(self, replies, backend=None):
        backend = backend or FakeBackend()
        calls = []

        def create(**kwargs):
            # The agent keeps appending to one list; record what each call saw.
            calls.append(dict(kwargs, messages=list(kwargs["messages"])))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        client = Mock()
        client.messages.create.side_effect = create
        fake = Mock()
        fake.return_value.__enter__ = Mock(return_value=client)
        fake.return_value.__exit__ = Mock(return_value=False)
        with patch("anthropic.Anthropic", fake):
            result = SearchAgent(backend, self.settings).run(
                {
                    "name": "Acme",
                    "website": "https://acme.example.com/",
                    "domain": "acme.example.com",
                },
                [],
                [{"from": "x", "subject": "y", "body": "z"}],
                time.monotonic() + 120,
            )
        return result, backend, calls

    def test_search_read_submit_keeps_only_retrieved_citations(self):
        (dossier, sources, errors), backend, calls = self.run_agent(
            [
                response(
                    tool_use("web_search", {"query": "Acme", "recent": True}, "a"),
                    tool_use("fetch_page", {"url": PAGE["url"]}, "b"),
                ),
                response(tool_use("submit_dossier", SUBMISSION, "c")),
            ]
        )
        backend.web_search.assert_called_once()
        self.assertTrue(backend.web_search.call_args.kwargs["recent"])
        # Both tool results come back in one user message.
        results = calls[1]["messages"][-1]["content"]
        self.assertEqual([item["tool_use_id"] for item in results], ["a", "b"])

        self.assertEqual(dossier["mode"], "agent")
        by_id = {source["id"]: source for source in sources}
        # The invented KPI and the source-less achievement were removed.
        self.assertEqual([kpi["label"] for kpi in dossier["kpis"]], ["Giá gói cơ bản"])
        founder = dossier["founders"][0]
        self.assertEqual(len(founder["achievements"]), 1)
        self.assertIs(founder["achievements"][0]["verified"], False)
        self.assertIs(dossier["summary"]["verified"], True)
        # Profile links must be pages that were retrieved.
        self.assertEqual(
            [p["link"] for p in founder["profiles"]],
            ["https://acme.example.com/", ""],
        )
        self.assertTrue(any(error["code"] == "EVIDENCE_REJECTED" for error in errors))
        self.assertEqual(dossier_source_ids(dossier), set(by_id))
        news_source = next(s for s in sources if s["url"] == HIT["url"])
        self.assertEqual(news_source["kind"], "news")
        self.assertEqual(news_source["access"], "snippet_only")
        self.assertEqual(news_source["published_at"], "2026-10-20")
        self.assertEqual(by_id[PAGE["id"]]["kind"], "official")

    def test_shallow_submission_is_rejected_once(self):
        (dossier, _, _), _, calls = self.run_agent(
            [
                response(tool_use("submit_dossier", SUBMISSION, "early")),
                response(
                    tool_use("web_search", {"query": "Acme", "recent": True}, "s"),
                    tool_use("fetch_page", {"url": PAGE["url"]}, "f"),
                ),
                response(tool_use("submit_dossier", SUBMISSION, "late")),
            ]
        )
        first = calls[1]["messages"][-1]["content"][0]
        self.assertTrue(first["is_error"])
        self.assertIn("too shallow", first["content"])
        self.assertIsNotNone(dossier)

    def test_invalid_dossier_is_reported_back_and_failures_fall_back(self):
        (dossier, sources, errors), _, _ = self.run_agent(
            [
                response(
                    tool_use("web_search", {"query": "a", "recent": True}, "s"),
                    tool_use("fetch_page", {"url": PAGE["url"]}, "f"),
                ),
                response(tool_use("submit_dossier", {"headline": "nope"}, "bad")),
                response(stop="end_turn"),
                response(stop="end_turn"),
            ]
        )
        self.assertIsNone(dossier)
        self.assertEqual(sources, [])
        self.assertTrue(errors)

        (dossier, _, errors), _, _ = self.run_agent([RuntimeError("provider down")])
        self.assertIsNone(dossier)
        self.assertEqual(errors[0]["code"], "SEARCH_AGENT_FAILED")

    def test_disabled_without_key_demo_mode_or_llm(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(SearchAgent(FakeBackend(), self.settings).enabled)
        demo = SimpleNamespace(mode="demo")
        self.assertFalse(SearchAgent(demo, self.settings).enabled)
        off = Settings(use_llm=False)
        self.assertFalse(SearchAgent(FakeBackend(), off).enabled)

    def test_search_and_fetch_budgets(self):
        self.settings = self.settings.model_copy(
            update={"agent_search_limit": 2, "agent_fetch_limit": 2}
        )
        (_, _, _), backend, calls = self.run_agent(
            [
                response(
                    *[
                        tool_use("web_search", {"query": f"q{i}"}, f"s{i}")
                        for i in range(3)
                    ],
                    tool_use("fetch_page", {"url": "https://a.example.com/1"}, "f1"),
                    tool_use("fetch_page", {"url": "https://a.example.com/2"}, "f2"),
                    tool_use("fetch_page", {"url": "https://a.example.com/3"}, "f3"),
                ),
                response(stop="end_turn"),
                response(stop="end_turn"),
            ]
        )
        self.assertEqual(backend.web_search.call_count, 2)
        self.assertEqual(backend.fetch.call_count, 2)
        results = calls[1]["messages"][-1]["content"]
        self.assertEqual(sum(1 for item in results if item.get("is_error")), 2)
        # Once both budgets are spent only submit_dossier stays available.
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            ["submit_dossier"],
        )


class DossierShapeTests(unittest.TestCase):
    FACTS = [
        {
            "id": "1",
            "category": "overview",
            "text": "Atlas là công ty AI.",
            "evidence": "Atlas",
            "source_ids": ["s1"],
            "status": "source_statement",
        },
        {
            "id": "2",
            "category": "people",
            "text": "Lê An sáng lập.",
            "evidence": "Lê An",
            "source_ids": ["s1"],
            "person": "Lê An",
            "person_section": "career",
            "status": "source_statement",
        },
        {
            "id": "3",
            "category": "risk",
            "text": "Có phản ánh chậm bàn giao.",
            "evidence": "x",
            "source_ids": ["s2"],
            "status": "allegation",
        },
    ]

    def test_fact_fallback_keeps_citations_and_lists_gaps(self):
        dossier = dossier_from_facts(
            {"name": "Atlas", "website": "https://a.example/"}, self.FACTS
        )
        self.assertEqual(dossier["mode"], "facts")
        self.assertEqual(dossier["founders"][0]["name"], "Lê An")
        self.assertEqual(dossier["risks"][0]["status"], "allegation")
        self.assertIn("Sản phẩm & giá bán", dossier["gaps"])
        self.assertEqual(dossier_source_ids(dossier), {"s1", "s2"})

    def test_scorecard_reports_only_what_the_dossier_holds(self):
        dossier = dossier_from_facts({"name": "Atlas", "website": ""}, self.FACTS)
        rows, percent = scorecard(dossier, [{"kind": "official"}], 42.4)
        by_name = {row["criterion"]: row["result"] for row in rows}
        self.assertEqual(
            list(by_name),
            [
                "Độ đầy đủ thông tin",
                "Độ chính xác",
                "Thời gian tạo báo cáo",
                "Số nguồn tham khảo",
                "Độ mới của dữ liệu",
                "Mức sẵn sàng",
            ],
        )
        self.assertLess(percent, 75)
        self.assertEqual(by_name["Thời gian tạo báo cáo"], "42 giây")
        self.assertTrue(by_name["Mức sẵn sàng"].startswith("Cần bổ sung"))
        self.assertIn("Chưa xác nhận", by_name["Độ mới của dữ liệu"])


class ReportPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(db_path=self.temp.name + "/r.sqlite3", use_llm=False)
        self.store = Store(self.settings.db_path)
        self.backend = DemoBackend(self.settings, "alice@example.com", self.store)
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

    def test_chat_returns_one_briefing_page_for_all_companies(self):
        with research_chat_scope(self.request) as context:
            result = research_emails(message_ids=["demo-1", "demo-3"])
        self.assertEqual(result["briefing_url"], "/briefings/" + result["batch_id"])
        self.assertEqual(context.batches[0]["url"], result["briefing_url"])
        self.assertEqual(len(context.batches[0]["companies"]), 2)
        for report in result["reports"]:
            self.assertEqual(report["highlights"]["name"], report["company"])
            self.assertTrue(report["scorecard"])

    def test_reports_carry_dossier_and_old_reports_are_upgraded(self):
        with research_chat_scope(self.request):
            result = research_emails(message_ids=["demo-1"])
        report_id = result["reports"][0]["id"]
        report = self.store.get("report", "alice@example.com", report_id)
        self.assertEqual(report["dossier"]["mode"], "facts")
        cited = dossier_source_ids(report["dossier"])
        self.assertTrue(cited <= {source["id"] for source in report["sources"]})
        # A report saved before dossiers existed is rebuilt when read.
        for key in ("dossier", "scorecard"):
            report.pop(key)
        self.store.put("report", "alice@example.com", report_id, report)
        upgraded = api.get_report(report_id, self.request)
        self.assertEqual(
            upgraded["dossier"]["headline"]["name"], report["company"]["name"]
        )
        self.assertEqual(len(upgraded["scorecard"]), 6)

    def test_calendar_is_checked_only_when_an_email_asks_for_a_meeting(self):
        self.backend.busy = Mock(wraps=self.backend.busy)
        self.backend.events = Mock(wraps=self.backend.events)
        with research_chat_scope(self.request):
            quiet = research_emails(message_ids=["demo-4"])
        report = quiet["reports"][0]
        self.assertEqual(report["calendar"]["status"], "not_requested")
        self.assertEqual(report["roles"]["calendar"], "not_requested")
        self.assertEqual(report["meetings"], [])
        self.backend.busy.assert_not_called()
        self.backend.events.assert_not_called()

        with research_chat_scope(self.request):
            asked = research_emails(message_ids=["demo-1"])
        report = asked["reports"][0]
        self.assertEqual(report["calendar"]["status"], "checked")
        self.assertEqual(report["roles"]["calendar"], "completed")
        self.assertEqual(len(report["meetings"]), 1)
        self.backend.busy.assert_called()

    def test_companies_are_researched_concurrently(self):
        spans = {}
        real_crawl = self.backend.crawl

        def slow_crawl(website, limit, deadline):
            started = time.monotonic()
            time.sleep(0.4)
            result = real_crawl(website, limit, deadline)
            spans[website] = (started, time.monotonic())
            return result

        self.backend.crawl = slow_crawl
        started = time.monotonic()
        with research_chat_scope(self.request) as context:
            research_emails(message_ids=["demo-1", "demo-2", "demo-3"])
        elapsed = time.monotonic() - started
        self.assertEqual(len(context.reports), 3)
        self.assertEqual(len(spans), 3)
        # Serial execution would take at least 3 x 0.4s.
        self.assertLess(elapsed, 1.1)

    def test_briefing_page_is_owner_scoped(self):
        with research_chat_scope(self.request):
            result = research_emails(message_ids=["demo-1"])
        stranger = SimpleNamespace(
            state=SimpleNamespace(
                login_session=SimpleNamespace(email="bob@example.com")
            )
        )
        self.assertEqual(
            api.briefing_page(result["batch_id"], self.request).status_code, 200
        )
        with self.assertRaises(ResearchError) as error:
            api.briefing_page(result["batch_id"], stranger)
        self.assertEqual(error.exception.code, "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
