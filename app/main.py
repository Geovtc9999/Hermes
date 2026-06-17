"""Hermes — API FastAPI (RAG CEGID)."""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db, embeddings, obs, storage
from .config import settings
from .ingest import ingest

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("hermes")

app = FastAPI(title=settings.app_name, version=settings.app_version)


@app.get("/health")
def health():
    """Liveness — toujours 200 si le process tourne."""
    return {"status": "ok", "service": settings.app_name, "version": settings.app_version}


@app.get("/ready")
def ready():
    """Readiness — état des dépendances (DB / S3 / Langfuse / embeddings)."""
    checks = {
        "db_configured": settings.db_configured,
        "s3_configured": settings.s3_configured,
        "langfuse": obs.healthcheck(),
        "llm_configured": settings.llm_configured,
        "embed_model": settings.embed_model,
    }
    # tests de connectivité best-effort
    try:
        if settings.db_configured:
            db.init_schema(); checks["db_ok"] = True
    except Exception as e:
        checks["db_ok"] = False; checks["db_error"] = str(e)[:200]
    try:
        if settings.s3_configured:
            checks["bucket"] = settings.s3_bucket
            checks["bucket_ok"] = storage.bucket_exists()
    except Exception as e:
        checks["bucket_ok"] = False; checks["s3_error"] = str(e)[:200]
    return checks


@app.get("/stats")
def get_stats():
    if not settings.db_configured:
        raise HTTPException(503, "DB non configurée")
    return db.stats()


class IngestReq(BaseModel):
    prefix: str = ""
    limit: int | None = None


@app.post("/ingest")
def post_ingest(req: IngestReq):
    if not settings.s3_configured:
        raise HTTPException(503, "Stockage objet non configuré")
    if not settings.db_configured:
        raise HTTPException(503, "DB non configurée")
    return ingest(prefix=req.prefix, limit=req.limit)


class QueryReq(BaseModel):
    question: str
    top_k: int | None = None


@app.post("/query")
def post_query(req: QueryReq):
    if not settings.db_configured:
        raise HTTPException(503, "DB non configurée")
    from .rag import query
    return query(req.question, top_k=req.top_k)


@app.on_event("startup")
def _startup():
    log.info("Hermes %s — démarrage", settings.app_version)
    # Préchargement du modèle d'embeddings (téléchargement ONNX au 1er boot).
    try:
        dim = embeddings.warmup()
        log.info("Embeddings prêts (%s, dim=%s)", settings.embed_model, dim)
    except Exception as e:
        log.warning("Embeddings non préchargés: %s", e)
