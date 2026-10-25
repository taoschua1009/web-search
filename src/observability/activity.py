"""Live view of what the agents are doing: teams, agents, tools, cost and latency.

Code reports through small handles (``team.agent(...)``, ``agent.llm(...)``,
``with agent.tool(...)``). Everything is held in memory, bounded, and visible only to the
person who started the work. The browser reads snapshots from ``/api/activity`` and a
Server-Sent-Events stream; nothing here changes how the agents behave, and no call into
this module can raise into the code being observed.
"""

from __future__ import annotations

import contextvars
import itertools
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from src.observability.pricing import calc_cost
from src.registry.guardrails import redact

logger = logging.getLogger(__name__)
MAX_TEAMS = 40
KEEP_FINISHED_SECONDS = 30 * 60
MAX_TOOL_HISTORY = 40
MAX_DETAIL = 140
_call_ids = itertools.count(1)


def _safe(function):
    """Observation must never break the observed code."""

    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.warning(
                "activity tracking failed in %s", function.__name__, exc_info=True
            )
            return None

    wrapper.__name__ = function.__name__
    return wrapper


class ToolCall:
    """Context manager for one tool call; ``fail()`` marks it as an error."""

    def __init__(self, agent: AgentHandle, name: str, detail: str):
        self.agent, self.name, self.detail = agent, name, detail
        self.status, self.started = "ok", time.time()
        self.id = next(_call_ids)

    def __enter__(self):
        self.agent._tool_started(self)
        return self

    def fail(self) -> None:
        self.status = "error"

    def __exit__(self, kind, error, trace):
        if error is not None:
            self.status = "error"
        self.agent._tool_finished(self)
        return False


class AgentHandle:
    """Reports one agent's progress. Also a context manager (completed/failed on exit)."""

    def __init__(self, tracker: ActivityTracker, team_id: str, agent_id: str):
        self._tracker, self._team_id, self._id = tracker, team_id, agent_id

    def _update(self, change):
        self._tracker._mutate(self._team_id, self._id, change)

    @_safe
    def llm(
        self, model: str, input_tokens: int, output_tokens: int, latency_ms: float
    ) -> None:
        """Record one model call: tokens, estimated cost and latency."""
        cost = calc_cost(model, input_tokens, output_tokens)

        def change(agent):
            llm = agent["llm"]
            llm["calls"] += 1
            llm["input_tokens"] += int(input_tokens)
            llm["output_tokens"] += int(output_tokens)
            llm["cost_usd"] += cost
            llm["total_ms"] += latency_ms
            llm["last_ms"] = latency_ms
            agent["model"] = agent["model"] or model

        self._update(change)

    def tool(self, name: str, detail: str = "") -> ToolCall:
        """``with agent.tool("web_search", query): ...`` records duration and outcome."""
        return ToolCall(self, name, detail)

    @_safe
    def _tool_started(self, call: ToolCall) -> None:
        def change(agent):
            agent["active_tools"][call.id] = {
                "name": call.name,
                "detail": _detail(call.detail),
                "started": call.started,
            }
            agent.update(current_tool=call.name, current_detail=_detail(call.detail))

        self._update(change)

    @_safe
    def _tool_finished(self, call: ToolCall) -> None:
        duration = (time.time() - call.started) * 1000

        def change(agent):
            counts = agent["tool_counts"].setdefault(
                call.name, {"calls": 0, "errors": 0, "total_ms": 0.0}
            )
            counts["calls"] += 1
            counts["errors"] += call.status == "error"
            counts["total_ms"] += duration
            agent["tools"].append(
                {
                    "name": call.name,
                    "detail": _detail(call.detail),
                    "started": call.started,
                    "duration_ms": duration,
                    "status": call.status,
                }
            )
            del agent["tools"][:-MAX_TOOL_HISTORY]
            agent["active_tools"].pop(call.id, None)
            latest = max(
                agent["active_tools"].values(), key=lambda t: t["started"], default=None
            )
            agent["current_tool"] = latest["name"] if latest else None
            agent["current_detail"] = latest["detail"] if latest else None

        self._update(change)

    @_safe
    def finish(self, status: str = "completed", note: str = "") -> None:
        """Mark the agent finished (idempotent)."""

        def change(agent):
            if agent["status"] == "running":
                agent.update(
                    status=status,
                    ended=time.time(),
                    note=_detail(note),
                    current_tool=None,
                    current_detail=None,
                    active_tools={},
                )

        self._update(change)

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace):
        self.finish(
            "failed" if error is not None else "completed",
            type(error).__name__ if error else "",
        )
        return False


