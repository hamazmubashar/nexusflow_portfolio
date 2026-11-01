"""Grounded RAG copilot: hybrid retrieval -> Gemini structured answer -> citation verification.

Grounding is enforced in three layers:
  1. Prompt: answer only from numbered <source> blocks, cite [S#] for every claim,
     prefer governing SOP/contract text over narrative email/chat, never invent IDs.
  2. Schema: Gemini returns JSON matching LLMAnswer (answer, citations with verbatim
     quotes, insufficient_context flag) via response_schema, at temperature 0.
  3. Verification: citations pointing at unknown sources are dropped, each quote is
     checked verbatim against its chunk, and `grounded` is False unless at least one
     citation survives and every [S#] marker in the answer refers to a cited source.
With no retrieved context the model is not called at all.
"""

from __future__ import annotations

import re
import time

import httpx
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.rag.retriever import HybridRetriever, RetrievedChunk

SYSTEM_PROMPT = """\
You are NexusFlow Autopilot Copilot, an assistant for NexusFlow Systems' IT support, customer \
success and finance teams. You answer questions about NexusFlow SOPs, contracts and account \
correspondence.

Rules. Follow all of them:
1. Use ONLY the information inside the <source> blocks provided with the question. Do not use \
outside knowledge, and do not guess or extrapolate numbers, dates, SLAs, prices or thresholds.
2. Every factual sentence in `answer` must end with one or more citation markers like [S1] or \
[S2][S4], referring to the source ids given.
3. For each source you cite, add an entry to `citations` with its source_id and a short quote \
copied VERBATIM (character for character, max ~30 words) from that source that supports the claim.
4. Reproduce identifiers exactly as written: CUST-XXXX, CTR-XXXX, TK-XXXX, INV-YYYY-XXXX, \
EMP-XXXX, SOP-XX. Never invent an identifier, approval ID (APR-XXXX), amount, or record that \
does not appear in the sources.
5. Sources marked authority="governing" (SOPs, MSA/SOW) define policy. Sources marked \
authority="narrative" (emails, chat transcripts, notices) describe scenarios, requests or opinions. \
Where they conflict, state the governing rule and say the narrative source is not authoritative. \
Never present a request, reservation or proposed action in a narrative source as approved, \
activated, paid or resolved.
6. When a rule involves arithmetic (e.g. seat cost per month vs the $500 approval threshold), show \
the calculation using only figures from the sources.
7. If the sources do not contain enough information to answer, set insufficient_context to true, \
say briefly what is missing, and do not attempt a partial guess.
8. Be concise and operational: lead with the direct answer, then the supporting rule or steps.
"""


class LLMCitation(BaseModel):
    source_id: str = Field(description="Source id such as S1")
    quote: str = Field(description="Verbatim supporting excerpt copied from that source")


class LLMAnswer(BaseModel):
    answer: str = Field(description="Answer with [S#] markers after every factual sentence")
    citations: list[LLMCitation]
    insufficient_context: bool
    confidence: Literal["high", "medium", "low"]


class Citation(BaseModel):
    source_id: str
    doc_id: str
    source_file: str
    section: str
    authority: str
    chunk_id: str
    quote: str
    quote_verified: bool


class CopilotResponse(BaseModel):
    question: str
    answer: str
    citations: list[Citation]
    insufficient_context: bool
    confidence: Literal["high", "medium", "low"]
    grounded: bool
    model: str
    warnings: list[str] = []
    sources_considered: list[dict] = []


NO_CONTEXT_ANSWER = (
    "I could not find any NexusFlow documentation relevant to this question, so I can't answer it "
    "without guessing. Try rephrasing, or include the SOP, customer (CUST-XXXX) or ticket (TK-XXXX) ID."
)

_MARKER_RE = re.compile(r"\[(S\d+)\]")


def _normalize(text: str) -> str:
    """Lower-case, strip markdown emphasis/backticks and collapse whitespace for quote matching."""
    text = re.sub(r"[*`_]", "", text.lower())
    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def build_context(chunks: list[RetrievedChunk]) -> tuple[str, dict[str, RetrievedChunk]]:
    by_id: dict[str, RetrievedChunk] = {}
    blocks = []
    for i, chunk in enumerate(chunks, 1):
        sid = f"S{i}"
        by_id[sid] = chunk
        blocks.append(
            f'<source id="{sid}" doc_id="{chunk.doc_id}" file="{chunk.source_file}" '
            f'section="{chunk.section}" authority="{chunk.authority}">\n{chunk.content}\n</source>'
        )
    return "\n\n".join(blocks), by_id


