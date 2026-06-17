"""Reranking par cross-encoder (fastembed TextCrossEncoder), optionnel.

Si le modèle n'est pas disponible / désactivé, on renvoie les candidats dans
l'ordre d'entrée (déjà fusionnés par RRF) : dégradation propre, jamais d'erreur.
"""
from __future__ import annotations

from functools import lru_cache

from .config import settings


@lru_cache(maxsize=1)
def _encoder():
    if not settings.rerank_enabled:
        return None
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        return TextCrossEncoder(model_name=settings.rerank_model, cache_dir=settings.embed_cache_dir)
    except Exception:
        return None


def rerank(query: str, candidates: list[dict], top_k: int) -> tuple[list[dict], str]:
    """Réordonne `candidates` (dicts avec clé 'content') par pertinence au `query`.

    Renvoie (liste_top_k, méthode) où méthode ∈ {"cross-encoder", "rrf"}.
    """
    enc = _encoder()
    if enc is None or not candidates:
        return candidates[:top_k], "rrf"
    try:
        scores = list(enc.rerank(query, [c["content"] for c in candidates]))
        for c, s in zip(candidates, scores):
            c["rerank_score"] = float(s)
        ordered = sorted(candidates, key=lambda c: c["rerank_score"], reverse=True)
        return ordered[:top_k], "cross-encoder"
    except Exception:
        return candidates[:top_k], "rrf"
