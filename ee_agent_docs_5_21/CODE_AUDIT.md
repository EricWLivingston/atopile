# EE-Agent Branch Code Audit & Optimization Plan

## Context

The `feature/ee-agent` branch adds ~19K lines across four new subsystems that turn atopile
into an LLM-driven circuit-design agent: the Anthropic provider + dynamic model router
(`src/atopile/server/agent/_ee/`), a RAG knowledge base (`src/ee_agent_rag/`), a SPICE
simulation runner (`src/ee_agent_spice/`), and a human-readable KiCad schematic exporter
(`src/faebryk/exporters/schematic/`), plus a parametric diode picker fix.

This document is the output of a read-only audit (diff against `main`, merge-base
`619eda7f`) for **security vulnerabilities, bugs, token/cost efficiency, and logic errors**,
with optimization and quality-of-life suggestions. The goal is the most efficient and
effective circuit-design agent possible. A future session should treat each finding as a
self-contained work item: each has a `file:line`, the problem, and a concrete fix. Findings
marked **[verified]** were confirmed by reading the code directly; others come from
sub-agent analysis and should be re-confirmed before fixing.

### Known-good baseline (revert target)

This audit was taken against the following commit. If any fix below introduces widespread
failures, this is the last verified-working state to revert to:

- **Commit:** `f9bcf3a7` — clean tree, all session-24 work committed
- **Subject:** feat(rag): textbook chunker + app_notes corpus live + research-before-design prompting
- **Branch:** `feature/ee-agent` (merge-base with `main`: `619eda7f`)
- **Revert:** `git reset --hard f9bcf3a7` (or branch from it before applying fixes:
  `git switch -c ee-agent-audit-fixes f9bcf3a7`).
- **Prior baseline** (last commit before the session-24 work was committed):
  `ee33c80b` — *docs(ee-agent): passdown — dynamic routing + diode picker done; M6 tabled*.

---

> **Resolution status (session 25).** P0 (H1–H3), P1 (T1–T6), and P2 (B1–B9) are all
> **implemented** on `feature/ee-agent` with tests; `ato`-adjacent suites green
> (`test/ee_agent_rag`, `test/ee_agent_spice/test_runner`, `test/server/agent`,
> `test/exporters/test_schematic_*`) and ruff clean on all touched files. Per-finding
> notes are inline below (✅). P3/Q items remain open. Verification commands that need API
> keys (RAG recall eval, live SPICE/Anthropic) were **not** run here — see Verification.
>
> Landing spots for the straightforward fixes (divergent ones noted inline ✅):
> - **H1/H2** `store.py` — BM25 sidecar is JSON (`{ids,documents,metadatas,tokenized}`,
>   rebuilt via `BM25Okapi` on load, mtime-cached); write is tmp + `os.replace`.
> - **H3** `store.sparse_search(filter=…)` applies the filter post-hoc + warns when the
>   index is missing despite documents existing; `retriever.py` always passes the filter.
> - **T1** `provider_anthropic.complete` marks the last tool def `cache_control: ephemeral`.
> - **T3** `model_router._heuristic_tier` short-circuits short, no-keyword, no-active-design
>   turns to `simple` with no API call (mid-design always classifies).
> - **T6** `provider_anthropic._messages_to_text` builds the compaction input from the
>   message tail instead of `json.dumps(old)[:50000]`.
> - **B1** `tools_pyspice._run` resolves + `is_relative_to(project_root)` containment guard.
> - **B3** `runner._num`/`_ident` validate analysis params (SPICE-number regex, identifier
>   regex, ac-variation enum) before interpolation.
> - **B4** `errors._PATTERNS` adds floating_node / include_error / param_error / ic_error.
> - **B5** `chunk.approx_tokens` uses tiktoken if present else `ceil(len/3)`; `enrich` reuses
>   it for `token_count`.
> - **B6** `enrich._summarize_one` wraps the OpenAI call → `None` on failure (never aborts).
> - **B7** `placement._warn_if_cells_overlap` checks pairwise cell disjointness, warns.
> - **B8** `enrich.stable_chunk_id` hashes full content (not first 200 chars).

## P0 — Critical / High

### H1. Pickle deserialization of the BM25 index (RCE surface) **[verified]**
- **Where:** `src/ee_agent_rag/store.py:92` (write), `:129` (`pickle.loads(self.bm25_path.read_bytes())`).
- **Problem:** The BM25 index is persisted with `pickle` and loaded unconditionally. Anyone
  who can write to the BM25 dir (shared cache, synced vector store, compromised ingest)
  achieves code execution on load. Pickle deserialization of attacker-influenced bytes is RCE.
