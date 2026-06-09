from __future__ import annotations

import hashlib
import html
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Any, Iterable
from uuid import uuid4

from app.core.config import get_settings
from app.storage.db import DatabaseConnectionManager

try:  # pragma: no cover - optional until LangChain text splitters are installed
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except Exception:  # pragma: no cover
    RecursiveCharacterTextSplitter = None

try:  # pragma: no cover - optional until PostgreSQL extras are installed
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover
    psycopg = None
    dict_row = None
    Jsonb = None

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSIONS = get_settings().embedding_dimensions
DEFAULT_TEXT_SEPARATORS = ["\n\n", "\n", ".", "!", "?", "。", "！", "？", "，", "、", " ", ""]


SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS travel_knowledge_documents (
    id uuid PRIMARY KEY,
    content_hash text NOT NULL UNIQUE,
    source_url text,
    source_name text NOT NULL DEFAULT 'rss',
    title text NOT NULL DEFAULT '',
    content text NOT NULL,
    summary text NOT NULL DEFAULT '',
    published_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_travel_knowledge_source_url ON travel_knowledge_documents (source_url);
CREATE INDEX IF NOT EXISTS idx_travel_knowledge_created_at ON travel_knowledge_documents (created_at DESC);
"""


@dataclass(frozen=True)
class KnowledgeDocument:
    id: str
    title: str
    content: str
    summary: str
    source_url: str | None
    source_name: str
    published_at: datetime | None
    score: float = 0.0


class HashingEmbeddingService:
    """Small deterministic embedding model for local-first retrieval.

    The project may run without a hosted embedding endpoint. This keeps the
    PostgreSQL vector store usable in tests and offline demos while preserving
    the same vector-search contract.
    """

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS):
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = self._tokens(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [round(value / norm, 8) for value in vector]

    def _tokens(self, text: str) -> list[str]:
        text = normalize_text(text).lower()
        words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text)
        grams: list[str] = []
        grams.extend(words)
        grams.extend("".join(words[index : index + 2]) for index in range(max(0, len(words) - 1)))
        grams.extend("".join(words[index : index + 3]) for index in range(max(0, len(words) - 2)))
        return grams


class DashScopeMultimodalEmbeddingService:
    """DashScope/Bailian embedding adapter for the configured Tongyi embedding model."""

    def __init__(
        self,
        model: str,
        api_key: str,
        dimensions: int = EMBEDDING_DIMENSIONS,
    ):
        self.model = model
        self.api_key = api_key
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        return self._embed_text(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_text(text) for text in texts]

    def _embed_text(self, text: str) -> list[float]:
        import dashscope

        text = normalize_text(text)
        if not text:
            return [0.0] * self.dimensions

        response = dashscope.MultiModalEmbedding.call(
            model=self.model,
            input=[{"text": text}],
            api_key=self.api_key,
            dimension=self.dimensions,
        )
        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"DashScope embedding failed: status={response.status_code}, code={response.code}, message={response.message}"
            )
        embeddings = response.output.get("embeddings") if response.output else None
        if not embeddings:
            raise RuntimeError("DashScope embedding response did not include embeddings.")
        vector = [float(value) for value in embeddings[0]["embedding"]]
        if len(vector) != self.dimensions:
            raise RuntimeError(f"Embedding dimension mismatch: expected {self.dimensions}, got {len(vector)}")
        return vector


def create_embedding_service():
    settings = get_settings()
    if settings.embedding_provider.lower() == "dashscope" and settings.embedding_api_key:
        return DashScopeMultimodalEmbeddingService(
            model=settings.embedding_model_id,
            api_key=settings.embedding_api_key,
            dimensions=settings.embedding_dimensions,
        )
    return HashingEmbeddingService(settings.embedding_dimensions)


class PostgresTravelVectorStore:
    def __init__(
        self,
        database_url: str,
        embeddings: Any | None = None,
        connection_manager: DatabaseConnectionManager | None = None,
    ):
        if (psycopg is None or dict_row is None or Jsonb is None) and connection_manager is None:
            raise RuntimeError("Travel vector store requires psycopg. Run: pip install -r backend/requirements.txt")
        self.database_url = database_url
        self.connections = connection_manager or DatabaseConnectionManager(database_url)
        self.embeddings = embeddings or create_embedding_service()
        self.embedding_dimensions = int(getattr(self.embeddings, "dimensions", EMBEDDING_DIMENSIONS))
        self._schema_ready = False

    def ensure_schema(self) -> None:
        with self.connections.connection() as conn:
            conn.execute(SCHEMA_SQL)
            self._ensure_embedding_column_dimension(conn)
        self._schema_ready = True

    def _ensure_embedding_column_dimension(self, conn) -> None:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                """
                SELECT atttypmod AS typmod
                FROM pg_attribute
                WHERE attrelid = 'travel_knowledge_documents'::regclass
                  AND attname = 'embedding'
                  AND NOT attisdropped
                """
            ).fetchone()
            current_dimensions = int(row["typmod"]) if row and row["typmod"] and row["typmod"] > 0 else None
            if current_dimensions == self.embedding_dimensions:
                return
            logger.warning(
                "Travel knowledge embedding dimension changed from %s to %s; clearing old embeddings.",
                current_dimensions,
                self.embedding_dimensions,
            )
            cur.execute("TRUNCATE TABLE travel_knowledge_documents")
            cur.execute(
                f"ALTER TABLE travel_knowledge_documents ALTER COLUMN embedding TYPE vector({self.embedding_dimensions})"
            )

    def _ensure_schema_once(self) -> None:
        if not self._schema_ready:
            self.ensure_schema()

    def health(self) -> dict[str, Any]:
        try:
            with self.connections.connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    row = cur.execute(
                        """
                        SELECT
                            EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector') AS pgvector_enabled,
                            to_regclass('travel_knowledge_documents') IS NOT NULL AS table_ready
                        """
                    ).fetchone()
                    result = {"enabled": True, "ok": True, **dict(row or {})}
                    if result.get("table_ready"):
                        stats = cur.execute(
                            """
                            SELECT
                                count(*) AS document_count,
                                max(created_at) AS latest_document_created_at,
                                max(published_at) AS latest_published_at
                            FROM travel_knowledge_documents
                            """
                        ).fetchone()
                        result.update(dict(stats or {}))
            return result
        except Exception as exc:
            logger.warning("Travel vector store health check failed: %s", exc)
            return {"enabled": True, "ok": False, "error": str(exc)}

    def add_text(
        self,
        content: str,
        source_url: str | None = None,
        title: str = "",
        source_name: str = "rss",
        published_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self._ensure_schema_once()
        chunks = split_text(normalize_text(content))
        if not chunks:
            return 0

        added = 0
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                for index, chunk in enumerate(chunks):
                    content_hash = stable_hash(source_url or title, chunk)
                    vector_literal = vector_to_sql_literal(self.embeddings.embed_query(chunk))
                    row = cur.execute(
                        """
                        INSERT INTO travel_knowledge_documents (
                            id, content_hash, source_url, source_name, title, content,
                            summary, published_at, metadata, embedding
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                        ON CONFLICT (content_hash) DO NOTHING
                        RETURNING id
                        """,
                        (
                            str(uuid4()),
                            content_hash,
                            source_url,
                            source_name,
                            title[:240],
                            chunk,
                            summarize_text(chunk),
                            published_at,
                            Jsonb({"chunk_index": index, **(metadata or {})}),
                            vector_literal,
                        ),
                    ).fetchone()
                    if row is not None:
                        added += 1
        return added

    def similarity_search(self, query: str, k: int = 5, source_name: str | None = None) -> list[KnowledgeDocument]:
        self._ensure_schema_once()
        vector_literal = vector_to_sql_literal(self.embeddings.embed_query(query))
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if source_name:
                    rows = cur.execute(
                        """
                        SELECT
                            id::text, title, content, summary, source_url, source_name,
                            published_at, 1 - (embedding <=> %s::vector) AS score
                        FROM travel_knowledge_documents
                        WHERE source_name = %s
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                        """,
                        (vector_literal, source_name, vector_literal, max(1, min(k, 12))),
                    ).fetchall()
                else:
                    rows = cur.execute(
                        """
                        SELECT
                            id::text, title, content, summary, source_url, source_name,
                            published_at, 1 - (embedding <=> %s::vector) AS score
                        FROM travel_knowledge_documents
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                        """,
                        (vector_literal, vector_literal, max(1, min(k, 12))),
                    ).fetchall()
        return [KnowledgeDocument(**dict(row)) for row in rows]

    def close(self) -> None:
        self.connections.close()


def create_travel_vector_store(database_url: str | None) -> PostgresTravelVectorStore | None:
    if not database_url:
        return None
    return PostgresTravelVectorStore(database_url)


def normalize_text(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def split_text(text: str, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    if not text:
        return []
    if RecursiveCharacterTextSplitter is not None:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            separators=DEFAULT_TEXT_SEPARATORS,
            length_function=len,
        )
        return [chunk for chunk in splitter.split_text(text) if chunk.strip()]
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(0, end - overlap)
    return [chunk for chunk in chunks if chunk]


def summarize_text(text: str, limit: int = 180) -> str:
    text = normalize_text(text)
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


def stable_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def vector_to_sql_literal(values: Iterable[float]) -> str:
    clean = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            number = 0.0
        clean.append(f"{number:.8f}")
    return f"[{','.join(clean)}]"

