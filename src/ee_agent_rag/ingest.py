"""Top-level ingest: classify -> parse -> chunk -> enrich -> embed -> store -> BM25.

Run as a module:

    uv run python -m ee_agent_rag.ingest data/datasheets/tlv713p.pdf
    uv run python -m ee_agent_rag.ingest data/datasheets/*.pdf --force
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from .chunk import chunk_dispatch
from .classify import classify
from .config import DOC_TYPE_TO_CORPUS
from .embed import OpenAIEmbedder
from .enrich import enrich
from .parse import parse
from .store import CorpusStore


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def ingest_one(
    path: Path,
    *,
    force: bool = False,
    reparse: bool = False,
    with_summaries: bool | None = None,
) -> dict:
    """Ingest a single document. Returns a status dict.

    ``force`` re-chunks/re-embeds/re-stores even if the source is unchanged but
    **reuses the cached parse** — it must never silently clobber a parse that was
    produced with special settings (e.g. premium mode). Pass ``reparse=True`` to
    also bust the parse cache (after changing the parser instruction).
    """
    path = Path(path)
    doc_type = classify(path)
    corpus = DOC_TYPE_TO_CORPUS[doc_type]
    source_hash = _sha256(path)

    store = CorpusStore(corpus)
    if not force and store.has_source(source_hash):
        return {"status": "skipped", "reason": "unchanged", "path": str(path)}
    store.delete_source(source_hash)  # no stale chunks if content/chunking changed

    parsed_md = parse(path, doc_type, force=reparse)
    raw_chunks = chunk_dispatch(parsed_md, doc_type)
    enriched = enrich(
        raw_chunks,
        corpus=corpus,
        source_path=str(path),
        source_hash=source_hash,
        doc_type=doc_type,
        with_summaries=with_summaries,
    )

    embeddings = OpenAIEmbedder().embed_documents([c["content"] for c in enriched])
    store.upsert(enriched, embeddings)
    n_total = store.rebuild_bm25()

    return {
        "status": "ingested",
        "path": str(path),
        "corpus": corpus,
        "doc_type": doc_type,
        "chunks": len(enriched),
        "corpus_total": n_total,
    }


def ingest_many(
    paths: list[Path], *, force: bool = False, reparse: bool = False
) -> list[dict]:
    results = []
    for p in paths:
        try:
            results.append(ingest_one(p, force=force, reparse=reparse))
        except Exception as e:  # noqa: BLE001 - report, keep going
            results.append({"status": "error", "path": str(p), "error": str(e)})
        print(results[-1])
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest documents into the RAG store.")
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-ingest even if unchanged (reuses the cached parse)",
    )
    ap.add_argument(
        "--reparse",
        action="store_true",
        help="also bust the parse cache (paid LlamaParse call per doc)",
    )
    args = ap.parse_args()
    ingest_many(args.paths, force=args.force or args.reparse, reparse=args.reparse)


if __name__ == "__main__":
    main()
