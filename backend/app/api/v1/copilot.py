"""RAG copilot: grounded answers (/ask) and raw hybrid retrieval (/search).

Callers: OPERATOR+ users (JWT) and the n8n P1 workflow (HMAC service principal).
"""

from __future__ import annotations

from functools import lru_cache
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.api.v1.webhooks import parse_event
from app.core.config import get_settings
from app.core.errors import APIError, problem_responses
from app.core.security import Principal, Role, require_role
from app.services import events
from app.services.rag.copilot import Copilot, CopilotResponse
from app.services.rag.retriever import HybridRetriever

router = APIRouter(prefix="/copilot", tags=["copilot"])
reader = require_role(Role.OPERATOR, Role.SERVICE)


class CopilotAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=3, max_length=2000)
    top_k: int = Field(6, ge=1, le=12)


class CopilotSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(5, ge=1, le=20)
    doc_types: list[str] | None = Field(None, examples=[["sops", "legal"]])


class SearchHit(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    section: str
    source_file: str
    doc_type: str
    authority: str
    content: str
    rrf_score: float
    semantic_rank: int | None
    similarity: float | None
    keyword_rank: int | None
    keyword_score: float | None


class CopilotSearchResponse(BaseModel):
    query: str
    embedding_model: str
    count: int
    results: list[SearchHit]


@lru_cache
def get_retriever() -> HybridRetriever:
    from app.services.rag.embeddings import get_embedder

    settings = get_settings()
    return HybridRetriever(settings.psycopg2_dsn, get_embedder(settings))


@lru_cache
def get_copilot() -> Copilot:
    if get_settings().copilot_engine == "native":
        return Copilot(get_retriever())
    from app.services.rag.langchain_copilot import LangChainCopilot

    return LangChainCopilot(get_retriever())


@router.post("/ask", response_model=CopilotResponse, responses=problem_responses(401, 403, 422, 503))
async def ask(request: Request, principal: Principal = Depends(reader), copilot: Copilot = Depends(get_copilot)):
    """Grounded answer with verified citations from SOPs, contracts and correspondence."""
    req = parse_event(CopilotAskRequest, await request.body())
    try:
        answer = await run_in_threadpool(copilot.ask, req.question, req.top_k)
    except RuntimeError as exc:  # every Gemini model failed (see Copilot._generate)
        await run_in_threadpool(_audit_ask, principal, req.question, None, str(exc))
        raise APIError(503, "LLM_UNAVAILABLE", str(exc)) from None
    await run_in_threadpool(_audit_ask, principal, req.question, answer, None)
    return answer


def _audit_ask(principal: Principal, question: str, answer: CopilotResponse | None, error: str | None) -> None:
    """One automation_events row per copilot call, so its usage and failures show up in analytics."""
    payload = {"asked_by": principal.subject, "question": question[:300]}
    if answer is not None:
        payload.update(grounded=answer.grounded, model=answer.model, citations=len(answer.citations),
                       insufficient_context=answer.insufficient_context)
    else:
        payload["error"] = error[:300] if error else None
    events.record_standalone(event_id=uuid4(), event_type="copilot.ask", direction="inbound",
                             source=principal.subject, subject_id=None,
                             status="PROCESSED" if answer is not None else "FAILED", payload=payload)


@router.post("/search", response_model=CopilotSearchResponse, responses=problem_responses(401, 403, 422, 503))
async def search(request: Request, _: Principal = Depends(reader),
                 retriever: HybridRetriever = Depends(get_retriever)):
    """Hybrid (pgvector cosine + full-text, RRF-fused) retrieval without generation."""
    req = parse_event(CopilotSearchRequest, await request.body())
    try:
        hits = await run_in_threadpool(retriever.search, req.query, req.top_k, req.doc_types)
    except RuntimeError as exc:
        raise APIError(503, "EMBEDDINGS_UNAVAILABLE", str(exc)) from None
    return CopilotSearchResponse(
        query=req.query, embedding_model=retriever.embedder.name, count=len(hits),
        results=[SearchHit(**{k: getattr(h, k) for k in SearchHit.model_fields if k != "authority"},
                           authority=h.authority) for h in hits],
    )
