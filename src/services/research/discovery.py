"""Topic-balanced public company research across multiple source domains."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.schemas.errors import ResearchError
from src.services.research.web_fetcher import domain_of, normalize_url, url_key

PLAN_VERSION = 4


TOPICS = (
    (
        "profile",
        "Hồ sơ, lĩnh vực & liên hệ",
        "company overview history industry headquarters contact",
    ),
    ("products", "Sản phẩm, dịch vụ & giá", "products services solutions pricing"),
    ("people", "Người sáng lập & lãnh đạo", "founder CEO leadership management team"),
    (
        "scale",
        "Quy mô & tài chính công khai",
        "employees revenue funding annual report financial results",
    ),
    (
        "partners",
        "Khách hàng, đối tác & dự án",
        "customers partnerships projects contracts case studies",
    ),
    (
        "social",
        "Hồ sơ nghề nghiệp & mạng xã hội",
        "LinkedIn Facebook GitHub official company profile",
    ),
    ("news", "Tin tức trong 30 ngày", "company news last 30 days"),
    (
        "risk",
        "Rủi ro & thông tin bất lợi",
        "company risks controversies disputes investigation",
    ),
)


def collect_sources(backend, company, deadline, page_budget):
    """Search all topics before selecting pages in rounds; count failed reads too."""
    errors, coverage, candidates = [], [], {}
    stopped = "PAGE_BUDGET" if page_budget <= 0 else None
    for topic_id, label, query in TOPICS:
        item = {
            "id": topic_id,
            "label": label,
            "query": query,
            "searched": False,
            "status": "not_searched",
            "results": 0,
            "read_sources": [],
        }
        coverage.append(item)
        if stopped or time.monotonic() >= deadline:
            item["reason"] = stopped or "DEADLINE_EXCEEDED"
            continue
        try:
            hits = backend.search(company["name"], query, deadline)
            item["searched"] = True
            item["status"] = "no_results" if not hits else "no_readable_sources"
            for hit in hits[:5]:
                try:
                    url = normalize_url(hit.get("url", ""))
                    key = url_key(url)
                except (ResearchError, TypeError):
                    continue
                candidate = candidates.setdefault(
                    key,
                    {
                        "url": url,
                        "snippet": str(hit.get("snippet") or "")[:1000],
                        "topics": [],
                    },
                )
                if topic_id not in candidate["topics"]:
                    candidate["topics"].append(topic_id)
                    item["results"] += 1
        except ResearchError as error:
            item.update(status="search_failed", reason=error.code)
            errors.append(
                {"code": error.code, "message": error.message, "topic": topic_id}
            )
            if error.code in {
                "BUDGET_EXCEEDED",
                "DEADLINE_EXCEEDED",
                "SEARCH_NOT_CONFIGURED",
                "SAMPLE_RESEARCH_UNAVAILABLE",
                "SEARCH_AUTH_REQUIRED",
                "SEARCH_QUOTA_EXCEEDED",
                "PROVIDER_RATE_LIMIT",
            }:
                stopped = error.code

    # Prefer one page per topic before a second. New domains win within each
    # round and one external host cannot consume the entire read budget.
    selected, chosen, domain_counts, topic_counts = [], set(), {}, {}
    for round_index in range(2):
        for topic_id, _label, _query in TOPICS:
            if len(selected) >= page_budget:
                break
            options = [
                (key, candidate)
                for key, candidate in candidates.items()
                if topic_id in candidate["topics"]
                and key not in chosen
                and domain_counts.get(domain_of(candidate["url"]), 0) < 2
            ]
            options.sort(
                key=lambda pair: domain_counts.get(domain_of(pair[1]["url"]), 0)
            )
            # A previously chosen cross-topic page already represents this topic.
            if topic_counts.get(topic_id, 0) > round_index:
                continue
            if not options:
                continue
            key, candidate = options[0]
            chosen.add(key)
            selected.append(candidate)
            domain = domain_of(candidate["url"])
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            for covered_topic in candidate["topics"]:
                topic_counts[covered_topic] = topic_counts.get(covered_topic, 0) + 1

    sources_by_index = {}
    if selected:
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = {}
            for index, candidate in enumerate(selected):
                if time.monotonic() >= deadline:
                    break
                futures[workers.submit(backend.fetch, candidate["url"], deadline)] = (
                    index,
                    candidate,
                )
            for future in as_completed(futures):
                index, candidate = futures[future]
                try:
                    source = dict(future.result())
                    domain = domain_of(source["url"])
                    official = domain == company["domain"] or domain.endswith(
                        "." + company["domain"]
                    )
                    if official:
                        source["kind"] = "official"
                    elif any(
                        domain == host or domain.endswith("." + host)
                        for host in ("facebook.com", "linkedin.com", "github.com")
                    ):
                        source["kind"] = "social"
                    elif (
                        source.get("kind") == "website"
                        and "news" in candidate["topics"]
                    ):
                        source["kind"] = "news"
                    source["research_topics"] = list(candidate["topics"])
                    source["discovery_topic"] = next(
                        query
                        for topic_id, _, query in TOPICS
                        if topic_id == candidate["topics"][0]
                    )
                    sources_by_index[index] = source
                    for item in coverage:
                        if item["id"] in candidate["topics"]:
                            item["status"] = "read"
                            item["read_sources"].append(source["id"])
                except ResearchError as error:
                    errors.append(
                        {
                            "code": error.code,
                            "message": error.message,
                            "url": candidate["url"],
                            "topics": candidate["topics"],
                            "snippet": candidate["snippet"],
                            "access": "snippet_only",
                        }
                    )
                except Exception:  # noqa: BLE001 - one bad page must not discard other sources
                    errors.append(
                        {
                            "code": "FETCH_FAILED",
                            "message": "Không đọc được nguồn; các nguồn khác vẫn tiếp tục.",
                            "url": candidate["url"],
                            "topics": candidate["topics"],
                            "access": "snippet_only",
                        }
                    )
    for item in coverage:
        if item["status"] == "no_readable_sources" and len(selected) >= page_budget:
            item["reason"] = "PAGE_BUDGET_OR_DOMAIN_LIMIT"
    return [source for _, source in sorted(sources_by_index.items())], errors, coverage
