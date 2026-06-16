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
| **Schematic — hierarchical real-symbol mode** (sheet per `.ato` module, labels = valid netlist; now the build default) | ✅ done & pushed |
| **Schematic — human-ready mode** (connectivity-clustered placement, bbox-aware cells, power/ground auto-wired via stub+oriented glyph, oriented labels, paper/title) | ✅ done & committed |
| M3 — tool-registration plumbing (`ee_ping` smoke + 3 real-tool scaffolds) | ✅ done & pushed |
| **M4 — `rag_search` retriever** (Chroma + LlamaParse + OpenAI embed + Cohere rerank; datasheets-only v1) | ✅ done & committed |
| **M4 tuning + table-fidelity hardening** (14-doc corpus, 50-q eval, recall@5 = 0.98; 3 parse-defect flavors fixed: instruction → premium → sidecar patches; `table_fidelity` scanner) | ✅ done & committed |
| **M5 — `pyspice_run` runner** (PySpice + libngspice; agent-authored netlist; passives + sources + discretes + `OPAMP_IDEAL`) | ✅ done & committed |
| **Skill-discovery tools** (`skills_list`/`skill_read` + per-tool guidance skills) | ✅ done & committed |
| **Agent run-log viewer** (Logs-tab "Agent" mode; streams `agent_events`) — *off-roadmap; built on request* | ✅ done & committed |
| **Dynamic model routing** (per-turn complexity classifier → Haiku/Sonnet/Opus tiers; `EE_AGENT_DYNAMIC_MODEL=1`, anthropic-only, off by default) | ✅ done & committed |
| **Diode auto-picking** (DIODES endpoint + `is_pickable_by_type` on `Diode`; fixes the silent "ghost component" drop) — *off-roadmap; user-reported* | ✅ done & committed |
| M6 — `ipc_check` — *registered stub; body TBD* | ⏸ tabled (user call, session 22) |
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
- **`.env` discovery was cwd-relative → silent "thinking…" freeze.** `config.from_env()`
  loaded the `.env` via `find_dotenv(usecwd=True)`, which walks up from the **backend's cwd
  = the *opened project* root**, normally *outside* the atopile checkout — so the fork's
  repo-root `.env` was never found, `EE_AGENT_PROVIDER` stayed unset → **defaulted to
  `openai` with no key**, and the run failed instantly with `run_failed: No API key
  configured` (visible in the agent log, **not** surfaced in the UI — the chat just sits on
  "thinking…"). Fix: `from_env()` now also loads the source-tree repo-root `.env`
  (`Path(__file__).resolve().parents[4]/".env"`) as a fallback after the cwd search
  (`override=False`, so a project-local `.env` still wins). Post-mortem path correction: the
  agent log is **`agent_logs.db`** (not `.sqlite`) under **`get_log_dir()`** =
  `~/Library/Logs/atopile/` on macOS — `sqlite3 … "SELECT timestamp,level,event,summary FROM
  agent_events ORDER BY id DESC"`. (Separate latent UX bug, not yet fixed: a `run_failed`
  shows as a perpetual "thinking…" instead of surfacing the error to the chat.)
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
  Same discipline in the human-ready mode: power stubs are **straight only** — L-shaped
  elbows toward "pretty vertical glyphs" can land exactly on the adjacent pin's stub end
  (2.54-pitch pin rows) and short *different* rails, so they were rejected. Straight stubs
  + disjoint placement cells (each = bbox + 15.24 mm margin > stub 5.08 + glyph 2.54) are
  short-free by construction; `test_schematic_placement.py` asserts cell disjointness.
- **KiCad property text angle is *relative* to the instance rotation.** A rotated power
  glyph rendered its net-name text vertical even with `(at x y 0)` on the property; the
  emitter compensates with `(360 − rot) % 360` so text stays horizontal in all four glyph
  orientations (`_power_instance_block`).
- **Late grid-snapping breaks geometric invariants.** Snapping each placed item's absolute
  origin independently shifts neighbors by ≤1 grid step and can collapse the inter-cell
  gap. Rule: make all placement *inputs* lattice-valued (extents rounded up at
  construction) so positions are sums of lattice values and never need late rounding
  (`placement.py` module docstring).
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
- **Per-call model override, never provider/config mutation.** The runner + provider
  are a module-level singleton shared by all sessions, so dynamic model selection must
  flow as a `model` kwarg through `LLMProvider.complete()` (both providers do
  `model or self._config.model` at payload-build); mutating config would race across
  concurrent sessions. Switching model mid-chain is provably safe on the Anthropic
  path (the transcript store rebuilds the full conversation per call and doesn't
  condition on model — API-verified: payload model == served model). The OpenAI
  Responses chain references server-side state created under one model and is
  **unverified** → `dynamic_model` is forced off for `provider != "anthropic"`.
  Router rules: fail-open to "standard" on any error/timeout (~5 s cap), and never
  downshift to "simple" while a design is in progress (parser-enforced, not just
  prompt-requested). NB the in-file `TestRunner` cases drive `run_turn` with
  duck-typed minimal configs → new config reads in the turn path need
  `getattr(cfg, ..., default)`.
