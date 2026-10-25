"""Calendar role: next-week availability and meeting assessment (read-only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from src.schemas.errors import ResearchError, error_view
from src.services.research.utils import next_week, utcnow


class CalendarAgent:
    """Read-only calendar role: availability and meeting-conflict assessment."""

    name = "calendar"

    def __init__(self, backend, settings):
        self.backend, self.settings = backend, settings

    @staticmethod
    def free_windows(start, end, busy, work_start, work_end):
        """Free working-hour windows on weekdays between the busy intervals."""
        windows, day = [], start
        while day < end:
            if day.weekday() < 5:
                left = day.replace(hour=work_start)
                right = (
                    (day + timedelta(days=1)).replace(hour=0)
                    if work_end == 24
                    else day.replace(hour=work_end)
                )
                cursor = left
                for a, b in sorted(busy):
                    if b <= cursor or a >= right:
                        continue
                    if a > cursor:
                        windows.append((cursor, min(a, right)))
                    cursor = max(cursor, b)
                if cursor < right:
                    windows.append((cursor, right))
            day += timedelta(days=1)
        return windows

    def not_requested(self, timezone_name):
        """Placeholder snapshot used when no e-mail asked for a meeting."""
        return {
            "status": "not_requested",
            "demo": self.backend.mode == "demo",
            "start": None,
            "end": None,
            "timezone": timezone_name,
            "checked_at": utcnow(),
            "free_windows": [],
            "candidate_slots": [],
            "busy_count": None,
            "message": "Email không yêu cầu lên lịch họp nên chưa kiểm tra lịch.",
        }

    def snapshot(self, now, timezone_name, duration):
        """Next-week availability; an unreadable calendar is reported, never assumed free."""
        start, end = next_week(now, timezone_name)
        base = {
            "status": "unavailable",
            "demo": self.backend.mode == "demo",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": timezone_name,
            "checked_at": utcnow(),
            "free_windows": [],
            "candidate_slots": [],
            "busy_count": None,
        }
        try:
            busy = self.backend.busy(start, end, timezone_name)
            windows = self.free_windows(
                start, end, busy, self.settings.work_start, self.settings.work_end
            )
            slots = []
            for a, b in windows:
                if b - a >= timedelta(minutes=duration):
                    slots.append(
                        {
                            "start": a.isoformat(),
                            "end": (a + timedelta(minutes=duration)).isoformat(),
                        }
                    )
            base.update(
                status="checked",
                busy_count=len(busy),
                free_windows=[
                    {"start": a.isoformat(), "end": b.isoformat()} for a, b in windows
                ],
                candidate_slots=slots[:3],
            )
        except ResearchError as error:
            base["error"] = error_view(error)
        return base

    def assess(self, message, meeting, snapshot, timezone_name):
        """Compare a proposed meeting with the calendar and draft alternative slots on conflict."""
        value = {
            "status": "needs_clarification",
            "message_id": message["id"],
            "proposal": meeting.model_dump(mode="json") if meeting else None,
            "alternatives": [],
            "checked_at": utcnow(),
            "draft": None,
        }
        if not meeting:
            value["message"] = (
                "Chưa có thời gian hẹn cụ thể; xem các khoảng trống tuần sau hoặc bổ sung ngày/giờ."
            )
            return value
        if meeting.end <= datetime.now(UTC):
            value["message"] = "Thời gian hẹn đã qua; cần xác nhận lại."
            return value
        try:
            events = self.backend.events(meeting.start, meeting.end, timezone_name)
            sender = parseaddr(message.get("from", ""))[1].lower()
            for event in events:
                start, end = (
                    event.get("start", {}).get("dateTime"),
                    event.get("end", {}).get("dateTime"),
                )
                if not start or not end:
                    continue
                participants = {
                    item.get("email", "").lower() for item in event.get("attendees", [])
                }
                if (
                    datetime.fromisoformat(start) == meeting.start
                    and datetime.fromisoformat(end) == meeting.end
                    and sender in participants
                ):
                    value.update(
                        status="already_scheduled",
                        event_id=event["id"],
                        message="Đã có lịch khớp thời gian và người tham gia.",
                    )
                    return value
            busy = self.backend.busy(meeting.start, meeting.end, timezone_name)
            conflict = any(a < meeting.end and b > meeting.start for a, b in busy)
            value["status"] = "conflict" if conflict else "available"
            value["message"] = (
                "Lịch hẹn trùng lịch hiện có."
                if conflict
                else "Khoảng hẹn hiện đang trống; chưa được giữ chỗ."
            )
            if conflict:
                duration = meeting.end - meeting.start
                windows = (
                    snapshot.get("free_windows", [])
                    if snapshot["status"] == "checked"
                    else []
                )
                slots = [
                    {
                        "start": window["start"],
                        "end": (
                            datetime.fromisoformat(window["start"]) + duration
                        ).isoformat(),
                    }
                    for window in windows
                    if datetime.fromisoformat(window["end"])
                    - datetime.fromisoformat(window["start"])
                    >= duration
                ][:3]
                value["alternatives"] = slots
                if slots and sender:
                    tz = ZoneInfo(timezone_name)
                    formatted = [
                        f"- {datetime.fromisoformat(slot['start']).astimezone(tz):%d/%m/%Y %H:%M}–{datetime.fromisoformat(slot['end']).astimezone(tz):%H:%M} ({timezone_name})"
                        for slot in slots
                    ]
                    value["draft"] = {
                        "to": [sender],
                        "subject": "Re: "
                        + message.get("subject", "Trao đổi lịch hẹn").removeprefix(
                            "Re: "
                        ),
                        "body": "Chào anh/chị,\n\nThời gian đề xuất hiện trùng lịch của tôi. Anh/chị có thể chọn một trong các khung giờ sau không?\n\n"
                        + "\n".join(formatted)
                        + "\n\nXin cảm ơn.",
                    }
        except ResearchError as error:
            value.update(
                status="unavailable",
                error=error_view(error),
                message="Chưa kiểm tra được lịch, không thể kết luận đang trống.",
            )
        return value

    def recheck(self, slots, timezone_name):
        """Raise ACTION_CHANGED when a proposed slot is past or no longer free."""
        for slot in slots:
            start, end = (
                datetime.fromisoformat(slot["start"]),
                datetime.fromisoformat(slot["end"]),
            )
            if start <= datetime.now(UTC):
                raise ResearchError(
                    "ACTION_CHANGED", "Giờ đề xuất đã qua. Hãy tạo bản nháp mới.", 409
                )
            if any(
                a < end and b > start
                for a, b in self.backend.busy(start, end, timezone_name)
            ):
                raise ResearchError(
                    "ACTION_CHANGED",
                    "Lịch đã thay đổi. Hãy tạo và duyệt bản nháp với giờ khác.",
                    409,
                )
