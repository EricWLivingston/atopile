# EE Agent — Option C Passdown

> **Purpose.** Track the EE-agent build under **Option C** (`09_HARNESS_ANALYSIS.md`):
> fork atopile, add an `AnthropicProvider` next to `OpenAIProvider` (switchable via
> config), reuse atopile's agent runner as-is, and add three custom tools
> (`rag_search`, `pyspice_run`, `ipc_check`). Out of scope: BOM tool, thermal tool,
> deepagents, LangGraph state machines.
>
> This file is the living memory across sessions. It is organized topically:
> **Status → Reusable facts → Lessons & gotchas → Completed work → Open items.**
> The dated progress log at the bottom is just an index.

---

## Status at a glance

| Milestone / feature | State |
|---|---|
| M1 — fork bootstrap | ✅ done & pushed |
| M2 — `AnthropicProvider` (dual provider, stateful, tested) | ✅ done & pushed |
| **Schematic emitter** (label mode + wire/ladder mode) — *not on the original roadmap; built on request* | ✅ done & pushed |
| **Schematic — hierarchical real-symbol mode** (sheet per `.ato` module, labels = valid netlist; now the build default) | ✅ done (working tree) |
| M3 — tool-registration plumbing (`ee_ping` smoke + 3 real-tool scaffolds) | ✅ done & pushed |
| **M4 — `rag_search` retriever** (Chroma + LlamaParse + OpenAI embed + Cohere rerank; datasheets-only v1) | ✅ done & committed |
| M5 `pyspice_run` · M6 `ipc_check` — *registered stubs; bodies TBD* | ⏳ next (M5) |
| M7 — end-to-end design + eval | ⬜ not started |

Fork: **`github.com/EricWLivingston/atopile`**, branch **`feature/ee-agent`**. `main` is
left clean to track upstream. All committed work is on the fork.

Doc set lives in `ee_agent_docs_5_21/` (`00`–`14` + RAG/ingestion). Most relevant:
`00_ARCHITECTURE` (Option C overview), `07_ATOPILE_GAPS` (upstream bugs/contrib
candidates), `08_PROJECT_PLAN` (7 milestones), `11_ANTHROPIC_PROVIDER` (provider
reference), `12_DEV_TEST_HARNESS` (test tiers), `13_KICAD_SCH_AND_FRONTEND_FILES`
(schematic emitter design + frontend caveat).

---

## Reusable facts (still true; check before relying on)

**Repo / git.** `origin` → the fork over **HTTPS** (no SSH key on this machine —
`git@github.com` fails `Permission denied (publickey)`); `upstream` →
`atopile/atopile` with `--push upstream no_push`. `uv run ato --version` ≈
`0.14.1004.post1.dev76`.

**Python install is editable** (`uv pip show atopile` → editable at repo root, venv is
**Python 3.14**). Edits under `src/atopile/**` and `src/faebryk/**` take effect on next
backend restart, no reinstall. `ato` is **not** on PATH — use `uv run ato …` or
`source .venv/bin/activate`.

**Agent runner seams** (`src/atopile/server/agent/`): runner is provider-agnostic;
`LLMProvider` is a `Protocol` — the provider is the extension point. Runner is a
**module-level singleton** built in `routes/agent/utils.py` from `AgentConfig.from_env()`
at import time → env changes after server start don't propagate, and **tests must not
import `routes.agent.utils`**. Every turn logs to `~/.atopile/agent_logs.sqlite`
(`model/sqlite.py::AgentLogs`) — primary post-mortem.

**Provider switch.** `EE_AGENT_PROVIDER=openai|anthropic` (default `openai`, so upstream
behavior is preserved). Anthropic creds from `ATOPILE_AGENT_ANTHROPIC_API_KEY` /
`ANTHROPIC_API_KEY`; default model `claude-sonnet-4-6`.

**Tests.** Agent tests at `test/server/agent/`; exporter tests at `test/exporters/`.
`test/` is already in `pyproject` `testpaths` — don't add new top-level test roots.
Live Anthropic tests are behind a `integration` marker and auto-skip without a key.
`ato dev test --llm` produces `artifacts/test-report.{json,html,llm.json}`.

**Dev loop (running the unreleased agent UI in real VS Code).**

