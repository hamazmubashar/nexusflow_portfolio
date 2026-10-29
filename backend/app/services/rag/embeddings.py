"""Embedding providers.

Gemini (default): gemini-embedding-001 truncated to 768 dims via Matryoshka
output_dimensionality. Truncated vectors are not unit-length, so we
L2-normalise them; cosine distance then behaves like the full-size model.

Local fallback: fastembed + BAAI/bge-base-en-v1.5 (768 dims, ONNX, CPU,
runs on Apple Silicon). Install with `pip install fastembed`; the model
(~210 MB) downloads on first use.

Each provider exposes `.name`, which is stored per row in
document_chunks.embedding_model: vectors from different models are never
compared with each other.
"""

from __future__ import annotations

import math
import time
from typing import Protocol, Sequence

from app.core.config import Settings, get_settings


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def l2_normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class GeminiEmbedder:
    BATCH_SIZE = 100  # API limit per embed_content call
    MAX_RETRIES = 5

    def __init__(self, settings: Settings, client=None):
        from google import genai

        self.model = settings.gemini_embedding_model
        self.dim = settings.embedding_dim
        self.name = f"gemini/{self.model}@{self.dim}"
        self._client = client or genai.Client(api_key=settings.gemini_api_key.strip())

    def _embed(self, texts: Sequence[str], task_type: str) -> list[list[float]]:
        from google.genai import errors, types

        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=self.dim)
        out: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH_SIZE):
            batch = list(texts[start : start + self.BATCH_SIZE])
            for attempt in range(self.MAX_RETRIES):
                try:
                    resp = self._client.models.embed_content(model=self.model, contents=batch, config=config)
                    break
                except errors.APIError as exc:
                    # 429 (free-tier quota) and 5xx are transient; anything else is a real error
                    if exc.code not in (429, 500, 503) or attempt == self.MAX_RETRIES - 1:
                        raise
                    time.sleep(2 ** attempt * 2)
            out.extend(l2_normalize(e.values) for e in resp.embeddings)
        return out

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


class LocalEmbedder:
    # bge models expect this instruction on queries (not on passages)
    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, settings: Settings):
        from fastembed import TextEmbedding  # optional dependency

        self._model = TextEmbedding(model_name=settings.local_embedding_model)
        self.dim = settings.embedding_dim
        self.name = f"local/{settings.local_embedding_model}"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [l2_normalize(v.tolist()) for v in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([self.QUERY_PREFIX + text])[0]


def get_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    if settings.embedding_provider == "local":
        return LocalEmbedder(settings)
    if settings.embedding_provider == "gemini":
        if not settings.gemini_api_key.strip():
            raise RuntimeError("GEMINI_API_KEY is empty; set it or use EMBEDDING_PROVIDER=local")
        return GeminiEmbedder(settings)
    raise ValueError(f"Unknown EMBEDDING_PROVIDER: {settings.embedding_provider!r}")
