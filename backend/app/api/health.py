"""GET /health: unauthenticated liveness + dependency status (no secrets, no business data).

200 when PostgreSQL is reachable ("ok", or "degraded" if n8n/Gemini/pgvector is not);
503 "down" when the database is unreachable. The n8n and Gemini probes are network calls,
so their results are cached for HEALTH_CACHE_SECONDS; a Gemini probe is a models.get on
the embedding model (no tokens consumed).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
import psycopg2
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.db import get_conn
from app.schemas.analytics import HealthResponse

router = APIRouter(tags=["system"])
STARTED = time.monotonic()
_cache: dict[str, tuple[float, dict]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timed(fn):
    t = time.perf_counter()
    result = fn()
    return result, round((time.perf_counter() - t) * 1000, 1)


def check_database() -> tuple[dict, dict]:
    try:
        def probe():
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                ext = cur.fetchone()
                chunks = {}
                if ext:
                    cur.execute("SELECT to_regclass('document_chunks') IS NOT NULL AS ok")
                    if cur.fetchone()["ok"]:
                        cur.execute("SELECT embedding_model, count(*) AS n FROM document_chunks GROUP BY 1")
                        chunks = {r["embedding_model"]: r["n"] for r in cur.fetchall()}
                return ext, chunks
        (ext, chunks), ms = _timed(probe)
    except psycopg2.Error as exc:
        down = {"status": "down", "detail": type(exc).__name__, "checked_at": _now()}
        return down, {**down, "detail": "database unreachable"}
    db = {"status": "ok", "latency_ms": ms, "checked_at": _now()}
    if ext is None:
        vec = {"status": "down", "detail": "pgvector extension not installed", "checked_at": _now()}
    else:
        vec = {"status": "ok" if chunks else "degraded", "extension_version": ext["extversion"],
               "indexed_chunks": chunks, "detail": None if chunks else "no document chunks indexed",
               "checked_at": _now()}
    return db, vec


async def _cached(key: str, probe) -> dict:
    ttl = get_settings().health_cache_seconds
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    result = await probe()
    _cache[key] = (time.monotonic(), result)
    return result


async def check_n8n() -> dict:
    parts = urlsplit(get_settings().n8n_webhook_url)
    t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            ok = (await client.get(f"{parts.scheme}://{parts.netloc}/healthz")).is_success
        status, detail = ("ok", None) if ok else ("down", "healthz returned an error")
    except httpx.HTTPError as exc:
        status, detail = "down", type(exc).__name__
    return {"status": status, "latency_ms": round((time.perf_counter() - t) * 1000, 1), "detail": detail,
            "checked_at": _now()}


async def check_gemini() -> dict:
    s = get_settings()
    if not s.gemini_api_key.strip():
        return {"status": "not_configured", "detail": "GEMINI_API_KEY is empty", "checked_at": _now()}

    def probe():
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=s.gemini_api_key.strip(),
                              http_options=types.HttpOptions(timeout=5000,
                                                             retry_options=types.HttpRetryOptions(attempts=1)))
        client.models.get(model=s.gemini_embedding_model)

    t = time.perf_counter()
    try:
        await asyncio.to_thread(probe)
        status, detail = "ok", None
    except Exception as exc:  # any SDK/network error means "can't use Gemini right now"
        status, detail = "down", f"{type(exc).__name__}"
    return {"status": status, "latency_ms": round((time.perf_counter() - t) * 1000, 1), "detail": detail,
            "checked_at": _now()}


@router.get("/health", response_model=HealthResponse,
            responses={503: {"model": HealthResponse, "description": "Database unreachable"}})
async def health(request: Request):
    db, vec = await asyncio.to_thread(check_database)
    n8n, gemini = await asyncio.gather(_cached("n8n", check_n8n), _cached("gemini", check_gemini))
    if db["status"] != "ok":
        overall = "down"
    elif all(c["status"] in ("ok", "not_configured") for c in (vec, n8n, gemini)) and gemini["status"] == "ok":
        overall = "ok"
    else:
        overall = "degraded"
    body = HealthResponse(
        status=overall, version=request.app.version, environment=get_settings().environment,
        uptime_seconds=round(time.monotonic() - STARTED, 1),
        dispatch_mode=getattr(request.app.state, "dispatcher", None).mode
        if getattr(request.app.state, "dispatcher", None) else "uninitialized",
        components={"database": db, "pgvector": vec, "n8n": n8n, "gemini": gemini},
    )
    return JSONResponse(status_code=503 if overall == "down" else 200, content=body.model_dump(mode="json"))
