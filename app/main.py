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
    force: bool = False   # True = ré-ingère même les sources déjà indexées


@app.post("/ingest")
def post_ingest(req: IngestReq):
    if not settings.s3_configured:
        raise HTTPException(503, "Stockage objet non configuré")
    if not settings.db_configured:
        raise HTTPException(503, "DB non configurée")
    return ingest(prefix=req.prefix, limit=req.limit, force=req.force)


class QueryReq(BaseModel):
    question: str
    top_k: int | None = None
    role: str | None = None               # permissions : rôle de l'appelant
    domains: list[str] | None = None      # filtre explicite par domaine
    versions: list[str] | None = None     # filtre explicite par version


@app.post("/query")
def post_query(req: QueryReq):
    if not settings.db_configured:
        raise HTTPException(503, "DB non configurée")
    from .rag import query
    return query(req.question, top_k=req.top_k, role=req.role,
                 domains=req.domains, versions=req.versions)


# --------------------------------------------------------------------------- #
# Indexation GOUVERNANCE (nexerp.knowledge_source / knowledge_chunk) + RBAC périmètre
# --------------------------------------------------------------------------- #
class GovIndexReq(BaseModel):
    mode: str = "pilot"                    # "pilot" | "minio"
    keys: list[str] | None = None          # mode=minio : clés bucket explicites
    prefix: str = ""                       # mode=minio : préfixe bucket
    limit: int = 12                        # mode=minio/pilot : nb max de sources
    kind: str = "cegid_doc"                # cegid_doc | donnee_interne | reglementaire | autre
    perimeter_id: str | None = None        # NULL = référentiel global org
    org_id: str | None = None


@app.post("/gov/index")
def post_gov_index(req: GovIndexReq):
    from . import govindex
    if req.mode == "pilot":
        return govindex.pilot(minio_limit=req.limit, minio_prefix=req.prefix)
    if req.mode == "minio":
        if not settings.s3_configured:
            raise HTTPException(503, "Stockage objet non configuré")
        keys = req.keys
        if keys is None:
            keys = [o["key"] for o in storage.list_objects(prefix=req.prefix)][: req.limit]
        out = []
        for k in keys:
            try:
                out.append(govindex.index_from_minio(
                    k, kind=req.kind, perimeter_id=req.perimeter_id, org_id=req.org_id))
            except Exception as e:
                out.append({"key": k, "status": "error", "reason": str(e)[:200]})
        return {"indexés": out, "stats": govindex.stats()}
    raise HTTPException(400, "mode invalide (pilot|minio)")


class GovSearchReq(BaseModel):
    question: str
    perimeters: list[str] | None = None    # None = pas de restriction (service/admin)
    org_id: str | None = None
    kinds: list[str] | None = None
    k: int | None = None


@app.post("/gov/search")
def post_gov_search(req: GovSearchReq):
    from . import govindex
    return govindex.search(req.question, perimeters=req.perimeters,
                           org_id=req.org_id, kinds=req.kinds, k=req.k)


@app.get("/gov/stats")
def get_gov_stats():
    from . import govindex
    return govindex.stats()


# --- Batch RESUMABLE : indexation de TOUT le corpus, en arrière-plan --------- #
class GovReindexReq(BaseModel):
    prefix: str = ""                       # limiter à un préfixe du bucket (optionnel)
    kind: str = "cegid_doc"
    perimeter_id: str | None = None        # NULL = référentiel global org
    org_id: str | None = None
    force: bool = False                    # True = ré-indexe même les docs déjà indexés
    limit: int | None = None               # borne le nb d'objets (tests)
    throttle: float = 0.0                  # pause (s) entre docs -> laisse du CPU au service


@app.post("/gov/reindex")
def post_gov_reindex(req: GovReindexReq):
    """Lance en ARRIÈRE-PLAN l'indexation de tout le corpus MinIO (retour immédiat).
    Suivre via GET /gov/reindex/status ; resumable (relancer reprend l'existant)."""
    import threading

    from . import govindex
    if not settings.s3_configured:
        raise HTTPException(503, "Stockage objet non configuré")
    if govindex._reindex_state["running"]:
        raise HTTPException(409, "Un batch d'indexation est déjà en cours")

    def _run():
        try:
            govindex.reindex_corpus(
                prefix=req.prefix, kind=req.kind, perimeter_id=req.perimeter_id,
                org_id=req.org_id, skip_indexed=not req.force,
                limit=req.limit, throttle=req.throttle)
        except Exception as e:  # pragma: no cover
            log.exception("reindex_corpus a échoué: %s", e)

    threading.Thread(target=_run, name="gov-reindex", daemon=True).start()
    return {"started": True, "status": govindex.reindex_status()}


@app.get("/gov/reindex/status")
def get_gov_reindex_status():
    from . import govindex
    return govindex.reindex_status()


@app.post("/gov/reindex/cancel")
def post_gov_reindex_cancel():
    """Demande l'arrêt propre du batch après le document en cours."""
    from . import govindex
    govindex._reindex_state["cancel"] = True
    return {"cancel_requested": True, "status": govindex.reindex_status()}


@app.on_event("startup")
def _startup():
    log.info("Hermes %s — démarrage", settings.app_version)
    # Préchargement NON bloquant du modèle d'embeddings (téléchargement ONNX ~1 Go au
    # 1er boot) dans un thread : /health reste disponible immédiatement.
    import threading

    def _warm():
        try:
            dim = embeddings.warmup()
            log.info("Embeddings prêts (%s, dim=%s)", settings.embed_model, dim)
        except Exception as e:  # le modèle se chargera paresseusement au 1er appel
            log.warning("Préchargement embeddings différé: %s", e)

    threading.Thread(target=_warm, daemon=True).start()