def verify_answer(question: str, raw: LLMAnswer, by_id: dict[str, RetrievedChunk], model: str) -> CopilotResponse:
    warnings: list[str] = []
    citations: list[Citation] = []
    for c in raw.citations:
        chunk = by_id.get(c.source_id)
        if chunk is None:
            warnings.append(f"Dropped citation to unknown source {c.source_id!r}")
            continue
        verified = bool(c.quote.strip()) and _normalize(c.quote) in _normalize(chunk.content)
        if not verified:
            warnings.append(f"Quote for {c.source_id} not found verbatim in {chunk.source_file}")
        citations.append(Citation(
            source_id=c.source_id, doc_id=chunk.doc_id, source_file=chunk.source_file,
            section=chunk.section, authority=chunk.authority, chunk_id=chunk.chunk_id,
            quote=c.quote, quote_verified=verified,
        ))

    cited_ids = {c.source_id for c in citations}
    markers = set(_MARKER_RE.findall(raw.answer))
    unknown_markers = sorted(markers - set(by_id))
    if unknown_markers:
        warnings.append(f"Answer references unknown sources: {', '.join(unknown_markers)}")
    uncited_markers = sorted((markers & set(by_id)) - cited_ids)
    if uncited_markers:
        warnings.append(f"Answer markers without a citation entry: {', '.join(uncited_markers)}")

    grounded = raw.insufficient_context or (
        bool(citations) and any(c.quote_verified for c in citations) and not unknown_markers
    )
    return CopilotResponse(
        question=question, answer=raw.answer, citations=citations,
        insufficient_context=raw.insufficient_context,
        confidence=raw.confidence if grounded else "low",
        grounded=grounded, model=model, warnings=warnings,
        sources_considered=[
            {"source_id": sid, "doc_id": ch.doc_id, "section": ch.section, "rrf_score": round(ch.rrf_score, 5)}
            for sid, ch in by_id.items()
        ],
    )


# Errors worth trying the next model for: unknown/retired model, quota, overload, timeout
FALLBACK_STATUS_CODES = {404, 429, 500, 503, 504}


class Copilot:
    ATTEMPTS_PER_MODEL = 3

    def __init__(
        self,
        retriever: HybridRetriever,
        client=None,
        model: str | None = None,
        fallback_models: list[str] | None = None,
        retry_backoff_s: float = 2.0,
    ):
        settings = get_settings()
        self.retriever = retriever
        self.model = model or settings.gemini_chat_model
        if fallback_models is None:
            fallback_models = [m.strip() for m in settings.gemini_chat_fallback_models.split(",") if m.strip()]
        self.models = [self.model] + [m for m in fallback_models if m != self.model]
        self.timeout_ms = settings.gemini_timeout_seconds * 1000
        self.retry_backoff_s = retry_backoff_s
        if client is None:
            from google import genai
            from google.genai import types

            # attempts=1: on failure we move to the next model instead of retrying a stuck one
            client = genai.Client(
                api_key=settings.gemini_api_key.strip(),
                http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
            )
        self._client = client

    def _generate(self, prompt: str):
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=LLMAnswer,
            http_options=types.HttpOptions(timeout=self.timeout_ms),
        )
        failures: list[str] = []
        for model in self.models:
            for attempt in range(self.ATTEMPTS_PER_MODEL):
                try:
                    return model, failures, self._client.models.generate_content(
                        model=model, contents=prompt, config=config
                    )
                except httpx.TimeoutException as exc:  # a hung request: same as a 504, try the next model
                    failures.append(f"{model}: timeout ({type(exc).__name__})")
                    break
                except httpx.TransportError as exc:  # connection reset/refused: retry this model
                    failures.append(f"{model}: {type(exc).__name__}")
                    if attempt == self.ATTEMPTS_PER_MODEL - 1:
                        break
                    time.sleep(self.retry_backoff_s * 2 ** attempt)
                    continue
                except errors.APIError as exc:
                    if exc.code not in FALLBACK_STATUS_CODES:
                        raise
                    failures.append(f"{model}: {exc.code} {exc.status}")
                    # Brief overload/quota blips get a retry; a retired model or a full
                    # timeout goes straight to the next model
                    if exc.code in (404, 504) or attempt == self.ATTEMPTS_PER_MODEL - 1:
                        break
                    time.sleep(self.retry_backoff_s * 2 ** attempt)
        raise RuntimeError(f"All Gemini models failed: {'; '.join(failures)}")

    def ask(self, question: str, top_k: int = 6) -> CopilotResponse:
        chunks = self.retriever.search(question, top_k=top_k)
        if not chunks:
            return CopilotResponse(
                question=question, answer=NO_CONTEXT_ANSWER, citations=[], insufficient_context=True,
                confidence="low", grounded=True, model=self.model,
            )

        context, by_id = build_context(chunks)
        prompt = f"<sources>\n{context}\n</sources>\n\nQuestion: {question}"
        model, failures, resp = self._generate(prompt)
        raw = resp.parsed if isinstance(resp.parsed, LLMAnswer) else LLMAnswer.model_validate_json(resp.text or "")
        result = verify_answer(question, raw, by_id, model)
        result.warnings[:0] = [f"Fell back past {f}" for f in failures]
        return result
