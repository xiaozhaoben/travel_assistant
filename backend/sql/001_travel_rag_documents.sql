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
    raw_content text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
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
    embedding vector(512) NOT NULL,
    province text,
    city text,
    scenic_spot text,
    data_type text,
    source_name text NOT NULL DEFAULT '',
    source_url text,
    publish_date date,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_doc_id ON documents (doc_id);
CREATE INDEX IF NOT EXISTS idx_documents_source_url ON documents (source_url);
CREATE INDEX IF NOT EXISTS idx_documents_city_type ON documents (province, city, data_type);
CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_id ON document_chunks (doc_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_city_type ON document_chunks (province, city, data_type);
CREATE INDEX IF NOT EXISTS idx_document_chunks_publish_date ON document_chunks (publish_date DESC);
