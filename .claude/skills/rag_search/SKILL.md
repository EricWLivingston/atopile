---
name: rag_search
description: "When and how to use rag_search: ground design decisions in the indexed engineering knowledge base (datasheets etc.) with cited chunks, vs web_search for open-web facts. Read before grounding datasheet claims."
---

# rag_search — knowledge-base retrieval guidance

`rag_search` does hybrid retrieval (dense + BM25 → RRF fusion → Cohere rerank) over an
indexed engineering corpus and returns ranked, **cited** chunks `{text, score, citation}`.
Use it to ground claims in real sources instead of recalling specs from memory.

## When to use

- A datasheet-answerable spec: quiescent current, dropout, absolute-max ratings, typical
  R_θJA, pinout, recommended support circuit.
- You're about to state a number or a design rule and want a citation behind it.
- Choosing between parts on documented characteristics.

Prefer `rag_search` over simulating something the datasheet already states, and over
`web_search` when the answer is in the indexed corpus.

## When NOT to use

- Open-web / current facts not in the corpus (new parts, prices, availability) → `web_search`.
- Behaviour that needs computing, not looking up (transient response, oscillation) →
  `pyspice_run` on a minimal subcircuit.
- Project-internal facts (what's in the design) → `report_bom` / `report_variables` /
  `project_read_file`.

## How to call

- `query`: a focused natural-language question ("TLV713 quiescent current at 5 V").
- `corpus`: optional subset of collections; null = search all.
- `top_k`: number of chunks (default 5).
- `filter`: optional metadata filter, e.g. `{"mpn": "TLV713P"}`.

## Reading results

Each hit has `text`, a relevance `score`, and a `citation` (source + page/section/mpn). When
you use a retrieved fact, **carry the citation** into your reasoning and output (e.g. "per the
TLV713 datasheet electrical-characteristics table, Iq ≈ 3.2 µA").

## Caveats

- v1 corpus is **datasheets-only**; standards/app-notes/textbook corpora are not yet
  ingested, so don't expect IPC/standards answers here yet.
- If retrieval returns nothing or errors (missing keys / empty index), it degrades to
  `{"ok": false, ...}` — fall back to `web_search` or ask the user; never invent a citation.
- Don't paste whole datasheets; ask a specific question and quote the cited chunk.

## Parsed-table caveats (read before quoting a table cell)

The chunks come from PDF→markdown parsing, and complex datasheet tables can lose
structure:

- **Flattened merged cells.** A value that spans several part-number columns in the PDF
  (one rating covering multiple variants) may appear under only ONE variant's column,
  with the sibling columns empty. If the cell for *your exact part* is blank but a
  sibling variant's column in the same row has a value, treat that value as **possibly
  shared** — corroborate from prose (Features / General Description / a second chunk)
  before committing it to a design, and flag the uncertainty next to your citation.
- **Column drift.** In wide multi-variant tables (especially pinouts), values can shift
  into the wrong column or rows can have the wrong cell count. If a pin table looks
  ragged or a value seems implausible for its column, prefer the per-pin prose
  description or re-query for the specific pin.
- Never present a spec read from a partially-empty table row as certain for a specific
  variant without one of the corroborations above.
