from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


from app.core.config import get_settings
from app.knowledge.job_store import (
    KnowledgeJobNotFound,
    KnowledgeJobStoreUnavailable,
    RedisKnowledgeJobStore,
)
import app.main as main_module


class FakeRedis:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        expire_error: Exception | None = None,
        execute_error: Exception | None = None,
    ):
        self.error = error
        self.expire_error = expire_error
        self.execute_error = execute_error
        self.hashes: dict[str, dict[str, str]] = {}
        self.calls: list[tuple] = []

    def pipeline(self, transaction=True):
        self.calls.append(("pipeline", transaction))
        return FakePipeline(self)

    def hset(self, key, mapping):
        self.calls.append(("hset", key, dict(mapping)))
        if self.error is not None:
            raise self.error
        self.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})
        return len(mapping)

    def hgetall(self, key):
        self.calls.append(("hgetall", key))
        if self.error is not None:
            raise self.error
        return dict(self.hashes.get(key, {}))

    def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))
        if self.expire_error is not None:
            raise self.expire_error
        return key in self.hashes


class FakePipeline:
    def __init__(self, redis: FakeRedis):
        self.redis = redis
        self.commands: list[tuple] = []

    def hset(self, key, mapping):
        self.redis.calls.append(("hset", key, dict(mapping)))
        self.commands.append(("hset", key, dict(mapping)))
        return self

    def expire(self, key, ttl):
        self.redis.calls.append(("expire", key, ttl))
        if self.redis.expire_error is not None:
            raise self.redis.expire_error
        self.commands.append(("expire", key, ttl))
        return self

    def execute(self):
        self.redis.calls.append(("execute",))
        if self.redis.execute_error is not None:
            raise self.redis.execute_error
        for command in self.commands:
            if command[0] == "hset":
                _, key, mapping = command
                self.redis.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})
        return [len(self.commands[0][2]), True]


def test_create_get_and_update_refresh_ttl_and_round_trip_json_result():
    redis = FakeRedis()
    store = RedisKnowledgeJobStore(redis, ttl_seconds=123)

    created = store.create(source_type="upload", message="queued")
    loaded = store.get(created.job_id)
    updated = store.update(
        created.job_id,
        status="completed",
        message="done",
        result={"doc_id": "doc-1", "chunks_added": 2},
        error_code=None,
    )

    assert loaded == created
    assert updated.status == "completed"
    assert updated.result == {"doc_id": "doc-1", "chunks_added": 2}
    assert [call for call in redis.calls if call[0] == "pipeline"] == [
        ("pipeline", True),
        ("pipeline", True),
    ]
    key = f"travel-assistant:knowledge-job:{created.job_id}"
    assert [call for call in redis.calls if call[0] == "expire"] == [
        ("expire", key, 123),
        ("expire", key, 123),
    ]
    assert json.loads(redis.hashes[key]["result_json"]) == {"doc_id": "doc-1", "chunks_added": 2}


def test_hash_schema_never_contains_request_or_user_content():
    redis = FakeRedis()
    store = RedisKnowledgeJobStore(redis, ttl_seconds=60)
    secret_content = "private request body and URL https://internal.example/secret"

    job = store.create(source_type="url", message="created")
    store.update(job.job_id, status="failed", error_code="KNOWLEDGE_INGEST_FAILED")

    key = f"travel-assistant:knowledge-job:{job.job_id}"
    assert set(redis.hashes[key]) == {
        "job_id",
        "status",
        "message",
        "source_type",
        "result_json",
        "error_code",
        "created_at",
        "updated_at",
    }
    serialized = json.dumps(redis.hashes[key], ensure_ascii=False)
    assert secret_content not in serialized
    assert "request" not in redis.hashes[key]
    assert "content" not in redis.hashes[key]
    assert "url" not in redis.hashes[key]


def test_expire_queue_failure_does_not_leave_hash_without_ttl():
    redis = FakeRedis(expire_error=RuntimeError("expire failed"))
    store = RedisKnowledgeJobStore(redis, ttl_seconds=60)

    with pytest.raises(KnowledgeJobStoreUnavailable):
        store.create(source_type="upload", message="queued")

    assert redis.hashes == {}


def test_transaction_execute_failure_does_not_leave_hash_without_ttl():
    redis = FakeRedis(execute_error=RuntimeError("execute failed"))
    store = RedisKnowledgeJobStore(redis, ttl_seconds=60)

    with pytest.raises(KnowledgeJobStoreUnavailable):
        store.create(source_type="upload", message="queued")

    assert redis.hashes == {}


