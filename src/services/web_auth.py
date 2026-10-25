"""Browser-bound Google OAuth and opaque, server-side login sessions."""

from dataclasses import dataclass, field
import json
import logging
import secrets
import time
from urllib.parse import urlencode

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from requests.exceptions import ConnectionError, Timeout

from src.configs.config import GOOGLE_WEB_CLIENT_FILE, GOOGLE_WEB_REDIRECT_URI
from src.services.gmail_service import SCOPES

SESSION_COOKIE = "agent_session"
OAUTH_COOKIE = "agent_oauth"
SESSION_SECONDS = 8 * 60 * 60
OAUTH_SECONDS = 10 * 60


@dataclass
class LoginSession:
    email: str
    credentials: Credentials
    expires_at: float
    agents: dict = field(default_factory=dict)


@dataclass
class PendingLogin:
    flow: Flow
    state: str
    expires_at: float


_sessions: dict[str, LoginSession] = {}
_pending: dict[str, PendingLogin] = {}
logger = logging.getLogger(__name__)


class InvalidLogin(PermissionError):
    """A callback does not match an unexpired login in this browser."""


class LoginFailure(Exception):
    """A safe, actionable error code without provider response contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _failure(stage: str, code: str, error: Exception | None = None) -> LoginFailure:
    # Never log raw OAuth exceptions: their messages may contain tokens or codes.
    logger.warning("Google login failed: stage=%s reason=%s type=%s", stage, code,
                   type(error).__name__ if error else "PermissionError")
    return LoginFailure(code)


def gmail_api_setup_url() -> str:
    with open(GOOGLE_WEB_CLIENT_FILE, encoding="utf-8") as stream:
        config = json.load(stream)
    project = config.get("web", {}).get("project_id", "")
    return "https://console.cloud.google.com/apis/library/gmail.googleapis.com?" + urlencode({"project": project})


def _prune() -> None:
    now = time.time()
    for store in (_sessions, _pending):
        for key, value in list(store.items()):
            if value.expires_at <= now:
                store.pop(key, None)


def start_login() -> tuple[str, str]:
    """Create a PKCE flow and bind it to a separate browser cookie."""
    _prune()
    flow = Flow.from_client_secrets_file(
        GOOGLE_WEB_CLIENT_FILE,
        scopes=SCOPES,
        redirect_uri=GOOGLE_WEB_REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    url, state = flow.authorization_url(
        access_type="offline", prompt="consent select_account",
    )
    browser_nonce = secrets.token_urlsafe(32)
    _pending[browser_nonce] = PendingLogin(flow, state, time.time() + OAUTH_SECONDS)
    return url, browser_nonce


def cancel_login(browser_nonce: str) -> None:
    _pending.pop(browser_nonce, None)


def _gmail_email(credentials: Credentials) -> str:
    profile = build("gmail", "v1", credentials=credentials, cache_discovery=False).users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "")
    if not email:
        raise PermissionError("Google did not return a Gmail account.")
    return email


def complete_login(browser_nonce: str, state: str, code: str) -> str:
    """Consume a matching flow once, then exchange the code over HTTPS."""
    _prune()
    pending = _pending.pop(browser_nonce, None)
    if not pending or not state or not secrets.compare_digest(state, pending.state) or not code:
        raise InvalidLogin("Login expired or did not match this browser. Please try again.")
    # Passing the code directly avoids relaxing OAuthlib's HTTPS requirement
    # globally just to accept a localhost HTTP redirect.
    try:
        pending.flow.fetch_token(code=code, timeout=20)
        credentials = pending.flow.credentials
    except (ConnectionError, Timeout) as error:
        raise _failure("token_exchange", "google_network", error) from None
    except Exception as error:
        provider_code = getattr(error, "error", "")
        known = {"invalid_client": "google_client", "invalid_grant": "google_code", "access_denied": "gmail_permission"}
        code = known.get(provider_code, "gmail_permission" if isinstance(error, Warning) else "google")
        raise _failure("token_exchange", code, error) from None
    granted = credentials.granted_scopes
    if not credentials.has_scopes(SCOPES) or (granted is not None and not set(SCOPES).issubset(granted)):
        raise _failure("scope_check", "gmail_permission")
    try:
        email = _gmail_email(credentials)
    except HttpError as error:
        reasons = set()
        try:
            details = json.loads(error.content).get("error", {})
            reasons.update(item.get("reason", "") for item in details.get("errors", []))
            reasons.update(item.get("reason", "") for item in details.get("details", []))
        except (ValueError, TypeError, AttributeError):
            pass
        if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            code = "gmail_api_disabled"
        elif reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
            code = "gmail_permission"
        elif error.resp.status == 403:
            code = "gmail_access"
        elif error.resp.status == 401:
            code = "google_code"
        else:
            code = "gmail_profile"
        raise _failure("gmail_profile", code, error) from None
    except (ConnectionError, Timeout) as error:
        raise _failure("gmail_profile", "google_network", error) from None
    except Exception as error:
        raise _failure("gmail_profile", "gmail_profile", error) from None
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = LoginSession(email, credentials, time.time() + SESSION_SECONDS)
    return session_id


def get_session(session_id: str) -> LoginSession | None:
    _prune()
    return _sessions.get(session_id)


def logout(session_id: str) -> None:
    _sessions.pop(session_id, None)
