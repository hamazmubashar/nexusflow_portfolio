"""Document ingestion: chunk nexusflow_documents/*.md, embed, store in document_chunks.

Incremental: a file whose content (and chunker version) is unchanged for the
current embedding model is skipped, so re-runs make no embedding API calls.
Rows for files that no longer exist are pruned. All writes for one run go in
a single transaction.

Usage (from backend/):
    python -m app.services.rag.ingest            # incremental
    python -m app.services.rag.ingest --force    # re-embed everything
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

from app.core.config import DOCUMENTS_DIR, get_settings
from app.core.db import apply_migrations
from app.services.rag.chunking import CHUNKER_VERSION, Chunk, chunk_document, iter_documents
from app.services.rag.embeddings import Embedder, get_embedder

log = logging.getLogger("nexusflow.rag.ingest")


@dataclass
class DocIngestReport:
    embedding_model: str
    files_indexed: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    files_pruned: list[str] = field(default_factory=list)
    chunks_written: int = 0


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


def file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() + f"|chunker={CHUNKER_VERSION}".encode()).hexdigest()


def ingest_documents(
    dsn: str,
    embedder: Embedder,
    docs_dir: Path = DOCUMENTS_DIR,
    force: bool = False,
) -> DocIngestReport:
    report = DocIngestReport(embedding_model=embedder.name)
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            apply_migrations(cur)

            cur.execute(
                "SELECT source_file, min(metadata->>'file_hash') FROM document_chunks "
                "WHERE embedding_model = %s GROUP BY source_file",
                (embedder.name,),
            )
            indexed = dict(cur.fetchall())

            pending: list[tuple[str, str, list[Chunk]]] = []
            on_disk: set[str] = set()
            for path in iter_documents(docs_dir):
                rel = path.relative_to(docs_dir).as_posix()
                on_disk.add(rel)
                fingerprint = file_fingerprint(path)
                if not force and indexed.get(rel) == fingerprint:
                    report.files_skipped.append(rel)
                    continue
                pending.append((rel, fingerprint, chunk_document(path, docs_dir)))

            # One embedding pass for every changed chunk (batched inside the embedder)
            all_chunks = [c for _, _, chunks in pending for c in chunks]
            vectors = embedder.embed_documents([c.content for c in all_chunks]) if all_chunks else []
            if any(len(v) != embedder.dim for v in vectors):
                raise ValueError(f"Embedder returned vectors that are not {embedder.dim}-d")
            vec_iter = iter(vectors)

            for rel, fingerprint, chunks in pending:
                cur.execute(
                    "DELETE FROM document_chunks WHERE embedding_model = %s AND source_file = %s",
                    (embedder.name, rel),
                )
                rows = [
                    (
                        c.source_file, c.doc_id, c.doc_type, c.title, c.section, c.chunk_index,
                        c.content, c.content_hash,
                        json.dumps({**c.metadata, "file_hash": fingerprint, "chunk_count": len(chunks)}),
                        embedder.name, to_pgvector(next(vec_iter)),
                    )
                    for c in chunks
                ]
                execute_values(
                    cur,
                    "INSERT INTO document_chunks (source_file, doc_id, doc_type, title, section, chunk_index, "
                    "content, content_hash, metadata, embedding_model, embedding) VALUES %s",
                    rows,
                    template="(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::vector)",
                )
                report.files_indexed.append(rel)
                report.chunks_written += len(rows)
                log.info("  indexed %-45s %3d chunks", rel, len(rows))

            stale = sorted(set(indexed) - on_disk)
            if stale:
                cur.execute(
                    "DELETE FROM document_chunks WHERE embedding_model = %s AND source_file = ANY(%s)",
                    (embedder.name, stale),
                )
                report.files_pruned = stale
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Embed nexusflow_documents into document_chunks.")
    parser.add_argument("--force", action="store_true", help="re-embed files even if unchanged")
    parser.add_argument("--docs-dir", type=Path, default=DOCUMENTS_DIR)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    embedder = get_embedder(settings)
    log.info("Embedding model: %s", embedder.name)
    report = ingest_documents(settings.psycopg2_dsn, embedder, docs_dir=args.docs_dir, force=args.force)
    log.info(
        "Done: %d files indexed (%d chunks), %d unchanged, %d pruned",
        len(report.files_indexed), report.chunks_written, len(report.files_skipped), len(report.files_pruned),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
