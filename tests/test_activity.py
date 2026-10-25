"""Live agent activity: tracker, per-owner isolation, SSE frames and what gets reported."""

import asyncio
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.api.activity_routes import event_stream
from src.observability import activity
from src.observability.activity import NULL_SCOPE, ActivityTracker
from src.observability.pricing import calc_cost


def usage(i, o):
    return SimpleNamespace(input_tokens=i, output_tokens=o)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = ActivityTracker()

    def test_agent_lifecycle_tools_cost_and_latency_are_aggregated(self):
        team = self.tracker.start_team("Alice@Example.com", "chat", "tạo lịch")
        agent = team.agent("Chat agent", "chat", model="claude-haiku-5-5")
        agent.llm("claude-haiku-5-5", 1000, 500, 400)
        agent.llm("claude-haiku-5-5", 3000, 100, 800)
        with agent.tool("list_calendar_events", "start_date=2026-10-26"):
            snapshot = self.tracker.snapshot("alice@example.com")
            running = snapshot["teams"][0]["agents"][0]
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["current_tool"], "list_calendar_events")
        try:
            with agent.tool("save_memory"):
                raise ValueError("boom")
        except ValueError:
            pass
        agent.finish()
        team.finish()

        view = self.tracker.snapshot("ALICE@example.com")  # owner match ignores case
        item = view["teams"][0]["agents"][0]
        self.assertEqual(
            (view["teams"][0]["status"], item["status"]), ("completed", "completed")
        )
        self.assertEqual(item["llm"]["calls"], 2)
        self.assertEqual(item["llm"]["avg_ms"], 600)
        self.assertAlmostEqual(
            item["llm"]["cost_usd"], calc_cost("claude-haiku-5-5", 4000, 600)
        )
        self.assertEqual(item["tool_counts"]["save_memory"]["errors"], 1)
        self.assertEqual(item["tool_counts"]["list_calendar_events"]["calls"], 1)
        self.assertIsNone(item["current_tool"])
        self.assertEqual(view["totals"]["tool_calls"], 2)
        self.assertAlmostEqual(view["totals"]["cost_usd"], item["llm"]["cost_usd"])
        self.assertAlmostEqual(view["teams"][0]["cost_usd"], item["llm"]["cost_usd"])

    def test_parallel_tools_are_all_listed_while_active_and_removed_when_done(self):
        team = self.tracker.start_team("a@x.com", "research", "r")
        agent = team.agent("Search (evidence)", "search")
        first, second = agent.tool("web_search", "q1"), agent.tool("web_search", "q2")
        first.__enter__()
        second.__enter__()

        def view():
            return self.tracker.snapshot("a@x.com")["teams"][0]["agents"][0]

        self.assertEqual([t["detail"] for t in view()["active_tools"]], ["q1", "q2"])
        self.assertEqual(view()["current_detail"], "q2")
        first.__exit__(None, None, None)
        self.assertEqual([t["detail"] for t in view()["active_tools"]], ["q2"])
        second.__exit__(None, None, None)
        self.assertEqual((view()["active_tools"], view()["current_tool"]), ([], None))
        # an agent that finishes (or is interrupted) never keeps phantom tools
        stuck = agent.tool("fetch_page", "x")
        stuck.__enter__()
        team.finish()
        self.assertEqual(view()["active_tools"], [])

    def test_context_manager_marks_failure_and_unfinished_agents_are_interrupted(self):
        team = self.tracker.start_team("a@x.com", "research", "r")
        with self.assertRaises(RuntimeError), team.agent("Boom") as agent:
            agent.llm("claude-haiku-5-5", 10, 10, 5)
            raise RuntimeError("x")
        team.agent("Forgotten")
        team.finish()
        agents = self.tracker.snapshot("a@x.com")["teams"][0]["agents"]
        self.assertEqual([a["status"] for a in agents], ["failed", "interrupted"])
        self.assertEqual(
            self.tracker.snapshot("a@x.com")["teams"][0]["status"], "failed"
        )

    def test_people_only_see_their_own_agents(self):
        self.tracker.start_team("alice@x.com", "chat", "secret plan").agent("A")
        self.assertEqual(self.tracker.snapshot("bob@x.com")["totals"]["teams"], 0)
        self.assertEqual(self.tracker.clear_finished("bob@x.com"), 0)
        self.assertEqual(self.tracker.snapshot("alice@x.com")["totals"]["teams"], 1)

    def test_details_are_masked_and_history_is_bounded(self):
        team = self.tracker.start_team(
            "a@x.com",
            "chat",
            "mail bob@example.com sk-ant-api03-abcdefghijklmnopqrstuvwx",
        )
        agent = team.agent("A")
        for index in range(60):
            with agent.tool("web_search", f"q{index} " + "x" * 400):
                pass
        item = self.tracker.snapshot("a@x.com")["teams"][0]
        self.assertNotIn("bob@example.com", json.dumps(item))
        self.assertNotIn("sk-ant", json.dumps(item))
        self.assertLessEqual(len(item["agents"][0]["tools"]), 10)
        self.assertEqual(item["agents"][0]["tool_counts"]["web_search"]["calls"], 60)
        self.assertLessEqual(
            max(len(t["detail"]) for t in item["agents"][0]["tools"]),
            activity.MAX_DETAIL + 1,
        )

    def test_retention_is_bounded_and_running_teams_are_never_dropped(self):
        running = self.tracker.start_team("a@x.com", "chat", "still running")
        for index in range(activity.MAX_TEAMS + 10):
            self.tracker.start_team("a@x.com", "chat", f"t{index}").finish()
        titles = [t["title"] for t in self.tracker.snapshot("a@x.com")["teams"]]
        self.assertLessEqual(len(titles), activity.MAX_TEAMS + 1)
        self.assertIn("still running", titles)
        running.finish()
        self.assertGreaterEqual(self.tracker.clear_finished("a@x.com"), 1)

    def test_finished_teams_expire_after_a_while(self):
        team = self.tracker.start_team("a@x.com", "chat", "old")
        team.finish()
        with patch.object(activity, "KEEP_FINISHED_SECONDS", -1):
            self.assertEqual(self.tracker.snapshot("a@x.com")["totals"]["teams"], 0)

    def test_concurrent_agents_do_not_lose_updates(self):
        team = self.tracker.start_team("a@x.com", "research", "parallel")
        agents = [team.agent(f"s{i}", "specialist") for i in range(6)]

        def work(agent):
            for _ in range(50):
                agent.llm("claude-haiku-5-5", 100, 10, 1)
                with agent.tool("t"):
                    pass

        threads = [threading.Thread(target=work, args=(a,)) for a in agents]
        [t.start() for t in threads]
        [t.join() for t in threads]
        totals = self.tracker.snapshot("a@x.com")["totals"]
        self.assertEqual(
            (totals["llm_calls"], totals["tool_calls"], totals["agents_running"]),
            (300, 300, 6),
        )

    def test_observation_can_never_break_the_observed_code(self):
        team = self.tracker.start_team("a@x.com", "chat", "x")
        agent = team.agent("A")
        with patch.object(
            self.tracker, "_mutate", side_effect=RuntimeError("tracker bug")
        ):
            agent.llm("claude-haiku-5-5", 1, 1, 1)
            with agent.tool("t"):
                pass
            agent.finish()
        with NULL_SCOPE.agent() as null, null.tool("x") as call:
            null.llm("m", 1, 1, 1)
            call.fail()


