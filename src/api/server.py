"""
FastAPI Server - Serves the Agent via HTTP API + HTML chat UI on port 9004.
"""

from html import escape
import os
import traceback
from urllib.parse import urlsplit
from typing import Literal
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from src.configs.config import FRONTEND_DIR, GOOGLE_WEB_REDIRECT_URI
from src.observability import telemetry  # noqa: F401 – initialises OTel providers
from src.agents.agent import Agent
from src.registry.registry import RateLimiter
from src.services import web_auth
from src.services.gmail_service import request_credentials
from src.api.activity_routes import router as activity_router
from src.api.research_routes import router as research_router
from src.schemas.errors import ResearchError
from src.tools.research import research_chat_scope

app = FastAPI(title="AI Agent - Assignment 1")
FastAPIInstrumentor.instrument_app(
    # ASGI instrumentation may match the path without query parameters.
    # Exclude the entire callback path so authorization codes never become spans.
    app, excluded_urls=r".*/auth/.*,^https?://[^/]+/?(?:\?.*)?$",
)
WEB_ORIGIN = GOOGLE_WEB_REDIRECT_URI.rstrip("/")
COOKIE_SECURE = urlsplit(WEB_ORIGIN).scheme == "https"


@app.middleware("http")
async def require_login(request: Request, call_next):
    login_session = web_auth.get_session(request.cookies.get(web_auth.SESSION_COOKIE, ""))
    request.state.login_session = login_session
    path = request.url.path
    public = {"/", "/login", "/auth/login", "/auth/demo", "/api/health"}
    if path not in public and login_session is None:
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={
                "response": "Vui lòng đăng nhập Google trước khi sử dụng agent.", "tools_used": [],
            }, headers={"Cache-Control": "no-store"})
        return RedirectResponse("/login", status_code=303)
    if login_session and login_session.demo:
        if path in {"/", "/chat"}:
            return RedirectResponse("/research", status_code=303)
        if path in {"/api/chat", "/api/clear", "/api/audit"}:
            return JSONResponse(status_code=403, content={"detail": "Demo sessions only access the isolated research workflow."})
    # Authenticated browser writes must originate from this application.
    origin = request.headers.get("origin")
    # no-referrer can make a same-origin form POST carry Origin: null.
    # Fetch Metadata still identifies its origin; cross-site writes remain denied.
    same_origin_form = origin == "null" and request.headers.get("sec-fetch-site") == "same-origin"
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != str(request.base_url).rstrip("/") and not same_origin_form:
        return JSONResponse(status_code=403, content={"detail": "Invalid request origin."})
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse(status_code=403, content={"detail": "Cross-site writes are not allowed."})
    token = request_credentials.set(login_session.credentials if login_session else None)
    try:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    finally:
        request_credentials.reset(token)


def _cookie(response, name: str, value: str, max_age: int):
    response.set_cookie(name, value, max_age=max_age, httponly=True,
                        secure=COOKIE_SECURE, samesite="lax", path="/")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if request.state.login_session:
        return RedirectResponse("/", status_code=303)
    errors = {
        "cancelled": "Bạn chưa cấp quyền. Hãy đăng nhập lại khi sẵn sàng.",
        "expired": "Phiên đăng nhập đã hết hạn hoặc không hợp lệ. Vui lòng thử lại.",
        "config": "Chưa cấu hình được đăng nhập Google. Kiểm tra file OAuth và redirect URI của project.",
        "google": "Không thể kết nối Gmail. Kiểm tra Gmail API, tài khoản Test users và quyền đọc Gmail, rồi thử lại.",
        "google_client": "Google từ chối cấu hình OAuth client. Kiểm tra client ID và client secret trong file OAuth của project.",
        "google_code": "Mã đăng nhập Google đã hết hạn hoặc không còn hợp lệ. Bấm đăng nhập để bắt đầu lại.",
        "google_network": "Server không kết nối được Google. Kiểm tra kết nối mạng rồi thử đăng nhập lại.",
        "gmail_permission": "Chưa được cấp quyền đọc Gmail. Đăng nhập lại và chọn quyền đọc email trong màn hình cấp quyền Google.",
        "gmail_api_disabled": "Gmail API chưa được bật cho Google Cloud project của ứng dụng. Bật Gmail API theo liên kết bên dưới rồi đăng nhập lại.",
        "gmail_access": "Google từ chối truy cập Gmail cho tài khoản này. Kiểm tra quyền Gmail hoặc chính sách Google Workspace của tài khoản.",
        "gmail_profile": "Không đọc được thông tin tài khoản Gmail. Kiểm tra tài khoản có Gmail hoạt động rồi thử lại.",
    }
    text = errors.get(error, "")
    page = (FRONTEND_DIR / "login.html").read_text(encoding="utf-8")
    help_link = ""
    if error == "gmail_api_disabled":
        try:
            url = web_auth.gmail_api_setup_url()
            help_link = '<a class="setup" target="_blank" rel="noopener noreferrer" href="' + escape(url, quote=True) + '">Mở Google Cloud để bật Gmail API</a>'
        except (OSError, ValueError):
            pass
    return HTMLResponse(page.replace("{{ERROR}}", escape(text)).replace("{{HELP}}", help_link))


