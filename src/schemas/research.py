"""Validated request, knowledge and approval contracts for the research workflow."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class Contract(BaseModel):
    """Base model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


class CompanySelection(Contract):
    """A company name and website the user confirmed for one e-mail."""

    message_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    website: str = Field(min_length=1, max_length=2048)


class Meeting(Contract):
    """A proposed meeting interval with explicit timezones."""

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_interval(self):
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("Meeting times require an explicit timezone.")
        minutes = (self.end - self.start).total_seconds() / 60
        if not 5 <= minutes <= 480:
            raise ValueError("Meeting duration must be 5–480 minutes.")
        return self


class ResearchRequest(Contract):
    """A batch of e-mails to research with the user's timezone and meeting length."""

    message_ids: list[str] = Field(min_length=1, max_length=20)
    selections: list[CompanySelection] = Field(default_factory=list, max_length=20)
    timezone: str = Field(default="Asia/Ho_Chi_Minh", max_length=80)
    duration_minutes: int = Field(default=30, ge=5, le=480, strict=True)
    meeting: Meeting | None = None

    @field_validator("message_ids")
    @classmethod
    def validate_ids(cls, values):
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("Invalid message ID.")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_selections(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone.") from None
        if len({item.message_id for item in self.selections}) != len(self.selections):
            raise ValueError(
                "Only one primary company selection per message is allowed."
            )
        if any(item.message_id not in self.message_ids for item in self.selections):
            raise ValueError("Company selections must reference selected messages.")
        return self


CATEGORIES = (
    "overview",
    "industry",
    "products",
    "scale",
    "contacts",
    "news",
    "risk",
    "people",
    "pricing",
    "financials",
    "partners",
)


class Fact(Contract):
    """One sourced statement about a company."""

    id: str = Field(min_length=1, max_length=100)
    category: Literal[
        "overview",
        "industry",
        "products",
        "scale",
        "contacts",
        "news",
        "risk",
        "people",
        "pricing",
        "financials",
        "partners",
    ]
    text: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=1500)
    source_ids: list[str] = Field(default_factory=list, max_length=10)
    provenance: Literal["public", "demo", "user"] = "public"
    status: str = Field(default="source_statement", max_length=80)
    person: str = Field(default="", max_length=160)
    person_section: Literal["career", "relationships", "profiles"] = "career"


class KnowledgeEdit(Contract):
    """A user's addition to a company's stored knowledge."""

    expected_version: int = Field(ge=1, strict=True)
    category: Literal[
        "overview",
        "industry",
        "products",
        "scale",
        "contacts",
        "news",
        "risk",
        "people",
        "pricing",
        "financials",
        "partners",
    ]
    text: str = Field(min_length=1, max_length=2000)
    source_url: str = Field(default="", max_length=2048)

    @field_validator("source_url")
    @classmethod
    def safe_link(cls, value):
        if value and (
            urlsplit(value).scheme not in {"http", "https"}
            or not urlsplit(value).hostname
        ):
            raise ValueError("A source must be an HTTP(S) URL.")
        if value and (urlsplit(value).username or urlsplit(value).password):
            raise ValueError("URL credentials are not allowed.")
        return value


def validate_address(value: str) -> str:
    """Accept a bare e-mail address only: no display name, no newlines."""
    if not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}",
        value,
    ):
        raise ValueError("Use a valid email address without display names or newlines.")
    return value


class SendPayload(Contract):
    """A validated e-mail to be sent after approval."""

    to: list[str] = Field(min_length=1, max_length=10)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)

    @field_validator("to")
    @classmethod
    def validate_recipients(cls, values):
        return list(dict.fromkeys(validate_address(value) for value in values))

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value):
        if "\r" in value or "\n" in value:
            raise ValueError("Email subject must be one line.")
        return value


class ActionRequest(Contract):
    """A request to propose a send or trash action for human approval."""

    run_id: str = Field(min_length=1, max_length=100)
    message_id: str = Field(min_length=1, max_length=200)
    kind: Literal["send_email", "trash_email"]
    send: SendPayload | None = None

    @model_validator(mode="after")
    def validate_action(self):
        if (self.kind == "send_email") != (self.send is not None):
            raise ValueError("Only send actions require a send payload.")
        return self


class Decision(Contract):
    """A user's approve/reject decision bound to an exact action version and hash."""

    decision: Literal["approve", "reject"]
    version: int = Field(ge=1, strict=True)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class EventPayload(Contract):
    """A calendar event to create after approval (no attendees: nobody is invited)."""

    title: str = Field(min_length=1, max_length=200)
    start: datetime
    end: datetime
    timezone: str = Field(default="Asia/Ho_Chi_Minh", max_length=80)
    description: str = Field(default="", max_length=2000)
    location: str = Field(default="", max_length=200)

    @field_validator("title", "location")
    @classmethod
    def single_line(cls, value):
        """Titles and locations are one line."""
        if "\r" in value or "\n" in value:
            raise ValueError("Must be a single line.")
        return value.strip()

    @model_validator(mode="after")
    def validate_interval(self):
        """Explicit timezone, a known zone name, and 5 minutes to 24 hours."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("Event times require an explicit timezone.")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone.") from None
        minutes = (self.end - self.start).total_seconds() / 60
        if not 5 <= minutes <= 1440:
            raise ValueError("Event duration must be 5 minutes to 24 hours.")
        return self


class EventDeletion(Contract):
    """An existing calendar event to delete after approval, as shown to the user."""

    event_id: str = Field(min_length=1, max_length=1024)
    title: str = Field(max_length=500)
    start: str = Field(max_length=64)
    end: str = Field(max_length=64)
    all_day: bool = False
    attendee_count: int = Field(default=0, ge=0)


class ActionEdit(Contract):
    """An edit of a pending send action; creates a new version."""

    version: int = Field(ge=1, strict=True)
    send: SendPayload


REQUIRED_CATEGORIES = ("overview", "industry", "products", "scale", "contacts", "news")
