from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised when langgraph is installed
    from langgraph.checkpoint.memory import InMemorySaver
except Exception:  # pragma: no cover - optional dependency fallback
    InMemorySaver = None

try:  # pragma: no cover - exercised when postgres checkpointer is installed
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except Exception:  # pragma: no cover - optional dependency fallback
    PostgresSaver = None
    ConnectionPool = None
    dict_row = None


def create_qa_checkpointer(database_url: str | None) -> Any | None:
    if database_url and PostgresSaver is not None and ConnectionPool is not None and dict_row is not None:
        try:
            pool = ConnectionPool(
                conninfo=database_url,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                min_size=1,
                max_size=4,
            )
            checkpointer = PostgresSaver(pool)
            checkpointer.setup()
            return checkpointer
        except Exception as exc:
            logger.warning("PostgreSQL LangGraph checkpointer unavailable, using memory checkpointer: %s", exc)

    if InMemorySaver is not None:
        return InMemorySaver()
    return None
