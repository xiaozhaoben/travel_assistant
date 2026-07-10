from __future__ import annotations

import logging
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
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

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


def ensure_users_table(connections: DatabaseConnectionManager) -> None:
    try:
        with connections.connection() as conn:
            conn.execute(USERS_TABLE_SQL)
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
                "SELECT id::text, username, password_hash, created_at FROM users WHERE username = %s",
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
                "SELECT id::text, username, password_hash, created_at FROM users WHERE id = %s",
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
                RETURNING id::text, username, created_at
                """,
                (username, password_hash),
            ).fetchone()
    return dict(row)


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
