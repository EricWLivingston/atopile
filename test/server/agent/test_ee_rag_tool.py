"""Offline tests for the rag_search agent-tool wrapper (no API keys / network).

Covers the M4 contract: query validation, happy-path formatting/truncation, and graceful
degradation when the retriever raises (e.g. missing keys / empty index). The agent must
never crash on retrieval.
"""

from __future__ import annotations

import asyncio

import pytest

from atopile.server.agent._ee import tools_rag


def _call(arguments: dict) -> dict:
    return asyncio.run(tools_rag.run_rag_search(arguments))


def test_missing_query_is_rejected_gracefully():
    out = _call({"query": "   "})
    assert out["ok"] is False
    assert "query is required" in out["error"]
    assert out["results"] == []


def test_retriever_exception_degrades_gracefully(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("COHERE_API_KEY is not set.")

    monkeypatch.setattr("ee_agent_rag.rag_search", boom)
    out = _call({"query": "TLV713P quiescent current"})
    assert out["ok"] is False
    assert "RuntimeError" in out["error"]
    assert out["results"] == []


def test_happy_path_formats_and_truncates(monkeypatch):
    long_text = "x" * 5000

    def fake(**kwargs):
        assert kwargs["query"] == "Iq of TLV713P"
        assert kwargs["top_k"] == 3
        return [
            {
                "text": long_text,
                "score": 0.91,
                "citation": {"mpn": "TLV713P", "page": 3, "section": "Electrical"},
            }
        ]

    monkeypatch.setattr("ee_agent_rag.rag_search", fake)
    out = _call({"query": "Iq of TLV713P", "top_k": 3})
    assert out["ok"] is True
    assert out["count"] == 1
    r = out["results"][0]
    assert len(r["text"]) == tools_rag._MAX_TEXT_CHARS  # trimmed
    assert r["score"] == 0.91
    assert r["citation"]["mpn"] == "TLV713P"


def test_corpus_and_filter_pass_through(monkeypatch):
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("ee_agent_rag.rag_search", fake)
    _call({"query": "q", "corpus": ["datasheets"], "filter": {"mpn": "TLV713P"}})
    assert seen["corpus"] == ["datasheets"]
    assert seen["filter"] == {"mpn": "TLV713P"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
