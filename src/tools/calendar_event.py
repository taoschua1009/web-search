"""Chat tools that *propose* creating or deleting a calendar event.

Nothing is written to Google Calendar here. The proposal waits in the approval queue
(and as a card in the chat); the change happens only when the user approves its exact
contents.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from src.configs.config import CALENDAR_TIMEZONE
from src.configs.tool_loader import tool_config
from src.registry.models import ToolDefinition
from src.schemas.research import EventDeletion, EventPayload
from src.services import calendar_service
from src.services.gmail_service import request_credentials
from src.services.research import runtime as rt
from src.services.research.backends.live import CALENDAR_EVENTS
from src.tools.research import current_chat_context

APPROVAL_URL = "/research?view=approvals"
CONNECT_URL = "/auth/login?research=1"
CONNECT_HINT = (
    "Mở /auth/login?research=1 (hoặc nút “Kết nối quyền Gmail + Calendar” ở /research), "
    "đăng nhập lại và cấp quyền sự kiện Calendar, rồi thử lại."
)


def _permission_missing(context) -> dict:
    """Tool result for a missing Calendar write permission, plus a connect card in chat."""
    context.actions.append(
        {
            "kind": "connect_google",
            "url": CONNECT_URL,
            "title": "Kết nối quyền Google Calendar",
        }
    )
    return {
        "code": "CALENDAR_WRITE_PERMISSION_REQUIRED",
        "message": "Tài khoản chưa cấp quyền sự kiện Calendar. " + CONNECT_HINT,
    }


def _aware(text: str, zone: ZoneInfo) -> datetime:
    """Parse an ISO time; a time without an offset is read in the user's timezone."""
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=zone)


SLOT_STEP = timedelta(minutes=30)
DAY_START, DAY_END = time(7, 0), time(23, 0)  # sensible hours to suggest
ALTERNATIVES = 3


def _now(zone: ZoneInfo) -> datetime:
    return datetime.now(zone)


def _busy(payload: EventPayload) -> list[tuple[datetime, datetime, str]] | None:
    """Timed events around the proposal (its days plus two more), None when unreadable."""
    zone = ZoneInfo(payload.timezone)
    first = payload.start.astimezone(zone).date()
    days = (payload.end.astimezone(zone).date() - first).days + 3
    try:
        events = calendar_service.list_events(
            first.isoformat(), days=days, tz_name=payload.timezone
        )
    except PermissionError:
        return None
    return [
        (
            datetime.fromisoformat(event["start"]),
            datetime.fromisoformat(event["end"]),
            event["title"],
        )
        for event in events
        if not event["all_day"] and event.get("status") != "cancelled"
    ]


def _overlapping(busy, start: datetime, end: datetime) -> list[tuple]:
    return [item for item in busy if item[0] < end and item[1] > start]


def _conflicts(busy, payload: EventPayload) -> list[dict] | None:
    """Existing events that overlap the proposal; None when the calendar was unreadable."""
    if busy is None:
        return None
    return [
        {"title": title, "start": start.isoformat(), "end": end.isoformat()}
        for start, end, title in _overlapping(busy, payload.start, payload.end)
    ]


def _alternatives(busy, payload: EventPayload) -> list[dict]:
    """Free slots of the same length: nearest times that day, then the same time later days."""
    zone = ZoneInfo(payload.timezone)
    length, now = payload.end - payload.start, _now(zone)
    start = payload.start.astimezone(zone)
    candidates = []
    for day in range(3):
        base = start + timedelta(days=day)
        for steps in range(0, 25):  # up to 12 hours either side, nearest first
            for sign in (1, -1) if steps else (1,):
                begin = base + sign * steps * SLOT_STEP
                end = begin + length
                if (
                    begin <= now
                    or begin.timetz().replace(tzinfo=None) < DAY_START
                    or end.date() != begin.date()
                    or end.timetz().replace(tzinfo=None) > DAY_END
                    or (day == 0 and steps == 0)  # the requested time itself
                    or _overlapping(busy, begin, end)
                ):
                    continue
                candidates.append((day, steps, begin, end))
    chosen: list[tuple] = []
    for day, steps, begin, end in sorted(candidates, key=lambda c: (c[0], c[1], c[2])):
        if all(abs(begin - other[2]) >= 2 * SLOT_STEP for other in chosen):
            chosen.append((day, steps, begin, end))
        if len(chosen) == ALTERNATIVES:
            break
    return [
        {"start": begin.isoformat(), "end": end.isoformat()}
        for _, _, begin, end in sorted(chosen, key=lambda c: c[2])
    ]


