"""Offline acceptance of the low-latency pipeline: parallel evidence, specialists, merge."""

import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.agents.fast_research_agent import FastResearchAgent, FastResult
from src.configs.research import Settings
from src.configs.research_prompts import SPECIALIST_TASKS
from src.schemas.errors import ResearchError
from src.services.research import runtime as rt
from src.services.research.backends.demo import DemoBackend
from src.services.research.dossier_service import exact_span, facts_from_dossier
from src.services.research.store import Store
from src.tools.research import research_chat_scope, research_emails

HOME = {
    "id": "src-home",
    "url": "https://acme.example.com/",
    "title": "Acme",
    "kind": "website",
    "retrieved_at": "2026-10-25T00:00:00+00:00",
    "published_at": None,
    "text": "Acme builds OCR for banks. Founder: Lan Tran. Plans start at 1.200.000 VND.",
    "access": "full_text",
    "demo": False,
    "links": ["https://acme.example.com/jobs", "https://acme.example.com/about"],
}


def page(path, text):
    return dict(
        HOME,
        id="src-" + path.strip("/"),
        url="https://acme.example.com" + path,
        text=text,
        links=[],
    )


class FakeBackend:
    mode = "live"

    def __init__(self, delay=0.0):
        self.delay = delay
        self.queries = []
        self.web_search = Mock(side_effect=self._search)
        self.fetch = Mock(side_effect=self._fetch)

    def _search(self, query, deadline, recent=False):
        time.sleep(self.delay)
        self.queries.append((query, recent))
        return [
            {
                "url": "https://news.example.org/" + str(len(self.queries)),
                "title": "Result",
                "snippet": "Acme raised a seed round on 2026-10-20.",
            }
        ]

    def _fetch(self, url, deadline):
        time.sleep(self.delay)
        if url.endswith("/jobs"):
            return page("/jobs", "Open roles: NLP Engineer (full-time).")
        if url.endswith("/about"):
            return page("/about", "Acme was founded in 2022 by Lan Tran in Hanoi.")
        if url.rstrip("/") == "https://acme.example.com":
            return dict(HOME)
        raise ResearchError("FETCH_FAILED", "unreadable")


def cited(
    text, evidence="Acme builds OCR for banks", urls=("https://acme.example.com/",)
):
    return {"text": text, "urls": list(urls), "evidence": evidence}


SECTIONS = {
    "profile": {
        "headline": {
            "name": "Acme",
            "website": "https://acme.example.com/",
            "industry": "OCR",
        },
        "summary": cited("Acme làm OCR cho ngân hàng."),
        "profile": [
            {
                "label": "Trụ sở",
                "value": "Hà Nội",
                "urls": ["https://acme.example.com/about"],
            }
        ],
        "kpis": [
            {"value": "2022", "label": "x", "urls": ["https://acme.example.com/about"]}
        ],
        "gaps": ["Doanh thu"],
    },
    "team": {
        "team": {
            "openings": [
                {
                    "title": "NLP Engineer",
                    "kind": "Full-time",
                    "urls": ["https://acme.example.com/jobs"],
                }
            ],
        },
        "gaps": ["Doanh thu"],
    },
    "founders": {
        "founders": [
            {
                "name": "Lan Tran",
                "achievements": [
                    cited(
                        "Sáng lập Acme.",
                        "founded in 2022 by Lan Tran",
                        ["https://acme.example.com/about"],
                    )
                ],
            }
        ]
    },
    "products": {
        "products": [
            {
                "name": "OCR",
                "price_visibility": "public",
                "urls": ["https://acme.example.com/"],
            }
        ]
    },
    "market": {
        "financials": [
            cited(
                "Gọi vốn hạt giống.",
                "raised a seed round",
                ["https://news.example.org/1"],
            )
        ]
    },
    "signals": {
        "news": [
            {
                "text": "Gọi vốn.",
                "date": "2026-10-20",
                "urls": ["https://news.example.org/1"],
            }
        ],
        "risk_note": "Đã tìm khiếu nại.",
        "meeting_prep": ["Hỏi về giá."],
    },
}


def tool_response(payload, call_id="c"):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(
                type="tool_use", id=call_id, name="submit_section", input=payload
            )
        ],
    )


class FastResearchTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(use_llm=True)
        env = patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fixture"})
        env.start()
        self.addCleanup(env.stop)
        self.company = {
            "name": "Acme",
            "domain": "acme.example.com",
            "website": "https://acme.example.com/",
        }

    def run_fast(self, create, backend=None):
        backend = backend or FakeBackend()
        client = Mock()
        client.messages.create.side_effect = create
        fake = Mock()
        fake.return_value.__enter__ = Mock(return_value=client)
        fake.return_value.__exit__ = Mock(return_value=False)
        with patch("anthropic.Anthropic", fake):
            result = FastResearchAgent(backend, self.settings).run(
                self.company,
                [{"from": "a", "subject": "b", "body": "c"}],
                time.monotonic() + 120,
            )
        return result, backend, client

    @staticmethod
    def by_topic(**kwargs):
        def create(**call):
            topic = next(
                t
                for t, task in SPECIALIST_TASKS.items()
                if call["system"].endswith(task)
            )
            outcome = kwargs.get(topic, SECTIONS[topic])
            if isinstance(outcome, Exception):
                raise outcome
            time.sleep(kwargs.get("_delay", 0))
            return tool_response(outcome)

        return create

    def test_evidence_and_specialists_run_in_parallel_then_merge(self):
        started = time.monotonic()
        result, backend, client = self.run_fast(
            self.by_topic(_delay=0.3), FakeBackend(delay=0.3)
        )
        elapsed = time.monotonic() - started
        # 9 searches + official reads + 6 specialists, serially, would take > 6s.
        self.assertLess(elapsed, 2.5)
        self.assertEqual(len(backend.queries), 9)
        self.assertEqual(client.messages.create.call_count, 6)
        self.assertTrue(any(recent for _, recent in backend.queries))
        # Official site pages were read from the home page's links.
        fetched = {call.args[0] for call in backend.fetch.call_args_list}
        self.assertIn("https://acme.example.com/jobs", fetched)

        dossier = result.dossier
        self.assertEqual(dossier["pipeline"], "fast")
        self.assertEqual(dossier["team"]["openings"][0]["title"], "NLP Engineer")
        self.assertEqual(dossier["founders"][0]["name"], "Lan Tran")
        self.assertEqual(dossier["products"][0]["name"], "OCR")
        self.assertEqual(len(dossier["financials"]), 1)
        self.assertEqual(dossier["gaps"], ["Doanh thu"])  # duplicates removed
        self.assertEqual(dossier["meeting_prep"], ["Hỏi về giá."])
        self.assertEqual([t.id for t in result.topics], list(SPECIALIST_TASKS))
        # Evidence quoted verbatim becomes a knowledge fact; others do not.
        self.assertTrue(result.facts)
        for fact in result.facts:
            source = next(s for s in result.sources if s["id"] in fact["source_ids"])
            self.assertIn(fact["evidence"], source["text"])
        self.assertLess(result.timings["total_seconds"], 2.5)

    def test_specialists_share_the_official_pages(self):
        _, backend, client = self.run_fast(self.by_topic())
        prompts = {
            call.kwargs["system"][-60:]: call.kwargs["messages"][0]["content"]
            for call in client.messages.create.call_args_list
        }
        self.assertEqual(len(prompts), 6)
        # Official pages are shared by every specialist.
        self.assertTrue(all("NLP Engineer" in text for text in prompts.values()))

    def test_failed_specialist_is_reported_not_fatal(self):
        result, _, _ = self.run_fast(self.by_topic(founders=RuntimeError("down")))
        self.assertEqual(result.dossier["founders"], [])
        self.assertTrue(any(e["code"] == "SPECIALIST_FAILED" for e in result.errors))
        self.assertTrue(any("Người sáng lập" in gap for gap in result.dossier["gaps"]))
        self.assertEqual(result.dossier["products"][0]["name"], "OCR")

    def test_all_specialists_failing_returns_none_for_classic_fallback(self):
        result, _, _ = self.run_fast(
            lambda **call: (_ for _ in ()).throw(RuntimeError("down"))
        )
        self.assertIsNone(result)

    def test_invalid_section_is_retried_once_with_the_problems(self):
        calls = {"n": 0}
        good = self.by_topic()

        def create(**call):
            if call["system"].endswith(SPECIALIST_TASKS["market"]):
                calls["n"] += 1
                if calls["n"] == 1:
                    return tool_response({"financials": "not a list"})
            return good(**call)

        result, _, client = self.run_fast(create)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(result.dossier["financials"]), 1)

    def test_disabled_without_key_demo_mode_llm_or_deep_mode(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(FastResearchAgent(FakeBackend(), self.settings).enabled)
        self.assertFalse(
            FastResearchAgent(SimpleNamespace(mode="demo"), self.settings).enabled
        )
        self.assertFalse(
            FastResearchAgent(FakeBackend(), Settings(use_llm=False)).enabled
        )
        self.assertFalse(
            FastResearchAgent(
                FakeBackend(), Settings(use_llm=True, agent_mode="deep")
            ).enabled
        )
        self.assertTrue(FastResearchAgent(FakeBackend(), self.settings).enabled)

    def test_exact_span_and_fact_derivation_need_verified_quotes(self):
        text = "Acme   builds\nOCR for BANKS in Hanoi."
        self.assertEqual(
            exact_span("acme builds ocr for banks", text),
            "Acme   builds\nOCR for BANKS",
        )
        self.assertEqual(exact_span("not there", text), "")
        dossier = {
            "summary": {
                "text": "s",
                "source_ids": ["a"],
                "verified": True,
                "evidence": "acme builds ocr",
            },
            "team": {
                "size": {
                    "text": "t",
                    "source_ids": ["a"],
                    "verified": False,
                    "evidence": "",
                },
                "signals": [],
                "culture": None,
                "hiring_note": None,
            },
            "founders": [],
            "customers_partners": [],
            "financials": [],
        }
        facts = facts_from_dossier(dossier, [{"id": "a", "text": text}])
        self.assertEqual([f["category"] for f in facts], ["overview"])
        self.assertEqual(facts[0]["evidence"], "Acme   builds\nOCR")


class LiveLikeBackend(DemoBackend):
    mode = "live"
    dataset = "synthetic"


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(db_path=self.temp.name + "/r.sqlite3", use_llm=False)
        self.store = Store(self.settings.db_path)
        self.backend = LiveLikeBackend(self.settings, "alice@example.com", self.store)
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

    def fast_result(self):
        dossier = {
            "mode": "agent",
            "pipeline": "fast",
            "headline": {
                "name": "Atlas Labs",
                "tagline": "",
                "status": "",
                "hq": "",
                "industry": "",
                "size_label": "",
                "website": "https://atlas.example.test/",
                "identity_note": "",
            },
            "summary": None,
            "kpis": [],
            "profile": [],
            "team": {
                "size": None,
                "signals": [],
                "culture": None,
                "openings": [],
                "hiring_note": None,
            },
            "founders": [],
            "products": [],
            "customers_partners": [],
            "financials": [],
            "news": [],
            "risks": [],
            "risk_note": "",
            "meeting_prep": ["Hỏi giá."],
            "conflicts": [],
            "gaps": [],
        }
        return FastResult(dossier=dossier, sources=[], facts=[], topics=[], errors=[])

    def test_fast_result_builds_the_report_and_is_reused_while_fresh(self):
        fake = Mock()
        fake.return_value.enabled = True
        fake.return_value.run.return_value = self.fast_result()
        with patch("src.workflows.research_workflow.FastResearchAgent", fake):
            with research_chat_scope(self.request):
                first = research_emails(message_ids=["demo-1"])
            self.assertEqual(fake.return_value.run.call_count, 1)
            report = self.store.get(
                "report", "alice@example.com", first["reports"][0]["id"]
            )
            self.assertEqual(report["dossier"]["meeting_prep"], ["Hỏi giá."])
            self.assertNotIn("dossier", report["research_scope"])
            with research_chat_scope(self.request):
                second = research_emails(message_ids=["demo-1", "demo-4"])
            # A fresh fast-pipeline dossier is reused, not searched again.
            self.assertEqual(fake.return_value.run.call_count, 1)
            reused = self.store.get(
                "report", "alice@example.com", second["reports"][0]["id"]
            )
            self.assertTrue(reused["memory_reused"])
            self.assertEqual(reused["dossier"]["meeting_prep"], ["Hỏi giá."])

    def test_fast_failure_falls_back_to_the_classic_pipeline(self):
        fake = Mock()
        fake.return_value.enabled = True
        fake.return_value.run.return_value = None
        with (
            patch("src.workflows.research_workflow.FastResearchAgent", fake),
            research_chat_scope(self.request),
        ):
            result = research_emails(message_ids=["demo-1"])
        report = self.store.get(
            "report", "alice@example.com", result["reports"][0]["id"]
        )
        self.assertEqual(report["dossier"]["mode"], "facts")
        self.assertTrue(report["sources"])


if __name__ == "__main__":
    unittest.main()
