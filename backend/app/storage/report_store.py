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
from app.storage.db import DatabaseConnectionManager
from app.storage.plan_log import PlanLogEntry

logger = logging.getLogger(__name__)


class ReportNotFound(LookupError):
    """Raised when a report does not exist for the requested owner."""


def _validate_owner(owner_type: str, owner_id: str) -> tuple[str, str]:
    if owner_type not in ("user", "anonymous"):
        raise ValueError("owner_type must be 'user' or 'anonymous'")
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id must be a non-empty string")
    return owner_type, owner_id


def _set_attraction_image_in_plan_payload(plan_payload: dict[str, Any], attraction_name: str, image_url: str) -> bool:
    changed = False
    for day in plan_payload.get("days") or []:
        if not isinstance(day, dict):
            continue
        for attraction in day.get("attractions") or []:
            if isinstance(attraction, dict) and attraction.get("name") == attraction_name:
                if attraction.get("image_url") != image_url:
                    attraction["image_url"] = image_url
                    changed = True
    return changed


def _set_attraction_image_in_result_payload(result_payload: dict[str, Any], attraction_name: str, image_url: str) -> bool:
    changed = _set_attraction_image_in_plan_payload(result_payload, attraction_name, image_url)
    for option in result_payload.get("options") or []:
        if isinstance(option, dict) and isinstance(option.get("plan"), dict):
            changed = _set_attraction_image_in_plan_payload(option["plan"], attraction_name, image_url) or changed
    return changed


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trip_reports (
    id uuid PRIMARY KEY,
    owner_type text,
    owner_id text,
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

ALTER TABLE trip_reports ADD COLUMN IF NOT EXISTS owner_type text;
ALTER TABLE trip_reports ADD COLUMN IF NOT EXISTS owner_id text;

CREATE TABLE IF NOT EXISTS trip_report_revisions (
    id uuid PRIMARY KEY,
    report_id uuid NOT NULL REFERENCES trip_reports(id) ON DELETE CASCADE,
    operation text NOT NULL,
    plan_payload jsonb NOT NULL,
    research_context_payload jsonb NOT NULL,
    budget_total integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plan_execution_logs (
    id uuid PRIMARY KEY,
    report_id uuid NOT NULL REFERENCES trip_reports(id) ON DELETE CASCADE,
    sequence integer NOT NULL,
    event_type text NOT NULL,
    component text NOT NULL,
    operation text NOT NULL,
    request_payload jsonb NOT NULL,
    response_payload jsonb,
    error text,
    duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trip_asset_cache (
    id uuid PRIMARY KEY,
    asset_type text NOT NULL,
    cache_key text NOT NULL,
    city text NOT NULL DEFAULT '',
    name text NOT NULL,
    value text NOT NULL,
    response_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_accessed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (asset_type, cache_key)
);

CREATE INDEX IF NOT EXISTS idx_trip_reports_created_at ON trip_reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_reports_city ON trip_reports (city);
CREATE INDEX IF NOT EXISTS idx_trip_reports_owner_created_at
    ON trip_reports (owner_type, owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_report_revisions_report_id ON trip_report_revisions (report_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_plan_execution_logs_report_id ON plan_execution_logs (report_id, sequence);
CREATE INDEX IF NOT EXISTS idx_plan_execution_logs_event_type ON plan_execution_logs (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_asset_cache_city_name ON trip_asset_cache (asset_type, city, name);
"""


class PostgresReportStore:
    def __init__(self, database_url: str, connection_manager: DatabaseConnectionManager | None = None):
        if (psycopg is None or dict_row is None or Jsonb is None) and connection_manager is None:
            raise RuntimeError("PostgreSQL storage requires psycopg. Run: pip install -r backend/requirements.txt")
        self.database_url = database_url
        self.connections = connection_manager or DatabaseConnectionManager(database_url)
        self._schema_ready = False

    def ensure_schema(self) -> None:
        with self.connections.connection() as conn:
            conn.execute(SCHEMA_SQL)
        self._schema_ready = True

    def _ensure_schema_once(self) -> None:
        if not self._schema_ready:
            self.ensure_schema()

    def health(self) -> dict[str, Any]:
        try:
            with self.connections.connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    row = cur.execute("SELECT 1 AS ok").fetchone()
            return {"enabled": True, "ok": bool(row and row["ok"] == 1)}
        except Exception as exc:
            logger.warning("PostgreSQL health check failed: %s", exc)
            return {"enabled": True, "ok": False, "error": str(exc)}

    def save_report(
        self,
        request: TripPlanRequest,
        result: TripPlanningResult,
        *,
        owner_type: str,
        owner_id: str,
    ) -> dict[str, Any]:
        owner_type, owner_id = _validate_owner(owner_type, owner_id)
        self._ensure_schema_once()
        report_id = str(uuid4())
        selected_plan = result.selected_plan
        request_payload = request.model_dump(mode="json")
        result_payload = result.model_dump(mode="json")
        selected_plan_payload = selected_plan.model_dump(mode="json")

        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    INSERT INTO trip_reports (
                        id, owner_type, owner_id, prompt, request_payload, result_payload, selected_plan_payload,
                        city, days_count, budget_total, generation_mode
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id::text, created_at, updated_at
                    """,
                    (
                        report_id,
                        owner_type,
                        owner_id,
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
        *,
        owner_type: str,
        owner_id: str,
    ) -> None:
        owner_type, owner_id = _validate_owner(owner_type, owner_id)
        self._ensure_schema_once()
        revision_id = str(uuid4())
        plan_payload = plan.model_dump(mode="json")
        research_payload = [snippet.model_dump(mode="json") for snippet in research_context or []]

        with self.connections.connection() as conn:
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
                    WHERE id = %s AND owner_type = %s AND owner_id = %s
                    RETURNING id
                    """,
                    (
                        Jsonb(plan_payload),
                        plan.city,
                        plan.days_count,
                        plan.budget.total,
                        plan.generation_mode,
                        report_id,
                        owner_type,
                        owner_id,
                    ),
                ).fetchone()
                if updated is None:
                    raise ReportNotFound(report_id)
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

    def save_plan_logs(self, report_id: str, logs: list[PlanLogEntry]) -> None:
        if not logs:
            return
        self._ensure_schema_once()
        rows = [
            (
                str(uuid4()),
                report_id,
                log.sequence,
                log.event_type,
                log.component,
                log.operation,
                Jsonb(log.request_payload),
                Jsonb(log.response_payload) if log.response_payload is not None else None,
                log.error,
                log.duration_ms,
                log.created_at,
            )
            for log in logs
        ]
        with self.connections.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO plan_execution_logs (
                        id, report_id, sequence, event_type, component, operation,
                        request_payload, response_payload, error, duration_ms, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
                    """,
                    rows,
                )

    def get_cached_asset(self, asset_type: str, cache_key: str) -> dict[str, Any] | None:
        self._ensure_schema_once()
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    UPDATE trip_asset_cache
                    SET last_accessed_at = now()
                    WHERE asset_type = %s AND cache_key = %s
                    RETURNING
                        id::text, asset_type, cache_key, city, name, value,
                        response_payload, created_at, updated_at, last_accessed_at
                    """,
                    (asset_type, cache_key),
                ).fetchone()
        return dict(row) if row else None

    def upsert_asset_cache(
        self,
        asset_type: str,
        cache_key: str,
        city: str,
        name: str,
        value: str,
        response_payload: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        self._ensure_schema_once()
        with self.connections.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trip_asset_cache (
                        id, asset_type, cache_key, city, name, value, response_payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (asset_type, cache_key)
                    DO UPDATE SET
                        city = EXCLUDED.city,
                        name = EXCLUDED.name,
                        value = EXCLUDED.value,
                        response_payload = EXCLUDED.response_payload,
                        updated_at = now(),
                        last_accessed_at = now()
                    """,
                    (
                        str(uuid4()),
                        asset_type,
                        cache_key,
                        city or "",
                        name,
                        value,
                        Jsonb(response_payload) if response_payload is not None else None,
                    ),
                )

    def update_report_attraction_image(
        self,
        report_id: str,
        attraction_name: str,
        image_url: str,
        *,
        owner_type: str,
        owner_id: str,
    ) -> bool:
        owner_type, owner_id = _validate_owner(owner_type, owner_id)
        self._ensure_schema_once()
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    SELECT result_payload, selected_plan_payload
                    FROM trip_reports
                    WHERE id = %s AND owner_type = %s AND owner_id = %s
                    FOR UPDATE
                    """,
                    (report_id, owner_type, owner_id),
                ).fetchone()
                if row is None:
                    raise ReportNotFound(report_id)

                result_payload = dict(row["result_payload"])
                selected_plan_payload = dict(row["selected_plan_payload"])
                changed = _set_attraction_image_in_result_payload(result_payload, attraction_name, image_url)
                changed = _set_attraction_image_in_plan_payload(selected_plan_payload, attraction_name, image_url) or changed
                if not changed:
                    return False

                updated = cur.execute(
                    """
                    UPDATE trip_reports
                    SET result_payload = %s,
                        selected_plan_payload = %s,
                        updated_at = now()
                    WHERE id = %s AND owner_type = %s AND owner_id = %s
                    RETURNING id
                    """,
                    (
                        Jsonb(result_payload),
                        Jsonb(selected_plan_payload),
                        report_id,
                        owner_type,
                        owner_id,
                    ),
                ).fetchone()
                if updated is None:
                    raise ReportNotFound(report_id)
        return True

    def list_reports(self, *, owner_type: str, owner_id: str, limit: int = 50) -> list[TripReportSummary]:
        owner_type, owner_id = _validate_owner(owner_type, owner_id)
        self._ensure_schema_once()
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    SELECT id::text, prompt, city, days_count, budget_total, generation_mode, created_at, updated_at
                    FROM trip_reports
                    WHERE owner_type = %s AND owner_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (owner_type, owner_id, max(1, min(limit, 200))),
                ).fetchall()
        return [TripReportSummary.model_validate(dict(row)) for row in rows]

    def assert_report_owner(self, report_id: str, *, owner_type: str, owner_id: str) -> None:
        owner_type, owner_id = _validate_owner(owner_type, owner_id)
        self._ensure_schema_once()
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                owned = cur.execute(
                    """
                    SELECT 1
                    FROM trip_reports
                    WHERE id = %s AND owner_type = %s AND owner_id = %s
                    """,
                    (report_id, owner_type, owner_id),
                ).fetchone()
        if owned is None:
            raise ReportNotFound(report_id)

    def get_report(self, report_id: str, *, owner_type: str, owner_id: str) -> TripReportDetail:
        owner_type, owner_id = _validate_owner(owner_type, owner_id)
        self._ensure_schema_once()
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                report = cur.execute(
                    """
                    SELECT
                        id::text, prompt, city, days_count, budget_total, generation_mode,
                        created_at, updated_at, request_payload, result_payload, selected_plan_payload
                    FROM trip_reports
                    WHERE id = %s AND owner_type = %s AND owner_id = %s
                    """,
                    (report_id, owner_type, owner_id),
                ).fetchone()
                if report is None:
                    raise ReportNotFound(report_id)
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

    def close(self) -> None:
        self.connections.close()


def create_report_store(database_url: str | None) -> PostgresReportStore | None:
    if not database_url:
        return None
    return PostgresReportStore(database_url)
