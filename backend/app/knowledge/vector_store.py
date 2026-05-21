from __future__ import annotations

import hashlib
import html
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from uuid import uuid4

try:  # pragma: no cover - optional until PostgreSQL extras are installed
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover
    psycopg = None
    dict_row = None
    Jsonb = None

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSIONS = 384


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


class PostgresTravelVectorStore:
    def __init__(self, database_url: str, embeddings: HashingEmbeddingService | None = None):
        if psycopg is None or dict_row is None or Jsonb is None:
            raise RuntimeError("Travel vector store requires psycopg. Run: pip install -r backend/requirements.txt")
        self.database_url = database_url
        self.embeddings = embeddings or HashingEmbeddingService()
        self._schema_ready = False

    def ensure_schema(self) -> None:
        with psycopg.connect(self.database_url) as conn:
            conn.execute(SCHEMA_SQL)
        self._schema_ready = True

    def _ensure_schema_once(self) -> None:
        if not self._schema_ready:
            self.ensure_schema()

    def health(self) -> dict[str, Any]:
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    row = cur.execute(
                        """
                        SELECT
                            EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector') AS pgvector_enabled,
                            to_regclass('travel_knowledge_documents') IS NOT NULL AS table_ready
                        """
                    ).fetchone()
            return {"enabled": True, "ok": True, **dict(row or {})}
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
        with psycopg.connect(self.database_url) as conn:
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

    def similarity_search(self, query: str, k: int = 5) -> list[KnowledgeDocument]:
        self._ensure_schema_once()
        vector_literal = vector_to_sql_literal(self.embeddings.embed_query(query))
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
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

