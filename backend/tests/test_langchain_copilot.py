"""LangChain (LCEL) copilot: retriever adapter, prompt, parser, retry/fallback policy, verification.

Offline tests use a fake google-genai client that raises the SDK's real error types, so
the with_retry / with_fallbacks policy is exercised exactly as it runs against Gemini.
The live test needs GEMINI_API_KEY and the indexed corpus (pytest -m live).
"""

import json
from types import SimpleNamespace

import pytest
from google.genai import errors
from langchain_core.documents import Document

from app.core.config import get_settings
from app.services.rag.copilot import NO_CONTEXT_ANSWER, SYSTEM_PROMPT, LLMAnswer, LLMCitation
from app.services.rag.langchain_copilot import (
    PARSER, PROMPT, HybridSearchRetriever, LangChainCopilot, TransientLLMError, gemini_runnable,
)
from app.services.rag.retriever import HybridRetriever, RetrievedChunk

SETTINGS = get_settings()


def chunk(i: int, content: str, authority="governing") -> RetrievedChunk:
    return RetrievedChunk(chunk_id=f"c{i}", source_file=f"sops/doc{i}.md", doc_id=f"SOP-0{i}", doc_type="sops",
                          title="t", section=f"sec {i}", chunk_index=0, content=content,
                          metadata={"authority": authority}, rrf_score=0.03, semantic_rank=i, similarity=0.8,
                          keyword_rank=i, keyword_score=0.4)


CHUNKS = [chunk(1, "Enterprise P1 resolution: 4 clock hours; initial response 15 clock minutes."),
          chunk(2, "Human Finance approval is mandatory when the aggregate exceeds $500.00.")]


class FakeHybrid(HybridRetriever):
    def __init__(self, chunks):  # no DB / embedder needed
        self.chunks, self.queries = chunks, []
        self.embedder = SimpleNamespace(name="fake@768")

    def search(self, query, top_k=5, doc_types=None, query_vector=None):
        self.queries.append((query, top_k))
        return self.chunks[:top_k]


GOOD = LLMAnswer(answer="Enterprise P1 must be resolved within 4 clock hours [S1].",
                 citations=[LLMCitation(source_id="S1", quote="Enterprise P1 resolution: 4 clock hours")],
                 insufficient_context=False, confidence="high")


class FakeGenAI:
    """google-genai client double: per-model scripted outcomes (an exception or an LLMAnswer)."""

    def __init__(self, script: dict[str, list]):
        self.script, self.calls = {m: list(v) for m, v in script.items()}, []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome.model_dump_json())


def server(code, status):
    return errors.ServerError(code, {"error": {"code": code, "status": status, "message": status}})


def client_err(code, status):
    return errors.ClientError(code, {"error": {"code": code, "status": status, "message": status}})


def copilot(script, chunks=CHUNKS, models=("m1", "m2")):
    fake = FakeGenAI(script)
    llm = gemini_runnable(list(models), client=fake, timeout_ms=1000, attempts=3)
    return LangChainCopilot(FakeHybrid(chunks), llm=llm, models=list(models)), fake


# --------------------------------------------------------------------------- building blocks


def test_prompt_template_carries_system_rules_and_format():
    assert set(PROMPT.input_variables) == {"question", "sources"}
    msgs = PROMPT.invoke({"question": "Q?", "sources": '<source id="S1">x</source>'}).to_messages()
    assert msgs[0].type == "system" and msgs[0].content.startswith(SYSTEM_PROMPT)
    assert '"insufficient_context"' in msgs[0].content  # PydanticOutputParser format instructions
    assert msgs[1].type == "human" and '<source id="S1">' in msgs[1].content and msgs[1].content.endswith("Q?")


def test_output_parser_round_trips_llm_answer():
    assert PARSER.parse(GOOD.model_dump_json()) == GOOD


def test_retriever_adapter_yields_documents_with_chunks():
    hybrid = FakeHybrid(CHUNKS)
    docs = HybridSearchRetriever(hybrid=hybrid, top_k=1).invoke("p1 sla")
    assert len(docs) == 1 and isinstance(docs[0], Document)
    assert docs[0].metadata["chunk"] is CHUNKS[0] and docs[0].metadata["authority"] == "governing"
    assert hybrid.queries == [("p1 sla", 1)]


# --------------------------------------------------------------------------- chain behaviour


def test_chain_answers_with_verified_citations():
    cp, fake = copilot({"m1": [GOOD]})
    r = cp.ask("What is the P1 SLA?", top_k=2)
    assert r.grounded and r.model == "m1" and r.citations[0].quote_verified
    assert r.citations[0].doc_id == "SOP-01" and r.warnings == ["engine: langchain"]
    call = fake.calls[0]
    assert call.config.temperature == 0.0 and call.config.response_schema is LLMAnswer
    assert "ONLY the information inside the <source> blocks" in call.config.system_instruction
    assert '<source id="S2" doc_id="SOP-02"' in call.contents


