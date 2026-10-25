"""Low-latency research: search and read everything at once, then extract in parallel.

Phase 1 runs every search query and the official-site reads at the same time (no model
in the loop). Phase 2 gives each specialist model only its own slice of that evidence and
one tool call to submit it. Phase 3 merges the slices in code and applies the same
citation validation as the tool-loop agent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from pydantic import (
    ValidationError,
)

from src.configs.research_prompts import (
    SECTION_LABELS,
    SPECIALIST_COMMON,
    SPECIALIST_TASKS,
)
from src.observability.activity import NULL_SCOPE
from src.registry.guardrails import scan_untrusted
from src.schemas.dossier import (
    Dossier,
    FoundersOut,
    Headline,
    MarketOut,
    ProductsOut,
    ProfileOut,
    SignalsOut,
    Team,
    TeamOut,
)
from src.schemas.errors import ResearchError
from src.schemas.workflow import FastResult, TopicCoverage
from src.services.research.domains import NEUTRAL_HOSTS
from src.services.research.dossier_service import (
    RetrievalState,
    facts_from_dossier,
    finalize,
    hit_source_id,
)
from src.services.research.web_fetcher import (
    CRAWL_EXTRA,
    CRAWL_GROUPS,
    domain_of,
    extended_crawl_group,
    url_key,
)

logger = logging.getLogger(__name__)


SEARCH_SLOTS = threading.BoundedSemaphore(12)  # shared by concurrent companies


PHASE1_SECONDS = 12


WAVE2_SECONDS = 6


PAGE_CHARS = 2500


OFFICIAL_PAGES = 6


SECTIONS = {
    "profile": ProfileOut,
    "team": TeamOut,
    "founders": FoundersOut,
    "products": ProductsOut,
    "market": MarketOut,
    "signals": SignalsOut,
}


def plan_queries(name, domain):
    """Search queries (and whether each is news-only) for every specialist topic."""
    q = f'"{name}"'
    return {
        "profile": [(f"{q} {domain} giới thiệu công ty trụ sở liên hệ", False)],
        "team": [(f"{q} tuyển dụng careers vị trí đang tuyển quy mô nhân sự", False)],
        "founders": [
            (f"{q} founder CEO sáng lập", False),
            (f"{q} founder LinkedIn GitHub Facebook", False),
        ],
        "products": [(f"{q} sản phẩm dịch vụ bảng giá pricing", False)],
        "market": [(f"{q} khách hàng đối tác doanh thu gọi vốn", False)],
        "signals": [
            (f"{q} news", True),
            (f"{q} sự kiện hội thảo blog tin tức", False),
            (f"{q} complaints lawsuit controversy scam review khiếu nại", False),
        ],
    }


class FastResearchAgent:
    """Parallel evidence gathering followed by parallel specialist extraction."""

    name = "fast_research"

    def __init__(self, backend, settings):
        self.backend, self.settings = backend, settings

    @property
    def enabled(self):
        """True in live mode with an API key, LLM use on and fast mode selected."""
        return bool(
            self.settings.use_llm
            and self.settings.agent_mode == "fast"
            and os.getenv("ANTHROPIC_API_KEY")
            and getattr(self.backend, "mode", "") == "live"
        )

    # -- phase 1: evidence ---------------------------------------------------
    def _search(
        self, state, topic, query, recent, deadline, watcher=NULL_SCOPE.agent()
    ):
        with SEARCH_SLOTS, watcher.tool("web_search", query) as call:
            try:
                hits = self.backend.web_search(query, deadline, recent=recent)
            except ResearchError as error:
                call.fail()
                return topic, [], {"code": error.code, "message": error.message}
        kept = []
        for hit in hits:
            try:
                key = url_key(hit["url"])
            except (ResearchError, KeyError, TypeError):
                continue
            with state.lock:
                state.hits.setdefault(key, hit)
            kept.append(hit)
        return topic, kept, None

    def _official(self, state, website, deadline, watcher=NULL_SCOPE.agent()):
        errors = []
        try:
            with watcher.tool("fetch_page", website):
                home = dict(self.backend.fetch(website, deadline))
        except ResearchError as error:
            return [], [{"code": error.code, "message": error.message, "url": website}]
        pages = [home]
        host = domain_of(home["url"])
        best = {}
        for link in home.get("links", []):
            try:
                if domain_of(link) != host:
                    continue
            except ResearchError:
                continue
            group = extended_crawl_group(link)
            if group < len(CRAWL_GROUPS) + len(CRAWL_EXTRA) and (
                group not in best or len(link) < len(best[group])
            ):
                best[group] = link
        chosen = [best[g] for g in sorted(best)][: OFFICIAL_PAGES - 1]

        def read(url):
            try:
                with watcher.tool("fetch_page", url):
                    return dict(self.backend.fetch(url, deadline)), None
            except ResearchError as error:
                return None, {"code": error.code, "message": error.message, "url": url}

        if chosen:
            with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
                for page, error in pool.map(read, chosen):
                    if page:
                        pages.append(page)
                    else:
                        errors.append(error)
        for page in pages:
            with state.lock:
                state.pages[url_key(page["url"])] = page
        return pages, errors

    # -- phase 2: specialists --------------------------------------------------
    @staticmethod
    def _tagged(kind: str, url: str, title: str, text: str) -> str:
        """One piece of evidence as labelled, delimited, screened data."""
        # A page must not be able to close its own tag and speak as the instructions.
        safe = re.sub(
            r"<\s*/\s*untrusted_source",
            "<\\/untrusted_source",
            text,
            flags=re.IGNORECASE,
        )
        flags = scan_untrusted(text)
        note = f' injection_flags="{",".join(flags)}"' if flags else ""
        return (
            f'<untrusted_source kind="{kind}" url="{url}"{note}>\n'
            f"Title: {title}\n{safe}\n</untrusted_source>"
        )

    def _evidence(self, pages, hits, extra=()):
        parts, seen = [], set()
        for page, limit in [(p, PAGE_CHARS) for p in pages] + [
            (p, 2000) for p in extra
        ]:
            key = url_key(page["url"])
            if key in seen:
                continue
            seen.add(key)
            parts.append(
                self._tagged(
                    "page", page["url"], page.get("title", ""), page["text"][:limit]
                )
            )
        for hit in hits:
            key = url_key(hit["url"])
            if key in seen:
                continue
            seen.add(key)
            parts.append(
                self._tagged(
                    "search_result",
                    hit["url"],
                    hit.get("title", ""),
                    hit.get("snippet", ""),
                )
            )
        return "\n\n".join(parts)

    def _wave2(
        self, state, company, hits_by_topic, deadline, watcher=NULL_SCOPE.agent()
    ):
        """Read the pages the searches pointed to, incl. a second official domain."""
        compact = re.sub(r"[^a-z0-9]", "", company.get("name", "").casefold())
        try:
            own_host = domain_of(company.get("website") or "")
        except ResearchError:
            own_host = ""
        chosen, domains = [], {own_host}
        queued = set(state.pages)

        def usable(hit):
            try:
                return domain_of(hit["url"]), url_key(hit["url"])
            except (ResearchError, KeyError, TypeError):
                return None, None

        for hits in hits_by_topic.values():
            for hit in hits:
                host, key = usable(hit)
                if (
                    not host
                    or host in domains
                    or len(domains) > 2
                    or any(host == n or host.endswith("." + n) for n in NEUTRAL_HOSTS)
                    or len(compact) < 4
                    or compact not in re.sub(r"[^a-z0-9]", "", host.split(".")[0])
                ):
                    continue
                domains.add(host)
                chosen.append((f"https://{host}/", None))
                if key not in queued:
                    chosen.append((hit["url"], None))
                    queued.add(key)
        for topic in ("founders", "signals", "products", "market"):
            for hit in hits_by_topic.get(topic, []):
                host, key = usable(hit)
                if (
                    host
                    and key not in queued
                    and not any(
                        host == n or host.endswith("." + n) for n in NEUTRAL_HOSTS
                    )
                ):
                    queued.add(key)
                    chosen.append((hit["url"], topic))
                    break
        shared, by_topic, errors = [], {}, []

        def read(item):
            url, topic = item
            try:
                with watcher.tool("fetch_page", url):
                    return topic, dict(self.backend.fetch(url, deadline)), None
            except ResearchError as error:
                return (
                    topic,
                    None,
                    {"code": error.code, "message": error.message, "url": url},
                )

        if chosen:
            with ThreadPoolExecutor(max_workers=len(chosen[:8])) as pool:
                for topic, page, error in pool.map(read, chosen[:8]):
                    if not page:
                        errors.append(error)
                        continue
                    with state.lock:
                        state.pages[url_key(page["url"])] = page
                    (by_topic.setdefault(topic, []) if topic else shared).append(page)
        return shared, by_topic, errors

    def _specialist(
        self, client, topic, briefing, evidence, deadline, handle=NULL_SCOPE.agent()
    ):
        task, model = SPECIALIST_TASKS[topic], SECTIONS[topic]
        tool = {
            "name": "submit_section",
            "description": "Submit your section. Call exactly once.",
            "input_schema": model.model_json_schema(),
        }
        messages = [
            {
                "role": "user",
                "content": json.dumps(briefing, ensure_ascii=False)
                + "\n\n=== EVIDENCE ===\n"
                + evidence,
            }
        ]
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining < 6:
                raise ResearchError("DEADLINE_EXCEEDED", "Hết thời gian trích xuất.")
            began = time.monotonic()
            response = client.messages.create(
                model=self.settings.fast_model,
                max_tokens=7000,
                timeout=min(45, remaining),
                output_config={"effort": "low"},
                system=SPECIALIST_COMMON + "\n\n" + task,
                tools=[tool],
                messages=messages,
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                handle.llm(
                    self.settings.fast_model,
                    usage.input_tokens,
                    usage.output_tokens,
                    (time.monotonic() - began) * 1000,
                )
            call = next(
                (
                    block
                    for block in response.content
                    if getattr(block, "type", "") == "tool_use"
                ),
                None,
            )
            if response.stop_reason in {"refusal", "max_tokens"} or call is None:
                raise ValueError(response.stop_reason or "no_tool_call")
            try:
                return model.model_validate(call.input)
            except ValidationError as error:
                if attempt:
                    raise
                problems = [
                    "/".join(str(p) for p in item["loc"]) + ": " + item["msg"]
                    for item in error.errors()[:5]
                ]
                messages.append({"role": "assistant", "content": response.content})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "is_error": True,
                                "content": json.dumps(
                                    {"error": "Invalid section", "problems": problems}
                                ),
                            }
                        ],
                    }
                )
        raise ValueError("unreachable")  # the loop always returns or raises

    # -- orchestration ---------------------------------------------------------
    def run(self, company, emails, deadline, scope=NULL_SCOPE):
        """Return FastResult, or None when nothing usable was produced.

        ``scope`` (optional) reports the evidence searcher and each specialist to the
        live Agents view.
        """
        import anthropic

        started = time.monotonic()
        errors, state = [], RetrievalState()
        plan = plan_queries(company["name"], company.get("domain", ""))
        phase1_deadline = min(deadline, started + PHASE1_SECONDS)
        hits_by_topic = {topic: [] for topic in plan}
        search_failures = dict.fromkeys(plan, 0)
        searcher = scope.agent("Search (evidence)", "search")
        with ThreadPoolExecutor(max_workers=12) as pool:
            official = pool.submit(
                self._official,
                state,
                company.get("website", ""),
                phase1_deadline,
                searcher,
            )
            searches = [
                pool.submit(
                    self._search, state, topic, query, recent, phase1_deadline, searcher
                )
                for topic, queries in plan.items()
                for query, recent in queries
            ]
            for future in searches:
                topic, hits, error = future.result()
                hits_by_topic[topic].extend(hits)
                if error:
                    search_failures[topic] += 1
                    errors.append(dict(error, topic=topic))
            pages, page_errors = official.result()
        errors.extend(page_errors)
        shared_pages, topic_pages, wave_errors = self._wave2(
            state,
            company,
            hits_by_topic,
            min(deadline, time.monotonic() + WAVE2_SECONDS),
            searcher,
        )
        errors.extend(wave_errors)
        searcher.finish()
        phase1 = time.monotonic() - started

        briefing = {
            "today": datetime.now(UTC).date().isoformat(),
            "company": {
                "name": company.get("name", ""),
                "candidate_website": company.get("website", ""),
            },
            "emails": emails,
        }
        phase2_deadline = min(deadline, time.monotonic() + 40)
        sections = {}
        with anthropic.Anthropic(max_retries=2) as client:

            def work(topic):
                handle = scope.agent(
                    f"Chuyên gia · {SECTION_LABELS[topic]}",
                    "specialist",
                    self.settings.fast_model,
                )
                try:
                    result = (
                        topic,
                        self._specialist(
                            client,
                            topic,
                            briefing,
                            self._evidence(
                                pages + shared_pages,
                                hits_by_topic[topic],
                                topic_pages.get(topic, []),
                            ),
                            phase2_deadline,
                            handle,
                        ),
                        None,
                    )
                    handle.finish()
                    return result
                except Exception as error:
                    handle.finish("failed", type(error).__name__)  # noqa: BLE001 - one failed slice must not discard the others
                    logger.warning(
                        "Specialist %s failed: %s %s",
                        topic,
                        type(error).__name__,
                        getattr(error, "status_code", ""),
                    )
                    return topic, None, error

            with ThreadPoolExecutor(max_workers=len(SECTIONS)) as pool:
                for topic, section, _error in pool.map(work, SECTIONS):
                    if section is not None:
                        sections[topic] = section
                    else:
                        errors.append(
                            {
                                "code": "SPECIALIST_FAILED",
                                "message": f"Mục “{SECTION_LABELS[topic]}” chưa hoàn tất; xem dữ liệu thiếu.",
                                "topic": topic,
                            }
                        )
        phase2 = time.monotonic() - started - phase1
        if not sections:
            return None

        profile = sections.get("profile")
        extras = [sections[t] for t in SECTIONS if t in sections]
        gaps = list(
            dict.fromkeys(
                [g for s in extras for g in s.gaps]
                + [
                    f"{SECTION_LABELS[t]}: chưa hoàn tất, cần chạy lại."
                    for t in SECTIONS
                    if t not in sections
                ]
            )
        )[:8]
        raw = Dossier(
            headline=profile.headline
            if profile
            else Headline(
                name=company.get("name", ""), website=company.get("website", "")
            ),
            summary=profile.summary if profile else None,
            kpis=list(
                {
                    (_squash(k.value + k.unit), _squash(k.label)[:24]): k
                    for s in reversed(extras)
                    for k in reversed(s.kpis)
                }.values()
            )[::-1][:6],
            profile=profile.profile if profile else [],
            team=sections["team"].team if "team" in sections else Team(),
            founders=sections["founders"].founders if "founders" in sections else [],
            products=sections["products"].products if "products" in sections else [],
            customers_partners=sections["market"].customers_partners
            if "market" in sections
            else [],
            financials=sections["market"].financials if "market" in sections else [],
            news=sections["signals"].news if "signals" in sections else [],
            risks=sections["signals"].risks if "signals" in sections else [],
            risk_note=sections["signals"].risk_note if "signals" in sections else "",
            meeting_prep=[m for s in extras for m in s.meeting_prep][:5],
            conflicts=list(dict.fromkeys(c for s in extras for c in s.conflicts))[:6],
            gaps=gaps,
        )
        dossier, sources, dropped = finalize(raw, state, company)
        if dropped:
            errors.append(
                {
                    "code": "EVIDENCE_REJECTED",
                    "message": f"Đã loại {dropped} mục trong hồ sơ vì không dẫn được nguồn đã truy xuất.",
                }
            )
        dossier["pipeline"] = "fast"
        topics = []
        for topic in plan:
            hits = hits_by_topic[topic]
            read = [hit_source_id(h["url"]) for h in hits]
            failed = search_failures[topic] == len(plan[topic])
            topics.append(
                TopicCoverage(
                    id=topic,
                    label=SECTION_LABELS[topic],
                    query=" | ".join(q for q, _ in plan[topic]),
                    searched=search_failures[topic] < len(plan[topic]),
                    status="search_failed"
                    if failed
                    else "read"
                    if hits or pages
                    else "no_results",
                    results=len(hits),
                    read_sources=read,
                )
            )
        return FastResult(
            dossier=dossier,
            sources=sources,
            facts=facts_from_dossier(dossier, sources),
            topics=topics,
            errors=errors,
            timings={
                "evidence_seconds": round(phase1, 1),
                "specialists_seconds": round(phase2, 1),
                "total_seconds": round(time.monotonic() - started, 1),
            },
            official_pages=len(pages),
            search_hits=sum(len(h) for h in hits_by_topic.values()),
            queries=sum(len(q) for q in plan.values()),
        )


def _squash(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())
