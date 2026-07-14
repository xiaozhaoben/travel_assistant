from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.storage.report_store as report_store_module
from app.auth.principal import create_principal_token
from app.domain.models import TripPlanRequest, TripPlanningResult, TripReportDetail, TripReportSummary
from app.main import app, settings
from app.storage.report_store import PostgresReportStore, SCHEMA_SQL
from app.workflows.agents import TravelAgentOrchestrator


def _token(subject: str, principal_type: str) -> str:
    return create_principal_token(
        subject=subject,
        principal_type=principal_type,
        username="tester" if principal_type == "user" else "",
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.jwt_expire_minutes,
    )


def _headers(subject: str, principal_type: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject, principal_type)}"}


class OwnedReportStore:
    def __init__(self):
        self.reports: dict[str, dict] = {}
        self.updated_plans: list[tuple[str, str, str]] = []
        self.updated_images: list[tuple[str, str, str, str]] = []

    def health(self):
        return {"enabled": True, "ok": True}

    def save_report(self, request, result, *, owner_type, owner_id):
        report_id = str(uuid4())
        now = datetime.now(timezone.utc)
        self.reports[report_id] = {
            "id": report_id,
            "prompt": request.prompt,
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "selected_plan": result.selected_plan.model_dump(mode="json"),
            "city": result.city,
            "days_count": result.days_count,
            "budget_total": result.budget.total,
            "generation_mode": result.generation_mode,
            "created_at": now,
            "updated_at": now,
            "owner_type": owner_type,
            "owner_id": owner_id,
            "revisions": [],
        }
        return {"id": report_id, "created_at": now, "updated_at": now}

    def save_plan_logs(self, report_id, logs):
        return None

    def _owned(self, report_id, owner_type, owner_id):
        report = self.reports.get(report_id)
        if report is None or (report["owner_type"], report["owner_id"]) != (owner_type, owner_id):
            raise report_store_module.ReportNotFound(report_id)
        return report

    def list_reports(self, *, owner_type, owner_id, limit=50):
        return [
            TripReportSummary.model_validate(report)
            for report in self.reports.values()
            if (report["owner_type"], report["owner_id"]) == (owner_type, owner_id)
        ][:limit]

    def get_report(self, report_id, *, owner_type, owner_id):
        return TripReportDetail.model_validate(self._owned(report_id, owner_type, owner_id))

    def update_report_plan(self, report_id, plan, operation, research_context, *, owner_type, owner_id):
        report = self._owned(report_id, owner_type, owner_id)
        report["selected_plan"] = plan.model_dump(mode="json")
        report["budget_total"] = plan.budget.total
        self.updated_plans.append((report_id, owner_type, owner_id))

    def update_report_attraction_image(self, report_id, attraction_name, image_url, *, owner_type, owner_id):
        self._owned(report_id, owner_type, owner_id)
        self.updated_images.append((report_id, owner_type, owner_id, attraction_name))
        return True

    def get_cached_asset(self, asset_type, cache_key):
        return {"value": "https://img.example.test/cached.jpg"}

    def upsert_asset_cache(self, *args, **kwargs):
        return None


class FakeImageProvider:
    def image_for(self, query):
        return "https://img.example.test/provider.jpg"


@pytest.fixture
def report_api(monkeypatch):
    store = OwnedReportStore()
    resources = SimpleNamespace(
        orchestrator=TravelAgentOrchestrator(disable_llm=True, disable_external_api=True),
        report_store=store,
        image_provider=FakeImageProvider(),
    )
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)
    return TestClient(app), store


def _create_report(client: TestClient, headers: dict[str, str], **extra):
    payload = {"prompt": "我想去北京玩 1 天，喜欢历史文化，预算中等", **extra}
    response = client.post("/api/trip/plan", json=payload, headers=headers)
    assert response.status_code == 200
    return response.json()["data"]


def test_reports_are_isolated_for_user_and_anonymous_principals(report_api):
    client, store = report_api
    user_headers = _headers("user-a", "user")
    anonymous_headers = _headers("anon-a", "anonymous")
    user_report = _create_report(
        client,
        user_headers,
        owner_type="anonymous",
        owner_id="forged-owner",
    )
    anonymous_report = _create_report(client, anonymous_headers)

    assert store.reports[user_report["report_id"]]["owner_type"] == "user"
    assert store.reports[user_report["report_id"]]["owner_id"] == "user-a"
    assert [item["id"] for item in client.get("/api/reports", headers=user_headers).json()["data"]] == [
        user_report["report_id"]
    ]
    assert [item["id"] for item in client.get("/api/reports", headers=anonymous_headers).json()["data"]] == [
        anonymous_report["report_id"]
    ]

    assert client.get(f"/api/reports/{anonymous_report['report_id']}", headers=user_headers).status_code == 404
    assert client.get(f"/api/reports/{user_report['report_id']}", headers=anonymous_headers).status_code == 404


