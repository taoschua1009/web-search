"""Offline acceptance tests for multi-company research and guarded mail writes."""

import base64
import json
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from pydantic import ValidationError

from src.agents.approval_agent import HumanApprovalAgent
from src.agents.calendar_agent import CalendarAgent
from src.agents.company_info_agent import CompanyInfoAgent
from src.agents.email_agent import EmailAgent
from src.agents.memory_agent import MemoryAgent
from src.agents.report_agent import ReportGenerationAgent
from src.api import server
from src.configs.research import Settings
from src.schemas.errors import ResearchError
from src.schemas.extraction import ExtractedFacts
from src.schemas.research import Decision, Meeting, ResearchRequest, SendPayload
from src.services import calendar_service, gmail_service, web_auth
from src.services.research import runtime as rt
from src.services.research.backends.demo import DemoBackend
from src.services.research.backends.live import (
    CALENDAR_EVENTS,
    CALENDAR_READ,
    GMAIL_MODIFY,
    GMAIL_READ,
    GMAIL_SEND,
    LiveBackend,
)
from src.services.research.store import Store
from src.services.research.utils import next_week, utcnow
from src.services.research.web_fetcher import (
    SafeFetcher,
    normalize_url,
    public_addresses,
)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(db_path=self.directory.name + "/research.sqlite3")
        self.store = Store(self.settings.db_path)

    def proposal(self, owner="alice", kind="send_email"):
        return self.store.propose(
            owner,
            kind,
            {
                "to": ["partner@example.com"],
                "subject": "Review",
                "body": "Reviewed content",
            },
            {"mode": "demo", "calendar_slots": []},
            1800,
        )

    @staticmethod
    def decision(action, value="approve"):
        return Decision(
            decision=value,
            version=action["version"],
            payload_hash=action["payload_hash"],
        )


class ApprovalTests(StoreCase):
    def test_concurrent_decisions_execute_once_and_replay_returns_same_result(self):
        backend = DemoBackend(self.settings)
        backend.send = Mock(return_value={"status": "simulated_send"})
        action = self.proposal()
        gate = HumanApprovalAgent(self.store, backend, self.settings)
        barrier = threading.Barrier(8)

        def approve():
            barrier.wait()
            return gate.decide("alice", action["id"], self.decision(action))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: approve(), range(8)))
        backend.send.assert_called_once()
        self.assertTrue(all(result["state"] == "approved" for result in results))
        self.assertEqual(
            gate.decide("alice", action["id"], self.decision(action))["execution"],
            "succeeded",
        )
        backend.send.assert_called_once()

    def test_rejection_never_checks_scopes_or_calls_provider(self):
        backend = Mock(mode="demo")
        action = self.proposal()
        result = HumanApprovalAgent(self.store, backend, self.settings).decide(
            "alice", action["id"], self.decision(action, "reject")
        )
        self.assertEqual(result["state"], "rejected")
        backend.check_write.assert_not_called()
        backend.send.assert_not_called()
        backend.trash.assert_not_called()

    def test_cross_owner_cannot_read_or_approve_action(self):
        action = self.proposal()
        backend = Mock(mode="demo")
        with self.assertRaises(ResearchError) as caught:
            HumanApprovalAgent(self.store, backend, self.settings).decide(
                "bob", action["id"], self.decision(action)
            )
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        backend.send.assert_not_called()

    def test_edit_invalidates_old_approval_and_sends_exact_new_content(self):
        action = self.proposal()
        updated = self.store.edit_action(
            "alice", action["id"], 1, {"body": "New exact content"}
        )
        self.assertEqual(updated["version"], 2)
        self.assertNotEqual(action["payload_hash"], updated["payload_hash"])
        backend = Mock(mode="demo")
        backend.send.return_value = {"status": "accepted"}
        gate = HumanApprovalAgent(self.store, backend, self.settings)
        with self.assertRaises(ResearchError) as caught:
            gate.decide("alice", action["id"], self.decision(action))
        self.assertEqual(caught.exception.code, "ACTION_CHANGED")
        backend.check_write.assert_not_called()
        gate.decide("alice", updated["id"], self.decision(updated))
        self.assertEqual(backend.send.call_args.args[0]["body"], "New exact content")

    def test_expired_and_forged_approvals_never_send(self):
        action = self.store.propose(
            "alice", "send_email", {"body": "x"}, {"mode": "demo"}, -1
        )
        backend = Mock(mode="demo")
        gate = HumanApprovalAgent(self.store, backend, self.settings)
        with self.assertRaises(ResearchError) as caught:
            gate.decide("alice", action["id"], self.decision(action))
        self.assertEqual(caught.exception.code, "APPROVAL_EXPIRED")
        other = self.proposal()
        with self.assertRaises(ResearchError):
            gate.decide(
                "alice",
                other["id"],
                Decision(decision="approve", version=1, payload_hash="0" * 64),
            )
        backend.send.assert_not_called()

    def test_unknown_send_outcome_survives_restart_without_replay(self):
        action = self.proposal()
        backend = Mock(mode="demo")
        backend.send.side_effect = TimeoutError("private provider details")
        result = HumanApprovalAgent(self.store, backend, self.settings).decide(
            "alice", action["id"], self.decision(action)
        )
        self.assertEqual(result["execution"], "outcome_unknown")
        self.assertNotIn("private provider details", json.dumps(result))
        restarted = Store(self.settings.db_path)
        HumanApprovalAgent(restarted, backend, self.settings).decide(
            "alice", action["id"], self.decision(action)
        )
        backend.send.assert_called_once()

    def test_crash_after_claim_is_unknown_after_restart(self):
        action = self.proposal()
        self.store.claim("alice", action["id"], "approve", 1, action["payload_hash"])
        restarted = Store(self.settings.db_path)
        self.assertEqual(
            restarted.action("alice", action["id"])["execution"], "outcome_unknown"
        )
        backend = Mock(mode="demo")
        HumanApprovalAgent(restarted, backend, self.settings).decide(
            "alice", action["id"], self.decision(action)
        )
        backend.send.assert_not_called()

    def test_changed_calendar_invalidates_send_before_any_write(self):
        tomorrow = datetime.now(UTC) + timedelta(days=2)
        slot = {
            "start": tomorrow.isoformat(),
            "end": (tomorrow + timedelta(minutes=30)).isoformat(),
        }
        action = self.store.propose(
            "alice",
            "send_email",
            {"body": "meeting"},
            {"mode": "demo", "calendar_slots": [slot], "timezone": "UTC"},
            1800,
        )
        backend = Mock(mode="demo")
        backend.busy.return_value = [(tomorrow, tomorrow + timedelta(hours=1))]
        result = HumanApprovalAgent(self.store, backend, self.settings).decide(
            "alice", action["id"], self.decision(action)
        )
        self.assertEqual(result["error"]["code"], "ACTION_CHANGED")
        self.assertEqual(result["execution"], "failed")
        backend.send.assert_not_called()

    def test_duplicate_proposals_share_one_review_record(self):
        first, second = self.proposal(), self.proposal()
        self.assertEqual(first["id"], second["id"])

    def test_read_only_scope_cannot_enable_write(self):
        credentials = Credentials(token="fixture", scopes=[GMAIL_READ])
        backend = LiveBackend(credentials, self.settings)
        for kind in ["send_email", "trash_email"]:
            with self.subTest(kind=kind), self.assertRaises(ResearchError) as caught:
                backend.check_write(kind)
            self.assertEqual(caught.exception.code, "SCOPE_REQUIRED")

    def test_same_action_cannot_be_approved_after_rejection(self):
        action = self.proposal()
        backend = Mock(mode="demo")
        gate = HumanApprovalAgent(self.store, backend, self.settings)
        gate.decide("alice", action["id"], self.decision(action, "reject"))
        with self.assertRaises(ResearchError):
            gate.decide("alice", action["id"], self.decision(action))
        backend.send.assert_not_called()


class ResearchApiTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.runtime_patch = patch.object(
            rt, "runtime", return_value=(self.settings, self.store)
        )
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.client = TestClient(
            server.app, base_url="http://localhost:9004", follow_redirects=False
        )
        self.addCleanup(self.client.close)
        self.client.post("/auth/demo?dataset=synthetic")
        self.headers = {"X-Research-Request": "1"}

    def research(self, ids=None, key=None):
        payload = {"message_ids": ids or ["demo-1", "demo-2", "demo-3", "demo-4"]}
        headers = dict(self.headers)
        if key:
            headers["Idempotency-Key"] = key
        result = self.client.post("/api/research", json=payload, headers=headers)
        self.assertEqual(result.status_code, 202, result.text)
        return self.client.get("/api/research-batches/" + result.json()["id"]).json()

    def report(self, batch, index=0):
        result = self.client.get("/api/reports/" + batch["runs"][index]["report_id"])
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_demo_three_companies_four_emails_and_no_live_provider(self):
        with patch.object(
            rt, "LiveBackend", side_effect=AssertionError("Live must not be called")
        ):
            batch = self.research()
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(len(batch["runs"]), 3)
        reports = [self.report(batch, index) for index in range(3)]
        self.assertEqual(
            sorted(len(report["messages"]) for report in reports), [1, 1, 2]
        )
        for report in reports:
            self.assertEqual(report["mode"], "demo")
            self.assertEqual(len(report["roles"]), 7)
            self.assertTrue(all(source["demo"] for source in report["sources"]))
            self.assertTrue(
                all(fact["provenance"] == "demo" for fact in report["facts"])
            )
            self.assertTrue(report["people"])
            for source in report["sources"]:
                self.assertIn(report["company"]["name"], source["text"])
        statuses = [
            meeting["status"] for report in reports for meeting in report["meetings"]
        ]
        for value in ["conflict", "already_scheduled", "available"]:
            self.assertIn(value, statuses)

    def test_every_fact_has_resolving_literal_evidence_and_measured_quality(self):
        report = self.report(self.research(["demo-1"]))
        sources = {source["id"]: source for source in report["sources"]}
        for fact in report["facts"]:
            for source_id in fact["source_ids"]:
                self.assertIn(fact["evidence"], sources[source_id]["text"])
        quality = report["quality"]
        self.assertEqual(quality["coverage_percent"], 100)
        self.assertEqual(quality["unique_sources"], 3)
        self.assertNotEqual(quality["coverage_percent"], 98)
        self.assertEqual(len(quality["recent_news_sources"]), 2)
        self.assertEqual(set(quality["covered"]), set(quality["required"]))

    def test_report_and_company_history_are_owner_scoped(self):
        report = self.report(self.research(["demo-1"]))
        with TestClient(
            server.app, base_url="http://localhost:9004", follow_redirects=False
        ) as other:
            other.post("/auth/demo")
            for path in [
                "/api/reports/" + report["id"],
                "/reports/" + report["id"],
                "/api/companies/" + report["company"]["id"],
                "/api/companies/" + report["company"]["id"] + "/knowledge/history",
            ]:
                self.assertEqual(other.get(path).status_code, 404, path)
            edited = other.patch(
                "/api/companies/" + report["company"]["id"] + "/knowledge",
                json={"expected_version": 1, "category": "overview", "text": "attack"},
                headers=self.headers,
            )
            self.assertEqual(edited.status_code, 404)

    def test_knowledge_edit_keeps_original_report_immutable_and_reuses_new_version(
        self,
    ):
        report = self.report(self.research(["demo-1"]))
        company_id = report["company"]["id"]
        edit = {
            "expected_version": 1,
            "category": "overview",
            "text": "Tôi bổ sung thông tin để chuẩn bị gặp khách hàng.",
        }
        updated = self.client.patch(
            "/api/companies/" + company_id + "/knowledge",
            json=edit,
            headers=self.headers,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["version"], 2)
        stale = self.client.patch(
            "/api/companies/" + company_id + "/knowledge",
            json=edit,
            headers=self.headers,
        )
        self.assertEqual(stale.status_code, 409)
        old = self.client.get("/api/reports/" + report["id"]).json()
        self.assertEqual(old, report)
        second = self.report(self.research(["demo-1"]))
        self.assertTrue(second["memory_reused"])
        self.assertTrue(any(fact["provenance"] == "user" for fact in second["facts"]))
        self.assertEqual(second["quality"]["coverage_percent"], 100)
        history = self.client.get(
            "/api/companies/" + company_id + "/knowledge/history"
        ).json()["revisions"]
        self.assertEqual([item["version"] for item in history], [2, 1])

    def test_send_requires_preview_and_approval_then_replays_without_resend(self):
        report = self.report(self.research(["demo-1"]))
        draft = next(
            meeting["draft"] for meeting in report["meetings"] if meeting["draft"]
        )
        request = {
            "run_id": report["run_id"],
            "message_id": "demo-1",
            "kind": "send_email",
            "send": draft,
        }
        proposal = self.client.post(
            "/api/actions", json=request, headers=self.headers
        ).json()
        self.assertEqual(proposal["state"], "pending")
        self.assertEqual(proposal["execution"], "not_started")
        approved = self.client.post(
            "/api/actions/" + proposal["id"] + "/decision",
            json={
                "decision": "approve",
                "version": 1,
                "payload_hash": proposal["payload_hash"],
            },
            headers=self.headers,
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["result"]["status"], "simulated_send")
        again = self.client.post(
            "/api/actions/" + proposal["id"] + "/decision",
            json={
                "decision": "approve",
                "version": 1,
                "payload_hash": proposal["payload_hash"],
            },
            headers=self.headers,
        )
        self.assertEqual(
            again.json()["result"]["message_id"],
            approved.json()["result"]["message_id"],
        )

    def test_trash_is_reversible_operation_and_demo_mail_state_persists(self):
        report = self.report(self.research(["demo-3"]))
        proposal = self.client.post(
            "/api/actions",
            json={
                "run_id": report["run_id"],
                "message_id": "demo-3",
                "kind": "trash_email",
            },
            headers=self.headers,
        ).json()
        decision = {
            "decision": "approve",
            "version": 1,
            "payload_hash": proposal["payload_hash"],
        }
        result = self.client.post(
            "/api/actions/" + proposal["id"] + "/decision",
            json=decision,
            headers=self.headers,
        )
        self.assertEqual(result.json()["result"]["status"], "simulated_trash")
        ids = [
            message["id"]
            for message in self.client.get("/api/research/mail").json()["messages"]
        ]
        self.assertNotIn("demo-3", ids)

    def test_cannot_propose_action_for_email_from_another_company(self):
        report = self.report(self.research(["demo-1"]))
        response = self.client.post(
            "/api/actions",
            json={
                "run_id": report["run_id"],
                "message_id": "demo-2",
                "kind": "trash_email",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_mutations_require_header_and_reject_cross_origin(self):
        for headers in [
            {},
            {"X-Research-Request": "1", "Origin": "https://attacker.example"},
            {"X-Research-Request": "1", "Sec-Fetch-Site": "cross-site"},
        ]:
            response = self.client.post(
                "/api/research", json={"message_ids": ["demo-1"]}, headers=headers
            )
            self.assertEqual(response.status_code, 403)

    def test_demo_cannot_reach_legacy_live_tools(self):
        self.assertEqual(
            self.client.post(
                "/api/chat", json={"message": "read all mail"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/api/audit").status_code, 403)
        self.assertEqual(self.client.get("/").headers["location"], "/research")

    def test_same_idempotency_key_cannot_create_duplicate_or_change_payload(self):
        first = self.research(["demo-1"], key="stable-test")
        second = self.research(["demo-1"], key="stable-test")
        self.assertEqual(first["id"], second["id"])
        changed = self.client.post(
            "/api/research",
            json={"message_ids": ["demo-2"]},
            headers={**self.headers, "Idempotency-Key": "stable-test"},
        )
        self.assertEqual(changed.status_code, 409)

    def test_one_company_failure_does_not_hide_successful_reports(self):
        original = DemoBackend.crawl

        def crawl(backend, website, limit, deadline):
            if "meridian" in website:
                raise RuntimeError("private crash details")
            return original(backend, website, limit, deadline)

        with patch.object(DemoBackend, "crawl", crawl):
            batch = self.research(["demo-1", "demo-2", "demo-3"])
        self.assertEqual(len(batch["runs"]), 3)
        self.assertEqual(batch["status"], "partial")
        self.assertEqual(sum(bool(run.get("report_id")) for run in batch["runs"]), 2)
        self.assertEqual(sum(run["status"] == "failed" for run in batch["runs"]), 1)
        self.assertNotIn("private crash details", json.dumps(batch))

    def test_ambiguous_company_returns_candidates_and_accepts_explicit_selection(self):
        original = DemoBackend.read_mail

        def ambiguous(backend, message_id):
            message = original(backend, message_id)
            message.update(
                {
                    "from": "Customer <customer@gmail.com>",
                    "body": "Companies https://one.example.test/ and https://two.example.test/",
                    "company_name": "",
                }
            )
            return message

        with patch.object(DemoBackend, "read_mail", ambiguous):
            batch = self.research(["demo-1"])
            self.assertEqual(batch["status"], "needs_input")
            self.assertEqual(len(batch["unresolved"][0]["candidates"]), 2)
            response = self.client.post(
                "/api/research",
                json={
                    "message_ids": ["demo-1"],
                    "selections": [
                        {
                            "message_id": "demo-1",
                            "name": "One Example",
                            "website": "https://one.example.test/",
                        }
                    ],
                },
                headers=self.headers,
            )
            resolved = self.client.get(
                "/api/research-batches/" + response.json()["id"]
            ).json()
            self.assertEqual(
                resolved["runs"][0]["company"]["domain"], "one.example.test"
            )

    def test_report_export_preserves_citations_and_demo_label(self):
        report = self.report(self.research(["demo-1"]))
        response = self.client.get("/api/reports/" + report["id"] + "/export.md")
        self.assertEqual(response.status_code, 200)
        self.assertIn("DEMO", response.text)
        for source in report["sources"]:
            self.assertIn(source["id"], response.text)
            self.assertIn(source["url"], response.text)


class CalendarTests(unittest.TestCase):
    def test_next_week_is_next_monday_through_following_monday(self):
        for now, expected in [
            (datetime(2026, 10, 25, 13, tzinfo=UTC), "2026-10-26"),
            (datetime(2026, 12, 31, tzinfo=UTC), "2027-01-04"),
        ]:
            start, end = next_week(now, "Asia/Ho_Chi_Minh")
            self.assertEqual(start.date().isoformat(), expected)
            self.assertEqual(start.weekday(), 0)
            self.assertEqual((end - start).days, 7)

    def test_overlapping_and_all_day_busy_intervals_are_not_free(self):
        start = datetime(2026, 10, 26, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
        busy = [
            (start, start + timedelta(days=1)),
            (start + timedelta(days=1, hours=9), start + timedelta(days=1, hours=11)),
            (start + timedelta(days=1, hours=10), start + timedelta(days=1, hours=12)),
        ]
        windows = CalendarAgent.free_windows(
            start, start + timedelta(days=2), busy, 9, 18
        )
        self.assertEqual(
            windows,
            [
                (
                    start + timedelta(days=1, hours=12),
                    start + timedelta(days=1, hours=18),
                )
            ],
        )

    def test_calendar_failure_is_unknown_not_free(self):
        backend = Mock()
        backend.busy.side_effect = ResearchError("PROVIDER_UNAVAILABLE", "cannot read")
        snapshot = CalendarAgent(backend, Settings()).snapshot(
            datetime.now(UTC), "UTC", 30
        )
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIsNone(snapshot["busy_count"])
        self.assertFalse(snapshot["free_windows"])

    def test_same_time_without_matching_participant_is_not_existing_meeting(self):
        start = datetime.now(UTC) + timedelta(days=2)
        end = start + timedelta(minutes=30)
        backend = Mock()
        backend.events.return_value = [
            {
                "id": "x",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                "attendees": [{"email": "someoneelse@example.com"}],
            }
        ]
        backend.busy.return_value = [(start, end)]
        value = CalendarAgent(backend, Settings()).assess(
            {"id": "x", "from": "partner@example.com", "subject": "Meeting"},
            Meeting(start=start, end=end),
            {"status": "checked", "free_windows": []},
            "UTC",
        )
        self.assertEqual(value["status"], "conflict")

    def test_web_calendar_never_falls_back_to_local_token(self):
        credentials = Credentials(token="fake", scopes=[GMAIL_READ])
        token = gmail_service.request_credentials.set(credentials)
        try:
            with patch.object(calendar_service, "build") as build:
                with self.assertRaises(PermissionError):
                    calendar_service._get_service()
                build.assert_not_called()
        finally:
            gmail_service.request_credentials.reset(token)

    def test_freebusy_provider_errors_are_not_empty_busy_list(self):
        backend = LiveBackend(
            Credentials(token="fake", scopes=[CALENDAR_READ]), Settings()
        )
        service = Mock()
        service.freebusy.return_value.query.return_value.execute.return_value = {
            "calendars": {"primary": {"errors": [{"reason": "notFound"}]}}
        }
        with (
            patch.object(backend, "service", return_value=service),
            self.assertRaises(ResearchError),
        ):
            backend.busy(
                datetime.now(UTC), datetime.now(UTC) + timedelta(days=1), "UTC"
            )


class WebSafetyTests(unittest.TestCase):
    def test_unsafe_urls_and_tracking_normalization(self):
        for url in [
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://169.254.169.254/",
            "http://10.0.0.1/",
            "https://localhost/",
            "https://a.local/",
            "https://user:password@example.com/",
            "https://example.com:8443/",
            "javascript:alert(1)",
        ]:
            with self.subTest(url=url), self.assertRaises(ResearchError):
                normalize_url(url)
        self.assertEqual(
            normalize_url("https://EXAMPLE.com/about?utm_source=x&a=1#top"),
            "https://example.com/about?a=1",
        )

    def test_mixed_public_private_dns_answers_are_blocked(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
        ]
        with (
            patch(
                "src.services.research.web_fetcher.socket.getaddrinfo",
                return_value=addresses,
            ),
            self.assertRaises(ResearchError) as caught,
        ):
            public_addresses("example.com", 443)
        self.assertEqual(caught.exception.code, "URL_BLOCKED")

    def test_private_redirect_and_cross_host_redirect_are_blocked(self):
        fetcher = SafeFetcher()
        with (
            patch.object(
                fetcher,
                "_request",
                return_value=(302, {"location": "http://127.0.0.1/"}, b""),
            ) as request,
            self.assertRaises(ResearchError),
        ):
            fetcher.fetch("https://example.com", time.monotonic() + 10)
        request.assert_called_once()
        with (
            patch.object(
                fetcher,
                "_request",
                return_value=(302, {"location": "https://other.example/"}, b""),
            ),
            self.assertRaises(ResearchError),
        ):
            fetcher.fetch(
                "https://example.com",
                time.monotonic() + 10,
                expected_domain="example.com",
            )

    def test_transport_pins_public_ip_and_verifies_tls_hostname(self):
        response = Mock(status=200)
        response.getheaders.return_value = [("Content-Type", "text/plain")]
        response.read.side_effect = [
            b"This is a public company page with enough information to parse.",
            b"",
        ]
        connection = Mock()
        connection.getresponse.return_value = response
        with (
            patch(
                "src.services.research.web_fetcher.public_addresses",
                return_value=["93.184.216.34"],
            ),
            patch(
                "src.services.research.web_fetcher.socket.create_connection"
            ) as connect,
            patch(
                "src.services.research.web_fetcher.ssl.create_default_context"
            ) as tls,
            patch(
                "src.services.research.web_fetcher.http.client.HTTPConnection",
                return_value=connection,
            ),
        ):
            SafeFetcher().fetch("https://example.com/", time.monotonic() + 10)
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        self.assertEqual(
            tls.return_value.wrap_socket.call_args.kwargs["server_hostname"],
            "example.com",
        )

    def test_oversize_response_is_rejected(self):
        response = Mock(status=200)
        response.getheaders.return_value = [
            ("Content-Type", "text/plain"),
            ("Content-Length", "99999999"),
        ]
        connection = Mock()
        connection.getresponse.return_value = response
        with (
            patch(
                "src.services.research.web_fetcher.public_addresses",
                return_value=["93.184.216.34"],
            ),
            patch("src.services.research.web_fetcher.socket.create_connection"),
            patch("src.services.research.web_fetcher.ssl.create_default_context"),
            patch(
                "src.services.research.web_fetcher.http.client.HTTPConnection",
                return_value=connection,
            ),
            self.assertRaises(ResearchError) as caught,
        ):
            SafeFetcher(max_bytes=100).fetch(
                "https://example.com/", time.monotonic() + 10
            )
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")


class EvidenceTests(StoreCase):
    def test_model_fabricated_quote_and_wrong_entity_are_rejected(self):
        source = {
            "id": "s1",
            "url": "https://news.example.com/",
            "title": "Other",
            "text": "Other Company has a reported dispute.",
            "kind": "news",
            "retrieved_at": utcnow(),
            "access": "full_text",
            "demo": False,
        }
        reasoner = Mock()
        reasoner.facts.return_value = ExtractedFacts.model_validate(
            {
                "facts": [
                    {
                        "category": "risk",
                        "source_id": "s1",
                        "evidence": "Other Company has a reported dispute.",
                        "status": "allegation",
                    }
                ]
            }
        )
        backend = Mock(mode="live")
        facts, errors = CompanyInfoAgent(backend, reasoner, self.settings).extract(
            {"name": "Target Company", "domain": "target.example.com"},
            [source],
            time.monotonic() + 10,
        )
        self.assertFalse(any(fact["category"] == "risk" for fact in facts))
        self.assertIn("ENTITY_UNCONFIRMED", [error["code"] for error in errors])
        source.update(kind="official", text="Target Company provides useful services.")
        reasoner.facts.return_value = ExtractedFacts.model_validate(
            {
                "facts": [
                    {
                        "category": "scale",
                        "source_id": "s1",
                        "evidence": "The company has 1000 employees.",
                    }
                ]
            }
        )
        facts, errors = CompanyInfoAgent(backend, reasoner, self.settings).extract(
            {"name": "Target Company", "domain": "target.example.com"},
            [source],
            time.monotonic() + 10,
        )
        self.assertFalse(any(fact["category"] == "scale" for fact in facts))
        self.assertIn("EVIDENCE_REJECTED", [error["code"] for error in errors])

    def test_stale_or_missing_company_fields_require_refresh(self):
        backend = DemoBackend(self.settings)
        sources, _ = backend.crawl(
            "https://atlas.example.test/", 10, time.monotonic() + 10
        )
        facts, _ = CompanyInfoAgent(backend, Mock(), self.settings).extract(
            {"name": "Atlas Labs", "domain": "atlas.example.test"},
            sources,
            time.monotonic() + 10,
        )
        self.store.save_company(
            "alice", "atlas.example.test", "Atlas Labs", facts, sources
        )
        _, fresh = MemoryAgent(self.store, self.settings).lookup(
            "alice", "atlas.example.test", datetime.now(UTC)
        )
        self.assertFalse(
            fresh, "Missing news must not count as a complete fresh company record"
        )
        news = backend.fetch(
            "https://news.example.test/atlas.example.test/news", time.monotonic() + 10
        )
        additional, _ = CompanyInfoAgent(backend, Mock(), self.settings).extract(
            {"name": "Atlas Labs", "domain": "atlas.example.test"},
            [news],
            time.monotonic() + 10,
        )
        self.store.save_company(
            "alice", "atlas.example.test", "Atlas Labs", additional, [news]
        )
        _, fresh = MemoryAgent(self.store, self.settings).lookup(
            "alice", "atlas.example.test", datetime.now(UTC)
        )
        self.assertTrue(fresh)
        _, fresh = MemoryAgent(self.store, self.settings).lookup(
            "alice", "atlas.example.test", datetime.now(UTC) + timedelta(days=8)
        )
        self.assertFalse(fresh)
        self.assertIsNone(self.store.company("bob", "atlas.example.test"))

    def test_uncited_facts_are_excluded_from_report(self):
        record = {
            "id": "c",
            "domain": "example.com",
            "name": "Example",
            "version": 1,
            "updated_at": utcnow(),
            "sources": [],
            "facts": [
                {
                    "id": "invented",
                    "category": "risk",
                    "text": "Unsupported",
                    "source_ids": [],
                }
            ],
        }
        report = ReportGenerationAgent().build(record, [], {}, [], [], "live", 0.01, {})
        self.assertFalse(report["facts"])
        self.assertEqual(report["quality"]["unique_sources"], 0)
        self.assertTrue(report["errors"])

    def test_naive_dates_header_injection_and_unknown_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            Meeting(start="2026-10-26T10:00", end="2026-10-26T10:30")
        with self.assertRaises(ValidationError):
            SendPayload(
                to=["valid@example.com"],
                subject="Hi\r\nBcc: stolen@example.com",
                body="text",
            )
        with self.assertRaises(ValidationError):
            SendPayload(
                to=["victim@example.com\r\nBcc: stolen@example.com"],
                subject="Hi",
                body="text",
            )
        with self.assertRaises(ValidationError):
            ResearchRequest(message_ids=["x"], mode="live")

    def test_google_send_builds_reviewed_mime_and_never_retries_write(self):
        backend = LiveBackend(
            Credentials(token="fake", scopes=[GMAIL_SEND]), self.settings
        )
        service = Mock()
        request = service.users.return_value.messages.return_value.send.return_value
        request.execute.return_value = {"id": "sent-1"}
        payload = {
            "to": ["partner@example.com"],
            "subject": "Reviewed subject",
            "body": "Exactly reviewed body",
            "thread_id": "thread-1",
            "rfc_message_id": "<original@example.com>",
        }
        with patch.object(backend, "service", return_value=service):
            result = backend.send(payload)
        arguments = (
            service.users.return_value.messages.return_value.send.call_args.kwargs
        )
        message = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(arguments["body"]["raw"])
        )
        self.assertEqual(message["To"], "partner@example.com")
        self.assertEqual(message["Subject"], "Reviewed subject")
        self.assertEqual(message["In-Reply-To"], "<original@example.com>")
        self.assertEqual(message.get_content().strip(), "Exactly reviewed body")
        request.execute.assert_called_once_with(num_retries=0)
        self.assertEqual(result["status"], "provider_accepted")
        self.assertFalse(result["delivery_confirmed"])

    def test_oauth_research_opt_in_requests_modify_and_calendar_without_full_mail_scope(
        self,
    ):
        flow = Mock()
        flow.authorization_url.return_value = ("https://accounts.google.com", "state")
        with patch.object(
            web_auth.Flow, "from_client_secrets_file", return_value=flow
        ) as factory:
            _, nonce = web_auth.start_login(research=True)
        self.addCleanup(web_auth.cancel_login, nonce)
        scopes = factory.call_args.kwargs["scopes"]
        self.assertEqual(scopes, [GMAIL_MODIFY, CALENDAR_READ, CALENDAR_EVENTS])
        self.assertNotIn("https://mail.google.com/", scopes)
        self.assertNotIn(GMAIL_READ, scopes)


class FinalRegressionTests(StoreCase):
    def test_research_permission_flag_survives_callback_host_redirect(self):
        with TestClient(
            server.app, base_url="http://127.0.0.1:9004", follow_redirects=False
        ) as client:
            response = client.get("/auth/login?research=1")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"], server.WEB_ORIGIN + "/auth/login?research=1"
        )

    def test_concurrent_first_requests_initialize_store_once(self):
        def construct(path):
            time.sleep(0.01)
            return self.store

        with (
            patch.object(rt, "_runtime", None),
            patch.object(rt.Settings, "from_env", return_value=self.settings),
            patch.object(rt, "Store", side_effect=construct) as factory,
        ):
            with ThreadPoolExecutor(max_workers=8) as workers:
                values = list(workers.map(lambda _: rt.runtime(), range(8)))
            factory.assert_called_once()
            self.assertTrue(all(value[1] is self.store for value in values))

    def test_revoked_google_session_is_cleared_before_login_redirect(self):
        with TestClient(
            server.app, base_url="http://localhost:9004", follow_redirects=False
        ) as client:
            client.post("/auth/demo")
            session_id = client.cookies.get(web_auth.SESSION_COOKIE)
            backend = Mock()
            backend.list_mail.side_effect = ResearchError(
                "AUTH_REQUIRED", "Expired", 401
            )
            with patch.object(rt, "backend_for", return_value=backend):
                result = client.get("/api/research/mail")
            self.assertEqual(result.status_code, 401)
            self.assertIsNone(web_auth.get_session(session_id))
            self.assertEqual(client.get("/login").status_code, 200)

    def test_no_referrer_same_origin_form_can_start_demo(self):
        with TestClient(
            server.app, base_url="http://localhost:9004", follow_redirects=False
        ) as client:
            response = client.post(
                "/auth/demo",
                headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/research")
            blocked = client.post(
                "/auth/logout",
                headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
            )
            self.assertEqual(blocked.status_code, 403)

    def test_research_never_follows_unsubscribe_or_token_links(self):
        backend = DemoBackend(self.settings)
        message = backend.read_mail("demo-1")
        message["body"] = (
            "Website https://atlas.example.test/ and https://atlas.example.test/unsubscribe?token=secret"
        )
        agent = EmailAgent(backend, Mock())
        company, *_ = agent.resolve(
            message,
            None,
            datetime.now(UTC),
            ResearchRequest(message_ids=["demo-1"]),
            time.monotonic() + 10,
        )
        self.assertEqual(company["website"], "https://atlas.example.test/")

    def test_http_503_after_send_is_unknown_and_missing_confirmation_id_is_unknown(
        self,
    ):
        from googleapiclient.errors import HttpError
        from httplib2 import Response

        backend = LiveBackend(
            Credentials(token="fake", scopes=[GMAIL_SEND]), self.settings
        )
        request = Mock()
        request.execute.side_effect = HttpError(
            Response({"status": "503"}),
            b'{"error":{"message":"private provider response"}}',
        )
        with self.assertRaises(ResearchError) as caught:
            backend.execute(request, write=True)
        self.assertEqual(caught.exception.code, "ACTION_OUTCOME_UNKNOWN")
        self.assertNotIn("private provider response", caught.exception.message)
        service = Mock()
        service.users.return_value.messages.return_value.send.return_value.execute.return_value = {}
        with (
            patch.object(backend, "service", return_value=service),
            self.assertRaises(ResearchError) as caught,
        ):
            backend.send(
                {"to": ["partner@example.com"], "subject": "Subject", "body": "body"}
            )
        self.assertEqual(caught.exception.code, "ACTION_OUTCOME_UNKNOWN")

    def test_deadline_stops_before_provider_request(self):
        backend = LiveBackend(
            Credentials(token="fake", scopes=[GMAIL_READ]), self.settings
        )
        backend.deadline = time.monotonic() - 1
        request = Mock()
        with self.assertRaises(ResearchError) as caught:
            backend.execute(request)
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        request.execute.assert_not_called()

    def test_modify_grant_satisfies_legacy_read_tools(self):
        self.assertTrue(
            gmail_service._has_read_access(
                Credentials(token="fake", scopes=[GMAIL_MODIFY])
            )
        )
        self.assertFalse(
            gmail_service._has_read_access(
                Credentials(token="fake", scopes=[CALENDAR_READ])
            )
        )

    def test_model_cannot_smuggle_approval_or_action_into_extraction(self):
        with self.assertRaises(ValidationError):
            ExtractedFacts.model_validate(
                {"facts": [], "approved": True, "send_to": "attacker@example.com"}
            )

    def test_search_reads_bounded_stream_and_does_not_fallback_to_mock(self):

        settings = self.settings.model_copy(
            update={
                "search_provider": "google",
                "google_key": "fake-key",
                "google_engine": "fake-engine",
            }
        )
        backend = LiveBackend(Credentials(token="fake", scopes=[GMAIL_READ]), settings)
        session = Mock()
        response = Mock()
        session.get.return_value = Mock(
            __enter__=Mock(return_value=response), __exit__=Mock(return_value=False)
        )
        response.status_code = 200
        response.iter_content.return_value = [b"x" * 1_000_001]
        with (
            patch(
                "src.services.research.backends.live.requests.Session",
                return_value=session,
            ),
            self.assertRaises(ResearchError) as caught,
        ):
            backend.search("Example", "news", time.monotonic() + 10)
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
        self.assertTrue(session.get.call_args.kwargs["stream"])
        self.assertFalse(session.trust_env)

    def test_google_freebusy_missing_timezone_is_unknown(self):
        backend = LiveBackend(
            Credentials(token="fake", scopes=[CALENDAR_READ]), self.settings
        )
        service = Mock()
        service.freebusy.return_value.query.return_value.execute.return_value = {
            "calendars": {
                "primary": {
                    "busy": [{"start": "2026-10-26T09:00", "end": "2026-10-26T10:00"}]
                }
            }
        }
        with (
            patch.object(backend, "service", return_value=service),
            self.assertRaises(ResearchError),
        ):
            backend.busy(
                datetime.now(UTC), datetime.now(UTC) + timedelta(days=1), "UTC"
            )


class CustomerSampleTests(StoreCase):
    def setUp(self):
        super().setUp()
        runtime_patch = patch.object(
            rt, "runtime", return_value=(self.settings, self.store)
        )
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        self.client = TestClient(
            server.app, base_url="http://localhost:9004", follow_redirects=False
        )
        self.addCleanup(self.client.close)
        self.client.post("/auth/demo")
        self.headers = {"X-Research-Request": "1"}

    def run_samples(self, ids=None):
        ids = ids or [
            message["id"]
            for message in self.client.get("/api/research/mail").json()["messages"]
        ]
        result = self.client.post(
            "/api/research", json={"message_ids": ids}, headers=self.headers
        )
        self.assertEqual(result.status_code, 202, result.text)
        return self.client.get("/api/research-batches/" + result.json()["id"]).json()

    def report_for(self, batch, name):
        run = next(run for run in batch["runs"] if run["company"]["name"] == name)
        return self.client.get("/api/reports/" + run["report_id"]).json()

    def test_default_dataset_contains_six_supplied_email_inputs(self):
        from src.services.research.backends.demo import CustomerSampleBackend

        config = self.client.get("/api/research/config").json()
        self.assertEqual(config["demo_dataset"], "customer")
        self.assertEqual(config["search_provider"], "input_only")
        backend = CustomerSampleBackend(self.settings)
        expected = {
            "FPT Software": (
                "Nguyễn Văn A",
                "https://fptsoftware.com",
                "Chúng tôi muốn tìm hiểu giải pháp AI cho doanh nghiệp.",
            ),
            "Vinamilk": (
                "Trần Minh Đức",
                "https://www.vinamilk.com.vn",
                "Mong muốn trao đổi về ứng dụng AI trong quản lý tài liệu.",
            ),
            "Samsung Electronics Vietnam": (
                "Sarah Lee",
                "https://www.samsung.com/vn/",
                "Xin hẹn buổi giới thiệu giải pháp vào tuần tới.",
            ),
            "Shopee Vietnam": (
                "David Chen",
                "https://shopee.vn",
                "Chúng tôi quan tâm đến giải pháp OCR.",
            ),
            "Viettel Solutions": (
                "Nguyễn Hoàng Anh",
                "https://viettelsolutions.vn",
                "Đính kèm hồ sơ năng lực để hai bên tham khảo.",
            ),
            "Bosch Global Software Technologies Vietnam": (
                "Emily Wong",
                "https://www.bosch.com/vn",
                "Chúng tôi muốn trao đổi về giải pháp AI Automation.",
            ),
        }
        self.assertEqual(len(backend.messages), 6)
        for message in backend.messages.values():
            sender, website, content = expected[message["company_name"]]
            self.assertEqual(message["sample_sender_name"], sender)
            self.assertEqual(message["provided_website"], website)
            self.assertEqual(message["sample_content"], content)
            self.assertEqual(message["date"], "")
            self.assertNotIn("meeting", message)
            self.assertTrue(message["sender_address_placeholder"])
            self.assertIn("@example.invalid", message["from"])

    def test_six_reports_do_not_invent_real_company_research(self):
        with patch.object(
            rt, "LiveBackend", side_effect=AssertionError("No live provider")
        ):
            batch = self.run_samples()
        self.assertEqual(len(batch["runs"]), 6)
        self.assertEqual(batch["status"], "partial")
        for run in batch["runs"]:
            report = self.client.get("/api/reports/" + run["report_id"]).json()
            self.assertEqual(report["demo_dataset"], "customer")
            self.assertEqual(report["quality"]["coverage_percent"], 0)
            self.assertTrue(
                all(fact["provenance"] == "user" for fact in report["facts"])
            )
            self.assertFalse(
                any(
                    fact["category"] in {"risk", "news", "people", "pricing", "scale"}
                    for fact in report["facts"]
                )
            )
            self.assertTrue(
                all(
                    source["kind"] == "sample_email" and source["url"] == ""
                    for source in report["sources"]
                )
            )
            self.assertEqual(report["roles"]["company_info"], "limited")
            self.assertTrue(report["calendar"]["demo"])

    def test_samsung_next_week_does_not_become_an_invented_appointment(self):
        report = self.report_for(
            self.run_samples(["sample-samsung-vietnam"]), "Samsung Electronics Vietnam"
        )
        meeting = report["meetings"][0]
        self.assertEqual(meeting["status"], "needs_clarification")
        self.assertIsNone(meeting["proposal"])
        self.assertIsNone(meeting["draft"])
        self.assertIn(
            "MEETING_AMBIGUOUS", [error["code"] for error in report["errors"]]
        )
        self.assertIn("chưa có ngày gửi", meeting["message"])

    def test_viettel_missing_attachment_is_explicit(self):
        report = self.report_for(
            self.run_samples(["sample-viettel-solutions"]), "Viettel Solutions"
        )
        self.assertIn(
            "ATTACHMENT_NOT_PROVIDED", [error["code"] for error in report["errors"]]
        )
        self.assertFalse(
            any(
                ".pdf" in fact["text"] or ".docx" in fact["text"]
                for fact in report["facts"]
            )
        )

    def test_switching_to_synthetic_uses_separate_demo_identity(self):
        batch = self.run_samples(["sample-fpt-software"])
        old_id = self.client.cookies.get(web_auth.SESSION_COOKIE)
        old_report = batch["runs"][0]["report_id"]
        self.client.post("/auth/demo?dataset=synthetic")
        self.assertIsNone(web_auth.get_session(old_id))
        self.assertEqual(
            self.client.get("/api/research/config").json()["demo_dataset"], "synthetic"
        )
        self.assertEqual(
            len(self.client.get("/api/research/mail").json()["messages"]), 4
        )
        self.assertEqual(self.client.get("/api/reports/" + old_report).status_code, 404)

    def test_invalid_dataset_cannot_select_live_backend(self):
        response = self.client.post("/auth/demo?dataset=live")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.client.get("/api/research/config").json()["demo_dataset"], "customer"
        )


if __name__ == "__main__":
    unittest.main()
