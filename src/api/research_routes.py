"""Authenticated research APIs, company knowledge, report pages and approvals."""

from __future__ import annotations

import uuid
from html import escape

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from src.agents.approval_agent import HumanApprovalAgent
from src.configs.config import FRONTEND_DIR
from src.schemas.errors import ResearchError
from src.schemas.research import (
    ActionEdit,
    ActionRequest,
    Decision,
    KnowledgeEdit,
    ResearchRequest,
)
from src.services.research import runtime as rt
from src.services.research.dossier_service import dossier_from_facts, scorecard
from src.workflows.research_workflow import ResearchWorkflow

router = APIRouter()


def browser_write(request):
    # A cross-origin HTML form cannot add this header; browser fetch requires
    # a CORS preflight, and this application does not grant cross-origin CORS.
    """Reject browser writes that do not carry the application's CSRF header."""
    if request.headers.get("x-research-request") != "1":
        raise ResearchError(
            "CSRF_REQUIRED", "Yêu cầu ghi phải đến từ giao diện ứng dụng.", 403
        )


@router.get("/research", response_class=HTMLResponse)
@router.get("/reports/{report_id}", response_class=HTMLResponse)
def research_page(request: Request, report_id: str | None = None):
    """Serve the research workspace (and the page of a stored report)."""
    if report_id:
        rt.runtime()[1].get("report", rt.owner(request), report_id)
    return HTMLResponse((FRONTEND_DIR / "research.html").read_text(encoding="utf-8"))


@router.get("/briefings/{batch_id}", response_class=HTMLResponse)
def briefing_page(batch_id: str, request: Request):
    """Serve the combined report page of one batch."""
    rt.runtime()[1].get("batch", rt.owner(request), batch_id)
    return HTMLResponse((FRONTEND_DIR / "research.html").read_text(encoding="utf-8"))


@router.get("/assets/research.css")
def research_styles():
    """Stylesheet of the research workspace."""
    return FileResponse(FRONTEND_DIR / "assets" / "research.css", media_type="text/css")


@router.get("/assets/research.js")
def research_script():
    """Script of the research workspace."""
    return FileResponse(FRONTEND_DIR / "assets" / "research.js", media_type="application/javascript")


@router.get("/api/research/config")
def research_config(request: Request):
    """Mode, providers and roles shown by the workspace."""
    settings, _ = rt.runtime()
    demo = getattr(request.state.login_session, "demo", False)
    return {
        "mode": "demo" if demo else "live",
        "demo_dataset": request.state.login_session.demo_dataset if demo else None,
        "email": rt.owner(request),
        "search_provider": (
            "input_only"
            if request.state.login_session.demo_dataset == "customer"
            else "mock"
        )
        if demo
        else settings.search_provider,
        "llm_enabled": False if demo else settings.use_llm,
        "timezone": "Asia/Ho_Chi_Minh",
        "roles": [
            "email",
            "web_research",
            "company_info",
            "calendar",
            "report_generation",
            "memory",
            "human_approval",
        ],
    }


@router.get("/api/research/mail")
def research_mail(request: Request, query: str = "in:inbox"):
    """List the user's mail for the inbox pane."""
    if len(query) > 500:
        raise ResearchError("INVALID_INPUT", "Truy vấn email quá dài.")
    result = rt.backend_for(request).list_mail(query=query, limit=20)
    result["mode"] = (
        "demo" if getattr(request.state.login_session, "demo", False) else "live"
    )
    return result


@router.post("/api/research", status_code=202)
def submit_research(
    payload: ResearchRequest,
    request: Request,
    tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None),
):
    """Queue a research batch; idempotent per Idempotency-Key."""
    browser_write(request)
    settings, store = rt.runtime()
    key = idempotency_key or str(uuid.uuid4())
    if not 1 <= len(key) <= 160:
        raise ResearchError("INVALID_INPUT", "Mã yêu cầu không hợp lệ.")
    backend = rt.backend_for(request)
    batch, created = store.create_batch(
        rt.owner(request), key, payload.model_dump(mode="json"), backend.mode
    )
    if created:
        tasks.add_task(
            ResearchWorkflow(store, settings).execute,
            rt.owner(request),
            batch["id"],
            payload.model_dump(mode="json"),
            backend,
        )
    return batch


@router.get("/api/research-batches")
def list_batches(request: Request):
    """The user's batches, newest first."""
    return {"batches": rt.runtime()[1].list("batch", rt.owner(request))}


@router.get("/api/research-batches/{batch_id}")
def batch_status(batch_id: str, request: Request):
    """One batch with its runs."""
    store = rt.runtime()[1]
    value = store.get("batch", rt.owner(request), batch_id)
    value["runs"] = [
        store.get("run", rt.owner(request), run["id"]) for run in value["runs"]
    ]
    return value


@router.get("/api/research/{run_id}")
def run_status(run_id: str, request: Request):
    """One run without private mail context."""
    value = rt.runtime()[1].get("run", rt.owner(request), run_id)
    value.pop("mail_context", None)
    return value


@router.get("/api/reports/{report_id}")
def get_report(report_id: str, request: Request):
    """One report; reports saved before dossiers existed are upgraded on read."""
    report = rt.runtime()[1].get("report", rt.owner(request), report_id)
    if "dossier" not in report:
        # Reports saved before dossiers existed are rebuilt from their cited facts.
        report["dossier"] = dossier_from_facts(
            {"name": report["company"]["name"], "website": ""}, report["facts"]
        )
        report["scorecard"] = scorecard(
            report["dossier"],
            report["sources"],
            report["quality"].get("elapsed_seconds", 0),
        )[0]
    return report


