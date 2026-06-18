from __future__ import annotations

from typing import Any

from .config import get_settings


def create_llm() -> Any | None:
    """Create a LangChain chat model from .env settings.

    The reference project uses OpenAI-compatible variables:
    LLM_MODEL_ID, LLM_API_KEY, LLM_BASE_URL and LLM_TIMEOUT.
    When LLM_API_KEY is absent, return None so local fallback planning still works.
    """

    settings = get_settings()
    if not settings.llm_api_key:
        return None

    try:
        from langchain_openai import ChatOpenAI
        import httpx
    except Exception:
        return None

    request_timeout = httpx.Timeout(
        timeout=settings.llm_timeout,
        connect=min(settings.llm_connect_timeout, settings.llm_timeout),
        read=settings.llm_timeout,
        write=settings.llm_timeout,
        pool=min(settings.llm_connect_timeout, settings.llm_timeout),
    )
    kwargs: dict[str, Any] = {
        "api_key": settings.llm_api_key,
        "model": settings.llm_model_id,
        "temperature": 0.4,
        "timeout": request_timeout,
        "max_retries": settings.llm_max_retries,
        "extra_body": {"enable_thinking": settings.llm_enable_thinking},
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url

    return ChatOpenAI(**kwargs)
