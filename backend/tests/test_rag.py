"""Stage 2 RAG tests.

Three tiers:
  * offline   - chunking, citation verification, copilot control flow (no DB, no network)
  * db        - ingestion, vector distance, keyword + hybrid search against live Postgres,
                using a deterministic hashing embedder stored under its own model name
  * live      - real Gemini embeddings + generation; skipped without GEMINI_API_KEY.
                Deselect with `pytest -m "not live"` to avoid API calls.
"""

import hashlib
import math
import re
from types import SimpleNamespace

import psycopg2
import pytest

from app.core.config import DOCUMENTS_DIR, get_settings
from app.services.rag.chunking import MAX_CHARS, chunk_document, extract_entities, iter_documents
from app.services.rag.copilot import Copilot, LLMAnswer, LLMCitation, NO_CONTEXT_ANSWER, verify_answer
from app.services.rag.ingest import ingest_documents, to_pgvector
from app.services.rag.retriever import HybridRetriever, RetrievedChunk

SETTINGS = get_settings()
DSN = SETTINGS.psycopg2_dsn


class HashingEmbedder:
    """Deterministic 768-d bag-of-words embedder: lexical, not semantic, but stable and offline."""

    name = "test/hashing@768"
    dim = 768

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+(?:-[0-9]+)*", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 10) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def _db_available() -> bool:
    try:
        psycopg2.connect(DSN, connect_timeout=2).close()
        return True
    except psycopg2.OperationalError:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="PostgreSQL not reachable at DATABASE_URL")


def query(sql, params=()):
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# --------------------------------------------------------------------------- offline: chunking

ALL_CHUNKS = {p.relative_to(DOCUMENTS_DIR).as_posix(): chunk_document(p, DOCUMENTS_DIR)
              for p in iter_documents(DOCUMENTS_DIR)}


def test_corpus_covers_all_four_folders():
    folders = {path.split("/")[0] for path in ALL_CHUNKS}
    assert folders == {"sops", "legal", "correspondence", "internal_ops"}
    assert len(ALL_CHUNKS) == 7


def test_every_chunk_carries_context_header_and_bounded_size():
    for path, chunks in ALL_CHUNKS.items():
        assert chunks, path
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
        for c in chunks:
            first_line, second_line = c.content.splitlines()[:2]
            assert first_line.startswith(f"[{c.doc_id}] {c.title} — {c.section}")
            assert f"Source: {path}" in second_line
            assert c.metadata["char_count"] <= MAX_CHARS


def test_sop_is_split_on_headings_with_doc_id():
    chunks = ALL_CHUNKS["sops/SOP_01_Ticket_Escalation.md"]
    assert {c.doc_id for c in chunks} == {"SOP-01"}
    assert [c.section for c in chunks] == [
        "Overview", "1. Severity rubric", "2. Human initial response matrix",
        "3. Procedure", "4. Synthetic example and ambiguity", "5. Audit and retrieval controls",
    ]
    rubric = chunks[1].content
    assert "| P1 |" in rubric and "4 clock hours" in rubric  # table kept whole
    assert all(c.metadata["authority"] == "governing" for c in chunks)


def test_email_thread_is_split_per_message():
    chunks = ALL_CHUNKS["correspondence/Email_Thread_P1_Outage.md"]
    messages = [c for c in chunks if c.section.startswith("Message from")]
    assert len(messages) == 5
    assert all(c.metadata["authority"] == "narrative" for c in chunks)
    assert {"CUST-0040", "CTR-0040", "TK-0139"} <= set(chunks[0].metadata["entities"])


def test_entity_extraction():
    text = "CUST-0011 owes INV-2026-0011 on CTR-0011; see SOP-02, ticket TK-0139, owner EMP-0082."
    assert extract_entities(text) == ["CTR-0011", "CUST-0011", "EMP-0082", "INV-2026-0011", "SOP-02", "TK-0139"]


def test_to_pgvector_literal():
    assert to_pgvector([0.5, -1.0, 0.0]) == "[0.50000000,-1.00000000,0.00000000]"


# --------------------------------------------------------------------------- offline: copilot


def _chunk(sid: int, content: str, authority="governing") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"00000000-0000-0000-0000-00000000000{sid}", source_file=f"sops/doc{sid}.md",
        doc_id=f"SOP-0{sid}", doc_type="sops", title="t", section=f"sec {sid}", chunk_index=0,
        content=content, metadata={"authority": authority}, rrf_score=0.03,
        semantic_rank=sid, similarity=0.8, keyword_rank=sid, keyword_score=0.5,
    )


BY_ID = {
    "S1": _chunk(1, "Enterprise P1 resolution: **4 clock hours**; initial response 15 clock minutes."),
    "S2": _chunk(2, "Human Finance approval is mandatory when the aggregate exceeds $500.00."),
}


