# RAG tuning summary — datasheets corpus (M4, 2026-06-10)

> **Result: recall@5 = 0.98 (49/50) against the user-approved 50-question validation
> set — gate was ≥ 0.80 (40/50).** Achieved with **all retrieval knobs at their
> defaults**; every improvement that mattered was *ingest fidelity*, not query-time
> tuning. That is the best possible outcome for generalization to the future corpora
> (textbooks, whitepapers, standards): nothing was bent toward datasheets.

## What was run

1. **Validation set** — 50 questions over the 14-PDF corpus
   (`ee_agent_docs_5_21/RAG_VALIDATION_SET.md`, mirrored to
   `src/ee_agent_rag/eval/datasets/datasheets.jsonl`): ~17 spec lookups,
   ~17 implementation/application questions, ~9 conceptual paraphrases, ~4
   exact-identifier (BM25) queries, ~3 part-numbering. Grounded in pdfminer text
   extracts *before* ingestion, so the set couldn't be written toward the pipeline.
2. **Ingest** — all 14 datasheets (~507 pages) → LlamaParse REST → section chunker →
   OpenAI `text-embedding-3-large` (1024-dim) → Chroma + BM25: **1156 chunks**.
3. **Eval** — `run_eval('datasheets', …, top_k=5)` → 49/50 on the first full run.
4. **Miss diagnosis** (four-stage dense/sparse/fused/reranked trace) + one targeted
   re-parse — see "The one residual miss" below.

## What actually changed (and why)

All changes are pipeline-fidelity fixes, found because the eval/bring-up exposed them.
None are datasheet-specific logic; each transfers to every future corpus.

| Change | File | Why |
|---|---|---|
| **Deterministic page markers** — fetch LlamaParse's per-page JSON result and synthesize `<!-- page N -->` in code | `parse.py` | LlamaParse **silently ignored** the instruction to emit page markers (0 markers in the first parsed doc). Page citations are part of the tool contract; instruction-following was the wrong mechanism. |
| **Tightened `[Diagram:]` instruction** — placeholders only for true figures; never swallow equations/prose | `parse.py` | LlamaParse collapsed an equation region *plus its surrounding prose* into one `[Diagram: …]` placeholder, deleting a fact from the index (see residual miss). Improved but didn't fully fix that page. |
| **Chunk-id ordinal** — `stable_chunk_id(…, ordinal)` | `enrich.py` | Repeated headings with identical openings (table headers, boilerplate) produced duplicate SHA ids; Chroma rejects duplicate ids in one upsert (6 of 14 docs failed). Ordinal keeps ids deterministic *and* unique. |
| **MPN patterns for 12 more vendors** + **filename-stem fallback** | `enrich.py` | Only 1 of 14 corpus parts matched the old TI/ST/Nordic-era patterns → `mpn: null` citations. Fallback also fixed a real regex trap: `\b` never fires at `_`, so filename matching needed `_`→space first. |
| **Stale-chunk deletion on re-ingest** | `store.py`, `ingest.py` | `force` re-ingest upserted new chunk ids without deleting old ones → stale chunks would linger after any parser/chunker change. |
| **Query-embedding disk cache** | `embed.py` | Embeddings are deterministic → lossless cache; repeated eval runs cost 0 OpenAI calls. |
| **Cohere rerank disk cache + 429 backoff** | `retriever.py` | Rerank is deterministic in (model, query, docs, top_n) → lossless cache; trial keys allow 10 calls/min, so the eval needed backoff. The cache files double as the API-call ledger. |
| **`must_source_contain` + subset eval (`indices`)** | `eval/runner.py` | Doc-attribution check that doesn't depend on parse-dependent metadata; subset re-eval lets a tuning iteration re-test only misses instead of burning 50 reranks. |

## Final knob values — **all defaults, unchanged**

| Knob | Value | Status |
|---|---|---|
| `EMBED_MODEL` / `EMBED_DIM` | `text-embedding-3-large` / 1024 | default |
| `DENSE_OVERSAMPLE` | 4 (→ 20 dense candidates at top_k=5) | default |
| `SPARSE_K` | 20 | default |
| `RRF_K` | 60 | default |
| `RERANK_CANDIDATES` | 30 | default |
| `RERANK_MODEL` | `rerank-v3.5` | default |
| `DATASHEET_MAX_TOKENS` | 3000 | default |
| `SUMMARIES_ENABLED` | off | default |

