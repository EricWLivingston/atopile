"""Query-time retrieval: dense + sparse -> RRF fusion -> Cohere rerank -> citations.

``rag_search`` is the single entry point the agent tool wraps. Synchronous on purpose
(Chroma/OpenAI/Cohere clients are sync); the async tool handler calls it via
``asyncio.to_thread``.
"""

from __future__ import annotations

import os

from .config import (
    CORPUS_NAMES,
    DENSE_OVERSAMPLE,
    RERANK_CANDIDATES,
    RERANK_MODEL,
    RRF_K,
    SPARSE_K,
)
from .embed import OpenAIEmbedder
from .store import CorpusStore


def reciprocal_rank_fusion(*ranked_lists: list[dict], k: int = RRF_K) -> list[dict]:
    """Merge ranked hit-lists by RRF. Each hit is a dict with an ``id`` key.

    score(d) = sum over lists of 1 / (k + rank_in_list(d)); rank is 0-based.
    """
    fused: dict[str, dict] = {}
    for hits in ranked_lists:
        for rank, hit in enumerate(hits):
            entry = fused.get(hit["id"])
            if entry is None:
                entry = {**hit, "rrf_score": 0.0}
                fused[hit["id"]] = entry
            entry["rrf_score"] += 1.0 / (k + rank)
    return sorted(fused.values(), key=lambda h: h["rrf_score"], reverse=True)


def _cohere_rerank(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """Rerank ``candidates`` (dicts with ``content``) with Cohere; attach ``score``."""
    if not candidates:
        return []
    key = os.environ.get("COHERE_API_KEY")
    if not key:
        raise RuntimeError("COHERE_API_KEY is not set. Add it to your .env.")

    import cohere

    client = cohere.ClientV2(api_key=key)
    resp = client.rerank(
        model=RERANK_MODEL,
        query=query,
        documents=[c["content"] for c in candidates],
        top_n=min(top_k, len(candidates)),
    )
    out = []
    for r in resp.results:
        hit = dict(candidates[r.index])
        hit["score"] = r.relevance_score
        out.append(hit)
    return out


def _build_citation(metadata: dict) -> dict:
    return {
        "corpus": metadata.get("corpus"),
        "source": metadata.get("source"),
        "page": metadata.get("page_start"),
        "section": metadata.get("section"),
        "clause": metadata.get("clause"),
        "mpn": metadata.get("mpn"),
    }


def rag_search(
    query: str,
    corpus: list[str] | str | None = None,
    top_k: int = 5,
    filter: dict | None = None,
) -> list[dict]:
    """Hybrid retrieval + Cohere rerank across one or more corpora.

    Returns ``[{text, score, citation}, ...]`` (the documented contract). ``corpus`` may
    be a name, a list of names, or ``None`` (= all known corpora).
    """
    if corpus is None:
        corpora = list(CORPUS_NAMES)
    elif isinstance(corpus, str):
        corpora = [corpus]
    else:
        corpora = list(corpus)

    embedder = OpenAIEmbedder()
    qvec = embedder.embed_query(query)

    # Gather candidates per corpus, fuse dense+sparse within each.
    all_candidates: list[dict] = []
    for c in corpora:
        store = CorpusStore(c)
        dense = store.dense_search(qvec, k=top_k * DENSE_OVERSAMPLE, filter=filter)
        sparse = store.sparse_search(query, k=SPARSE_K) if not filter else []
        all_candidates.extend(reciprocal_rank_fusion(dense, sparse))

    # Re-fuse across corpora by RRF score order, cap, then cross-corpus rerank.
    all_candidates.sort(key=lambda h: h.get("rrf_score", 0.0), reverse=True)
    candidates = all_candidates[:RERANK_CANDIDATES]
    reranked = _cohere_rerank(query, candidates, top_k)

    return [
        {
            "text": hit["content"],
            "score": hit.get("score"),
            "citation": _build_citation(hit["metadata"]),
        }
        for hit in reranked
    ]
