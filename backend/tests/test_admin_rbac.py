from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.auth.service as auth_service
import app.main as main_module
from app.auth.principal import Principal, create_principal_token
from app.auth.service import (
    UserRoleNotFound,
    change_user_role,
    configure_auth,
    create_user,
    get_current_user,
    get_user_by_id,
    get_user_by_username,
    list_user_role_audit,
    migrate_auth_schema,
    require_admin_principal,
)
from app.core.api_errors import install_api_error_handlers
from app.core.config import get_settings


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.connection.executed.append((normalized, params))
        if "INSERT INTO user_role_audit" in normalized:
            self.connection.audit_values = params
            if self.connection.fail_audit:
                raise RuntimeError("private database details")
        return self

    def fetchone(self):
        return self.connection.fetchone_value

    def fetchall(self):
        return self.connection.fetchall_value


class FakeConnection:
    def __init__(self, *, fetchone=None, fetchall=None, fail_audit=False):
        self.fetchone_value = fetchone
        self.fetchall_value = list(fetchall or [])
        self.fail_audit = fail_audit
        self.executed: list[tuple[str, object]] = []
        self.audit_values = None
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.executed.append((normalized, params))
        return FakeCursor(self)

    def cursor(self, **_kwargs):
        return FakeCursor(self)


class FakeConnectionManager:
    def __init__(self, connection: FakeConnection):
        self._connection = connection

    def connection(self):
        return self._connection


class FailingConnectionManager:
    def connection(self):
        raise RuntimeError("private database connection details")


AUTH_SECRET = "admin-rbac-test-secret"
AUTH_ALGORITHM = "HS256"


def user_headers(user_id: str = "user-1", username: str = "alice") -> dict[str, str]:
    token = create_principal_token(
        user_id,
        "user",
        username,
        AUTH_SECRET,
        AUTH_ALGORITHM,
        30,
    )
    return {"Authorization": f"Bearer {token}"}


def anonymous_headers() -> dict[str, str]:
    token = create_principal_token(
        "anon-1",
        "anonymous",
        "",
        AUTH_SECRET,
        AUTH_ALGORITHM,
        30,
    )
    return {"Authorization": f"Bearer {token}"}


def admin_dependency_client(manager) -> TestClient:
    configure_auth(manager, AUTH_SECRET, AUTH_ALGORITHM)
    app = FastAPI()
    install_api_error_handlers(app)

    @app.get("/admin")
    def admin_only(principal: Principal = Depends(require_admin_principal)):
        return {"subject": principal.subject}

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def restore_auth_configuration():
    original_connections = auth_service._connections
    yield
    auth_service._connections = original_connections
    settings = get_settings()
    auth_service.configure_principal_auth(settings.jwt_secret_key, settings.jwt_algorithm)


def test_auth_schema_migration_adds_default_role_constraint_and_audit_table():
    connection = FakeConnection()

    migrate_auth_schema(FakeConnectionManager(connection))

    sql = "\n".join(statement for statement, _params in connection.executed)
    assert "ADD COLUMN IF NOT EXISTS role" in sql
    assert "DEFAULT 'user'" in sql
    assert "UPDATE users SET role = 'user' WHERE role IS NULL" in sql
    assert "ALTER COLUMN role SET DEFAULT 'user'" in sql
    assert "ALTER COLUMN role SET NOT NULL" in sql
    assert "CHECK (role IN ('user', 'admin'))" in sql
    assert "CREATE TABLE IF NOT EXISTS user_role_audit" in sql
    assert connection.committed is True


def test_create_user_always_returns_default_user_role():
    connection = FakeConnection(
        fetchone={
            "id": "user-1",
            "username": "alice",
            "role": "user",
            "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
        }
    )

    user = create_user(FakeConnectionManager(connection), "alice", "hash")

    assert user["role"] == "user"
    insert_sql = connection.executed[0][0]
    assert "INSERT INTO users (username, password_hash)" in insert_sql
    assert "RETURNING id::text, username, role, created_at" in insert_sql


