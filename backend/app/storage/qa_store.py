from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

try:  # pragma: no cover - exercised when psycopg is installed
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover - lets tests run without database extras
    psycopg = None
    dict_row = None
    Jsonb = None

from app.domain.models import TravelKnowledgeSource, TravelQAChatMessage, TravelQAConversationDetail, TravelQAConversationSummary
from app.storage.db import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class QAConversationNotFound(LookupError):
    """Raised when a conversation is absent or owned by another principal."""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS travel_qa_conversations (
    id uuid PRIMARY KEY,
    user_id text,
    anonymous_id text,
    title text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS travel_qa_messages (
    id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES travel_qa_conversations(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL,
    sources_payload jsonb NOT NULL DEFAULT '[]'::jsonb,
    retrieved_count integer NOT NULL DEFAULT 0,
    generation_mode text,
    used_web_search boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_travel_qa_conversations_user_updated
    ON travel_qa_conversations (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_travel_qa_conversations_anonymous_updated
    ON travel_qa_conversations (anonymous_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_travel_qa_messages_conversation_created
    ON travel_qa_messages (conversation_id, created_at);
"""


class PostgresQAConversationStore:
    def __init__(self, database_url: str, connection_manager: DatabaseConnectionManager | None = None):
        if (psycopg is None or dict_row is None or Jsonb is None) and connection_manager is None:
            raise RuntimeError("PostgreSQL QA storage requires psycopg. Run: pip install -r backend/requirements.txt")
        self.database_url = database_url
        self.connections = connection_manager or DatabaseConnectionManager(database_url)
        self._schema_ready = False

    def ensure_schema(self) -> None:
        with self.connections.connection() as conn:
            conn.execute(SCHEMA_SQL)
            conn.execute(
                "ALTER TABLE travel_qa_messages "
                "ADD COLUMN IF NOT EXISTS used_web_search boolean NOT NULL DEFAULT false"
            )
        self._schema_ready = True

    def _ensure_schema_once(self) -> None:
        if not self._schema_ready:
            self.ensure_schema()

    def health(self) -> dict[str, Any]:
        try:
            with self.connections.connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    row = cur.execute("SELECT 1 AS ok").fetchone()
            return {"enabled": True, "ok": bool(row and row["ok"] == 1)}
        except Exception as exc:
            logger.warning("PostgreSQL QA storage health check failed: %s", exc)
            return {"enabled": True, "ok": False, "error": str(exc)}

    def get_or_create_conversation(
        self,
        conversation_id: str | None = None,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema_once()
        if conversation_id:
            existing = self._get_conversation_row(conversation_id, user_id=user_id, anonymous_id=anonymous_id)
            if existing is not None:
                return existing
            if self._get_conversation_row(conversation_id) is not None:
                raise QAConversationNotFound(conversation_id)

        new_id = str(uuid4())
        conversation_title = _conversation_title(title or "新的旅行问答")
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    INSERT INTO travel_qa_conversations (id, user_id, anonymous_id, title)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id::text, user_id, anonymous_id, title, created_at, updated_at
                    """,
                    (new_id, user_id or None, anonymous_id or None, conversation_title),
                ).fetchone()
        return dict(row)

    def _get_conversation_row(
        self,
        conversation_id: str,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
    ) -> dict[str, Any] | None:
        owner_clause, owner_params = _owner_predicate(user_id, anonymous_id)
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    f"""
                    SELECT id::text, user_id, anonymous_id, title, created_at, updated_at
                    FROM travel_qa_conversations
                    WHERE id = %s {owner_clause}
                    """,
                    (conversation_id, *owner_params),
                ).fetchone()
        return dict(row) if row else None

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        sources: list[TravelKnowledgeSource] | None = None,
        retrieved_count: int = 0,
        generation_mode: str | None = None,
        used_web_search: bool = False,
    ) -> dict[str, Any]:
        self._ensure_schema_once()
        if user_id or anonymous_id:
            if self._get_conversation_row(conversation_id, user_id=user_id, anonymous_id=anonymous_id) is None:
                raise QAConversationNotFound(conversation_id)
        message_id = str(uuid4())
        source_payload = [source.model_dump(mode="json") for source in sources or []]
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    INSERT INTO travel_qa_messages (
                        id, conversation_id, role, content, sources_payload, retrieved_count, generation_mode,
                        used_web_search
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id::text, conversation_id::text, role, content, sources_payload,
                              retrieved_count, generation_mode, used_web_search, created_at
                    """,
                    (
                        message_id,
                        conversation_id,
                        role,
                        content,
                        Jsonb(source_payload),
                        retrieved_count,
                        generation_mode,
                        used_web_search,
                    ),
                ).fetchone()
                cur.execute(
                    "UPDATE travel_qa_conversations SET updated_at = now() WHERE id = %s",
                    (conversation_id,),
                )
        return dict(row)

    def get_recent_messages(
        self,
        conversation_id: str,
        limit: int = 8,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
    ) -> list[dict[str, str]]:
        self._ensure_schema_once()
        if user_id or anonymous_id:
            if self._get_conversation_row(conversation_id, user_id=user_id, anonymous_id=anonymous_id) is None:
                raise QAConversationNotFound(conversation_id)
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    SELECT role, content
                    FROM travel_qa_messages
                    WHERE conversation_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (conversation_id, limit),
                ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def list_conversations(
        self,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        limit: int = 50,
    ) -> list[TravelQAConversationSummary]:
        self._ensure_schema_once()
        where_clause = ""
        params: list[Any] = []
        if user_id:
            where_clause = "WHERE user_id = %s"
            params.append(user_id)
        elif anonymous_id:
            where_clause = "WHERE anonymous_id = %s"
            params.append(anonymous_id)
        params.append(limit)
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    f"""
                    SELECT id::text, user_id, anonymous_id, title, created_at, updated_at
                    FROM travel_qa_conversations
                    {where_clause}
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    params,
                ).fetchall()
        return [TravelQAConversationSummary.model_validate(dict(row)) for row in rows]

    def get_conversation(
        self,
        conversation_id: str,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
    ) -> TravelQAConversationDetail | None:
        self._ensure_schema_once()
        conversation = self._get_conversation_row(conversation_id, user_id=user_id, anonymous_id=anonymous_id)
        if (user_id or anonymous_id) and conversation is None:
            raise QAConversationNotFound(conversation_id)
        if conversation is None:
            return None
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    SELECT id::text, conversation_id::text, role, content, sources_payload,
                           retrieved_count, generation_mode, used_web_search, created_at
                    FROM travel_qa_messages
                    WHERE conversation_id = %s
                    ORDER BY created_at ASC
                    """,
                    (conversation_id,),
                ).fetchall()
        messages = [_message_from_row(dict(row)) for row in rows]
        return TravelQAConversationDetail.model_validate({**conversation, "messages": messages})

    def close(self) -> None:
        self.connections.close()

    def merge_anonymous(self, anonymous_id: str, user_id: str) -> tuple[int, int]:
        self._ensure_schema_once()
        with self.connections.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*) FROM travel_qa_messages m
                    JOIN travel_qa_conversations c ON c.id = m.conversation_id
                    WHERE c.anonymous_id = %s AND c.user_id IS NULL
                    """,
                    (anonymous_id,),
                )
                merged_messages = int(cur.fetchone()[0] or 0)
                cur.execute(
                    """
                    UPDATE travel_qa_conversations
                    SET user_id = %s, anonymous_id = NULL
                    WHERE anonymous_id = %s AND user_id IS NULL
                    """,
                    (user_id, anonymous_id),
                )
                merged_conversations = cur.rowcount or 0
        return merged_conversations, merged_messages


class InMemoryQAConversationStore:
    def __init__(self):
        self.conversations: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}

    def health(self) -> dict[str, Any]:
        return {"enabled": True, "ok": True, "memory_only": True}

    def get_or_create_conversation(self, conversation_id=None, user_id=None, anonymous_id=None, title=None):
        if conversation_id and conversation_id in self.conversations:
            row = self.conversations[conversation_id]
            if (user_id or anonymous_id) and not _owner_matches(row, user_id, anonymous_id):
                raise QAConversationNotFound(conversation_id)
            return row
        now = datetime.now(timezone.utc)
        conversation_id = conversation_id or str(uuid4())
        row = {
            "id": conversation_id,
            "user_id": user_id,
            "anonymous_id": anonymous_id,
            "title": _conversation_title(title or "新的旅行问答"),
            "created_at": now,
            "updated_at": now,
        }
        self.conversations[conversation_id] = row
        self.messages.setdefault(conversation_id, [])
        return row

    def append_message(self, conversation_id, role, content, **kwargs):
        user_id = kwargs.pop("user_id", None)
        anonymous_id = kwargs.pop("anonymous_id", None)
        if (user_id or anonymous_id) and (
            conversation_id not in self.conversations
            or not _owner_matches(self.conversations[conversation_id], user_id, anonymous_id)
        ):
            raise QAConversationNotFound(conversation_id)
        now = datetime.now(timezone.utc)
        row = {
            "id": str(uuid4()),
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "sources_payload": [source.model_dump(mode="json") for source in kwargs.get("sources") or []],
            "retrieved_count": kwargs.get("retrieved_count", 0),
            "generation_mode": kwargs.get("generation_mode"),
            "used_web_search": bool(kwargs.get("used_web_search", False)),
            "created_at": now,
        }
        self.messages.setdefault(conversation_id, []).append(row)
        if conversation_id in self.conversations:
            self.conversations[conversation_id]["updated_at"] = now
        return row

    def get_recent_messages(self, conversation_id, limit=8, *, user_id=None, anonymous_id=None):
        if (user_id or anonymous_id) and (
            conversation_id not in self.conversations
            or not _owner_matches(self.conversations[conversation_id], user_id, anonymous_id)
        ):
            raise QAConversationNotFound(conversation_id)
        rows = self.messages.get(conversation_id, [])[-limit:]
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def list_conversations(self, user_id=None, anonymous_id=None, limit=50):
        rows = list(self.conversations.values())
        if user_id:
            rows = [row for row in rows if row.get("user_id") == user_id]
        elif anonymous_id:
            rows = [row for row in rows if row.get("anonymous_id") == anonymous_id]
        rows = sorted(rows, key=lambda row: row["updated_at"], reverse=True)[:limit]
        return [TravelQAConversationSummary.model_validate(row) for row in rows]

    def get_conversation(self, conversation_id, *, user_id=None, anonymous_id=None):
        row = self.conversations.get(conversation_id)
        if row is None or ((user_id or anonymous_id) and not _owner_matches(row, user_id, anonymous_id)):
            if user_id or anonymous_id:
                raise QAConversationNotFound(conversation_id)
            return None
        messages = [_message_from_row(item) for item in self.messages.get(conversation_id, [])]
        return TravelQAConversationDetail.model_validate({**row, "messages": messages})

    def close(self):
        return None

    def merge_anonymous(self, anonymous_id: str, user_id: str) -> tuple[int, int]:
        conversations = [
            row for row in self.conversations.values()
            if row.get("anonymous_id") == anonymous_id and not row.get("user_id")
        ]
        message_count = 0
        for row in conversations:
            row["user_id"] = user_id
            row["anonymous_id"] = None
            message_count += len(self.messages.get(row["id"], []))
        return len(conversations), message_count


