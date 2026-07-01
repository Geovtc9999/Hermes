"""Indexation GOUVERNANCE : sources -> nexerp.knowledge_source / knowledge_chunk.

Distinct du store interne de Hermès (`hermes_chunks`) : ce module alimente le
schéma gouverné du produit (RBAC par `perimeter_id`, index HNSW cosinus) et fournit
une recherche sémantique **sourcée** (citations = title/uri de la knowledge_source).

Contrainte : réutilise le MÊME modèle d'embedding e5-1024 que Hermès
(`embeddings.embed_passages` / `embed_query`) pour que les vecteurs d'index et de
requête soient comparables (sinon le KNN cosinus n'a aucun sens).

Cloisonnement : `cegid_doc` = référentiel global de l'org (`perimeter_id IS NULL`,
lisible par tous) ; `donnee_interne` = scopée à un périmètre. La recherche filtre
`perimeter_id IS NULL OR perimeter_id = ANY(<périmètres autorisés>)` DANS le SQL —
un fragment non autorisé n'est jamais ni récupéré ni cité.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector

from . import embeddings, storage
from .config import settings
from .extract import derive_meta, extract_text, supported
from .obs import observe

KNOWLEDGE_KINDS = {"cegid_doc", "donnee_interne", "reglementaire", "autre"}


# --------------------------------------------------------------------------- #
# Connexion à la base GOUVERNANCE (schéma `nexerp`)
# --------------------------------------------------------------------------- #
def _gov_url() -> str:
    """URL de la base gouvernance. `NEXERP_DATABASE_URL` explicite si fournie, sinon
    dérivée de la `DATABASE_URL` de Hermès en remplaçant le nom de base par
    `gov_db_name` (même cluster pgvector `postgres18-recette`)."""
    if getattr(settings, "nexerp_database_url", None):
        return settings.nexerp_database_url  # type: ignore[return-value]
    base = settings.database_url or ""
    if not base:
        return ""
    db_name = getattr(settings, "gov_db_name", "nexerp")
    # remplace le dernier segment de path (nom de base) avant un éventuel '?'
    return re.sub(r"/[^/?]+(\?|$)", f"/{db_name}\\1", base, count=1)


@contextmanager
def _connect():
    url = _gov_url()
    if not url:
        raise RuntimeError("Base gouvernance non configurée (NEXERP_DATABASE_URL / DATABASE_URL)")
    conn = psycopg.connect(url, autocommit=True)
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("SET search_path TO nexerp, public")
        register_vector(conn)
        yield conn
    finally:
        conn.close()


def _chunk(text: str) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    size, overlap = settings.chunk_size, settings.chunk_overlap
    out, i = [], 0
    while i < len(text):
        out.append(text[i : i + size])
        i += max(1, size - overlap)
    return out


def _vlit(qvec) -> str:
    return "[" + ",".join(str(float(x)) for x in qvec) + "]"


def _default_org(conn) -> str:
    row = conn.execute(
        "SELECT id FROM nexerp.organization ORDER BY created_at LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("Aucune organisation dans nexerp.organization")
    return row[0]


def default_org_id() -> str:
    with _connect() as conn:
        return str(_default_org(conn))


def first_perimeter(conn) -> str | None:
    row = conn.execute(
        "SELECT id FROM nexerp.perimeter ORDER BY created_at LIMIT 1"
    ).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# Indexation
# --------------------------------------------------------------------------- #
def index_document(
    *,
    title: str,
    uri: str,
    kind: str,
    perimeter_id: str | None,
    text: str,
    org_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Chunk + embed (e5 `passage:`) + upsert `knowledge_source` (+ ses `knowledge_chunk`).
    Idempotent par (org_id, uri) : ré-indexer remplace les chunks de la source."""
    if kind not in KNOWLEDGE_KINDS:
        raise ValueError(f"kind invalide: {kind} (attendu {sorted(KNOWLEDGE_KINDS)})")
    chunks = _chunk(text)
    if not chunks:
        return {"title": title, "uri": uri, "status": "skip", "reason": "texte-vide", "chunks": 0}
    vectors = embeddings.embed_passages(chunks)
    with _connect() as conn:
        oid = org_id or _default_org(conn)
        row = conn.execute(
            "SELECT id FROM nexerp.knowledge_source WHERE org_id=%s AND uri=%s",
            (oid, uri),
        ).fetchone()
        if row:
            sid = row[0]
            conn.execute("DELETE FROM nexerp.knowledge_chunk WHERE source_id=%s", (sid,))
            conn.execute(
                "UPDATE nexerp.knowledge_source SET title=%s, kind=%s, perimeter_id=%s, indexed_at=now() WHERE id=%s",
                (title, kind, perimeter_id, sid),
            )
        else:
            sid = conn.execute(
                "INSERT INTO nexerp.knowledge_source (org_id, kind, title, uri, perimeter_id, indexed_at) "
                "VALUES (%s,%s,%s,%s,%s, now()) RETURNING id",
                (oid, kind, title, uri, perimeter_id),
            ).fetchone()[0]
        with conn.cursor() as cur:
            for idx, (c, v) in enumerate(zip(chunks, vectors)):
                meta = {
                    **(metadata or {}),
                    "chunk_index": idx,
                    "content_sha": hashlib.sha256(c.encode("utf-8")).hexdigest(),
                    "title": title,
                    "uri": uri,
                }
                cur.execute(
                    "INSERT INTO nexerp.knowledge_chunk (source_id, content, embedding, metadata) "
                    "VALUES (%s,%s,%s,%s)",
                    (sid, c, v, json.dumps(meta, ensure_ascii=False)),
                )
    return {
        "title": title,
        "uri": uri,
        "kind": kind,
        "perimeter_id": str(perimeter_id) if perimeter_id else None,
        "status": "ok",
        "chunks": len(chunks),
    }


