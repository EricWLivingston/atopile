"""Thin agent-side wrapper for the ``rag_search`` tool (M4).

Bridges atopile's async tool handler to the synchronous, framework-agnostic retriever in
``ee_agent_rag``. Every failure mode (missing keys, empty index, import error) degrades
to a ``{"ok": False, "error": ...}`` payload so a live run never crashes on retrieval.
"""

from __future__ import annotations

import asyncio
from typing import Any

_MAX_TEXT_CHARS = 1500  # trim chunk bodies so the tool result stays prompt-sized


def _run(arguments: dict[str, Any]) -> dict[str, Any]:
    """Synchronous core — runs in a worker thread."""
    query = (arguments.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required", "results": []}

    from ee_agent_rag import rag_search

    raw = rag_search(
        query=query,
        corpus=arguments.get("corpus"),
        top_k=int(arguments.get("top_k", 5)),
        filter=arguments.get("filter"),
    )
    results = [
        {
            "text": r["text"][:_MAX_TEXT_CHARS],
            "score": r["score"],
            "citation": r["citation"],
        }
        for r in raw
    ]
    return {"ok": True, "query": query, "count": len(results), "results": results}


async def run_rag_search(arguments: dict[str, Any]) -> dict[str, Any]:
    """Async entry called by the registered tool handler."""
    try:
        return await asyncio.to_thread(_run, arguments)
    except Exception as e:  # noqa: BLE001 - tool must not crash the run
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "results": [],
        }
