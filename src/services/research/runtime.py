"""Per-process research runtime: settings, store and the per-request backend."""

from __future__ import annotations

from threading import Lock

from src.configs.research import Settings
from src.schemas.errors import ResearchError
from src.services.research.backends.demo import CustomerSampleBackend, DemoBackend
from src.services.research.backends.live import LiveBackend
from src.services.research.store import Store

_runtime_lock = Lock()
_runtime = None


def runtime():
    """Settings and store, created once even with concurrent first requests."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            settings = Settings.from_env()
            _runtime = settings, Store(settings.db_path)
        return _runtime


def owner(request):
    """Lower-cased account e-mail of the signed-in session."""
    session = request.state.login_session
    if not session:
        raise ResearchError("AUTH_REQUIRED", "Vui lòng đăng nhập.", 401)
    return session.email.lower()


def backend_for(request):
    """Demo backend for demo sessions, otherwise the live Google/Tavily backend."""
    settings, store = runtime()
    session = request.state.login_session
    if getattr(session, "demo", False):
        backend = (
            CustomerSampleBackend if session.demo_dataset == "customer" else DemoBackend
        )
        return backend(settings, owner(request), store)
    return LiveBackend(session.credentials, settings)
