"""
Google Calendar Tools - List events and find free time slots (read-only).
"""

from datetime import date

from src.registry.models import ToolDefinition
from src.configs.tool_loader import tool_config
from src.services import calendar_service


# ============================================================
# Tool 1: LIST CALENDAR EVENTS
# ============================================================

def list_calendar_events(start_date: str = "", days: int = 1) -> dict:
    """List calendar events starting from start_date for `days` days."""
    start_date = start_date or date.today().isoformat()
    events = calendar_service.list_events(start_date, days=days)
    return {"start_date": start_date, "days": days, "total_events": len(events), "events": events}


list_events_tool = ToolDefinition(
    **tool_config("list_calendar_events"),
    handler=list_calendar_events,
)


# ============================================================
# Tool 2: FIND FREE SLOTS
# ============================================================

def find_free_slots(
    start_date: str = "",
    end_date: str = "",
    duration_minutes: int = 30,
    work_start: str = "09:00",
    work_end: str = "18:00",
    skip_weekends: bool = True,
) -> dict:
    """Find free time slots in the user's calendar within working hours."""
    start_date = start_date or date.today().isoformat()
    days = calendar_service.find_free_slots(
        start_date=start_date,
        end_date=end_date or None,
        duration_minutes=duration_minutes,
        work_start=work_start,
        work_end=work_end,
        skip_weekends=skip_weekends,
    )
    return {
        "timezone": calendar_service.CALENDAR_TIMEZONE,
        "duration_minutes": duration_minutes,
        "working_hours": f"{work_start}-{work_end}",
        "days": days,
    }


find_free_slots_tool = ToolDefinition(
    **tool_config("find_free_slots"),
    handler=find_free_slots,
)


ALL_CALENDAR_TOOLS = [list_events_tool, find_free_slots_tool]
