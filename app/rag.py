"""RAG : retrieval pgvector (+ réponse Claude citée, optionnelle) + trace Langfuse."""
from __future__ import annotations

from . import db, embeddings, perms, rerank
from .config import settings
from .obs import log_generation, observe

SYSTEM = (
    "Tu es Hermes, l'assistant RAG de NEXERP sur la documentation CEGID Retail. "
    "Réponds uniquement à partir des EXTRAITS fournis. Cite tes sources avec [n] "
    "renvoyant au numéro de l'extrait. Si l'information n'est pas dans les extraits, "
    "dis-le clairement. Réponds en français, de façon concise et précise."
)


def _format_context(passages: list[dict]) -> str:
    blocs = []
    for i, p in enumerate(passages, 1):
        src = p["source_key"]
        ver = p.get("version") or "?"
        blocs.append(f"[{i}] (source: {src} — version: {ver})\n{p['content']}")
    return "\n\n".join(blocs)


def _answer_with_claude(question: str, passages: list[dict], trace) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    context = _format_context(passages)
    user = f"EXTRAITS :\n{context}\n\nQUESTION : {question}"
    resp = client.messages.create(
        model=settings.answer_model,
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    log_generation(trace, name="hermes.answer", model=settings.answer_model,
                   input=user, output=answer, usage=usage)
    return {"answer": answer, "usage": usage}


def query(question: str, top_k: int | None = None, *, role: str | None = None,
          domains: list[str] | None = None, versions: list[str] | None = None) -> dict:
    """RAG hybride + reranking + permissions + citations systématiques."""
    k = top_k or settings.top_k
    eff_domains, eff_versions, perm_info = perms.resolve(role, domains, versions)

    with observe(name="hermes.query", input={"question": question},
                 metadata={"top_k": k, "permissions": perm_info}) as trace:
        qvec = embeddings.embed_query(question)
        candidates = db.hybrid_search(qvec, question, domains=eff_domains, versions=eff_versions)
        passages, rmethod = rerank.rerank(question, candidates, k)

        # Citations SYSTÉMATIQUES (toujours présentes, traçables à la source)
        citations = []
        for i, p in enumerate(passages):
            citations.append({
                "n": i + 1,
                "source_key": p["source_key"],
                "version": p.get("version"),
                "domaine": p.get("domaine"),
                "chunk_index": p.get("chunk_index"),
                "retrieval": p.get("methods", []),
                "scores": {
                    "vector": round(float(p.get("vscore") or 0), 4),
                    "lexical": round(float(p.get("lscore") or 0), 4),
                    "rrf": round(float(p.get("rrf") or 0), 5),
                    **({"rerank": round(float(p["rerank_score"]), 4)} if "rerank_score" in p else {}),
                },
            })

        result = {
            "question": question,
            "retrieval": {"mode": "hybride (vectoriel + lexical, RRF)",
                          "reranking": rmethod, "permissions": perm_info},
            "citations": citations,
            "passages": [{"n": i + 1, "content": p["content"][:800], **citations[i]}
                         for i, p in enumerate(passages)],
        }

        if not passages:
            result["answer"] = None
            result["note"] = "Aucun extrait autorisé/pertinent pour cette requête (permissions ou corpus)."
            return result

        if settings.llm_configured:
            try:
                a = _answer_with_claude(question, passages, trace)
                result["answer"] = a["answer"]
                result["usage"] = a["usage"]
            except Exception as e:
                result["answer"] = None
                result["llm_error"] = str(e)[:200]
        else:
            result["answer"] = None
            result["note"] = ("Réponse LLM désactivée (ANTHROPIC_API_KEY absente) — "
                              "extraits cités renvoyés tels quels.")
        return result
