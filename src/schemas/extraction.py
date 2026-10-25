"""Structured outputs requested from the model when it extracts evidence from text."""

from __future__ import annotations

from typing import Literal

from pydantic import (
    Field,
)

from src.schemas.research import Contract, Meeting


class ExtractedFact(Contract):
    """One fact the model extracted, with the verbatim evidence it relies on."""

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
    source_id: str
    evidence: str = Field(min_length=10, max_length=1200)
    status: Literal[
        "source_statement", "allegation", "investigation", "concluded_finding"
    ] = "source_statement"
    person: str = Field(default="", max_length=160)
    person_section: Literal["career", "relationships", "profiles"] = "career"


class ExtractedFacts(Contract):
    """Facts extracted from a batch of sources."""

    company_name: str = Field(default="", max_length=200)
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=40)


class EmailContext(Contract):
    """Company, meeting and meeting request extracted from one e-mail."""

    company_name: str = Field(default="", max_length=200)
    meeting_requested: bool = False
    meeting: Meeting | None = None
    clarification: str = Field(default="", max_length=500)


class CompanyIdentity(Contract):
    """Whether a retrieved page is the official site of the named company."""

    official: bool = False
    evidence: str = Field(default="", max_length=1200)
