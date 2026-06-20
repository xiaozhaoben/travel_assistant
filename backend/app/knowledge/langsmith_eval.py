from __future__ import annotations

import logging
from typing import Any, Callable

from app.domain.models import TravelQAResponse


DEFAULT_DATASET_NAME = "travel_test"
DEFAULT_EXPERIMENT_PREFIX = "travel_test experiment"


def exact_match(outputs: dict, reference_outputs: dict) -> bool:
    return outputs == reference_outputs


def quiet_langsmith_eval_loggers(level: int = logging.WARNING) -> None:
    for logger_name in (
        "langsmith",
        "langsmith.client",
        "langsmith._internal._background_thread",
        "urllib3",
        "urllib3.connectionpool",
    ):
        logging.getLogger(logger_name).setLevel(level)


def dataset_has_examples(client: Any, dataset_name: str) -> bool:
    return next(client.list_examples(dataset_name=dataset_name, limit=1), None) is not None


def create_travel_qa_target(qa_agent: Any, default_top_k: int = 5) -> Callable[[dict], dict]:
    def target(inputs: dict) -> dict:
        question = question_from_inputs(inputs)
        top_k = top_k_from_inputs(inputs, default_top_k)
        conversation_history = conversation_history_from_inputs(inputs)
        response = qa_agent.ask(
            question,
            top_k=top_k,
            conversation_history=conversation_history,
        )
        return travel_qa_response_to_outputs(response)

    return target


def create_default_travel_qa_target() -> Callable[[dict], dict]:
    from app.main import get_app_resources

    resources = get_app_resources()
    return create_travel_qa_target(resources.qa_agent)


def question_from_inputs(inputs: dict) -> str:
    for key in ("question", "locale", "query", "input"):
        value = inputs.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    raise ValueError("LangSmith example inputs must include question, locale, query, or input.")


def top_k_from_inputs(inputs: dict, default_top_k: int) -> int:
    try:
        return int(inputs.get("top_k") or default_top_k)
    except (TypeError, ValueError):
        return default_top_k


def conversation_history_from_inputs(inputs: dict) -> list[dict[str, str]]:
    value = inputs.get("conversation_history") or inputs.get("history") or []
    if not isinstance(value, list):
        return []
    history: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role and content:
            history.append({"role": role, "content": content})
    return history


def travel_qa_response_to_outputs(response: Any) -> dict:
    data = getattr(response, "data", None)
    if data is not None:
        return travel_qa_response_to_outputs(data)
    if isinstance(response, TravelQAResponse):
        return {"answer": response.answer}
    if isinstance(response, dict):
        payload = response.get("data") if isinstance(response.get("data"), dict) else response
        return {"answer": str(payload.get("answer") or "")}
    return {"answer": str(getattr(response, "answer", response) or "")}