@pytest.mark.parametrize("getter", [get_user_by_username, get_user_by_id])
def test_user_queries_return_database_role(getter):
    connection = FakeConnection(
        fetchone={
            "id": "user-1",
            "username": "alice",
            "password_hash": "hash",
            "role": "admin",
            "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
        }
    )

    user = getter(FakeConnectionManager(connection), "alice" if getter is get_user_by_username else "user-1")

    assert user is not None
    assert user["role"] == "admin"
    assert " role," in connection.executed[0][0]


def test_change_user_role_updates_and_audits_in_one_transaction():
    connection = FakeConnection(fetchone={"id": "user-1", "username": "alice", "role": "user"})

    result = change_user_role(
        FakeConnectionManager(connection),
        "alice",
        "admin",
        changed_by="deploy-user",
    )

    assert result.user_id == "user-1"
    assert result.previous_role == "user"
    assert result.new_role == "admin"
    assert result.changed is True
    assert connection.committed is True
    assert connection.audit_values == ("user-1", "alice", "user", "admin", "deploy-user")
    assert any("FOR UPDATE" in sql for sql, _params in connection.executed)


def test_repeating_same_role_is_idempotent_and_does_not_audit():
    connection = FakeConnection(fetchone={"id": "user-1", "username": "alice", "role": "admin"})

    result = change_user_role(
        FakeConnectionManager(connection),
        "alice",
        "admin",
        changed_by="deploy-user",
    )

    assert result.changed is False
    assert connection.audit_values is None
    assert not any(sql.startswith("UPDATE users") for sql, _params in connection.executed)


def test_change_user_role_rejects_invalid_role_before_database_access():
    connection = FakeConnection()

    with pytest.raises(ValueError, match="unsupported user role"):
        change_user_role(FakeConnectionManager(connection), "alice", "owner", changed_by="deploy-user")

    assert connection.executed == []


def test_change_user_role_rejects_unknown_user():
    connection = FakeConnection(fetchone=None)

    with pytest.raises(UserRoleNotFound):
        change_user_role(
            FakeConnectionManager(connection),
            "missing",
            "admin",
            changed_by="deploy-user",
        )

    assert connection.audit_values is None


def test_change_user_role_rolls_back_when_audit_insert_fails():
    connection = FakeConnection(
        fetchone={"id": "user-1", "username": "alice", "role": "user"},
        fail_audit=True,
    )

    with pytest.raises(RuntimeError, match="private database details"):
        change_user_role(
            FakeConnectionManager(connection),
            "alice",
            "admin",
            changed_by="deploy-user",
        )

    assert connection.committed is False
    assert connection.rolled_back is True


def test_list_user_role_audit_is_newest_first_and_limit_is_bounded():
    connection = FakeConnection(
        fetchall=[
            {
                "previous_role": "user",
                "new_role": "admin",
                "changed_by": "deploy-user",
                "changed_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
            }
        ]
    )

    rows = list_user_role_audit(FakeConnectionManager(connection), "user-1", limit=1000)

    assert rows[0]["new_role"] == "admin"
    sql, params = connection.executed[0]
    assert "ORDER BY changed_at DESC" in sql
    assert params == ("user-1", 100)


def test_admin_dependency_allows_database_admin_with_existing_jwt():
    manager = FakeConnectionManager(
        FakeConnection(fetchone={"id": "user-1", "username": "alice", "role": "admin"})
    )

    response = admin_dependency_client(manager).get("/admin", headers=user_headers())

    assert response.status_code == 200
    assert response.json() == {"subject": "user-1"}