class StreamTests(unittest.TestCase):
    def test_stream_sends_a_frame_per_change_and_stops_on_disconnect(self):
        tracker = ActivityTracker()
        team = tracker.start_team("a@x.com", "chat", "t")
        checks = iter([False, False, False, True])

        async def disconnected():
            value = next(checks)
            if not value and len(frames) == 1:
                team.agent("late agent")  # a change while connected
            return value

        frames = []

        async def collect():
            async for frame in event_stream(
                tracker, "a@x.com", disconnected, poll=0.01, heartbeat=0.02
            ):
                frames.append(frame)

        asyncio.run(collect())
        data = [json.loads(f[6:]) for f in frames if f.startswith("data: ")]
        self.assertEqual(len(data[0]["teams"][0]["agents"]), 0)
        self.assertEqual(len(data[-1]["teams"][0]["agents"]), 1)

    def test_stream_is_scoped_to_the_owner_and_sends_keep_alives(self):
        tracker = ActivityTracker()
        tracker.start_team("alice@x.com", "chat", "alice only").finish()
        checks = iter([False] * 6 + [True])

        async def disconnected():
            return next(checks)

        frames = []

        async def collect():
            async for frame in event_stream(
                tracker, "bob@x.com", disconnected, poll=0.01, heartbeat=0.02
            ):
                frames.append(frame)

        asyncio.run(collect())
        self.assertEqual(json.loads(frames[0][6:])["totals"]["teams"], 0)
        self.assertNotIn("alice only", "".join(frames))
        self.assertTrue(any(frame.startswith(": keep-alive") for frame in frames))