def propose_calendar_event(
    title: str,
    start: str,
    end: str,
    timezone: str = "",
    description: str = "",
    location: str = "",
) -> dict:
    """Queue an event for the user's approval and report any overlapping events."""
    context = current_chat_context()
    if context is None:
        return {
            "code": "WEB_LOGIN_REQUIRED",
            "message": "Mở /chat và đăng nhập Google để tạo sự kiện lịch.",
        }
    credentials = request_credentials.get()
    if credentials is None or not credentials.has_scopes([CALENDAR_EVENTS]):
        return _permission_missing(context)
    zone_name = timezone or CALENDAR_TIMEZONE
    try:
        zone = ZoneInfo(zone_name)
        payload = EventPayload(
            title=title,
            start=_aware(start, zone),
            end=_aware(end, zone),
            timezone=zone_name,
            description=description,
            location=location,
        )
    except (ValidationError, ValueError, ZoneInfoNotFoundError):
        return {
            "code": "INVALID_INPUT",
            "message": "Cần tiêu đề, giờ bắt đầu/kết thúc hợp lệ (ISO 8601, 5 phút đến 24 giờ) và múi giờ đúng.",
        }
    busy = _busy(payload)
    conflicts = _conflicts(busy, payload)
    alternatives = _alternatives(busy, payload) if conflicts else []
    settings, store = rt.runtime()
    owner = rt.owner(context.request)
    action = store.propose(
        owner,
        "create_event",
        payload.model_dump(mode="json"),
        {"mode": "live", "mailbox": owner, "timezone": zone_name, "calendar_slots": []},
        settings.approval_seconds,
    )
    # Everything the chat needs to show an exact-event approval card inline.
    context.actions.append(
        {
            "id": action["id"],
            "kind": "create_event",
            "url": APPROVAL_URL,
            "title": payload.title,
            "payload": action["payload"],
            "version": action["version"],
            "payload_hash": action["payload_hash"],
            "conflicts": conflicts,
            "alternatives": alternatives,
        }
    )
    return {
        "status": "awaiting_user_approval",
        "created": False,
        "action_id": action["id"],
        "approval_url": APPROVAL_URL,
        "event": payload.model_dump(mode="json"),
        "conflicts": conflicts,
        "alternatives": alternatives,
        "message": (
            "Sự kiện CHƯA được tạo: đang chờ người dùng duyệt trong hàng chờ phê duyệt. "
            + (
                "Chưa kiểm tra được trùng lịch."
                if conflicts is None
                else f"TRÙNG {len(conflicts)} sự kiện đã có: cảnh báo rõ và đề nghị chọn một khung giờ khác trong `alternatives` (nếu có)."
                if conflicts
                else "Không trùng sự kiện nào."
            )
        ),
    }


def propose_calendar_event_deletion(event_id: str) -> dict:
    """Queue deleting one existing event for the user's approval."""
    context = current_chat_context()
    if context is None:
        return {
            "code": "WEB_LOGIN_REQUIRED",
            "message": "Mở /chat và đăng nhập Google để xóa sự kiện lịch.",
        }
    credentials = request_credentials.get()
    if credentials is None or not credentials.has_scopes([CALENDAR_EVENTS]):
        return _permission_missing(context)
    try:
        event = calendar_service.get_event(event_id.strip())
    except PermissionError:
        return _permission_missing(context)
    except Exception:  # noqa: BLE001 - provider details never reach the model
        return {
            "code": "CALENDAR_UNAVAILABLE",
            "message": "Không đọc được sự kiện từ Google Calendar. Thử lại sau.",
        }
    if event is None:
        return {
            "code": "EVENT_NOT_FOUND",
            "message": "Không tìm thấy sự kiện (có thể đã bị xóa). Dùng list_calendar_events để lấy đúng id.",
        }
    try:
        payload = EventDeletion(
            event_id=event["id"],
            title=event["title"],
            start=event["start"],
            end=event["end"],
            all_day=event["all_day"],
            attendee_count=event["attendee_count"],
        )
    except ValidationError:
        return {
            "code": "INVALID_INPUT",
            "message": "Sự kiện này không xóa được qua chat.",
        }
    settings, store = rt.runtime()
    owner = rt.owner(context.request)
    action = store.propose(
        owner,
        "delete_event",
        payload.model_dump(mode="json"),
        {"mode": "live", "mailbox": owner, "calendar_slots": []},
        settings.approval_seconds,
    )
    context.actions.append(
        {
            "id": action["id"],
            "kind": "delete_event",
            "url": APPROVAL_URL,
            "title": payload.title,
            "payload": action["payload"],
            "version": action["version"],
            "payload_hash": action["payload_hash"],
        }
    )
    return {
        "status": "awaiting_user_approval",
        "deleted": False,
        "action_id": action["id"],
        "approval_url": APPROVAL_URL,
        "event": action["payload"],
        "message": (
            "Sự kiện CHƯA bị xóa: đang chờ người dùng duyệt. "
            + (
                f"Sự kiện có {payload.attendee_count} người tham dự; nêu rõ cho người dùng."
                if payload.attendee_count
                else ""
            )
        ),
    }


ALL_CALENDAR_EVENT_TOOLS = [
    ToolDefinition(
        **tool_config("propose_calendar_event"), handler=propose_calendar_event
    ),
    ToolDefinition(
        **tool_config("propose_calendar_event_deletion"),
        handler=propose_calendar_event_deletion,
    ),
]