class TeamHandle:
    """A group of agents working on one request (a chat turn or a research batch)."""

    def __init__(self, tracker: ActivityTracker, team_id: str, owner: str):
        self._tracker, self.id, self.owner = tracker, team_id, owner

    def agent(
        self, name: str, role: str = "agent", group: str = "", model: str | None = None
    ) -> AgentHandle:
        """Register an agent as running now."""
        agent_id = self._tracker._add_agent(self.id, name, role, group, model)
        return AgentHandle(self._tracker, self.id, agent_id)

    def scope(self, group: str = "") -> Scope:
        """A view that creates agents under one group label (e.g. one company)."""
        return Scope(self, group)

    @_safe
    def finish(self, status: str | None = None) -> None:
        self._tracker._finish_team(self.id, status)


class Scope:
    """``team`` + ``group`` so deep code can add agents without knowing either."""

    def __init__(self, team: TeamHandle, group: str = ""):
        self.team, self.group = team, group

    def agent(
        self, name: str, role: str = "agent", model: str | None = None
    ) -> AgentHandle:
        return self.team.agent(name, role, self.group, model)


class _NullAgent:
    def llm(self, *args, **kwargs): ...
    def finish(self, *args, **kwargs): ...
    def tool(self, name, detail=""):
        return _NullTool()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _NullTool:
    def fail(self): ...
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NullScope:
    """Used when nobody is watching: every call is a no-op."""

    group = ""

    def agent(self, *args, **kwargs):
        return _NullAgent()


NULL_SCOPE = NullScope()


def _detail(text: Any) -> str:
    text = str(redact(str(text or "")))
    return text if len(text) <= MAX_DETAIL else text[:MAX_DETAIL] + "…"


