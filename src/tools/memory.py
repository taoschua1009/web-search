"""
Memory Tools - Long-term memory using RAG (embed + vector search).
"""

from datetime import datetime
from src.registry.models import ToolDefinition
from src.configs.tool_loader import tool_config
from src.services import embedding, vectorstore


# ============================================================
# Tool 1: SAVE MEMORY
# ============================================================

def save_memory(content: str, category: str = "general") -> dict:
    """Save information to long-term memory."""
    vector = embedding.embed_query(content)
    metadata = {
        "category": category,
        "created_at": datetime.now().isoformat(),
    }
    vectorstore.save_memory(text=content, embedding=vector, metadata=metadata)
    return {
        "status": "saved",
        "content_preview": content[:100],
        "category": category,
    }


save_memory_tool = ToolDefinition(
    **tool_config("save_memory"),
    handler=save_memory,
)


# ============================================================
# Tool 2: SEARCH MEMORY
# ============================================================

def search_memory(query: str, top_k: int = 5) -> dict:
    """Search long-term memory for relevant information."""
    vector = embedding.embed_query(query)
    results = vectorstore.search_memory(query_vector=vector, top_k=top_k)
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