- **Fix:** Serialize the reconstructable state to JSON instead — `BM25Okapi` can be rebuilt
  from `{ids, documents, metadatas}` plus its tokenized corpus (already computed at
  `store.py:88`). Store tokenized docs + params as JSON and reconstruct `BM25Okapi` on load.
  If pickle must stay short-term, restrict file perms (0600) and document the trust boundary.

### H2. BM25 index write is not atomic **[verified]**
- **Where:** `src/ee_agent_rag/store.py:92` — `self.bm25_path.write_bytes(pickle.dumps(...))`.
- **Problem:** A crash/interrupt mid-write leaves a truncated index; the next `sparse_search`
  fails or silently degrades to dense-only (see H3).
- **Fix:** Write to `bm25_path.with_suffix(".tmp")` then `os.replace()` for an atomic rename.
  Pairs naturally with the JSON migration in H1.

### H3. Sparse-search failures degrade recall silently
- **Where:** `src/ee_agent_rag/store.py:131-135` (`sparse_search` returns `[]` when index
  missing/corrupt); `src/ee_agent_rag/retriever.py:139` (`... if not filter else []`).
- **Problem:** Two silent degradations to dense-only retrieval: (a) missing/corrupt BM25
  index, (b) **any** metadata filter disables BM25 entirely. Hybrid retrieval is exactly
  what catches rare lexical tokens (MPNs, acronyms) — losing it silently undercuts the
  recall@5=0.98 the corpus was tuned to.
- **Fix:** Log a warning when BM25 is unexpectedly absent. For filtered queries, apply the
  filter to BM25 results post-hoc (BM25 metadatas are already stored) instead of skipping
  sparse search.

---

## P1 — Token / Cost Efficiency (primary user priority)

### T1. Tool definitions are re-sent uncached every turn **[verified]**
- **Where:** `src/atopile/server/agent/_ee/provider_anthropic.py:448-459` marks only the
  *system prompt* with `cache_control: ephemeral`. Tool schemas in
  `tool_definitions_ee.py` (verbose, ~7-line descriptions each) are sent every request with
  no cache breakpoint.
- **Fix:** Add `cache_control: {"type": "ephemeral"}` to the **last tool definition** in the
  array (Anthropic caches the whole tools block up to the marked breakpoint). Order the
  cache breakpoints tools → system → conversation prefix. This is the single biggest
  per-turn token win since tools + system are stable across a session.

### T2. Verify the cache prefix actually persists across turns **[verified]**
- **Where:** `provider_anthropic.py` system-prompt builder + the per-turn request assembly.
- **Problem:** Ephemeral caching only pays off if the cached prefix (tools + system +
  early conversation) is byte-identical turn-to-turn. Dynamic routing (T3) swaps the
  `model` between turns — **cache is keyed per-model**, so alternating Haiku/Sonnet/Opus
  turns each miss the cache and re-bill the full prefix.
- **Fix:** Measure `cache_read_input_tokens` vs `cache_creation_input_tokens` in responses.
  If routing causes thrashing, consider pinning the cache-bearing prefix to one model, or
  only escalating model tier on multi-turn design phases rather than per-turn.
- **✅ Done (instrumentation):** `provider_anthropic._log_cache_metrics` logs read/creation
  per turn at DEBUG with the model id, so per-model thrash from routing is now observable.
  Pinning/escalation policy left as a follow-up pending real-traffic numbers.

### T3. Dynamic router adds a full extra LLM call per turn
- **Where:** `src/atopile/server/agent/_ee/model_router.py:97-119` — every user turn makes a
  classification call (`router_model`, 5s cap) before the real call.
- **Problem:** A Haiku classification round-trip per turn adds latency + tokens to *every*
  interaction, including trivial ones, and interacts badly with prompt caching (T2).
- **Fix options (cheapest first):** (a) Skip routing for short/obvious turns via a cheap
  heuristic (token length, presence of design keywords) and only call the classifier when
  ambiguous; (b) cache the tier for a turn-window rather than re-classifying mid-design;
  (c) use Anthropic structured output / a single token logprob instead of a full generation.

### T4. RAG over-fetches and over-reranks
- **Where:** `src/ee_agent_rag/config.py` (`DENSE_OVERSAMPLE=4`, `RERANK_CANDIDATES=30`),
  `retriever.py:138-145`.
- **Problem:** For `top_k=5` this fetches ~20 dense + 20 sparse and Cohere-reranks 30 docs
  per query. Cohere bills per doc; at eval scale (×1000 queries) this is real money, and
  it adds latency to every agent `rag_search`.
