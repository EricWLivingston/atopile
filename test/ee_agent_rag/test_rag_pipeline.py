"""Offline unit tests for the RAG pipeline's pure logic (no API keys / network)."""

from __future__ import annotations

from ee_agent_rag.chunk import approx_tokens, chunk_datasheet
from ee_agent_rag.enrich import (
    enrich,
    extract_mpn_and_manufacturer,
    stable_chunk_id,
)
from ee_agent_rag.retriever import reciprocal_rank_fusion
from ee_agent_rag.store import _scrub, _to_where, tokenize

SAMPLE_MD = """\
# TLV713P LDO
<!-- page 1 -->
banner noise
## Absolute Maximum Ratings
<!-- page 2 -->
Vin max 6 V
## Electrical Characteristics
<!-- page 3 -->
Iq typ 150 nA at Vin=5 V
"""


def test_chunk_splits_on_h2_and_drops_tiny_preamble():
    chunks = chunk_datasheet(SAMPLE_MD)
    sections = [c.section_path for c in chunks]
    assert sections == ["Absolute Maximum Ratings", "Electrical Characteristics"]


def test_chunk_recovers_page_range():
    chunks = chunk_datasheet(SAMPLE_MD)
    ec = next(c for c in chunks if c.section_path == "Electrical Characteristics")
    assert (ec.page_start, ec.page_end) == (3, 3)


def test_mpn_extraction():
    mpn, mfr = extract_mpn_and_manufacturer("The TLV713P is an LDO")
    assert mpn == "TLV713P"
    assert mfr == "Texas Instruments"
    assert extract_mpn_and_manufacturer("no part here") == (None, None)


def test_chunk_id_is_deterministic():
    a = stable_chunk_id("h", "Electrical Characteristics", "Iq typ 150 nA ...")
    b = stable_chunk_id("h", "Electrical Characteristics", "Iq typ 150 nA ...")
    c = stable_chunk_id("h", "Absolute Maximum Ratings", "Iq typ 150 nA ...")
    assert a == b and a != c


def test_enrich_offline_no_summary():
    chunks = chunk_datasheet(SAMPLE_MD)
    out = enrich(
        chunks,
        corpus="datasheets",
        source_path="x.pdf",
        source_hash="hash",
        doc_type="datasheet",
        with_summaries=False,
    )
    assert len(out) == len(chunks)
    m = out[0]["metadata"]
    assert m["corpus"] == "datasheets"
    assert m["summary"] is None
    assert "mpn" in m  # datasheet enrichment ran


def test_rrf_rewards_agreement():
    dense = [{"id": "a", "content": "", "metadata": {}},
             {"id": "b", "content": "", "metadata": {}}]
    sparse = [{"id": "a", "content": "", "metadata": {}},
              {"id": "c", "content": "", "metadata": {}}]
    fused = reciprocal_rank_fusion(dense, sparse)
    assert fused[0]["id"] == "a"  # appears top of both -> wins
    assert {h["id"] for h in fused} == {"a", "b", "c"}


def test_scrub_drops_none_and_encodes_lists():
    scrubbed = _scrub({"a": None, "b": 1, "c": [1, 2], "mpn": "TLV713P"})
    assert "a" not in scrubbed
    assert scrubbed["b"] == 1
    assert scrubbed["c"] == "[1, 2]"
    assert scrubbed["mpn"] == "TLV713P"


def test_where_builder():
    assert _to_where(None) is None
    assert _to_where({"mpn": "X"}) == {"mpn": {"$eq": "X"}}
    multi = _to_where({"mpn": "X", "corpus": "datasheets"})
    assert multi == {"$and": [{"mpn": {"$eq": "X"}}, {"corpus": {"$eq": "datasheets"}}]}


def test_tokenizer_keeps_part_numbers_whole():
    assert tokenize("TLV713P IPC-2221 Vin=5V") == ["tlv713p", "ipc-2221", "vin", "5v"]


def test_approx_tokens():
    assert approx_tokens("a" * 400) == 100