| Change type | Rebuild | Apply |
|---|---|---|
| Python `src/atopile/**`, `src/faebryk/**` | none (editable) | reload VS Code window |
| Extension host TS `src/vscode-atopile/src/**` | `npm run compile` | reload window |
| Webview React `src/ui-server/src/**` | `npm run build:webviews` | reload window |
| Ship a VSIX | all above + `npx vsce package` + `code --install-extension` | per release |

`kicad-cli` for schematic validation: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`
(v10.0.3).

---

## Lessons & gotchas (the *why* — most valuable to carry forward)

- **uv stale local-project metadata cache.** A bulk `uv add` of EE deps wrote them to
  `pyproject.toml` but `uv` resolved a cached package set and **never persisted them to
  `uv.lock` or installed** (`Using cached metadata for: atopile @ file://…`); `uv sync`
  was a silent no-op. Fix: **`uv lock --refresh`** to bust the cache, then `uv sync`. Also
  scope deps to what's needed now — several EE deps (voyageai/cohere/qdrant/pyspice/
  llama-parse) likely lack **Python 3.14** wheels, which probably triggered the bad resolve.
- **Anthropic Messages API is stateless; the runner assumes OpenAI's stateful Responses
  API.** After turn 1 the runner sends only the per-turn *delta* and relies on
  `previous_response_id` to rebuild history. The first `AnthropicProvider` ignored that →
  turn-2 sent a lone `tool_result` with no preceding `tool_use` → **Anthropic 400
  (orphaned tool_result)**; the error didn't match the chain-recovery snippets, so the run
  failed right after the checklist (the "freeze"). Fix: the provider keeps its **own
  transcript store** keyed by response id and rebuilds the full conversation each call (see
  Completed work → M2). This is the model for any future stateless provider.
- **API key delivery to the non-interactive shell.** Keys set in the user's interactive
  shell aren't visible to the tool shell. Resolved with a gitignored project-root **`.env`**
  sourced explicitly (`set -a; . ./.env; set +a; uv run pytest …`). `conftest.py` reads
  `os.getenv` directly (no dotenv), so the key must already be in the process env.
- **`kicad.dumps` for schematics is broken** (`07_ATOPILE_GAPS.md` §2.11, proven with
  kicad-cli 10.0.3): re-dumping a known-good `.kicad_sch` fixture yields a file KiCad
  refuses to load (drops `(symbol_instances)`/`(sheet_instances)`, mis-emits `(symbol …)`).
  So the emitter writes **sexp text directly**; the typed model is used only to *read*
  (parse `.kicad_sym` for geometry). The *read* side is fine.
- **Symbol-format version mismatch.** Cached `.kicad_sym` are KiCad-9 grammar
  (`20241229`); embedding them verbatim into a `20211123` schematic makes KiCad reject the
  file. Fix: **regenerate** the symbol in the schematic's own embedded grammar from the
  parsed typed model. Rule: lib-symbol name must equal the instance `lib_id`
  (`atopile:<name>`), child unit symbols keep the bare `<name>` prefix; symbol Y flips on
  instantiation, so a symbol-space pin `(px, py)` maps to schematic `(ix+px, iy−py)`.
- **Wire routing can silently short nets** (connectivity is geometric: a wire over another
  pin, or a junction where nets cross, merges them). The ladder design is **provably
  short-free** by construction — every pin gets a globally unique x-lane and the routing
  channel holds no pins, so a drop can only *cross* another net (no junction) — and this is
  **verified**, not just argued, via `kicad-cli sch export netlist` membership assertions.
- **easyeda.com returns HTTP 403 (CloudFront) during part-picking**, breaking builds for
  network reasons unrelated to our code. Worked around in `lcsc.py` with a browser
  User-Agent. A cleaner upstream fix (clear error instead of cryptic `JSONDecodeError`)
  lives as a local commit in `easyeda2kicad.py` and is **not** pushed (no personal fork of
  that repo yet).
- **macOS VS Code translocation (user-side env).** `Visual Studio Code.app` outside
  `/Applications` gets Gatekeeper-translocated to an ephemeral mount, breaking the
  `/usr/local/bin/code` symlink. Permanent fix: move the app into `/Applications`,
  `xattr -dr com.apple.quarantine`, relaunch, re-run "Install 'code' command in PATH".