- **The cwd trap struck a third time: `ee_agent_rag.config.DATA_ROOT` defaulted to
  `./data` (cwd-relative).** The backend's cwd is the *opened project*, so the agent's
  `rag_search` in production would have opened an empty store at `<project>/data/.chroma`
  and degraded to `{ok:false}` (all prior end-to-end checks ran from the repo root and
  masked it). Fix: default now anchors at the source tree
  (`Path(__file__).parents[2]/'data'`); `EE_DATA_ROOT` still overrides. Rule of thumb by
  now: **any default path in code the backend imports must be source-tree-anchored, never
  cwd-relative** (same class as the session-17 `.env` fix).
- **Chroma metadata is scalar + non-null only.** No `None`, no lists. `store._scrub` drops
  `None` (e.g. empty MPN) and JSON-encodes lists, or upsert raises. Always scrub on write.
- **Don't trust LlamaParse instruction-following for structure you can synthesize.** It
  silently ignored "insert `<!-- page N -->` markers" (0 markers emitted), and twice
  collapsed an equation region *plus its prose* into a `[Diagram: …]` placeholder
  (deleting "a 6.8 µH inductance is recommended" from the index — the only eval miss).
  Fix: fetch the **per-page JSON result** (`…/result/json`) and synthesize page markers in
  code (`parse.py`); treat parse-instruction wording as best-effort only.
- **Chroma rejects duplicate ids within one upsert.** `stable_chunk_id` collided on
  repeated headings with identical first-200-chars (table headers/boilerplate; 6 of 14
  docs failed). Fixed by adding the chunk **ordinal** to the id — still deterministic
  across re-ingests. Related: re-ingest now **deletes the doc's old chunks first**
  (`store.delete_source`), else content changes leave stale chunks behind.
- **`\b` never fires at `_`** (underscore is a regex word char) — filename-stem MPN
  matching silently failed / left trailing-`-` artifacts until stems got `_`→space
  treatment (`enrich.mpn_from_filename`). Every corpus PDF's MPN now resolves from
  content regex (12 new vendor patterns) with filename fallback.
- **LlamaParse flattens merged table cells & drifts columns** (user-caught): a value
  spanning multiple part-number columns lands under ONE column (CD0603 VRRM), and wide
  multi-variant pin tables shift values across columns (NVT2008) — silent spec
  misattribution with a confident citation. Layered fix: merged-cell replication rule in
  `DATASHEET_INSTRUCTION` (fixed CD0603), `parse(..., premium=True)` escalation for docs
  the instruction can't fix (fixed NVT2008's pin table), permanent suspect scanner
  `ee_agent_rag/eval/table_fidelity.py` (notebook Step 2b; reports *suspects* — sparse
  triangular matrices are legit), and a "Parsed-table caveats" section in the
  `rag_search` skill. **Related trap:** `ingest --force` used to also bust the parse
  cache and silently clobbered a premium parse with a standard one — re-ingest (`force`)
  and re-parse (`--reparse`) are now separate flags.
