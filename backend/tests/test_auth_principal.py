from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt

from app.auth.principal import (
    Principal,
    configure_principal_auth,
    create_principal_token,
    decode_principal_token,
    get_current_principal,
    get_current_principal_optional,
)
from app.core.api_errors import api_error, install_api_error_handlers
from app.core.config import get_settings
from app.domain.models import TravelQARequest


SECRET = "principal-test-secret"
ALGORITHM = "HS256"


@pytest.fixture(autouse=True)
def _restore_principal_auth_configuration():
    yield
    settings = get_settings()
    configure_principal_auth(settings.jwt_secret_key, settings.jwt_algorithm)


def _token(subject: str, principal_type: str, username: str = "", expire_minutes: int = 30) -> str:
    return create_principal_token(
        subject=subject,
        principal_type=principal_type,
        username=username,
        secret=SECRET,
        algorithm=ALGORITHM,
        expire_minutes=expire_minutes,
    )


def _dependency_client() -> TestClient:
    configure_principal_auth(SECRET, ALGORITHM)
    app = FastAPI()
    install_api_error_handlers(app)

    @app.get("/required")
    def required(principal: Principal = Depends(get_current_principal)):
        return {
            "subject": principal.subject,
            "principal_type": principal.principal_type,
            "username": principal.username,
        }

    @app.get("/optional")
    def optional(principal: Principal | None = Depends(get_current_principal_optional)):
        return {"subject": principal.subject if principal else None}

    @app.get("/teapot")
    def teapot():
        raise api_error(418, "TEAPOT", "short and stout")

    @app.get("/plain-error")
    def plain_error():
        raise HTTPException(status_code=409, detail="database password leaked")

    @app.get("/validated")
    def validated(count: int):
        return {"count": count}

    @app.get("/crash")
    def crash():
        raise RuntimeError("secret token must not leak")

    return TestClient(app, raise_server_exceptions=False)


def test_anonymous_token_round_trip():
    token = _token("anon-123", "anonymous")

    principal = decode_principal_token(token, SECRET, ALGORITHM)

    assert principal == Principal(subject="anon-123", principal_type="anonymous")
    assert principal.anonymous_id == "anon-123"
    assert principal.user_id is None


def test_user_token_round_trip():
    token = _token("user-123", "user", username="alice")

    principal = decode_principal_token(token, SECRET, ALGORITHM)

    assert principal == Principal(subject="user-123", principal_type="user", username="alice")
    assert principal.user_id == "user-123"
    assert principal.anonymous_id is None


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: _token("anon-123", "anonymous") + "tampered", id="tampered"),
        pytest.param(lambda: _token("anon-123", "anonymous", expire_minutes=-1), id="expired"),
    ],
)
def test_invalid_principal_token_is_unauthorized(token):
    with pytest.raises(HTTPException) as exc_info:
        decode_principal_token(token(), SECRET, ALGORITHM)

    assert exc_info.value.status_code == 401


def test_unknown_principal_type_is_rejected():
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "service-123",
            "principal_type": "service",
            "preferred_username": "",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(HTTPException) as exc_info:
        decode_principal_token(token, SECRET, ALGORITHM)

    assert exc_info.value.status_code == 401


def test_legacy_user_token_with_username_is_accepted():
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "legacy-user",
            "preferred_username": "old-alice",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    principal = decode_principal_token(token, SECRET, ALGORITHM)

    assert principal == Principal(subject="legacy-user", principal_type="user", username="old-alice")


@pytest.mark.parametrize(
    "path,authorization",
    [
        ("/required", None),
        ("/required", "Basic abc"),
        ("/required", "Bearer"),
        ("/required", "Bearer invalid-token"),
        ("/optional", "Basic abc"),
        ("/optional", "Bearer"),
        ("/optional", "Bearer invalid-token"),
    ],
)
def test_missing_or_invalid_authorization_is_unauthorized(path: str, authorization: str | None):
    client = _dependency_client()
    headers = {"Authorization": authorization} if authorization is not None else {}

    response = client.get(path, headers=headers)

    assert response.status_code == 401
    assert response.json()["success"] is False
    assert response.json()["request_id"]


def test_optional_principal_only_allows_truly_missing_authorization():
    client = _dependency_client()

    response = client.get("/optional")

    assert response.status_code == 200
    assert response.json() == {"subject": None}


def test_valid_bearer_header_returns_principal():
    client = _dependency_client()

    response = client.get("/required", headers={"Authorization": f"Bearer {_token('user-1', 'user', 'alice')}"})

    assert response.status_code == 200
    assert response.json() == {"subject": "user-1", "principal_type": "user", "username": "alice"}


def test_api_error_handler_returns_stable_shape_and_request_id():
    client = _dependency_client()

    response = client.get("/teapot", headers={"X-Request-ID": "client-request-123"})

    assert response.status_code == 418
    assert response.headers["X-Request-ID"] == "client-request-123"
    assert response.json() == {
        "success": False,
        "code": "TEAPOT",
        "message": "short and stout",
        "request_id": "client-request-123",
    }


def test_plain_http_exception_is_sanitized_and_bad_request_id_is_replaced():
    client = _dependency_client()

    response = client.get("/plain-error", headers={"X-Request-ID": "bad id with spaces"})

    assert response.status_code == 409
    assert response.json()["success"] is False
    assert response.json()["code"] == "HTTP_409"
    assert response.json()["message"] == "请求冲突"
    assert "database password leaked" not in response.text
    assert response.json()["request_id"] != "bad id with spaces"
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_request_validation_error_has_stable_shape_and_request_id():
    client = _dependency_client()

    response = client.get("/validated?count=not-an-integer", headers={"X-Request-ID": "validation-123"})

    assert response.status_code == 422
    assert response.json() == {
        "success": False,
        "code": "REQUEST_VALIDATION_FAILED",
        "message": "请求参数校验失败",
        "request_id": "validation-123",
    }
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_unhandled_exception_has_stable_shape_without_exception_details():
    client = _dependency_client()

    response = client.get("/crash", headers={"X-Request-ID": "internal-error-123"})

    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "code": "INTERNAL_ERROR",
        "message": "服务器内部错误",
        "request_id": "internal-error-123",
    }
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert "secret token must not leak" not in response.text


def test_travel_qa_request_ignores_legacy_identity_fields():
    request = TravelQARequest.model_validate(
        {"question": "南京怎么玩？", "user_id": "spoofed-user", "anonymous_id": "spoofed-anon"}
    )

    assert "user_id" not in TravelQARequest.model_fields
    assert "anonymous_id" not in TravelQARequest.model_fields
    assert request.model_dump() == {
        "question": "南京怎么玩？",
        "top_k": 5,
        "conversation_id": None,
    }


def test_anonymous_endpoint_issues_server_generated_principal_token():
    from app.main import app, settings

    client = TestClient(app)
    response = client.post("/api/auth/anonymous")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["principal_type"] == "anonymous"
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == settings.anonymous_jwt_expire_minutes * 60
    principal = decode_principal_token(data["access_token"], settings.jwt_secret_key, settings.jwt_algorithm)
    assert principal.subject == data["subject"]
    assert principal.principal_type == "anonymous"