- **No circular re-export.** `AnthropicProvider` is imported directly from
  `_ee.provider_anthropic` in `utils.py`; do **not** re-export it from `provider.py`
  (that creates `provider.py → _ee → provider.py`).
- **LlamaParse SDK is unusable on Python 3.14.** `llama-parse`'s `llama_cloud` dependency
  does `import pydantic.v1`, and pydantic 2.12's bundled v1 shim is broken on 3.14
  (`llama_cloud.types` fails at *import* with a `UndefinedType` validator error). chromadb /
  cohere / rank-bm25 / pdfminer all import fine. Fix: **dropped the `llama-parse` package**
  and call the LlamaParse REST API directly with httpx (`ee_agent_rag/parse.py`) — same
  service / `LLAMA_CLOUD_API_KEY` / `parsing_instruction`, minus the broken (huge) tree.
  This is the template for any future LlamaIndex-adjacent dep on 3.14.
- **Chroma metadata is scalar + non-null only.** No `None`, no lists. `store._scrub` drops
  `None` (e.g. empty MPN) and JSON-encodes lists, or upsert raises. Always scrub on write.
- **`uv add` stale-cache trap struck again** for the RAG deps (landed in `pyproject` but not
  `uv.lock`); `uv lock --refresh` then `uv sync` fixed it, as before.

---

## Completed work (condensed)

### M1 — fork bootstrap ✅
Fork + remotes as above; branch `feature/ee-agent`. Trimmed the roadmap from 4 tools to
3 (dropped `pinmux_check`) → 7 milestones, `<$185` budget. Skills used as-shipped (no
`.claude/skills/*` changes).

### M2 — `AnthropicProvider` (dual provider, stateful) ✅ pushed
- `src/atopile/server/agent/_ee/provider_anthropic.py` — implements the `LLMProvider`
  protocol; translates OpenAI↔Anthropic message/tool shapes; reuses the real
  `_extract_text`/`_extract_function_calls`/`_extract_output_phase` from
  `orchestrator_helpers.py`; client-side compaction (no server-side `responses.compact`).