def test_unknown_job_has_stable_not_found_exception():
    store = RedisKnowledgeJobStore(FakeRedis(), ttl_seconds=60)

    with pytest.raises(KnowledgeJobNotFound):
        store.get("missing")
    with pytest.raises(KnowledgeJobNotFound):
        store.update("missing", status="running")


def test_missing_or_failed_redis_has_stable_unavailable_exception():
    unavailable = RedisKnowledgeJobStore(None, ttl_seconds=60)
    failed = RedisKnowledgeJobStore(FakeRedis(error=RuntimeError("redis://secret@endpoint")), ttl_seconds=60)

    with pytest.raises(KnowledgeJobStoreUnavailable):
        unavailable.create(source_type="upload", message="queued")
    with pytest.raises(KnowledgeJobStoreUnavailable) as raised:
        failed.get("job-1")

    assert "secret" not in str(raised.value)


def test_settings_default_knowledge_job_ttl_is_seven_days(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_JOB_TTL_SECONDS", raising=False)

    settings = get_settings()

    assert settings.knowledge_job_ttl_seconds == 7 * 24 * 60 * 60


def test_create_app_resources_builds_job_store_even_when_rate_limit_is_disabled(monkeypatch):
    settings = replace(
        get_settings(),
        redis_url=None,
        redis_host=None,
        rate_limit_enabled=False,
        knowledge_job_ttl_seconds=321,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "create_report_store", lambda *_: None)
    monkeypatch.setattr(main_module, "create_travel_vector_store", lambda *_: None)
    monkeypatch.setattr(main_module, "create_qa_conversation_store", lambda *_: None)
    monkeypatch.setattr(main_module, "create_qa_checkpointer", lambda *_: None)
    monkeypatch.setattr(main_module, "TravelAgentOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr(main_module, "TravelNewsIngestionAgent", lambda *_: object())
    monkeypatch.setattr(main_module, "TravelQuestionAnsweringAgent", lambda *_, **__: object())
    monkeypatch.setattr(main_module, "UnsplashMCPClient", lambda *_, **__: object())

    resources = main_module.create_app_resources()

    assert isinstance(resources.knowledge_job_store, RedisKnowledgeJobStore)
    with pytest.raises(KnowledgeJobStoreUnavailable):
        resources.knowledge_job_store.create(source_type="upload", message="queued")


def test_job_status_endpoint_delegates_to_resource_store_and_maps_errors(monkeypatch):
    redis = FakeRedis()
    store = RedisKnowledgeJobStore(redis, ttl_seconds=60)
    job = store.create(source_type="upload", message="created")
    resources = SimpleNamespace(knowledge_job_store=store)
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)

    response = TestClient(main_module.app).get(f"/api/knowledge/documents/jobs/{job.job_id}")
    missing = TestClient(main_module.app).get("/api/knowledge/documents/jobs/missing")

    assert response.status_code == 200
    assert response.json()["data"]["job_id"] == job.job_id
    assert missing.status_code == 404
    assert missing.json()["code"] == "KNOWLEDGE_JOB_NOT_FOUND"


def test_job_status_endpoint_has_no_in_memory_fallback(monkeypatch):
    resources = SimpleNamespace(knowledge_job_store=RedisKnowledgeJobStore(None, ttl_seconds=60))
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)

    response = TestClient(main_module.app).get("/api/knowledge/documents/jobs/any")

    assert response.status_code == 503
    assert response.json()["code"] == "KNOWLEDGE_JOB_STORE_UNAVAILABLE"
    assert not hasattr(main_module, "knowledge_ingest_jobs")


def test_create_job_endpoint_maps_missing_resource_store_to_stable_503(monkeypatch):
    resources = SimpleNamespace(knowledge_job_store=None)
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)

    response = TestClient(main_module.app, raise_server_exceptions=False).post(
        "/api/knowledge/documents/auto/jobs",
        json={"file_name": "guide.md", "content": "private travel notes"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "KNOWLEDGE_JOB_STORE_UNAVAILABLE"


class _FakeOrchestrator:
    def __init__(self):
        self.planner = SimpleNamespace(llm=None)
        self.amap = object()
        self.unsplash = object()

    def configure_reflection_memory(self, _store):
        return None
