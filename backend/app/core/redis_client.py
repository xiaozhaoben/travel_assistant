from __future__ import annotations

import logging
from typing import Any

from redis import Redis


logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self, client: Any | None, *, enabled: bool):
        self.client = client
        self.enabled = enabled

    def health(self) -> dict[str, bool]:
        if not self.enabled:
            return {"enabled": False, "ok": False}
        if self.client is None:
            return {"enabled": True, "ok": False}
        try:
            ok = bool(self.client.ping())
        except Exception as exc:
            logger.warning("Redis health check failed exception_type=%s", type(exc).__name__)
            ok = False
        return {"enabled": True, "ok": ok}

    def close(self) -> None:
        if self.client is None:
            return
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


def create_redis_client(settings) -> RedisClient:
    enabled = bool(settings.redis_url or settings.redis_host)
    if not enabled:
        return RedisClient(None, enabled=False)

    options = {
        "decode_responses": True,
        "socket_connect_timeout": settings.redis_connect_timeout_seconds,
        "socket_timeout": settings.redis_read_timeout_seconds,
        "health_check_interval": 30,
        "max_connections": settings.redis_max_connections,
    }
    if settings.redis_url:
        client = Redis.from_url(settings.redis_url, **options)
    elif settings.redis_host:
        client = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            db=settings.redis_db,
            **options,
        )
    else:
        client = None
    return RedisClient(client, enabled=True)
