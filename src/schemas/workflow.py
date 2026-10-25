"""Typed records of a research batch: what is persisted and what fast research returns.

The store keeps plain JSON; ``to_storage`` produces exactly that, so API consumers and
older rows keep working while the code that builds them is validated.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    """A stored JSON record: unknown fields are kept, assignments are validated."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    def to_storage(self) -> dict[str, Any]:
        """The JSON form written to the store (unset optional fields omitted)."""
        return self.model_dump(mode="json", exclude_none=True)


class RunRef(Record):
    """A company run as listed inside its batch."""

    id: str
    name: str
    domain: str
    message_count: int


class Unresolved(Record):
    """An e-mail whose company could not be determined and needs user input."""

    message_id: str
    subject: str = ""
    name: str = ""
    candidates: list[str] = Field(default_factory=list)
    code: str = ""
    message: str = ""


class Batch(Record):
    """One research request over several e-mails."""

    id: str
    status: str
    mode: str
    created_at: str
    runs: list[RunRef] = Field(default_factory=list)
    unresolved: list[Unresolved] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, str] | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None


class Run(Record):
    """The research of one company inside a batch."""

    id: str
    batch_id: str
    status: str = "queued"
    mode: str
    company: dict[str, Any]
    message_ids: list[str]
    created_at: str
    roles: dict[str, str]
    report_id: str | None = None
    finished_at: str | None = None
    error: dict[str, str] | None = None
    mail_context: list[dict[str, str]] | None = None


class TopicCoverage(Record):
    """Which sources were searched and read for one research topic."""

    id: str
    label: str
    query: str = ""
    searched: bool = False
    status: str = "not_searched"
    results: int = 0
    read_sources: list[str] = Field(default_factory=list)
    reason: str | None = None


class SourceRef(Record):
    """A source listed in the research scope (the text lives with the company record)."""

    id: str | None = None
    url: str | None = None
    title: str | None = None
    kind: str | None = None
    research_topics: list[str] | None = None
    retrieved_at: str | None = None


class ResearchScope(Record):
    """Summary of how a company was researched, stored with its record."""

    plan_version: int
    searched_at: str
    topics: list[TopicCoverage] = Field(default_factory=list)
    page_limit: int
    search_limit: int
    website_pages: int
    external_pages: int
    unique_domains: int
    sources: list[SourceRef] = Field(default_factory=list)
    timings: dict[str, float] | None = None
    dossier: dict[str, Any] | None = None
    limitation: str = ""


class FastResult(BaseModel):
    """Everything fast research produced for one company."""

    dossier: dict[str, Any]
    sources: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    topics: list[TopicCoverage]
    errors: list[dict[str, Any]]
    timings: dict[str, float] = Field(default_factory=dict)
    official_pages: int = 0
    search_hits: int = 0
    queries: int = 0
