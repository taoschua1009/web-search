"""Tavily search over a fixed API endpoint with bounded, sanitized responses."""

from __future__ import annotations

import json
import time

import requests

from src.schemas.errors import ResearchError
from src.services.research.web_fetcher import normalize_url


def tavily_search(key, name, topic, deadline, *, recent=False, max_results=5):
    """Search Tavily and return at most ``max_results`` sanitized hits."""
    if not key:
        raise ResearchError(
            "SEARCH_NOT_CONFIGURED",
            "Thêm TAVILY_API_KEY vào .env để tìm kiếm Internet bằng Tavily.",
        )
    remaining = min(15, deadline - time.monotonic())
    if remaining <= 0:
        raise ResearchError("DEADLINE_EXCEEDED", "Đã hết thời gian tìm kiếm Internet.")
    payload = {
        "query": f"{name[:200]} {topic[:200]}".strip(),
        "search_depth": "basic",
        "topic": "general",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "auto_parameters": False,
    }
    if recent or "last 30 days" in topic:
        payload["time_range"] = "month"
    session = requests.Session()
    session.trust_env = False
    try:
        with session.post(
            "https://api.tavily.com/search",
            headers={"Authorization": "Bearer " + key},
            json=payload,
            timeout=remaining,
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code != 200:
                code = {
                    401: "SEARCH_AUTH_REQUIRED",
                    403: "SEARCH_AUTH_REQUIRED",
                    429: "PROVIDER_RATE_LIMIT",
                    432: "SEARCH_QUOTA_EXCEEDED",
                    433: "SEARCH_QUOTA_EXCEEDED",
                }.get(response.status_code, "SEARCH_UNAVAILABLE")
                raise ResearchError(
                    code,
                    "Tavily từ chối yêu cầu. Kiểm tra API key, hạn mức hoặc kết nối dịch vụ.",
                )
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                if time.monotonic() >= deadline:
                    raise ResearchError(
                        "DEADLINE_EXCEEDED", "Đã hết thời gian tìm kiếm Internet."
                    )
                size += len(chunk)
                if size > 1_000_000:
                    raise ResearchError(
                        "BUDGET_EXCEEDED", "Kết quả Tavily vượt giới hạn kích thước."
                    )
                chunks.append(chunk)
            result = json.loads(b"".join(chunks))
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise TypeError("Invalid search response")
        hits, seen = [], set()
        for item in result["results"][:20]:
            if not isinstance(item, dict):
                continue
            try:
                url = normalize_url(item.get("url", ""))
            except ResearchError:
                continue
            if url in seen:
                continue
            seen.add(url)
            hits.append(
                {
                    "url": url,
                    "title": str(item.get("title", ""))[:300],
                    "snippet": str(item.get("content") or "")[:1000],
                    "demo": False,
                }
            )
        return hits[:max_results]
    except ResearchError:
        raise
    except Exception:  # noqa: BLE001 - sanitize provider responses and credentials
        # No provider body, key or HTTP exception is exposed to model/user/logs.
        raise ResearchError(
            "SEARCH_UNAVAILABLE", "Không kết nối hoặc không đọc được kết quả Tavily."
        ) from None
    finally:
        session.close()