- **Flavor 3 of the LlamaParse table defects — column shift from split/merged body
  cells — is invisible to cell-count checks and unfixable by instruction.** A header
  column whose body cells split/merge per row (CD0603 EC table's test-condition/variant
  column) shifts every later value one column left; rows stay well-formed, so the
  flavor-1/2 scanner passed it, and the merged-cell instruction kept the shift. Premium
  parse fixed the alignment but replicated the **section title into every header cell**
  via `<br/>`. Per the "synthesize what you can't trust the instruction to do" rule, the
  pollution is stripped in code (`parse._strip_header_title_pollution`, applied on every
  `parse()` return incl. cache hits — cache files stay raw, fix retroactive, zero new
  parse jobs) and the flavor is now caught by a scanner heuristic
  (`column_shift_suspect`: header has Min+Max, ≥4 rows, Min ≥80% filled, Max 100% empty).
  **Even premium can leave individual rows shifted** (user-caught: CD0603's two
  last-per-variant VF rows kept `Min 0.43/0.47` that are really `Typ`, with `Max 0.5`
  dropped — verified against the PDF via a pdfminer column dump; row-level partial
  shifts are below the table-level heuristic's radar). Last-resort rung:
  `parse._apply_sidecar_patch` — a manual `<hash>.patch.json` beside the cache file
  (`{find, replace, note}` list, applied on every read; an entry not matching exactly
  once is skipped with a warning, so a `--reparse` invalidates patches safely). The
  escalation ladder is: instruction → premium → sidecar patch.
- **Cohere trial keys: 10 calls/min** → 429 mid-eval. `_cohere_rerank` now has 20/40/60 s
  backoff *and* a lossless disk cache (deterministic in model|query|docs|top_n); query
  embeddings are disk-cached too (`data/.embed_cache`, `.rerank_cache`). The cache files
  double as the paid-call ledger; repeated evals are free.
- **`uv add` stale-cache trap struck again** for the RAG deps (landed in `pyproject` but not
  `uv.lock`); `uv lock --refresh` then `uv sync` fixed it, as before. **And again for
  `pyspice` (M5)** — same fix. NB the `uv lock --refresh` re-resolution also *pruned* stale
  transitive entries (`sqlalchemy`/`tiktoken`/`nltk`/`tinytag`/`regex`) from the env; none are
  imported anywhere in `src/`/`test/` (grep-checked) and atopile+agent still import clean, so
  it's harmless lock hygiene, not breakage.
- **`02_SIMULATION.md`'s premise is wrong: atopile emits no SPICE netlist.** Grep-verified —
  no `.cir`, no ngspice integration, no SPICE models on any library part (the only "ngspice"
  hit is an unused field in the KiCad-netlist schema). So M5 is *not* "wrap PySpice around an
  existing `build/<target>/netlist.cir`." Decision (user-locked): **agent authors the SPICE
  netlist text** (the analog subcircuit it wants to verify, not the whole board); the tool
  signature gained a `netlist` string param alongside `netlist_path`. No graph→SPICE generator
  built (deferred). The schematic emitter's `extract_components()` remains the template if one
  is ever wanted.
- **libngspice isn't on macOS's default dyld path.** PySpice's `NgSpiceShared` dlopens
  `libngspice.dylib` by bare name → `OSError` on a brew install (`/opt/homebrew/lib`). Fix:
  `runner._ngspice_library_path()` discovers the abs path (brew dirs + `HOMEBREW_PREFIX` +
  `EE_SPICE_NGSPICE_LIB` override + `ctypes.util.find_library` fallback) and sets
  `NgSpiceShared.LIBRARY_PATH` (a `'…/libngspice{}.dylib'` template — the `{}` is the
  instance-id slot, '' for id 0). Needs a one-time `brew install ngspice` (libngspice 46).
- **ngspice's C core is a non-thread-safe process singleton, and PySpice needs a distinctly
  *named* shared lib per simultaneous instance (`libngspiceN.dylib`).** So don't
  `new_instance()` per call — keep **one** instance (id 0 → plain `libngspice`) behind a
  `threading.Lock`, reset with `exec_command("destroy all")` between runs. The agent wrapper
  already serialises via `asyncio.to_thread`; the lock makes concurrent tool calls safe.
- **Reading results out of `NgSpiceShared`.** After `load_circuit(deck)` + `run()`: vector keys
  are **bare node names** for voltages (`out`, not `v(out)`), `<src>#branch` for source currents
  (`v1#branch` for `i(v1)`), and the sweep axis is `time`/`frequency`/`v-sweep`. The `Vector`
  has **no `as_ndarray()`** — use `vec._data` (numpy array). Pick the newest non-`const` entry
  in `plot_names` (ngspice lists current-first). `runner._resolve` maps the agent's probe
  spelling onto these keys. PySpice prints a benign "Unsupported Ngspice version 46" warning.
- **PySpice raises its *own* exceptions, not just empty plots.** A bad/insoluble deck makes
  `run()` raise `NgSpiceCommandError` (e.g. singular matrix), *not* return an empty result.
  `_run_ngspice` catches any non-`NgspiceError` from `load_circuit`/`run`, classifies the
  captured ngspice log (`errors.classify_log`), and re-raises as a structured `NgspiceError`.
  Log capture is via a `send_char` override on the `NgSpiceShared` subclass.

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

### M5 — `pyspice_run` runner ✅ committed
Fills in the M5 tool body. Stack (user-locked): **PySpice 1.5 + libngspice 46**,
agent-authored netlist, passives + sources + **discretes** (bundled model lib). New
framework-agnostic package **`src/ee_agent_spice/`** (knows nothing about the runner) + a
thin agent wrapper — the M4 shape exactly.
- **Pipeline.** `simulate(netlist, analysis, params, probes, project_root)` → `build_deck`
  (title + agent body with trailing `.end` stripped + auto-`.include` bundled models +
  `.save` probes + analysis control card) → `_run_ngspice` (locked singleton
  `NgSpiceShared.load_circuit`/`run`, vectors via `vec._data`) → persist **all** vectors
  (probes + axis) to `build/sim/<run_id>.npz`, return **per-probe min/max/mean** only
  (context-safety, `02_SIMULATION` §5). Analyses: `op` / `dc` (sweep or → op) / `ac`
  (mag+phase via magnitude) / `tran` (`uic` supported).
- **Bundled models** (`models/ee_agent.lib`, auto-included): `Dgen`/`Dschottky`/`DLED`,
  `Q2N3904`/`Q2N3906`, `NMOS_GEN`/`PMOS_GEN` — first-order, topology-accurate not
  vendor-accurate; the schema tells the agent these names + to inline a vendor `.model` for
  accuracy. Lib path discovery + singleton/lock + result-key naming: see Lessons.
- **Wiring.** `_ee/tools_pyspice.py::run_pyspice` (async, `asyncio.to_thread`) resolves
  `netlist` text **or** `netlist_path`, degrades every failure (no netlist, `ImportError` =
  no PySpice, `OSError` = no libngspice, convergence) to `{success:false, errors:[…]}`;
  `_ee/tools_ee.py` M5 stub swapped to delegate (passes `project_root` as `project_path`).
  Schema in `tool_definitions_ee.py` updated: added `netlist`, made `netlist_path` optional,
  added `op` to the enum, required only `analysis`, documented the model names. Package added
  to `pyproject` wheel `packages`.
- **Tests.** `test/ee_agent_spice/test_runner.py` (offline pure logic: deck assembly, control
  cards, save targets, probe resolution, summaries, error classification) +
  `test/ee_agent_spice/test_sim_live.py` (live, `skipif` no libngspice — asserts real
  physics: divider bias 3.33 V, RC τ, RC low-pass passband, diode drop, convergence-failure
  handling) + `test/server/agent/test_ee_pyspice_tool.py` (wrapper degradation/pass-through);
  M3 stub test updated (`pyspice_run` now live). **Agent suite 70 pass / 4 skip** (the 4 are
  Anthropic live-integration), ruff clean.
- **Verified end-to-end** through the real `execute_tool('pyspice_run', …)` dispatch path: a
  2-transistor **astable multivibrator** (bundled `Q2N3904`, ~68k/10µF) oscillates at
  **1.13 Hz** — inside the M5 done-def 0.8–1.3 Hz band — railing 0.03↔5.01 V, `.npz`
  persisted. (Astables need a tiny C asymmetry + `uic` to start in SPICE.)
- **Deps:** `pyspice` (pulls scipy/cffi/ply) + system `brew install ngspice`. All on 3.14.
- **Dev surface.** `notebooks/pyspice_testbed.ipynb` (gitignored via `*.ipynb`): paste a
  netlist → deck preview → all-parameter summary table (`probes=[]` = every vector) →
  waveform plots from the persisted `.npz` (Bode for `ac`, traces for `tran`/`dc`, bars
  for `op`) → optional `execute_tool('pyspice_run', …)` pass. Sim outputs under
  `notebooks/sim_runs/` (also git-invisible). Includes a paste-ready examples gallery
  (divider/RC/rectifier/NMOS-sweep/astable). Verified by full `nbconvert --execute`.

### Skill-discovery tools (`skills_list` + `skill_read`) ✅ committed
On-demand agent skill library + per-tool guidance, so the agent can pull *how/when/scope*
guidance just-in-time instead of always-loading it (token-efficient) — and so we can nudge
proper tool use (e.g. don't simulate digital/datasheet-answerable/whole-board with
`pyspice_run`).
- **The gap.** The agent only ever loads the **3** `fixed_skill_ids` (`agent`/`ato`/
  `planning`) into context every turn (`context.py:load_required_skill_docs`); it **never
  scans `skills_dir`**, so the other 18 skill dirs were invisible and there was no on-demand
  fetch / guidance tool. Putting a doc in `.claude/skills/` does **nothing** for the agent by
  itself — discoverability comes only from a tool that reads it. (`.claude/skills/` is also
  *Claude Code's* skill dir, so new dirs there also show up as IDE-invokable skills — harmless.)
- **Tools (additive, `_ee/`):** `skills_list()` scans `config.skills_dir` → `[{id,
  description, always_loaded}]` (frontmatter `description`, fallback to first heading; the 3
  fixed ids flagged); `skill_read(skill_id)` returns the SKILL.md body (truncated via
  `_truncate_middle`, ~12 KB), degrading to `{ok:false, available:[…]}` on unknown id.
  `skill_read` rejects ids outside `^[A-Za-z0-9_-]+$` (path-traversal guard). Config via a
  bare `AgentConfig()` (all defaults; no dotenv/provider side-effects). Lives in
  `_ee/tools_skills.py`; handlers in `tools_ee.py`; schemas in `tool_definitions_ee.py`;
  directory entries in `mediator_catalog.py` (category `research`, `discovery`).
- **Per-tool guidance docs (real deliverable)** as ordinary skills in the same dir:
  `.claude/skills/{pyspice_run,rag_search,ipc_check}/SKILL.md`. `pyspice_run` is the governor
  (when/when-NOT/scope=minimal-subcircuit, models, params, probing, astable start-up tip,
  "a failed sim is feedback not a build failure"); `rag_search` (ground vs web_search,
  citations, datasheets-only caveat); `ipc_check` is a **seed** (M6 forthcoming, returns a
  stub). **Hook for future tools = drop `.claude/skills/<tool>/SKILL.md`** — auto-discovered,
  no code change.
- **Always-on nudge** (the bit that makes it fire): `# Tool Usage Recipes` in
  `.claude/skills/agent/SKILL.md` gained `## Skill Library` ("call `skills_list`, then
  `skill_read('<id>')` before a specialized task") + `## Verification & Simulation` (the
  pyspice gate). **Three reinforcing awareness layers** (the agent never scans a folder):
  (1) the runner sends *all* tool definitions+descriptions to the model every turn
  (`runner.py:440,565`) so it always sees `skills_list`/`skill_read`; (2) the always-loaded
  `agent/SKILL.md` Skill-Library nudge; (3) each guidance-bearing tool's **own description**
  ends with `call skill_read('<tool>') before first use` — added to **all three**
  (`pyspice_run`/`rag_search`/`ipc_check`) so they self-advertise their skill identically.
- **Verified:** `test/server/agent/test_ee_skills.py` (7 offline: discovery, fixed-flagging,
  body fetch, unknown→available, traversal-reject, frontmatter parse) + `test_ee_tools.py`
  consistency set updated. Agent suite **44 pass / 4 skip**, ruff clean. End-to-end via
  `execute_tool`: `skills_list`→24 skills (3 flagged), `skill_read('pyspice_run')`→5 KB body,
  and `build_system_prompt` renders the new recipes. **`pyspice_run` is not a build step**
  (grep-confirmed) — a wrong sim never fails `ato build`; worst case is wasted turns, which
  the guidance curbs.

### Agent run-log viewer (Logs tab "Agent" mode) ✅ committed
Surfaces the agent's **own run log** (`agent_events`) in the existing build-server Logs
viewer, so you can watch planning / tool calls / `run_failed` live alongside build & test
logs. Motivated by the silent-"thinking…" debug session: the run error was only in the DB,
never in the UI.
- **The gap.** The Logs viewer's `/ws/logs` only read **build** (`Logs`) and **test**
  (`TestLogs`) DBs; the agent's run events live in a *separate* DB (`agent_events` in
  **`agent_logs.db`**, `~/Library/Logs/atopile/`) the viewer couldn't reach. The viewer *did*
  have an "agent" choice — but that's the **audience** dropdown (`user|developer|agent`), a
  strict `WHERE audience='agent'` filter on build logs; **nothing ever emits an
  agent-audience build log** (all 76k build rows are `developer`), so picking it just showed
  an empty pane. (That audience filter is unrelated to this feature and is still effectively
  a dead option for build logs.)
- **Design.** New **`agent` `LogMode`** beside build/test. Agent rows are mapped onto the
  **shared entry shape** server-side (`Log.agent_row_to_entry`: `summary`→message,
  `tool_name`/`phase`→stage column, level/timestamp passthrough, grouped under
  `agent.<event>` loggers), so the **existing `LogDisplay` renders them with zero new row
  UI**. Session id is **optional**: blank → **follow-latest** mode. Level filter + live
  cursor reuse the build/test machinery.
- **Follow-latest auto-switch (backend-only).** In follow-latest mode `_push_agent_stream`
  **re-resolves `latest_session_id()` every poll** (cheap rowid-ordered `LIMIT 1`), so a
  viewer left open across runs always tracks the newest session — including a run that
  *starts after* the panel was opened. On a session change (incl. the first push) it sends
  the new session's full batch as an **`agent_logs_result` (viewer *replaces*)**; steady-state
  new rows for the same session go as **`agent_logs_stream` (*append*)**. The client stays
  dumb (no reset signal): the existing onmessage already replaces on `*_result` / appends on
  `*_stream`. Switch detection uses a `PrivateAttr _followed_session` on `AgentStreamQuery`;
  an **explicit** `agent_session_id` pins one session (no auto-switch). +2 tests (fake-WS
  drives replace→append→switch and the pinned-no-switch path). **Gotcha that motivated this:**
  every window reload/extension reinstall restarts the backend on a **new port**, dropping the
  panel's WS; the panel then showed a stale snapshot. Auto-follow + a fresh reconnect now
  re-replace with the current session.
- **Server.** `model/sqlite.py`: `AgentLogs.latest_session_id()` (`fetch_chunk` already
  existed, incl. `levels`/`after_id`). `dataclasses.py`: `Log.AgentQuery`/`AgentStreamQuery`/
  `AgentStreamEntryPydantic`/`AgentResult`/`AgentStreamResult` + the `agent_row_to_entry`
  mapper. `routes/logs.py`: `_push_agent_stream` + an `agent` dispatch branch (one-shot DESC +
  streaming ASC) selected by an explicit `agent: true` in the WS payload.
- **Frontend** (`src/ui-server/.../log-viewer/` + `LogViewer.tsx`): `'agent'` `LogMode`,
  agent entry/request/result types, `agent_logs_result`/`agent_logs_stream` handling in
  `useLogWebSocket` (+ `startAgentStream`/`buildAgentLogRequest`), the **"Agent" mode button**
  + optional `Session ID (latest)` input, and agent cases in the auto-stream effect / stage
  header.
- **Verified.** `test/server/test_logs_agent.py` (6: latest-session, level+session filter, row
  mapping incl. summary→event fallback, result serialization, idempotent init); agent suite 48
  pass, ruff clean; frontend `tsc --noEmit` clean + 15 existing log/ws tests pass + prod `vite
  build` succeeds. Data path exercised against the **real** `agent_logs.db`. **Webview rebuilt
  & deployed** to `src/vscode-atopile/resources/webviews/` (the path the installed extension
  loads) — **reload the VS Code window** to pick it up (Python is editable; webview needs the
  rebuilt bundle).
- **Deferred:** no server-side tool/stage *filter* for agent rows yet (tool shows in the stage
  column but isn't a query filter); the audience dropdown's dead `agent` option could be hidden
  in agent mode. (Follow-latest auto-switch — previously deferred — is now **done**, see above.)

## Open items

- **FOLLOW-UP — extend auto-picking to MOSFETs and other active components.** The
  diode picker (session 23) proved the recipe; the user's call: LEDs/MOSFETs/actives
  stay **manual-pick for now** (parametric auto-pick of actives is too coarse), but
  the path is open. Facts to reuse: the components API already serves more classes —
  `POST /v0/query/leds` and `/v0/query/mosfets` both exist (probed live: fake
  endpoints 404, these return pydantic field specs; `leds` wants
  `package, qty, forward_voltage, reverse_working_voltage, reverse_leakage_current,
  max_current, max_brightness, color`). Recipe per class: (1) add the endpoint to
  `Pickable.Endpoint` (`src/faebryk/library/Pickable.py`); (2) attach
  `is_pickable_by_type.MakeChild(endpoint=…, params={...})` to the library module
  with keys exactly matching the endpoint's field spec; (3) nothing else — request
  dataclasses are generated dynamically (`picker/api/models.py make_dataclass`),
  literal serialization already matches the wire format, and the post-pick verify
  step guards mismatches. LED needs enum (`color`) serialization care; check
  `test_pick_led_by_colour` (currently skipped "TODO: add support for diodes" —
  unskip/adapt when LED lands). Upstream-contrib candidate alongside the diode picker.

- **Silent-ghost UX (found during the diode investigation, not fixed):** a module with
  no picker and no footprint logs only `ATTENTION: No pickers and no footprint …` and
  the build still reports success — the part is then absent from BOM/netlist/PCB/
  schematic while its nets remain (the "ghost"). Diodes are fixed; any other
  unpickable type still fails this silently. Promoting the warning to a visible build
  warning/strict error is a one-line policy change (`picker.py:165`) but changes
  upstream-visible behavior — needs a deliberate call.

- **Schematic real-symbol lookup misses some picked parts (cosmetic):**
  `_find_symbol_file` rebuilds the parts-dir name by sanitizing
  `has_part_picked.manufacturer` — "UNI-ROYAL(Uniroyal Elec)" ≠ cached dir
  `UNI_ROYAL_0603WAF1002T5E`, so those parts get the generic-box fallback instead of
  their cached real symbol (electrically complete, just less pretty). Fix idea: derive
  the dir from the footprint lib-id prefix instead of re-sanitizing the mfr string.

- **Table-fidelity suspects left for review (low priority).** The new
  `column_shift_suspect` scanner flags 5 suspects in 2 docs (both reviewed, left as-is):
  SPX3819's JEDEC package-outline table is a **genuine** shift (`A`: 1.75 = the JEDEC MAX
  sits under NOM, MAX empty — mechanical dims, low retrieval stakes; premium re-parse if
  it ever matters); RM46's timing tables are mostly **legit min-only** rows (setup/cycle
  times are minimums) with a couple of ambiguous rows. This is the human-review queue the
  scanner is meant to produce, not a regression.