- **Fix:** Sweep `RERANK_CANDIDATES` down to 15–20 and `DENSE_OVERSAMPLE` to 2–3; keep only
  the smallest values that hold recall@5. Add the chosen values to `RAG_TUNING_SUMMARY.md`.
- **✅ Done (conservative):** `DENSE_OVERSAMPLE 4→3`, `RERANK_CANDIDATES 30→20` in
  `config.py` with a comment. NOT eval-swept here (needs OPENAI/COHERE keys) — both still
  comfortably cover `top_k=5`; run `python -m ee_agent_rag.eval.runner` to push lower.

### T5. Tool results returned to the model are not size-bounded at the source
- **Where:** SPICE summary (`ee_agent_spice/runner.py` probe selection ~`:244-253`),
  `rag_search` chunk payloads, schematic export summaries.
- **Problem:** Truncation only happens as emergency context-overflow recovery in
  `provider_anthropic.py:608`. Large tool outputs (all SPICE node vectors incl. internal
  nets; full chunk text ×k) are billed in full on the turn they're produced.
- **Fix:** Cap output at the tool boundary: SPICE should return summary stats (min/max/mean,
  key transitions) not raw vectors, and default to user-named probes only; `rag_search`
  should return trimmed chunk windows + citation, not whole sections.
- **✅ Already satisfied at source (verified):** `tools_rag._run` trims each chunk to
  `_MAX_TEXT_CHARS=1500` + citation; `ee_agent_spice.simulate` persists raw vectors to
  `.npz` and returns only per-probe `{min,max,mean}` summaries. No code change needed; the
  `provider_anthropic` shrink path remains the emergency backstop, not the primary bound.

### T6. Context-compaction summarizer serializes then discards 50K chars **[verified]**
- **Where:** `provider_anthropic.py:273` — `json.dumps(old, ...)[:50000]` then a full
  summarizer model call.
- **Problem:** Off the hot path (only fires on overflow) but expensive when it does. Whole
  history is JSON-serialized just to slice it.
- **Fix:** Build the summarizer input from the tail of the message list directly (bounded by
  message count/token estimate) rather than serialize-then-truncate; use the cheap
  `summary_model`.

---

## P2 — Bugs / Logic Errors

### B1. `pyspice_run` netlist path: no traversal guard
- **Where:** `src/atopile/server/agent/_ee/tools_pyspice.py:42-47` — relative paths joined
  to `project_root` then `read_text()` with no containment check.
- **Fix:** Resolve and assert the path stays within `project_root`
  (`path.resolve().is_relative_to(project_root.resolve())`); reject `..` escapes.

### B2. No timeout on ngspice execution (hang / DoS)
- **Where:** `src/ee_agent_spice/runner.py:173-203` — `ng.load_circuit()` / `ng.run()` under
  `_LOCK` with no wall-clock bound. A non-converging/stiff deck hangs the worker and blocks
  all other sims (lock held).
- **Fix:** Wrap the run in a wall-clock timeout (thread + join-with-timeout, or SIGALRM on
  Unix); on timeout, abort and return a `timeout` error type. Surface the limit in the
  `pyspice_run` skill so the agent picks sane `t_end`/`n_points`.
