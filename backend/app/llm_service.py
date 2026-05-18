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
    except Exception:
        return None

    kwargs: dict[str, Any] = {
        "api_key": settings.llm_api_key,
        "model": settings.llm_model_id,
        "temperature": 0.4,
        "timeout": settings.llm_timeout,
        "max_retries": 0,
        "extra_body": {"enable_thinking": settings.llm_enable_thinking},
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url

    return ChatOpenAI(**kwargs)
