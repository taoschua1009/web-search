"""Live agent activity: the Agents page, a JSON snapshot and a Server-Sent-Events stream.

Everything is scoped to the signed-in person; nobody can see another person's agents.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from src.api.research_routes import browser_write
from src.configs.config import FRONTEND_DIR
from src.observability.activity import ActivityTracker, tracker
from src.services.research import runtime as rt

router = APIRouter()
POLL_SECONDS = 0.3
HEARTBEAT_SECONDS = 15


@router.get("/agents", response_class=HTMLResponse)
def agents_page():
    """The live agents monitor."""
    return HTMLResponse((FRONTEND_DIR / "agents.html").read_text(encoding="utf-8"))


@router.get("/assets/agents.css")
def agents_styles():
    return FileResponse(FRONTEND_DIR / "assets" / "agents.css", media_type="text/css")


@router.get("/assets/agents.js")
def agents_script():
    return FileResponse(
        FRONTEND_DIR / "assets" / "agents.js", media_type="application/javascript"
    )


@router.get("/api/activity")
def activity_snapshot(request: Request):
    """Current teams and agents of the signed-in person."""
    return tracker.snapshot(rt.owner(request))


@router.post("/api/activity/clear")
def clear_finished(request: Request):
    """Forget this person's finished teams."""
    browser_write(request)
    return {"cleared": tracker.clear_finished(rt.owner(request))}


async def event_stream(
    source: ActivityTracker,
    owner: str,
    is_disconnected: Callable[[], Awaitable[bool]],
    poll: float = POLL_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """SSE frames: a snapshot whenever something changed, a comment as keep-alive.

    Elapsed times are sent as of the snapshot, so a running agent's clock is advanced by
    the browser between frames; a frame is also sent once a second while anything runs.
    """
    last_version, last_sent, idle = -1, 0.0, 0.0
    while not await is_disconnected():
        snapshot = source.snapshot(owner)
        running = (
            snapshot["totals"]["agents_running"] or snapshot["totals"]["teams_running"]
        )
        if snapshot["version"] != last_version or (
            running and snapshot["now"] - last_sent >= 1.0
        ):
            last_version, last_sent, idle = snapshot["version"], snapshot["now"], 0.0
            yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
        elif idle >= heartbeat:
            idle = 0.0
            yield ": keep-alive\n\n"
        await asyncio.sleep(poll)
        idle += poll


@router.get("/api/activity/stream")
async def activity_stream(request: Request):
    """Real-time updates for the Agents page."""
    owner = rt.owner(request)
    return StreamingResponse(
        event_stream(tracker, owner, request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