- **M6 — implement `ipc_check` (next):** still a registered, schema'd, model-callable
  graceful stub in `_ee/tools_ee.py` — fill in the logic there. **Template is now M4 *and*
  M5**: real logic in a framework-agnostic package (`ee_agent_rag/`, `ee_agent_spice/`), thin
  `_ee/tools_*.py` wrapper that delegates + degrades. M6 leans on the **standards corpus**
  (IPC-2221B/2152), which is the RAG expansion still pending (datasheets-only today). The
  `ee_ping` smoke tool can be removed once a real tool proves the path in production.
- **`pyspice_run` follow-ups (deferred):** graph→SPICE auto-generation (agent authors decks
  for now; `extract_components()` is the template); Monte Carlo / temp sweeps / noise; the
  `sim_inspect(result_file, expr)` post-processing tool (`02_SIMULATION` §7); the
  "when to simulate" skill/prompt guidance (`02_SIMULATION` §6) — kept M5 to body+tests like
  M4, runtime skill update can follow.
- **M4 RAG — expand beyond datasheets (the main follow-up).** ~~v1 is datasheets-only~~ —
  **app_notes/white papers are now LIVE** (session 24): the chunker already covered
  `app_note` (reuses the datasheet chunker), so the path needed only data + prompts.
  SLVA079 ingested (31 chunks, corpus `app_notes`), seed eval
  `eval/datasets/app_notes.jsonl` (4 Qs, recall@5 = 1.0, gate 0.70 — expand alongside
  the corpus). **Research-before-design prompting** added in the same session so the
  agent consults the KB *before* authoring circuits: `agent/SKILL.md` gained a
  "Research Before Design" recipe; `planning/SKILL.md` Phase-1 step 1 + step 7 now
  put `rag_search` ahead of `web_search`; the `rag_search` tool description + skill
  carry the pre-design trigger. Adding more white papers = drop PDFs in
  `data/app_notes/` + `python -m ee_agent_rag.ingest data/app_notes/*.pdf` (+ a couple
  of eval Qs). Remaining corpora still not wired:
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
  check: *(none left for the 3 tools)*. **pyspice is now IN** (M5; needs system `brew install
  ngspice`). M4 RAG deps are **in**: chromadb, cohere, rank-bm25, pdfminer-six.
  `voyageai`/`qdrant`/`llama-parse` were **not** used — we went OpenAI embed / Chroma /
  LlamaParse-REST instead; do not re-add them.