def test_admin_dependency_rejects_user_and_old_token_after_demotion():
    connection = FakeConnection(fetchone={"id": "user-1", "username": "alice", "role": "admin"})
    client = admin_dependency_client(FakeConnectionManager(connection))
    headers = user_headers()
    assert client.get("/admin", headers=headers).status_code == 200

    connection.fetchone_value = {"id": "user-1", "username": "alice", "role": "user"}
    response = client.get("/admin", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_ADMIN_REQUIRED"


def test_admin_dependency_rejects_anonymous_before_role_lookup():
    response = admin_dependency_client(FailingConnectionManager()).get(
        "/admin",
        headers=anonymous_headers(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_USER_REQUIRED"


def test_admin_dependency_rejects_missing_database_user():
    response = admin_dependency_client(FakeConnectionManager(FakeConnection(fetchone=None))).get(
        "/admin",
        headers=user_headers(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_ADMIN_REQUIRED"


def test_admin_dependency_fails_closed_when_role_store_is_unavailable():
    response = admin_dependency_client(FailingConnectionManager()).get(
        "/admin",
        headers=user_headers(),
    )

    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_ROLE_CHECK_UNAVAILABLE"
    assert "private database" not in response.text


def test_current_user_uses_database_role_instead_of_token_claims():
    manager = FakeConnectionManager(
        FakeConnection(fetchone={"id": "user-1", "username": "renamed", "role": "admin"})
    )
    configure_auth(manager, AUTH_SECRET, AUTH_ALGORITHM)

    current = get_current_user(user_headers(username="old-name")["Authorization"])

    assert current == {"user_id": "user-1", "username": "renamed", "role": "admin"}


def test_current_user_rejects_token_for_deleted_user():
    configure_auth(FakeConnectionManager(FakeConnection(fetchone=None)), AUTH_SECRET, AUTH_ALGORITHM)

    with pytest.raises(HTTPException) as raised:
        get_current_user(user_headers()["Authorization"])

    assert raised.value.status_code == 401
    assert raised.value.code == "AUTH_TOKEN_INVALID"


def test_current_user_fails_closed_when_database_is_unavailable():
    configure_auth(FailingConnectionManager(), AUTH_SECRET, AUTH_ALGORITHM)

    with pytest.raises(HTTPException) as raised:
        get_current_user(user_headers()["Authorization"])

    assert raised.value.status_code == 503
    assert raised.value.code == "AUTH_ROLE_CHECK_UNAVAILABLE"


def test_register_response_role_is_always_user(monkeypatch):
    monkeypatch.setattr(main_module, "get_auth_connections", lambda: object())
    monkeypatch.setattr(main_module, "get_user_by_username", lambda _connections, _username: None)
    monkeypatch.setattr(
        main_module,
        "create_user",
        lambda _connections, username, _password_hash: {
            "id": "user-new",
            "username": username,
            "role": "user",
        },
    )
    monkeypatch.setattr(main_module, "hash_password", lambda _password: "hash")

    response = TestClient(main_module.app, raise_server_exceptions=False).post(
        "/api/auth/register",
        json={"username": "new_user", "password": "secret1"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["role"] == "user"


def test_login_response_returns_database_role(monkeypatch):
    monkeypatch.setattr(main_module, "get_auth_connections", lambda: object())
    monkeypatch.setattr(
        main_module,
        "get_user_by_username",
        lambda _connections, _username: {
            "id": "admin-1",
            "username": "alice",
            "password_hash": "hash",
            "role": "admin",
        },
    )
    monkeypatch.setattr(main_module, "verify_password", lambda _plain, _hashed: True)

    response = TestClient(main_module.app, raise_server_exceptions=False).post(
        "/api/auth/login",
        json={"username": "alice", "password": "secret1"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["role"] == "admin"


def test_auth_me_response_includes_database_current_role():
    main_module.app.dependency_overrides[main_module.get_current_user] = lambda: {
        "user_id": "admin-1",
        "username": "alice",
        "role": "admin",
    }
    try:
        response = TestClient(main_module.app).get("/api/auth/me")
    finally:
        main_module.app.dependency_overrides.pop(main_module.get_current_user, None)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "user_id": "admin-1",
        "username": "alice",
        "role": "admin",
    }


class FakeCliManager:
    def __init__(self, _database_url: str):
        self.closed = False

    def close(self):
        self.closed = True


def test_admin_cli_promotes_user_and_records_local_actor(monkeypatch, capsys):
    from app.auth import admin_cli

    manager = FakeCliManager("postgresql://example")
    calls = []
    monkeypatch.setattr(admin_cli, "migrate_auth_schema", lambda current: calls.append(("migrate", current)))
    monkeypatch.setattr(
        admin_cli,
        "change_user_role",
        lambda current, username, role, *, changed_by: calls.append(
            ("change", current, username, role, changed_by)
        )
        or SimpleNamespace(username=username, previous_role="user", new_role=role, changed=True),
    )

    exit_code = admin_cli.main(
        ["promote", "alice"],
        settings_loader=lambda: SimpleNamespace(database_url="postgresql://example"),
        manager_factory=lambda _url: manager,
        actor_loader=lambda: "deploy-user",
    )

    assert exit_code == 0
    assert calls == [
        ("migrate", manager),
        ("change", manager, "alice", "admin", "deploy-user"),
    ]
    assert "alice: user -> admin" in capsys.readouterr().out
    assert manager.closed is True


def test_admin_cli_demote_is_idempotent(monkeypatch, capsys):
    from app.auth import admin_cli

    manager = FakeCliManager("postgresql://example")
    monkeypatch.setattr(admin_cli, "migrate_auth_schema", lambda _manager: None)
    monkeypatch.setattr(
        admin_cli,
        "change_user_role",
        lambda _manager, username, role, *, changed_by: SimpleNamespace(
            username=username,
            previous_role=role,
            new_role=role,
            changed=False,
        ),
    )

    exit_code = admin_cli.main(
        ["demote", "alice"],
        settings_loader=lambda: SimpleNamespace(database_url="postgresql://example"),
        manager_factory=lambda _url: manager,
        actor_loader=lambda: "deploy-user",
    )

    assert exit_code == 0
    assert "alice: 已是 user" in capsys.readouterr().out
    assert manager.closed is True


def test_admin_cli_show_prints_current_role_and_recent_audit(monkeypatch, capsys):
    from app.auth import admin_cli

    manager = FakeCliManager("postgresql://example")
    changed_at = datetime(2026, 7, 15, 8, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(admin_cli, "migrate_auth_schema", lambda _manager: None)
    monkeypatch.setattr(
        admin_cli,
        "get_user_by_username",
        lambda _manager, _username: {"id": "user-1", "username": "alice", "role": "admin"},
    )
    monkeypatch.setattr(
        admin_cli,
        "list_user_role_audit",
        lambda _manager, _user_id: [
            {
                "previous_role": "user",
                "new_role": "admin",
                "changed_by": "deploy-user",
                "changed_at": changed_at,
            }
        ],
    )

    exit_code = admin_cli.main(
        ["show", "alice"],
        settings_loader=lambda: SimpleNamespace(database_url="postgresql://example"),
        manager_factory=lambda _url: manager,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "用户名: alice" in output
    assert "角色: admin" in output
    assert "user -> admin" in output
    assert "deploy-user" in output
    assert manager.closed is True


def test_admin_cli_rejects_missing_database_configuration(capsys):
    from app.auth import admin_cli

    exit_code = admin_cli.main(
        ["show", "alice"],
        settings_loader=lambda: SimpleNamespace(database_url=None),
        manager_factory=lambda _url: pytest.fail("manager should not be created"),
    )

    assert exit_code == 2
    assert "DATABASE_URL 未配置" in capsys.readouterr().err


def test_admin_cli_unknown_user_returns_safe_error_and_closes_manager(monkeypatch, capsys):
    from app.auth import admin_cli

    manager = FakeCliManager("postgresql://example")
    monkeypatch.setattr(admin_cli, "migrate_auth_schema", lambda _manager: None)
    monkeypatch.setattr(
        admin_cli,
        "change_user_role",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(UserRoleNotFound("missing")),
    )

    exit_code = admin_cli.main(
        ["promote", "missing"],
        settings_loader=lambda: SimpleNamespace(database_url="postgresql://example"),
        manager_factory=lambda _url: manager,
    )

    assert exit_code == 2
    assert "用户不存在: missing" in capsys.readouterr().err
    assert manager.closed is True


def test_admin_cli_database_failure_does_not_expose_private_details(monkeypatch, capsys):
    from app.auth import admin_cli

    manager = FakeCliManager("postgresql://example")
    monkeypatch.setattr(
        admin_cli,
        "migrate_auth_schema",
        lambda _manager: (_ for _ in ()).throw(RuntimeError("private database details")),
    )

    exit_code = admin_cli.main(
        ["show", "alice"],
        settings_loader=lambda: SimpleNamespace(database_url="postgresql://example"),
        manager_factory=lambda _url: manager,
    )

    error = capsys.readouterr().err
    assert exit_code == 1
    assert "角色管理失败" in error
    assert "private database details" not in error
    assert manager.closed is True
