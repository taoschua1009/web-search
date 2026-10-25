"""Pydantic models: settings validation and the typed batch records."""

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from src.configs.research import Settings
from src.schemas.research import Meeting
from src.schemas.workflow import Batch, FastResult, ResearchScope, Run, Unresolved
from src.workflows.models import CompanyGroup, ResolvedEmail


class SettingsTests(unittest.TestCase):
    def test_defaults_are_valid_and_secrets_stay_out_of_repr(self):
        settings = Settings(tavily_key="secret-t", google_key="secret-g")
        self.assertEqual(settings.agent_mode, "fast")
        self.assertNotIn("secret-t", repr(settings))
        self.assertNotIn("secret-g", repr(settings))

    def test_settings_are_immutable(self):
        with self.assertRaises(ValidationError):
            Settings().max_pages = 5

    def test_out_of_range_values_are_rejected_as_value_errors(self):
        for bad in (
            {"max_pages": 0},
            {"max_pages": 61},
            {"max_search_queries": 31},
            {"deadline_seconds": 5},
            {"agent_mode": "turbo"},
            {"search_provider": "bing"},
            {"approval_seconds": 10},
            {"work_start": 18, "work_end": 9},
        ):
            with self.subTest(bad), self.assertRaises(ValueError):
                Settings(**bad)

    def test_from_env_parses_and_validates_strings(self):
        env = {
            "RESEARCH_MAX_PAGES": "12",
            "RESEARCH_DEADLINE_SECONDS": "300",
            "RESEARCH_USE_LLM": "false",
            "RESEARCH_AGENT_MODE": "deep",
        }
        with patch.dict("os.environ", env):
            settings = Settings.from_env()
        self.assertEqual(
            (settings.max_pages, settings.deadline_seconds, settings.use_llm),
            (12, 300.0, False),
        )
        self.assertEqual(settings.agent_mode, "deep")
        for key, value in (
            ("RESEARCH_MAX_PAGES", "abc"),
            ("RESEARCH_AGENT_MODE", "x"),
            ("RESEARCH_WORK_END", "5"),
        ):
            with patch.dict("os.environ", {key: value}), self.assertRaises(ValueError):
                Settings.from_env()


class RecordTests(unittest.TestCase):
    STORED = {
        "id": "b1",
        "status": "queued",
        "mode": "live",
        "created_at": "2026-10-26T00:00:00+00:00",
        "runs": [],
        "unresolved": [],
        "errors": [],
        "future_field": {"kept": True},
    }

    def test_batch_round_trips_without_losing_unknown_fields(self):
        batch = Batch.model_validate(self.STORED)
        batch.status = "partial"
        batch.unresolved.append(Unresolved(message_id="m1", name="Acme"))
        stored = batch.to_storage()
        self.assertEqual(stored["future_field"], {"kept": True})
        self.assertEqual(stored["status"], "partial")
        self.assertEqual(stored["unresolved"][0]["message_id"], "m1")
        self.assertNotIn("error", stored)  # unset optionals are omitted

    def test_run_stores_only_what_is_set(self):
        run = Run(
            id="r1",
            batch_id="b1",
            mode="live",
            company={"name": "Acme", "domain": "acme.test"},
            message_ids=["m1"],
            created_at="t",
            roles={"email": "queued"},
        )
        run.status = "partial"
        run.mail_context = [{"id": "m1", "from": "a", "subject": "s"}]
        stored = run.to_storage()
        self.assertEqual(stored["status"], "partial")
        self.assertNotIn("report_id", stored)
        with self.assertRaises(ValidationError):
            run.roles = {"email": 1}  # assignments are validated

    def test_scope_and_fast_result_validate_their_shape(self):
        scope = ResearchScope(
            plan_version=4,
            searched_at="t",
            topics=[
                {"id": "profile", "label": "Hồ sơ", "searched": True, "status": "read"}
            ],
            page_limit=24,
            search_limit=9,
            website_pages=3,
            external_pages=10,
            unique_domains=5,
            sources=[{"id": "s1", "url": "https://a.test/"}],
        ).to_storage()
        self.assertEqual(scope["topics"][0]["read_sources"], [])
        self.assertNotIn("timings", scope)
        with self.assertRaises(ValidationError):
            FastResult(
                dossier={}, sources=[], facts=[], topics=[{"label": "x"}], errors=[]
            )


class CompanyGroupTests(unittest.TestCase):
    def test_group_collects_meeting_requests_and_message_warnings(self):
        group = CompanyGroup(
            company={"name": "Acme", "domain": "acme.test"}, reasoner=None
        )
        group.add(
            ResolvedEmail(
                message={
                    "id": "m1",
                    "body_truncated": True,
                    "from": "a",
                    "subject": "s",
                    "body": "b" * 3000,
                },
                wants_meeting=True,
                clarification="Chưa rõ giờ",
            )
        )
        group.add(
            ResolvedEmail(
                message={
                    "id": "m2",
                    "attachment_notice": "Thiếu file",
                    "from": "a",
                    "subject": "s",
                    "body": "",
                },
            )
        )
        self.assertEqual([r.message["id"] for r in group.meeting_requests], ["m1"])
        self.assertEqual(
            sorted(e["code"] for e in group.errors),
            ["ATTACHMENT_NOT_PROVIDED", "EMAIL_TRUNCATED", "MEETING_AMBIGUOUS"],
        )
        self.assertEqual(len(group.email_excerpts()[0]["body"]), 1500)

    def test_meeting_objects_are_validated(self):
        with self.assertRaises(ValidationError):
            ResolvedEmail(
                message={"id": "m"},
                meeting={"start": "2026-10-26T10:00:00", "end": "2026-10-26T10:30:00"},
            )
        meeting = Meeting(
            start="2026-10-26T10:00:00+07:00", end="2026-10-26T10:30:00+07:00"
        )
        self.assertEqual(
            ResolvedEmail(message={"id": "m"}, meeting=meeting).meeting, meeting
        )


if __name__ == "__main__":
    unittest.main()
