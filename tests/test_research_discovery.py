"""Offline tests for balanced multi-topic, multi-source company research."""

import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from src.agents.company_info_agent import CompanyInfoAgent
from src.configs.research import Settings
from src.schemas.errors import ResearchError
from src.schemas.extraction import ExtractedFacts
from src.schemas.research import ResearchRequest
from src.services.research.backends.demo import DemoBackend
from src.services.research.discovery import PLAN_VERSION, TOPICS, collect_sources
from src.services.research.store import Store
from src.services.research.utils import utcnow
from src.services.research.web_fetcher import SafeFetcher
from src.workflows.research_workflow import ResearchWorkflow

COMPANY = {
    "name": "Atlas Labs",
    "domain": "atlas.example.com",
    "website": "https://atlas.example.com/",
}


def source(url, index=0, kind="website"):
    return {
        "id": "source-" + str(index),
        "url": url,
        "title": "Atlas Labs",
        "text": "Atlas Labs has a publicly documented company statement number "
        + str(index),
        "kind": kind,
        "retrieved_at": utcnow(),
        "published_at": None,
        "demo": False,
        "links": [],
    }


def broad_backend(same_domain=False):
    backend = Mock(mode="live")
    ids = {query: topic for topic, _, query in TOPICS}

    def search(name, query, deadline):
        topic = ids[query]
        return [
            {
                "url": f"https://{'publisher' if same_domain else topic + str(index)}.example.com/{topic}/{index}",
                "snippet": "Discovery preview only",
            }
            for index in range(5)
        ]

    backend.search.side_effect = search
    backend.fetch.side_effect = lambda url, deadline: source(url, url)
    return backend


