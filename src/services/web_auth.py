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

from src.configs.config import (
    GOOGLE_WEB_CLIENT_FILE,
    GOOGLE_WEB_REDIRECT_URI,
    LOGIN_SESSION_FILE,
    LOGIN_SESSION_HOURS,
    LOGIN_SESSION_KEY,
)
from src.services.gmail_service import SCOPES
from src.services.session_store import EncryptedSessionStore

RESEARCH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    # Creating events happens only after the user approves the exact event.
    "https://www.googleapis.com/auth/calendar.events",
]
SESSION_COOKIE = "agent_session"
OAUTH_COOKIE = "agent_oauth"
SESSION_SECONDS = int(LOGIN_SESSION_HOURS * 60 * 60)
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
OAUTH_SECONDS = 10 * 60


@dataclass
class LoginSession:
    email: str
    credentials: Credentials | None
    expires_at: float
    agents: dict = field(default_factory=dict)
    demo: bool = False
    demo_dataset: str = "synthetic"


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


def _has_gmail_access(credentials: Credentials) -> bool:
    """Gmail read or modify must be granted; Calendar may be declined on Google's screen."""
    granted = credentials.granted_scopes
    for scopes in (SCOPES, [GMAIL_MODIFY]):
        if credentials.has_scopes(scopes) and (granted is None or set(scopes) <= set(granted)):
            return True
    return False


def _granted_only(credentials: Credentials) -> Credentials:
    """Credentials whose scopes are what Google granted, not merely what was requested."""
    granted = getattr(credentials, "granted_scopes", None)
    if not isinstance(credentials, Credentials) or not granted:
        return credentials
    try:
        return Credentials(
            token=credentials.token, refresh_token=credentials.refresh_token,
            token_uri=credentials.token_uri, client_id=credentials.client_id,
            client_secret=credentials.client_secret, scopes=list(granted),
            granted_scopes=list(granted), expiry=credentials.expiry,
        )
    except (TypeError, ValueError):
        return credentials


def _web_client_id() -> str:
    """The OAuth client id of this application ('' when it is not configured)."""
    try:
        with open(GOOGLE_WEB_CLIENT_FILE, encoding="utf-8") as stream:
            return json.load(stream).get("web", {}).get("client_id", "")
    except (OSError, ValueError):
        return ""


_session_store = EncryptedSessionStore(LOGIN_SESSION_FILE, LOGIN_SESSION_FILE.with_suffix(".key"), LOGIN_SESSION_KEY)


def _persistable(session: LoginSession | None, client_id: str) -> bool:
    """Only real Google credentials issued to this application's OAuth client are stored."""
    credentials = session.credentials if session else None
    return bool(
        session and not session.demo and client_id and isinstance(credentials, Credentials)
        and credentials.client_id == client_id and credentials.refresh_token
    )


def _persist() -> None:
    """Save live Google sessions (encrypted)."""
    client_id = _web_client_id()
    records = {}
    for session_id, session in _sessions.items():
        if not _persistable(session, client_id):
            continue
        credentials = session.credentials
        records[session_id] = {
            "email": session.email, "expires_at": session.expires_at,
            "credentials": json.loads(credentials.to_json()),
        }
    try:
        _session_store.save(records)
    except OSError:
        logger.warning("Could not save login sessions; a restart will require signing in again.")


def restore_sessions() -> int:
    """Load unexpired sessions saved by an earlier run; returns how many were restored."""
    client_id, now, restored = _web_client_id(), time.time(), 0
    for session_id, record in _session_store.load().items():
        try:
            info = record["credentials"]
            if not client_id or info.get("client_id") != client_id or record["expires_at"] <= now:
                continue
            credentials = Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))
            _sessions[session_id] = LoginSession(record["email"], credentials, float(record["expires_at"]))
            restored += 1
        except (KeyError, TypeError, ValueError):
            continue
    return restored


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


def start_login(research: bool = False) -> tuple[str, str]:
    """Create a PKCE flow and bind it to a separate browser cookie.

    Every login asks for the full scope set once (``research`` is kept only so older
    links keep working), so Calendar access never has to be requested separately.
    """
    _prune()
    scopes = RESEARCH_SCOPES
    flow = Flow.from_client_secrets_file(
        GOOGLE_WEB_CLIENT_FILE,
        scopes=scopes,
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
    if not _has_gmail_access(credentials):
        raise _failure("scope_check", "gmail_permission")
    credentials = _granted_only(credentials)
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
    if _persistable(_sessions[session_id], _web_client_id()):
        _persist()
    return session_id


def get_session(session_id: str) -> LoginSession | None:
    _prune()
    return _sessions.get(session_id)


def logout(session_id: str) -> None:
    removed = _sessions.pop(session_id, None)
    if _persistable(removed, _web_client_id()):
        _persist()


def create_demo_session(dataset: str = "customer") -> str:
    """Create an isolated identity with no Google credentials or live-tool access."""
    _prune()
    session_id = secrets.token_urlsafe(32)
    email = "demo-" + secrets.token_hex(12) + "@example.invalid"
    if dataset not in {"customer", "synthetic"}:
        raise ValueError("Unknown sample dataset.")
    _sessions[session_id] = LoginSession(email, None, time.time() + SESSION_SECONDS, demo=True, demo_dataset=dataset)
    return session_id


restore_sessions()
