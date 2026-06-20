from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Header, HTTPException, status

from app.storage.db import DatabaseConnectionManager

logger = logging.getLogger(__name__)

try:
    from passlib.context import CryptContext
except ImportError:  # pragma: no cover
    CryptContext = None  # type: ignore[assignment,misc]

try:
    from jose import JWTError, jwt
except ImportError:  # pragma: no cover
    JWTError = None  # type: ignore[assignment,misc]
    jwt = None  # type: ignore[assignment]


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
    if jwt is None:
        raise RuntimeError("python-jose is required. Run: pip install python-jose[cryptography]")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "preferred_username": username,
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_access_token(token: str, secret: str, algorithm: str) -> dict[str, Any]:
    if jwt is None or JWTError is None:
        raise RuntimeError("python-jose is required. Run: pip install python-jose[cryptography]")
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
        user_id = payload.get("sub")
        username = payload.get("preferred_username")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的认证凭据")
        return {"user_id": str(user_id), "username": str(username or "")}
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证凭据已过期或无效") from exc


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
_secret: str = "change-me-in-production"
_algorithm: str = "HS256"


def configure_auth(connections: DatabaseConnectionManager, secret: str, algorithm: str) -> None:
    global _connections, _secret, _algorithm
    _connections = connections
    _secret = secret
    _algorithm = algorithm


def get_auth_connections() -> DatabaseConnectionManager:
    if _connections is None:
        raise HTTPException(status_code=503, detail="认证服务未初始化")
    return _connections


def get_current_user(authorization: str = Header(..., alias="Authorization")) -> dict[str, Any]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的认证头")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证凭据为空")
    return decode_access_token(token, _secret, _algorithm)


def get_current_user_optional(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any] | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    try:
        return decode_access_token(token, _secret, _algorithm)
    except HTTPException:
        return None
