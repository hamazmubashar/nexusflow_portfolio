"""Hybrid retrieval over document_chunks: pgvector cosine + Postgres full-text, fused with RRF.

Both candidate lists are computed in one SQL round trip:
  * semantic: top-N by cosine distance (`<=>`, served by the HNSW index)
  * keyword:  top-N by ts_rank_cd over the weighted tsvector (title A, section B, body C).
    The query is turned into an OR of its stemmed lexemes, so a natural-language
    question doesn't need every word to appear (plain websearch_to_tsquery ANDs them).
Ties are broken by (source_file, chunk_index) so rankings are reproducible.
Reciprocal Rank Fusion: score = Σ w_i / (k + rank_i), with k = 60 (Cormack et al., 2009).
RRF uses ranks only, so cosine similarities and ts_rank values never need calibrating.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import psycopg2

from app.services.rag.embeddings import Embedder
from app.services.rag.ingest import to_pgvector

HYBRID_SQL = """
WITH semantic_candidates AS (
    SELECT chunk_id, source_file, chunk_index, embedding <=> %(qvec)s::vector AS distance
    FROM document_chunks
    WHERE embedding_model = %(model)s
      AND (%(doc_types)s::text[] IS NULL OR doc_type = ANY(%(doc_types)s::text[]))
    ORDER BY embedding <=> %(qvec)s::vector
    LIMIT %(candidates)s
),
semantic AS (
    SELECT chunk_id, 1 - distance AS similarity,
           row_number() OVER (ORDER BY distance, source_file, chunk_index) AS rank
    FROM semantic_candidates
),
kw_query AS (
    SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) AS q
    FROM unnest(tsvector_to_array(to_tsvector('english', %(query)s))) AS lexeme
),
keyword_candidates AS (
    SELECT d.chunk_id, d.source_file, d.chunk_index, ts_rank_cd(d.content_tsv, kw.q, 32) AS score
    FROM document_chunks d, kw_query kw
    WHERE kw.q IS NOT NULL
      AND d.embedding_model = %(model)s
      AND (%(doc_types)s::text[] IS NULL OR d.doc_type = ANY(%(doc_types)s::text[]))
      AND d.content_tsv @@ kw.q
    ORDER BY score DESC, d.source_file, d.chunk_index
    LIMIT %(candidates)s
),
keyword AS (
    SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC, source_file, chunk_index) AS rank
    FROM keyword_candidates
),
fused AS (
    SELECT coalesce(s.chunk_id, k.chunk_id) AS chunk_id,
           s.rank AS semantic_rank, s.similarity,
           k.rank AS keyword_rank, k.score AS keyword_score,
           coalesce(%(w_sem)s / (%(rrf_k)s + s.rank), 0)
         + coalesce(%(w_kw)s  / (%(rrf_k)s + k.rank), 0) AS rrf_score
    FROM semantic s FULL OUTER JOIN keyword k ON s.chunk_id = k.chunk_id
)
SELECT d.chunk_id::text, d.source_file, d.doc_id, d.doc_type, d.title, d.section, d.chunk_index,
       d.content, d.metadata, f.rrf_score, f.semantic_rank, f.similarity, f.keyword_rank, f.keyword_score
FROM fused f JOIN document_chunks d USING (chunk_id)
ORDER BY f.rrf_score DESC, f.semantic_rank NULLS LAST, d.source_file, d.chunk_index
LIMIT %(top_k)s
"""


@dataclass
class RetrievedChunk:
    chunk_id: str
    source_file: str
    doc_id: str
    doc_type: str
    title: str
    section: str
    chunk_index: int
    content: str
    metadata: dict
    rrf_score: float
    semantic_rank: int | None
    similarity: float | None
    keyword_rank: int | None
    keyword_score: float | None

    @property
    def authority(self) -> str:
        return self.metadata.get("authority", "reference")


class HybridRetriever:
    def __init__(
        self,
        dsn: str,
        embedder: Embedder,
        rrf_k: int = 60,
        semantic_weight: float = 1.0,
        keyword_weight: float = 1.0,
        candidates: int = 20,
    ):
        self.dsn = dsn
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.semantic_weight = semantic_weight
        self.keyword_weight = keyword_weight
        self.candidates = candidates

    def search(
        self,
        query: str,
        top_k: int = 5,
        doc_types: Sequence[str] | None = None,
        query_vector: list[float] | None = None,
    ) -> list[RetrievedChunk]:
        if not query.strip():
            return []
        qvec = query_vector or self.embedder.embed_query(query)
        params = {
            "qvec": to_pgvector(qvec),
            "query": query,
            "model": self.embedder.name,
            "doc_types": list(doc_types) if doc_types else None,
            "candidates": max(self.candidates, top_k),
            "top_k": top_k,
            "rrf_k": float(self.rrf_k),
            "w_sem": float(self.semantic_weight),
            "w_kw": float(self.keyword_weight),
        }
        conn = psycopg2.connect(self.dsn)
        try:
            with conn, conn.cursor() as cur:
                # HNSW returns at most ef_search rows before the WHERE filter is applied;
                # iterative scan (pgvector >= 0.8) keeps searching until enough rows pass it
                cur.execute("SET LOCAL hnsw.ef_search = %s", (max(40, params["candidates"] * 2),))
                cur.execute("SET LOCAL hnsw.iterative_scan = strict_order")
                cur.execute(HYBRID_SQL, params)
                return [RetrievedChunk(*row) for row in cur.fetchall()]
        finally:
            conn.close()
