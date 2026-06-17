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
        # Index ANN (cosine). ivfflat nécessite des données pour être efficace ;
        # créé tôt, il reste valide.
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {settings.pg_table}_embed_idx "
            f"ON {settings.pg_table} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
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


def search(query_embedding, top_k: int = 6) -> list[dict]:
    with connect() as conn:
        cur = conn.execute(
            f"""
            SELECT source_key, version, domaine, chunk_index, content, metadata,
                   1 - (embedding <=> %s) AS score
            FROM {settings.pg_table}
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (query_embedding, query_embedding, top_k),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


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
