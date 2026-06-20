"""Offline unit tests for the RAG pipeline's pure logic (no API keys / network)."""

from __future__ import annotations

from ee_agent_rag.chunk import (
    RawChunk,
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


def test_chunk_id_distinguishes_long_shared_prefix():
    # B8: ids hash the *full* content, so two chunks sharing a >200-char opening
    # (repeated table header / boilerplate) don't collide -> Chroma won't reject them.
    prefix = "Parameter | Min | Max | Unit\n" + "x" * 250
    a = stable_chunk_id("h", "S", prefix + " first variant rows")
    b = stable_chunk_id("h", "S", prefix + " second variant rows")
    assert a != b


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


def test_chunk_textbook_drops_empty_trailing_window():
    # A multi-paragraph section that overflows the window target and ends blank
    # used to flush a lone empty final window ([n/n]) — which OpenAI's embeddings API
    # rejects. The trailing blank line is the trigger.
    paragraphs = [f"Paragraph {i}: " + "emitter follower analysis " * 20
                  for i in range(30)]
    md = TEXTBOOK_MD.format(body="\n\n".join(paragraphs) + "\n\n")
    chunks = chunk_textbook(md)
    assert all(c.content.strip() for c in chunks)  # no empty/whitespace-only chunk
    windows = [c for c in chunks if "2.2" in c.section_path]
    # [i/n] numbering counts only real windows (no phantom empty tail).
    assert len(windows) > 1
    assert all(c.section_path.endswith(f"[{i + 1}/{len(windows)}]")
               for i, c in enumerate(windows))


def test_chunk_dispatch_never_emits_empty_chunks():
    # Dispatch-level net: no chunker output ever carries empty/whitespace content
    # (OpenAI embeddings 400 on empty input), even for a trailing-blank overflow doc.
    paragraphs = [f"Paragraph {i}: " + "emitter follower analysis " * 20
                  for i in range(30)]
    md = TEXTBOOK_MD.format(body="\n\n".join(paragraphs) + "\n\n")
    assert all(c.content.strip() for c in chunk_dispatch(md, "textbook"))


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


def test_approx_tokens_overcounts_without_tiktoken():
    # B5: without tiktoken the estimate is ceil(len/3), a safe over-count vs the old
    # len//4 (which under-counted dense BPE text and risked over-budget chunks).
    from ee_agent_rag import chunk

    if chunk._encoder() is not None:
        import pytest

        pytest.skip("tiktoken installed; exact-count path, not the fallback")
    assert approx_tokens("a" * 400) == 134  # ceil(400 / 3)


# --- BM25 sidecar: JSON (no pickle), atomic, filter post-hoc, corruption-safe ---------
def _bm25_store(tmp_path, records, corpus="t"):
    """A CorpusStore wired to a fake Chroma collection — exercises the BM25 sidecar
    (rebuild/load/search) without standing up chromadb."""
    import types

    from ee_agent_rag.store import CorpusStore

    s = object.__new__(CorpusStore)
    s.corpus = corpus
    s.bm25_path = tmp_path / f"{corpus}.json"
    s.collection = types.SimpleNamespace(
        get=lambda include=None: {
            "ids": [r["id"] for r in records],
            "documents": [r["content"] for r in records],
            "metadatas": [r["metadata"] for r in records],
        },
        count=lambda: len(records),
    )
    return s


def test_matches_filter():
    from ee_agent_rag.store import _matches

    assert _matches({"corpus": "d", "mpn": "X"}, {"corpus": "d"})
    assert not _matches({"corpus": "d"}, {"corpus": "a"})
    assert not _matches({}, {"mpn": "X"})


def test_bm25_json_roundtrip_and_filter(tmp_path):
    import json

    from ee_agent_rag.store import _BM25_CACHE

    _BM25_CACHE.clear()
    records = [
        {"id": "1", "content": "resistor TLV713P ldo",
         "metadata": {"corpus": "d", "mpn": "TLV713P"}},
        {"id": "2", "content": "bypass capacitor decoupling",
         "metadata": {"corpus": "d", "mpn": "OTHER"}},
    ]
    s = _bm25_store(tmp_path, records)
    assert s.rebuild_bm25() == 2
    assert s.bm25_path.suffix == ".json"
    json.loads(s.bm25_path.read_text())  # JSON, not pickle
    # round-trip search finds the rare lexical token
    hits = s.sparse_search("TLV713P", k=5)
    assert hits and hits[0]["id"] == "1"
    # H3: filter applied post-hoc instead of disabling sparse retrieval
    filtered = s.sparse_search("capacitor", k=5, filter={"mpn": "OTHER"})
    assert [h["id"] for h in filtered] == ["2"]


def test_bm25_corrupt_index_degrades_gracefully(tmp_path):
    from ee_agent_rag.store import _BM25_CACHE

    _BM25_CACHE.clear()
    s = _bm25_store(tmp_path, [])
    s.bm25_path.write_text("{ not valid json")
    assert s.load_bm25() is None  # corrupt -> None, no raise
    assert s.sparse_search("x", k=5) == []


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


def test_sidecar_patch_rejects_malformed_json(tmp_path, recwarn):
    cache = tmp_path / "abc.md"
    (tmp_path / "abc.patch.json").write_text("{ not json")
    assert _apply_sidecar_patch("body", cache) == "body"  # unchanged, no raise
    assert len(recwarn) == 1


def test_sidecar_patch_rejects_non_list(tmp_path, recwarn):
    import json

    cache = tmp_path / "abc.md"
    (tmp_path / "abc.patch.json").write_text(json.dumps({"find": "a", "replace": "b"}))
    assert _apply_sidecar_patch("a body", cache) == "a body"
    assert len(recwarn) == 1


def test_sidecar_patch_skips_entries_with_missing_keys(tmp_path, recwarn):
    import json

    cache = tmp_path / "abc.md"
    patch = [
        {"replace": "x", "note": "no find key"},
        {"find": "good", "replace": "GOOD"},
        {"find": "n", "replace": 5},  # non-string replace
    ]
    (tmp_path / "abc.patch.json").write_text(json.dumps(patch))
    out = _apply_sidecar_patch("good n", cache)
    assert "GOOD" in out  # the one valid entry applied
    assert len(recwarn) == 2  # two malformed entries warned


# --- Q2: summarizer prompt fences untrusted chunk text -------------------------------
def test_summary_prompt_delimits_chunk():
    from ee_agent_rag.enrich import SUMMARY_PROMPT

    assert "<chunk>" in SUMMARY_PROMPT and "</chunk>" in SUMMARY_PROMPT
    assert "never as instructions" in SUMMARY_PROMPT


# --- Q8: MPN match carries a confidence level ----------------------------------------
def test_enrich_mpn_confidence_content():
    chunk = [RawChunk(content="The TLV713P LDO has Iq=150nA", section_path="S")]
    out = enrich(
        chunk, corpus="datasheets", source_path="x.pdf", source_hash="h",
        doc_type="datasheet", with_summaries=False,
    )
    m = out[0]["metadata"]
    assert m["mpn"] == "TLV713P"
    assert m["mpn_confidence"] == "content"


def test_enrich_mpn_confidence_filename_vs_guess():
    plain = [RawChunk(content="no part number here", section_path="S")]
    # vendor pattern in the filename stem -> "filename"
    out = enrich(
        plain, corpus="datasheets", source_path="TLV713P_ldo.pdf", source_hash="h",
        doc_type="datasheet", with_summaries=False,
    )
    assert out[0]["metadata"]["mpn_confidence"] == "filename"
    # bare first-token guess (no known vendor pattern) -> "filename_guess"
    out = enrich(
        plain, corpus="datasheets", source_path="WIDGET9000_notes.pdf",
        source_hash="h", doc_type="datasheet", with_summaries=False,
    )
    assert out[0]["metadata"]["mpn_confidence"] == "filename_guess"