class TopicResearchTests(unittest.TestCase):
    def test_every_topic_gets_a_page_before_the_first_topic_gets_all_pages(self):
        backend = broad_backend()
        pages, errors, coverage = collect_sources(
            backend, COMPANY, time.monotonic() + 10, 8
        )
        self.assertEqual(backend.search.call_count, 8)
        self.assertEqual(len(pages), 8)
        self.assertEqual(errors, [])
        self.assertTrue(all(item["read_sources"] for item in coverage))
        self.assertEqual(
            {topic for page in pages for topic in page["research_topics"]},
            {topic for topic, _, _ in TOPICS},
        )
        self.assertGreater(len({page["url"].split("/")[2] for page in pages}), 1)

    def test_domain_diversity_prevents_one_publisher_from_taking_the_whole_budget(self):
        backend = broad_backend(same_domain=True)
        pages, _errors, coverage = collect_sources(
            backend, COMPANY, time.monotonic() + 10, 16
        )
        self.assertEqual(len(pages), 2)
        self.assertEqual(backend.fetch.call_count, 2)
        self.assertEqual(backend.search.call_count, 8)
        self.assertTrue(
            any(item["status"] == "no_readable_sources" for item in coverage)
        )

    def test_tracking_and_www_variants_are_fetched_once_and_keep_all_topics(self):
        backend = Mock()
        backend.search.side_effect = lambda *args: [
            {"url": "https://www.publisher.example.com/story/?utm_source=test"},
            {"url": "https://publisher.example.com/story"},
        ]
        backend.fetch.return_value = source("https://publisher.example.com/story")
        pages, _, coverage = collect_sources(
            backend, COMPANY, time.monotonic() + 10, 16
        )
        backend.fetch.assert_called_once()
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0]["research_topics"]), 8)
        self.assertTrue(all(item["status"] == "read" for item in coverage))

    def test_missing_configuration_stops_remaining_queries_and_declares_each_gap(self):
        backend = Mock()
        backend.search.side_effect = ResearchError(
            "SEARCH_NOT_CONFIGURED", "Missing key"
        )
        pages, errors, coverage = collect_sources(
            backend, COMPANY, time.monotonic() + 10, 16
        )
        backend.search.assert_called_once()
        backend.fetch.assert_not_called()
        self.assertEqual(pages, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(coverage), 8)
        self.assertTrue(
            all(item.get("reason") == "SEARCH_NOT_CONFIGURED" for item in coverage)
        )

    def test_zero_page_budget_and_expired_deadline_never_spend_search_calls(self):
        for budget, deadline in [
            (0, time.monotonic() + 10),
            (10, time.monotonic() - 1),
        ]:
            backend = Mock()
            pages, _, coverage = collect_sources(backend, COMPANY, deadline, budget)
            backend.search.assert_not_called()
            backend.fetch.assert_not_called()
            self.assertEqual(pages, [])
            self.assertTrue(all(item["status"] == "not_searched" for item in coverage))

    def test_failed_reads_consume_the_page_budget_and_snippets_are_not_evidence(self):
        backend = broad_backend()
        backend.fetch.side_effect = ResearchError("FETCH_FAILED", "Blocked page")
        pages, errors, coverage = collect_sources(
            backend, COMPANY, time.monotonic() + 10, 4
        )
        self.assertEqual(backend.fetch.call_count, 4)
        self.assertEqual(pages, [])
        self.assertEqual(len(errors), 4)
        self.assertTrue(all(error["access"] == "snippet_only" for error in errors))
        self.assertTrue(all(not item["read_sources"] for item in coverage))

    def test_one_provider_query_failure_preserves_other_topics(self):
        backend = broad_backend()
        original = backend.search.side_effect

        def search(name, query, deadline):
            if query == TOPICS[0][2]:
                raise ResearchError("SEARCH_UNAVAILABLE", "Temporary failure")
            return original(name, query, deadline)

        backend.search.side_effect = search
        pages, errors, coverage = collect_sources(
            backend, COMPANY, time.monotonic() + 10, 16
        )
        self.assertTrue(pages)
        self.assertEqual(coverage[0]["status"], "search_failed")
        self.assertTrue(coverage[-1]["read_sources"])
        self.assertEqual(len(errors), 1)

    def test_official_and_social_sources_keep_their_identity(self):
        backend = Mock()
        backend.search.side_effect = lambda *args: [
            {"url": "https://atlas.example.com/products"},
            {"url": "https://uk.linkedin.com/company/atlas-labs"},
        ]
        backend.fetch.side_effect = lambda url, deadline: source(url, url)
        pages, _, _ = collect_sources(backend, COMPANY, time.monotonic() + 10, 16)
        self.assertEqual({page["kind"] for page in pages}, {"official", "social"})

    def test_bounded_research_configuration_rejects_excessive_queries(self):
        with (
            patch.dict("os.environ", {"RESEARCH_MAX_SEARCH_QUERIES": "31"}),
            self.assertRaises(ValueError),
        ):
            Settings.from_env()


