"""RAG : retrieval pgvector (+ réponse Claude citée, optionnelle) + trace Langfuse."""
from __future__ import annotations

from . import db, embeddings
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


def query(question: str, top_k: int | None = None) -> dict:
    k = top_k or settings.top_k
    with observe(name="hermes.query", input={"question": question}, metadata={"top_k": k}) as trace:
        qvec = embeddings.embed_query(question)
        passages = db.search(qvec, top_k=k)
        citations = [
            {"n": i + 1, "source_key": p["source_key"], "version": p.get("version"),
             "score": round(float(p["score"]), 4)}
            for i, p in enumerate(passages)
        ]
        result = {
            "question": question,
            "citations": citations,
            "passages": [{"n": i + 1, "content": p["content"][:800], **citations[i]}
                         for i, p in enumerate(passages)],
        }
        if settings.llm_configured and passages:
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