def index_from_minio(
    key: str,
    *,
    kind: str = "cegid_doc",
    perimeter_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Indexe un objet du bucket `cegid-sources` dans la base gouvernance."""
    if not supported(key):
        return {"key": key, "status": "skip", "reason": "type-non-supporte", "chunks": 0}
    data = storage.get_bytes(key)
    text = extract_text(key, data)
    version, domaine = derive_meta(key)
    r = index_document(
        title=key.split("/")[-1],
        uri=f"s3://{settings.s3_bucket}/{key}",
        kind=kind,
        perimeter_id=perimeter_id,
        text=text,
        org_id=org_id,
        metadata={"source_key": key, "version": version, "domaine": domaine},
    )
    r["key"] = key
    return r


# --------------------------------------------------------------------------- #
# Recherche sémantique (KNN cosinus HNSW) — RBAC par périmètre
# --------------------------------------------------------------------------- #
def search(
    question: str,
    *,
    perimeters: list[str] | None = None,
    org_id: str | None = None,
    kinds: list[str] | None = None,
    k: int | None = None,
) -> dict:
    """Recherche KNN cosinus (index HNSW) sur `knowledge_chunk`, cloisonnée par périmètre.

    - `perimeters=None`  -> aucune restriction de périmètre (service/admin).
    - `perimeters=[...]`  -> `perimeter_id IS NULL` (global) OU ∈ liste.
    Renvoie des fragments **sourcés** (title/uri/kind) = citations.
    """
    k = k or settings.top_k
    qv = embeddings.embed_query(question)
    v = _vlit(qv)

    where = ["kc.embedding IS NOT NULL"]
    params: list = []
    if org_id is not None:
        where.append("ks.org_id = %s")
        params.append(org_id)
    if perimeters is not None:
        where.append("(ks.perimeter_id IS NULL OR ks.perimeter_id = ANY(%s))")
        params.append(list(perimeters))
    if kinds:
        where.append("ks.kind = ANY(%s)")
        params.append(list(kinds))

    sql = (
        "SELECT kc.content, ks.title, ks.uri, ks.kind::text AS kind, "
        "ks.perimeter_id::text AS perimeter_id, kc.metadata, "
        f"1 - (kc.embedding <=> %s::vector) AS score "
        "FROM nexerp.knowledge_chunk kc "
        "JOIN nexerp.knowledge_source ks ON ks.id = kc.source_id "
        "WHERE " + " AND ".join(where) + " "
        # KNN cosinus via l'index HNSW idx_chunk_embedding (vector_cosine_ops)
        "ORDER BY kc.embedding <=> %s::vector LIMIT %s"
    )
    with observe(name="hermes.gov_search", metadata={"k": k, "perimetres": perimeters, "kinds": kinds}):
        with _connect() as conn:
            cur = conn.execute(sql, [v, *params, v, k])
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    fragments = [
        {
            "content": r["content"],
            "score": round(float(r["score"]), 4),
            "source": {
                "title": r["title"],
                "uri": r["uri"],
                "kind": r["kind"],
                "perimeter_id": r["perimeter_id"],
                "domaine": (r["metadata"] or {}).get("domaine"),
                "version": (r["metadata"] or {}).get("version"),
            },
        }
        for r in rows
    ]
    citations = sorted({f["source"]["uri"] or f["source"]["title"] for f in fragments})
    return {"question": question, "k": k, "fragments": fragments, "citations": citations}


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def stats() -> dict:
    with _connect() as conn:
        chunks = conn.execute("SELECT count(*) FROM nexerp.knowledge_chunk").fetchone()[0]
        sources = conn.execute("SELECT count(*) FROM nexerp.knowledge_source").fetchone()[0]
        by_kind = conn.execute(
            "SELECT kind::text, count(*) FROM nexerp.knowledge_source GROUP BY kind ORDER BY 2 DESC"
        ).fetchall()
        scoped = conn.execute(
            "SELECT count(*) FROM nexerp.knowledge_source WHERE perimeter_id IS NOT NULL"
        ).fetchone()[0]
    return {
        "knowledge_chunk": chunks,
        "knowledge_source": sources,
        "sources_par_kind": dict(by_kind),
        "sources_scopées_périmètre": scoped,
    }


# --------------------------------------------------------------------------- #
# Indexation pilote
# --------------------------------------------------------------------------- #
_INTERNE_PILOTE = (
    "Procédure interne NEXERP — Entité test. Rapprochement bancaire mensuel : "
    "à la clôture, l'opérateur exporte le journal de banque depuis CEGID Retail, "
    "pointe les écritures avec le relevé, et signale tout écart supérieur à 50 € au "
    "contrôleur de gestion via le portail. Le seuil d'alerte interne est fixé à 50 € "
    "(paramètre confidentiel propre à l'Entité test, non diffusé aux autres périmètres). "
    "Les caisses sont clôturées quotidiennement et la remise en banque est effectuée "
    "sous 24 h ouvrées."
)


def pilot(*, minio_limit: int = 12, minio_prefix: str = "") -> dict:
    """Indexation pilote :
      1. Corpus CEGID depuis MinIO -> `cegid_doc`, périmètre NULL (référentiel global org).
      2. Un échantillon `donnee_interne` scopé au 1er périmètre -> démontre le cloisonnement.
    Puis une recherche de démonstration + un test RBAC (autre périmètre ne voit pas l'interne).
    """
    report: dict = {"cegid": [], "interne": None, "demo": None, "rbac_test": None}
    with _connect() as conn:
        org_id = str(_default_org(conn))
        perim = first_perimeter(conn)
    report["org_id"] = org_id
    report["perimetre_pilote"] = perim

    # 1) Doc CEGID (global org)
    objects = [o for o in storage.list_objects(prefix=minio_prefix) if supported(o["key"])]
    objects = objects[:minio_limit]
    with observe(name="hermes.gov_pilot", metadata={"candidats": len(objects)}):
        for o in objects:
            try:
                r = index_from_minio(o["key"], kind="cegid_doc", perimeter_id=None, org_id=org_id)
            except Exception as e:  # robustesse : un fichier KO ne bloque pas le lot
                r = {"key": o["key"], "status": "error", "reason": str(e)[:200], "chunks": 0}
            report["cegid"].append(r)

    # 2) Donnée interne scopée (démo RBAC)
    if perim:
        report["interne"] = index_document(
            title="Procédure interne — Rapprochement bancaire (Entité test)",
            uri="internal://entite-test/procedures/rapprochement-bancaire",
            kind="donnee_interne",
            perimeter_id=perim,
            text=_INTERNE_PILOTE,
            org_id=org_id,
            metadata={"confidentialite": "interne", "domaine": "procedures-internes"},
        )

    # 3) Démo recherche (accès global CEGID)
    report["demo"] = search(
        "Comment clôturer une caisse et faire le rapprochement comptable ?",
        perimeters=[perim] if perim else None,
        org_id=org_id,
        k=5,
    )
    # 4) Test RBAC : un périmètre BIDON ne doit PAS voir la donnée interne scopée
    report["rbac_test"] = search(
        "seuil d'alerte rapprochement bancaire Entité test",
        perimeters=["00000000-0000-0000-0000-000000000000"],  # périmètre inexistant
        org_id=org_id,
        k=5,
    )
    report["stats"] = stats()
    return report
