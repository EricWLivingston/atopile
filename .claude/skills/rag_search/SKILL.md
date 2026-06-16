---
name: rag_search
description: "When and how to use rag_search: research design topology before authoring (app notes/white papers/textbooks) and ground specs in the indexed knowledge base (datasheets etc.) with cited chunks, vs web_search (reputable sites only) for designs the corpus lacks. Read before designing or grounding datasheet claims."
---

# rag_search — knowledge-base retrieval guidance

`rag_search` does hybrid retrieval (dense + BM25 → RRF fusion → Cohere rerank) over an
indexed engineering corpus and returns ranked, **cited** chunks `{text, score, citation}`.
Use it to ground claims in real sources instead of recalling specs from memory.

## When to use

- **Before designing a circuit or subsystem** (planning phase): query for design
  guidance on the topology you're about to build — app notes, white papers, and
  textbooks are indexed alongside datasheets. The corpus is the best first source for
  **application guidance, theory, and worked examples**. Examples: "buck converter
  inductor ripple selection", "RS-485 termination and failsafe biasing", "LDO PSRR vs
  output capacitor", "emitter follower output impedance derivation". Do this *before*
  writing `.ato`, and carry the citations into the spec's docstrings.
- A datasheet-answerable spec: quiescent current, dropout, absolute-max ratings, typical
  R_θJA, pinout, recommended support circuit.
- You're about to state a number or a design rule and want a citation behind it.
- Choosing between parts on documented characteristics.

Prefer `rag_search` over simulating something the datasheet already states, and over
`web_search` when the answer is in the indexed corpus.

## When NOT to use

- Open-web / current facts not in the corpus (new parts, prices, availability) → `web_search`
  (reputable sites only — see "Web search for designs" below).
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

## Web search for designs — reputable sources only

The knowledge base is the **first** stop for application guidance, theory, and worked
examples. `web_search` remains a legitimate complement when looking for *designs* —
reference circuits, proven implementations, design ideas the corpus doesn't cover — but
restrict it to reputable sources:

- Semiconductor manufacturers' own sites (TI, Analog Devices, onsemi, NXP, ST,
  Microchip, Infineon, Nexperia, …): app notes, reference designs, eval-board docs.
- Established engineering references and standards bodies' public material.
- Never base a design decision on an unattributed forum post, content-farm article, or
  hobby blog. If such a source is all you can find, corroborate it against the
  knowledge base or a vendor source and flag the weak provenance explicitly.

Preferred sites to check first: *(list pending — to be filled in by the user)*

## Caveats

- Indexed corpora today: **datasheets**, **app notes/white papers**, and **textbooks**
  (the textbook pipeline is live; coverage grows as books are ingested). The standards
  corpus is not yet ingested — don't expect IPC/standards answers here yet.
  An empty result for a topic means the corpus lacks it, not that nothing exists →
  fall back to `web_search` (reputable sites only).
- If retrieval returns nothing or errors (missing keys / empty index), it degrades to
  `{"ok": false, ...}` — fall back to `web_search` or ask the user; never invent a citation.
- Don't paste whole datasheets; ask a specific question and quote the cited chunk.
- Textbook citations carry `book` and `chapter` in the citation — quote them like
  "per <book>, <chapter>" the same way you'd cite a datasheet section.

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