- **Schematic — hierarchical real-symbol mode (DONE, pushed `51b75613`).** Goal was
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
- **Schematic — power symbols (M3b, DONE, pushed `51b75613`).** Rail pins now render as real
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
- **Schematic — deterministic filenames + cleanup (M3c, DONE, pushed `51b75613`).** Child sheet
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
  work (sessions 11–13) was committed later as `51b75613` (pushed).**
- Session 14 — **M4 `rag_search` retriever** built end-to-end: framework-agnostic
  `ee_agent_rag/` package (Chroma + LlamaParse-REST + OpenAI embed + Cohere rerank, no
  LangChain), agent wrapper `_ee/tools_rag.py`, eval runner, step-by-step tuning notebook
  (gitignored) + local `16_RAG_NOTEBOOK_AND_TUNING.md`. Cleared the LlamaParse-SDK-on-3.14
  blocker (REST workaround). 40 pass / 4 skip, ruff clean; **committed `fe5517d2`**
  (datasheets-only v1; needs keys + PDFs + a ~30-Q eval to tune for real).
- Session 15 — **M5 `pyspice_run` runner** built end-to-end: framework-agnostic
  `ee_agent_spice/` (PySpice + libngspice, agent-authored netlist, bundled discrete models),
  agent wrapper `_ee/tools_pyspice.py`, schema updated, M5 stub swapped. Cleared two real
  gaps: atopile emits no SPICE netlist (→ agent-authored decks) and libngspice isn't on the
  macOS dyld path (→ abs-path discovery). Astable multivibrator oscillates 1.13 Hz via the
  real tool path (M5 done-def met). 70 pass / 4 skip, ruff clean. **Working tree only.**
