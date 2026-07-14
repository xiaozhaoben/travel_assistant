from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth.principal import create_principal_token
from app.core.config import get_settings


settings = get_settings()


def auth_headers(principal_type: str, subject: str = "principal-1") -> dict[str, str]:
    token = create_principal_token(
        subject,
        principal_type,
        "admin" if principal_type == "user" else "",
        settings.jwt_secret_key,
        settings.jwt_algorithm,
        30,
    )
    return {"Authorization": f"Bearer {token}"}


MANAGED_ENDPOINTS = [
    ("post", "/api/news/ingest", {"feed_urls": []}),
    ("post", "/api/knowledge/documents", {"title": "Guide", "content": "Body"}),
    ("post", "/api/knowledge/documents/from-url", {"source_url": "https://example.com/guide"}),
    ("post", "/api/knowledge/documents/auto", {"content": "Body"}),
    ("post", "/api/knowledge/documents/from-url/jobs", {"source_url": "https://example.com/guide"}),
    ("post", "/api/knowledge/documents/auto/jobs", {"content": "Body"}),
    ("get", "/api/knowledge/documents/jobs/job-1", None),
    ("post", "/api/knowledge/search", {"query": "Chengdu"}),
    ("get", "/api/news/status", None),
]


@pytest.mark.parametrize(("method", "path", "payload"), MANAGED_ENDPOINTS)
def test_knowledge_management_endpoints_require_bearer_token(method, path, payload):
    client = TestClient(main_module.app)

    response = client.request(method, path, json=payload)

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize(("method", "path", "payload"), MANAGED_ENDPOINTS)
def test_knowledge_management_endpoints_reject_anonymous_principal(method, path, payload):
    client = TestClient(main_module.app)

    response = client.request(method, path, json=payload, headers=auth_headers("anonymous"))

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_USER_REQUIRED"


def test_user_principal_reaches_knowledge_business_layer(monkeypatch):
    calls = []

    class Store:
        def ingest_document(self, **kwargs):
            calls.append(kwargs)
            return {"doc_id": "doc-1", "chunks_added": 1}

    store = Store()
    resources = main_module.get_app_resources()
    monkeypatch.setattr(main_module, "travel_vector_store", store)
    monkeypatch.setattr(resources, "travel_vector_store", store)

    response = TestClient(main_module.app).post(
        "/api/knowledge/documents",
        headers=auth_headers("user", "user-1"),
        json={"title": "Guide", "content": "Public guide"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"doc_id": "doc-1", "chunks_added": 1}
    assert calls[0]["title"] == "Guide"


def test_private_url_is_rejected_before_business_fetch_and_uses_api_error_shape(monkeypatch):
    calls = []

    class Store:
        def ingest_document(self, **kwargs):
            calls.append(kwargs)
            return {"doc_id": "unexpected", "chunks_added": 1}

    resources = main_module.get_app_resources()
    store = Store()
    monkeypatch.setattr(main_module, "travel_vector_store", store)
    monkeypatch.setattr(resources, "travel_vector_store", store)
    monkeypatch.setattr(resources.safe_url_fetcher, "resolver", lambda _host, _port: ["127.0.0.1"])

    response = TestClient(main_module.app).post(
        "/api/knowledge/documents/from-url",
        headers=auth_headers("user", "user-1"),
        json={"source_url": "https://example.com/internal"},
    )

    assert response.status_code == 403
    assert response.json()["success"] is False
    assert response.json()["code"] == "URL_FORBIDDEN"
    assert "request_id" in response.json()
    assert calls == []


def test_knowledge_limit_uses_required_user_principal_subject(monkeypatch):
    class RecordingLimiter:
        def __init__(self):
            self.calls = []

        def enforce(self, policy, subject):
            self.calls.append((policy, subject))
            return policy.limit - 1

    limiter = RecordingLimiter()
    resources = SimpleNamespace(
        rate_limiter=limiter,
        rate_limit_policies={"knowledge_read": SimpleNamespace(limit=30)},
        travel_vector_store=SimpleNamespace(health=lambda: {"enabled": True, "ok": True}),
    )
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)

    response = TestClient(main_module.app).get(
        "/api/news/status",
        headers=auth_headers("user", "user-42"),
    )

    assert response.status_code == 200
    assert limiter.calls[0][1] == "principal:user:user-42"