@app.get("/auth/login")
def google_login(request: Request, research: bool = False):
    # Start on the callback host so its browser-bound cookie is returned by Google.
    if str(request.base_url).rstrip("/") != WEB_ORIGIN:
        return RedirectResponse(WEB_ORIGIN + "/auth/login" + ("?research=1" if research else ""), status_code=303)
    web_auth.cancel_login(request.cookies.get(web_auth.OAUTH_COOKIE, ""))
    try:
        url, browser_nonce = web_auth.start_login(research=True) if research else web_auth.start_login()
    except (OSError, ValueError, KeyError):
        return RedirectResponse("/login?error=config", status_code=303)
    response = RedirectResponse(url, status_code=303)
    _cookie(response, web_auth.OAUTH_COOKIE, browser_nonce, web_auth.OAUTH_SECONDS)
    return response


@app.post("/auth/logout")
def google_logout(request: Request):
    web_auth.logout(request.cookies.get(web_auth.SESSION_COOKIE, ""))
    web_auth.cancel_login(request.cookies.get(web_auth.OAUTH_COOKIE, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(web_auth.SESSION_COOKIE, path="/")
    response.delete_cookie(web_auth.OAUTH_COOKIE, path="/")
    return response


CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",  # the chat and login pages carry inline scripts
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Browser hardening on every response, including redirects and errors."""
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.get("/api/me")
def current_user(request: Request):
    return {"email": request.state.login_session.email}

# Per-session agents (keyed by session_id)
sessions: dict[str, Agent] = {}


MAX_AGENTS_PER_LOGIN = 5
MAX_MESSAGE_CHARS = 8000
chat_limiter = RateLimiter(max_calls=12, window_seconds=60)  # per signed-in person


class TooManySessions(Exception):
    """One login may keep only a few chat sessions (each holds history and cost)."""


def get_agent(session_id: str, login_session: web_auth.LoginSession | None = None) -> Agent:
    store = login_session.agents if login_session else sessions
    if session_id not in store:
        if login_session and len(store) >= MAX_AGENTS_PER_LOGIN:
            raise TooManySessions()
        store[session_id] = Agent(
            service_api_key="sk-admin-001",
            principal=login_session.email if login_session else None,
        )
    return store[session_id]


# Create the default agent at startup so its zero-valued metric series are exported
# before the first chat request (otherwise Prometheus drops the first increment)
if os.getenv("ANTHROPIC_API_KEY"):
    get_agent("default")


# ---- API Models ----

SESSION_ID = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,64}$")


class SessionRequest(BaseModel):
    session_id: str = SESSION_ID


class ChatRequest(SessionRequest):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ChatResponse(BaseModel):
    response: str
    tools_used: list[str] = []
    reports: list[dict] = []
    research_batches: list[dict] = []
    actions: list[dict] = []


# ---- API Endpoints ----

@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    try:
        # Attempt budget per person: repeated probing (and the model spend it causes) is capped.
        try:
            chat_limiter.check(request.state.login_session.email)
        except RuntimeError:
            return JSONResponse(
                status_code=429,
                content={"response": "Bạn gửi yêu cầu quá nhanh. Hãy chờ một chút rồi thử lại.", "tools_used": []},
                headers={"Retry-After": "30"},
            )
        agent = get_agent(req.session_id, request.state.login_session)
        audit_before = agent.audit_mark()
        with research_chat_scope(request, req.message) as research:
            response = agent.run(req.message)

        new_logs = agent.audit_since(audit_before)
        tools_used = [log["tool"] for log in new_logs]
        # A calendar tool that failed for lack of permission gets a one-click connect card.
        if any("/auth/login?research=1" in str(log.get("error") or "") for log in new_logs) and not any(
            action.get("kind") == "connect_google" for action in research.actions
        ):
            research.actions.append(
                {"kind": "connect_google", "url": "/auth/login?research=1", "title": "Kết nối quyền Google Calendar"}
            )

        return ChatResponse(response=response, tools_used=tools_used, reports=research.reports, research_batches=research.batches, actions=research.actions)
    except TooManySessions:
        return JSONResponse(
            status_code=429,
            content={"response": "Quá nhiều phiên chat đang mở. Hãy xóa hoặc tải lại trang.", "tools_used": []},
        )
    except Exception:  # noqa: BLE001 - details go to the server log, never to the browser
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"response": "Đã xảy ra lỗi khi xử lý yêu cầu. Hãy thử lại.", "tools_used": []},
        )


@app.post("/api/clear")
def clear_session(req: SessionRequest, request: Request):
    session_id = req.session_id or "default"
    store = request.state.login_session.agents
    if session_id in store:
        store[session_id].clear_history()
    return {"status": "cleared", "session_id": session_id}


@app.get("/api/audit")
def get_audit(request: Request, session_id: str = Query("default", pattern=r"^[A-Za-z0-9_-]{1,64}$")):
    # Read-only: never create an agent just to look at a log. Entries are already masked.
    agent = request.state.login_session.agents.get(session_id)
    return {"audit_log": agent.get_audit_log() if agent else []}


@app.get("/api/health")
def health():
    return {"status": "ok", "port": 9004}


# ---- Serve HTML UI ----

@app.get("/", response_class=HTMLResponse)
def serve_ui(request: Request):
    # This root URI is already registered in the project's Google Web client.
    # Consume the callback before redirecting to a clean URL without its code.
    params = request.query_params
    if any(name in params for name in ("code", "state", "error")):
        nonce = request.cookies.get(web_auth.OAUTH_COOKIE, "")
        if "error" in params:
            web_auth.cancel_login(nonce)
            response = RedirectResponse("/login?error=cancelled", status_code=303)
        else:
            try:
                session_id = web_auth.complete_login(nonce, params.get("state", ""), params.get("code", ""))
            except web_auth.InvalidLogin:
                response = RedirectResponse("/login?error=expired", status_code=303)
            except web_auth.LoginFailure as error:
                response = RedirectResponse("/login?error=" + error.code, status_code=303)
            except Exception:
                # OAuth exceptions can contain codes or tokens; do not echo them.
                response = RedirectResponse("/login?error=google", status_code=303)
            else:
                web_auth.logout(request.cookies.get(web_auth.SESSION_COOKIE, ""))
                response = RedirectResponse("/", status_code=303)
                _cookie(response, web_auth.SESSION_COOKIE, session_id, web_auth.SESSION_SECONDS)
        response.delete_cookie(web_auth.OAUTH_COOKIE, path="/")
        return response
    if not request.state.login_session:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/research", status_code=303)


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text(encoding="utf-8"))


@app.exception_handler(ResearchError)
async def research_error(request: Request, error: ResearchError):
    response = JSONResponse(status_code=error.status, content={"code": error.code, "message": error.message})
    if error.status == 401:
        web_auth.logout(request.cookies.get(web_auth.SESSION_COOKIE, ""))
        response.delete_cookie(web_auth.SESSION_COOKIE, path="/")
    return response


@app.post("/auth/demo")
def demo_login(request: Request, dataset: Literal["customer", "synthetic"] = "customer"):
    if os.getenv("RESEARCH_DEMO_ENABLED", "true").lower() != "true":
        return JSONResponse(status_code=404, content={"detail": "Demo disabled."})
    current = request.state.login_session
    if current and (not current.demo or current.demo_dataset == dataset):
        return RedirectResponse("/research", status_code=303)
    if current:
        web_auth.logout(request.cookies.get(web_auth.SESSION_COOKIE, ""))
    session_id = web_auth.create_demo_session(dataset)
    response = RedirectResponse("/research", status_code=303)
    _cookie(response, web_auth.SESSION_COOKIE, session_id, web_auth.SESSION_SECONDS)
    return response


app.include_router(research_router)
app.include_router(activity_router)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9004, access_log=False)
