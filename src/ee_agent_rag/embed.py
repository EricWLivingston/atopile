"""Embeddings via OpenAI ``text-embedding-3-large`` (synchronous, batched).

Uses the ``dimensions`` request param (Matryoshka truncation) so EMBED_DIM is a single
tunable. ``input_type`` has no OpenAI equivalent — the same model is used for documents
and queries, which is correct for OpenAI embeddings.
"""

from __future__ import annotations

from .config import EMBED_BATCH_SIZE, EMBED_DIM, EMBED_MODEL


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
        return self._embed([text])[0]
