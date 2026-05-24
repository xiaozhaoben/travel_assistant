from __future__ import annotations

from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import BaseModel


_current_recorder: ContextVar["PlanLogRecorder | None"] = ContextVar("plan_log_recorder", default=None)
_SECRET_KEY_PARTS = ("key", "token", "secret", "password", "authorization")
_MAX_TEXT_LENGTH = 20000


@dataclass(frozen=True)
class PlanLogEntry:
    sequence: int
    event_type: str
    component: str
    operation: str
    request_payload: Any
    response_payload: Any | None = None
    error: str | None = None
    duration_ms: int | None = None
    created_at: datetime | None = None


class PlanLogRecorder(AbstractContextManager["PlanLogRecorder"]):
    def __init__(self) -> None:
        self.entries: list[PlanLogEntry] = []
        self._token = None

    def __enter__(self) -> "PlanLogRecorder":
        self._token = _current_recorder.set(self)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._token is not None:
            _current_recorder.reset(self._token)

    def record(
        self,
        *,
        event_type: str,
        component: str,
        operation: str,
        request_payload: Any,
        response_payload: Any | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self.entries.append(
            PlanLogEntry(
                sequence=len(self.entries) + 1,
                event_type=event_type,
                component=component,
                operation=operation,
                request_payload=to_jsonable(request_payload),
                response_payload=to_jsonable(response_payload) if response_payload is not None else None,
                error=str(error)[:1000] if error else None,
                duration_ms=duration_ms,
                created_at=datetime.now(timezone.utc),
            )
        )


def record_plan_event(
    *,
    event_type: str,
    component: str,
    operation: str,
    request_payload: Any,
    response_payload: Any | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    recorder = _current_recorder.get()
    if recorder is None:
        return
    recorder.record(
        event_type=event_type,
        component=component,
        operation=operation,
        request_payload=request_payload,
        response_payload=response_payload,
        error=error,
        duration_ms=duration_ms,
    )


def record_api_call(
    *,
    component: str,
    operation: str,
    request_payload: Any,
    response_payload: Any | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    record_plan_event(
        event_type="api_call",
        component=component,
        operation=operation,
        request_payload=request_payload,
        response_payload=response_payload,
        error=error,
        duration_ms=duration_ms,
    )


def record_llm_call(
    *,
    component: str,
    operation: str,
    request_payload: Any,
    response_payload: Any | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    record_plan_event(
        event_type="llm_call",
        component=component,
        operation=operation,
        request_payload=request_payload,
        response_payload=response_payload,
        error=error,
        duration_ms=duration_ms,
    )


def elapsed_ms(start: float) -> int:
    return int((perf_counter() - start) * 1000)


def to_jsonable(value: Any) -> Any:
    value = _redact(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT_LENGTH else value[:_MAX_TEXT_LENGTH] + "...<truncated>"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if hasattr(value, "content"):
        return {
            "type": value.__class__.__name__,
            "content": to_jsonable(getattr(value, "content")),
        }
    return str(value)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in _SECRET_KEY_PARTS):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    return value
