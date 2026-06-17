# Hermes — service RAG CEGID (NEXERP IA Factory)

Couche **02 · RAG CEGID** de l'IA Factory : ingestion de la documentation CEGID
(bucket `cegid-sources`) → embeddings → **PostgreSQL/pgvector** → retrieval cité,
le tout **instrumenté par Langfuse**.

## Architecture (v1)

```
cegid-sources (MinIO) ──▶ extraction (pdf/docx/pptx/html/txt)
                          └─▶ chunking ─▶ embeddings (fastembed, local, multilingue)
                                          └─▶ pgvector (postgres18-recette)
/query ──▶ embed question ─▶ recherche cosinus pgvector ─▶ extraits cités
                                          └─▶ (option) réponse Claude citée
Toutes les opérations sont tracées dans Langfuse (coût/latence) si configuré.
```

Choix v1 : **embeddings locaux** (fastembed / ONNX, modèle multilingue e5) → aucune
clé externe nécessaire pour l'ingestion et le retrieval. La **réponse LLM (Claude)**
et **Langfuse** s'activent automatiquement dès que leurs clés sont fournies, sinon
dégradation propre (les extraits cités sont renvoyés).

## Endpoints

| Méthode | Route | Rôle |
|---|---|---|
| GET  | `/health` | Liveness |
| GET  | `/ready`  | État des dépendances (DB, S3, Langfuse, embeddings) |
| GET  | `/stats`  | Nb de chunks / sources / par version |
| POST | `/ingest` | `{ "prefix": "v11/", "limit": 50 }` — ingère le corpus (ou un sous-ensemble) |
| POST | `/query`  | `{ "question": "…", "top_k": 6 }` — réponse citée |

## Configuration

Voir [`.env.example`](.env.example). Variables clés : `DATABASE_URL`,
`S3_ENDPOINT`/`S3_ACCESS_KEY`/`S3_SECRET_KEY`/`S3_BUCKET`, `EMBED_MODEL`/`EMBED_DIM`,
`ANTHROPIC_API_KEY` (option), `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST` (option).

## Déploiement

Image Docker (voir `Dockerfile`). Sur Coolify : application Git, réseau `recette`
(pour joindre Postgres, MinIO et Langfuse), port interne `8000`. Volume persistant
recommandé sur `/data` (cache du modèle d'embeddings).

## Limites v1

- Types ingérés : `pdf, docx, pptx, txt, md, csv, html, xml`. **Non gérés** : `.chm`,
  `.doc` (binaire), médias — à ajouter en v2.
- Retrieval vectoriel pur (cosinus) ; le retrieval hybride (BM25 + reranking) est prévu en v2.

---
NEXERP — IA Factory · `11-Agents-AI/Hermes`
