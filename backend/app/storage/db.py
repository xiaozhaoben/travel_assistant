from __future__ import annotations

from typing import Any, Callable


class DatabaseConnectionManager:
    def __init__(
        self,
        database_url: str,
        *,
        pool_factory: Callable[..., Any] | None = None,
        connect_factory: Callable[[str], Any] | None = None,
        min_size: int = 1,
        max_size: int = 5,
    ):
        self.database_url = database_url
        self.connect_factory = connect_factory
        self._pool = None

        if pool_factory is None:
            try:
                from psycopg_pool import ConnectionPool

                pool_factory = ConnectionPool
            except Exception:
                pool_factory = None

        if pool_factory is not None:
            self._pool = pool_factory(
                conninfo=database_url,
                min_size=min_size,
                max_size=max_size,
                open=True,
            )

    def connection(self):
        if self._pool is not None:
            return self._pool.connection()
        if self.connect_factory is not None:
            return self.connect_factory(self.database_url)

        import psycopg

        return psycopg.connect(self.database_url)

    def close(self) -> None:
        close = getattr(self._pool, "close", None)
        if callable(close):
            close()
