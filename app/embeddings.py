"""Embeddings locaux via fastembed (ONNX, multilingue, sans clé externe).

Les modèles e5 attendent des préfixes 'passage:' / 'query:'.
"""
from __future__ import annotations

import os
from functools import lru_cache

from .config import settings


@lru_cache(maxsize=1)
def _model():
    os.environ.setdefault("FASTEMBED_CACHE_PATH", settings.embed_cache_dir)
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=settings.embed_model, cache_dir=settings.embed_cache_dir)


def _is_e5() -> bool:
    return "e5" in settings.embed_model.lower()


def embed_passages(texts: list[str]) -> list[list[float]]:
    if _is_e5():
        texts = [f"passage: {t}" for t in texts]
    return [list(v) for v in _model().embed(texts)]


def embed_query(text: str) -> list[float]:
    if _is_e5():
        text = f"query: {text}"
    return list(next(iter(_model().embed([text]))))


def warmup() -> int:
    """Force le téléchargement/chargement du modèle ; renvoie la dimension."""
    v = embed_query("warmup")
    return len(v)