def _owner_predicate(user_id: str | None, anonymous_id: str | None) -> tuple[str, tuple[str, ...]]:
    if user_id:
        return "AND user_id = %s", (user_id,)
    if anonymous_id:
        return "AND anonymous_id = %s", (anonymous_id,)
    return "", ()


def _owner_matches(row: dict[str, Any], user_id: str | None, anonymous_id: str | None) -> bool:
    return bool(
        (user_id and row.get("user_id") == user_id and not row.get("anonymous_id"))
        or (anonymous_id and row.get("anonymous_id") == anonymous_id and not row.get("user_id"))
    )


def create_qa_conversation_store(database_url: str | None) -> PostgresQAConversationStore | None:
    if not database_url:
        return None
    return PostgresQAConversationStore(database_url)


def _message_from_row(row: dict[str, Any]) -> TravelQAChatMessage:
    return TravelQAChatMessage.model_validate(
        {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
            "sources": row.get("sources_payload") or [],
            "retrieved_count": row.get("retrieved_count") or 0,
            "generation_mode": row.get("generation_mode"),
            "used_web_search": bool(row.get("used_web_search", False)),
            "created_at": row["created_at"],
        }
    )


def _conversation_title(value: str) -> str:
    text = " ".join(str(value or "").strip().split())
    return (text[:40] or "新的旅行问答")