def test_verify_answer_accepts_verbatim_quotes_ignoring_markdown():
    raw = LLMAnswer(
        answer="Enterprise P1 must be resolved within 4 clock hours [S1].",
        citations=[LLMCitation(source_id="S1", quote="Enterprise P1 resolution: 4 clock hours")],
        insufficient_context=False, confidence="high",
    )
    resp = verify_answer("q", raw, BY_ID, "m")
    assert resp.grounded and resp.confidence == "high" and resp.warnings == []
    assert resp.citations[0].doc_id == "SOP-01" and resp.citations[0].quote_verified


def test_verify_answer_flags_hallucinated_sources_and_quotes():
    raw = LLMAnswer(
        answer="Approval is needed above $400 [S2][S7].",
        citations=[
            LLMCitation(source_id="S2", quote="approval is needed above $400"),  # not in source
            LLMCitation(source_id="S9", quote="whatever"),                        # unknown source
        ],
        insufficient_context=False, confidence="high",
    )
    resp = verify_answer("q", raw, BY_ID, "m")
    assert not resp.grounded and resp.confidence == "low"
    assert [c.source_id for c in resp.citations] == ["S2"]
    assert not resp.citations[0].quote_verified
    assert any("S9" in w for w in resp.warnings) and any("S7" in w for w in resp.warnings)


class FakeRetriever:
    def __init__(self, chunks):
        self.chunks = chunks

    def search(self, question, top_k=6):
        return self.chunks[:top_k]


class FakeGenAI:
    def __init__(self, answer: LLMAnswer):
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._generate)
        self._answer = answer

    def _generate(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        return SimpleNamespace(parsed=self._answer, text=self._answer.model_dump_json())


def test_copilot_does_not_call_model_without_context():
    client = FakeGenAI(LLMAnswer(answer="x", citations=[], insufficient_context=False, confidence="high"))
    resp = Copilot(FakeRetriever([]), client=client, model="m").ask("Parental leave policy?")
    assert client.calls == []
    assert resp.insufficient_context and resp.answer == NO_CONTEXT_ANSWER


def test_copilot_sends_numbered_sources_and_strict_config():
    answer = LLMAnswer(
        answer="Finance approval is mandatory above $500.00 [S2].",
        citations=[LLMCitation(source_id="S2", quote="Human Finance approval is mandatory")],
        insufficient_context=False, confidence="high",
    )
    client = FakeGenAI(answer)
    resp = Copilot(FakeRetriever(list(BY_ID.values())), client=client, model="m").ask("Threshold?")
    call = client.calls[0]
    assert '<source id="S1" doc_id="SOP-01"' in call.contents and 'id="S2"' in call.contents
    assert call.config.temperature == 0.0 and call.config.response_schema is LLMAnswer
    assert "ONLY the information inside the <source> blocks" in call.config.system_instruction
    assert resp.grounded and resp.citations[0].source_file == "sops/doc2.md"


def test_copilot_falls_back_when_primary_model_unavailable():
    from google.genai import errors

    answer = LLMAnswer(
        answer="Finance approval is mandatory above $500.00 [S2].",
        citations=[LLMCitation(source_id="S2", quote="Human Finance approval is mandatory")],
        insufficient_context=False, confidence="high",
    )
    client = FakeGenAI(answer)
    ok = client._generate

    def flaky(model, contents, config):
        if model == "primary":
            raise errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}})
        return ok(model, contents, config)

    client.models.generate_content = flaky
    resp = Copilot(FakeRetriever(list(BY_ID.values())), client=client, model="primary",
                   fallback_models=["backup"], retry_backoff_s=0).ask("Threshold?")
    assert resp.model == "backup" and resp.grounded
    assert resp.warnings == ["Fell back past primary: 503 UNAVAILABLE"] * Copilot.ATTEMPTS_PER_MODEL


def test_native_copilot_treats_network_timeouts_as_model_failures():
    import httpx

    answer = LLMAnswer(answer="Finance approval is mandatory above $500.00 [S2].",
                       citations=[LLMCitation(source_id="S2", quote="Human Finance approval is mandatory")],
                       insufficient_context=False, confidence="high")
    client = FakeGenAI(answer)
    ok = client._generate

    def hung(model, contents, config):
        if model == "primary":
            raise httpx.ReadTimeout("The read operation timed out")
        return ok(model, contents, config)

    client.models.generate_content = hung
    resp = Copilot(FakeRetriever(list(BY_ID.values())), client=client, model="primary",
                   fallback_models=["backup"], retry_backoff_s=0).ask("Threshold?")
    assert resp.model == "backup" and resp.warnings == ["Fell back past primary: timeout (ReadTimeout)"]


# --------------------------------------------------------------------------- db: ingestion + search


@pytest.fixture(scope="module")
def hashing_index():
    embedder = HashingEmbedder()
    report = ingest_documents(DSN, embedder, force=True)
    yield embedder, report
    query("DELETE FROM document_chunks WHERE embedding_model = %s RETURNING 1", (embedder.name,))