def test_no_context_refuses_without_calling_the_model():
    cp, fake = copilot({"m1": [GOOD]}, chunks=[])
    r = cp.ask("Parental leave?")
    assert r.insufficient_context and r.answer == NO_CONTEXT_ANSWER and fake.calls == []


def test_hallucinated_quote_is_flagged_not_trusted():
    bad = LLMAnswer(answer="Approval above $400 [S2].",
                    citations=[LLMCitation(source_id="S2", quote="approval above $400")],
                    insufficient_context=False, confidence="high")
    r = copilot({"m1": [bad]})[0].ask("threshold?")
    assert not r.grounded and r.confidence == "low" and not r.citations[0].quote_verified


def test_overload_is_retried_on_same_model():
    cp, fake = copilot({"m1": [server(503, "UNAVAILABLE"), GOOD]})
    r = cp.ask("p1?")
    assert r.model == "m1" and [c.model for c in fake.calls] == ["m1", "m1"]


def test_timeout_and_retired_model_fall_back_to_next_model():
    cp, fake = copilot({"m1": [server(504, "DEADLINE_EXCEEDED")], "m2": [GOOD]})
    assert cp.ask("p1?").model == "m2"
    cp, fake = copilot({"m1": [client_err(404, "NOT_FOUND")], "m2": [GOOD]})
    assert cp.ask("p1?").model == "m2" and [c.model for c in fake.calls] == ["m1", "m2"]


def test_quota_exhaustion_retries_then_falls_back():
    q = client_err(429, "RESOURCE_EXHAUSTED")
    cp, fake = copilot({"m1": [q, q, q], "m2": [GOOD]})
    assert cp.ask("p1?").model == "m2" and [c.model for c in fake.calls] == ["m1", "m1", "m1", "m2"]


def test_network_timeout_falls_back_like_a_504():
    import httpx

    cp, fake = copilot({"m1": [httpx.ReadTimeout("read timed out")], "m2": [GOOD]})
    assert cp.ask("p1?").model == "m2" and [c.model for c in fake.calls] == ["m1", "m2"]
    cp, _ = copilot({"m1": [httpx.ReadTimeout("t")], "m2": [httpx.ReadTimeout("t")]})
    with pytest.raises(RuntimeError, match="timeout"):
        cp.ask("p1?")


def test_all_models_failing_raises_runtime_error_for_503_mapping():
    cp, _ = copilot({"m1": [server(504, "DEADLINE_EXCEEDED")], "m2": [server(504, "DEADLINE_EXCEEDED")]})
    with pytest.raises(RuntimeError, match="All Gemini models failed"):
        cp.ask("p1?")


def test_non_transient_errors_are_not_masked():
    cp, fake = copilot({"m1": [client_err(400, "INVALID_ARGUMENT")], "m2": [GOOD]})
    with pytest.raises(errors.ClientError):
        cp.ask("p1?")
    assert [c.model for c in fake.calls] == ["m1"]


def test_transient_error_hierarchy():
    from app.services.rag.langchain_copilot import RetryableLLMError

    assert issubclass(RetryableLLMError, TransientLLMError)


def test_api_uses_langchain_engine_by_default(monkeypatch):
    from app.api.v1 import copilot as copilot_api

    assert SETTINGS.copilot_engine == "langchain"
    copilot_api.get_copilot.cache_clear()
    monkeypatch.setattr(copilot_api, "get_retriever", lambda: FakeHybrid(CHUNKS))
    try:
        assert isinstance(copilot_api.get_copilot(), LangChainCopilot)
    finally:
        copilot_api.get_copilot.cache_clear()


# --------------------------------------------------------------------------- live


@pytest.mark.live
@pytest.mark.skipif(not SETTINGS.gemini_api_key.strip(), reason="GEMINI_API_KEY not set")
def test_live_langchain_copilot_grounded_answer():
    from app.services.rag.embeddings import get_embedder

    cp = LangChainCopilot(HybridRetriever(SETTINGS.psycopg2_dsn, get_embedder(SETTINGS)))
    r = cp.ask("What is the Enterprise P1 initial response and resolution SLA?")
    assert r.grounded and not r.insufficient_context
    assert any(c.doc_id == "SOP-01" and c.quote_verified for c in r.citations)
    assert "15" in r.answer and ("4 clock hours" in r.answer.lower() or "four clock hours" in r.answer.lower())
    assert json.loads(r.model_dump_json())["warnings"][0] == "engine: langchain"