class ApiTests(unittest.TestCase):
    def setUp(self):
        from src.api import server
        from src.services import web_auth

        web_auth._sessions.clear()
        self.addCleanup(web_auth._sessions.clear)
        self.tracker = ActivityTracker()
        for target in ("src.api.activity_routes.tracker",):
            patcher = patch(target, self.tracker)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(
            server.app, base_url="http://localhost:9004", follow_redirects=False
        )
        sid = web_auth.create_demo_session()
        web_auth._sessions[sid].demo, web_auth._sessions[sid].email = False, "me@x.com"
        self.client.cookies.set(web_auth.SESSION_COOKIE, sid)

    def test_pages_and_snapshot_need_login_and_are_owner_scoped(self):
        self.tracker.start_team("me@x.com", "chat", "mine").agent("A")
        self.tracker.start_team("other@x.com", "chat", "theirs")
        data = self.client.get("/api/activity").json()
        self.assertEqual([t["title"] for t in data["teams"]], ["mine"])
        self.assertEqual(self.client.get("/agents").status_code, 200)
        self.assertIn("agents.js", self.client.get("/agents").text)
        for path in ("/assets/agents.css", "/assets/agents.js"):
            self.assertEqual(self.client.get(path).status_code, 200)
        anonymous = TestClient(
            self.client.app, base_url="http://localhost:9004", follow_redirects=False
        )
        self.assertEqual(anonymous.get("/api/activity").status_code, 401)
        self.assertEqual(anonymous.get("/agents").status_code, 303)

    def test_clearing_needs_the_csrf_header_and_only_removes_finished_own_teams(self):
        done = self.tracker.start_team("me@x.com", "chat", "done")
        done.finish()
        self.tracker.start_team("me@x.com", "chat", "running")
        self.tracker.start_team("other@x.com", "chat", "theirs").finish()
        self.assertEqual(self.client.post("/api/activity/clear").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/activity/clear", headers={"x-research-request": "1"}
            ).json(),
            {"cleared": 1},
        )
        self.assertEqual(
            [t["title"] for t in self.tracker.snapshot("me@x.com")["teams"]],
            ["running"],
        )
        self.assertEqual(self.tracker.snapshot("other@x.com")["totals"]["teams"], 1)


