"""The cited company dossier and the section outputs that specialists submit for it."""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class Loose(BaseModel):
    """Base for model-produced data: unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")


class Cited(Loose):
    """A statement with the URLs and optional verbatim evidence behind it."""

    text: str
    urls: list[str] = Field(default_factory=list)
    evidence: str = Field(
        default="", description="Optional verbatim excerpt of the cited page."
    )
    estimated: bool = Field(
        default=False, description="True when the figure is inferred; say how in text."
    )


class Kpi(Loose):
    """One headline figure for the overview tiles."""

    value: str
    unit: str = ""
    label: str
    urls: list[str] = Field(default_factory=list)
    estimated: bool = False


class KeyValue(Loose):
    """A labelled profile row."""

    label: str
    value: str
    urls: list[str] = Field(default_factory=list)


class Opening(Loose):
    """An open job position."""

    title: str
    kind: str = ""
    urls: list[str] = Field(default_factory=list)


class Team(Loose):
    """Team size, signals, culture and open positions."""

    size: Cited | None = None
    signals: list[Cited] = Field(default_factory=list)
    culture: Cited | None = None
    openings: list[Opening] = Field(default_factory=list)
    hiring_note: Cited | None = None


class Relation(Loose):
    """A notable person or organisation connected to a founder."""

    name: str
    detail: str = ""
    urls: list[str] = Field(default_factory=list)


class DigitalProfile(Loose):
    """A public profile (LinkedIn, GitHub...) with its stated numbers."""

    platform: str
    handle: str = ""
    detail: str = ""
    link: str = ""
    urls: list[str] = Field(default_factory=list)


class Founder(Loose):
    """A founder or key person with achievements, network and profiles."""

    name: str
    role: str = ""
    subtitle: str = ""
    achievements: list[Cited] = Field(default_factory=list)
    network: list[Relation] = Field(default_factory=list)
    profiles: list[DigitalProfile] = Field(default_factory=list)
    style: Cited | None = None
    strength: Cited | None = None


class Price(Loose):
    """One price point of a product."""

    label: str
    value: str
    note: str = ""


class Product(Loose):
    """A product or service line with its prices."""

    name: str
    tag: str = ""
    price_visibility: Literal["public", "not_public", "free", "unknown"] = "unknown"
    summary: str = ""
    bullets: list[str] = Field(default_factory=list)
    prices: list[Price] = Field(default_factory=list)
    callout: str = ""
    urls: list[str] = Field(default_factory=list)


class NewsItem(Loose):
    """A recent news item."""

    text: str
    date: str = Field(default="", description="ISO date YYYY-MM-DD when known.")
    urls: list[str] = Field(default_factory=list)


class Risk(Loose):
    """An adverse finding with its status."""

    text: str
    status: Literal[
        "source_statement", "allegation", "investigation", "concluded_finding"
    ] = "source_statement"
    urls: list[str] = Field(default_factory=list)


class Headline(Loose):
    """Identity and headline facts of the company."""

    name: str
    tagline: str = ""
    status: str = ""
    hq: str = ""
    industry: str = ""
    size_label: str = ""
    website: str = Field(default="", description="Official website you verified.")
    identity_note: str = Field(
        default="",
        description="How the company identity was verified or why a candidate was rejected.",
    )


class Dossier(Loose):
    """The complete cited company dossier."""

    headline: Headline
    summary: Cited | None = None
    kpis: list[Kpi] = Field(default_factory=list)
    profile: list[KeyValue] = Field(default_factory=list)
    team: Team = Field(default_factory=Team)
    founders: list[Founder] = Field(default_factory=list)
    products: list[Product] = Field(default_factory=list)
    customers_partners: list[Cited] = Field(default_factory=list)
    financials: list[Cited] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    risk_note: str = ""
    meeting_prep: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class Extras(Loose):
    """Fields every specialist may add: KPIs, meeting prep, conflicts and gaps."""

    kpis: list[Kpi] = []
    meeting_prep: list[str] = []
    conflicts: list[str] = []
    gaps: list[str] = []


class ProfileOut(Extras):
    """Company profile section."""

    headline: Headline
    summary: Cited | None = None
    profile: list[KeyValue] = []


class TeamOut(Extras):
    """Team and hiring section."""

    team: Team = Team()


class FoundersOut(Extras):
    """Founders and key people section."""

    founders: list[Founder] = []


class ProductsOut(Extras):
    """Products and prices section."""

    products: list[Product] = []


class MarketOut(Extras):
    """Customers, partners and financials section."""

    customers_partners: list[Cited] = []
    financials: list[Cited] = []


class SignalsOut(Extras):
    """News and risks section."""

    news: list[NewsItem] = []
    risks: list[Risk] = []
    risk_note: str = ""
