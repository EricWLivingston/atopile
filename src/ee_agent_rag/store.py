"""Chroma vector store + a BM25 sparse sidecar (no LangChain).

One Chroma collection per corpus (cosine), embeddings supplied by us (Chroma's built-in
embedder is unused). BM25 lives beside it as a **JSON** sidecar (``{ids, documents,
metadatas, tokenized}``); ``rank_bm25.BM25Okapi`` is rebuilt from the stored tokenized
corpus on load. BM25 must be in-process, so it can't live in Chroma.

The sidecar is deliberately *not* pickle: pickle deserialization of attacker-influenced
bytes is remote code execution, and the BM25 dir is a writable cache (shared/synced).
JSON of the reconstructable state removes that trust-boundary gap (see CODE_AUDIT H1)
and the write is atomic (tmp + ``os.replace``) so a crash mid-write can't leave a
truncated index.

Chroma metadata must be scalar and non-null; ``_scrub`` drops ``None`` and JSON-encodes
any list/dict so ingest never trips over an empty MPN field.
"""

from __future__ import annotations

import json
import logging
import os
import re

from .config import BM25_DIR, CHROMA_PATH

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9.+/-]+")

# In-process cache of loaded BM25 blobs, keyed by path -> (mtime_ns, blob). Retriever
# constructs a fresh CorpusStore per query, so without this every sparse_search would
# re-read + re-fit BM25Okapi from disk. Invalidated automatically when the file's mtime
# changes (re-ingest rewrites it via os.replace, which bumps mtime).
_BM25_CACHE: dict[str, tuple[int, dict]] = {}


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


def _matches(metadata: dict, filter: dict) -> bool:
    """Equality match of a flat ``{field: value}`` filter against stored metadata —
    the in-process equivalent of the Chroma ``$eq``/``$and`` ``where`` clause, used to
    apply filters to BM25 results that never went through Chroma."""
    return all(metadata.get(k) == v for k, v in filter.items())


class CorpusStore:
    def __init__(self, corpus: str):
        import chromadb

        self.corpus = corpus
        self.client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        self.collection = self.client.get_or_create_collection(
            name=corpus, metadata={"hnsw:space": "cosine"}
        )
        self.bm25_path = BM25_DIR / f"{corpus}.json"

    # --- ingest side --------------------------------------------------------------
    def has_source(self, source_hash: str) -> bool:
        got = self.collection.get(where={"source_hash": source_hash}, limit=1)
        return bool(got["ids"])

    def delete_source(self, source_hash: str) -> None:
        """Drop all chunks of a document — re-ingest must not leave stale chunks
        behind when content/chunking changed (chunk ids change with content)."""
        self.collection.delete(where={"source_hash": source_hash})

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
        records = self.all_records()
        if not records:
            return 0
        tokenized = [tokenize(r["content"]) for r in records]
        blob = {
            "ids": [r["id"] for r in records],
            "documents": [r["content"] for r in records],
            "metadatas": [r["metadata"] for r in records],
            "tokenized": tokenized,
        }
        self.bm25_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: a crash mid-write must not leave a truncated index that the next
        # sparse_search would fail/silently-degrade on. Write to a temp file in the same
        # dir, then os.replace (atomic rename on POSIX + Windows).
        tmp = self.bm25_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob))
        os.replace(tmp, self.bm25_path)
        _BM25_CACHE.pop(str(self.bm25_path), None)
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
        """Load the JSON sidecar and rebuild ``BM25Okapi`` from its tokenized corpus.

        Cached in-process keyed by file mtime. Returns ``None`` (and logs a warning)
        on a missing or corrupt index — never raises, so a query degrades to
        dense-only rather than crashing.
        """
        if not self.bm25_path.exists():
            return None
        path_key = str(self.bm25_path)
        mtime = self.bm25_path.stat().st_mtime_ns
        cached = _BM25_CACHE.get(path_key)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        from rank_bm25 import BM25Okapi

        try:
            raw = json.loads(self.bm25_path.read_text())
            blob = {
                "bm25": BM25Okapi(raw["tokenized"]),
                "ids": raw["ids"],
                "documents": raw["documents"],
                "metadatas": raw["metadatas"],
            }
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning(
                "BM25 index for corpus %r is corrupt (%s); sparse retrieval disabled "
                "until re-ingest. Path: %s",
                self.corpus,
                exc,
                self.bm25_path,
            )
            return None
        _BM25_CACHE[path_key] = (mtime, blob)
        return blob

    def sparse_search(
        self, query: str, k: int, filter: dict | None = None
    ) -> list[dict]:
        """BM25 top-``k``. When ``filter`` is given it is applied to the BM25 metadatas
        post-hoc (rather than skipping sparse retrieval), so hybrid recall — the whole
        point of BM25 for rare lexical tokens like MPNs — survives a filtered query."""
        blob = self.load_bm25()
        if blob is None:
            # Distinguish "no corpus ingested yet" (expected) from "documents exist but
            # the sidecar is missing" (a real degradation worth surfacing).
            try:
                if self.collection.count() > 0:
                    log.warning(
                        "BM25 index missing for corpus %r though %d documents exist; "
                        "run ingest to rebuild. Falling back to dense-only retrieval.",
                        self.corpus,
                        self.collection.count(),
                    )
            except Exception:  # noqa: BLE001 - count() must never break a query
                pass
            return []
        scores = blob["bm25"].get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out: list[dict] = []
        for i in order:
            if filter and not _matches(blob["metadatas"][i], filter):
                continue
            out.append(
                {
                    "id": blob["ids"][i],
                    "content": blob["documents"][i],
                    "metadata": blob["metadatas"][i],
                    "score": float(scores[i]),
                }
            )
            if len(out) >= k:
                break
        return out
