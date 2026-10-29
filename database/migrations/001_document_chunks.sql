-- Stage 2: vector + full-text storage for the RAG corpus (nexusflow_documents/).
-- Idempotent; applied automatically by app.services.rag.ingest.
-- Embedding dimension 768 = gemini-embedding-001 with output_dimensionality=768
-- (also matches the local fallback, BAAI/bge-base-en-v1.5).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_file     text        NOT NULL,              -- path relative to nexusflow_documents/
    doc_id          text        NOT NULL,              -- e.g. SOP-01, MSA-v1.0, Email_Thread_P1_Outage
    doc_type        text        NOT NULL,              -- folder: sops | legal | correspondence | internal_ops
    title           text        NOT NULL,
    section         text        NOT NULL DEFAULT '',
    chunk_index     integer     NOT NULL CHECK (chunk_index >= 0),
    content         text        NOT NULL,              -- chunk text incl. its context header
    content_hash    char(64)    NOT NULL,
    metadata        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    embedding_model text        NOT NULL,              -- vectors are only comparable within one model
    embedding       vector(768) NOT NULL,
    content_tsv     tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', coalesce(title, '')),   'A') ||
                        setweight(to_tsvector('english', coalesce(section, '')), 'B') ||
                        setweight(to_tsvector('english', content),               'C')
                    ) STORED,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (embedding_model, source_file, chunk_index)
);

-- Approximate nearest neighbour on cosine distance (<=>)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Keyword side of hybrid search
CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv ON document_chunks USING gin (content_tsv);

-- Entity / filter lookups, e.g. metadata @> '{"entities": ["CUST-0040"]}'
CREATE INDEX IF NOT EXISTS idx_chunks_metadata ON document_chunks USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_model_source ON document_chunks (embedding_model, source_file);
