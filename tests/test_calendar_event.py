"""Creating calendar events: proposal in chat, exact approval, idempotent write."""

import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials

from src.agents.approval_agent import HumanApprovalAgent
from src.configs.research import Settings
from src.schemas.errors import ResearchError
from src.schemas.research import Decision, EventPayload
from src.services import calendar_service
from src.services.gmail_service import request_credentials
from src.services.research import runtime as rt
from src.services.research.backends.demo import DemoBackend
from src.services.research.backends.live import (
    CALENDAR_EVENTS,
    CALENDAR_READ,
    LiveBackend,
)
from src.services.research.store import Store
from src.tools.calendar_event import (
    propose_calendar_event,
    propose_calendar_event_deletion,
)
from src.tools.research import research_chat_scope


class EventPayloadTests(unittest.TestCase):
    def test_requires_timezone_order_and_sane_duration(self):
        good = {
            "title": "Demo",
            "start": "2026-10-26T19:30:00+07:00",
            "end": "2026-10-26T21:30:00+07:00",
        }
        self.assertEqual(EventPayload(**good).title, "Demo")
        for bad in (
            {**good, "start": "2026-10-26T19:30:00"},  # naive time
            {**good, "end": "2026-10-26T19:32:00+07:00"},  # under 5 minutes
            {**good, "end": "2026-10-28T21:30:00+07:00"},  # over 24 hours
            {**good, "title": "a\nb"},
            {**good, "timezone": "Mars/Base"},
            {**good, "extra": 1},
        ):
            with self.subTest(bad), self.assertRaises(ValueError):
                EventPayload(**bad)


class ProposeToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(db_path=self.temp.name + "/r.sqlite3", use_llm=False)
        self.store = Store(self.settings.db_path)
        patcher = patch.object(rt, "_runtime", (self.settings, self.store))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request = SimpleNamespace(
            state=SimpleNamespace(
                login_session=SimpleNamespace(email="alice@example.com")
            )
        )

    def propose(self, scopes=(CALENDAR_READ, CALENDAR_EVENTS), events=(), **overrides):
        args = {
            "title": "Demo dự án cuối khóa",
            "start": "2026-10-26T19:30:00",
            "end": "2026-10-26T21:30:00",
        }
        args.update(overrides)
        token = request_credentials.set(Credentials(token="t", scopes=list(scopes)))
        try:
            with (
                patch.object(
                    calendar_service, "list_events", return_value=list(events)
                ),
                research_chat_scope(self.request) as context,
            ):
                return propose_calendar_event(**args), context
        finally:
            request_credentials.reset(token)

    def test_proposal_waits_for_approval_and_nothing_is_written(self):
        result, context = self.propose()
        self.assertEqual(result["status"], "awaiting_user_approval")
        self.assertFalse(result["created"])
        self.assertEqual(result["conflicts"], [])
        action = self.store.action("alice@example.com", result["action_id"])
        self.assertEqual((action["kind"], action["state"]), ("create_event", "pending"))
        # A time without an offset is read in the user's timezone (UTC+7).
        self.assertTrue(action["payload"]["start"].endswith("+07:00"))
        card = context.actions[0]
        self.assertEqual(card["url"], "/research?view=approvals")
        # The chat approval card carries the exact version and hash it will approve.
        self.assertEqual(card["version"], action["version"])
        self.assertEqual(card["payload_hash"], action["payload_hash"])
        self.assertEqual(card["payload"], action["payload"])
        self.assertEqual(card["conflicts"], [])
        again, _ = self.propose()
        self.assertEqual(
            again["action_id"], result["action_id"]
        )  # same event, same action

    def test_overlapping_events_are_reported_not_hidden(self):
        busy = {
            "title": "Họp nhóm",
            "start": "2026-10-26T20:00:00+07:00",
            "end": "2026-10-26T21:00:00+07:00",
            "all_day": False,
        }
        elsewhere = dict(
            busy,
            title="Sáng",
            start="2026-10-26T08:00:00+07:00",
            end="2026-10-26T09:00:00+07:00",
        )
        allday = dict(busy, title="Nghỉ lễ", all_day=True)
        result, _ = self.propose(events=[busy, elsewhere, allday])
        self.assertEqual([c["title"] for c in result["conflicts"]], ["Họp nhóm"])

    def test_a_conflict_comes_with_free_alternatives_of_the_same_length(self):
        busy = {
            "title": "Họp nhóm",
            "start": "2026-10-26T20:00:00+07:00",
            "end": "2026-10-26T21:00:00+07:00",
            "all_day": False,
        }
        tomorrow = dict(
            busy,
            title="Mai",
            start="2026-10-27T19:30:00+07:00",
            end="2026-10-27T21:30:00+07:00",
        )
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
        with patch("src.tools.calendar_event._now", return_value=now):
            result, context = self.propose(events=[busy, tomorrow])
        alts = result["alternatives"]
        self.assertEqual(context.actions[0]["alternatives"], alts)
        self.assertTrue(1 <= len(alts) <= 3)
        for alt in alts:
            start, end = (
                datetime.fromisoformat(alt["start"]),
                datetime.fromisoformat(alt["end"]),
            )
            self.assertEqual(end - start, timedelta(hours=2))  # same length
            self.assertGreater(start, now)
            self.assertFalse(
                start < datetime.fromisoformat(busy["end"])
                and end > datetime.fromisoformat(busy["start"])
            )
            self.assertFalse(
                start < datetime.fromisoformat(tomorrow["end"])
                and end > datetime.fromisoformat(tomorrow["start"])
                and start.date() == datetime.fromisoformat(tomorrow["start"]).date()
            )
        starts = [datetime.fromisoformat(a["start"]) for a in alts]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len({s.isoformat() for s in starts}), len(starts))
        self.assertIn("TRÙNG", result["message"])

    def test_no_alternatives_without_a_conflict_or_when_unreadable(self):
        result, _ = self.propose(events=[])
        self.assertEqual(result["alternatives"], [])
        with patch.object(calendar_service, "list_events", side_effect=PermissionError):
            token = request_credentials.set(
                Credentials(token="t", scopes=[CALENDAR_EVENTS])
            )
            try:
                with research_chat_scope(self.request):
                    result = propose_calendar_event(
                        "Demo", "2026-10-26T19:30:00", "2026-10-26T20:00:00"
                    )
            finally:
                request_credentials.reset(token)
        self.assertEqual(result["alternatives"], [])

    def test_unreadable_calendar_is_unknown_not_free(self):
        with patch.object(calendar_service, "list_events", side_effect=PermissionError):
            token = request_credentials.set(
                Credentials(token="t", scopes=[CALENDAR_EVENTS])
            )
            try:
                with research_chat_scope(self.request):
                    result = propose_calendar_event(
                        "Demo", "2026-10-26T19:30:00", "2026-10-26T20:00:00"
                    )
            finally:
                request_credentials.reset(token)
        self.assertIsNone(result["conflicts"])

    def test_missing_write_permission_gives_reconnect_steps_and_proposes_nothing(self):
        result, context = self.propose(scopes=(CALENDAR_READ,))
        self.assertEqual(result["code"], "CALENDAR_WRITE_PERMISSION_REQUIRED")
        self.assertIn("/auth/login?research=1", result["message"])
        self.assertEqual(self.store.actions("alice@example.com"), [])
        # The chat shows a one-click connect card instead of only text.
        self.assertEqual(context.actions[0]["kind"], "connect_google")
        self.assertEqual(context.actions[0]["url"], "/auth/login?research=1")

    def test_invalid_input_and_missing_login(self):
        result, _ = self.propose(end="2026-10-26T19:31:00")
        self.assertEqual(result["code"], "INVALID_INPUT")
        result, _ = self.propose(start="not a time")
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(
            propose_calendar_event("x", "2026-10-26T19:30:00", "2026-10-26T20:00:00")[
                "code"
            ],
            "WEB_LOGIN_REQUIRED",
        )


