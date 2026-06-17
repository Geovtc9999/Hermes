"""Accès PostgreSQL / pgvector."""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterable

import psycopg
from pgvector.psycopg import register_vector

from .config import settings


@contextmanager
def connect():
    if not settings.db_configured:
        raise RuntimeError("DATABASE_URL non configurée")
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    with connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {settings.pg_table} (
                id           bigserial PRIMARY KEY,
                source_key   text NOT NULL,
                version      text,
                domaine      text,
                chunk_index  int  NOT NULL,
                content      text NOT NULL,
                metadata     jsonb DEFAULT '{{}}'::jsonb,
                embedding    vector({settings.embed_dim}),
                content_sha  text,
                created_at   timestamptz DEFAULT now(),
                UNIQUE (source_key, chunk_index)
            )
            """
        )
        # Colonne lexicale full-text (générée depuis content) — se peuple
        # automatiquement pour les lignes existantes lors de l'ajout.
        conn.execute(
            f"ALTER TABLE {settings.pg_table} ADD COLUMN IF NOT EXISTS tsv tsvector "
            f"GENERATED ALWAYS AS (to_tsvector('{settings.ts_config}', content)) STORED"
        )
        # Index ANN (cosine) + GIN (lexical) + filtres permissions.
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {settings.pg_table}_embed_idx "
            f"ON {settings.pg_table} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {settings.pg_table}_tsv_idx "
            f"ON {settings.pg_table} USING gin (tsv)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {settings.pg_table}_acl_idx "
            f"ON {settings.pg_table} (version, domaine)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {settings.pg_table}_src_idx ON {settings.pg_table} (source_key)"
        )


def upsert_chunks(rows: Iterable[dict]) -> int:
    n = 0
    with connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    f"""
                    INSERT INTO {settings.pg_table}
                        (source_key, version, domaine, chunk_index, content, metadata, embedding, content_sha)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (source_key, chunk_index) DO UPDATE SET
                        content = EXCLUDED.content,
                        metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding,
                        content_sha = EXCLUDED.content_sha
                    """,
                    (
                        r["source_key"], r.get("version"), r.get("domaine"),
                        r["chunk_index"], r["content"], json.dumps(r.get("metadata", {})),
                        r["embedding"], r.get("content_sha"),
                    ),
                )
                n += 1
    return n


def _acl(domains, versions):
    """Construit la clause WHERE de permissions (par domaine/version)."""
    clauses, params = [], []
    if domains is not None:
        clauses.append("domaine = ANY(%s)"); params.append(list(domains))
    if versions is not None:
        clauses.append("version = ANY(%s)"); params.append(list(versions))
    return clauses, params


def _vector_candidates(conn, qvec, k, domains, versions):
    clauses, ap = _acl(domains, versions)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (f"SELECT id, source_key, version, domaine, chunk_index, content, "
           f"1-(embedding<=>%s) AS vscore FROM {settings.pg_table}{where} "
           f"ORDER BY embedding<=>%s LIMIT %s")
    cur = conn.execute(sql, [qvec, *ap, qvec, k])
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _lexical_candidates(conn, qtext, k, domains, versions):
    clauses, ap = _acl(domains, versions)
    tsq = f"websearch_to_tsquery('{settings.ts_config}', %s)"
    conds = [f"tsv @@ {tsq}"] + clauses
    where = " WHERE " + " AND ".join(conds)
    sql = (f"SELECT id, source_key, version, domaine, chunk_index, content, "
           f"ts_rank(tsv, {tsq}) AS lscore FROM {settings.pg_table}{where} "
           f"ORDER BY lscore DESC LIMIT %s")
    # %s ordre : ts_rank(qtext) | WHERE tsv@@(qtext) | clauses ACL | LIMIT
    cur = conn.execute(sql, [qtext, qtext, *ap, k])
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def hybrid_search(qvec, qtext, *, domains=None, versions=None) -> list[dict]:
    """Recherche hybride : vectorielle + lexicale, fusionnées par Reciprocal Rank
    Fusion (RRF). Renvoie jusqu'à `rerank_candidates` chunks (avec scores + méthodes)
    pour reranking en aval. Filtres de permissions appliqués DANS le SQL.
    """
    if domains == [] or versions == []:   # deny explicite -> rien
        return []
    with connect() as conn:
        vec = _vector_candidates(conn, qvec, settings.hybrid_vector_k, domains, versions)
        lex = _lexical_candidates(conn, qtext, settings.hybrid_lexical_k, domains, versions)

    by_id: dict = {}
    K = settings.rrf_k
    for rank, row in enumerate(vec):
        e = by_id.setdefault(row["id"], {**row, "lscore": 0.0, "rrf": 0.0, "methods": set()})
        e["rrf"] += 1.0 / (K + rank + 1); e["methods"].add("vector")
    for rank, row in enumerate(lex):
        e = by_id.get(row["id"])
        if e is None:
            e = by_id.setdefault(row["id"], {**row, "vscore": 0.0, "rrf": 0.0, "methods": set()})
        e["rrf"] += 1.0 / (K + rank + 1); e["methods"].add("lexical"); e["lscore"] = row["lscore"]
    fused = sorted(by_id.values(), key=lambda r: r["rrf"], reverse=True)
    for r in fused:
        r["methods"] = sorted(r["methods"])
    return fused[: settings.rerank_candidates]


def stats() -> dict:
    with connect() as conn:
        total = conn.execute(f"SELECT count(*) FROM {settings.pg_table}").fetchone()[0]
        srcs = conn.execute(
            f"SELECT count(DISTINCT source_key) FROM {settings.pg_table}"
        ).fetchone()[0]
        by_ver = conn.execute(
            f"SELECT version, count(*) FROM {settings.pg_table} GROUP BY version ORDER BY 2 DESC"
        ).fetchall()
        return {"chunks": total, "sources": srcs, "par_version": dict(by_ver)}