The §6 tuning loop (`16_RAG_NOTEBOOK_AND_TUNING.md`) was armed but never needed past
step 1: the only miss localized to "fact absent from the index" (parser drop), which no
query-time knob can recover.

## The one residual miss (#30)

*"What inductor value is recommended for a typical TPS61030 application?"*
(needle: `6.8`).

- The answer sentence ("…a 6.8 µH inductance is recommended") sits directly after
  Equation 5 on p.14 of the TI PDF. LlamaParse dropped it in **both** parse attempts
  (original and tightened instruction) — the page mixes display equations, prose, and a
  table, and the parser truncates the section at the equation.
- Retrieval still behaves correctly: top-1 is the right document, right section
  ("Detailed Design Procedure" / Inductor Selection prose with Equation 4/5 context,
  correct MPN + page). The `6.8 µH` value survives in application-circuit component
  tables, which rank ~6–8 (inside the rerank shortlist, just below top-5).
- An agent using the tool would land on the design-procedure section and the typical
  application circuit — i.e. the practically-right answer surface. Scored strictly
  against the text needle, it's a miss.
- **Not** worth fixing by knob distortion (e.g. raising `top_k` globally) — that's
  overfitting one question. The real fix, if ever needed, is parse-level (e.g. a
  vision-capable parse mode for equation-heavy pages).

## API-call ledger (budget: < 100 per key)

| Key | Spent | Breakdown |
|---|---|---|
| **LlamaParse** | ~17 jobs | 14 parses + 2 from the pre-fix run (1 complete, 1 in-flight when killed) + 1 TPS61030 re-parse |
| **OpenAI** | ~80 calls | ~18 ingest embed batches + ~10 re-spent on the 6 docs that failed on duplicate ids + 1–2 re-ingest batches + 50 query embeds (now cached — further evals are free) |
| **Cohere** | ~60 reranks | 50 first full eval (10 of them from the 429-interrupted run, reused via cache) + 5 subset re-eval + ~5 traces (all cached now) |

## Generalization notes (for textbooks / whitepapers / standards)

Expected to **transfer as-is**: page-marker synthesis, chunk-id ordinal, stale-chunk
deletion, embed/rerank caches, RRF/rerank defaults, `must_source_contain` eval key.

Expected to **need per-corpus work** (already designed for, see passdown "Open items"):
corpus-specific chunkers (clause-aware for standards, heading-aware ~600 tok for
textbooks), a parse instruction per doc type (`parse._INSTRUCTIONS`), metadata
extractors (standard/revision/clause instead of MPN), and a fresh ~30–50-question eval
set per corpus (`RECALL_BASELINES` already seeds the gates). The datasheet knobs in
`config.py` are per-type constants, so tuning them never leaks into other corpora.

## Table-fidelity hardening (follow-up, same day)

User-spotted failure mode: retrieval returns the right chunk, but LlamaParse **flattened
merged cells** — CD0603's abs-max table had `VRRM 40 V` (which spans both part variants
in the PDF) under only one column, so a model could misattribute the spec *with a
confident citation*. A corpus scan found 2 affected docs (CD0603: 4 flattened spans;
NVT2008: pin-table **column drift** — values shifted across columns).

Fixes, layered:
- **Parse instruction** (`DATASHEET_INSTRUCTION`): explicit merged-cell replication rule
  ("replicate a spanning value into EVERY column/row it covers; never shift values").
  Fixed CD0603 completely (VRRM/IFSM now in both columns; scanner-clean).
- **Premium parse mode** for docs the instruction can't fix: `parse(..., premium=True)`
  / `EE_PARSE_PREMIUM=1` (REST `premium_mode`). Fixed NVT2008's pin table (per-variant
  pin numbers + row-spanned descriptions all correct). Residual 10 "suspects" are a
  legitimately-sparse triangular Rpu matrix — true negatives.
- **Permanent scanner** `ee_agent_rag/eval/table_fidelity.py` (+5 unit tests, notebook
  Step 2b): flags span-flattening + ragged rows over the parse cache; free regression
  gate for every future corpus. It reports *suspects*, not verdicts.
- **Ingest semantics fix**: `ingest --force` no longer busts the parse cache (it
  silently clobbered the premium parse once); explicit `--reparse` is the paid path.
- **Agent guardrail** (`.claude/skills/rag_search/SKILL.md`): "Parsed-table caveats" —
  blank-cell-next-to-sibling-value means *possibly shared*, corroborate before
  committing; ragged pin tables → prefer prose.

