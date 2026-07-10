from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Literal

from fastapi import Depends, Header, HTTPException, status

from app.core.api_errors import api_error

try:
    from jose import JWTError, jwt
except ImportError:  # pragma: no cover
    JWTError = None  # type: ignore[assignment,misc]
    jwt = None  # type: ignore[assignment]


PrincipalType = Literal["anonymous", "user"]


@dataclass(frozen=True)
class Principal:
    subject: str
    principal_type: PrincipalType
    username: str = ""

    @property
    def user_id(self) -> str | None:
        return self.subject if self.principal_type == "user" else None

    @property
    def anonymous_id(self) -> str | None:
        return self.subject if self.principal_type == "anonymous" else None


def create_principal_token(
    subject: str,
    principal_type: PrincipalType,
    username: str,
    secret: str,
    algorithm: str,
    expire_minutes: int,
) -> str:
    if jwt is None:
        raise RuntimeError("python-jose is required. Run: pip install python-jose[cryptography]")
    if principal_type not in ("anonymous", "user"):
        raise ValueError("unsupported principal type")
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": subject,
            "principal_type": principal_type,
            "preferred_username": username,
            "iat": now,
            "exp": now + timedelta(minutes=expire_minutes),
        },
        secret,
        algorithm=algorithm,
    )


def decode_principal_token(token: str, secret: str, algorithm: str) -> Principal:
    if jwt is None or JWTError is None:
        raise RuntimeError("python-jose is required. Run: pip install python-jose[cryptography]")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            options={"require_exp": True, "require_iat": True, "require_sub": True},
        )
    except JWTError as exc:
        raise _invalid_token() from exc

    subject = payload.get("sub")
    username = payload.get("preferred_username")
    principal_type = payload.get("principal_type")
    if "principal_type" not in payload and username:
        principal_type = "user"
    # Migration boundary: legacy user subjects may be non-UUID values, but they
    # must remain non-empty strings until all historical tokens have expired.
    if (
        not isinstance(subject, str)
        or not subject
        or principal_type not in ("anonymous", "user")
    ):
        raise _invalid_token()
    return Principal(
        subject=subject,
        principal_type=principal_type,
        username=str(username or ""),
    )


_secret = "change-me-in-production"
_algorithm = "HS256"
_bearer_pattern = re.compile(r"^Bearer[ \t]+([^ \t]+)$", re.IGNORECASE)


def configure_principal_auth(secret: str, algorithm: str) -> None:
    global _secret, _algorithm
    _secret = secret
    _algorithm = algorithm


def get_current_principal(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Principal:
    token = _bearer_token(authorization)
    return decode_principal_token(token, _secret, _algorithm)


def get_current_principal_optional(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Principal | None:
    if authorization is None:
        return None
    token = _bearer_token(authorization)
    return decode_principal_token(token, _secret, _algorithm)


def require_user_principal(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.principal_type != "user":
        raise api_error(
            status.HTTP_403_FORBIDDEN,
            "AUTH_USER_REQUIRED",
            "该操作需要登录用户身份",
        )
    return principal


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_REQUIRED", "需要 Bearer 认证令牌")
    match = _bearer_pattern.fullmatch(authorization)
    if match is None:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_HEADER", "无效的认证头")
    return match.group(1)


def _invalid_token() -> HTTPException:
    return api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_TOKEN_INVALID", "认证令牌已过期或无效")
