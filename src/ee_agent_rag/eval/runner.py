"""Recall@K eval for the retriever.

Each line of the dataset is a query plus expectations:

    {"query": "...", "must_contain_mpn": "TLV713P", "must_contain_section": "Elec"}

A query "passes" if at least one of the top-K results satisfies every stated expectation
(``must_contain_mpn`` exact match on citation MPN; ``must_contain_section`` substring,
case-insensitive). Target: recall@5 >= the corpus baseline in ``config``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import RECALL_BASELINES
from ..retriever import rag_search


def _passes(q: dict, results: list[dict]) -> bool:
    ok = True
    if "must_contain_mpn" in q:
        ok &= any(
            r["citation"].get("mpn") == q["must_contain_mpn"] for r in results
        )
    if "must_contain_section" in q:
        needle = q["must_contain_section"].lower()
        ok &= any(
            needle in (r["citation"].get("section") or "").lower() for r in results
        )
    if "must_contain_text" in q:
        needle = q["must_contain_text"].lower()
        ok &= any(needle in (r["text"] or "").lower() for r in results)
    return ok


def run_eval(
    corpus: str, dataset_path: Path, top_k: int = 5, verbose: bool = True
) -> dict:
    queries = [
        json.loads(line)
        for line in Path(dataset_path).read_text().splitlines()
        if line.strip()
    ]
    hits, misses = 0, []
    for q in queries:
        results = rag_search(q["query"], corpus=corpus, top_k=top_k)
        if _passes(q, results):
            hits += 1
        else:
            misses.append(q["query"])
            if verbose:
                top = results[0]["citation"] if results else None
                print(f"MISS: {q['query']!r} -> top={top}")
    recall = hits / len(queries) if queries else 0.0
    baseline = RECALL_BASELINES.get(corpus, 0.8)
    return {
        "corpus": corpus,
        "total": len(queries),
        "hits": hits,
        f"recall_at_{top_k}": round(recall, 3),
        "baseline": baseline,
        "pass": recall >= baseline,
        "misses": misses,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="datasheets")
    ap.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).parent / "datasets" / "datasheets.jsonl",
    )
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()
    print(json.dumps(run_eval(args.corpus, args.dataset, args.top_k), indent=2))


if __name__ == "__main__":
    main()
