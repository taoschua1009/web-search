"""Web research role: resolves a company's official site and gathers sources."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit, urlunsplit

from src.schemas.errors import ResearchError, error_view
from src.services.research.discovery import collect_sources
from src.services.research.domains import NON_COMPANY_DOMAINS
from src.services.research.web_fetcher import domain_of


def _compact(value: str) -> str:
    """Lower-cased text without spaces or punctuation, for name comparison."""
    return re.sub(r"[^\w]", "", value.casefold())


class WebResearchAgent:
    """Resolves a company's official website and gathers topic-balanced sources."""

    name = "web_research"

    def __init__(self, backend):
        self.backend = backend

    def discover(self, name, deadline):
        """Top search results for the company's official website."""
        try:
            return self.backend.search(name, "official website", deadline)[:3]
        except ResearchError:
            return []

    def resolve_company(self, name, reasoner, deadline, max_pages=2):
        """Resolve a missing email URL only from a fetched, matching official page."""
        errors, candidates, verified = [], [], {}
        try:
            results = self.backend.search(name, "official website", deadline)[:3]
        except ResearchError as error:
            return None, [], [error_view(error)]
        candidates = [item["url"] for item in results]
        fetched = 0
        for result in results[:max_pages]:
            if time.monotonic() >= deadline:
                break
            try:
                fetched += 1
                source = self.backend.fetch(result["url"], deadline)
                domain = domain_of(source["url"])
                if any(
                    domain == value or domain.endswith("." + value)
                    for value in NON_COMPANY_DOMAINS
                ):
                    continue
                # An unrelated news/directory domain cannot become the official
                # company site merely because a model found the company name.
                brand_tokens = [
                    token
                    for token in re.findall(r"[^\W_]+", name.casefold())
                    if len(token) >= 3
                ]
                host_parts = domain.split(".")[:-1]
                if not any(
                    token in part for token in brand_tokens for part in host_parts
                ):
                    continue
                match = reasoner.company_identity(name, source, deadline)
                if (
                    not match
                    or not match.official
                    or not match.evidence
                    or match.evidence not in source["text"]
                ):
                    continue
                if _compact(name) not in _compact(match.evidence):
                    continue
                parsed = urlsplit(source["url"])
                verified[domain] = {
                    "name": name,
                    "domain": domain,
                    "website": urlunsplit((parsed.scheme, parsed.netloc, "/", "", "")),
                    "evidence": "retrieved_official_identity",
                }
            except ResearchError as error:
                errors.append(error_view(error))
        if len(verified) == 1:
            company = next(iter(verified.values()))
            company["discovery_pages"] = fetched
            return company, candidates, errors
        return None, candidates, errors

    def collect(self, company, deadline, remaining_pages):
        """Search every topic and read the best pages within the page budget."""
        sources, errors, self.topic_results = collect_sources(
            self.backend, company, deadline, remaining_pages
        )
        return sources, errors
