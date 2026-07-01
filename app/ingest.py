"""Pipeline d'ingestion : cegid-sources -> extraction -> chunking -> embeddings -> pgvector."""
from __future__ import annotations

import hashlib

from . import db, embeddings, storage
from .config import settings
from .extract import derive_meta, extract_text, supported
from .obs import observe


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


def ingest_one(key: str) -> dict:
    if not supported(key):
        return {"key": key, "status": "skip", "reason": "type-non-supporte", "chunks": 0}
    data = storage.get_bytes(key)
    text = extract_text(key, data)
    chunks = _chunk(text)
    if not chunks:
        return {"key": key, "status": "skip", "reason": "texte-vide", "chunks": 0}
    version, domaine = derive_meta(key)
    vectors = embeddings.embed_passages(chunks)
    rows = []
    for idx, (c, v) in enumerate(zip(chunks, vectors)):
        rows.append({
            "source_key": key, "version": version, "domaine": domaine,
            "chunk_index": idx, "content": c, "embedding": v,
            "content_sha": hashlib.sha256(c.encode("utf-8")).hexdigest(),
            "metadata": {"version": version, "domaine": domaine},
        })
    n = db.upsert_chunks(rows)
    return {"key": key, "status": "ok", "chunks": n, "version": version}


def ingest_bytes(
    key: str,
    data: bytes,
    *,
    version: str | None = None,
    domaine: str | None = None,
    force: bool = True,
) -> dict:
    """Ingère un blob en mémoire (sans lecture MinIO)."""
    if not supported(key):
        return {"key": key, "status": "skip", "reason": "type-non-supporte", "chunks": 0}
    if not force and key in db.existing_sources():
        return {"key": key, "status": "skip", "reason": "deja-indexe", "chunks": 0}
    text = extract_text(key, data)
    chunks = _chunk(text)
    if not chunks:
        return {"key": key, "status": "skip", "reason": "texte-vide", "chunks": 0}
    if version is None or domaine is None:
        dv, dd = derive_meta(key)
        version = version or dv
        domaine = domaine or dd
    vectors = embeddings.embed_passages(chunks)
    rows = []
    for idx, (c, v) in enumerate(zip(chunks, vectors)):
        rows.append({
            "source_key": key, "version": version, "domaine": domaine,
            "chunk_index": idx, "content": c, "embedding": v,
            "content_sha": hashlib.sha256(c.encode("utf-8")).hexdigest(),
            "metadata": {"version": version, "domaine": domaine},
        })
    n = db.upsert_chunks(rows)
    return {"key": key, "status": "ok", "chunks": n, "version": version, "domaine": domaine}


def ingest_file(local_path: str, key: str | None = None, **kwargs) -> dict:
    """Ingère un fichier local ; `key` = clé bucket (défaut : v11/specs/<basename>)."""
    import os
    key = key or f"v11/specs/{os.path.basename(local_path)}"
    with open(local_path, "rb") as f:
        data = f.read()
    return ingest_bytes(key, data, **kwargs)


def ingest(prefix: str = "", limit: int | None = None, force: bool = False) -> dict:
    """Ingère le corpus (ou un sous-ensemble). Reprenable : par défaut, les sources
    déjà indexées sont sautées (force=True pour tout ré-ingérer). Tracé Langfuse."""
    db.init_schema()
    objects = storage.list_objects(prefix=prefix)
    objects = [o for o in objects if supported(o["key"])]
    deja = 0
    if not force:
        done = db.existing_sources()
        before = len(objects)
        objects = [o for o in objects if o["key"] not in done]
        deja = before - len(objects)
    if limit:
        objects = objects[:limit]

    with observe(name="hermes.ingest", metadata={"prefix": prefix, "candidats": len(objects), "deja_indexes": deja}):
        ok = skip = err = total_chunks = 0
        details = []
        for o in objects:
            try:
                r = ingest_one(o["key"])
            except Exception as e:  # robustesse : un fichier KO ne bloque pas le lot
                r = {"key": o["key"], "status": "error", "reason": str(e)[:200], "chunks": 0}
            details.append(r)
            if r["status"] == "ok":
                ok += 1; total_chunks += r["chunks"]
            elif r["status"] == "skip":
                skip += 1
            else:
                err += 1
    return {"candidats": len(objects), "deja_indexes": deja, "ingerés": ok,
            "ignorés": skip, "erreurs": err, "chunks": total_chunks, "details": details[:50]}