@needs_db
def test_ingestion_stores_768d_unit_vectors(hashing_index):
    embedder, report = hashing_index
    expected = sum(len(c) for c in ALL_CHUNKS.values())
    assert report.chunks_written == expected and len(report.files_indexed) == 7
    [(n, dims_min, dims_max, norm_min, norm_max, files)] = query(
        "SELECT count(*), min(vector_dims(embedding)), max(vector_dims(embedding)), "
        "min(vector_norm(embedding)), max(vector_norm(embedding)), count(DISTINCT source_file) "
        "FROM document_chunks WHERE embedding_model = %s", (embedder.name,))
    assert (n, dims_min, dims_max, files) == (expected, 768, 768, 7)
    assert norm_min == pytest.approx(1.0, abs=1e-4) and norm_max == pytest.approx(1.0, abs=1e-4)


@needs_db
def test_reingest_is_incremental(hashing_index):
    embedder, _ = hashing_index
    report = ingest_documents(DSN, embedder)
    assert report.files_indexed == [] and len(report.files_skipped) == 7


@needs_db
def test_vector_cosine_distance_query(hashing_index):
    embedder, _ = hashing_index
    [(chunk_id, vec)] = query(
        "SELECT chunk_id::text, embedding::text FROM document_chunks "
        "WHERE embedding_model = %s AND doc_id = 'SOP-02' AND chunk_index = 1", (embedder.name,))
    rows = query(
        "SELECT chunk_id::text, embedding <=> %s::vector AS d FROM document_chunks "
        "WHERE embedding_model = %s ORDER BY d LIMIT 3", (vec, embedder.name))
    assert rows[0][0] == chunk_id and rows[0][1] == pytest.approx(0.0, abs=1e-6)
    assert rows[0][1] < rows[1][1] <= rows[2][1] <= 2.0


@needs_db
def test_hnsw_and_fts_indexes_exist():
    names = {r[0] for r in query("SELECT indexname FROM pg_indexes WHERE tablename = 'document_chunks'")}
    assert {"idx_chunks_embedding_hnsw", "idx_chunks_content_tsv"} <= names


@needs_db
def test_keyword_search_finds_exact_identifiers(hashing_index):
    embedder, _ = hashing_index
    keyword_only = HybridRetriever(DSN, embedder, semantic_weight=0.0)
    top = keyword_only.search("INV-2026-0011 overdue", top_k=3)
    assert top[0].doc_id == "Finance_Collection_Notice"
    assert top[0].keyword_rank == 1


@needs_db
def test_hybrid_search_fuses_both_rankings(hashing_index):
    embedder, _ = hashing_index
    results = HybridRetriever(DSN, embedder).search("Enterprise P1 resolution clock hours severity", top_k=5)
    assert results[0].doc_id == "SOP-01"
    assert any(r.semantic_rank and r.keyword_rank for r in results)
    assert [r.rrf_score for r in results] == sorted((r.rrf_score for r in results), reverse=True)
    assert max(r.rrf_score for r in results) <= 2 / 61 + 1e-9  # two lists, k=60


@needs_db
def test_hybrid_search_doc_type_filter(hashing_index):
    embedder, _ = hashing_index
    results = HybridRetriever(DSN, embedder).search("seat approval", top_k=5, doc_types=["internal_ops"])
    assert results and {r.doc_type for r in results} == {"internal_ops"}


# --------------------------------------------------------------------------- live: Gemini

needs_gemini = pytest.mark.skipif(
    not SETTINGS.gemini_api_key.strip() or SETTINGS.gemini_api_key.startswith("your-"),
    reason="GEMINI_API_KEY not set",
)


@pytest.fixture(scope="module")
def gemini_retriever():
    from app.services.rag.embeddings import GeminiEmbedder

    embedder = GeminiEmbedder(SETTINGS)
    ingest_documents(DSN, embedder)  # no-op if already indexed
    return HybridRetriever(DSN, embedder)


@pytest.mark.live
@needs_db
@needs_gemini
@pytest.mark.parametrize("question,doc_id,section", [
    ("What is the resolution SLA for an Enterprise P1 ticket?", "SOP-01", "1. Severity rubric"),
    ("Does adding 43 seats on an Enterprise plan need Finance approval?", "SOP-02", None),
    ("Is CUST-0011 overdue on INV-2026-0011?", "Finance_Collection_Notice", None),
])
def test_live_hybrid_retrieval(gemini_retriever, question, doc_id, section):
    top = gemini_retriever.search(question, top_k=3)
    assert top[0].doc_id == doc_id
    if section:
        assert section in {r.section for r in top}


@pytest.mark.live
@needs_db
@needs_gemini
def test_live_copilot_grounded_answer(gemini_retriever):
    resp = Copilot(gemini_retriever).ask("What is the Enterprise P1 initial response and resolution SLA?")
    assert resp.grounded and not resp.insufficient_context
    assert any(c.doc_id == "SOP-01" and c.quote_verified for c in resp.citations)
    assert "4 clock hours" in resp.answer.lower() or "four clock hours" in resp.answer.lower()
    assert "15" in resp.answer


@pytest.mark.live
@needs_db
@needs_gemini
def test_live_copilot_refuses_out_of_scope(gemini_retriever):
    resp = Copilot(gemini_retriever).ask("What is NexusFlow's parental leave policy?")
    assert resp.insufficient_context
