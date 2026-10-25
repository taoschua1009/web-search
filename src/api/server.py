"""
FastAPI Server - Serves the Agent via HTTP API + HTML chat UI on port 9004.
"""

from html import escape
import traceback
from urllib.parse import urlsplit
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from src.configs.config import GOOGLE_WEB_REDIRECT_URI, STATIC_DIR
from src.observability import telemetry  # noqa: F401 – initialises OTel providers
from src.agents.agent import Agent
from src.services import web_auth
from src.services.gmail_service import request_credentials

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
    public = {"/", "/login", "/auth/login", "/api/health"}
    if path not in public and login_session is None:
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={
                "response": "Vui lòng đăng nhập Google trước khi sử dụng agent.", "tools_used": [],
            }, headers={"Cache-Control": "no-store"})
        return RedirectResponse("/login", status_code=303)
    # Authenticated browser writes must originate from this application.
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse(status_code=403, content={"detail": "Invalid request origin."})
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
    page = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
    help_link = ""
    if error == "gmail_api_disabled":
        try:
            url = web_auth.gmail_api_setup_url()
            help_link = '<a class="setup" target="_blank" rel="noopener noreferrer" href="' + escape(url, quote=True) + '">Mở Google Cloud để bật Gmail API</a>'
        except (OSError, ValueError):
            pass
    return HTMLResponse(page.replace("{{ERROR}}", escape(text)).replace("{{HELP}}", help_link))


@app.get("/auth/login")
def google_login(request: Request):
    # Start on the callback host so its browser-bound cookie is returned by Google.
    if str(request.base_url).rstrip("/") != WEB_ORIGIN:
        return RedirectResponse(WEB_ORIGIN + "/auth/login", status_code=303)
    web_auth.cancel_login(request.cookies.get(web_auth.OAUTH_COOKIE, ""))
    try:
        url, browser_nonce = web_auth.start_login()
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


@app.get("/api/me")
def current_user(request: Request):
    return {"email": request.state.login_session.email}

# Per-session agents (keyed by session_id)
sessions: dict[str, Agent] = {}


def get_agent(session_id: str, login_session: web_auth.LoginSession | None = None) -> Agent:
    store = login_session.agents if login_session else sessions
    if session_id not in store:
        store[session_id] = Agent(service_api_key="sk-admin-001")
    return store[session_id]


# Create the default agent at startup so its zero-valued metric series are exported
# before the first chat request (otherwise Prometheus drops the first increment)
get_agent("default")


# ---- API Models ----

class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str


class ChatResponse(BaseModel):
    response: str
    tools_used: list[str] = []


# ---- API Endpoints ----

@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    try:
        agent = get_agent(req.session_id, request.state.login_session)
        audit_before = len(agent.get_audit_log())
        response = agent.run(req.message)

        new_logs = agent.get_audit_log()[audit_before:]
        tools_used = [log["tool"] for log in new_logs]

        return ChatResponse(response=response, tools_used=tools_used)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"response": f"Error: {e}", "tools_used": []},
        )


@app.post("/api/clear")
def clear_session(req: ChatRequest, request: Request):
    session_id = req.session_id or "default"
    store = request.state.login_session.agents
    if session_id in store:
        store[session_id].clear_history()
    return {"status": "cleared", "session_id": session_id}


@app.get("/api/audit")
def get_audit(request: Request, session_id: str = "default"):
    agent = get_agent(session_id, request.state.login_session)
    return {"audit_log": agent.get_audit_log()}


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
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9004, access_log=False)
