from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

try:  # pragma: no cover - exercised only when psycopg is installed
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover - lets tests run without database extras
    psycopg = None
    dict_row = None
    Jsonb = None

from app.domain.models import ResearchSnippet, TripPlan, TripPlanRequest, TripPlanningResult, TripReportDetail, TripReportSummary

logger = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trip_reports (
    id uuid PRIMARY KEY,
    prompt text NOT NULL,
    request_payload jsonb NOT NULL,
    result_payload jsonb NOT NULL,
    selected_plan_payload jsonb NOT NULL,
    city text NOT NULL,
    days_count integer NOT NULL,
    budget_total integer NOT NULL,
    generation_mode text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trip_report_revisions (
    id uuid PRIMARY KEY,
    report_id uuid NOT NULL REFERENCES trip_reports(id) ON DELETE CASCADE,
    operation text NOT NULL,
    plan_payload jsonb NOT NULL,
    research_context_payload jsonb NOT NULL,
    budget_total integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trip_reports_created_at ON trip_reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_reports_city ON trip_reports (city);
CREATE INDEX IF NOT EXISTS idx_trip_report_revisions_report_id ON trip_report_revisions (report_id, created_at DESC);
"""


class PostgresReportStore:
    def __init__(self, database_url: str):
        if psycopg is None or dict_row is None or Jsonb is None:
            raise RuntimeError("PostgreSQL storage requires psycopg. Run: pip install -r backend/requirements.txt")
        self.database_url = database_url
        self._schema_ready = False

    def ensure_schema(self) -> None:
        with psycopg.connect(self.database_url) as conn:
            conn.execute(SCHEMA_SQL)
        self._schema_ready = True

    def _ensure_schema_once(self) -> None:
        if not self._schema_ready:
            self.ensure_schema()

    def health(self) -> dict[str, Any]:
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    row = cur.execute("SELECT 1 AS ok").fetchone()
            return {"enabled": True, "ok": bool(row and row["ok"] == 1)}
        except Exception as exc:
            logger.warning("PostgreSQL health check failed: %s", exc)
            return {"enabled": True, "ok": False, "error": str(exc)}

    def save_report(self, request: TripPlanRequest, result: TripPlanningResult) -> dict[str, Any]:
        self._ensure_schema_once()
        report_id = str(uuid4())
        selected_plan = result.selected_plan
        request_payload = request.model_dump(mode="json")
        result_payload = result.model_dump(mode="json")
        selected_plan_payload = selected_plan.model_dump(mode="json")

        with psycopg.connect(self.database_url) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    INSERT INTO trip_reports (
                        id, prompt, request_payload, result_payload, selected_plan_payload,
                        city, days_count, budget_total, generation_mode
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id::text, created_at, updated_at
                    """,
                    (
                        report_id,
                        request.prompt,
                        Jsonb(request_payload),
                        Jsonb(result_payload),
                        Jsonb(selected_plan_payload),
                        selected_plan.city,
                        selected_plan.days_count,
                        selected_plan.budget.total,
                        selected_plan.generation_mode,
                    ),
                ).fetchone()
        return dict(row)

    def update_report_plan(
        self,
        report_id: str,
        plan: TripPlan,
        operation: str,
        research_context: list[ResearchSnippet] | None = None,
    ) -> None:
        self._ensure_schema_once()
        revision_id = str(uuid4())
        plan_payload = plan.model_dump(mode="json")
        research_payload = [snippet.model_dump(mode="json") for snippet in research_context or []]

        with psycopg.connect(self.database_url) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                updated = cur.execute(
                    """
                    UPDATE trip_reports
                    SET selected_plan_payload = %s,
                        city = %s,
                        days_count = %s,
                        budget_total = %s,
                        generation_mode = %s,
                        updated_at = now()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (
                        Jsonb(plan_payload),
                        plan.city,
                        plan.days_count,
                        plan.budget.total,
                        plan.generation_mode,
                        report_id,
                    ),
                ).fetchone()
                if updated is None:
                    raise ValueError(f"Report not found: {report_id}")
                cur.execute(
                    """
                    INSERT INTO trip_report_revisions (
                        id, report_id, operation, plan_payload, research_context_payload, budget_total
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        revision_id,
                        report_id,
                        operation,
                        Jsonb(plan_payload),
                        Jsonb(research_payload),
                        plan.budget.total,
                    ),
                )

    def list_reports(self, limit: int = 50) -> list[TripReportSummary]:
        self._ensure_schema_once()
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    SELECT id::text, prompt, city, days_count, budget_total, generation_mode, created_at, updated_at
                    FROM trip_reports
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (max(1, min(limit, 200)),),
                ).fetchall()
        return [TripReportSummary.model_validate(dict(row)) for row in rows]

    def get_report(self, report_id: str) -> TripReportDetail | None:
        self._ensure_schema_once()
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                report = cur.execute(
                    """
                    SELECT
                        id::text, prompt, city, days_count, budget_total, generation_mode,
                        created_at, updated_at, request_payload, result_payload, selected_plan_payload
                    FROM trip_reports
                    WHERE id = %s
                    """,
                    (report_id,),
                ).fetchone()
                if report is None:
                    return None
                revisions = cur.execute(
                    """
                    SELECT
                        id::text, report_id::text, operation, plan_payload,
                        research_context_payload, budget_total, created_at
                    FROM trip_report_revisions
                    WHERE report_id = %s
                    ORDER BY created_at DESC
                    """,
                    (report_id,),
                ).fetchall()

        payload = dict(report)
        return TripReportDetail.model_validate(
            {
                "id": payload["id"],
                "prompt": payload["prompt"],
                "city": payload["city"],
                "days_count": payload["days_count"],
                "budget_total": payload["budget_total"],
                "generation_mode": payload["generation_mode"],
                "created_at": payload["created_at"],
                "updated_at": payload["updated_at"],
                "request": payload["request_payload"],
                "result": payload["result_payload"],
                "selected_plan": payload["selected_plan_payload"],
                "revisions": [
                    {
                        "id": row["id"],
                        "report_id": row["report_id"],
                        "operation": row["operation"],
                        "plan": row["plan_payload"],
                        "research_context": row["research_context_payload"],
                        "budget_total": row["budget_total"],
                        "created_at": row["created_at"],
                    }
                    for row in revisions
                ],
            }
        )


def create_report_store(database_url: str | None) -> PostgresReportStore | None:
    if not database_url:
        return None
    return PostgresReportStore(database_url)
