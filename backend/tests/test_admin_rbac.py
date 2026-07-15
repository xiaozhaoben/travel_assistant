from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.auth.service import (
    UserRoleNotFound,
    change_user_role,
    create_user,
    get_user_by_id,
    get_user_by_username,
    list_user_role_audit,
    migrate_auth_schema,
)


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


def test_auth_schema_migration_adds_default_role_constraint_and_audit_table():
    connection = FakeConnection()

    migrate_auth_schema(FakeConnectionManager(connection))

    sql = "\n".join(statement for statement, _params in connection.executed)
    assert "ADD COLUMN IF NOT EXISTS role" in sql
    assert "DEFAULT 'user'" in sql
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
