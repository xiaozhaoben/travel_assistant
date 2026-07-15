from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


from app.core.api_errors import ApiError
from app.auth.principal import create_principal_token
from app.core.config import get_settings
from app.core.rate_limit import Policy, RateLimiter
from app.core.redis_client import RedisClient, create_redis_client
import app.auth.service as auth_service
import app.main as main_module


def _user_headers(subject: str = "knowledge-admin") -> dict[str, str]:
    settings = get_settings()
    token = create_principal_token(
        subject,
        "user",
        "admin",
        settings.jwt_secret_key,
        settings.jwt_algorithm,
        30,
    )
    return {"Authorization": f"Bearer {token}"}


class FakeRedis:
    def __init__(self, *, ping_result=True, eval_error: Exception | None = None):
        self.ping_result = ping_result
        self.eval_error = eval_error
        self.calls: list[tuple] = []
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.closed = False

    def ping(self):
        self.calls.append(("ping",))
        if isinstance(self.ping_result, Exception):
            raise self.ping_result
        return self.ping_result

    def eval(self, script, numkeys, key, window_seconds):
        self.calls.append(("eval", script, numkeys, key, window_seconds))
        if self.eval_error is not None:
            raise self.eval_error
        current = self.counts.get(key, 0) + 1
        self.counts[key] = current
        if current == 1:
            self.ttls[key] = int(window_seconds)
        return [current, self.ttls[key]]

    def close(self):
        self.closed = True


def test_rate_limit_isolated_by_principal_and_hashes_subject():
    fake = FakeRedis()
    limiter = RateLimiter(fake, enabled=True)
    policy = Policy("qa", limit=2, window_seconds=60, fail_open=True)

    first = limiter.enforce(policy, "user:alice@example.test")
    second = limiter.enforce(policy, "user:alice@example.test")
    other = limiter.enforce(policy, "user:bob@example.test")

    assert (first, second, other) == (1, 0, 1)
    eval_keys = [call[3] for call in fake.calls if call[0] == "eval"]
    assert eval_keys[0] == eval_keys[1]
    assert eval_keys[0] != eval_keys[2]
    assert all("alice@example.test" not in key and "bob@example.test" not in key for key in eval_keys)
    assert all(len(key.rsplit(":", 1)[-1]) == 64 for key in eval_keys)


def test_rate_limit_exceeded_has_stable_code_and_retry_after_header():
    fake = FakeRedis()
    limiter = RateLimiter(fake, enabled=True)
    policy = Policy("login", limit=1, window_seconds=45, fail_open=False)
    limiter.enforce(policy, "ip:127.0.0.1")

    with pytest.raises(ApiError) as raised:
        limiter.enforce(policy, "ip:127.0.0.1")

    assert raised.value.status_code == 429
    assert raised.value.code == "RATE_LIMITED"
    assert raised.value.headers == {"Retry-After": "45"}


@pytest.mark.parametrize(
    ("policy", "expected_status", "expected_code"),
    [
        (Policy("qa", 20, 60, fail_open=True), None, None),
        (Policy("login", 10, 60, fail_open=False), 503, "REDIS_UNAVAILABLE"),
        (Policy("knowledge_write", 5, 60, fail_open=False), 503, "REDIS_UNAVAILABLE"),
    ],
)
def test_redis_failure_obeys_policy_fail_mode(policy, expected_status, expected_code):
    limiter = RateLimiter(FakeRedis(eval_error=RuntimeError("sensitive redis endpoint")), enabled=True)

    if expected_status is None:
        remaining = limiter.enforce(policy, "subject")
        assert remaining == policy.limit
    else:
        with pytest.raises(ApiError) as raised:
            limiter.enforce(policy, "subject")
        assert raised.value.status_code == expected_status
        assert raised.value.code == expected_code


