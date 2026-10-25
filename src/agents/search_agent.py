"""Search agent: a tool-using model researches one company and submits a cited dossier."""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from pydantic import (
    ValidationError,
)

from src.configs.research_prompts import DEEP_AGENT_SYSTEM
from src.observability.activity import NULL_SCOPE
from src.schemas.dossier import Dossier
from src.schemas.errors import ResearchError
from src.services.research.dossier_service import RetrievalState, finalize
from src.services.research.web_fetcher import normalize_url, url_key

logger = logging.getLogger(__name__)


MAX_TURNS = 16


FINAL_TURNS = 2  # the last turns only expose submit_dossier


def _tool_definitions(limits):
    return [
        {
            "name": "web_search",
            "description": (
                "Search the public web. Returns up to 8 results with url, title and a snippet. "
                f"At most {limits['search']} searches per run."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (any language).",
                    },
                    "recent": {
                        "type": "boolean",
                        "description": "Restrict to the last month (for news).",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "fetch_page",
            "description": (
                "Read one public web page as text (already-read pages are free). "
                f"At most {limits['fetch']} new pages per run. Some sites (LinkedIn, Facebook) "
                "cannot be read; use the search snippet instead."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
        {
            "name": "submit_dossier",
            "description": "Submit the final cited dossier. Call once, when research is done.",
            "input_schema": Dossier.model_json_schema(),
        },
    ]


class SearchAgent:
    """Tool-loop research agent (deep mode): searches, reads pages, submits a cited dossier."""

    name = "search_agent"

    def __init__(self, backend, settings):
        self.backend, self.settings = backend, settings
        self.limits = {
            "search": settings.agent_search_limit,
            "fetch": settings.agent_fetch_limit,
        }

    @property
    def enabled(self):
        """True in live mode with an API key and LLM use on."""
        return bool(
            self.settings.use_llm
            and os.getenv("ANTHROPIC_API_KEY")
            and getattr(self.backend, "mode", "") == "live"
        )

    # -- tools ---------------------------------------------------------------
    def _search(self, state, args, deadline):
        query = str(args.get("query", "")).strip()[:300]
        if not query:
            return {"error": "Empty query."}, True
        with state.lock:
            if state.searches >= self.limits["search"]:
                return {"error": "Search budget exhausted; call submit_dossier."}, True
            state.searches += 1
            state.recent_searches += bool(args.get("recent"))
        try:
            hits = self.backend.web_search(
                query, deadline, recent=bool(args.get("recent"))
            )
        except ResearchError as error:
            return {"error": error.message}, True
        out = []
        for hit in hits:
            try:
                key = url_key(hit["url"])
            except (ResearchError, KeyError, TypeError):
                continue
            with state.lock:
                state.hits.setdefault(key, hit)
            out.append(
                {
                    "url": hit["url"],
                    "title": hit.get("title", ""),
                    "snippet": hit.get("snippet", "")[:600],
                }
            )
        return {"results": out}, False

    def _fetch(self, state, args, seed, deadline):
        try:
            url = normalize_url(str(args.get("url", "")))
            key = url_key(url)
        except ResearchError as error:
            return {"error": error.message}, True
        with state.lock:
            source = state.pages.get(key) or seed.get(key)
            if source is None and state.fetches >= self.limits["fetch"]:
                return {"error": "Page budget exhausted; call submit_dossier."}, True
            if source is None:
                state.fetches += 1
        if source is None:
            try:
                source = dict(self.backend.fetch(url, deadline))
            except ResearchError as error:
                return {"error": error.message, "url": url}, True
        with state.lock:
            state.pages[key] = source
        text = source.get("text", "")
        return {
            "url": source["url"],
            "title": source.get("title", ""),
            "published_at": source.get("published_at"),
            "truncated": len(text) > 12000,
            "text": text[:12000],
        }, False

    def _submit(self, state, args, finishing):
        wanted = {
            "searches": min(8, self.limits["search"]),
            "page reads": min(4, self.limits["fetch"]),
        }
        done = {"searches": state.searches, "page reads": state.fetches}
        short = [
            f"{name} ({done[name]}/{need})"
            for name, need in wanted.items()
            if done[name] < need
        ]
        if state.recent_searches < 1 and self.limits["search"] > 1:
            short.append("a recent=true news search (0/1)")
        if short and not finishing and not state.rejected_shallow:
            state.rejected_shallow = True
            return {
                "error": "Research is too shallow to submit: "
                + ", ".join(short)
                + ". Cover every topic in the checklist (profile, team and open positions, "
                "founders and their public profiles, products and prices, customers and "
                "partners, news in the last 30 days, risks/complaints) and read the "
                "official pages, then submit again."
            }, True
        try:
            state.dossier = Dossier.model_validate(args)
        except ValidationError as error:
            problems = [
                "/".join(str(part) for part in item["loc"]) + ": " + item["msg"]
                for item in error.errors()[:6]
            ]
            return {
                "error": "Invalid dossier. Fix and resubmit.",
                "problems": problems,
            }, True
        return {"ok": True}, False

    # -- loop ----------------------------------------------------------------
    def run(self, company, seed_sources, emails, deadline, scope=NULL_SCOPE):
        """Return (dossier dict | None, sources used, errors).

        ``scope`` (optional) reports this agent's model calls and tools to the live view.
        """
        handle = scope.agent("Search agent (deep)", "search", self.settings.agent_model)
        result = None
        try:
            result = self._run(company, seed_sources, emails, deadline, handle)
            return result
        finally:
            handle.finish("completed" if result and result[0] else "failed")

    def _run(self, company, seed_sources, emails, deadline, handle):
        errors = []
        if not self.enabled:
            return None, [], errors
        import anthropic

        state = RetrievalState()
        seed = {}
        for source in seed_sources:
            if source.get("url") and not source.get("demo"):
                try:
                    seed[url_key(source["url"])] = source
                except ResearchError:
                    continue
        briefing = {
            "today": datetime.now(UTC).date().isoformat(),
            "company": {
                "name": company.get("name", ""),
                "candidate_website": company.get("website", ""),
                "how_candidate_was_found": company.get("evidence", ""),
            },
            "emails": emails,
            "pages_already_read": [
                {"url": source["url"], "title": source.get("title", "")}
                for source in seed.values()
            ][:30],
            "budgets": self.limits,
        }
        messages = [
            {"role": "user", "content": json.dumps(briefing, ensure_ascii=False)}
        ]
        tools = _tool_definitions(self.limits)
        submit_only = [tool for tool in tools if tool["name"] == "submit_dossier"]
        nudged = False
        try:
            with anthropic.Anthropic(max_retries=3) as client:
                for turn in range(MAX_TURNS):
                    remaining = deadline - time.monotonic()
                    if remaining < 15:
                        errors.append(
                            {
                                "code": "DEADLINE_EXCEEDED",
                                "message": "Search agent hết thời gian trước khi hoàn tất hồ sơ.",
                            }
                        )
                        break
                    finishing = (
                        turn >= MAX_TURNS - FINAL_TURNS
                        or remaining < 60
                        or (
                            state.searches >= self.limits["search"]
                            and state.fetches >= self.limits["fetch"]
                        )
                    )
                    began = time.monotonic()
                    response = client.messages.create(
                        model=self.settings.agent_model,
                        max_tokens=16000,
                        timeout=min(150, remaining),
                        output_config={"effort": "high"},
                        system=DEEP_AGENT_SYSTEM,
                        tools=submit_only if finishing else tools,
                        messages=messages,
                    )
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        handle.llm(
                            self.settings.agent_model,
                            usage.input_tokens,
                            usage.output_tokens,
                            (time.monotonic() - began) * 1000,
                        )
                    if response.stop_reason in {"refusal", "max_tokens"}:
                        raise ValueError(response.stop_reason)
                    messages.append({"role": "assistant", "content": response.content})
                    calls = [
                        block
                        for block in response.content
                        if getattr(block, "type", "") == "tool_use"
                    ]
                    if not calls:
                        if state.dossier or nudged:
                            break
                        nudged = True
                        messages.append(
                            {
                                "role": "user",
                                "content": "Research is over. Call submit_dossier now with the verified findings.",
                            }
                        )
                        continue

                    def execute(block, finishing=finishing):
                        args = block.input if isinstance(block.input, dict) else {}
                        if block.name in {"web_search", "fetch_page"}:
                            detail = args.get("query") or args.get("url") or ""
                            with handle.tool(block.name, detail) as running:
                                outcome = (
                                    self._search(state, args, deadline)
                                    if block.name == "web_search"
                                    else self._fetch(state, args, seed, deadline)
                                )
                                if outcome[1]:
                                    running.fail()
                                return outcome
                        if block.name == "submit_dossier":
                            return self._submit(state, args, finishing)
                        return {"error": "Unknown tool."}, True

                    with ThreadPoolExecutor(max_workers=4) as pool:
                        outcomes = list(pool.map(execute, calls))
                    results = []
                    for block, (payload, failed) in zip(calls, outcomes, strict=True):
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(payload, ensure_ascii=False),
                                **({"is_error": True} if failed else {}),
                            }
                        )
                    messages.append({"role": "user", "content": results})
                    if state.dossier:
                        break
        except Exception as error:  # noqa: BLE001 - provider/model failures degrade to the fact-based dossier
            # Only the exception type and HTTP status are logged, never payloads.
            logger.warning(
                "Search agent failed: %s %s",
                type(error).__name__,
                getattr(error, "status_code", ""),
            )
            errors.append(
                {
                    "code": "SEARCH_AGENT_FAILED",
                    "message": "Search agent gặp lỗi; hồ sơ được dựng từ dữ liệu đã thu thập.",
                }
            )
        if state.dossier is None:
            if (
                not any(item["code"] == "DEADLINE_EXCEEDED" for item in errors)
                and not errors
            ):
                errors.append(
                    {
                        "code": "SEARCH_AGENT_INCOMPLETE",
                        "message": "Search agent chưa nộp hồ sơ; hồ sơ được dựng từ dữ liệu đã thu thập.",
                    }
                )
            return None, [], errors
        dossier, sources, dropped = finalize(state.dossier, state, company)
        if dropped:
            errors.append(
                {
                    "code": "EVIDENCE_REJECTED",
                    "message": f"Đã loại {dropped} mục trong hồ sơ vì không dẫn được nguồn đã truy xuất.",
                }
            )
        return dossier, sources, errors
