"""Intermediate, validated models used while a batch is being processed."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agents.email_agent import EmailAgent
from src.agents.web_research_agent import WebResearchAgent
from src.schemas.errors import ResearchError
from src.schemas.research import CompanySelection, Meeting, ResearchRequest

EMAIL_EXCERPT_CHARS = 1500


class WorkflowModel(BaseModel):
    """Base for models that carry live objects (agents, reasoners, exceptions)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ResolvedEmail(WorkflowModel):
    """Outcome of resolving one e-mail to a company."""

    message: dict[str, Any]
    company: dict[str, Any] | None = None
    candidates: list[str] = Field(default_factory=list)
    meeting: Meeting | None = None
    clarification: str = ""
    errors: list[dict[str, Any]] = Field(default_factory=list)
    discovery_errors: list[dict[str, Any]] = Field(default_factory=list)
    wants_meeting: bool = False
    reasoner: Any = None
    error: ResearchError | None = None
    deadline_exceeded: bool = False


class MeetingRequest(BaseModel):
    """One e-mail of a company and whether it asks for a meeting."""

    message: dict[str, Any]
    meeting: Meeting | None = None
    requested: bool = False


class CompanyGroup(WorkflowModel):
    """All e-mails that belong to one company (identified by its domain)."""

    company: dict[str, Any]
    reasoner: Any
    messages: list[dict[str, Any]] = Field(default_factory=list)
    meetings: list[MeetingRequest] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def meeting_requests(self) -> list[MeetingRequest]:
        """E-mails that ask for a meeting; only these trigger the calendar check."""
        return [item for item in self.meetings if item.requested]

    def add(self, resolved: ResolvedEmail) -> None:
        """Merge one resolved e-mail into the group."""
        message = resolved.message
        self.messages.append(message)
        self.meetings.append(
            MeetingRequest(
                message=message,
                meeting=resolved.meeting,
                requested=resolved.wants_meeting,
            )
        )
        self.errors.extend(resolved.errors)
        if resolved.clarification:
            self.errors.append(
                {"code": "MEETING_AMBIGUOUS", "message": resolved.clarification}
            )
        if message.get("attachment_notice"):
            self.errors.append(
                {
                    "code": "ATTACHMENT_NOT_PROVIDED",
                    "message": message["attachment_notice"],
                }
            )
        if message.get("body_truncated"):
            self.errors.append(
                {
                    "code": "EMAIL_TRUNCATED",
                    "message": "Một email bị cắt bớt nội dung; kết quả có thể thiếu ngữ cảnh.",
                }
            )

    def email_excerpts(self) -> list[dict[str, str]]:
        """Untrusted e-mail context handed to the research agents."""
        return [
            {
                key: m.get(key, "")[:EMAIL_EXCERPT_CHARS]
                for key in ("from", "subject", "body")
            }
            for m in self.messages
        ]


class BatchContext(WorkflowModel):
    """Per-batch values shared by the e-mail resolution workers."""

    request: ResearchRequest
    now: datetime
    deadline: float
    backend: Any
    email_agent: EmailAgent
    web_agent: WebResearchAgent
    selections: dict[str, CompanySelection]
    reasoners: dict[str, Any] = Field(default_factory=dict)
    team: Any = None  # live-activity team (None when not tracked)
    email_watcher: Any = None  # activity handle of the e-mail stage


class ResearchOutcome(BaseModel):
    """What the research stage produced for one company."""

    record: dict[str, Any]
    dossier: dict[str, Any] | None = None
    reused: bool = False
