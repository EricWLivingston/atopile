"""EE-agent RAG retriever (Option C, milestone M4).

A framework-agnostic engineering-knowledge retriever: ingest PDFs into a Chroma vector
store + BM25 sidecar, then retrieve by fusing dense (OpenAI ``text-embedding-3-large``)
and sparse (BM25) hits via RRF and reranking with Cohere. No LangChain — each step is a
thin wrapper over its SDK so the seams stay swappable.

The agent surface (``rag_search``) lives in ``atopile.server.agent._ee.tools_rag`` and
delegates here; this package knows nothing about atopile's agent runner.
"""

from __future__ import annotations

from .retriever import rag_search

__all__ = ["rag_search"]
