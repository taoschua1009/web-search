"""
Google Calendar Service - OAuth2 (user login) wrapper for Calendar API v3.
Read-only: list events and compute free slots from free/busy data.

First-time login (opens a browser, saves token.json):
    python -m src.services.calendar_service
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.configs.config import (
    CALENDAR_TIMEZONE,
    GOOGLE_CALENDAR_ID,
    GOOGLE_OAUTH_CLIENT_FILE,
    GOOGLE_OAUTH_PORT,
    GOOGLE_TOKEN_FILE,
)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

_service = None


def login() -> str:
    """Run the OAuth2 browser flow and save the token. Run once from a terminal."""
    if not os.path.exists(GOOGLE_OAUTH_CLIENT_FILE):
        raise FileNotFoundError(
            f"OAuth client file not found: '{GOOGLE_OAUTH_CLIENT_FILE}'. "
            "Download it from Google Cloud Console > APIs & Services > Credentials."
        )
    flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_OAUTH_CLIENT_FILE, SCOPES)
    creds = flow.run_local_server(port=GOOGLE_OAUTH_PORT, prompt="consent")
    with open(GOOGLE_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    os.chmod(GOOGLE_TOKEN_FILE, 0o600)
    return f"Login successful. Token saved to {GOOGLE_TOKEN_FILE}"


def _get_credentials() -> Credentials:
    if not os.path.exists(GOOGLE_TOKEN_FILE):
        raise FileNotFoundError(
            f"Not logged in to Google Calendar ('{GOOGLE_TOKEN_FILE}' missing). "
            "Run `python -m src.services.calendar_service` once, then retry."
        )
    creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(GOOGLE_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        else:
            raise PermissionError("Google token expired. Run `python -m src.services.calendar_service` again.")
    return creds


def _get_service():
    from src.services.gmail_service import request_credentials

    browser_credentials = request_credentials.get()
    if browser_credentials is not None:
        if not browser_credentials.has_scopes(SCOPES):
            raise PermissionError(
                "Calendar permission is not granted for the signed-in Google account. Tell the user to "
                "open /auth/login?research=1 (or the \"Kết nối quyền Gmail + Calendar\" button on "
                "/research), approve Calendar access, then ask again."
            )
        # Never fall back to another user's local token in a browser request.
        return build("calendar", "v3", credentials=browser_credentials, cache_discovery=False)
    global _service
    if _service is None:
        _service = build("calendar", "v3", credentials=_get_credentials())
    return _service


def _tz(tz_name: str | None = None) -> ZoneInfo:
    return ZoneInfo(tz_name or CALENDAR_TIMEZONE)


def _day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def list_events(start_date: str, days: int = 1, tz_name: str | None = None) -> list[dict]:
    """List events from start_date (YYYY-MM-DD) for `days` days."""
    tz = _tz(tz_name)
    start = datetime.combine(date.fromisoformat(start_date), time.min, tzinfo=tz)
    end = start + timedelta(days=days)

    result = _get_service().events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        timeZone=str(tz),
    ).execute()

    events = []
    for e in result.get("items", []):
        s, en = e.get("start", {}), e.get("end", {})
        events.append({
            "id": e.get("id"),
            "title": e.get("summary", "(no title)"),
            "start": s.get("dateTime") or s.get("date"),
            "end": en.get("dateTime") or en.get("date"),
            "all_day": "date" in s,
            "location": e.get("location", ""),
            "status": e.get("status", ""),
        })
    return events


def get_event(event_id: str) -> dict | None:
    """One event of the primary calendar, or None when it does not exist (any more)."""
    try:
        event = (
            _get_service()
            .events()
            .get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id)
            .execute()
        )
    except HttpError as error:
        if error.resp.status in (404, 410):
            return None
        raise
    if event.get("status") == "cancelled":
        return None
    start, end = event.get("start", {}), event.get("end", {})
    return {
        "id": event.get("id"),
        "title": event.get("summary", "(no title)"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "date" in start,
        "attendee_count": len(event.get("attendees", [])),
    }


def _busy_intervals(start: datetime, end: datetime, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": str(tz),
        "items": [{"id": GOOGLE_CALENDAR_ID}],
    }
    resp = _get_service().freebusy().query(body=body).execute()
    calendar = resp.get("calendars", {}).get(GOOGLE_CALENDAR_ID)
    if calendar is None or calendar.get("errors"):
        raise PermissionError("Calendar free/busy is unavailable; availability is unknown.")
    busy = calendar.get("busy", [])
    out = []
    for b in busy:
        out.append((
            datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(tz),
            datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(tz),
        ))
    return sorted(out)


def find_free_slots(
    start_date: str,
    end_date: str | None = None,
    duration_minutes: int = 30,
    work_start: str = "09:00",
    work_end: str = "18:00",
    skip_weekends: bool = True,
    tz_name: str | None = None,
) -> list[dict]:
    """Return free slots (>= duration_minutes) inside working hours, per day."""
    tz = _tz(tz_name)
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date) if end_date else first
    if last < first:
        first, last = last, first

    ws = time.fromisoformat(work_start)
    we = time.fromisoformat(work_end)
    range_start, _ = _day_bounds(first, tz)
    _, range_end = _day_bounds(last, tz)
    busy = _busy_intervals(range_start, range_end, tz)

    now = datetime.now(tz)
    min_len = timedelta(minutes=duration_minutes)
    days_out = []
    d = first
    while d <= last:
        if skip_weekends and d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        win_start = max(datetime.combine(d, ws, tzinfo=tz), now)
        win_end = datetime.combine(d, we, tzinfo=tz)

        cursor, free = win_start, []
        for b_start, b_end in busy:
            if b_end <= cursor or b_start >= win_end:
                continue
            if b_start - cursor >= min_len:
                free.append((cursor, b_start))
            cursor = max(cursor, b_end)
        if win_end - cursor >= min_len:
            free.append((cursor, win_end))

        days_out.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%A"),
            "free_slots": [{"start": a.strftime("%H:%M"), "end": b.strftime("%H:%M")} for a, b in free],
        })
        d += timedelta(days=1)
    return days_out


if __name__ == "__main__":
    from argparse import ArgumentParser

    ArgumentParser(description="Connect Google Calendar with read-only OAuth access.").parse_args()
    print(login())
