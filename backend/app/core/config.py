from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BACKEND_DIR / ".env"


@dataclass(frozen=True)
class Settings:
    llm_model_id: str
    llm_api_key: str | None
    llm_base_url: str | None
    llm_timeout: float
    llm_enable_thinking: bool
    model_provider: str
    host: str
    port: int
    cors_origins: list[str]
    log_level: str
    unsplash_access_key: str | None
    unsplash_secret_key: str | None
    pexels_api_key: str | None
    pixabay_api_key: str | None
    openverse_client_id: str | None
    openverse_client_secret: str | None
    wikimedia_user_agent: str
    amap_api_key: str | None
    web_search_mcp_command: str | None
    web_search_mcp_tool: str
    mcp_timeout_seconds: float
    embedding_provider: str
    embedding_model_id: str
    embedding_dimensions: int
    embedding_api_key: str | None
    planner_mode: str
    research_cache_enabled: bool
    research_cache_ttl_seconds: int
    research_cache_max_entries: int
    disable_llm: bool
    disable_external_api: bool
    database_url: str | None

    @property
    def has_llm_credentials(self) -> bool:
        return bool(self.llm_api_key)


def get_settings() -> Settings:
    load_dotenv(ENV_PATH, override=False)

    model_provider = os.getenv("MODEL_PROVIDER", "openai-compatible")
    llm_model_id = os.getenv("LLM_MODEL_ID") or os.getenv("MODEL_NAME") or "gpt-4o-mini"
    llm_api_key = (
        os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )
    llm_base_url = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or os.getenv("DEEPSEEK_BASE_URL")
    )
    cors_origins = [
        item.strip()
        for item in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174").split(",")
        if item.strip()
    ]

    return Settings(
        llm_model_id=llm_model_id,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_timeout=float(os.getenv("LLM_TIMEOUT", "60")),
        llm_enable_thinking=_env_bool("LLM_ENABLE_THINKING", default=False),
        model_provider=model_provider,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        cors_origins=cors_origins,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        unsplash_access_key=os.getenv("UNSPLASH_ACCESS_KEY"),
        unsplash_secret_key=os.getenv("UNSPLASH_SECRET_KEY"),
        pexels_api_key=os.getenv("PEXELS_API_KEY"),
        pixabay_api_key=os.getenv("PIXABAY_API_KEY"),
        openverse_client_id=os.getenv("OPENVERSE_CLIENT_ID"),
        openverse_client_secret=os.getenv("OPENVERSE_CLIENT_SECRET"),
        wikimedia_user_agent=os.getenv("WIKIMEDIA_USER_AGENT", "travel-assistant/1.0 (local development; contact: example@example.com)"),
        amap_api_key=os.getenv("AMAP_API_KEY") or os.getenv("AMAP_MAPS_API_KEY"),
        web_search_mcp_command=os.getenv("WEB_SEARCH_MCP_COMMAND"),
        web_search_mcp_tool=os.getenv("WEB_SEARCH_MCP_TOOL", "web_search"),
        mcp_timeout_seconds=float(os.getenv("MCP_TIMEOUT_SECONDS", "20")),
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", "dashscope"),
        embedding_model_id=os.getenv("EMBEDDING_MODEL_ID", "tongyi-embedding-vision-plus-2026-03-06"),
        embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "512")),
        embedding_api_key=(
            os.getenv("EMBEDDING_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        ),
        planner_mode=os.getenv("PLANNER_MODE", "fast"),
        research_cache_enabled=_env_bool("RESEARCH_CACHE_ENABLED", default=True),
        research_cache_ttl_seconds=int(os.getenv("RESEARCH_CACHE_TTL_SECONDS", "86400")),
        research_cache_max_entries=int(os.getenv("RESEARCH_CACHE_MAX_ENTRIES", "200")),
        disable_llm=_env_bool("DISABLE_LLM"),
        disable_external_api=_env_bool("DISABLE_EXTERNAL_API"),
        database_url=_database_url_from_env(),
    )


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url_from_env() -> str | None:
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url

    host = os.getenv("POSTGRES_HOST")
    database = os.getenv("POSTGRES_DB")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    if not all([host, database, user, password]):
        return None

    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{quote_plus(user or '')}:{quote_plus(password or '')}@{host}:{port}/{quote_plus(database or '')}"
