"""
Embedding Service - Uses OpenAI text-embedding-3-small.
"""

from openai import OpenAI
from src.configs.config import OPENAI_API_KEY, EMBEDDING_MODEL

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def embed_texts(texts: list[str]) -> list[list[float]]:
    response = _get_client().embeddings.create(input=texts, model=EMBEDDING_MODEL)
    return [item.embedding for item in response.data]


def embed_query(query: str) -> list[float]:
    return embed_texts([query])[0]
