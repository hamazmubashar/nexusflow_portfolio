"""Central settings, loaded from the repo-root .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_DATA_DIR = REPO_ROOT / "database" / "seed_data"
DOCUMENTS_DIR = REPO_ROOT / "nexusflow_documents"
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg2://nexusflow:change-me-local-only@localhost:5432/nexusflow"
    gemini_api_key: str = ""
    n8n_webhook_url: str = "http://localhost:5678/webhook"
    n8n_db_name: str = "n8n"                      # n8n's database on the same server; read-only telemetry

    # --- RAG (Stage 2) ---
    embedding_provider: str = "gemini"            # gemini | local (fastembed, offline)
    gemini_embedding_model: str = "gemini-embedding-001"
    local_embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_dim: int = 768                      # must match vector(768) in 001_document_chunks.sql
    gemini_chat_model: str = "gemini-3.5-flash"
    # Tried in order when the primary model is unavailable, overloaded or times out
    gemini_chat_fallback_models: str = "gemini-3.8-flash,gemini-flash-latest"
    gemini_timeout_seconds: int = 60
    copilot_engine: str = "langchain"             # langchain | native (same prompt, schema, verification)

    # --- Webhooks / n8n (Stage 3) ---
    webhook_signing_secret: str = ""
    webhook_tolerance_seconds: int = 300
    n8n_dispatch_mode: str = "auto"               # live | mock | auto
    n8n_dispatch_timeout_seconds: float = 10.0
    n8n_dispatch_max_attempts: int = 3

    # --- Governance agent / HITL (Stage 4) ---
    # Separate key for approval tokens; if unset it is derived from WEBHOOK_SIGNING_SECRET
    approval_token_secret: str = ""
    approval_token_ttl_hours: int = 72
    # Base URL the approval card links to (how an approver or n8n reaches this API)
    api_public_base_url: str = "http://localhost:8010"
    governance_approver_role: str = "Finance Director"

    # --- API hardening (Stage 5) ---
    environment: str = "development"              # development | staging | production
    log_format: str = "json"                      # json | text
    log_level: str = "INFO"
    cors_allow_origins: str = "http://localhost:3000,http://localhost:5173"
    # HS256 key for user JWTs; if unset it is derived from WEBHOOK_SIGNING_SECRET (domain-separated)
    jwt_secret: str = ""
    jwt_issuer: str = "nexusflow-autopilot"
    jwt_audience: str = "nexusflow-api"
    jwt_ttl_minutes: int = 480
    health_cache_seconds: int = 60                # throttles the Gemini/n8n probes behind /health

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def psycopg2_dsn(self) -> str:
        """DATABASE_URL without the SQLAlchemy driver suffix, for raw psycopg2."""
        return self.database_url.replace("postgresql+psycopg2://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
