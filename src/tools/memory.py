"""
Memory Tools - Long-term memory using RAG (embed + vector search).

Memory is a persistence channel for injected text, so writes are screened and bounded,
every entry belongs to one person and expires, and what is read back is untrusted data
(OWASP: memory poisoning, isolation between users and sessions).
"""

import re
import time
from datetime import datetime

from src.configs.config import MEMORY_TTL_DAYS
from src.configs.tool_loader import tool_config
from src.registry.guardrails import scan_untrusted, strip_secrets
from src.registry.models import ToolDefinition
from src.services import embedding, vectorstore
from src.services.research import runtime as rt
from src.tools.research import current_chat_context

MAX_MEMORY_CHARS = 2000
MAX_TOP_K = 10
CATEGORY = re.compile(r"^[a-z0-9_ \-]{1,40}$")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _owner() -> str:
    """The signed-in person for this chat turn ('local' for the CLI)."""
    context = current_chat_context()
    if context is None:
        return "local"
    try:
        return rt.owner(context.request)
    except Exception:  # noqa: BLE001 - no session means no shared bucket
        raise PermissionError("[MEMORY] Memory needs a signed-in user.") from None


def clean_memory_text(content: str) -> str:
    """Bounded, secret-free text, or ValueError when it reads like instructions."""
    text = CONTROL.sub("", content).strip()
    if not text:
        raise ValueError("[MEMORY] Nothing to save.")
    if len(text) > MAX_MEMORY_CHARS:
        raise ValueError(f"[MEMORY] A memory may hold at most {MAX_MEMORY_CHARS} characters.")
    flags = scan_untrusted(text)
    if flags:
        raise ValueError(
            "[MEMORY] Not saved: the text looks like instructions to the assistant "
            f"({', '.join(flags)}). Save plain facts or preferences instead."
        )
    return strip_secrets(text)


# ============================================================
# Tool 1: SAVE MEMORY
# ============================================================

def save_memory(content: str, category: str = "general") -> dict:
    """Save information to long-term memory."""
    owner = _owner()
    text = clean_memory_text(content)
    category = category.strip().lower()
    category = category if CATEGORY.match(category) else "general"
    vector = embedding.embed_query(text)
    now = time.time()
    metadata = {
        "category": category,
        "owner": owner,
        "created_at": datetime.now().isoformat(),
        "expires_at": now + MEMORY_TTL_DAYS * 86400,
    }
    vectorstore.save_memory(text=text, embedding=vector, metadata=metadata)
    return {
        "status": "saved",
        "content_preview": text[:100],
        "category": category,
        "expires_in_days": MEMORY_TTL_DAYS,
    }


save_memory_tool = ToolDefinition(
    **tool_config("save_memory"),
    handler=save_memory,
)


# ============================================================
# Tool 2: SEARCH MEMORY
# ============================================================

def search_memory(query: str, top_k: int = 5) -> dict:
    """Search the caller's own long-term memory for relevant information."""
    owner = _owner()
    vector = embedding.embed_query(query)
    results = vectorstore.search_memory(
        query_vector=vector, owner=owner, top_k=max(1, min(int(top_k), MAX_TOP_K))
    )
    return {
        "query": query,
        "results_count": len(results),
        "memories": results,
    }


search_memory_tool = ToolDefinition(
    **tool_config("search_memory"),
    handler=search_memory,
)


ALL_MEMORY_TOOLS = [save_memory_tool, search_memory_tool]