def test_redis_health_never_exposes_connection_or_exception_details():
    secret = "redis://user:password@private.example:6379/0"
    client = RedisClient(
        FakeRedis(ping_result=RuntimeError(secret)),
        enabled=True,
    )

    health = client.health()

    assert health == {"enabled": True, "ok": False}
    assert secret not in repr(health)


def test_redis_resource_close_closes_underlying_client():
    fake = FakeRedis()
    client = RedisClient(fake, enabled=True)

    client.close()

    assert fake.closed is True


def test_create_redis_client_uses_safe_pool_options(monkeypatch):
    captured = {}

    class FakeRedisFactory:
        @classmethod
        def from_url(cls, url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeRedis()

    monkeypatch.setattr("app.core.redis_client.Redis", FakeRedisFactory)
    base = get_settings()
    settings = replace(
        base,
        redis_url="redis://example.test:6379/0",
        rate_limit_enabled=False,
        redis_connect_timeout_seconds=1.5,
        redis_read_timeout_seconds=2.5,
        redis_max_connections=20,
    )

    resource = create_redis_client(settings)

    assert resource.enabled is True
    assert captured == {
        "url": "redis://example.test:6379/0",
        "decode_responses": True,
        "socket_connect_timeout": 1.5,
        "socket_timeout": 2.5,
        "health_check_interval": 30,
        "max_connections": 20,
    }


def test_create_redis_client_supports_host_connection_fields(monkeypatch):
    captured = {}

    class FakeRedisFactory:
        def __new__(cls, **kwargs):
            captured.update(kwargs)
            return FakeRedis()

    monkeypatch.setattr("app.core.redis_client.Redis", FakeRedisFactory)
    settings = replace(
        get_settings(),
        redis_url=None,
        redis_host="cache.internal.test",
        redis_port=6380,
        redis_password="test-placeholder-password",
        redis_db=4,
        redis_connect_timeout_seconds=1.25,
        redis_read_timeout_seconds=2.25,
        redis_max_connections=12,
    )

    resource = create_redis_client(settings)

    assert resource.enabled is True
    assert captured == {
        "host": "cache.internal.test",
        "port": 6380,
        "password": "test-placeholder-password",
        "db": 4,
        "decode_responses": True,
        "socket_connect_timeout": 1.25,
        "socket_timeout": 2.25,
        "health_check_interval": 30,
        "max_connections": 12,
    }


def test_standard_redis_socket_timeout_takes_priority(monkeypatch):
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("REDIS_READ_TIMEOUT_SECONDS", "9.5")

    settings = get_settings()

    assert settings.redis_read_timeout_seconds == 3.5


def test_legacy_redis_read_timeout_remains_supported(monkeypatch):
    monkeypatch.delenv("REDIS_SOCKET_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("REDIS_READ_TIMEOUT_SECONDS", "4.5")

    settings = get_settings()

    assert settings.redis_read_timeout_seconds == 4.5


def test_rate_limit_defaults_are_disabled_without_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)

    settings = get_settings()

    assert settings.redis_url is None
    assert settings.rate_limit_enabled is False
    assert settings.rate_limit_anonymous_issue_limit == 20
    assert settings.rate_limit_login_limit == 10
    assert settings.rate_limit_qa_limit == 20
    assert settings.rate_limit_planning_limit == 5
    assert settings.rate_limit_map_limit == 60
    assert settings.rate_limit_knowledge_read_limit == 30
    assert settings.rate_limit_knowledge_write_limit == 5
    assert settings.url_fetch_total_timeout_seconds == 30.0


def test_anonymous_endpoint_uses_socket_ip_and_ignores_forwarded_header(monkeypatch):
    class RecordingLimiter:
        def __init__(self):
            self.calls = []

        def enforce(self, policy, subject):
            self.calls.append((policy, subject))
            return policy.limit - 1

    limiter = RecordingLimiter()
    resources = SimpleNamespace(
        rate_limiter=limiter,
        rate_limit_policies={
            "anonymous_issue": Policy("anonymous_issue", 20, 60, False),
        },
    )
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)

    response = TestClient(main_module.app).post(
        "/api/auth/anonymous",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )

    assert response.status_code == 200
    assert limiter.calls == [(resources.rate_limit_policies["anonymous_issue"], "ip:testclient")]