Verification: affected eval questions 7/7; 10-question regression sample 9/10 (the one
miss is the pre-existing #30 equation-drop, unchanged); answer spot-check now returns
`VRRM | 40 | 40 | V`; offline suite 59 pass / 4 skip, ruff clean. Ledger delta: ~6
LlamaParse jobs (incl. 1 premium 33-pager + 2 wasted on the clobber bug), ~3 embed
batches, ~16 reranks.

### Flavor 3 — column shift from split/merged body cells (next session)

A third flavor, user-spotted in CD0603's *Electrical Characteristics* table: a header
column whose **body cells split/merge per row** (the test-condition/variant column)
shifts every later value one column left — VF Typ values (0.35/0.38) landed under
**Min**, Max came out entirely empty, one IRRM row lost its test condition. Cell counts
stay consistent, so the flavor-1/2 scanner checks pass it. The merged-cell instruction
did **not** fix it (standard parse kept the shift).

Fixes, layered like before:
- **Premium re-parse** of CD0603 aligned everything correctly (and recovered a
  per-variant grouping column), but replicated the section title into **every header
  cell** via `<br/>`. Rather than burn another premium call on an instruction LlamaParse
  may ignore, the pollution is stripped **in code**: `parse._strip_header_title_pollution`
  (≥2 header cells sharing one `<br/>`-prefix → strip it; a bare-title cell → blank).
  Applied on every `parse()` return (fresh + cache-hit), so cache files stay raw API
  output and the fix is retroactive — no new parse jobs, durable for future docs.
- **Scanner heuristic** (`column_shift_suspect`, +4 unit tests): EC-style tables (header
  has Min *and* Max, matched on the cell tail so raw polluted caches work) with ≥4
  well-formed rows where **Min is ≥80% filled and Max is 100% empty** → suspect. The
  corrected CD0603 parse scans clean; the old shifted shape is flagged.
- **Re-ingest** CD0603 with `--force` (reuses the cached premium parse — `--reparse`
  would clobber it with a paid standard parse): old shifted chunks deleted, 15 clean
  chunks stored.

The corpus scan now also flags 5 new suspects in 2 other docs (reviewed, left as-is):
SPX3819's JEDEC package-outline table is a **genuine** shift (`A`: 1.75 — the JEDEC MAX —
sits under NOM, MAX empty; mechanical dims, low retrieval stakes; premium re-parse if it
ever matters), and RM46's timing tables are mostly **legit min-only** rows (setup/cycle
times are minimums) with a couple of ambiguous rows — exactly the human-review queue the
scanner is meant to produce.

Verification: eval questions 0/1/46 → 3/3 recall@5; stored-chunk spot-check shows clean
headers, `VF | IF = 50 mA | … | Typ 0.35` and both IRRM rows with test conditions +
Max (1 / 50 µA); offline RAG suite 21 pass, ruff clean. Ledger delta: 0 LlamaParse jobs,
+1 embed batch (re-ingest), +0 query embeds / +3 reranks (cached eval).

**Residual (user-caught) + sidecar patches.** Even the premium parse left the *two
last-per-variant* VF rows shifted (200 mA → `Min 0.43 / Typ 0.5`, 300 mA →
`Min 0.47 / Typ 0.5`; ground truth via a pdfminer column dump of the PDF: Min empty,
Typ 0.43/0.47, **Max 0.5**). Row-level partial shifts like this are below the
table-level scanner heuristic's radar (Min was only 2/12 filled) and no instruction
fixes them — so `parse()` gained a **manual sidecar-patch layer** as the last-resort
rung of the ladder: `<hash>.patch.json` next to the cache file, a list of
`{find, replace, note}` applied on every read; an entry that doesn't match exactly
once is *skipped with a warning* (a `--reparse` invalidates patches safely instead of
mis-applying them). Cache files stay raw. CD0603's patch fixes the two rows
(re-ingested; eval 3/3; stored rows verified). Escalation ladder is now:
instruction → premium → sidecar patch, with the scanner + eyeballs as the detector.

## Verification trail

- Full eval output: recall@5 = 0.98, pass=true (gate 0.80).
- Real tool path: `execute_tool('rag_search', …)` returns cited results
  (NVT2008 Ron → "General description", RM46L852 lockstep → "Device Overview",
  scores ~0.9) through the production dispatch.
- Offline suite: **95 passed / 4 skipped** (live-Anthropic), ruff clean.