class ActivityTracker:
    """Thread-safe, bounded, per-owner store of team/agent activity."""

    def __init__(self):
        self._lock = threading.RLock()
        self._teams: OrderedDict[str, dict] = OrderedDict()
        self._ids = itertools.count(1)
        self.version = 0

    # -- writes ---------------------------------------------------------------------
    @_safe
    def start_team(
        self, owner: str, kind: str, title: str, parent: str | None = None
    ) -> TeamHandle:
        team_id = f"t{next(self._ids)}"
        with self._lock:
            self._teams[team_id] = {
                "id": team_id,
                "owner": (owner or "local").lower(),
                "kind": kind,
                "title": _detail(title),
                "parent": parent,
                "status": "running",
                "started": time.time(),
                "ended": None,
                "agents": OrderedDict(),
            }
            self._trim()
            self.version += 1
        return TeamHandle(self, team_id, (owner or "local").lower())

    def _trim(self) -> None:
        while len(self._teams) > MAX_TEAMS:
            victim = next(
                (k for k, t in self._teams.items() if t["status"] != "running"), None
            )
            if victim is None:
                break
            del self._teams[victim]

    def _add_agent(
        self, team_id: str, name: str, role: str, group: str, model: str | None
    ) -> str:
        with self._lock:
            team = self._teams.get(team_id)
            agent_id = f"{team_id}a{next(self._ids)}"
            if team is not None:
                team["agents"][agent_id] = {
                    "id": agent_id,
                    "name": name,
                    "role": role,
                    "group": group,
                    "model": model,
                    "status": "running",
                    "started": time.time(),
                    "ended": None,
                    "current_tool": None,
                    "current_detail": None,
                    "note": "",
                    "tools": [],
                    "tool_counts": {},
                    "active_tools": {},
                    "llm": {
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                        "total_ms": 0.0,
                        "last_ms": 0.0,
                    },
                }
                self.version += 1
        return agent_id

    def _mutate(self, team_id: str, agent_id: str, change) -> None:
        with self._lock:
            agent = self._teams.get(team_id, {}).get("agents", {}).get(agent_id)
            if agent is not None:
                change(agent)
                self.version += 1

    def _finish_team(self, team_id: str, status: str | None) -> None:
        with self._lock:
            team = self._teams.get(team_id)
            if team is None or team["status"] != "running":
                return
            for agent in team["agents"].values():
                if agent["status"] == "running":
                    agent.update(
                        status="interrupted",
                        ended=time.time(),
                        current_tool=None,
                        current_detail=None,
                        active_tools={},
                    )
            failed = any(a["status"] == "failed" for a in team["agents"].values())
            team.update(
                status=status or ("failed" if failed else "completed"),
                ended=time.time(),
            )
            self.version += 1

    def clear_finished(self, owner: str) -> int:
        owner = owner.lower()
        with self._lock:
            gone = [
                k
                for k, t in self._teams.items()
                if t["owner"] == owner and t["status"] != "running"
            ]
            for key in gone:
                del self._teams[key]
            self.version += bool(gone)
        return len(gone)

    # -- reads ------------------------------------------------------------------------
    def snapshot(self, owner: str) -> dict:
        """JSON-ready state of this owner's teams, newest first, with totals."""
        owner, now = owner.lower(), time.time()
        with self._lock:
            for key in [
                k
                for k, t in self._teams.items()
                if t["status"] != "running"
                and now - (t["ended"] or now) > KEEP_FINISHED_SECONDS
            ]:
                del self._teams[key]
            teams = [
                self._view(t, now)
                for t in reversed(self._teams.values())
                if t["owner"] == owner
            ]
            version = self.version
        agents = [a for t in teams for a in t["agents"]]
        calls = [a["llm"] for a in agents]
        total_calls = sum(c["calls"] for c in calls)
        return {
            "version": version,
            "now": now,
            "totals": {
                "teams": len(teams),
                "teams_running": sum(t["status"] == "running" for t in teams),
                "agents": len(agents),
                "agents_running": sum(a["status"] == "running" for a in agents),
                "tool_calls": sum(
                    sum(v["calls"] for v in a["tool_counts"].values()) for a in agents
                ),
                "llm_calls": total_calls,
                "cost_usd": sum(c["cost_usd"] for c in calls),
                "avg_llm_ms": (sum(c["total_ms"] for c in calls) / total_calls)
                if total_calls
                else 0.0,
            },
            "teams": teams,
        }

    @staticmethod
    def _view(team: dict, now: float) -> dict:
        agents = []
        for agent in team["agents"].values():
            llm = dict(agent["llm"])
            llm["avg_ms"] = llm["total_ms"] / llm["calls"] if llm["calls"] else 0.0
            tool_counts = {
                name: {
                    **stats,
                    "avg_ms": stats["total_ms"] / stats["calls"]
                    if stats["calls"]
                    else 0.0,
                }
                for name, stats in agent["tool_counts"].items()
            }
            agents.append(
                {
                    **agent,
                    "llm": llm,
                    "tool_counts": tool_counts,
                    "tools": list(agent["tools"][-10:]),
                    "active_tools": [
                        {**t, "elapsed_ms": (now - t["started"]) * 1000}
                        for t in sorted(
                            agent["active_tools"].values(), key=lambda t: t["started"]
                        )
                    ],
                    "elapsed_ms": ((agent["ended"] or now) - agent["started"]) * 1000,
                }
            )
        cost = sum(a["llm"]["cost_usd"] for a in agents)
        return {
            **{k: v for k, v in team.items() if k not in {"agents", "owner"}},
            "elapsed_ms": ((team["ended"] or now) - team["started"]) * 1000,
            "cost_usd": cost,
            "agents": agents,
        }


tracker = ActivityTracker()
current_team: contextvars.ContextVar[TeamHandle | None] = contextvars.ContextVar(
    "current_team", default=None
)