- Session 16 — **on-demand skill tools** `skills_list`/`skill_read` (`_ee/tools_skills.py`)
  exposing the whole `.claude/skills/` library just-in-time (only the 3 fixed skills were ever
  loaded before), + per-tool guidance docs `pyspice_run`/`rag_search`/`ipc_check`(seed) as
  skills, + always-on nudge in `agent/SKILL.md` recipes (simulation gate) + a localized
  `skill_read('<tool>')` pointer in **all three** EE tool descriptions
  (`pyspice_run`/`rag_search`/`ipc_check`) so each self-advertises its skill. Future tools get
  guidance by dropping one `SKILL.md`. 7 tests; agent suite 44 pass / 4 skip, ruff clean;
  verified end-to-end via `execute_tool` + `build_system_prompt`. **Working tree only.**
  (Also: local gitignored `SESSION_SUMMARY_2026-06-09.md` written for quick human review;
  `.gitignore` glob `ee_agent_docs_5_21/SESSION_SUMMARY_*.md` added.)
- Session 17 — **agent debug + run-log viewer**. Diagnosed the silent-"thinking…" freeze:
  the backend's cwd is the *opened project* (outside the fork), so `find_dotenv(usecwd=True)`
  missed the repo-root `.env` → provider fell back to `openai` with no key → instant
  `run_failed` the UI never surfaced. Fixed `config.from_env()` to also load the source-tree
  repo-root `.env` (cwd still wins). Then built the **Logs-tab "Agent" mode** (`agent_events`
  via `/ws/logs`, latest-session default, mapped onto the shared entry shape; +6 tests, agent
  suite 48 pass, frontend typechecks/builds, webview deployed). Both working-tree only.
  (Corrected post-mortem path: agent log is `agent_logs.db` under `~/Library/Logs/atopile/`,
  not `~/.atopile/agent_logs.sqlite`.)