def test_api_rate_limit_response_includes_stable_code_and_retry_after(monkeypatch):
    fake = FakeRedis()
    resources = SimpleNamespace(
        rate_limiter=RateLimiter(fake, enabled=True),
        rate_limit_policies={
            "anonymous_issue": Policy("anonymous_issue", 1, 37, False),
        },
    )
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)
    client = TestClient(main_module.app)

    assert client.post("/api/auth/anonymous").status_code == 200
    response = client.post("/api/auth/anonymous")

    assert response.status_code == 429
    assert response.json()["code"] == "RATE_LIMITED"
    assert response.headers["Retry-After"] == "37"


def test_knowledge_read_is_fail_closed_at_api_boundary(monkeypatch):
    resources = SimpleNamespace(
        rate_limiter=RateLimiter(
            FakeRedis(eval_error=ConnectionError("private cache details")),
            enabled=True,
        ),
        rate_limit_policies={
            "knowledge_read": Policy("knowledge_read", 30, 60, False),
        },
    )
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)
    monkeypatch.setattr(auth_service, "get_auth_connections", lambda: object())
    monkeypatch.setattr(
        auth_service,
        "get_user_by_id",
        lambda _connections, user_id: {"id": user_id, "role": "admin"},
    )

    response = TestClient(main_module.app).get("/api/news/status", headers=_user_headers())

    assert response.status_code == 503
    assert response.json()["code"] == "REDIS_UNAVAILABLE"


def test_redis_failure_warning_is_throttled_per_policy_and_exception(monkeypatch, caplog):
    from app.core import rate_limit as rate_limit_module

    now = [100.0]
    monkeypatch.setattr(rate_limit_module, "monotonic", lambda: now[0])
    rate_limit_module._warning_timestamps.clear()
    limiter = RateLimiter(FakeRedis(eval_error=ConnectionError("private")), enabled=True)
    qa_policy = Policy("qa-throttle-test", 20, 60, True)
    planning_policy = Policy("planning-throttle-test", 5, 60, True)

    with caplog.at_level(logging.WARNING, logger="app.core.rate_limit"):
        limiter.enforce(qa_policy, "one")
        limiter.enforce(qa_policy, "two")
        limiter.enforce(planning_policy, "one")
        now[0] += 61
        limiter.enforce(qa_policy, "three")

    warnings = [record for record in caplog.records if "Redis rate limit check failed" in record.message]
    assert [(record.policy, record.exception_type) for record in warnings] == [
        ("qa-throttle-test", "ConnectionError"),
        ("planning-throttle-test", "ConnectionError"),
        ("qa-throttle-test", "ConnectionError"),
    ]


def test_health_exposes_only_safe_redis_flags(monkeypatch):
    safe_health = {"enabled": True, "ok": False}
    resources = SimpleNamespace(
        redis_client=SimpleNamespace(health=lambda: safe_health),
        report_store=None,
        travel_vector_store=None,
        qa_store=None,
    )
    monkeypatch.setattr(main_module, "get_app_resources", lambda: resources)

    response = TestClient(main_module.app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["redis"] == safe_health


def test_close_app_resources_closes_redis_resource():
    redis_resource = SimpleNamespace(closed=False)

    def close():
        redis_resource.closed = True

    redis_resource.close = close
    resources = SimpleNamespace(
        report_store=None,
        travel_vector_store=None,
        qa_store=None,
        qa_checkpointer=None,
        image_provider=None,
        redis_client=redis_resource,
        orchestrator=SimpleNamespace(amap=None, unsplash=None),
    )

    main_module.close_app_resources(resources)

    assert redis_resource.closed is True
