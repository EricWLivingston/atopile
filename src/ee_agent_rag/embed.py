"""Embeddings via OpenAI ``text-embedding-3-large`` (synchronous, batched).

Uses the ``dimensions`` request param (Matryoshka truncation) so EMBED_DIM is a single
tunable. ``input_type`` has no OpenAI equivalent — the same model is used for documents
and queries, which is correct for OpenAI embeddings.

Query embeddings are cached on disk (keyed by model|dim|text hash): embeddings are
deterministic, so the cache is lossless and makes repeated eval runs free. Document
embeddings are not cached here — ingest already skips unchanged files by source hash.
"""

from __future__ import annotations

import hashlib
import json

from .config import EMBED_BATCH_SIZE, EMBED_CACHE, EMBED_DIM, EMBED_MODEL


class OpenAIEmbedder:
    def __init__(self, model: str = EMBED_MODEL, dim: int = EMBED_DIM):
        from openai import OpenAI

        self.client = OpenAI()
        self.model = model
        self.dim = dim

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            resp = self.client.embeddings.create(
                input=batch, model=self.model, dimensions=self.dim
            )
            # API preserves input order; sort by index defensively.
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        key = hashlib.sha256(f"{self.model}|{self.dim}|{text}".encode()).hexdigest()
        cache = EMBED_CACHE / f"{key}.json"
        if cache.exists():
            return json.loads(cache.read_text())
        vec = self._embed([text])[0]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(vec))
        return vec