class InstrumentationTests(unittest.TestCase):
    """The agents really report what they do."""

    def setUp(self):
        self.tracker = ActivityTracker()
        for target in (
            "src.agents.agent.tracker",
            "src.workflows.research_workflow.tracker",
        ):
            patcher = patch(target, self.tracker)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_chat_agent_reports_model_calls_tools_cost_and_latency(self):
        from src.agents.agent import Agent
        from src.registry import registry as registry_module
        from src.registry.registry import RateLimiter

        patcher = patch.object(registry_module, "rate_limiter", RateLimiter(100, 60))
        patcher.start()
        self.addCleanup(patcher.stop)
        call = SimpleNamespace(
            type="tool_use", name="search_memory", input={"query": "x"}, id="1"
        )
        replies = [
            SimpleNamespace(
                stop_reason="tool_use", content=[call], usage=usage(1000, 100)
            ),
            SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(text="done")],
                usage=usage(2000, 50),
            ),
        ]
        with patch("src.agents.agent.anthropic.Anthropic") as client:
            client.return_value.messages.create.side_effect = replies
            agent = Agent(principal="Me@X.com")
            with patch.object(
                agent.registry.get_tool("search_memory"),
                "handler",
                Mock(return_value={"memories": []}),
            ):
                self.assertEqual(agent.run("hello"), "done")
        data = self.tracker.snapshot("me@x.com")
        team = data["teams"][0]
        self.assertEqual(
            (team["kind"], team["status"], team["title"]),
            ("chat", "completed", "hello"),
        )
        item = team["agents"][0]
        self.assertEqual(
            (item["name"], item["status"], item["llm"]["calls"]),
            ("Chat agent", "completed", 2),
        )
        self.assertEqual(item["llm"]["input_tokens"], 3000)
        self.assertGreater(item["llm"]["cost_usd"], 0)
        self.assertEqual(item["tool_counts"]["search_memory"]["calls"], 1)

    def test_a_failing_chat_turn_is_marked_failed(self):
        from src.agents.agent import Agent

        with patch("src.agents.agent.anthropic.Anthropic") as client:
            client.return_value.messages.create.side_effect = RuntimeError("down")
            agent = Agent(principal="me@x.com")
            with self.assertRaises(RuntimeError):
                agent.run("hello")
        team = self.tracker.snapshot("me@x.com")["teams"][0]
        self.assertEqual(
            (team["status"], team["agents"][0]["status"]), ("failed", "failed")
        )

    def test_fast_research_reports_each_specialist_and_the_search_tools(self):
        from src.agents.fast_research_agent import SECTIONS, FastResearchAgent
        from src.configs.research import Settings
        from tests.test_research_fast import SECTIONS as OUT
        from tests.test_research_fast import FakeBackend, tool_response  # noqa: F401

        settings = Settings(use_llm=True)
        team = self.tracker.start_team("me@x.com", "research", "r")

        def create(**call):
            topic = next(
                t for t, task in activity_tasks.items() if call["system"].endswith(task)
            )
            response = tool_response(OUT[topic])
            response.usage = usage(5000, 800)
            return response

        from src.configs.research_prompts import SPECIALIST_TASKS as activity_tasks

        client = Mock()
        client.messages.create.side_effect = create
        fake = Mock()
        fake.return_value.__enter__ = Mock(return_value=client)
        fake.return_value.__exit__ = Mock(return_value=False)
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x"}),
            patch("anthropic.Anthropic", fake),
        ):
            result = FastResearchAgent(FakeBackend(), settings).run(
                {
                    "name": "Acme",
                    "domain": "acme.example.com",
                    "website": "https://acme.example.com/",
                },
                [],
                time.monotonic() + 60,
                team.scope("Acme"),
            )
        team.finish()
        self.assertIsNotNone(result)
        agents = self.tracker.snapshot("me@x.com")["teams"][0]["agents"]
        specialists = [a for a in agents if a["role"] == "specialist"]
        self.assertEqual(len(specialists), len(SECTIONS))
        for item in specialists:
            self.assertEqual(
                (item["group"], item["status"], item["llm"]["calls"]),
                ("Acme", "completed", 1),
            )
            self.assertEqual(item["model"], settings.fast_model)
            self.assertGreater(item["llm"]["cost_usd"], 0)
        searcher = next(a for a in agents if a["name"] == "Search (evidence)")
        self.assertEqual(searcher["status"], "completed")
        self.assertEqual(searcher["tool_counts"]["web_search"]["calls"], 9)
        self.assertIn("fetch_page", searcher["tool_counts"])

    def test_research_workflow_reports_a_team_with_company_agents(self):
        import tempfile

        from src.configs.research import Settings
        from src.schemas.research import ResearchRequest
        from src.services.research.backends.demo import DemoBackend
        from src.services.research.store import Store
        from src.workflows.research_workflow import ResearchWorkflow

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(db_path=directory + "/r.sqlite3", use_llm=False)
            store = Store(settings.db_path)
            request = ResearchRequest(message_ids=["demo-1", "demo-3"])
            batch, _ = store.create_batch(
                "Me@X.com", "k", request.model_dump(mode="json"), "demo"
            )
            ResearchWorkflow(store, settings).execute(
                "Me@X.com",
                batch["id"],
                request.model_dump(mode="json"),
                DemoBackend(settings, "me@x.com", store),
            )
        team = self.tracker.snapshot("me@x.com")["teams"][0]
        self.assertEqual((team["kind"], team["status"]), ("research", "completed"))
        groups = {a["group"] for a in team["agents"]}
        self.assertEqual(groups, {"", "Atlas Labs", "Northstar Coffee"})
        for company in ("Atlas Labs", "Northstar Coffee"):
            names = [a["name"] for a in team["agents"] if a["group"] == company]
            self.assertIn("Memory agent", names)
            self.assertIn("Report agent", names)
        self.assertEqual(team["agents"][0]["name"], "Email agent")
        self.assertTrue(all(a["status"] == "completed" for a in team["agents"]))


if __name__ == "__main__":
    unittest.main()
