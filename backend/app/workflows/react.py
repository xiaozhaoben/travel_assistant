from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from app.storage.plan_log import elapsed_ms, record_plan_event


@dataclass(frozen=True)
class ReActAttempt:
    iteration: int
    content: str | None = None
    thought: str | None = None
    error: str | None = None
    duration_ms: int | None = None


class ReActMaxIterationsExceeded(RuntimeError):
    def __init__(self, attempts: list[ReActAttempt]):
        self.attempts = attempts
        last_error = attempts[-1].error if attempts else "unknown error"
        super().__init__(f"ReAct generation failed after {len(attempts)} iterations: {last_error}")


class ReActOutputRunner:
    def __init__(self, component: str, max_iterations: int = 3):
        self.component = component
        self.max_iterations = max(1, min(max_iterations, 10))

    def run(
        self,
        *,
        base_prompt: str,
        invoke: Callable[[str, int], str],
        parse: Callable[[str], dict[str, Any]],
        build: Callable[[dict[str, Any]], Any],
        memory_context: list[str] | None = None,
    ) -> tuple[Any, list[ReActAttempt]]:
        attempts: list[ReActAttempt] = []
        feedback = ""
        memory_context = memory_context or []
        for iteration in range(1, self.max_iterations + 1):
            prompt = self._prompt_for_iteration(
                base_prompt=base_prompt,
                iteration=iteration,
                feedback=feedback,
                memory_context=memory_context,
            )
            start = time.perf_counter()
            content: str | None = None
            try:
                content = invoke(prompt, iteration)
                data = parse(content)
                result = build(data)
                thought = data.get("thought") if isinstance(data, dict) else None
                attempt = ReActAttempt(
                    iteration=iteration,
                    content=content,
                    thought=str(thought)[:500] if thought else None,
                    duration_ms=elapsed_ms(start),
                )
                attempts.append(attempt)
                self._record_attempt("success", attempt)
                return result, attempts
            except Exception as exc:
                attempt = ReActAttempt(
                    iteration=iteration,
                    content=content,
                    error=str(exc)[:1000],
                    duration_ms=elapsed_ms(start),
                )
                attempts.append(attempt)
                operation = "retry" if iteration < self.max_iterations else "failed"
                self._record_attempt(operation, attempt)
                feedback = self._feedback_from_error(exc)
        raise ReActMaxIterationsExceeded(attempts)

    def _prompt_for_iteration(
        self,
        *,
        base_prompt: str,
        iteration: int,
        feedback: str,
        memory_context: list[str],
    ) -> str:
        if iteration == 1 and not memory_context:
            return base_prompt
        sections = [base_prompt]
        if memory_context:
            sections.append(
                "Retrieved planner memory:\n"
                + "\n".join(f"- {item}" for item in memory_context[:5])
            )
        if feedback:
            sections.append(
                "Previous attempt failed. Regenerate the `thought` field as a short repair note, "
                "then return a corrected JSON object only. Error feedback:\n"
                f"{feedback}"
            )
        return "\n\n".join(sections)

    def _feedback_from_error(self, exc: Exception) -> str:
        return (
            f"{exc.__class__.__name__}: {str(exc)[:1200]}\n"
            "Fix the invalid fields, use available attraction/hotel identifiers when possible, "
            "and choose a simpler valid tool/output shape if needed."
        )

    def _record_attempt(self, operation: str, attempt: ReActAttempt) -> None:
        record_plan_event(
            event_type="react_iteration",
            component=self.component,
            operation=operation,
            request_payload={"iteration": attempt.iteration},
            response_payload={
                "thought": attempt.thought,
                "content": attempt.content,
            }
            if attempt.content is not None or attempt.thought is not None
            else None,
            error=attempt.error,
            duration_ms=attempt.duration_ms,
        )