def test_recalculate_and_photo_report_writes_reject_cross_owner(report_api):
    client, store = report_api
    user_headers = _headers("user-a", "user")
    other_headers = _headers("user-b", "user")
    report = _create_report(client, user_headers)
    report_id = report["report_id"]
    plan = report["options"][0]["plan"]

    denied_recalc = client.post(
        "/api/trip/recalculate",
        json={"report_id": report_id, "plan": plan},
        headers=other_headers,
    )
    denied_photo = client.get(
        "/api/poi/photo",
        params={"name": "故宫博物院", "city": "北京", "report_id": report_id},
        headers=other_headers,
    )

    assert denied_recalc.status_code == 404
    assert denied_photo.status_code == 404
    assert not any(item[0] == report_id and item[2] == "user-b" for item in store.updated_plans)
    assert not any(item[0] == report_id and item[2] == "user-b" for item in store.updated_images)

    allowed_recalc = client.post(
        "/api/trip/recalculate",
        json={"report_id": report_id, "plan": plan},
        headers=user_headers,
    )
    allowed_photo = client.get(
        "/api/poi/photo",
        params={"name": "故宫博物院", "city": "北京", "report_id": report_id},
        headers=user_headers,
    )
    assert allowed_recalc.status_code == 200
    assert allowed_photo.status_code == 200
    assert (report_id, "user", "user-a") in store.updated_plans
    assert any(item[:3] == (report_id, "user", "user-a") for item in store.updated_images)


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("post", "/api/trip/plan", {"prompt": "我想去北京玩 1 天，喜欢历史文化，预算中等"}),
        ("get", "/api/reports", None),
        ("get", "/api/reports/missing", None),
        ("post", "/api/trip/recalculate", {"plan": {}}),
        ("get", "/api/poi/photo?name=故宫&report_id=missing", None),
    ],
)
def test_report_endpoints_require_bearer_identity(report_api, method, path, json):
    client, _ = report_api
    response = client.request(method, path, json=json)
    assert response.status_code == 401


def test_photo_without_report_id_remains_public(report_api):
    client, _ = report_api
    response = client.get("/api/poi/photo", params={"name": "故宫博物院", "city": "北京"})
    assert response.status_code == 200


def test_report_schema_adds_nullable_owner_columns_and_owner_index():
    normalized = " ".join(SCHEMA_SQL.split()).lower()
    assert "owner_type text" in normalized
    assert "owner_id text" in normalized
    assert "(owner_type, owner_id, created_at desc)" in normalized


@pytest.mark.parametrize(
    ("owner_type", "owner_id"),
    [("admin", "owner"), ("user", ""), ("anonymous", "   ")],
)
def test_report_store_rejects_invalid_owner_before_database_access(owner_type, owner_id):
    store = object.__new__(PostgresReportStore)
    store._schema_ready = False
    with pytest.raises(ValueError):
        store.list_reports(owner_type=owner_type, owner_id=owner_id)


def test_legacy_unowned_reports_are_not_visible(report_api):
    client, store = report_api
    headers = _headers("user-a", "user")
    report = _create_report(client, headers)
    store.reports[report["report_id"]]["owner_type"] = None
    store.reports[report["report_id"]]["owner_id"] = None

    assert client.get("/api/reports", headers=headers).json()["data"] == []
    assert client.get(f"/api/reports/{report['report_id']}", headers=headers).status_code == 404


class RecordingCursor:
    def __init__(self, statements):
        self.statements = statements
        self._one = None
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, tuple(params)))
        self._all = []
        if normalized.startswith("INSERT INTO trip_reports"):
            self._one = {
                "id": params[0],
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        elif normalized.startswith("UPDATE trip_reports SET selected_plan_payload"):
            self._one = {"id": params[5]}
        elif normalized.startswith("SELECT result_payload, selected_plan_payload"):
            self._one = None
        elif normalized.startswith("SELECT id::text, prompt"):
            self._one = None
            self._all = []
        elif "FROM trip_reports WHERE id" in normalized:
            self._one = None
        else:
            self._one = None
        return self

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class RecordingConnection:
    def __init__(self, statements):
        self.statements = statements

    def cursor(self, row_factory=None):
        return RecordingCursor(self.statements)


class RecordingConnections:
    def __init__(self):
        self.statements = []

    @contextmanager
    def connection(self):
        yield RecordingConnection(self.statements)


def test_postgres_report_queries_bind_owner_to_each_report_operation(report_api, monkeypatch):
    client, api_store = report_api
    headers = _headers("user-a", "user")
    created = _create_report(client, headers)
    saved = api_store.reports[created["report_id"]]
    connections = RecordingConnections()
    store = PostgresReportStore("postgresql://unused", connection_manager=connections)
    store._schema_ready = True
    monkeypatch.setattr(report_store_module, "Jsonb", lambda value: value)

    store.save_report(
        TripPlanRequest.model_validate(saved["request"]),
        TripPlanningResult.model_validate(saved["result"]),
        owner_type="user",
        owner_id="user-a",
    )
    assert store.list_reports(owner_type="user", owner_id="user-a") == []
    with pytest.raises(report_store_module.ReportNotFound):
        store.get_report(created["report_id"], owner_type="user", owner_id="user-a")
    with pytest.raises(report_store_module.ReportNotFound):
        store.update_report_attraction_image(
            created["report_id"],
            "故宫博物院",
            "https://img.example.test/new.jpg",
            owner_type="user",
            owner_id="user-a",
        )
    store.update_report_plan(
        created["report_id"],
        TripPlanningResult.model_validate(saved["result"]).selected_plan,
        "recalculate_only",
        owner_type="user",
        owner_id="user-a",
    )

    report_statements = [item for item in connections.statements if "trip_reports" in item[0]]
    assert report_statements
    for sql, params in report_statements:
        if sql.startswith("INSERT INTO trip_reports"):
            assert "owner_type, owner_id" in sql
            assert params[1:3] == ("user", "user-a")
        else:
            assert "owner_type = %s AND owner_id = %s" in sql
            assert any(params[index : index + 2] == ("user", "user-a") for index in range(len(params) - 1))
