from __future__ import annotations

import hashlib
import html
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http import HTTPStatus
from typing import Any, Iterable

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
DEFAULT_TEXT_SEPARATORS = [
    "\n\n",
    "\n",
    "\u3002",
    "\uff01",
    "\uff1f",
    "\uff1b",
    "\uff0c",
    ".",
    "!",
    "?",
    ";",
    ",",
    " ",
    "",
]
DEFAULT_CHUNK_SIZE = 700
DEFAULT_CHUNK_OVERLAP = 100
CHUNK_STRATEGY = "recursive_markdown_v1"


SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id bigserial PRIMARY KEY,
    doc_id text NOT NULL UNIQUE,
    title text NOT NULL DEFAULT '',
    source_name text NOT NULL DEFAULT '',
    source_url text,
    source_type text NOT NULL DEFAULT 'manual',
    publish_date date,
    crawl_date timestamptz NOT NULL DEFAULT now(),
    province text,
    city text,
    data_type text,
    content_hash text NOT NULL DEFAULT '',
    version_id integer NOT NULL DEFAULT 1,
    is_deleted boolean NOT NULL DEFAULT false,
    deleted_at timestamptz,
    raw_content text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id bigserial PRIMARY KEY,
    doc_id text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    chunk_id text NOT NULL UNIQUE,
    title text NOT NULL DEFAULT '',
    section text NOT NULL DEFAULT '',
    content text NOT NULL,
    embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL,
    province text,
    city text,
    scenic_spot text,
    data_type text,
    source_name text NOT NULL DEFAULT '',
    source_url text,
    publish_date date,
    content_hash text NOT NULL DEFAULT '',
    version_id integer NOT NULL DEFAULT 1,
    chunk_index integer NOT NULL DEFAULT 0,
    chunk_count integer NOT NULL DEFAULT 0,
    chunk_strategy text NOT NULL DEFAULT '{CHUNK_STRATEGY}',
    chunk_size integer,
    chunk_overlap integer,
    embedding_model text NOT NULL DEFAULT '',
    embedding_dimension integer NOT NULL DEFAULT {EMBEDDING_DIMENSIONS},
    is_deleted boolean NOT NULL DEFAULT false,
    deleted_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_doc_id ON documents (doc_id);
