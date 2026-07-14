from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BACKEND_DIR / ".env"


@dataclass(frozen=True)
class Settings:
    llm_model_id: str
    llm_api_key: str | None
    llm_base_url: str | None
    llm_timeout: float
    llm_connect_timeout: float
    llm_max_retries: int
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
    planner_max_iterations: int
    research_cache_enabled: bool
    research_cache_ttl_seconds: int
    research_cache_max_entries: int
    disable_llm: bool
    disable_external_api: bool
    database_url: str | None
    tavily_api_key: str | None
    tavily_max_results: int
    tavily_search_depth: str
    rollinggo_hotel_mcp_url: str | None
    rollinggo_hotel_api_key: str | None
    rollinggo_hotel_accept_language: str
    jwt_secret_key: str
    jwt_algorithm: str
    jwt_expire_minutes: int
    anonymous_jwt_expire_minutes: int
    redis_url: str | None
    redis_host: str | None
    redis_port: int
    redis_password: str | None
    redis_db: int
    redis_connect_timeout_seconds: float
    redis_read_timeout_seconds: float
    redis_max_connections: int
    knowledge_job_ttl_seconds: int
    url_fetch_max_bytes: int
    url_fetch_connect_timeout_seconds: float
    url_fetch_read_timeout_seconds: float
    rate_limit_enabled: bool
    rate_limit_anonymous_issue_limit: int
    rate_limit_register_limit: int
    rate_limit_login_limit: int
    rate_limit_qa_limit: int
    rate_limit_planning_limit: int
    rate_limit_map_limit: int
    rate_limit_knowledge_read_limit: int
    rate_limit_knowledge_write_limit: int
    rate_limit_window_seconds: int

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
        _normalize_cors_origin(item)
        for item in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174").split(",")
        if item.strip()
    ]
    redis_url = _env_str("REDIS_URL")
    redis_host = _env_str("REDIS_HOST")

    return Settings(
        llm_model_id=llm_model_id,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_timeout=float(os.getenv("LLM_TIMEOUT", "60")),
        llm_connect_timeout=float(os.getenv("LLM_CONNECT_TIMEOUT", "8")),
        llm_max_retries=max(0, min(_env_int("LLM_MAX_RETRIES", 0), 5)),
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
        planner_max_iterations=max(1, min(int(os.getenv("PLANNER_MAX_ITERATIONS", "3")), 10)),
        research_cache_enabled=_env_bool("RESEARCH_CACHE_ENABLED", default=True),
        research_cache_ttl_seconds=int(os.getenv("RESEARCH_CACHE_TTL_SECONDS", "86400")),
        research_cache_max_entries=int(os.getenv("RESEARCH_CACHE_MAX_ENTRIES", "200")),
        disable_llm=_env_bool("DISABLE_LLM"),
        disable_external_api=_env_bool("DISABLE_EXTERNAL_API"),
        database_url=_database_url_from_env(),
        tavily_api_key=_env_str("TAVILY_API_KEY"),
        tavily_max_results=max(1, min(_env_int("TAVILY_MAX_RESULTS", 5), 10)),
        tavily_search_depth=_env_str("TAVILY_SEARCH_DEPTH", "basic") or "basic",
        rollinggo_hotel_mcp_url=_env_str("ROLLINGGO_HOTEL_MCP_URL"),
        rollinggo_hotel_api_key=_env_str("ROLLINGGO_HOTEL_API_KEY"),
        rollinggo_hotel_accept_language=_env_str("ROLLINGGO_HOTEL_ACCEPT_LANGUAGE", "zh_CN") or "zh_CN",
        jwt_secret_key=os.getenv("JWT_SECRET_KEY", "change-me-in-production"),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        jwt_expire_minutes=int(os.getenv("JWT_EXPIRE_MINUTES", "1440")),
        anonymous_jwt_expire_minutes=int(os.getenv("ANONYMOUS_JWT_EXPIRE_MINUTES", "43200")),
        redis_url=redis_url,
        redis_host=redis_host,
        redis_port=_env_int("REDIS_PORT", 6379),
        redis_password=_env_str("REDIS_PASSWORD"),
        redis_db=_env_int("REDIS_DB", 0),
        redis_connect_timeout_seconds=float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "2")),
        redis_read_timeout_seconds=float(
            _env_str("REDIS_SOCKET_TIMEOUT_SECONDS")
            or _env_str("REDIS_READ_TIMEOUT_SECONDS", "2")
            or "2"
        ),
        redis_max_connections=max(1, _env_int("REDIS_MAX_CONNECTIONS", 20)),
        knowledge_job_ttl_seconds=max(1, _env_int("KNOWLEDGE_JOB_TTL_SECONDS", 7 * 24 * 60 * 60)),
        url_fetch_max_bytes=max(1, _env_int("URL_FETCH_MAX_BYTES", 2 * 1024 * 1024)),
        url_fetch_connect_timeout_seconds=max(0.1, float(os.getenv("URL_FETCH_CONNECT_TIMEOUT_SECONDS", "8"))),
        url_fetch_read_timeout_seconds=max(0.1, float(os.getenv("URL_FETCH_READ_TIMEOUT_SECONDS", "20"))),
        rate_limit_enabled=_env_bool("RATE_LIMIT_ENABLED", default=bool(redis_url or redis_host)),
        rate_limit_anonymous_issue_limit=max(1, _env_int("RATE_LIMIT_ANONYMOUS_ISSUE_LIMIT", 20)),
        rate_limit_register_limit=max(1, _env_int("RATE_LIMIT_REGISTER_LIMIT", 10)),
        rate_limit_login_limit=max(1, _env_int("RATE_LIMIT_LOGIN_LIMIT", 10)),
        rate_limit_qa_limit=max(1, _env_int("RATE_LIMIT_QA_LIMIT", 20)),
        rate_limit_planning_limit=max(1, _env_int("RATE_LIMIT_PLANNING_LIMIT", 5)),
        rate_limit_map_limit=max(1, _env_int("RATE_LIMIT_MAP_LIMIT", 60)),
        rate_limit_knowledge_read_limit=max(1, _env_int("RATE_LIMIT_KNOWLEDGE_READ_LIMIT", 30)),
        rate_limit_knowledge_write_limit=max(1, _env_int("RATE_LIMIT_KNOWLEDGE_WRITE_LIMIT", 5)),
        rate_limit_window_seconds=max(1, _env_int("RATE_LIMIT_WINDOW_SECONDS", 60)),
    )


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _normalize_cors_origin(value: str) -> str:
    origin = value.strip().rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return origin


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
