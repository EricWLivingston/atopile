"""Centralized configuration for the RAG pipeline.

Every tunable lives here so the notebook (``notebooks/rag_pipeline.ipynb``) can override
a single constant and re-run a step. Values that cost money or need a key read from the
environment; structural knobs (chunk caps, fusion ``k``, baselines) are plain constants.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

DocType = Literal["datasheet", "app_note", "standard", "textbook", "internal"]
Corpus = Literal[
    "datasheets", "app_notes", "standards", "textbooks", "internal_standards"
]

DOC_TYPE_TO_CORPUS: dict[DocType, Corpus] = {
    "datasheet": "datasheets",
    "app_note": "app_notes",
    "standard": "standards",
    "textbook": "textbooks",
    "internal": "internal_standards",
}
CORPUS_NAMES: set[str] = set(DOC_TYPE_TO_CORPUS.values())

# --- Embeddings (OpenAI text-embedding-3-large) -----------------------------------
# 3-large is natively 3072-dim but supports Matryoshka truncation via the ``dimensions``
# request param. 1024 keeps storage/latency down with little quality loss; bump to 3072
# in the notebook if recall needs it. EMBED_DIM must match between ingest and query.
EMBED_MODEL = os.environ.get("EE_EMBED_MODEL", "text-embedding-3-large")
EMBED_DIM = int(os.environ.get("EE_EMBED_DIM", "1024"))
EMBED_BATCH_SIZE = 128

# --- Summaries (optional, OpenAI chat) --------------------------------------------
# v1 stores summaries but does NOT retrieve against them (see RAG_IMPLEMENTATION_PLAN
# "what this phase does NOT include"). Off by default so first bring-up needs no extra
# LLM calls; flip on in the notebook to populate the field.
SUMMARIES_ENABLED = os.environ.get("EE_RAG_SUMMARIES", "0") == "1"
SUMMARY_MODEL = os.environ.get("EE_SUMMARY_MODEL", "gpt-4o-mini")

# --- Reranker (Cohere) ------------------------------------------------------------
RERANK_MODEL = os.environ.get("EE_RERANK_MODEL", "rerank-v3.5")

# --- LlamaParse (REST; the SDK is unusable on py3.14) -----------------------------
LLAMA_PARSE_BASE_URL = os.environ.get(
    "LLAMA_CLOUD_BASE_URL", "https://api.cloud.llamaindex.ai"
)

# --- Retrieval knobs --------------------------------------------------------------
# Cohere bills per reranked document and over-fetching adds latency to every agent
# rag_search, so these are kept to the smallest values that still comfortably cover a
# top_k=5 query (CODE_AUDIT T4). DENSE_OVERSAMPLE=3 -> 15 dense candidates; reranking 20
# fused candidates. A full eval sweep (python -m ee_agent_rag.eval.runner) can push
# these lower if recall@5 holds; raise them in the notebook if recall regresses.
DENSE_OVERSAMPLE = 3  # dense pre-fetch = top_k * this (was 4)
SPARSE_K = 20  # BM25 candidates
RRF_K = 60  # reciprocal-rank-fusion constant
RERANK_CANDIDATES = 20  # how many fused candidates to hand the reranker (was 30)

# --- Chunking caps (approx tokens) ------------------------------------------------
DATASHEET_MAX_TOKENS = 3000
TEXTBOOK_TARGET_TOKENS = 600
TEXTBOOK_OVERLAP_TOKENS = 100
INTERNAL_MAX_TOKENS = 1500

# --- Storage roots ----------------------------------------------------------------
# Default anchors at the *source tree* (<repo>/data), NOT the cwd: the backend server's
# cwd is the opened project, so a cwd-relative default would silently point the agent's
# rag_search at an empty store. EE_DATA_ROOT still overrides.
_REPO_DATA = Path(__file__).resolve().parents[2] / "data"
DATA_ROOT = Path(os.environ.get("EE_DATA_ROOT", _REPO_DATA)).resolve()
PARSED_CACHE = DATA_ROOT / ".parsed_cache"  # parsed markdown, keyed by source hash
CHROMA_PATH = Path(
    os.environ.get("EE_CHROMA_PATH", str(DATA_ROOT / ".chroma"))
).resolve()
BM25_DIR = DATA_ROOT / ".bm25"
# Disk caches for paid query-time calls (identical inputs -> identical outputs, so
# caching is lossless). Keyed by content hash; safe to delete anytime.
EMBED_CACHE = DATA_ROOT / ".embed_cache"  # query embeddings
RERANK_CACHE = DATA_ROOT / ".rerank_cache"  # Cohere rerank responses

# --- Recall@5 baselines (eval gate) -----------------------------------------------
RECALL_BASELINES: dict[Corpus, float] = {
    "datasheets": 0.80,
    "standards": 0.90,
    "app_notes": 0.70,
    "textbooks": 0.65,
    "internal_standards": 0.85,
}
