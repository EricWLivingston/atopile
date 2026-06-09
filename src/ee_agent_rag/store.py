"""Chroma vector store + a BM25 sparse sidecar (no LangChain).

One Chroma collection per corpus (cosine), embeddings supplied by us (Chroma's built-in
embedder is unused). BM25 lives beside it as a pickled ``rank_bm25.BM25Okapi`` rebuilt
from the collection's documents — BM25 must be in-process, so it can't live in Chroma.

Chroma metadata must be scalar and non-null; ``_scrub`` drops ``None`` and JSON-encodes
any list/dict so ingest never trips over an empty MPN field.
"""

from __future__ import annotations

import json
import pickle
import re

from .config import BM25_DIR, CHROMA_PATH

_TOKEN_RE = re.compile(r"[A-Za-z0-9.+/-]+")


def tokenize(text: str) -> list[str]:
    """Lowercase, parts-friendly tokenizer (keeps ``TLV713P`` / ``IPC-2221`` whole)."""
    return _TOKEN_RE.findall(text.lower())


def _scrub(metadata: dict) -> dict:
    out: dict = {}
    for k, v in metadata.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = json.dumps(v)  # lists/dicts -> JSON string
    return out


def _to_where(filter: dict | None) -> dict | None:
    """Turn a flat ``{field: value}`` filter into a Chroma ``where`` clause."""
    if not filter:
        return None
    clauses = [{k: {"$eq": v}} for k, v in filter.items()]
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class CorpusStore:
    def __init__(self, corpus: str):
        import chromadb

        self.corpus = corpus
        self.client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        self.collection = self.client.get_or_create_collection(
            name=corpus, metadata={"hnsw:space": "cosine"}
        )
        self.bm25_path = BM25_DIR / f"{corpus}.pkl"

    # --- ingest side --------------------------------------------------------------
    def has_source(self, source_hash: str) -> bool:
        got = self.collection.get(where={"source_hash": source_hash}, limit=1)
        return bool(got["ids"])

    def upsert(self, chunks: list[dict], embeddings: list[list[float]]) -> None:
        self.collection.upsert(
            ids=[c["metadata"]["chunk_id"] for c in chunks],
            embeddings=embeddings,
            documents=[c["content"] for c in chunks],
            metadatas=[_scrub(c["metadata"]) for c in chunks],
        )

    def all_records(self) -> list[dict]:
        got = self.collection.get(include=["documents", "metadatas"])
        return [
            {"id": i, "content": doc, "metadata": meta}
            for i, doc, meta in zip(got["ids"], got["documents"], got["metadatas"])
        ]

    def rebuild_bm25(self) -> int:
        from rank_bm25 import BM25Okapi

        records = self.all_records()
        if not records:
            return 0
        tokenized = [tokenize(r["content"]) for r in records]
        bm25 = BM25Okapi(tokenized)
        self.bm25_path.parent.mkdir(parents=True, exist_ok=True)
        self.bm25_path.write_bytes(
            pickle.dumps(
                {
                    "bm25": bm25,
                    "ids": [r["id"] for r in records],
                    "documents": [r["content"] for r in records],
                    "metadatas": [r["metadata"] for r in records],
                }
            )
        )
        return len(records)

    # --- query side ---------------------------------------------------------------
    def dense_search(
        self, query_vec: list[float], k: int, filter: dict | None = None
    ) -> list[dict]:
        res = self.collection.query(
            query_embeddings=[query_vec],
            n_results=k,
            where=_to_where(filter),
            include=["documents", "metadatas", "distances"],
        )
        out = []
        for i in range(len(res["ids"][0])):
            out.append(
                {
                    "id": res["ids"][0][i],
                    "content": res["documents"][0][i],
                    "metadata": res["metadatas"][0][i],
                    "distance": res["distances"][0][i],
                }
            )
        return out

    def load_bm25(self) -> dict | None:
        if not self.bm25_path.exists():
            return None
        return pickle.loads(self.bm25_path.read_bytes())

    def sparse_search(self, query: str, k: int) -> list[dict]:
        blob = self.load_bm25()
        if blob is None:
            return []
        scores = blob["bm25"].get_scores(tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        return [
            {
                "id": blob["ids"][i],
                "content": blob["documents"][i],
                "metadata": blob["metadatas"][i],
                "score": float(scores[i]),
            }
            for i in ranked
        ]
