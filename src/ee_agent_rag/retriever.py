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

# Bump to force-invalidate the on-disk rerank cache (CODE_AUDIT Q6).
_RERANK_CACHE_VERSION = 1


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
    """Rerank ``candidates`` (dicts with ``content``) with Cohere; attach ``score``.

    Responses are disk-cached: rerank is deterministic in (model, query, documents,
    top_n), so the cache is lossless and repeated runs over unchanged candidates are
    free (matters for eval-driven tuning, where most queries don't change between
    iterations).
    """
    if not candidates:
        return []
    key = os.environ.get("COHERE_API_KEY")
    if not key:
        raise RuntimeError("COHERE_API_KEY is not set. Add it to your .env.")

    import hashlib
    import json

    from .config import RERANK_CACHE

    documents = [c["content"] for c in candidates]
    top_n = min(top_k, len(candidates))
    # The full document *text* is part of the key, so a re-ingest that changes a chunk's
    # content already busts the cache. _RERANK_CACHE_VERSION is a manual escape hatch:
    # bump it to force-invalidate every entry if the rerank semantics themselves change
    # (CODE_AUDIT Q6).
    digest = hashlib.sha256(
        json.dumps(
            [_RERANK_CACHE_VERSION, RERANK_MODEL, query, top_n, documents]
        ).encode()
    ).hexdigest()
    cache_path = RERANK_CACHE / f"{digest}.json"

    if cache_path.exists():
        ranked = json.loads(cache_path.read_text())
    else:
        import time

        import cohere

        client = cohere.ClientV2(api_key=key)
        for attempt in range(4):
            try:
                resp = client.rerank(
                    model=RERANK_MODEL, query=query, documents=documents, top_n=top_n
                )
                break
            except cohere.errors.TooManyRequestsError:
                # Trial keys allow 10 calls/min; back off and retry.
                if attempt == 3:
                    raise
                time.sleep(20.0 * (attempt + 1))
        ranked = [
            {"index": r.index, "relevance_score": r.relevance_score}
            for r in resp.results
        ]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(ranked))

    out = []
    for r in ranked:
        hit = dict(candidates[r["index"]])
        hit["score"] = r["relevance_score"]
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
        "book": metadata.get("book"),
        "chapter": metadata.get("chapter"),
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
        # Pass the filter through: BM25 applies it post-hoc so a filtered query keeps
        # hybrid (dense+sparse) recall instead of silently degrading to dense-only.
        sparse = store.sparse_search(query, k=SPARSE_K, filter=filter)
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
