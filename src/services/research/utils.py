"""Small helpers shared by the research services."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> str:
    """Current UTC time as an ISO string."""
    return datetime.now(UTC).isoformat()


def canonical(value) -> str:
    """Stable JSON used for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value) -> str:
    """SHA-256 of the canonical JSON of a value."""
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def next_week(now: datetime, timezone_name: str):
    """Start and end of the next Monday-to-Monday week in the given timezone."""
    local = now.astimezone(ZoneInfo(timezone_name))
    start = (local + timedelta(days=7 - local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(days=7)
