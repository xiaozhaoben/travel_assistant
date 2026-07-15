from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException, status

from app.auth.principal import (
    configure_principal_auth,
    create_principal_token,
    decode_principal_token,
    get_current_principal,
    get_current_principal_optional,
    require_user_principal,
)
from app.storage.db import DatabaseConnectionManager

logger = logging.getLogger(__name__)

try:
    from passlib.context import CryptContext
except ImportError:  # pragma: no cover
    CryptContext = None  # type: ignore[assignment,misc]

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text UNIQUE NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'user',
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

USERS_ROLE_MIGRATION_SQL = """
ALTER TABLE users
ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'user';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'users_role_check'
          AND conrelid = 'users'::regclass
    ) THEN
        ALTER TABLE users
        ADD CONSTRAINT users_role_check CHECK (role IN ('user', 'admin'));
    END IF;
END
$$;
"""

USER_ROLE_AUDIT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_role_audit (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    username text NOT NULL,
    previous_role text NOT NULL CHECK (previous_role IN ('user', 'admin')),
    new_role text NOT NULL CHECK (new_role IN ('user', 'admin')),
    changed_by text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_role_audit_user_changed_at
ON user_role_audit (user_id, changed_at DESC);
"""


@dataclass(frozen=True)
class RoleChangeResult:
    user_id: str
    username: str
    previous_role: str
    new_role: str
    changed: bool


class UserRoleNotFound(LookupError):
    pass

_pwd_context: CryptContext | None = None


def _get_pwd_context() -> CryptContext:
    global _pwd_context
    if _pwd_context is None:
        if CryptContext is None:
            raise RuntimeError("passlib is required. Run: pip install passlib[bcrypt]")
        _pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return _pwd_context


def hash_password(plain: str) -> str:
    return _get_pwd_context().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _get_pwd_context().verify(plain, hashed)


def create_access_token(
    user_id: str,
    username: str,
    secret: str,
    algorithm: str,
    expire_minutes: int,
) -> str:
    return create_principal_token(
        subject=user_id,
        principal_type="user",
        username=username,
        secret=secret,
        algorithm=algorithm,
        expire_minutes=expire_minutes,
    )


def decode_access_token(token: str, secret: str, algorithm: str) -> dict[str, Any]:
    principal = decode_principal_token(token, secret, algorithm)
    if principal.principal_type != "user":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的用户认证凭据")
    return {"user_id": principal.subject, "username": principal.username}


def migrate_auth_schema(connections: DatabaseConnectionManager) -> None:
    with connections.connection() as conn:
        conn.execute(USERS_TABLE_SQL)
        conn.execute(USERS_ROLE_MIGRATION_SQL)
        conn.execute(USER_ROLE_AUDIT_TABLE_SQL)


def ensure_users_table(connections: DatabaseConnectionManager) -> None:
    try:
        migrate_auth_schema(connections)
        logger.info("Users table ensured")
    except Exception as exc:
        logger.warning("Failed to ensure users table: %s", exc)


def get_user_by_username(connections: DatabaseConnectionManager, username: str) -> dict[str, Any] | None:
    try:
        from psycopg.rows import dict_row
    except ImportError:
        return None
    with connections.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT id::text, username, password_hash, role, created_at FROM users WHERE username = %s",
                (username,),
            ).fetchone()
    return dict(row) if row else None


def get_user_by_id(connections: DatabaseConnectionManager, user_id: str) -> dict[str, Any] | None:
    try:
        from psycopg.rows import dict_row
    except ImportError:
        return None
    with connections.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT id::text, username, password_hash, role, created_at FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
    return dict(row) if row else None


def create_user(connections: DatabaseConnectionManager, username: str, password_hash: str) -> dict[str, Any]:
    from psycopg.rows import dict_row

    with connections.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                """
                INSERT INTO users (username, password_hash)
                VALUES (%s, %s)
                RETURNING id::text, username, role, created_at
                """,
                (username, password_hash),
            ).fetchone()
    return dict(row)


def change_user_role(
    connections: DatabaseConnectionManager,
    username: str,
    new_role: str,
    *,
    changed_by: str,
) -> RoleChangeResult:
    if new_role not in {"user", "admin"}:
        raise ValueError("unsupported user role")
    actor = changed_by.strip() or "unknown"
    from psycopg.rows import dict_row

    with connections.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            user = cur.execute(
                "SELECT id::text, username, role FROM users WHERE username = %s FOR UPDATE",
                (username,),
            ).fetchone()
            if user is None:
                raise UserRoleNotFound(username)
            user_id = str(user["id"])
            current_username = str(user["username"])
            previous_role = str(user["role"])
            if previous_role == new_role:
                return RoleChangeResult(
                    user_id=user_id,
                    username=current_username,
                    previous_role=previous_role,
                    new_role=new_role,
                    changed=False,
                )
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
            cur.execute(
                """
                INSERT INTO user_role_audit
                    (user_id, username, previous_role, new_role, changed_by)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, current_username, previous_role, new_role, actor),
            )
            return RoleChangeResult(
                user_id=user_id,
                username=current_username,
                previous_role=previous_role,
                new_role=new_role,
                changed=True,
            )


def list_user_role_audit(
    connections: DatabaseConnectionManager,
    user_id: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    try:
        from psycopg.rows import dict_row
    except ImportError:
        return []
    bounded_limit = max(1, min(int(limit), 100))
    with connections.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT previous_role, new_role, changed_by, changed_at
                FROM user_role_audit
                WHERE user_id = %s
                ORDER BY changed_at DESC
                LIMIT %s
                """,
                (user_id, bounded_limit),
            ).fetchall()
    return [dict(row) for row in rows]


def merge_anonymous_conversations(
    connections: DatabaseConnectionManager,
    user_id: str,
    anonymous_id: str,
) -> int:
    with connections.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE travel_qa_conversations
                SET user_id = %s, anonymous_id = NULL
                WHERE anonymous_id = %s AND user_id IS NULL
                """,
                (user_id, anonymous_id),
            )
            return cur.rowcount or 0


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

# 模块级引用，由 main.py 在 lifespan 中注入
_connections: DatabaseConnectionManager | None = None


def configure_auth(connections: DatabaseConnectionManager, secret: str, algorithm: str) -> None:
    global _connections
    _connections = connections
    configure_principal_auth(secret, algorithm)


def get_auth_connections() -> DatabaseConnectionManager:
    if _connections is None:
        raise HTTPException(status_code=503, detail="认证服务未初始化")
    return _connections


def get_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    principal = require_user_principal(get_current_principal(authorization))
    return {"user_id": principal.subject, "username": principal.username}


def get_current_user_optional(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any] | None:
    principal = get_current_principal_optional(authorization)
    if principal is None:
        return None
    principal = require_user_principal(principal)
    return {"user_id": principal.subject, "username": principal.username}
