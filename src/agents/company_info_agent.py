"""Company info role: reads the company website and extracts cited facts."""

from __future__ import annotations

from src.schemas.errors import ResearchError, error_view
from src.schemas.research import Fact
from src.services.research.utils import digest


class CompanyInfoAgent:
    """Reads the company website and extracts facts that cite their source."""

    name = "company_info"

    def __init__(self, backend, reasoner, settings):
        self.backend, self.reasoner, self.settings = backend, reasoner, settings

    def website(self, company, deadline):
        """Crawl the company website within the page budget."""
        available = max(1, self.settings.max_pages - company.get("discovery_pages", 0))
        official_limit = min(self.settings.max_website_pages, max(1, available // 3))
        return self.backend.crawl(company["website"], official_limit, deadline)

    def extract(self, company, sources, deadline):
        """Turn sources into facts whose evidence appears verbatim in the cited source."""
        facts, errors = [], []
        if self.backend.mode == "demo":
            for source in sources:
                for category, text in source.get("fixture_facts", []):
                    fact = Fact(
                        id="fact-" + digest([source["id"], category, text])[:20],
                        category=category,
                        text=text,
                        evidence=text,
                        source_ids=[source["id"]],
                        provenance="user"
                        if source.get("fixture_origin") == "user_sample"
                        else "demo",
                        status="user_provided"
                        if source.get("fixture_origin") == "user_sample"
                        else "allegation"
                        if category == "risk"
                        else "source_statement",
                    ).model_dump()
                    fact.update(source.get("fixture_people", {}).get(text, {}))
                    facts.append(fact)
            return facts, errors
        source_map = {source["id"]: source for source in sources}
        for offset in range(0, len(sources), 6):
            source_batch = sources[offset : offset + 6]
            try:
                extracted = (
                    self.reasoner.facts(company, source_batch, deadline)
                    if source_batch
                    else None
                )
                if extracted:
                    if extracted.company_name and any(
                        extracted.company_name in source["text"]
                        for source in sources
                        if source.get("kind") == "official"
                    ):
                        company["name"] = extracted.company_name
                    for item in extracted.facts:
                        source = source_map.get(item.source_id)
                        if source and source.get("kind") != "official":
                            entity_text = source["text"].casefold()
                            if (
                                company["name"].casefold() not in entity_text
                                and company["domain"].casefold() not in entity_text
                            ):
                                errors.append(
                                    {
                                        "code": "ENTITY_UNCONFIRMED",
                                        "message": "Bỏ qua nhận định từ nguồn chưa khớp đúng công ty.",
                                    }
                                )
                                continue
                        if not source or item.evidence not in source["text"]:
                            errors.append(
                                {
                                    "code": "EVIDENCE_REJECTED",
                                    "message": "Bỏ qua nhận định không khớp trích đoạn nguồn.",
                                }
                            )
                            continue
                        fact = Fact(
                            id="fact-"
                            + digest([item.source_id, item.category, item.evidence])[
                                :20
                            ],
                            category=item.category,
                            text=item.evidence,
                            evidence=item.evidence,
                            source_ids=[item.source_id],
                            status=item.status,
                        ).model_dump()
                        if item.person and item.person in source["text"]:
                            fact.update(
                                person=item.person, person_section=item.person_section
                            )
                        facts.append(fact)
            except ResearchError as error:
                errors.append(error_view(error))
        if not facts:
            errors.append(
                {
                    "code": "EXTRACTION_LIMITED",
                    "message": "Chưa phân tích được hồ sơ đầy đủ; hiển thị trích đoạn nguồn đã đọc, không tự điền thông tin thiếu.",
                }
            )
            for source in sources:
                if (
                    source.get("kind") != "official"
                    and company["name"].casefold() not in source["text"].casefold()
                    and company["domain"].casefold() not in source["text"].casefold()
                ):
                    continue
                # Literal excerpts are safe fallback evidence; no guessed financial/person/risk claims.
                excerpt = source["text"][:700]
                facts.append(
                    Fact(
                        id="fact-" + digest([source["id"], excerpt])[:20],
                        category="overview",
                        text=excerpt,
                        evidence=excerpt,
                        source_ids=[source["id"]],
                    ).model_dump()
                )
        return facts, errors