- **Stateful emulation of `previous_response_id`:** an LRU transcript store
  (`_MAX_TRANSCRIPTS=64`) keyed by minted response id; `complete()` rebuilds the full
  conversation (deep-copied so retries/shrinking don't mutate history) and stores the
  assistant turn so the next delta's `tool_result` pairs with a real `tool_use`. Unknown
  `previous_response_id` → raise a message containing `"previous_response_id"` so the
  existing `run_turn_with_chain_recovery` retries from full local history; empty deltas /
  assistant-terminated transcripts get a `"Continue."` user turn.
- **Wiring:** `config.py` gained `provider` + `EE_AGENT_PROVIDER` branch (anthropic
  `base_url=""` → SDK default, which is what stops Claude from hitting the OpenAI
  endpoint); `routes/agent/utils.py` `_make_provider()` selects the provider. Default
  OpenAI path unchanged.
- **Tests** at `test/server/agent/`: 14 offline unit (translation helpers), 1 parity
  (both providers, monkeypatched request seam, equal normalized output), 4 offline state
  (the freeze fix), 3 live integration. Offline 19 pass / 4 skip; live user-confirmed
  end-to-end (agent proceeds past the checklist into real multi-turn tool use).

### Schematic emitter ✅ pushed (`src/faebryk/exporters/schematic/`)
Atopile had **no** Python `.kicad_sch` writer; this adds one. Emits **sexp text**
(because `kicad.dumps` is broken — see Lessons). Build step `generate_schematic` in
`build_steps.py` (`@muster.register("schematic", dependencies=[prepare_nets],
produces_artifact=True)`, in `generate_default`'s deps) writes
`config.build.paths.output_base.with_suffix(".kicad_sch")` — the exact path
`domains/manufacturing.py` surfaces as `outputs.kicad_sch`, so it auto-integrates (no
route/frontend changes).

- **IR extraction:** components = `has_designator` implementors
  (`Traits.bind(des).get_obj_raw()`); value via `has_simple_value_representation`; pads
  via `has_associated_footprint.get_footprint().get_pads()` (`pad.pad_number`); net per
  pad via a reverse map from `F.Net…get_instances(g)` → `get_connected_pads()` (`is_pad`
  hashes by node uuid, so net-pad and footprint-pad compare equal).
- **Label mode** (`render`, `draw_wires=False`): grid of symbols with a `global_label`
  per pin. Uses **real cached symbols where available** — `real_symbol.py` regenerates the
  `.kicad_sym` into the schematic's native grammar (see Lessons); generic box fallback
  (`generic_symbol.py`). Symbol located by `is_atomic_part.symbol` or by
  `has_part_picked` mfr/partno → `<Mfr>_<Partno>/*.kicad_sym`.
- **Wire mode** (`render_wired`, **the build default**): generic **bottom-pin boxes**, one
  global x-lane per pin, each net drawn as a horizontal **trunk** + vertical **drops** +
  **junctions** in the channel below, one net label per trunk. Provably short-free (see
  Lessons). Real symbols are label-mode only.
- **Verified:** 11 exporter tests (incl. two `kicad-cli sch export netlist`
  no-shorts checks); `ato build examples/i2c` → KiCad loads + renders, ERC **0 errors**
  (only benign `atopile`-nickname warnings), netlist reproduces exact nets
  (hv=6, lv=4, SDA/SCL/Alert=2).

### M3 — tool-registration plumbing ✅ pushed (`src/atopile/server/agent/_ee/`)
Wires the EE tool surface through atopile's existing machinery; **no tool logic yet**.
- **How a tool becomes live (the seams):** a handler via `@_register_tool(name)` in
  `_TOOL_HANDLERS` (dispatched by `execute_tool`, `tools.py:2068`) **and** a schema in
  `get_tool_definitions()`. The runner sends *all* `ToolRegistry.definitions()` to the
  model (`runner.py:440,565`) — no mediator gate on exposure. `_ensure_tool_registry_
  consistency` (`tools.py:569`) enforces schema⇔handler parity at first call, else raises.
  `mediator_catalog._TOOL_DIRECTORY` is a non-gating discovery/suggestion list. No
  per-tool policy allowlist (policy gates file paths only).
- **EE code stays in `_ee/`:** `tools_ee.py` (4 `@_register_tool` handlers) +
  `tool_definitions_ee.py` (`get_ee_tool_definitions()` schemas). Two one-line core seams:
  a **bottom-of-`tools.py`** import (`from ._ee import tools_ee`) triggers handler
  registration (placed last so the back-import of `_register_tool` resolves); a splice of
  `*get_ee_tool_definitions()` in `tool_definitions.py`. Plus 3 `_TOOL_DIRECTORY` entries
  in `mediator_catalog.py` for the real tools.
- **Tools:** `ee_ping(message)→{ok,echo}` (throwaway smoke proof, not in the directory) +
  `rag_search`/`pyspice_run`/`ipc_check` registered with their **documented schemas**
  (`05_RAG`/`02_SIMULATION`/`04_VERIFICATION`) but **graceful stub bodies**
  (`{"ok": False, "error": "… not implemented yet (M4/M5/M6)"}`) so live runs degrade
  cleanly until the bodies land.
- **Verified:** `test/server/agent/test_ee_tools.py` (7 offline tests: consistency guard
  passes, all 4 schema'd+registered, `ee_ping` echoes, stubs return gracefully, real
  tools in `available_tool_names()`). Full agent suite 26 pass / 4 skip; ruff clean.

### M4 — `rag_search` retriever ✅ committed (`fe5517d2`)
Fills in the M4 tool body. Stack locked (no LangChain): **Chroma · LlamaParse · OpenAI
`text-embedding-3-large` · Cohere rerank**. New framework-agnostic package
**`src/ee_agent_rag/`** (knows nothing about the agent runner) + a thin agent wrapper.
- **Pipeline.** Ingest: `classify → parse(LlamaParse REST) → chunk(section-aware ##) →
  enrich(MPN regex + deterministic chunk_id + optional OpenAI summary) → embed(OpenAI,
  dim-tunable) → Chroma + rank-bm25 sidecar`. Query (`retriever.rag_search`): dense(Chroma)
  + sparse(BM25) → **RRF fusion** → **Cohere rerank** → `{text, score, citation}`.
- **LlamaParse via REST** (`parse.py`), not the SDK — the SDK can't import on 3.14 (see
  Lessons). Parsed markdown cached by file hash under `data/.parsed_cache/`.
- **Wiring.** `_ee/tools_rag.py::run_rag_search` calls the sync retriever via
  `asyncio.to_thread` and degrades to `{ok:false,error}` on any failure (missing keys,
  empty index, import error) so a live run never crashes; `_ee/tools_ee.py` M4 stub swapped
  to delegate. Heavy deps imported lazily in the handler → server startup stays light.
- **Eval.** `eval/runner.py` recall@K gate (datasheets baseline **0.80**); seed dataset
  `eval/datasets/datasheets.jsonl` (4 Qs — **expand to ~30** for a real signal).
- **Tests.** `test/ee_agent_rag/test_rag_pipeline.py` (offline pure logic: chunk+pages,
  MPN, deterministic id, RRF, scrub, where-builder, tokenizer) + `test/server/agent/
  test_ee_rag_tool.py` (wrapper: query-validation, exception degradation, format/truncate,
  pass-through); M3 stub test updated (`rag_search` now live). **40 pass / 4 skip, ruff
  clean.** End-to-end retrieval quality is validated in the notebook + eval (needs keys).
- **Dev surface.** `notebooks/rag_pipeline.ipynb` — one cell per stage, `%autoreload`, the
  retrieval stages shown separately (dense/sparse/fused/reranked) for tuning. **gitignored**
  (`*.ipynb`). Full how-to/tuning/knob reference in **`16_RAG_NOTEBOOK_AND_TUNING.md`**
  (also gitignored, local-only). `.env.example` at repo root → copy to `.env` (gitignored):
  `OPENAI_API_KEY`, `LLAMA_CLOUD_API_KEY`, `COHERE_API_KEY`. Corpus PDFs go in
  `data/datasheets/` (`data/` gitignored).
- **Deps added:** `chromadb`, `cohere`, `rank-bm25`, `pdfminer-six` (+ `jupyterlab`,
  `ipykernel` dev). All resolve on 3.14.

---

## Open items

- **M5–M6 — implement the remaining tool bodies (next):** `pyspice_run` (M5),
  `ipc_check` (M6) are registered, schema'd, and model-callable as graceful stubs in
  `_ee/tools_ee.py` — fill in the logic there (M4's `_ee/tools_rag.py` → `ee_agent_rag/` is
  the template: real logic in a framework-agnostic package, thin wrapper delegates). The
  `ee_ping` smoke tool can be removed once a real tool proves the path in production.
- **M4 RAG — expand beyond datasheets (the main follow-up).** v1 is **datasheets-only**, one
  deterministic retrieval path. The pipeline is built for more corpora but they're not wired:
  - **Add data + corpus-specific parsers/chunkers.** `classify.py` already routes
    standards / app_notes / textbooks / internal by folder + first-page regex, and
    `config.DOC_TYPE_TO_CORPUS` maps them — but `chunk._CHUNKERS` only has datasheets/
    app_notes, so `chunk_dispatch` raises `NotImplementedError` for the rest. Each new type
    needs: a parser instruction in `parse._INSTRUCTIONS` (or reuse), a chunker (**standards**
    = clause-aware, one chunk per numbered clause `6.2.1`; **textbooks** = heading-aware
    ~600 tok; **internal** = heading-aware), type-specific enrich metadata
    (standards: `standard`/`revision`/`clause`; textbooks: `book`/`chapter`), and a few
    `MPN_PATTERNS`-style extractors. Then drop PDFs in `data/<corpus>/` and ingest.
  - **Standards corpus caveat:** IPC-2221A vs B differ — tag `revision`, prefer latest;
    most IPC standards are paid (use company-licensed copies / public MIL-STD for dev).
    This corpus is what M6 `ipc_check` will lean on, so it's a natural pairing.
  - **Then wire query routing:** v1 hardcodes/searches all corpora; add the Haiku corpus
    classifier (`05_RAG.md` pipeline) so a query hits the right collection(s).
  - Other deferred RAG bits: query rewriting/expansion, retrieving against the stored
    `summary` field, the `get_standard_clause` direct-lookup tool, byte-stable chunk-id
    determinism. Build the eval set per corpus (`RECALL_BASELINES` already has baselines).
- **Deferred deps** for later milestones, re-add with `uv lock --refresh` + a 3.14-wheel
  check: pyspice (M5 sim). (M4 RAG deps are **in**: chromadb, cohere, rank-bm25,
  pdfminer-six. `voyageai`/`qdrant`/`llama-parse` were **not** used — we went OpenAI embed /
  Chroma / LlamaParse-REST instead; do not re-add them.)
- **Schematic — hierarchical real-symbol mode (DONE, working tree).** Goal was
  human-readable schematics from `.ato` code. KiCad has **no** schematic autorouter /
  autoplacer / autoclean (`kicad-cli sch` = erc/export/upgrade only), so the chosen design
  is a **polished human-cleanup base**: real cached symbols placed on **one sheet per
  `.ato` module** (hierarchical sheets), with a `global_label` on every pin. The
  load-bearing invariant: a KiCad netlist is defined by label *names*, and **global labels
  connect across the whole sheet hierarchy** — so the schematic is electrically complete
  with **zero wires**, and a human can rearrange/route it without breaking the netlist
  (`kicad-cli sch erc`/`export netlist` verify at any point). All in
  `src/faebryk/exporters/schematic/kicad/schematic.py`: `build_sheet_tree`
  (groups components by `is_ato_module` via `get_hierarchy`/`get_implementors`),
  `render_hierarchical` / `render_sheet_tree` (root + child `.kicad_sch` files; instances
  centralised in the root's `(symbol_instances)` with nested `/<sheet>/<sym>` paths),
  `classify_nets` (power/ground via `ElectricPower.hv/lv`). `export_schematic` gained a
  `mode` flag (`hierarchical` default | `wired` | `labels`; legacy `draw_wires` maps on);
  `build_steps.py:generate_schematic` now selects `hierarchical`. **Verified:** 7 new
  exporter tests (cross-sheet netlist + classify_nets), ruff clean, and `ato build
  examples/i2c` → ERC **0 errors**, netlist exact (hv=6, lv=4, SDA/SCL/Alert=2) across the
  `temp_sensor` sub-sheet. Format was locked first with a kicad-cli spike (the 2-sheet
  `NET_SHARED` cross-sheet proof).
- **Schematic — power symbols (M3b, DONE, working tree).** Rail pins now render as real
  KiCad **power symbols** (GND triangle / power up-arrow) instead of labels; signal pins
  keep labels. `build_power_symbol` / `build_pwr_flag_symbol` in `generic_symbol.py`;
  `render_sheet_tree` gained a `net_roles` arg and a `_place_power` branch
  (`render_hierarchical` feeds it `classify_nets(app)`); `SchematicSummary.power_symbols`.
  **Key gotchas (kicad-cli-locked, spike `/tmp/pwr_spike/`):** a power symbol connects by
  the `(power)` flag **+ a `power_in` pin whose `name` is the net** — a `passive` pin
  *splits* the net (no name connection). A `power_in`-only net then errors
  `power_pin_not_driven`, so the emitter drops **one `PWR_FLAG`** (a `power_out` driver,
  pin name `~`, connects by geometry) atop the first pin of each rail net → ERC back to
  **0 errors**. +4 tests (23 total in the file); `ato build examples/i2c` ERC 0 errors,
  netlist still exact, hv→arrows / lv→triangles, one PWR_FLAG per rail. (Real-symbol pins
  are typed `Unspecified` in the cached `.kicad_sym`, so a handful of benign `pin_to_pin`
  *warnings* remain — pin-metadata only, not connectivity.)
- **Schematic — deterministic filenames + cleanup (M3c, DONE, working tree).** Child sheet
  files were named `<root>-<module>-<8 hex>` where the hex was a fresh `uuid4()` **per
  build**, so rebuilds piled up duplicate files for the same module. Fixed in `schematic.py`:
  `render_sheet_tree._assign` now derives `file_stem` from the module's **sanitized name
  path** (`default-temp_sensor.kicad_sch`, no hex; `seen_stems` adds a deterministic `-2` on
  collision); `sheet.uuid` is still random but only used internally (the `(sheet)` block +
  `(symbol_instances)` path). `export_schematic` (hierarchical) now **deletes stale**
  `{stem}-*.kicad_sch` not in the current output (root has no `-`, never matched).
  **Verified:** `ato build examples/i2c` twice → exactly `default.kicad_sch` +
  `default-temp_sensor.kicad_sch` both times, ERC 0 errors; +3 tests (26 total), ruff clean.
  (Deferred: making the internal uuids deterministic for byte-identical rebuilds.)
- **Schematic follow-ups (optional):** real symbols in *wire* mode; agent
  `schematic_export` tool (deferred; pattern in `tools.py` / `tool_definitions_project.py`);
  **Q2 image export** (`kicad-cli sch export svg/pdf` wrapper — trivial now the schematic is
  readable). Stale child `.kicad_sch` from a prior build aren't cleaned up (harmless; KiCad
  loads only referenced sheets). Upstream: fix `kicad.dumps` schematic write (`07` §2.11).
- **`12_DEV_TEST_HARNESS.md` §2** still owes a Tier-3 caveat: the browser
  `ato serve frontend` file explorer is a no-op (calls `postToExtension`, which only
  works inside a VS Code webview). The chat panel works in a browser; the file explorer
  needs the extension. (The other two old Session-2 doc edits — schematic exclusion in
  `00`, emitter candidate in `07` — are now moot/done since the emitter shipped.)
- **easyeda2kicad** clear-error fix: push once a personal fork of
  `atopile/easyeda2kicad.py` exists.

---

## Progress log (index)

- Sessions 1–2 — full doc set written. No source modified.
- Session 3 — local VSIX built/installed; dev-loop table established; AnthropicProvider
  edit surface mapped against source.
- Session 4 — M1 fork; pinmux dropped (4→3 tools); `AnthropicProvider` landed additively;
  uv stale-cache trap resolved (`anthropic==0.105.2` locked).
- Session 5 — M2 wired + full test suite; live Anthropic green (working tree only).
- Session 6 — M2 hardened: fixed the multi-turn freeze (stateful transcript); first
  commits pushed to the fork (+ easyeda 403 picker workaround).
- Session 7 — schematic emitter investigated; `kicad.dumps` blocker found; text-emitter
  spike validated; paused (docs only).
- Session 8 — schematic emitter BUILT (label mode, real-symbol regeneration); i2c loads,
  ERC-clean; 7 tests; pushed.
- Session 9 — schematic emitter draws net wires (ladder routing, wire mode default);
  provably + verifiably short-free; 11 tests; pushed.
- Session 10 — M3 tool-registration plumbing: `ee_ping` smoke + `rag_search`/
  `pyspice_run`/`ipc_check` registered as schema'd, model-callable graceful stubs in
  `_ee/`; 7 tests; pushed.
- Session 11 — schematic **hierarchical real-symbol mode** (human-cleanup base): sheet per
  `.ato` module, labels carry connectivity (valid netlist, no wires), now the build
  default; format locked via kicad-cli 2-sheet spike; i2c ERC-clean + exact netlist; +7
  tests (18 total), ruff clean. Working tree only.
- Session 12 — schematic **power symbols (M3b)**: rail pins → GND-triangle / power-arrow
  glyphs + one `PWR_FLAG` driver per rail (clears `power_pin_not_driven`); grammar locked
  via kicad-cli spike; i2c ERC-clean + exact netlist; +4 tests (23 total), ruff clean.
  Working tree only.
- Session 13 — schematic **M3c**: deterministic child-sheet filenames (no per-build hex)
  + stale-file cleanup, fixing duplicate `<module>` files piling up across builds; i2c
  double-build → stable two files, ERC-clean; +3 tests (26 total), ruff clean. **Schematic
  work (sessions 11–13) still not committed/pushed.**
- Session 14 — **M4 `rag_search` retriever** built end-to-end: framework-agnostic
  `ee_agent_rag/` package (Chroma + LlamaParse-REST + OpenAI embed + Cohere rerank, no
  LangChain), agent wrapper `_ee/tools_rag.py`, eval runner, step-by-step tuning notebook
  (gitignored) + local `16_RAG_NOTEBOOK_AND_TUNING.md`. Cleared the LlamaParse-SDK-on-3.14
  blocker (REST workaround). 40 pass / 4 skip, ruff clean; **committed `fe5517d2`**
  (datasheets-only v1; needs keys + PDFs + a ~30-Q eval to tune for real).
- **Next** — tune M4 on a real datasheet corpus (keys + ~10 PDFs + expand eval), **and/or**
  expand RAG to standards/app_notes/textbooks corpora (see Open items), **then** M5
  (`pyspice_run` body). Q2 schematic image export still optional.