CREATE INDEX IF NOT EXISTS idx_documents_source_url ON documents (source_url);
CREATE INDEX IF NOT EXISTS idx_documents_city_type ON documents (province, city, data_type);
CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_id ON document_chunks (doc_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_version ON document_chunks (doc_id, version_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_city_type ON document_chunks (province, city, data_type);
CREATE INDEX IF NOT EXISTS idx_document_chunks_publish_date ON document_chunks (publish_date DESC);
CREATE INDEX IF NOT EXISTS idx_document_chunks_active ON document_chunks (doc_id) WHERE is_deleted = false;
"""


METADATA_SCHEMA_MIGRATION_SQL = f"""
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash text NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS version_id integer NOT NULL DEFAULT 1;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_deleted boolean NOT NULL DEFAULT false;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS content_hash text NOT NULL DEFAULT '';
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS version_id integer NOT NULL DEFAULT 1;
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chunk_index integer NOT NULL DEFAULT 0;
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chunk_count integer NOT NULL DEFAULT 0;
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chunk_strategy text NOT NULL DEFAULT '{CHUNK_STRATEGY}';
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chunk_size integer;
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chunk_overlap integer;
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_model text NOT NULL DEFAULT '';
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_dimension integer NOT NULL DEFAULT {EMBEDDING_DIMENSIONS};
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS is_deleted boolean NOT NULL DEFAULT false;
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_version ON document_chunks (doc_id, version_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_active ON document_chunks (doc_id) WHERE is_deleted = false;
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


@dataclass(frozen=True)
class TravelDocumentChunk:
    chunk_id: str
    title: str
    section: str
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TravelChunkSearchResult:
    chunk_id: str
    title: str
    section: str
    content: str
    source_name: str
    source_url: str | None
    publish_date: date | datetime | None
    score: float
    metadata: dict[str, Any] | None = None


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

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

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
        batch_size: int = 16,
    ):
        self.model = model
        self.api_key = api_key
        self.dimensions = dimensions
        self.batch_size = max(1, int(batch_size))

    def embed_query(self, text: str) -> list[float]:
        return self._embed_text(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        import dashscope

        normalized = [normalize_text(text) for text in texts]
        vectors: list[list[float] | None] = []
        batch_input = []
        batch_positions = []
        for index, text in enumerate(normalized):
            if text:
                batch_positions.append(index)
                batch_input.append({"text": text})
                vectors.append(None)
            else:
                vectors.append([0.0] * self.dimensions)
        if not batch_input:
            return [list(vector or []) for vector in vectors]

        for offset in range(0, len(batch_input), self.batch_size):
            input_slice = batch_input[offset : offset + self.batch_size]
            position_slice = batch_positions[offset : offset + self.batch_size]
            response = dashscope.MultiModalEmbedding.call(
                model=self.model,
                input=input_slice,
                api_key=self.api_key,
                dimension=self.dimensions,
            )
            if response.status_code != HTTPStatus.OK:
                raise RuntimeError(
                    f"DashScope embedding failed: status={response.status_code}, code={response.code}, message={response.message}"
                )
            embeddings = response.output.get("embeddings") if response.output else None
            if not embeddings or len(embeddings) != len(input_slice):
                raise RuntimeError("DashScope embedding response count did not match input count.")
            for position, item in zip(position_slice, embeddings):
                vector = [float(value) for value in item["embedding"]]
                if len(vector) != self.dimensions:
                    raise RuntimeError(f"Embedding dimension mismatch: expected {self.dimensions}, got {len(vector)}")
                vectors[position] = vector
        return [list(vector or []) for vector in vectors]

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
            conn.execute(METADATA_SCHEMA_MIGRATION_SQL)
            self._ensure_embedding_column_dimension(conn)
        self._schema_ready = True

    def _ensure_embedding_column_dimension(self, conn) -> None:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                """
                SELECT atttypmod AS typmod
                FROM pg_attribute
                WHERE attrelid = 'document_chunks'::regclass
                  AND attname = 'embedding'
                  AND NOT attisdropped
                """
            ).fetchone()
            current_dimensions = int(row["typmod"]) if row and row["typmod"] and row["typmod"] > 0 else None
            if current_dimensions in {self.embedding_dimensions, self.embedding_dimensions + 4}:
                return
            logger.warning(
                "Travel knowledge embedding dimension changed from %s to %s; clearing document chunks.",
                current_dimensions,
                self.embedding_dimensions,
            )
            cur.execute("TRUNCATE TABLE document_chunks")
            cur.execute(f"ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector({self.embedding_dimensions})")

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
                            to_regclass('documents') IS NOT NULL AS documents_ready,
                            to_regclass('document_chunks') IS NOT NULL AS chunks_ready
                        """
                    ).fetchone()
                    result = {"enabled": True, "ok": True, **dict(row or {})}
                    result["table_ready"] = bool(result.get("documents_ready") and result.get("chunks_ready"))
                    if result.get("table_ready"):
                        stats = cur.execute(
                            """
                            SELECT
                                (SELECT count(*) FROM documents) AS document_count,
                                (SELECT count(*) FROM document_chunks) AS chunk_count,
                                (SELECT max(created_at) FROM documents) AS latest_document_created_at,
                                (SELECT max(publish_date) FROM documents) AS latest_published_at
                            """
                        ).fetchone()
                        result.update(dict(stats or {}))
            return result
        except Exception as exc:
            logger.warning("Travel vector store health check failed: %s", exc)
            return {"enabled": True, "ok": False, "error": str(exc)}

    def ingest_document(
        self,
        *,
        title: str,
        content: str,
        source_name: str,
        source_url: str | None = None,
        source_type: str = "manual",
        publish_date: date | datetime | str | None = None,
        province: str | None = None,
        city: str | None = None,
        data_type: str | None = None,
        scenic_spot: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema_once()
        raw_content = normalize_document_content(content)
        if not raw_content:
            return {"doc_id": "", "chunks_added": 0}

        document_metadata = dict(metadata or {})
        normalized_publish_date = parse_publish_date(publish_date)
        doc_id = stable_document_id(
            explicit_id=document_metadata.get("doc_id"),
            source_type=source_type,
            source_url=source_url,
            source_name=source_name,
            title=title,
        )
        content_hash = prefixed_content_hash(raw_content)
        embedding_model = self.embedding_model_name()
        chunks = split_markdown_document(
            title=title,
            content=raw_content,
            metadata={
                **document_metadata,
                "content_hash": content_hash,
                "province": province,
                "city": city,
                "data_type": data_type,
                "source_name": source_name,
                "source_url": source_url,
                "publish_date": normalized_publish_date.isoformat() if normalized_publish_date else None,
            },
        )
        if not chunks:
            return {"doc_id": doc_id, "chunks_added": 0}

        # Generate chunk vectors before opening the write transaction. This keeps
        # long embedding calls out of the database transaction and allows true
        # batching when the provider supports it.
        chunk_vectors = self.embed_texts([chunk.content for chunk in chunks])
        added = 0
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                previous = cur.execute(
                    """
                    SELECT version_id, content_hash, is_deleted
                    FROM documents
                    WHERE doc_id = %s
                    FOR UPDATE
                    """,
                    (doc_id,),
                ).fetchone()
                previous_version = int((previous or {}).get("version_id") or 0)
                previous_hash = str((previous or {}).get("content_hash") or "")
                previous_deleted = bool((previous or {}).get("is_deleted") or False)
                if previous and not previous_deleted and previous_hash == content_hash:
                    return {"doc_id": doc_id, "chunks_added": 0, "version_id": previous_version}

                version_id = previous_version + 1 if previous else 1
                document_metadata = {
                    **document_metadata,
                    "doc_id": doc_id,
                    "content_hash": content_hash,
                    "version_id": version_id,
                    "chunk_count": len(chunks),
                    "chunk_strategy": CHUNK_STRATEGY,
                    "chunk_size": DEFAULT_CHUNK_SIZE,
                    "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
                    "embedding_model": embedding_model,
                    "embedding_dimension": self.embedding_dimensions,
                    "is_deleted": False,
                }
                cur.execute(
                    """
                    INSERT INTO documents (
                        doc_id, title, source_name, source_url, source_type,
                        publish_date, province, city, data_type, content_hash,
                        version_id, is_deleted, deleted_at, raw_content, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, NULL, %s, %s)
                    ON CONFLICT (doc_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        source_name = EXCLUDED.source_name,
                        source_url = EXCLUDED.source_url,
                        source_type = EXCLUDED.source_type,
                        publish_date = EXCLUDED.publish_date,
                        province = EXCLUDED.province,
                        city = EXCLUDED.city,
                        data_type = EXCLUDED.data_type,
                        content_hash = EXCLUDED.content_hash,
                        version_id = EXCLUDED.version_id,
                        is_deleted = false,
                        deleted_at = NULL,
                        raw_content = EXCLUDED.raw_content,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    RETURNING id
                    """,
                    (
                        doc_id,
                        title[:240],
                        source_name[:160],
                        source_url,
                        source_type[:80],
                        normalized_publish_date,
                        province,
                        city,
                        data_type,
                        content_hash,
                        version_id,
                        raw_content,
                        jsonb(document_metadata),
                    ),
                ).fetchone()
                # Rebuild chunks for the document while keeping older versions available for audit.
                cur.execute(
                    """
                    UPDATE document_chunks
                    SET is_deleted = true, deleted_at = now(), updated_at = now()
                    WHERE doc_id = %s AND is_deleted = false
                    """,
                    (doc_id,),
                )
                for index, chunk in enumerate(chunks):
                    chunk_content_hash = prefixed_content_hash(chunk.content)
                    chunk_id = stable_hash(doc_id, str(version_id), str(index), chunk.section, chunk_content_hash)
                    vector_literal = vector_to_sql_literal(chunk_vectors[index])
                    chunk_metadata = {
                        **chunk.metadata,
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                        "doc_id": doc_id,
                        "content_hash": chunk_content_hash,
                        "document_content_hash": content_hash,
                        "version_id": version_id,
                        "chunk_strategy": CHUNK_STRATEGY,
                        "chunk_size": DEFAULT_CHUNK_SIZE,
                        "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
                        "embedding_model": embedding_model,
                        "embedding_dimension": self.embedding_dimensions,
                        "is_deleted": False,
                    }
                    row = cur.execute(
                        """
                        INSERT INTO document_chunks (
                            doc_id, chunk_id, title, section, content, embedding,
                            province, city, scenic_spot, data_type, source_name,
                            source_url, publish_date, content_hash, version_id,
                            chunk_index, chunk_count, chunk_strategy, chunk_size,
                            chunk_overlap, embedding_model, embedding_dimension,
                            is_deleted, deleted_at, metadata
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false,
                            NULL, %s
                        )
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            title = EXCLUDED.title,
                            section = EXCLUDED.section,
                            content = EXCLUDED.content,
                            embedding = EXCLUDED.embedding,
                            province = EXCLUDED.province,
                            city = EXCLUDED.city,
                            scenic_spot = EXCLUDED.scenic_spot,
                            data_type = EXCLUDED.data_type,
                            source_name = EXCLUDED.source_name,
                            source_url = EXCLUDED.source_url,
                            publish_date = EXCLUDED.publish_date,
                            content_hash = EXCLUDED.content_hash,
                            version_id = EXCLUDED.version_id,
                            chunk_index = EXCLUDED.chunk_index,
                            chunk_count = EXCLUDED.chunk_count,
                            chunk_strategy = EXCLUDED.chunk_strategy,
                            chunk_size = EXCLUDED.chunk_size,
                            chunk_overlap = EXCLUDED.chunk_overlap,
                            embedding_model = EXCLUDED.embedding_model,
                            embedding_dimension = EXCLUDED.embedding_dimension,
                            is_deleted = false,
                            deleted_at = NULL,
                            metadata = EXCLUDED.metadata,
                            updated_at = now()
                        RETURNING id
                        """,
                        (
                            doc_id,
                            chunk_id,
                            chunk.title[:240],
                            chunk.section[:240],
                            chunk.content,
                            vector_literal,
                            province,
                            city,
                            scenic_spot,
                            data_type,
                            source_name[:160],
                            source_url,
                            normalized_publish_date,
                            chunk_content_hash,
                            version_id,
                            index,
                            len(chunks),
                            CHUNK_STRATEGY,
                            DEFAULT_CHUNK_SIZE,
                            DEFAULT_CHUNK_OVERLAP,
                            embedding_model,
                            self.embedding_dimensions,
                            jsonb(chunk_metadata),
                        ),
                    ).fetchone()
                    if row is not None:
                        added += 1
        return {"doc_id": doc_id, "chunks_added": added, "version_id": version_id}

    def add_text(
        self,
        content: str,
        source_url: str | None = None,
        title: str = "",
        source_name: str = "rss",
        published_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        result = self.ingest_document(
            title=title,
            content=content,
            source_name=source_name,
            source_url=source_url,
            source_type=str((metadata or {}).get("source_type") or source_name or "rss"),
            publish_date=published_at,
            province=(metadata or {}).get("province"),
            city=(metadata or {}).get("city"),
            data_type=(metadata or {}).get("data_type"),
            scenic_spot=(metadata or {}).get("scenic_spot"),
            metadata=metadata,
        )
        return int(result["chunks_added"])

    def embed_text(self, text: str) -> list[float]:
        try:
            vector = [float(value) for value in self.embeddings.embed_query(text)]
        except Exception as exc:
            logger.warning("Embedding generation failed: %s", exc)
            raise RuntimeError(f"Embedding generation failed: {exc}") from exc
        if len(vector) != self.embedding_dimensions:
            raise RuntimeError(f"Embedding dimension mismatch: expected {self.embedding_dimensions}, got {len(vector)}")
        return vector

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        try:
            embed_documents = getattr(self.embeddings, "embed_documents", None)
            if callable(embed_documents):
                vectors = embed_documents(texts)
            else:
                vectors = [self.embeddings.embed_query(text) for text in texts]
            clean_vectors = [[float(value) for value in vector] for vector in vectors]
        except Exception as exc:
            logger.warning("Batch embedding generation failed: %s", exc)
            raise RuntimeError(f"Batch embedding generation failed: {exc}") from exc
        if len(clean_vectors) != len(texts):
            raise RuntimeError(f"Embedding count mismatch: expected {len(texts)}, got {len(clean_vectors)}")
        for vector in clean_vectors:
            if len(vector) != self.embedding_dimensions:
                raise RuntimeError(f"Embedding dimension mismatch: expected {self.embedding_dimensions}, got {len(vector)}")
        return clean_vectors

    def embedding_model_name(self) -> str:
        model = getattr(self.embeddings, "model", None)
        if model:
            return str(model)
        return get_settings().embedding_model_id or self.embeddings.__class__.__name__

    def search_chunks(
        self,
        *,
        query: str,
        top_k: int = 5,
        province: str | None = None,
        city: str | None = None,
        data_type: str | None = None,
        publish_date_from: date | datetime | str | None = None,
        publish_date_to: date | datetime | str | None = None,
        source_type: str | None = None,
        source_name: str | None = None,
    ) -> list[TravelChunkSearchResult]:
        self._ensure_schema_once()
        vector_literal = vector_to_sql_literal(self.embed_text(query))
        date_from = parse_publish_date(publish_date_from)
        date_to = parse_publish_date(publish_date_to)
        # Keep filters as bind parameters; only the static SQL shape is interpolated.
        params: list[Any] = [
            vector_literal,
            city,
            city,
            province,
            province,
            data_type,
            data_type,
            source_name,
            source_name,
            source_type,
            source_type,
            date_from,
            date_from,
            date_to,
            date_to,
            vector_literal,
            max(1, min(int(top_k), 20)),
        ]
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    SELECT
                        c.chunk_id,
                        c.title,
                        c.section,
                        c.content,
                        c.source_name,
                        c.source_url,
                        c.publish_date,
                        c.metadata,
                        1 - (c.embedding <=> %s::vector) AS score
                    FROM document_chunks c
                    JOIN documents d ON d.doc_id = c.doc_id
                    WHERE c.is_deleted = false
                      AND d.is_deleted = false
                      AND (%s::text IS NULL OR c.city = %s::text)
                      AND (%s::text IS NULL OR c.province = %s::text)
                      AND (%s::text IS NULL OR c.data_type = %s::text)
                      AND (%s::text IS NULL OR c.source_name = %s::text)
                      AND (%s::text IS NULL OR d.source_type = %s::text)
                      AND (%s::date IS NULL OR c.publish_date >= %s::date)
                      AND (%s::date IS NULL OR c.publish_date <= %s::date)
                    ORDER BY c.embedding <=> %s::vector
                    LIMIT %s
                    """,
                    tuple(params),
                ).fetchall()
        return [TravelChunkSearchResult(**dict(row)) for row in rows]

    def keyword_search_chunks(
        self,
        *,
        query: str,
        top_k: int = 5,
        province: str | None = None,
        city: str | None = None,
        data_type: str | None = None,
        publish_date_from: date | datetime | str | None = None,
        publish_date_to: date | datetime | str | None = None,
        source_type: str | None = None,
        source_name: str | None = None,
    ) -> list[TravelChunkSearchResult]:
        self._ensure_schema_once()
        terms = lexical_query_terms(query)
        if not terms:
            return []
        date_from = parse_publish_date(publish_date_from)
        date_to = parse_publish_date(publish_date_to)
        params: list[Any] = [
            terms,
            city,
            city,
            province,
            province,
            data_type,
            data_type,
            source_name,
            source_name,
            source_type,
            source_type,
            date_from,
            date_from,
            date_to,
            date_to,
            max(1, min(int(top_k), 20)),
        ]
        with self.connections.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    WITH query_terms AS (
                        SELECT DISTINCT lower(term) AS term
                        FROM unnest(%s::text[]) AS t(term)
                        WHERE length(trim(term)) > 1
                    ),
                    filtered_chunks AS (
                        SELECT
                            c.chunk_id,
                            c.title,
                            c.section,
                            c.content,
                            c.source_name,
                            c.source_url,
                            c.publish_date,
                            c.metadata,
                            greatest(length(c.content), 1)::float AS doc_len,
                            lower(
                                coalesce(c.title, '') || ' ' ||
                                coalesce(c.section, '') || ' ' ||
                                coalesce(c.content, '') || ' ' ||
                                coalesce(c.metadata::text, '')
                            ) AS haystack
                        FROM document_chunks c
                        JOIN documents d ON d.doc_id = c.doc_id
                        WHERE c.is_deleted = false
                          AND d.is_deleted = false
                          AND (%s::text IS NULL OR c.city = %s::text)
                          AND (%s::text IS NULL OR c.province = %s::text)
                          AND (%s::text IS NULL OR c.data_type = %s::text)
                          AND (%s::text IS NULL OR c.source_name = %s::text)
                          AND (%s::text IS NULL OR d.source_type = %s::text)
                          AND (%s::date IS NULL OR c.publish_date >= %s::date)
                          AND (%s::date IS NULL OR c.publish_date <= %s::date)
                    ),
                    corpus AS (
                        SELECT
                            greatest(count(*), 1)::float AS total_docs,
                            greatest(avg(doc_len), 1)::float AS avg_doc_len
                        FROM filtered_chunks
                    ),
                    idf AS (
                        SELECT
                            qt.term,
                            ln(((corpus.total_docs - count(fc.chunk_id)::float + 0.5) / (count(fc.chunk_id)::float + 0.5)) + 1.0) AS value
                        FROM query_terms qt
                        CROSS JOIN corpus
                        LEFT JOIN filtered_chunks fc ON position(qt.term in fc.haystack) > 0
                        GROUP BY qt.term, corpus.total_docs
                    ),
                    scored AS (
                        SELECT
                            fc.chunk_id,
                            fc.title,
                            fc.section,
                            fc.content,
                            fc.source_name,
                            fc.source_url,
                            fc.publish_date,
                            fc.metadata,
                            SUM(
                                idf.value * (
                                    (term_hit.tf * 2.2) /
                                    (term_hit.tf + 1.2 * (0.25 + 0.75 * fc.doc_len / corpus.avg_doc_len))
                                )
                            ) AS bm25_score
                        FROM filtered_chunks fc
                        CROSS JOIN corpus
                        JOIN query_terms qt ON position(qt.term in fc.haystack) > 0
                        JOIN idf ON idf.term = qt.term
                        CROSS JOIN LATERAL (
                            SELECT (
                                (length(fc.haystack) - length(replace(fc.haystack, qt.term, ''))) /
                                greatest(length(qt.term), 1)
                            )::float AS tf
                        ) term_hit
                        WHERE term_hit.tf > 0
                        GROUP BY
                            fc.chunk_id, fc.title, fc.section, fc.content, fc.source_name,
                            fc.source_url, fc.publish_date, fc.metadata, fc.doc_len, corpus.avg_doc_len
                    )
                    SELECT
                        chunk_id,
                        title,
                        section,
                        content,
                        source_name,
                        source_url,
                        publish_date,
                        metadata,
                        bm25_score AS score
                    FROM scored
                    ORDER BY bm25_score DESC
                    LIMIT %s
                    """,
                    tuple(params),
                ).fetchall()
        return [TravelChunkSearchResult(**dict(row)) for row in rows]

    def similarity_search(self, query: str, k: int = 5, source_name: str | None = None) -> list[KnowledgeDocument]:
        results = self.search_chunks(query=query, top_k=k, source_name=source_name)
        return [
            KnowledgeDocument(
                id=item.chunk_id,
                title=item.title,
                content=item.content,
                summary=summarize_text(item.content),
                source_url=item.source_url,
                source_name=item.source_name,
                published_at=as_datetime(item.publish_date),
                score=item.score,
            )
            for item in results
        ]

    def keyword_search(self, query: str, k: int = 5, source_name: str | None = None) -> list[KnowledgeDocument]:
        results = self.keyword_search_chunks(query=query, top_k=k, source_name=source_name)
        return [
            KnowledgeDocument(
                id=item.chunk_id,
                title=item.title,
                content=item.content,
                summary=summarize_text(item.content),
                source_url=item.source_url,
                source_name=item.source_name,
                published_at=as_datetime(item.publish_date),
                score=item.score,
            )
            for item in results
        ]

    def close(self) -> None:
        self.connections.close()


def create_travel_vector_store(database_url: str | None) -> PostgresTravelVectorStore | None:
    if not database_url:
        return None
    return PostgresTravelVectorStore(database_url)


def split_markdown_document(
    *,
    title: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[TravelDocumentChunk]:
    text = normalize_document_content(content)
    if not text:
        return []
    sections = markdown_sections(title, text)
    chunks: list[TravelDocumentChunk] = []
    base_metadata = dict(metadata or {})
    for section, section_text in sections:
        for split in split_text(section_text, chunk_size=chunk_size, overlap=overlap):
            clean = split.strip()
            if not clean:
                continue
            chunk_index = len(chunks)
            chunks.append(
                TravelDocumentChunk(
                    chunk_id=stable_hash(title, section, str(chunk_index), clean),
                    title=title,
                    section=section,
                    content=clean,
                    metadata={**base_metadata, "section": section, "chunk_index": chunk_index},
                )
            )
    return chunks


def markdown_sections(title: str, content: str) -> list[tuple[str, str]]:
    lines = content.splitlines()
    heading_stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_section = title or "文档"
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        text = "\n".join(line for line in current_lines).strip()
        if text:
            sections.append((current_section, text))

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            current_lines.append(line)
            continue
        flush()
        current_lines = []
        level = len(match.group(1))
        heading = match.group(2).strip()
        heading_stack = [(item_level, item_title) for item_level, item_title in heading_stack if item_level < level]
        heading_stack.append((level, heading))
        path = [item_title for _, item_title in heading_stack]
        if title and (not path or path[0] != title):
            path.insert(0, title)
        current_section = " > ".join(path)

    flush()
    if sections:
        return sections
    return [(title or "文档", content.strip())]


def build_rag_context(results: list[TravelChunkSearchResult], max_chars: int = 3500) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    used = 0
    for result in results:
        key = result.chunk_id or stable_hash(result.title, result.section, result.content)
        if key in seen:
            continue
        seen.add(key)
        source = result.source_url or result.source_name or "unknown"
        publish_date = result.publish_date.isoformat() if hasattr(result.publish_date, "isoformat") else ""
        block = (
            f"[来源{len(parts) + 1}] 标题：{result.title}\n"
            f"章节：{result.section or result.title}\n"
            f"来源：{result.source_name} {source}\n"
            f"发布时间：{publish_date or '未知'}\n"
            f"相关度：{round(float(result.score or 0.0), 4)}\n"
            f"内容：{result.content}"
        )
        if used and used + len(block) + 2 > max_chars:
            break
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts)


def normalize_text(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_document_content(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text: str, chunk_size: int = 700, overlap: int = 100) -> list[str]:
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


def lexical_query_terms(text: str, limit: int = 24) -> list[str]:
    normalized = normalize_text(text).lower()
    raw_terms = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", normalized)
    terms: list[str] = []
    stopwords = {"怎么", "哪些", "如何", "什么", "一下", "安排", "推荐", "可以", "有限", "进行", "以及"}
    for term in raw_terms:
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            if 2 <= len(term) <= 12 and term not in stopwords:
                terms.append(term)
            for size in (2, 3):
                for index in range(0, max(0, len(term) - size + 1)):
                    gram = term[index : index + size]
                    if gram not in stopwords:
                        terms.append(gram)
        elif term not in stopwords:
            terms.append(term)
    unique: list[str] = []
    seen: set[str] = set()
    for term in sorted(terms, key=len, reverse=True):
        if term in seen:
            continue
        seen.add(term)
        unique.append(term)
        if len(unique) >= limit:
            break
    return unique


def summarize_text(text: str, limit: int = 180) -> str:
    text = normalize_text(text)
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


def parse_publish_date(value: date | datetime | str | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(text[:10])


def as_datetime(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def stable_document_id(
    *,
    explicit_id: Any | None,
    source_type: str,
    source_url: str | None,
    source_name: str,
    title: str,
) -> str:
    if explicit_id:
        return str(explicit_id)
    if source_url:
        return stable_hash("source-url", source_type or "", source_url)
    return stable_hash("source-title", source_type or "", source_name or "", title or "")


def prefixed_content_hash(content: str) -> str:
    return f"sha256:{stable_hash(content)}"


def stable_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part or "").encode("utf-8", errors="ignore"))
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


def jsonb(value: dict[str, Any]):
    if Jsonb is not None:
        return Jsonb(value)
    return value