- Session 18 — **M4 tuned & validated on a real corpus.** 50-question user-approved eval
  (`RAG_VALIDATION_SET.md` + `datasheets.jsonl`; 30 core approved, +20 implementation-
  focused on request), 14 datasheets ingested (1156 chunks), **recall@5 = 0.98** with all
  retrieval knobs at defaults — every win was ingest fidelity: synthesized page markers
  (LlamaParse ignores the marker instruction), chunk-id ordinal (Chroma dup-id rejects),
  12 new MPN patterns + filename fallback (`\b`-vs-`_` trap), stale-chunk deletion on
  re-ingest, embed/rerank disk caches + Cohere 429 backoff. Sole miss = LlamaParse
  equation-region prose drop (documented, not knob-fixable). Budget kept: ~17 LlamaParse
  jobs / ~80 OpenAI / ~60 Cohere (<100 each). Suite 95 pass / 4 skip, ruff clean. Full
  detail: `RAG_TUNING_SUMMARY.md`. Working tree only.
- Session 19 — **schematic human-ready mode** (user-requested): real symbols + manual
  signal wiring as the workflow, power/ground **auto-wired** (per rail pin: straight
  5.08 mm stub wire outward + rotation-mapped GND/PWR glyph + horizontal net-name text;
  PWR_FLAG perpendicular at the stub end). New `placement.py`: connectivity clustering
  (anchors >4 pins; satellites orbit by Σ1/deg(net) affinity; lattice-valued cells,
  disjointness = the no-shorts invariant). `SymbolDef` grew `pin_geo` (angle/length) +
  `bbox` (real + generic); labels orient by pin outward direction; ref/value anchored to
  bbox; per-sheet content-aware paper + title block. Verified: 37 exporter tests (11 new),
  ruff clean; `examples/i2c` ERC 0 + exact netlist (hv=6, lv=4, SDA/SCL/Alert=2);
  `led_badge grid10x10` (111 sheets, real WS2812 symbols) ERC 0; SVG renders eyeballed;
  double-build file set deterministic (uuid bytes still random — known deferred).
  led_badge `badge` target fails in **part picking** (solver contradiction, pre-existing,
  unrelated). Working tree only.
