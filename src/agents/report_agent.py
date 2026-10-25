"""Report generation role: builds the cited report page data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.schemas.research import REQUIRED_CATEGORIES
from src.services.research.dossier_service import (
    dossier_from_facts,
    dossier_source_ids,
    scorecard,
)
from src.services.research.utils import utcnow


class ReportGenerationAgent:
    """Builds the cited report page data from stored knowledge and the dossier."""

    name = "report_generation"
    REQUIRED = REQUIRED_CATEGORIES

    def build(
        self,
        company_record,
        messages,
        calendar,
        meetings,
        errors,
        mode,
        elapsed,
        roles,
        dossier=None,
    ):
        """Assemble the report; facts without valid evidence are rejected and counted."""
        sources = {source["id"]: source for source in company_record.get("sources", [])}
        facts, rejected = [], 0
        for fact in company_record.get("facts", []):
            if not fact.get("source_ids") or any(
                source_id not in sources for source_id in fact["source_ids"]
            ):
                rejected += 1
                continue
            if fact.get("provenance") != "user" and any(
                fact.get("evidence", "") not in sources[source_id].get("text", "")
                for source_id in fact["source_ids"]
            ):
                rejected += 1
                continue
            facts.append(fact)
        if dossier is None:
            dossier = dossier_from_facts(
                {"name": company_record["name"], "website": ""}, facts
            )
        cited_ids = {source_id for fact in facts for source_id in fact["source_ids"]}
        cited_ids |= dossier_source_ids(dossier) & set(sources)
        used = [sources[source_id] for source_id in sorted(cited_ids)]
        score_rows, completeness = scorecard(dossier, used, elapsed)
        public_categories = {
            fact["category"] for fact in facts if fact.get("provenance") != "user"
        }
        covered = [
            category for category in self.REQUIRED if category in public_categories
        ]
        dated_news = [source for source in used if source.get("kind") == "news"]
        cutoff = datetime.now(UTC) - timedelta(days=30)
        recent = []
        unknown_dates = 0
        for source in dated_news:
            try:
                published = datetime.fromisoformat(source["published_at"])
                if published.utcoffset() is None:
                    published = published.replace(tzinfo=UTC)
                if cutoff <= published <= datetime.now(UTC):
                    recent.append(source["id"])
            except (ValueError, TypeError, KeyError):
                unknown_dates += 1
        people = {}
        for fact in facts:
            if fact.get("person"):
                profile = people.setdefault(
                    fact["person"],
                    {
                        "name": fact["person"],
                        "career": [],
                        "relationships": [],
                        "profiles": [],
                    },
                )
                profile[fact.get("person_section", "career")].append(fact["id"])
        if rejected:
            errors.append(
                {
                    "code": "EVIDENCE_REJECTED",
                    "message": f"Đã loại {rejected} nhận định không có bằng chứng hợp lệ.",
                }
            )
        return {
            "company": {
                key: company_record[key]
                for key in ("id", "domain", "name", "version", "updated_at")
            },
            "mode": mode,
            "generated_at": utcnow(),
            "facts": facts,
            "dossier": dossier,
            "scorecard": score_rows,
            "sources": used,
            "people": list(people.values()),
            "messages": [
                {
                    key: message.get(key, "")
                    for key in (
                        "id",
                        "thread_id",
                        "from",
                        "subject",
                        "date",
                        "body_truncated",
                    )
                }
                for message in messages
            ],
            "calendar": calendar,
            "meetings": meetings,
            "errors": errors,
            "roles": roles,
            "risk_scope": "Nguồn đã kiểm tra trong lượt nghiên cứu này; ưu tiên tin 30 ngày gần nhất. Không tìm thấy thông tin bất lợi không đồng nghĩa không có rủi ro.",
            "quality": {
                "checklist_version": 1,
                "covered": covered,
                "required": list(self.REQUIRED),
                "missing": [
                    category for category in self.REQUIRED if category not in covered
                ],
                "coverage_percent": round(100 * len(covered) / len(self.REQUIRED)),
                "dossier_completeness": completeness,
                "elapsed_seconds": round(elapsed, 2),
                "unique_sources": len(
                    {source.get("url") or source["id"] for source in used}
                ),
                "recent_news_sources": recent,
                "news_unknown_dates": unknown_dates,
                "accuracy": "Chưa được đánh giá độc lập; các trích đoạn được đối chiếu với nội dung nguồn.",
                "readiness": "Cần người dùng đánh giá; xem dữ liệu thiếu, lịch và giới hạn nguồn.",
            },
        }
