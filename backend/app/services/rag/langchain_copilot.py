"""LangChain (LCEL) composition of the Stage 2 RAG copilot.

    {"question"} ─► RunnablePassthrough.assign(docs = question | HybridSearchRetriever)
                ─► answer_or_refuse:
                     no docs  ─► refusal (the model is never called)
                     docs     ─► build_context ─► ChatPromptTemplate ─► Gemini (with_retry ▸ with_fallbacks)
                                ─► PydanticOutputParser[LLMAnswer] ─► verify_answer ─► CopilotResponse

Same guarantees as the native copilot (app.services.rag.copilot), on purpose: the system prompt,
the numbered <source> blocks, the JSON schema and the citation/quote verification are all reused,
so the two engines are drop-in replacements for each other (COPILOT_ENGINE=langchain|native).
LangChain contributes the composition: a standard BaseRetriever, prompt templates, an output
parser, and declarative retry/fallback policies instead of a hand-written loop.
"""

from __future__ import annotations

from functools import partial
from operator import itemgetter
from typing import Any

import httpx

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompt_values import ChatPromptValue
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableLambda, RunnablePassthrough
from pydantic import ConfigDict

from app.core.config import get_settings
from app.services.rag.copilot import (
    FALLBACK_STATUS_CODES, NO_CONTEXT_ANSWER, SYSTEM_PROMPT, CopilotResponse, LLMAnswer, build_context, verify_answer,
)
from app.services.rag.retriever import HybridRetriever

RETRY_SAME_MODEL = {429, 500, 503}   # brief overload/quota blips
# 404 (retired model) and 504 (full timeout) skip straight to the next model


class TransientLLMError(Exception):
    """Gemini failure worth trying another model for."""


class RetryableLLMError(TransientLLMError):
    """Gemini failure worth retrying on the same model first."""


class HybridSearchRetriever(BaseRetriever):
    """LangChain retriever over our pgvector + full-text RRF search. Documents keep the original chunk."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    hybrid: HybridRetriever
    top_k: int = 6
    doc_types: list[str] | None = None

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        return [
            Document(
                page_content=c.content,
                metadata={"chunk": c, "chunk_id": c.chunk_id, "doc_id": c.doc_id, "section": c.section,
                          "source_file": c.source_file, "authority": c.authority, "rrf_score": c.rrf_score},
            )
            for c in self.hybrid.search(query, top_k=self.top_k, doc_types=self.doc_types)
        ]


PARSER = PydanticOutputParser(pydantic_object=LLMAnswer)
PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT + "\n{format_instructions}"),
    ("human", "<sources>\n{sources}\n</sources>\n\nQuestion: {question}"),
]).partial(format_instructions=PARSER.get_format_instructions())


def _gemini_call(client, model: str, timeout_ms: int, prompt: ChatPromptValue) -> dict:
    """One Gemini call in JSON mode. Returns {"model", "text"}; raises Transient/Retryable errors for fallback."""
    from google.genai import errors, types

    system = "\n\n".join(m.content for m in prompt.messages if m.type == "system")
    human = "\n\n".join(m.content for m in prompt.messages if m.type != "system")
    try:
        resp = client.models.generate_content(
            model=model, contents=human,
            config=types.GenerateContentConfig(
                system_instruction=system, temperature=0.0, response_mime_type="application/json",
                response_schema=LLMAnswer, http_options=types.HttpOptions(timeout=timeout_ms)),
        )
    except httpx.TimeoutException as exc:  # hung request: like a 504, move to the next model
        raise TransientLLMError(f"{model}: timeout ({type(exc).__name__})") from exc
    except httpx.TransportError as exc:    # connection reset/refused: worth retrying this model
        raise RetryableLLMError(f"{model}: {type(exc).__name__}") from exc
    except errors.APIError as exc:
        if exc.code in RETRY_SAME_MODEL:
            raise RetryableLLMError(f"{model}: {exc.code} {exc.status}") from exc
        if exc.code in FALLBACK_STATUS_CODES:
            raise TransientLLMError(f"{model}: {exc.code} {exc.status}") from exc
        raise
    return {"model": model, "text": resp.text or ""}


def gemini_runnable(models: list[str], client=None, timeout_ms: int | None = None, attempts: int = 3) -> Runnable:
    """prompt -> {"model", "text"}: each model retried on blips, then the next model as fallback."""
    settings = get_settings()
    if client is None:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key.strip(),
                              http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)))
    timeout_ms = timeout_ms or settings.gemini_timeout_seconds * 1000

    def one(model: str) -> Runnable:
        return RunnableLambda(partial(_gemini_call, client, model, timeout_ms), name=f"gemini:{model}").with_retry(
            retry_if_exception_type=(RetryableLLMError,), stop_after_attempt=attempts, wait_exponential_jitter=True)

    primary, *rest = models
    chain = one(primary)
    return chain.with_fallbacks([one(m) for m in rest], exceptions_to_handle=(TransientLLMError,)) if rest else chain


class LangChainCopilot:
    """Drop-in replacement for app.services.rag.copilot.Copilot built from LangChain runnables."""

    def __init__(self, retriever: HybridRetriever, llm: Runnable | None = None, models: list[str] | None = None):
        settings = get_settings()
        if models is None:
            fallbacks = [m.strip() for m in settings.gemini_chat_fallback_models.split(",") if m.strip()]
            models = [settings.gemini_chat_model] + [m for m in fallbacks if m != settings.gemini_chat_model]
        self.models = models
        self.hybrid = retriever
        self.retriever = retriever  # same attribute name as the native Copilot
        self.llm = llm or gemini_runnable(models)
        # prompt | llm | parser — the generation half, reusable on its own
        self.answer_chain: Runnable = (
            PROMPT | self.llm
            | RunnableLambda(lambda r: {"model": r["model"], "answer": PARSER.parse(r["text"])}, name="parse_answer")
        )

    def chain(self, top_k: int = 6) -> Runnable:
        """The full LCEL pipeline: {"question": str} -> CopilotResponse."""
        docs = itemgetter("question") | HybridSearchRetriever(hybrid=self.hybrid, top_k=top_k)
        return RunnablePassthrough.assign(docs=docs) | RunnableLambda(self._answer_or_refuse, name="answer_or_refuse")

    def _answer_or_refuse(self, x: dict[str, Any]) -> CopilotResponse:
        question, docs = x["question"], x["docs"]
        if not docs:
            return CopilotResponse(question=question, answer=NO_CONTEXT_ANSWER, citations=[], insufficient_context=True,
                                   confidence="low", grounded=True, model=self.models[0])
        context, by_id = build_context([d.metadata["chunk"] for d in docs])
        try:
            out = self.answer_chain.invoke({"sources": context, "question": question})
        except TransientLLMError as exc:  # every model exhausted
            raise RuntimeError(f"All Gemini models failed: {exc}") from exc
        result = verify_answer(question, out["answer"], by_id, out["model"])
        result.warnings.insert(0, "engine: langchain")
        return result

    def ask(self, question: str, top_k: int = 6) -> CopilotResponse:
        return self.chain(top_k).invoke({"question": question})