- Session 20 — **table-fidelity hardening** (user-driven): three LlamaParse defect flavors
  found in the corpus. (1) **Flattened merged cells** (CD0603 abs-max VRRM under one part
  column) → fixed by a merged-cell replication rule in `DATASHEET_INSTRUCTION`. (2)
  **Column drift** in wide multi-variant pin tables (NVT2008) → fixed by new
  `parse(..., premium=True)` / `EE_PARSE_PREMIUM` escalation knob. (3) **Column shift**
  from split/merged body cells under a single header column (CD0603 EC table, Min/Typ/Max
  all shifted) → premium parse verified correct alignment but **re-ingest + eval re-check
  + scanner heuristic still pending** (see IN FLIGHT in Open items). Also built
  `ee_agent_rag/eval/table_fidelity.py` scanner (+5 tests, notebook Step 2b), fixed
  `ingest --force` clobbering premium parses (`--reparse` is now the explicit paid path),
  added "Parsed-table caveats" to the rag_search skill. Eval after flavors 1–2: affected
  7/7, regression sample 9/10 (only the known #30 miss). Suite 59 pass / 4 skip, ruff
  clean. **Session ended at limit mid-flavor-3; resumed and finished in session 21.**
- Session 21 — **flavor-3 column shift finished** (the session-20 IN FLIGHT item):
  header-title pollution from the premium parse stripped **in code**
  (`parse._strip_header_title_pollution`, runs on every `parse()` return incl. cache
  hits — cache stays raw, no new parse jobs) instead of another paid instruction
  attempt; CD0603 re-ingested with `--force` (premium parse reused, old shifted chunks
  deleted, 15 clean chunks); eval 0/1/46 → 3/3, stored-chunk spot-check shows VF Typ
  0.35 + IRRM test conditions/Max correct; scanner gained `column_shift_suspect`
  (Min+Max header, ≥4 rows, Min ≥80% filled, Max empty) + 4 unit tests + 2 normalizer
  tests (RAG suite 21 pass, ruff clean). Corpus scan: CD0603 clean; 5 new suspects in
  SPX3819 (genuine, mechanical dims) / RM46 (mostly legit min-only timing) recorded in
  Open items. `RAG_TUNING_SUMMARY.md` updated with the flavor-3 section. Follow-up
  (user-caught): premium still left CD0603's two last-per-variant VF rows shifted →
  added the **sidecar-patch layer** (`_apply_sidecar_patch`, `<hash>.patch.json`, +2
  tests, RAG suite 23 pass) + the CD0603 patch; re-ingested, eval 3/3, stored rows
  verified against the PDF. Notebook Step 2b updated (third kind in the summary loop +
  caveats). Also: **`OPAMP_IDEAL` added to the bundled spice lib** (`ee_agent.lib`
  subckt: `X1 inp inn out OPAMP_IDEAL`, single-pole A0 100k / GBW ~1 MHz, Rin 10 Meg,
  Rout 10, NO rails → never clips; advertised in the tool description +
  `pyspice_run` skill; live test `test_ideal_opamp_inverting_gain` asserts gain −10,
  AC sweep through the agent wrapper shows 9.999 passband + GBW/f rolloff). And the
  **`DATA_ROOT` cwd-trap fix** (see Lessons) + `.env.example` comment updated; verified
  from a foreign cwd: store resolves to `<repo>/data`, 1065 chunks visible. Suites
  101 pass / 4 skip.
- Session 22 — **dynamic model routing** (user-requested; M6 tabled): per-turn
  complexity classifier (`_ee/model_router.py`, Haiku call, fail-open to standard,
  parser-enforced no-downshift while a design is in progress) →
  Haiku 4.5 / Sonnet 4.6 / Opus 4.8 tiers; mechanism = optional `model` kwarg through
  `LLMProvider.complete()` (both providers; per-call, singleton-safe); runner routes
  once per `run_turn` and threads `effective_model` through all 4 provider call sites,
  telemetry (`model_routed` event, progress payloads, `AgentTurnResult.model`) and the
  agent log viewer for free. Opt-in `EE_AGENT_DYNAMIC_MODEL=1`, anthropic-only
  (config-gated), defaults off. +19 tests (`test_model_router.py`, incl. live
  classification: easy→simple, hard board design→complex, 2.8 s); live-verified
  payload-model == API-served-model for override + default; `claude-opus-4-8` id
  validated. Suite 62 pass / 4 skip (+1 live), ruff clean. **Deferred:** mid-run
  escalation on circuit-breaker/chain-recovery signals (the per-call seam now exists),
  OpenAI-path routing. **NB:** the local `.env` pins `ATOPILE_AGENT_MODEL` to haiku
  (old cheap-testing choice) — remove it so the standard tier is sonnet when enabling
  routing.
- Session 23 — **diode auto-picking** (user-reported "ghost component"): a bare
  `new Diode` with only constraints was silently dropped from BOM/netlist/PCB/
  schematic (picker logs `ATTENTION: No pickers and no footprint`, build still
  green) because `Pickable.Endpoint` only wired resistors/capacitors/inductors —
  while the live components API already serves `/v0/query/diodes` (probed: returns
  10 real parts for VF 0.5–0.8 V in the client's own wire format). Fix: `DIODES`
  endpoint enum entry + `is_pickable_by_type` on `Diode` with params exactly matching
  the API field spec (~12 lines); + `test_pick_diode_by_params` (live, mirrors the
  resistor sibling). Verified: auto-pick example now picks **D1 = LRC SM260AF**, in
  BOM + schematic with its real symbol, ERC 0, netlist connectivity exact; picker
  suite 32 pass / 1 skip, ruff clean. MOSFET/LED/actives expansion deliberately
  deferred (manual-pick preferred) — see Open items FOLLOW-UP. Two adjacent findings
  recorded as Open items (silent-ghost UX, symbol-dirname mismatch).
- Session 24 — **research-before-design + app_notes corpus live** (user-requested):
  the agent's rag_search awareness was entirely reactive/datasheet-framed (and the
  planning skill steered pre-design research to `web_search`), so it would never
  consult white papers/textbooks before designing. Prompt-side fix across all four
  awareness surfaces (agent SKILL recipe "Research Before Design", planning Phase-1
  + step-7 rag-first ordering, tool description, rag_search skill). Data-side:
  app_notes path proven end-to-end — TI SLVA079 ingested (31 chunks), design-guidance
  query returns cited app-note chunks through the real tool wrapper, seed
  `app_notes.jsonl` eval 4/4 (gate 0.70), fidelity scan clean. Suites 85 pass /
  5 skip, ruff clean. Textbook corpus still needs its chunker (see Open items).
- **Next** — M7 (end-to-end design + eval) and/or expand RAG corpora (see Open
  items); M6 `ipc_check` is tabled (stub stays registered + degrades gracefully).
  Q2 schematic image export still optional.
