from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import BACKEND_DIR
from app.storage.plan_log import to_jsonable
from app.workflows.react import ReActAttempt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReflectionFinding:
    category: str
    summary: str
    repair_hint: str
    count: int = 1


class ReflectionMemoryStore:
    source_name = "planner-reflection"

    def __init__(self, vector_store: Any | None):
        self.vector_store = vector_store

    def retrieve(self, query: str, k: int = 3) -> list[str]:
        if self.vector_store is None or not hasattr(self.vector_store, "similarity_search"):
            return []
        try:
            docs = self.vector_store.similarity_search(query, k=k, source_name=self.source_name)
        except TypeError:
            docs = self.vector_store.similarity_search(query, k=k)
        except Exception as exc:
            logger.warning("Planner reflection memory retrieval failed: %s", exc)
            return []
        snippets: list[str] = []
        for doc in docs:
            source = getattr(doc, "source_name", "")
            if source and source != self.source_name:
                continue
            text = getattr(doc, "summary", None) or getattr(doc, "content", None) or str(doc)
            if text:
                snippets.append(str(text)[:500])
        return snippets[:k]

    def remember_failure(self, finding: ReflectionFinding, failed_case: dict[str, Any]) -> None:
        if self.vector_store is None or not hasattr(self.vector_store, "add_text"):
            return
        content = "\n".join(
            [
                f"Planner failure pattern: {finding.category}",
                f"Summary: {finding.summary}",
                f"Repair hint: {finding.repair_hint}",
                f"Failed case: {json.dumps(to_jsonable(failed_case), ensure_ascii=False)}",
            ]
        )
        try:
            self.vector_store.add_text(
                content=content,
                source_url=None,
                title=f"Planner reflection: {finding.category}",
                source_name=self.source_name,
                metadata={
                    "category": finding.category,
                    "count": finding.count,
                    "created_by": "SupervisorReflectionAgent",
                },
            )
        except Exception as exc:
            logger.warning("Planner reflection memory update failed: %s", exc)


class FailedCaseNotebook:
    def __init__(self, path: Path | None = None):
        self.path = path or BACKEND_DIR / "runtime" / "planner_failed_cases.jsonl"

    def append(self, failed_case: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(to_jsonable(failed_case), ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Planner failed-case notebook write failed: %s", exc)


class SupervisorReflectionAgent:
    """Rule-based supervisor that turns failed tool/LLM attempts into reusable lessons."""

    def __init__(
        self,
        memory_store: ReflectionMemoryStore | None = None,
        notebook: FailedCaseNotebook | None = None,
    ):
        self.memory_store = memory_store
        self.notebook = notebook or FailedCaseNotebook()

    def review(
        self,
        *,
        component: str,
        prompt: str,
        attempts: list[ReActAttempt],
        context: dict[str, Any] | None = None,
    ) -> list[ReflectionFinding]:
        failures = [attempt for attempt in attempts if attempt.error]
        if not failures:
            return []
        categories: dict[str, int] = {}
        for attempt in failures:
            category = self._categorize(attempt.error or "")
            categories[category] = categories.get(category, 0) + 1

        findings = [
            ReflectionFinding(
                category=category,
                summary=f"{component} hit {count} {category} failure(s) before fallback or repair.",
                repair_hint=self._repair_hint(category),
                count=count,
            )
            for category, count in categories.items()
        ]
        failed_case = {
            "component": component,
            "prompt_excerpt": prompt[:2000],
            "context": context or {},
            "attempts": [attempt.__dict__ for attempt in attempts],
            "findings": [finding.__dict__ for finding in findings],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.notebook.append(failed_case)
        if self.memory_store is not None:
            for finding in findings:
                self.memory_store.remember_failure(finding, failed_case)
        return findings

    def _categorize(self, error: str) -> str:
        lowered = error.lower()
        if "json" in lowered:
            return "json_format"
        if "validation" in lowered or "pydantic" in lowered or "must include" in lowered:
            return "schema_validation"
        if "days" in lowered or "option" in lowered:
            return "missing_plan_shape"
        return "planner_generation"

    def _repair_hint(self, category: str) -> str:
        hints = {
            "json_format": "Return one JSON object only; remove Markdown fences and prose.",
            "schema_validation": "Match PlannerLLMOutput: include options[].plan.days or top-level days.",
            "missing_plan_shape": "Include at least one day with hotel, attractions, meals, route points, and weather.",
            "planner_generation": "Prefer a smaller valid plan and let local normalization fill optional details.",
        }
        return hints.get(category, hints["planner_generation"])
