"""Multi-company research workflow: one agent team per company, run in parallel.

Stages of one batch:

1. read the selected e-mails and resolve each one's company (in parallel);
2. group e-mails by company and start the calendar check *only* when an e-mail
   asks for a meeting;
3. research every company concurrently (memory -> fast research, or the classic
   pipeline as fallback) and build one cited report per company;
4. derive the batch outcome from the per-company runs.

Failures are isolated per e-mail and per company, and never leak provider
payloads or input text into logs or reports.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics, trace

from src.agents.calendar_agent import CalendarAgent
from src.agents.company_info_agent import CompanyInfoAgent
from src.agents.email_agent import EmailAgent
from src.agents.fast_research_agent import FastResearchAgent
from src.agents.memory_agent import MemoryAgent
from src.agents.report_agent import ReportGenerationAgent
from src.agents.search_agent import SearchAgent
from src.agents.web_research_agent import WebResearchAgent
from src.configs.research import Settings
from src.observability.activity import NULL_SCOPE, current_team, tracker
from src.schemas.errors import ResearchError, error_view
from src.schemas.research import ResearchRequest
from src.schemas.workflow import (
    Batch,
    FastResult,
    ResearchScope,
    Run,
    RunRef,
    Unresolved,
)
from src.services.research.discovery import PLAN_VERSION, TOPICS
from src.services.research.dossier_service import dossier_from_facts
from src.services.research.reasoner import Reasoner
from src.services.research.store import Store
from src.services.research.utils import utcnow
from src.services.research.web_fetcher import domain_of
from src.workflows.models import (
    BatchContext,
    CompanyGroup,
    ResearchOutcome,
    ResolvedEmail,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
run_counter = meter.create_counter("research.runs")
stage_latency = meter.create_histogram("research.stage.seconds", unit="s")

ROLES = (
    "email",
    "web_research",
    "company_info",
    "calendar",
    "report_generation",
    "memory",
    "human_approval",
)
MAX_PARALLEL_EMAILS = 4
MAX_PARALLEL_COMPANIES = 6
CALENDAR_DONE = {"checked", "not_requested"}
CLASSIC_LIMITATION = (
    "Nghiên cứu theo nhóm thông tin và ngân sách công khai; không bảo đảm bao phủ "
    "toàn bộ Internet. Nguồn không đọc được và dữ liệu thiếu được ghi rõ."
)
FAST_LIMITATION = (
    "Tìm song song theo các nhóm thông tin trong ngân sách thời gian; không bảo đảm "
    "bao phủ toàn bộ Internet. Nguồn không đọc được và dữ liệu thiếu được ghi rõ."
)
FAILED_BATCH = {
    "code": "WORKFLOW_FAILED",
    "message": "Không hoàn thành được lượt nghiên cứu. Các báo cáo đã lưu vẫn có thể đọc.",
}
FAILED_COMPANY = {
    "code": "WORKFLOW_FAILED",
    "message": "Không nghiên cứu được công ty này; các công ty khác vẫn tiếp tục.",
}


class ResearchWorkflow:
    """Coordinates the research roles for a batch of e-mails."""

    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings

    # ------------------------------------------------------------------ batch
    def execute(self, owner: str, batch_id: str, payload: dict, backend) -> None:
        """Run a stored batch to completion; always leaves a final batch status."""
        request = ResearchRequest.model_validate(payload)
        batch = Batch.model_validate(self.store.get("batch", owner, batch_id))
        batch.status = "running"
        self._save("batch", owner, batch_id, batch)
        started = time.monotonic()
        deadline = started + self.settings.deadline_seconds
        backend.deadline = deadline
        now = datetime.now(UTC)
        calendar_pool = ThreadPoolExecutor(max_workers=1)
        parent = current_team.get()
        team = tracker.start_team(
            owner,
            "research",
            f"Nghiên cứu {len(request.message_ids)} email",
            parent=parent.id if parent else None,
        )
        email_watcher = (
            team.agent("Email agent", "email") if team else NULL_SCOPE.agent()
        )
        try:
            context = BatchContext(
                request=request,
                now=now,
                deadline=deadline,
                backend=backend,
                email_agent=EmailAgent(backend, self._reasoner(backend, email_watcher)),
                web_agent=WebResearchAgent(backend),
                selections={item.message_id: item for item in request.selections},
                team=team,
                email_watcher=email_watcher,
            )
            with email_watcher.tool("read_mail", f"{len(request.message_ids)} email"):
                messages, read_errors = context.email_agent.read(request.message_ids)
            batch.errors.extend(read_errors)
            with email_watcher.tool("resolve_company", f"{len(messages)} email"):
                resolved = self._resolve_emails(messages, context)
            email_watcher.finish()
            groups = self._group(batch, resolved)

            # The calendar is only touched when some e-mail asks for a meeting,
            # and it runs alongside the company research rather than before it.
            calendar_future = None
            if any(group.meeting_requests for group in groups.values()):
                calendar_future = calendar_pool.submit(
                    CalendarAgent(backend, self.settings).snapshot,
                    now,
                    request.timezone,
                    request.duration_minutes,
                )
            self._run_companies(owner, batch, groups, calendar_future, context)
            self._finish_batch(owner, batch, started)
        except Exception:  # noqa: BLE001 - isolate failed jobs without leaking input or provider exceptions
            logger.error(
                "Research batch interrupted",
                extra={"batch_id": batch_id},
                exc_info=False,
            )
            batch.status, batch.error = "failed", FAILED_BATCH
            email_watcher.finish("failed")
            self._save("batch", owner, batch_id, batch)
        finally:
            calendar_pool.shutdown(wait=False)
            if team:
                team.finish("failed" if batch.status == "failed" else None)

    def _reasoner(self, backend, watcher=None) -> Reasoner:
        reasoner = Reasoner(self.settings.use_llm and backend.mode == "live")
        if watcher is not None:
            reasoner.on_usage = watcher.llm  # extraction calls show up in the live view
        return reasoner

    def _save(self, kind: str, owner: str, record_id: str, record) -> None:
        """Persist a typed record in its stored JSON form."""
        self.store.put(kind, owner, record_id, record.to_storage())

    def _resolve_emails(
        self, messages: list, context: BatchContext
    ) -> list[ResolvedEmail]:
        workers = max(1, min(MAX_PARALLEL_EMAILS, len(messages)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda m: self._resolve_email(m, context), messages))

    def _resolve_email(self, message: dict, context: BatchContext) -> ResolvedEmail:
        """Identify one e-mail's company (safe to run concurrently)."""
        if time.monotonic() >= context.deadline:
            return ResolvedEmail(message=message, deadline_exceeded=True)
        selection = context.selections.get(message["id"])
        try:
            preliminary, _, _, _ = context.email_agent.resolve(
                message,
                selection,
                context.now,
                context.request,
                context.deadline,
                extract_context=False,
            )
            reasoner = context.reasoners.setdefault(
                preliminary["domain"] or "message:" + message["id"],
                self._reasoner(context.backend, context.email_watcher),
            )
            resolver = EmailAgent(context.backend, reasoner)
            company, meeting, clarification, errors = resolver.resolve(
                message,
                selection,
                context.now,
                context.request,
                context.deadline,
            )
            candidates = company.get("candidates", [])
            discovery_errors: list = []
            if not company["domain"] and not candidates and company["name"]:
                verified, candidates, discovery_errors = (
                    context.web_agent.resolve_company(
                        company["name"],
                        reasoner,
                        context.deadline,
                        max_pages=min(2, self.settings.max_pages - 1),
                    )
                )
                company = verified or company
            return ResolvedEmail(
                message=message,
                company=company,
                candidates=candidates,
                meeting=meeting,
                clarification=clarification,
                errors=errors + discovery_errors,
                discovery_errors=discovery_errors,
                wants_meeting=resolver.meeting_requested,
                reasoner=reasoner,
            )
        except ResearchError as error:
            return ResolvedEmail(message=message, error=error)

    def _group(
        self, batch: Batch, resolved: list[ResolvedEmail]
    ) -> dict[str, CompanyGroup]:
        """Group resolved e-mails by company; record the ones that cannot be."""
        groups: dict[str, CompanyGroup] = {}
        for item in resolved:
            message = item.message
            if item.deadline_exceeded:
                batch.errors.append(
                    {
                        "code": "DEADLINE_EXCEEDED",
                        "message": "Đã hết ngân sách xử lý email.",
                    }
                )
            elif item.error:
                batch.unresolved.append(
                    Unresolved(**error_view(item.error), message_id=message["id"])
                )
            elif not item.company["domain"]:
                batch.errors.extend(item.discovery_errors)
                batch.unresolved.append(
                    Unresolved(
                        message_id=message["id"],
                        subject=message.get("subject", ""),
                        name=item.company["name"],
                        candidates=item.candidates,
                        code="COMPANY_AMBIGUOUS",
                        message="Chọn tên và website công ty để nghiên cứu đúng đối tượng.",
                    )
                )
            else:
                group = groups.setdefault(
                    item.company["domain"],
                    CompanyGroup(company=item.company, reasoner=item.reasoner),
                )
                group.add(item)
        return groups

    def _run_companies(
        self,
        owner: str,
        batch: Batch,
        groups: dict[str, CompanyGroup],
        calendar_future: Future | None,
        context: BatchContext,
    ) -> None:
        """Start one run per company; all companies are researched at once."""
        workers = max(1, min(len(groups), MAX_PARALLEL_COMPANIES))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for group in groups.values():
                run = self._new_run(owner, batch, group, context.backend)
                futures.append(
                    pool.submit(
                        self._run_company,
                        owner,
                        run,
                        group,
                        calendar_future,
                        context,
                    )
                )
            self._save("batch", owner, batch.id, batch)
            for future in as_completed(futures):
                future.result()

    def _new_run(self, owner: str, batch: Batch, group: CompanyGroup, backend) -> Run:
        run = Run(
            id=str(uuid.uuid4()),
            batch_id=batch.id,
            mode=backend.mode,
            company=group.company,
            message_ids=[message["id"] for message in group.messages],
            created_at=utcnow(),
            roles=dict.fromkeys(ROLES, "queued"),
        )
        self._save("run", owner, run.id, run)
        batch.runs.append(
            RunRef(
                id=run.id,
                name=group.company["name"],
                domain=group.company["domain"],
                message_count=len(group.messages),
            )
        )
        return run

    def _finish_batch(self, owner: str, batch: Batch, started: float) -> None:
        """Derive the batch status from its runs and persist it."""
        statuses = [
            self.store.get("run", owner, run.id)["status"] for run in batch.runs
        ]
        if (not batch.runs and not batch.unresolved) or (
            statuses
            and all(status == "failed" for status in statuses)
            and not batch.unresolved
        ):
            batch.status = "failed"
        elif batch.errors or any(s in {"failed", "partial"} for s in statuses):
            batch.status = "partial"
        else:
            batch.status = "needs_input" if batch.unresolved else "completed"
        batch.finished_at = utcnow()
        batch.elapsed_seconds = round(time.monotonic() - started, 2)
        self._save("batch", owner, batch.id, batch)

    # ---------------------------------------------------------------- company
    def _run_company(
        self,
        owner: str,
        run: Run,
        group: CompanyGroup,
        calendar_future: Future | None,
        context: BatchContext,
    ) -> None:
        """Research one company and store its report; never raises."""
        backend, request = context.backend, context.request
        started = time.monotonic()
        run.status = "running"
        run.roles["email"] = "completed"
        errors = list(group.errors)
        self._save("run", owner, run.id, run)
        with tracer.start_as_current_span(
            "research.company", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("research.mode", backend.mode)
            span.set_attribute("research.run_id", run.id)
            try:
                scope = (
                    context.team.scope(group.company["name"])
                    if context.team
                    else NULL_SCOPE
                )
                research = self._research(
                    owner, group, backend, context.deadline, errors, run.roles, scope
                )
                snapshot, meetings = self._check_calendar(
                    group, calendar_future, request, backend, run.roles, scope
                )
                with scope.agent("Report agent", "report") as reporter:
                    with reporter.tool("build_report", group.company["name"]):
                        self._publish_report(
                            owner,
                            run,
                            group,
                            research,
                            snapshot,
                            meetings,
                            errors,
                            backend,
                            time.monotonic() - started,
                        )
                run_counter.add(1, {"mode": backend.mode, "status": run.status})
            except ResearchError as error:
                run.status, run.error = "failed", error_view(error)
                run_counter.add(1, {"mode": backend.mode, "status": "failed"})
            except Exception:  # noqa: BLE001 - isolate failed jobs without leaking input or provider exceptions
                logger.error(
                    "Company run failed", extra={"run_id": run.id}, exc_info=False
                )
                run.status, run.error = "failed", FAILED_COMPANY
            finally:
                span.set_attribute("research.status", run.status)
                stage_latency.record(
                    time.monotonic() - started,
                    {"role": "company_workflow", "mode": backend.mode},
                )
                self._save("run", owner, run.id, run)

    def _research(
        self, owner, group, backend, deadline, errors, roles, scope=NULL_SCOPE
    ) -> ResearchOutcome:
        """Reuse fresh stored knowledge, else research the company anew."""
        company = group.company
        memory = MemoryAgent(self.store, self.settings)
        with scope.agent("Memory agent", "memory") as watcher:
            with watcher.tool("lookup", company["domain"]):
                record, fresh = memory.lookup(
                    owner, company["domain"], datetime.now(UTC)
                )
        fresh = self._is_fresh(record, fresh, backend)
        roles["memory"] = "cache_hit" if fresh else "completed"
        if fresh:
            roles["company_info"] = roles["web_research"] = "reused_memory"
            dossier = (record.get("research_scope") or {}).get("dossier")
            return ResearchOutcome(record=record, dossier=dossier, reused=True)
        if time.monotonic() >= deadline:
            raise ResearchError(
                "DEADLINE_EXCEEDED", "Đã hết thời gian nghiên cứu công ty."
            )
        fast = FastResearchAgent(backend, self.settings)
        result = (
            fast.run(company, group.email_excerpts(), deadline, scope)
            if fast.enabled
            else None
        )
        if result:
            record = self._save_fast(owner, group, memory, result, errors)
            roles["company_info"] = roles["web_research"] = "completed"
            return ResearchOutcome(record=record, dossier=result.dossier)
        with scope.agent("Classic research", "search") as classic:
            with classic.tool("topic_search_and_extract", company["name"]):
                record, dossier = self._classic(
                    owner, group, backend, deadline, memory, errors, roles, scope
                )
        return ResearchOutcome(record=record, dossier=dossier)

    def _is_fresh(self, record, memory_fresh: bool, backend) -> bool:
        """Whether stored knowledge may be reused instead of researching again."""
        if backend.mode != "live":
            return memory_fresh
        scope = (record or {}).get("research_scope") or {}
        if FastResearchAgent(backend, self.settings).enabled:
            try:
                age = datetime.now(UTC) - datetime.fromisoformat(scope["searched_at"])
            except (KeyError, TypeError, ValueError):
                return False
            return bool(
                record
                and age < timedelta(days=self.settings.ttl_days)
                and scope.get("plan_version") == PLAN_VERSION
                and (scope.get("dossier") or {}).get("pipeline") == "fast"
            )
        if not memory_fresh:
            return False
        topics = scope.get("topics", [])
        return (
            scope.get("plan_version") == PLAN_VERSION
            and all(item.get("searched") for item in topics)
            and len(topics) == len(TOPICS)
            # A fallback dossier is retried instead of cached for days.
            and (
                not SearchAgent(backend, self.settings).enabled
                or (scope.get("dossier") or {}).get("mode") == "agent"
            )
        )

    def _scope(
        self,
        *,
        sources: list[dict],
        dossier: dict,
        topics: list,
        search_limit: int,
        website_pages: int,
        external_pages: int,
        source_keys: tuple[str, ...],
        limitation: str,
        timings: dict[str, float] | None = None,
    ) -> dict:
        """Validated research-scope summary, in the form stored with the record."""
        return ResearchScope(
            plan_version=PLAN_VERSION,
            searched_at=utcnow(),
            topics=topics,
            page_limit=self.settings.max_pages,
            search_limit=search_limit,
            website_pages=website_pages,
            external_pages=external_pages,
            unique_domains=len({domain_of(s["url"]) for s in sources if s.get("url")}),
            sources=[{key: s.get(key) for key in source_keys} for s in sources],
            timings=timings,
            dossier=dossier,
            limitation=limitation,
        ).to_storage()

    def _save_fast(self, owner, group, memory, result: FastResult, errors) -> dict:
        errors.extend(result.errors)
        scope = self._scope(
            sources=result.sources,
            dossier=result.dossier,
            topics=result.topics,
            search_limit=result.queries,
            website_pages=result.official_pages,
            external_pages=result.search_hits,
            source_keys=("id", "url", "title", "kind", "retrieved_at"),
            limitation=FAST_LIMITATION,
            timings=result.timings,
        )
        logger.info("Fast research timings", extra={"timings": result.timings})
        return memory.save(owner, group.company, result.facts, result.sources, scope)

    def _classic(
        self, owner, group, backend, deadline, memory, errors, roles, scope=NULL_SCOPE
    ):
        """Topic search + website crawl + fact extraction.

        Used for demo data, the optional deep agent mode, and as the fallback when
        fast research yields nothing.
        """
        company = group.company
        info = CompanyInfoAgent(backend, group.reasoner, self.settings)
        web = WebResearchAgent(backend)
        available = max(1, self.settings.max_pages - company.get("discovery_pages", 0))
        official_limit = min(self.settings.max_website_pages, max(1, available // 3))
        search_agent = SearchAgent(backend, self.settings)
        deep = search_agent.enabled and self.settings.agent_mode == "deep"
        with ThreadPoolExecutor(max_workers=3) as research:
            website = research.submit(info.website, company, deadline)
            external = research.submit(
                web.collect, company, deadline, max(0, available - official_limit)
            )
            agent_run = (
                research.submit(
                    search_agent.run,
                    company,
                    [],
                    group.email_excerpts(),
                    deadline,
                    scope,
                )
                if deep
                else None
            )
            pages, page_errors = website.result()
            additional, search_errors = external.result()
            agent_result = agent_run.result() if agent_run else None
        sources = list(
            {source["url"]: source for source in pages + additional}.values()
        )[: self.settings.max_pages]
        errors.extend(page_errors + search_errors)
        # Company website statements are clearly attributed to that company.
        for source in pages:
            if not source.get("demo"):
                source["kind"] = "official"
        facts, extraction_errors = info.extract(company, sources, deadline)
        errors.extend(extraction_errors)
        dossier = None
        if agent_result:
            dossier, agent_sources, agent_errors = agent_result
            errors.extend(agent_errors)
            known = {source["id"] for source in sources}
            sources.extend(s for s in agent_sources if s["id"] not in known)
        if dossier is None:
            dossier = dossier_from_facts(company, facts)
        scope = self._scope(
            sources=sources,
            dossier=dossier,
            topics=getattr(web, "topic_results", []),
            search_limit=self.settings.max_search_queries,
            website_pages=len(pages),
            external_pages=len(additional),
            source_keys=(
                "id",
                "url",
                "title",
                "kind",
                "research_topics",
                "retrieved_at",
            ),
            limitation=CLASSIC_LIMITATION,
        )
        record = memory.save(owner, company, facts, sources, scope)
        roles["company_info"] = (
            "completed"
            if pages and not getattr(backend, "input_only", False)
            else "limited"
        )
        roles["web_research"] = "completed" if additional else "limited"
        return record, dossier

    # --------------------------------------------------------------- calendar
    def _check_calendar(
        self, group, calendar_future, request, backend, roles, scope=NULL_SCOPE
    ):
        """Assess meeting requests; skipped entirely when nobody asked to meet."""
        agent = CalendarAgent(backend, self.settings)
        asking = group.meeting_requests
        if not asking:
            roles["calendar"] = "not_requested"
            return agent.not_requested(request.timezone), []
        with scope.agent("Calendar agent", "calendar") as watcher:
            with watcher.tool("check_availability", request.timezone):
                snapshot = calendar_future.result()
                meetings = [
                    agent.assess(item.message, item.meeting, snapshot, request.timezone)
                    for item in asking
                ]
        roles["calendar"] = (
            "completed" if snapshot["status"] == "checked" else "limited"
        )
        return snapshot, meetings

    # ----------------------------------------------------------------- report
    def _publish_report(
        self,
        owner: str,
        run: Run,
        group: CompanyGroup,
        research: ResearchOutcome,
        snapshot: dict,
        meetings: list[dict],
        errors: list[dict],
        backend,
        elapsed: float,
    ) -> None:
        """Build, store and link the report; mark the run ready or partial."""
        roles = run.roles
        roles["report_generation"] = "completed"
        roles["human_approval"] = "awaiting_user_action"
        report = ReportGenerationAgent().build(
            research.record,
            group.messages,
            snapshot,
            meetings,
            errors,
            backend.mode,
            elapsed,
            dict(roles),
            dossier=research.dossier,
        )
        if getattr(backend, "input_only", False):
            self._label_input_only(report, group, meetings)
        # The dossier is a report field of its own; keep the scope summary small.
        report["research_scope"] = {
            key: value
            for key, value in (research.record.get("research_scope") or {}).items()
            if key != "dossier"
        } or None
        report["demo_dataset"] = getattr(backend, "dataset", None)
        report.update(
            run_id=run.id, batch_id=run.batch_id, memory_reused=research.reused
        )
        self.store.put("report", owner, run.id, report)
        # Keep only action references/headers, never whole private message bodies.
        run.mail_context = [
            {
                key: message.get(key, "")
                for key in ("id", "thread_id", "rfc_message_id", "from", "subject")
            }
            for message in group.messages
        ]
        run.status = (
            "partial"
            if errors or snapshot["status"] not in CALENDAR_DONE
            else "report_ready"
        )
        run.report_id, run.finished_at = run.id, utcnow()

    @staticmethod
    def _label_input_only(report: dict, group: CompanyGroup, meetings: list) -> None:
        report["risk_scope"] = (
            "Chưa nghiên cứu nguồn bên ngoài cho bộ email mẫu; không có cơ sở kết luận "
            "thông tin tích cực hoặc bất lợi về công ty."
        )
        for assessment in meetings:
            message = next(
                m for m in group.messages if m["id"] == assessment["message_id"]
            )
            if message.get("meeting_clarification"):
                assessment["message"] = message["meeting_clarification"]