class ProposeDeletionTests(ProposeToolTests):
    EVENT = {
        "id": "ev1",
        "title": "Demo dự án cuối khóa",
        "start": "2026-10-26T19:30:00+07:00",
        "end": "2026-10-26T21:30:00+07:00",
        "all_day": False,
        "attendee_count": 0,
    }

    def delete(
        self, event="default", scopes=(CALENDAR_READ, CALENDAR_EVENTS), error=None
    ):
        event = self.EVENT if event == "default" else event
        token = request_credentials.set(Credentials(token="t", scopes=list(scopes)))
        try:
            with (
                patch.object(
                    calendar_service, "get_event", return_value=event, side_effect=error
                ),
                research_chat_scope(self.request) as context,
            ):
                return propose_calendar_event_deletion("ev1"), context
        finally:
            request_credentials.reset(token)

    def test_deletion_waits_for_approval_and_shows_the_exact_event(self):
        result, context = self.delete()
        self.assertEqual(result["status"], "awaiting_user_approval")
        self.assertFalse(result["deleted"])
        action = self.store.action("alice@example.com", result["action_id"])
        self.assertEqual((action["kind"], action["state"]), ("delete_event", "pending"))
        self.assertEqual(action["payload"]["event_id"], "ev1")
        card = context.actions[0]
        self.assertEqual(
            (card["kind"], card["payload_hash"]),
            ("delete_event", action["payload_hash"]),
        )

    def test_attendees_are_flagged_and_missing_events_are_not_proposed(self):
        result, _ = self.delete(event=dict(self.EVENT, attendee_count=3))
        self.assertIn("3 người", result["message"])
        result, _ = self.delete(event=None)
        self.assertEqual(result["code"], "EVENT_NOT_FOUND")

    def test_missing_permission_offers_connect_card_and_proposes_nothing(self):
        result, context = self.delete(scopes=(CALENDAR_READ,))
        self.assertEqual(result["code"], "CALENDAR_WRITE_PERMISSION_REQUIRED")
        self.assertEqual(context.actions[0]["kind"], "connect_google")
        result, context = self.delete(error=PermissionError("no calendar"))
        self.assertEqual(context.actions[0]["kind"], "connect_google")
        self.assertEqual(self.store.actions("alice@example.com"), [])


