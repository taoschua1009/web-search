"""Research workspace settings, validated from the environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.configs.config import PROJECT_ROOT


class Settings(BaseModel):
    """Research workspace settings; every budget is range-checked."""

    model_config = ConfigDict(frozen=True)

    db_path: str = str(PROJECT_ROOT / ".runtime" / "research.sqlite3")
    search_provider: Literal["disabled", "google", "tavily"] = "disabled"
    google_key: str = Field(default="", repr=False)
    google_engine: str = ""
    tavily_key: str = Field(default="", repr=False)
    use_llm: bool = True
    max_pages: int = Field(default=24, ge=1, le=60)
    max_search_queries: int = Field(default=10, ge=1, le=30)
    max_website_pages: int = Field(default=8, ge=1, le=20)
    deadline_seconds: float = Field(default=480, ge=10, le=900)
    agent_mode: Literal["fast", "deep"] = "fast"
    agent_model: str = "claude-sonnet-5-5"
    fast_model: str = "claude-haiku-5-5"
    agent_search_limit: int = Field(default=16, ge=1, le=40)
    agent_fetch_limit: int = Field(default=14, ge=1, le=40)
    ttl_days: int = Field(default=7, ge=1)
    approval_seconds: int = Field(default=1800, ge=60, le=86400)
    work_start: int = Field(default=9, ge=0, le=24)
    work_end: int = Field(default=18, ge=0, le=24)

    @model_validator(mode="after")
    def _working_hours_are_ordered(self):
        if not self.work_start < self.work_end:
            raise ValueError("work_start must be earlier than work_end.")
        return self

    @classmethod
    def from_env(cls) -> Settings:
        """Read settings from the environment; invalid values raise ``ValueError``."""
        path = Path(os.getenv("RESEARCH_DB_PATH", cls.model_fields["db_path"].default))
        path = path.expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        env = os.getenv
        return cls(
            db_path=str(path),
            search_provider=env("RESEARCH_SEARCH_PROVIDER", "tavily"),
            google_key=env("GOOGLE_SEARCH_API_KEY", ""),
            google_engine=env("GOOGLE_SEARCH_ENGINE_ID", ""),
            tavily_key=env("TAVILY_API_KEY", ""),
            use_llm=env("RESEARCH_USE_LLM", "true").lower() == "true",
            max_pages=env("RESEARCH_MAX_PAGES", "24"),
            max_search_queries=env("RESEARCH_MAX_SEARCH_QUERIES", "10"),
            max_website_pages=env("RESEARCH_MAX_WEBSITE_PAGES", "8"),
            deadline_seconds=env("RESEARCH_DEADLINE_SECONDS", "480"),
            agent_mode=env("RESEARCH_AGENT_MODE", "fast"),
            agent_model=env("RESEARCH_AGENT_MODEL", "claude-sonnet-5-5"),
            fast_model=env("RESEARCH_FAST_MODEL", "claude-haiku-5-5"),
            agent_search_limit=env("RESEARCH_AGENT_SEARCHES", "16"),
            agent_fetch_limit=env("RESEARCH_AGENT_FETCHES", "14"),
            ttl_days=env("RESEARCH_COMPANY_TTL_DAYS", "7"),
            approval_seconds=env("RESEARCH_APPROVAL_SECONDS", "1800"),
            work_start=env("RESEARCH_WORK_START", "9"),
            work_end=env("RESEARCH_WORK_END", "18"),
        )
