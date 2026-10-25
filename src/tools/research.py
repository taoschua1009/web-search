"""Conversational entry point to the research workflow: the research_emails tool."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from pydantic import (
    Field,
    ValidationError,
)

from src.configs.tool_loader import tool_config
from src.registry.models import ToolDefinition
from src.schemas.errors import ResearchError
from src.schemas.research import CompanySelection, Contract, ResearchRequest
from src.services.research import runtime as rt
from src.services.research.dossier_service import dossier_digest
from src.workflows.research_workflow import ResearchWorkflow


class ChatResearchRequest(Contract):
    """Arguments of the research_emails tool."""

    latest_count: int = Field(default=2, ge=1, le=20, strict=True)
    query: str = Field(default="in:inbox", max_length=500)
    message_ids: list[str] = Field(default_factory=list, max_length=20)
    selections: list[CompanySelection] = Field(default_factory=list, max_length=20)
    timezone: str = Field(default="Asia/Ho_Chi_Minh", max_length=80)
    duration_minutes: int = Field(default=30, ge=5, le=480, strict=True)


@dataclass
class ChatResearchContext:
    """Per-turn state: the request, the user's text and the reports produced."""

    request: object
    user_message: str
    reports: list[dict] = field(default_factory=list)
    batches: list[dict] = field(default_factory=list)
    result: dict | None = None
    actions: list[dict] = field(default_factory=list)


_context = ContextVar("chat_research", default=None)


@contextmanager
def research_chat_scope(request, user_message=""):
    """Bind the signed-in request to this chat turn's research tool calls."""
    context = ChatResearchContext(request, user_message)
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def current_chat_context() -> ChatResearchContext | None:
    """The research context bound to this chat turn, if any."""
    return _context.get()


def research_emails(**arguments):
    """Run one owner-scoped batch per turn without executing email writes."""
    context = _context.get()
    if context is None:
        return {
            "code": "WEB_LOGIN_REQUIRED",
            "message": "Mở /chat và đăng nhập Google để nghiên cứu email.",
        }
    if context.result is not None:
        return context.result
    try:
        payload = ChatResearchRequest.model_validate(arguments)
        if any(item.website not in context.user_message for item in payload.selections):
            return {
                "code": "WEBSITE_CONFIRMATION_REQUIRED",
                "message": "Vui lòng cung cấp website công ty trong tin nhắn; không dùng địa chỉ do model tự đoán.",
            }
        account = rt.owner(context.request)
        settings, store = rt.runtime()
        backend = rt.backend_for(context.request)
        mail_errors = []
        if payload.message_ids:
            ids = payload.message_ids
        else:
            listing = backend.list_mail(query=payload.query, limit=payload.latest_count)
            ids = [message["id"] for message in listing["messages"]][
                : payload.latest_count
            ]
            mail_errors = listing.get("errors", [])
        if not ids:
            return {
                "code": "NO_EMAILS",
                "message": "Không tìm thấy email phù hợp.",
                "errors": mail_errors,
            }
        request = ResearchRequest(
            message_ids=ids,
            selections=payload.selections,
            timezone=payload.timezone,
            duration_minutes=payload.duration_minutes,
        )
        batch, created = store.create_batch(
            account, str(uuid.uuid4()), request.model_dump(mode="json"), backend.mode
        )
        if created:
            ResearchWorkflow(store, settings).execute(
                account, batch["id"], request.model_dump(mode="json"), backend
            )
        batch = store.get("batch", account, batch["id"])
        reports, summaries = [], []
        for item in batch["runs"]:
            run = store.get("run", account, item["id"])
            if not run.get("report_id"):
                summaries.append(
                    {
                        "company": item["name"],
                        "status": run["status"],
                        "error": run.get("error"),
                    }
                )
                continue
            report = store.get("report", account, run["report_id"])
            card = {
                "id": run["report_id"],
                "company": report["company"]["name"],
                "url": "/reports/" + run["report_id"],
                "mode": report["mode"],
                "status": run["status"],
            }
            reports.append(card)
            facts, cited = [], set()
            for category in (
                "overview",
                "industry",
                "products",
                "scale",
                "contacts",
                "people",
                "pricing",
                "financials",
                "partners",
                "news",
                "risk",
            ):
                for fact in [f for f in report["facts"] if f["category"] == category][
                    :2
                ]:
                    facts.append(
                        {
                            key: fact.get(key)
                            for key in (
                                "id",
                                "category",
                                "text",
                                "evidence",
                                "source_ids",
                                "status",
                                "provenance",
                            )
                        }
                    )
                    cited.update(fact["source_ids"])
            summaries.append(
                {
                    **card,
                    "highlights": dossier_digest(report.get("dossier")),
                    "scorecard": report.get("scorecard", []),
                    "facts": facts,
                    "sources": [
                        {
                            key: source.get(key)
                            for key in (
                                "id",
                                "url",
                                "title",
                                "kind",
                                "published_at",
                                "retrieved_at",
                            )
                        }
                        for source in report["sources"]
                        if source["id"] in cited
                    ],
                    "calendar": {
                        key: report["calendar"].get(key)
                        for key in (
                            "status",
                            "timezone",
                            "start",
                            "end",
                            "demo",
                            "error",
                        )
                    },
                    "free_windows": report["calendar"].get("free_windows", [])[:5],
                    "meetings": report["meetings"],
                    "quality": report["quality"],
                    "roles": report["roles"],
                    "errors": report["errors"],
                    "risk_scope": report["risk_scope"],
                    "memory_reused": report["memory_reused"],
                    "research_scope": report.get("research_scope"),
                }
            )
        context.reports.extend(reports)
        briefing_url = "/briefings/" + batch["id"] if reports else None
        context.batches.append(
            {
                "id": batch["id"],
                "status": batch["status"],
                "url": briefing_url or "/research",
                "companies": [card["company"] for card in reports],
            }
        )
        context.result = {
            "batch_id": batch["id"],
            "status": batch["status"],
            "mode": backend.mode,
            "selected_message_ids": ids,
            "briefing_url": briefing_url,
            "reports": summaries,
            "unresolved": batch["unresolved"],
            "errors": mail_errors + batch["errors"],
            "message": "Báo cáo đầy đủ nằm ở briefing_url (một trang cho tất cả công ty) và url của từng báo cáo. Trả lời NGẮN: mỗi công ty 3-4 ý chính từ highlights, nêu dữ liệu thiếu và giới hạn, rồi dẫn liên kết trang báo cáo. Không chép lại toàn bộ báo cáo. Không gửi/xóa email hoặc tạo lịch. Yêu cầu website nếu còn công ty chưa xác định.",
        }
        if batch.get("error"):
            context.result["error"] = batch["error"]
        return context.result
    except ValidationError:
        return {
            "code": "INVALID_INPUT",
            "message": "Chọn 1–20 email và cung cấp múi giờ/website hợp lệ.",
        }
    except ResearchError as error:
        return {"code": error.code, "message": error.message}


ALL_RESEARCH_TOOLS = [
    ToolDefinition(**tool_config("research_emails"), handler=research_emails)
]