- **✅ Done (best-effort):** `_SIM_TIMEOUT_S` (env `EE_SPICE_TIMEOUT_S`, default 30s) bounds
  **both** `_LOCK.acquire(timeout=…)` (so a wedged run can't block the worker pool forever)
  **and** the run itself via a watchdog thread + `join(timeout)` → structured `timeout`
  error. Caveat documented in code: ngspice's C core can't be force-killed, so a timed-out
  worker thread lingers until it unwinds; cleanup (`destroy all`) is skipped while it's
  alive to avoid concurrent corruption. Skill updated with the budget + guidance.

### B3. SPICE `params` interpolated into the deck without validation
- **Where:** `src/ee_agent_spice/runner.py` control-card construction (~`:46-66`).
- **Problem:** `params` values (tran/ac/dc fields) are interpolated into control cards with
  no numeric/identifier validation; a malformed value breaks the deck or injects directives.
- **Fix:** Validate each analysis param against an expected type (float/int/enum) before
  formatting; reject non-numeric where numbers are required.

### B4. `classify_log` fall-through is too generic
- **Where:** `src/ee_agent_spice/errors.py:35-74` — common ngspice failures (floating node,
  undefined model/param, missing `.include`, `.ic` failure) fall through to `"unknown"`.
- **Problem:** The agent can't distinguish recoverable (add a `.model`) from structural
  (floating node) failures, so it retries blindly — wasted turns + tokens.
- **Fix:** Add 5–10 patterns for the common cases above with actionable rationale strings.

### B5. Approx token count undercounts → oversized chunks/embeds
- **Where:** `src/ee_agent_rag/chunk.py:35-36` — `len(text)//4`.
- **Problem:** BPE often exceeds chars/4 for dense technical text; chunks tagged "3000 tok"
  can exceed embedding/model limits, causing failed embeds or truncated context.
- **Fix:** Use `tiktoken` for the budget (or divide by 3 as a safety margin). Centralize so
  chunker and embed batching share one counter.

### B6. Summary-enrichment errors are unhandled and abort ingest
- **Where:** `src/ee_agent_rag/enrich.py:99-118` — `_summarize_one` has no try/except; one
  bad chunk kills the whole `enrich()`/ingest.
- **Fix:** Wrap the call, log + return `None` on failure, continue. (Low blast radius since
  summaries default off, but it's a footgun when enabled.)

### B7. Schematic placement disjointness is asserted in docs, not in code
- **Where:** `src/faebryk/exporters/schematic/kicad/placement.py:185-196` — shelf packing
  uses per-cluster bounding boxes but never validates final positions are pairwise disjoint;
  relies on `CELL_MARGIN` padding.
- **Fix:** Add a post-placement assertion/debug check that cluster boxes don't overlap and
  stay within `usable_width`; emit a warning (not a crash) if violated. Prevents silently
  overlapping symbols/wires in the human-readable schematic the agent ships.

### B8. `stable_chunk_id` can collide on repeated boilerplate
- **Where:** `src/ee_agent_rag/enrich.py:86-96` — id derived from first 200 chars +
  section + ordinal; identical opening text collides and Chroma rejects duplicate ids.
- **Fix:** Hash the full chunk content (or a content hash + ordinal), not the 200-char prefix.

### B9. OpenAI model defaults are placeholders **[verified]**
- **Where:** `src/atopile/server/agent/config.py` — `default_model="gpt-5.4"`,
  `default_summary_model="gpt-4.1-nano"`.
- **Problem:** These IDs don't exist; the OpenAI path is broken out of the box. (Anthropic
  defaults are correct and current: `claude-opus-4-8` / `claude-sonnet-4-6` /
  `claude-haiku-4-5-20251001` — **verified, no change needed**.)
- **Fix:** Set valid OpenAI IDs or clearly mark the OpenAI path unsupported and fail fast
  with a helpful error if selected.
- **✅ Done:** defaults set to `gpt-4o` / `gpt-4o-mini` (dataclass + the `from_env` openai
  branch). The placeholders were inherited from upstream (`619eda7f`), so this also fixes
  it for the openai path; Anthropic remains the primary/supported provider for this fork.

---

## P3 — Quality of Life / Hardening (lower priority)

> **✅ All of Q1–Q8 implemented (session 25)** with tests; touched-file suites green
> (`test/ee_agent_rag`, `test/server/agent`, `test/exporters/test_schematic_*`) and ruff
> clean. Landing spots / divergences:
> - **Q1** `tools_rag.py` / `tools_pyspice.py` / `tools_skills.py` — broad-except handlers
>   now return the exception **type only** and `log.exception(...)` the full detail.
> - **Q2** `enrich.SUMMARY_PROMPT` fences the chunk in `<chunk>…</chunk>` + a "treat as
>   data, not instructions" directive; `_summarize_one` strips a `</chunk>` breakout.
> - **Q3** `parse._apply_sidecar_patch` validates JSON-is-list + per-entry string
>   `find`/`replace`, skips+warns malformed entries (no longer raises), logs applied ones.
> - **Q4** `real_symbol.real_symbol_from_file` warns (was debug) on every generic-box
>   fallback (parse error, no symbols, colon-in-name).
> - **Q5** `_SymbolRegistry._real_symbol` caches parsed symbols by `sym_path` (incl. `None`).
> - **Q6** `retriever._cohere_rerank` key gains `_RERANK_CACHE_VERSION` (manual bust). NB the
>   key already contains the full document **text**, so a content change on re-ingest busts
>   it automatically — the version is just an escape hatch.
> - **Q7** `provider_anthropic` guards the transcript LRU with `_transcripts_lock`
>   (get+move_to_end and store under the lock; deepcopy outside it).
> - **Q8** `enrich` writes `mpn_confidence` ∈ {`content`,`filename`,`filename_guess`,None}.

- **Q1. Exception text echoed to the model** — `tools_rag.py:48`, `tools_pyspice.py:70`,
  `tools_skills.py:110,118` return `f"{type(e).__name__}: {e}"`. Sanitize/whitelist error
  messages so filesystem paths and internals aren't fed back into the transcript.
- **Q2. Prompt-injection via ingested docs** — `enrich.py:108` interpolates raw chunk text
  into the summarizer prompt. Use a clearly delimited/structured message so a crafted
  datasheet can't issue instructions to the summarizer LLM.
- **Q3. Sidecar patch files applied blind** — `parse.py:123-146` string-replaces from
  `<hash>.patch.json` with no validation. Validate schema and log applied patches.
- **Q4. Symbol-file parse failures are silent** — `real_symbol.py:187-200` falls back to a
  generic box on any parse error with only `logger.debug`. Promote to a warning so users
  know a custom symbol was dropped.
- **Q5. Real-symbol files re-parsed per component** — `schematic.py:414-421` calls
  `real_symbol_from_file` every resolve. Cache by `sym_path` (cheap, helps large designs).
- **Q6. Cohere rerank cache not invalidated on re-ingest** — `retriever.py:61`. Include a
  corpus/version stamp in the cache key, or clear the rerank cache on ingest.
- **Q7. Transcript LRU has no locking** — `provider_anthropic.py:61,176-179`. If the server
  handles concurrent sessions, guard `_transcripts` mutation with a lock to avoid races on
  `move_to_end`/`popitem`.
- **Q8. MPN regex/ filename fallback false positives** — `enrich.py:23-45,66-83`. Add a
  confidence field; metadata-only impact, but noisy filters.

---

## Branch-Wide Suggestions

1. **Move tools/system to a cached prefix and instrument cache hit-rate** (T1/T2). Biggest
   recurring cost lever; everything else is secondary until this is measured.
2. **Make tool outputs "agent-shaped" at the source** (T5): summary-first, bounded, with a
   stable schema. The agent reasons better over compact structured results and it directly
   cuts tokens.
3. **Replace pickle with JSON across the RAG persistence layer** (H1) and make all index
   writes atomic (H2) — small change, removes the only RCE surface and the corruption class.
4. **Add wall-clock timeouts to every external/long op** the agent can trigger: ngspice
   (B2), LlamaParse jobs (`parse.py`), Cohere/OpenAI calls. The agent should never be able
   to wedge a worker.
5. **Tighten the failure→retry loop**: better SPICE error classification (B4) and
   sanitized, structured tool errors (Q1) so the agent spends turns fixing the right thing
   instead of retrying blind.
6. **Document the trust model** in `ee_agent_docs_5_21/00_ARCHITECTURE.md`: the agent is
   trusted to author netlists/paths, but ingested documents and cached artifacts are not.
   Several findings (H1, Q2, Q3, B1) are really "untrusted-input boundary" gaps.

---

## Verification (when fixes are implemented)

- **RAG:** run `python -m ee_agent_rag.eval.runner` and confirm recall@5 holds at/above the
  baselines in `config.py` after T4 retune; run `test/ee_agent_rag/test_rag_pipeline.py` and
  `test/ee_agent_rag/test_table_fidelity.py`.
- **Pickle→JSON / atomic write:** add a round-trip test (build index, reload, assert
  identical sparse results) and a corruption test (truncate file → graceful rebuild).
- **Agent core:** `test/server/agent/test_anthropic_provider*.py`, `test_model_router.py`,
  `test_provider_parity.py`. For caching, assert `cache_read_input_tokens > 0` on the second
  turn of a session.
- **SPICE:** `test/server/agent/test_ee_pyspice_tool.py`, `test/ee_agent_spice/test_runner.py`;
  add a non-converging deck to confirm the new timeout (B2) returns a `timeout` error.
- **Schematic:** `test/exporters/test_schematic_placement.py` — add an overlap assertion case
  for B7.
- **Picker:** `test/libs/picker/test_pickers.py` for the diode parametric pick.
- **Full suite:** `ato dev test` per `CLAUDE.md`.

*Note:* sub-agent findings that were **checked and found to be non-issues** and excluded:
parse-cache path traversal (filename is hashed), placement grid alignment (`CELL_MARGIN` is a
multiple of `GRID_SNAP`), unnamed-net label handling (correctly guarded), LCSC User-Agent
workaround (hardcoded, safe). The diode "ghost component" picker fix **is present and
correct** at `Diode.py:48-59` (one sub-agent wrongly reported it missing).