class ExtractionAndCrawlTests(unittest.TestCase):
    def test_pages_after_the_tenth_are_analyzed_and_late_risk_evidence_is_retained(
        self,
    ):
        pages = [source(f"https://publisher{i}.example.com/page", i) for i in range(19)]
        backend = Mock(mode="live")
        reasoner = Mock()

        def extract(company, batch, deadline):
            return ExtractedFacts.model_validate(
                {
                    "facts": [
                        {
                            "category": "risk"
                            if page["id"] == "source-18"
                            else "overview",
                            "source_id": page["id"],
                            "evidence": page["text"],
                        }
                        for page in batch
                    ]
                }
            )

        reasoner.facts.side_effect = extract
        facts, errors = CompanyInfoAgent(backend, reasoner, Settings()).extract(
            dict(COMPANY), pages, time.monotonic() + 10
        )
        self.assertEqual(len(facts), 19)
        self.assertEqual(errors, [])
        self.assertEqual(
            [len(call.args[1]) for call in reasoner.facts.call_args_list], [6, 6, 6, 1]
        )
        self.assertTrue(any(fact["category"] == "risk" for fact in facts))

    def test_one_model_batch_failure_preserves_evidence_from_later_pages(self):
        pages = [source(f"https://publisher{i}.example.com/page", i) for i in range(7)]
        reasoner = Mock()
        reasoner.facts.side_effect = [
            ResearchError("MODEL_UNAVAILABLE", "Temporary failure"),
            ExtractedFacts.model_validate(
                {
                    "facts": [
                        {
                            "category": "financials",
                            "source_id": pages[-1]["id"],
                            "evidence": pages[-1]["text"],
                        }
                    ]
                }
            ),
        ]
        facts, errors = CompanyInfoAgent(
            Mock(mode="live"), reasoner, Settings()
        ).extract(dict(COMPANY), pages, time.monotonic() + 10)
        self.assertEqual(facts[0]["source_ids"], [pages[-1]["id"]])
        self.assertEqual(facts[0]["category"], "financials")
        self.assertEqual(errors[0]["code"], "MODEL_UNAVAILABLE")

    def test_website_crawl_does_not_use_all_slots_on_product_navigation(self):
        fetcher = SafeFetcher()
        home = source(COMPANY["website"], 0)
        home["links"] = [
            f"https://atlas.example.com/products/{i}" for i in range(10)
        ] + [
            "https://atlas.example.com/team",
            "https://atlas.example.com/investors",
            "https://atlas.example.com/contact",
        ]
        fetcher.fetch = Mock(
            side_effect=lambda url, *args, **kwargs: (
                home if url == home["url"] else source(url, url)
            )
        )
        pages, _ = fetcher.crawl(COMPANY["website"], 5, time.monotonic() + 10)
        paths = {page["url"] for page in pages}
        self.assertIn("https://atlas.example.com/team", paths)
        self.assertIn("https://atlas.example.com/investors", paths)
        self.assertIn("https://atlas.example.com/contact", paths)
        self.assertEqual(len(pages), 5)


class ResearchScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings(db_path=temp.name + "/test.sqlite3", use_llm=False)
        self.store = Store(self.settings.db_path)
        self.backend = DemoBackend(self.settings, "alice", self.store)

    def run_batch(self, backend=None):
        payload = ResearchRequest(message_ids=["demo-1"]).model_dump(mode="json")
        batch, _ = self.store.create_batch(
            "alice", str(time.monotonic_ns()), payload, "demo"
        )
        ResearchWorkflow(self.store, self.settings).execute(
            "alice", batch["id"], payload, backend or self.backend
        )
        batch = self.store.get("batch", "alice", batch["id"])
        run = self.store.get("run", "alice", batch["runs"][0]["id"])
        return self.store.get("report", "alice", run["report_id"])

    def test_report_persists_topic_coverage_and_all_read_sources(self):
        report = self.run_batch()
        scope = report["research_scope"]
        self.assertEqual(scope["plan_version"], PLAN_VERSION)
        self.assertEqual(len(scope["topics"]), 8)
        self.assertTrue(all(item["searched"] for item in scope["topics"]))
        self.assertEqual(scope["page_limit"], 24)
        company = self.store.get_company("alice", report["company"]["id"])
        stored = dict(company["research_scope"])
        # The dossier lives in its own report field, not in the scope summary.
        self.assertEqual(
            stored.pop("dossier")["headline"]["name"],
            report["dossier"]["headline"]["name"],
        )
        self.assertEqual(stored, scope)

    def test_legacy_narrow_live_cache_requires_a_new_broad_research(self):
        first = self.run_batch()
        record = self.store.get_company("alice", first["company"]["id"])
        self.store.save_company(
            "alice",
            record["domain"],
            record["name"],
            record["facts"],
            record["sources"],
            research_scope={},
        )
        self.backend.mode = "live"
        self.backend.search = Mock(wraps=self.backend.search)
        second = self.run_batch()
        self.assertFalse(second["memory_reused"])
        self.assertEqual(self.backend.search.call_count, 8)
        self.assertEqual(second["research_scope"]["plan_version"], PLAN_VERSION)
        self.assertEqual(
            self.store.get("report", "alice", first["id"])["research_scope"],
            first["research_scope"],
        )

    def test_full_scope_live_cache_is_reused_without_new_searches(self):
        self.run_batch()
        self.backend.mode = "live"
        self.backend.search = Mock(wraps=self.backend.search)
        second = self.run_batch()
        self.assertTrue(second["memory_reused"])
        self.backend.search.assert_not_called()
