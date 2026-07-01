"""Hermes — configuration (chargée depuis l'environnement)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Service ---
    app_name: str = "Hermes RAG CEGID"
    app_version: str = "0.3.0"
    log_level: str = "INFO"

    # --- PostgreSQL / pgvector ---
    # Ex: postgresql://postgres:****@postgres18-recette:5432/postgres
    database_url: str | None = Field(default=None)
    pg_table: str = "hermes_chunks"

    # --- Base GOUVERNANCE (schéma nexerp : knowledge_source / knowledge_chunk) ---
    # Si absent, dérivée de database_url en remplaçant le nom de base par gov_db_name.
    nexerp_database_url: str | None = Field(default=None)
    gov_db_name: str = "nexerp"

    # --- Embeddings (fastembed, local, pas de clé externe) ---
    embed_model: str = "intfloat/multilingual-e5-large"
    embed_dim: int = 1024  # multilingual-e5-large -> 1024 ; ajuster si on change de modèle
    embed_cache_dir: str = "/data/models"

    # --- Stockage objet (MinIO / S3) : corpus cegid-sources ---
    s3_endpoint: str | None = None          # ex: minio-xxxx:9000 (sans schéma)
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_secure: bool = False
    s3_bucket: str = "cegid-sources"

    # --- Ingestion ---
    chunk_size: int = 1200      # caractères
    chunk_overlap: int = 200
    ingest_batch: int = 64

    # --- Recherche hybride (vectorielle + lexicale) ---
    hybrid_vector_k: int = 30      # candidats vectoriels
    hybrid_lexical_k: int = 30     # candidats lexicaux (BM25/ts_rank)
    rrf_k: int = 60                # constante Reciprocal Rank Fusion
    ts_config: str = "french"      # config full-text PostgreSQL

    # --- Reranking (cross-encoder, optionnel, dégradé en RRF si indispo) ---
    rerank_enabled: bool = True
    rerank_model: str = "jinaai/jina-reranker-v2-base-multilingual"
    rerank_candidates: int = 20    # nb de candidats fusionnés à reranker

    # --- Permissions au retrieval (par rôle / domaine) ---
    # JSON: {"role": {"domains": [...], "versions": [...]}}  (listes = autorisé ; absent = tout)
    role_policy: str = "{}"
    permissions_default: str = "allow"  # "allow" ou "deny" pour un rôle hors policy

    # --- LLM réponse (Claude) — optionnel ---
    anthropic_api_key: str | None = None
    answer_model: str = "claude-opus-4-8"
    top_k: int = 6

    # --- Observabilité Langfuse — optionnel ---
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None  # ex: http://langfuse-xxxx:3000

    @property
    def s3_configured(self) -> bool:
        return bool(self.s3_endpoint and self.s3_access_key and self.s3_secret_key)

    @property
    def db_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def llm_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def langfuse_configured(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key and self.langfuse_host)

    @property
    def role_policy_parsed(self) -> dict:
        import json
        try:
            return json.loads(self.role_policy) or {}
        except Exception:
            return {}


settings = Settings()