class ApprovalAndWriteTests(unittest.TestCase):
    PAYLOAD = {
        "title": "Demo",
        "start": "2026-10-26T19:30:00+07:00",
        "end": "2026-10-26T21:30:00+07:00",
        "timezone": "Asia/Ho_Chi_Minh",
        "description": "",
        "location": "",
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(db_path=self.temp.name + "/r.sqlite3", use_llm=False)
        self.store = Store(self.settings.db_path)

    def propose(self, mode):
        return self.store.propose(
            "alice@example.com",
            "create_event",
            self.PAYLOAD,
            {"mode": mode, "mailbox": "alice@example.com", "calendar_slots": []},
            600,
        )

    def decision(self, action, verdict="approve"):
        return Decision(
            decision=verdict,
            version=action["version"],
            payload_hash=action["payload_hash"],
        )

    def test_approval_creates_exactly_once_and_rejection_never_writes(self):
        backend = SimpleNamespace(
            mode="live",
            check_write=Mock(),
            create_event=Mock(return_value={"status": "created", "event_id": "e1"}),
        )
        agent = HumanApprovalAgent(self.store, backend, self.settings)
        action = self.propose("live")
        done = agent.decide("alice@example.com", action["id"], self.decision(action))
        self.assertEqual(done["execution"], "succeeded")
        agent.decide("alice@example.com", action["id"], self.decision(action))  # replay
        backend.create_event.assert_called_once_with(self.PAYLOAD)
        backend.check_write.assert_called_once_with("create_event")

        other = self.store.propose(
            "alice@example.com",
            "create_event",
            dict(self.PAYLOAD, title="Khác"),
            {"mode": "live", "mailbox": "alice@example.com", "calendar_slots": []},
            600,
        )
        agent.decide("alice@example.com", other["id"], self.decision(other, "reject"))
        backend.create_event.assert_called_once()

    def test_demo_backend_only_simulates(self):
        agent = HumanApprovalAgent(
            self.store,
            DemoBackend(self.settings, "alice@example.com", self.store),
            self.settings,
        )
        action = self.propose("demo")
        done = agent.decide("alice@example.com", action["id"], self.decision(action))
        self.assertEqual(done["result"]["status"], "simulated_create")

    def live_backend(self, events):
        backend = LiveBackend(
            Credentials(token="t", scopes=[CALENDAR_EVENTS]), self.settings
        )
        backend.check_write = Mock()
        backend.service = Mock(return_value=SimpleNamespace(events=lambda: events))
        return backend

    def test_live_create_sets_a_content_derived_id_and_sends_no_invitations(self):
        events = Mock()
        events.get.return_value = "get-req"
        events.insert.return_value = "insert-req"
        backend = self.live_backend(events)
        seen = {}

        def execute(request, write=False):
            if request == "get-req":
                raise ResearchError("NOT_FOUND", "x", 404)
            seen["write"] = write
            return {
                "id": events.insert.call_args.kwargs["body"]["id"],
                "htmlLink": "https://cal/e",
            }

        backend.execute = execute
        result = backend.create_event(self.PAYLOAD)
        body = events.insert.call_args.kwargs["body"]
        self.assertEqual(result["status"], "created")
        self.assertTrue(seen["write"])
        self.assertEqual(events.insert.call_args.kwargs["sendUpdates"], "none")
        self.assertEqual(body["summary"], "Demo")
        self.assertNotIn("attendees", body)
        self.assertRegex(body["id"], r"^[0-9a-f]{40}$")

    DELETION = {
        "event_id": "ev1",
        "title": "Demo",
        "start": "2026-10-26T19:30:00+07:00",
        "end": "2026-10-26T21:30:00+07:00",
        "all_day": False,
        "attendee_count": 0,
    }

    def delete_backend(self, current):
        events = Mock()
        backend = self.live_backend(events)

        def execute(request, write=False):
            if request == "get-req":
                if current is None:
                    raise ResearchError("NOT_FOUND", "x", 404)
                return current
            return ""

        events.get.return_value = "get-req"
        events.delete.return_value = "delete-req"
        backend.execute = execute
        return backend, events

    def test_live_delete_removes_only_the_approved_event_without_notifying(self):
        current = {
            "id": "ev1",
            "summary": "Demo",
            "start": {"dateTime": "2026-10-26T19:30:00+07:00"},
        }
        backend, events = self.delete_backend(current)
        self.assertEqual(backend.delete_event(self.DELETION)["status"], "deleted")
        self.assertEqual(
            events.delete.call_args.kwargs,
            {"calendarId": "primary", "eventId": "ev1", "sendUpdates": "none"},
        )

    def test_delete_refuses_an_event_that_changed_and_tolerates_a_missing_one(self):
        changed = {
            "id": "ev1",
            "summary": "Khác",
            "start": {"dateTime": "2026-10-26T19:30:00+07:00"},
        }
        backend, events = self.delete_backend(changed)
        with self.assertRaises(ResearchError) as error:
            backend.delete_event(self.DELETION)
        self.assertEqual(error.exception.code, "ACTION_CHANGED")
        events.delete.assert_not_called()
        for current in (None, {"id": "ev1", "status": "cancelled"}):
            backend, events = self.delete_backend(current)
            self.assertEqual(
                backend.delete_event(self.DELETION)["status"], "already_deleted"
            )
            events.delete.assert_not_called()

    def test_demo_delete_is_only_simulated_through_approval(self):
        agent = HumanApprovalAgent(
            self.store,
            DemoBackend(self.settings, "alice@example.com", self.store),
            self.settings,
        )
        action = self.store.propose(
            "alice@example.com",
            "delete_event",
            self.DELETION,
            {"mode": "demo", "mailbox": "alice@example.com", "calendar_slots": []},
            600,
        )
        done = agent.decide("alice@example.com", action["id"], self.decision(action))
        self.assertEqual(done["result"]["status"], "simulated_delete")

    def test_retry_after_an_unknown_outcome_does_not_duplicate(self):
        events = Mock()
        backend = self.live_backend(events)
        backend.execute = Mock(
            return_value={"id": "e", "status": "confirmed", "htmlLink": "https://cal/e"}
        )
        self.assertEqual(backend.create_event(self.PAYLOAD)["status"], "already_exists")
        events.insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
