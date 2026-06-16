"""Offline unit tests for the RAG pipeline's pure logic (no API keys / network)."""

from __future__ import annotations

from ee_agent_rag.chunk import (
    approx_tokens,
    chunk_datasheet,
    chunk_dispatch,
    chunk_textbook,
)
from ee_agent_rag.enrich import (
    enrich,
    extract_mpn_and_manufacturer,
    stable_chunk_id,
)
from ee_agent_rag.parse import _apply_sidecar_patch, _strip_header_title_pollution
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


TEXTBOOK_MD = """\
# The Art of Electronics
<!-- page 1 -->
cover noise
## Chapter 2: Bipolar Transistors
<!-- page 61 -->
### 2.1 Introduction
The transistor is our most important example of an active component.
### 2.2 Emitter Follower
{body}
## Chapter 3: Field-Effect Transistors
<!-- page 131 -->
Short chapter body.
"""


def test_chunk_textbook_keeps_small_sections_whole():
    md = TEXTBOOK_MD.format(body="Vout follows Vin minus a diode drop.")
    chunks = chunk_textbook(md)
    paths = [c.section_path for c in chunks]
    # Small chapters stay whole; no windowing suffixes anywhere.
    assert "Chapter 3: Field-Effect Transistors" in paths
    assert all("[" not in p for p in paths)
    ch2 = next(c for c in chunks if "Chapter 2" in c.section_path)
    assert ch2.extras == {"chapter": "Chapter 2: Bipolar Transistors"}


def test_chunk_textbook_windows_oversized_section_with_overlap():
    paragraphs = [f"Paragraph {i}: " + "emitter follower analysis " * 20
                  for i in range(30)]
    md = TEXTBOOK_MD.format(body="\n\n".join(paragraphs))
    chunks = chunk_textbook(md)
    windows = [c for c in chunks
               if c.section_path.startswith("Chapter 2: Bipolar Transistors > 2.2")]
    assert len(windows) > 1
    assert all("/" in c.section_path for c in windows)  # "[i/n]" suffix
    assert all(approx_tokens(c.content) < 2 * 600 for c in windows)
    # Overlap: each window starts with the tail of the previous one.
    for prev, cur in zip(windows, windows[1:]):
        last_para = prev.content.split("\n\n")[-1]
        assert last_para in cur.content
    # Chapter heading still carried in extras for metadata.
    assert all(c.extras == {"chapter": "Chapter 2: Bipolar Transistors"}
               for c in windows)


def test_chunk_dispatch_routes_textbook():
    md = TEXTBOOK_MD.format(body="short")
    assert chunk_dispatch(md, "textbook")


def test_enrich_textbook_metadata():
    md = TEXTBOOK_MD.format(body="Vout follows Vin minus a diode drop.")
    out = enrich(
        chunk_textbook(md),
        corpus="textbooks",
        source_path="data/textbooks/The_Art_of_Electronics.pdf",
        source_hash="hash",
        doc_type="textbook",
        with_summaries=False,
    )
    m = next(o["metadata"] for o in out if "Chapter 2" in o["metadata"]["section"])
    assert m["book"] == "The Art of Electronics"
    assert m["chapter"] == "Chapter 2: Bipolar Transistors"
    assert "mpn" not in m  # datasheet-only enrichment must not run


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


_TITLE = "Electrical Characteristics (@ TA = 25°C)"
_POLLUTED_TABLE = f"""\
## {_TITLE}

| {_TITLE}<br/>Parameter | {_TITLE}<br/>Min. | {_TITLE} | {_TITLE}<br/>Unit |
| --- | --- | --- | --- |
| Forward Voltage | 0.35 | CD0603-B0240R | V |
"""


def test_strip_header_title_pollution():
    out = _strip_header_title_pollution(_POLLUTED_TABLE)
    lines = out.splitlines()
    assert lines[2] == "| Parameter | Min. |  | Unit |"
    # body rows and separator untouched
    assert lines[3] == "| --- | --- | --- | --- |"
    assert "| Forward Voltage | 0.35 | CD0603-B0240R | V |" in out
    # idempotent, and clean headers pass through unchanged
    assert _strip_header_title_pollution(out) == out


def test_strip_header_pollution_ignores_unrelated_br():
    # cells with <br/> but no shared prefix must not be rewritten
    md = "| A<br/>x | B<br/>y |\n| --- | --- |\n| 1 | 2 |\n"
    assert _strip_header_title_pollution(md) == md


def test_sidecar_patch_applies_and_skips_unsafe(tmp_path, recwarn):
    import json

    cache = tmp_path / "abc.md"
    patch = [
        {"find": "| 0.43 | 0.5 |  |", "replace": "|  | 0.43 | 0.5 |", "note": "shift"},
        {"find": "not present", "replace": "x", "note": "stale entry"},
        {"find": "row", "replace": "x", "note": "ambiguous entry"},
    ]
    (tmp_path / "abc.patch.json").write_text(json.dumps(patch))
    md = "row one | 0.43 | 0.5 |  |\nrow two\n"
    out = _apply_sidecar_patch(md, cache)
    assert "|  | 0.43 | 0.5 |" in out
    assert "row two" in out  # ambiguous 'row' entry not applied
    assert len(recwarn) == 2  # stale + ambiguous both warned


def test_sidecar_patch_noop_without_file(tmp_path):
    assert _apply_sidecar_patch("text", tmp_path / "none.md") == "text"
