"""Memory role: freshness checks and persistence of company knowledge."""

from __future__ import annotations

from datetime import datetime, timedelta

from src.schemas.research import REQUIRED_CATEGORIES


def source_view(source):
    """A source without the crawl-only fields that should not be stored."""
    return {
        key: value
        for key, value in source.items()
        if key not in {"links", "fixture_facts", "fixture_people"}
    }


class MemoryAgent:
    """Stored company knowledge: freshness checks and persistence."""

    name = "memory"

    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def lookup(self, owner, domain, now):
        """Return (record, fresh): fresh only when every required category is still valid."""
        value = self.store.company(owner, domain)
        fresh_categories = set()
        if value:
            sources = {source["id"]: source for source in value.get("sources", [])}
            for fact in value.get("facts", []):
                if fact.get("provenance") == "user" or not fact.get("source_ids"):
                    continue
                valid = True
                for source_id in fact["source_ids"]:
                    source = sources.get(source_id)
                    try:
                        retrieved = (
                            datetime.fromisoformat(source["retrieved_at"])
                            if source
                            else None
                        )
                        valid = (
                            valid
                            and retrieved is not None
                            and timedelta(0)
                            <= now - retrieved
                            < timedelta(days=self.settings.ttl_days)
                        )
                        valid = valid and bool(
                            source
                            and fact.get("evidence")
                            and fact["evidence"] in source.get("text", "")
                        )
                    except (ValueError, TypeError):
                        valid = False
                if valid:
                    fresh_categories.add(fact["category"])
        return value, set(REQUIRED_CATEGORIES).issubset(fresh_categories)

    def save(self, owner, company, facts, sources, research_scope=None):
        """Persist facts, sources and the research scope as a new knowledge version."""
        return self.store.save_company(
            owner,
            company["domain"],
            company["name"],
            facts,
            [source_view(source) for source in sources],
            research_scope=research_scope,
        )