@router.get("/api/companies/{company_id}")
def get_company(company_id: str, request: Request):
    """Stored knowledge of one company."""
    return rt.runtime()[1].get_company(rt.owner(request), company_id)


@router.patch("/api/companies/{company_id}/knowledge")
def edit_knowledge(company_id: str, payload: KnowledgeEdit, request: Request):
    """Add a user-provided fact to a company's knowledge."""
    browser_write(request)
    return rt.runtime()[1].edit_company(rt.owner(request), company_id, payload)


@router.get("/api/companies/{company_id}/knowledge/history")
def knowledge_history(company_id: str, request: Request):
    """Versions of a company's knowledge."""
    return {"revisions": rt.runtime()[1].history(rt.owner(request), company_id)}


@router.post("/api/actions", status_code=201)
def propose_action(payload: ActionRequest, request: Request):
    """Create a send/trash action that waits for the user's approval."""
    browser_write(request)
    settings, store = rt.runtime()
    run = store.get("run", rt.owner(request), payload.run_id)
    report = store.get("report", rt.owner(request), payload.run_id)
    message = next(
        (
            message
            for message in run.get("mail_context", [])
            if message["id"] == payload.message_id
        ),
        None,
    )
    if not message:
        raise ResearchError("NOT_FOUND", "Email không thuộc báo cáo này.", 404)
    metadata = {
        "run_id": payload.run_id,
        "message_id": payload.message_id,
        "mailbox": rt.owner(request),
        "mode": run["mode"],
        "timezone": report["calendar"]["timezone"],
        "calendar_slots": [],
    }
    if payload.kind == "send_email":
        data = payload.send.model_dump()
        data["thread_id"] = message.get("thread_id", "")
        rfc_id = message.get("rfc_message_id", "")
        if "\r" in rfc_id or "\n" in rfc_id or len(rfc_id) > 500:
            raise ResearchError("INVALID_INPUT", "Header email không hợp lệ.")
        data["rfc_message_id"] = rfc_id
        assessment = next(
            (
                item
                for item in report["meetings"]
                if item["message_id"] == payload.message_id
            ),
            None,
        )
        if assessment and assessment["status"] == "conflict":
            metadata["calendar_slots"] = assessment["alternatives"]
    else:
        data = {"message_id": payload.message_id}
    return store.propose(
        rt.owner(request), payload.kind, data, metadata, settings.approval_seconds
    )


@router.get("/api/actions")
def list_actions(request: Request):
    """The user's pending and finished actions."""
    return {"actions": rt.runtime()[1].actions(rt.owner(request))}


@router.get("/api/actions/{action_id}")
def get_action(action_id: str, request: Request):
    """One action."""
    return rt.runtime()[1].action(rt.owner(request), action_id)


@router.patch("/api/actions/{action_id}")
def edit_action(action_id: str, payload: ActionEdit, request: Request):
    """Edit a pending send action (creates a new version)."""
    browser_write(request)
    return rt.runtime()[1].edit_action(
        rt.owner(request), action_id, payload.version, payload.send.model_dump()
    )


@router.post("/api/actions/{action_id}/decision")
def decide_action(action_id: str, payload: Decision, request: Request):
    """Approve or reject an action; only an exact approval executes it."""
    browser_write(request)
    settings, store = rt.runtime()
    return HumanApprovalAgent(store, rt.backend_for(request), settings).decide(
        rt.owner(request), action_id, payload
    )


@router.get("/api/reports/{report_id}/export.md")
def markdown_export(report_id: str, request: Request):
    """Download a report as Markdown."""
    report = rt.runtime()[1].get("report", rt.owner(request), report_id)
    lines = [
        "# " + escape(report["company"]["name"]),
        "",
        "DEMO — dữ liệu mô phỏng" if report["mode"] == "demo" else "Báo cáo nghiên cứu",
        "",
        "Ngày tạo: " + report["generated_at"],
        "",
    ]
    for fact in report["facts"]:
        lines.extend(
            [
                "## " + fact["category"],
                "",
                escape(fact["text"]),
                "",
                "Nguồn: " + ", ".join(fact["source_ids"]),
                "",
            ]
        )
    scope = report.get("research_scope") or {}
    if scope.get("topics"):
        lines.extend(
            ["## Phạm vi nghiên cứu", "", escape(scope.get("limitation", "")), ""]
        )
        for topic in scope["topics"]:
            lines.append(
                "- "
                + escape(topic["label"])
                + ": "
                + escape(topic["status"])
                + " — "
                + str(len(topic["read_sources"]))
                + " nguồn đã đọc"
            )
        lines.extend(["", "### Tất cả trang đã đọc (có thể chưa được trích dẫn)", ""])
        for source in scope.get("sources", []):
            lines.append(
                "- "
                + escape(source.get("title") or "")
                + " — "
                + escape(source.get("url") or "")
            )
        lines.append("")
    lines.extend(
        [
            "## Lịch và giới hạn",
            "",
            escape(str(report["calendar"])),
            "",
            escape(report["risk_scope"]),
            "",
            "## Nguồn",
            "",
        ]
    )
    for source in report["sources"]:
        lines.append(
            f"- {source['id']}: {escape(source['title'])} — {escape(source['url'])} — {source['retrieved_at']}"
        )
    for error in report["errors"]:
        lines.append("- Giới hạn: " + escape(error["message"]))
    return Response(
        "\n".join(lines),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="report-{uuid.UUID(report_id)}.md"'
        },
    )
