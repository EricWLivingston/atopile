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
| **Hero-prompt hardening** (parallel-tool 400 fix; run-failure UX — spinner liveness + stall watchdog; routing floor + mid-turn escalation; token/failure spend budgets; `ato` pitfalls) — *session 26, user-driven* | ✅ done (working tree) |
| **Cost-control + build-reliability (session 27)** (complex tier Opus→**Sonnet** default, Opus opt-in; diode `.package` doc bug; `scorecard --trace`; hero target now **builds clean** — auto-picks `D1=1N5817WS`, emits 6 sheets+BOM; `ato` skill under-constrain rules) — *user-driven* | ✅ done (working tree) |
| **Hero addendum + buck/RS-485 integration (session 28)** (`SHOWCASE_PROMPT_ADDENDUM.md`; agent-built TPS563201 12V→5V buck + SP3485 RS-485 wrappers integrated into `dac-buffer.ato`, builds clean) — *user-driven* | ✅ done (working tree) |
| **Net-naming overhaul (session 28)** (voltage-aware rails `+5V`/`+3V3`/`+12V`, shared `GND` + fragment warning, IC-pin fallback, passive-`.power` skip; `ato` skill guidance + tests) — *user-driven* | ✅ done (working tree) |
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
at import time → env changes after server start don't propagate (**restart the backend /
reload the VS Code window after any `.env` edit**), and **tests must not import
`routes.agent.utils`**. Every turn logs to **`~/Library/Logs/atopile/agent_logs.db`**
(macOS; `get_log_dir()`, `model/sqlite.py::AgentLogs`, table `agent_events`) — primary
post-mortem (`sqlite3 "file:$DB?mode=ro" "SELECT … FROM agent_events ORDER BY id DESC"`).

**Provider switch.** `EE_AGENT_PROVIDER=openai|anthropic` (default `openai`, so upstream
behavior is preserved). Anthropic creds from `ATOPILE_AGENT_ANTHROPIC_API_KEY` /
`ANTHROPIC_API_KEY`; default model `claude-sonnet-4-6`.

**Model tiers (dynamic routing).** simple→Haiku, standard→Sonnet, **complex→Sonnet**
(session 27 change; was Opus). The agent runs a whole design as *one turn*, so a
per-turn route to the complex tier pins **every** call in that turn to that tier — with
complex=Opus a single hero run burned the entire API balance (`run_failed: credit
balance too low`). **Opus is now opt-in** via `ATOPILE_AGENT_MODEL_COMPLEX=claude-opus-4-8`,
which also restores Opus as the `_escalate_model` ceiling (the build-failure escalation
ladder tops out at `cfg.model_complex`). Tier→model mapping and the ladder both key off
`cfg.model_complex`, so no runner/router code changed.

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
  - **Sibling 400 fixed session 26 (orphaned tool_*use*, parallel calls).** Anthropic also
    requires every `tool_result` block to **lead** the user turn (contiguous, before any
    text). `orchestrator_helpers._build_function_call_outputs_for_model` injects a
    `{role:user}` nudge right after a successful `parts_install` result; with two parallel
    `parts_install` the delta became `[tool_result, text, tool_result, text]` in one user
    turn → 400 "`tool_use` ids found without `tool_result` blocks immediately after". Fix:
    `_flush_user` stable-partitions tool_result blocks to the front; defensive
    `_repair_orphaned_tool_uses` injects a synthetic `[no result captured]` result for any
    unmatched `tool_use` so a stray drop degrades to one missing result, never a dead run.
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
  agent_events ORDER BY id DESC"`. (Separate latent UX bug, **FIXED session 26**: a
  `run_failed`/stalled run showed as a perpetual "thinking…" — the checklist spinner was
  keyed on `item.status=='doing'`, not run liveness. Now the spinner only animates while
  `message.pending`, the checklist head shows an Errored/Stopped chip, and a stall watchdog
  surfaces "No activity for Ns — may be stuck" after ≥60 s of progress silence.)
- **Config is read once at import → stale-env runs look like routing bugs (session 26).**
  A hero run executed all 37 turns on Haiku with **zero `model_routed` events** — dynamic
  routing never fired because the backend was started before the `.env` was set up (and
  `.env` had `ATOPILE_AGENT_MODEL` pinned). `config.from_env()` runs at import via
  `load_dotenv`, so **any `.env` change needs a backend restart / VS Code window reload**.
  Diagnose routing from the log: no `model_routed` rows, or only one model tier across
  `model_request_started`, ⇒ routing isn't engaged. Belt-and-suspenders added so a misroute
  can't silently grind on the weakest model: (1) a content-based **floor** in
  `model_router._min_floor` clamps long/design-keyword-dense prompts up — needed because the
  **first** turn has `has_active_design=False`, so the no-downshift rule doesn't protect it
  and the Haiku classifier under-rated a board-design prompt as `simple`; (2) **mid-turn
  escalation** + a **failure budget** (below).
- **The routing floor inverted into an all-Opus cost blowout (session 27).** The session-26
  floor (added to stop all-Haiku) over-corrected: the next hero run (session `d3e13b4b`)
  classified the long "design a … board" prompt as **complex → Opus** and ran **every** call
  on Opus, because **the agent executes a whole design as ONE turn** (`trace.turn_started`=1,
  65 tool-loops, ~59 API calls) and routing classifies **once per turn**. ~2.6M Opus tokens
  in 8.7 min **exhausted the API balance** → `run_failed: 400 "credit balance too low"`.
  Per-turn token/failure budgets didn't bite (non-cached input ~1.3M < 1.5M; 1.5M Opus still
  ≈ $25). Fix (user pick): **default `model_complex` Opus→Sonnet**, Opus opt-in via
  `ATOPILE_AGENT_MODEL_COMPLEX=claude-opus-4-8` (which also restores Opus as the
  `_escalate_model` ceiling — the ladder tops at `cfg.model_complex`). Tier→model and the
  ladder both key off `cfg.model_complex`, so no runner/router code changed. Lesson for any
  future tiering: **per-turn routing == per-run model here**, because the design is one turn.
- **`.package` is an `SMDSize` size code, NOT a footprint name (session 27).** The build died
  at `init-build-context` with `Invalid package: SOD-123` — and the agent had copied
  `diode.package = "SOD-123"` **verbatim from `Diode`'s `usage_example`** (and the `ato`
  skill). `.package` is parsed by `compiler/overrides.py::_parse_smd_size` into `SMDSize`
  (passive size codes: `I0402`/`M1005`/`SMD…mm`); a diode footprint can't be expressed there.
  Fixed the wrong example in `src/faebryk/library/Diode.py` (`usage_example`) and documented
  it in `ato/SKILL.md`. For auto-picked parts, **omit `.package`** and let the picker choose.
- **Over-specifying picked parts blocks the picker (session 27).** Getting the hero target to
  build clean exposed a pattern, now codified in the `ato` skill (4c "Constrain loosely" +
  three 5b error rows): (a) **never hand-pin `.lcsc_id`/MPN on a passive** — a pinned part
  can have no LCSC footprint/symbol → `LCSC has no footprint/symbol for any candidate` (the
  agent's `c1.lcsc_id="C49326616"` killed the build; auto-pick from value+package fixed it);
  (b) **picking params must be intervals, not exact** (`x = 20V` → "assigned to an exact value
  instead of … an interval" — use `>=`/`<=`/`within`); (c) **bound direction matters** —
  datasheet *maximums* (Vf, leakage) use `<=` (a lower-bounded range drops parts spec'd only
  as a max), *ratings* (reverse V, current) use `>=`; (d) **constrain only what's required** —
  an extra `max_current >= 1A` left **zero** matches (`No matching component found`). Final
  working diode block: `forward_voltage <= 0.55V`, `reverse_working_voltage >= 20V`, no
  `max_current`, no `.package` → auto-picks **`1N5817WS`** (D1).
- **`build_run` is async-queue → build failures are invisible to the circuit breaker
  (session 26).** `build_run` returns `{success:true,"Build queued",buildId}` immediately;
  the actual ERROR/ALERT logs only appear later via `build_logs_search`. So a failing
  build→edit→rebuild loop never trips the identical-tool-failure breaker (each `build_run`
  "succeeds") and can grind to the loop/time caps. The runner now detects failure from a
  `build_logs_search` result carrying `"level":"ERROR"/"ALERT"`, counts it per turn, and
  escalates the model one tier at ≥2 failures, then graceful-stops at `max_build_failures`
  (default 4). The loose default caps were also tightened (`max_tool_loops` 240→80,
  `max_turn_seconds` 7200→1800) and a **per-turn token budget** added
  (`ATOPILE_AGENT_MAX_TURN_TOKENS`, default 1.5M, 0=off) — there was previously **no**
  cumulative spend cap (the dead `context_hard_max_tokens` field was never a budget).
- **Recurring `.ato` authoring pitfalls a weak model trips on (session 26).** Three distinct
  build errors from one Haiku run, now in the `ato` skill's "Common build errors → fixes":
  `within` is only valid inside `assert <field> within A to B` (not a bare expression);
  `for` loops need `#pragma experiment("FOR_LOOP")` at the file top; referencing a
  non-existent pin yields `Field '<pkg>.<PIN>' could not be resolved` (verify the package
  interface first). The correct syntax was already documented — the fix is making it
  scannable + escalating off the weak tier when builds repeat.
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
- **Net naming lives in `src/faebryk/libs/net_naming.py::attach_net_names()` (session 28).**
  Called from `build_steps.py:684` inside `prepare_nets`, *after* pick + `attach_random_designators`
  → solved voltages, picked parts, footprints/pads, and designators are all available there.
  Priority: explicit `has_net_name` > EXPECTED suggestion > **voltage rail name** > SUGGESTED
  suggestion / implicit > **device-pin fallback** > conflict-resolution (prefix→LCA→numeric).
  `check_net_names` (line 685) forbids two nets sharing a final name — so "all grounds = GND"
  can't literally name two distinct nets `GND`; secondaries de-conflict and a warning fires.
  The schematic emitter consumes names verbatim (`classify_nets`, `build_power_symbol(net)`),
  so cleaning the names cleans the schematic labels for free.
- **`Capacitor`/`Resistor`/`Inductor` carry a convenience `.power` interface auto-bonded to
  their two pins (`power.lv ~ unnamed[1]`) — it is a *pin alias, not a rail* (session 28).**
  This bit hard: the bootstrap cap's `power.lv` lands on the buck **SW** node, so a naive
  "any `ElectricPower.lv` ⇒ ground" classifier mislabels SW as ground (`cboot-power-GND`), and
  the *real* 20-node system ground got demoted to `rs485-power-GND`. Fix: skip rail markers
  whose owning component is a passive (`has_designator_prefix` ∈ {R,C,L,FB,…}) in both rail-role
  and rail-voltage detection (`_rail_marker_is_passive`). After the skip the SW node correctly
  becomes `SW` (IC-pin fallback) and there is one clean `GND`.
- **Rail voltage = the *tightest-spec* member, not the midpoint of the coupled range (session
  28).** A rail net bus-aliases many `ElectricPower`s; reading the solved superset (`get_values()`
  / `solver.try_extract_superset`) gives a range widened by downstream coupling (diode drop, LDO
  headroom) whose midpoint drifts off-nominal (a `+4V1` for a 5 V rail) — and looser sinks on the
  same net (e.g. a DAC tolerating 1.7–5.5 V) add stray candidates. Pick the member with the
  smallest **relative** width (that's the author's `assert … within`), then format with
  `_format_rail_voltage` (`+5V`, `+3V3`, `+12V`; the `+3V3` vs `+3.3V` spelling is one isolated
  line). Near-zero/non-finite → no rail name (degrade to pin/generic).
- **Local file deps need `ato sync` before a build sees them (session 28).** Adding a package to
  `ato.yaml` `dependencies` is not enough — `ato build` resolves `local/<id>` from the project's
  `.ato/modules/local/` cache, and a stale cache fails with `Local dependency path
  packages/<X> does not exist`. Run `ato sync` (in the project dir) to install the new local
  packages into the cache first; the already-cached deps from a prior build are untouched.

---

## Completed work (condensed)

### M1 — fork bootstrap ✅
Fork + remotes as above; branch `feature/ee-agent`. Trimmed the roadmap from 4 tools to
3 (dropped `pinmux_check`) → 7 milestones, `<$185` budget. Skills used as-shipped (no
`.claude/skills/*` changes).

### M2 — `AnthropicProvider` (dual provider, stateful) ✅ pushed
`src/atopile/server/agent/_ee/provider_anthropic.py` implements the `LLMProvider` protocol;
translates OpenAI↔Anthropic message/tool shapes (reusing
`orchestrator_helpers._extract_*`); client-side compaction. Provider reference:
`11_ANTHROPIC_PROVIDER.md`.
- **Stateful emulation of `previous_response_id`** (the load-bearing design): an LRU
  transcript store (`_MAX_TRANSCRIPTS=64`) keyed by minted response id; `complete()`
  rebuilds the full conversation (deep-copied) and stores the assistant turn so the next
  delta's `tool_result` pairs with a real `tool_use`. Unknown id → raise a message
  containing `"previous_response_id"` so `run_turn_with_chain_recovery` retries from full
  local history; empty/assistant-terminated transcripts get a `"Continue."` user turn.
  (See Lessons for the stateless-vs-stateful 400 class this solves, incl. the session-26
  parallel-tool sibling.)
- **Wiring:** `config.py` `EE_AGENT_PROVIDER` branch (anthropic `base_url=""` → SDK default);
  `routes/agent/utils.py::_make_provider()` selects it. Default OpenAI path unchanged.

### Schematic emitter ✅ pushed (`src/faebryk/exporters/schematic/`)
Atopile had **no** Python `.kicad_sch` writer; this adds one, emitting **sexp text** (because
`kicad.dumps` is broken — see Lessons). Design + IR-extraction detail:
`13_KICAD_SCH_AND_FRONTEND_FILES.md`.
- **Integration:** build step `generate_schematic` (`build_steps.py`) writes
  `output_base.with_suffix(".kicad_sch")` — the exact path `domains/manufacturing.py`
  surfaces as `outputs.kicad_sch`, so it auto-integrates (no route/frontend changes).
- **Modes** (`export_schematic(mode=…)`): `hierarchical` (build default — one sheet per
  `.ato` module, global labels = valid netlist, zero wires, human-cleanup base) | `wired`
  (generic boxes + provably short-free ladder routing) | `labels`. Real cached symbols are
  regenerated into the schematic's native grammar (`real_symbol.py`); generic-box fallback
  (`generic_symbol.py`). Rail pins render as KiCad power symbols + one `PWR_FLAG`/rail.
  Short-free + power-symbol + deterministic-filename invariants are all in Lessons.

### M3 — tool-registration plumbing ✅ pushed (`src/atopile/server/agent/_ee/`)
Wires the EE tool surface through atopile's existing machinery.
- **How a tool becomes live (the reusable seams):** a handler via `@_register_tool(name)` in
  `_TOOL_HANDLERS` (dispatched by `execute_tool`) **and** a schema in `get_tool_definitions()`.
  The runner sends *all* `ToolRegistry.definitions()` to the model — no mediator gate on
  exposure. `_ensure_tool_registry_consistency` enforces schema⇔handler parity at first call.
  `mediator_catalog._TOOL_DIRECTORY` is a non-gating discovery list. No per-tool policy
  allowlist (policy gates file paths only).
- **EE code stays in `_ee/`:** `tools_ee.py` (handlers) + `tool_definitions_ee.py` (schemas).
  Two one-line core seams: a **bottom-of-`tools.py`** import `from ._ee import tools_ee`
  (last, so the back-import of `_register_tool` resolves) + a `*get_ee_tool_definitions()`
  splice in `tool_definitions.py`. New tools follow this shape; `ee_ping` smoke tool can be
  removed once a real tool proves the path in production.

### M4 — `rag_search` retriever ✅ committed (`fe5517d2`)
Framework-agnostic package **`src/ee_agent_rag/`** (no agent-runner coupling) + thin wrapper
`_ee/tools_rag.py`. Stack (no LangChain): **Chroma · LlamaParse-REST · OpenAI
`text-embedding-3-large` · Cohere rerank**. Full reference: `05_RAG.md`,
`16_RAG_NOTEBOOK_AND_TUNING.md`, `RAG_TUNING_SUMMARY.md`.
- **Pipeline.** Ingest: `classify → parse(LlamaParse REST) → chunk(section-aware) →
  enrich(MPN + deterministic chunk_id + optional summary) → embed → Chroma + rank-bm25
  sidecar`. Query (`retriever.rag_search`): dense + sparse(BM25) → **RRF fusion** → **Cohere
  rerank** → `{text, score, citation}`. Wrapper degrades to `{ok:false,error}` on any failure
  (missing keys / empty index / import error), heavy deps imported lazily.
- **Tuned & validated** (session 18): 14-doc datasheet corpus, 50-q eval, **recall@5 = 0.98**
  with retrieval knobs at defaults — every win was ingest fidelity (see the RAG Lessons:
  synthesized page markers, ordinal chunk-ids, MPN patterns, table-defect ladder). Corpus
  expanded to app_notes (session 24); textbooks pending (see Open items).
- **Dev/data surface (all gitignored):** `notebooks/rag_pipeline.ipynb`; corpus PDFs in
  `data/<corpus>/`; keys `OPENAI_API_KEY`/`LLAMA_CLOUD_API_KEY`/`COHERE_API_KEY`.
  Deps (all on 3.14): `chromadb`, `cohere`, `rank-bm25`, `pdfminer-six`.

---

### M5 — `pyspice_run` runner ✅ committed
Framework-agnostic package **`src/ee_agent_spice/`** + wrapper `_ee/tools_pyspice.py` — the
M4 shape exactly. Stack: **PySpice 1.5 + libngspice 46**, agent-authored netlist, bundled
discrete models. Reference: `02_SIMULATION.md`, `pyspice_run` skill.
- **Pipeline.** `simulate(netlist, analysis, params, probes, project_root)` → `build_deck`
  (auto-`.include` bundled models + `.save` probes + analysis control card) → `_run_ngspice`
  (locked singleton; vectors via `vec._data`) → persist **all** vectors to
  `build/sim/<run_id>.npz`, return **per-probe min/max/mean** only (context-safety).
  Analyses: `op`/`dc`/`ac`/`tran`. Wrapper degrades every failure (no netlist / no PySpice /
  no libngspice / convergence) to `{success:false, errors:[…]}`.
- **Bundled models** (`models/ee_agent.lib`, incl. `OPAMP_IDEAL`): topology-accurate, not
  vendor-accurate; schema tells the agent the names + to inline a vendor `.model` for
  accuracy. Lib-path discovery, the non-thread-safe ngspice singleton/lock, result-key
  naming, and the wall-clock timeout (`EE_SPICE_TIMEOUT_S`) are all in Lessons.
- **Deps:** `pyspice` (+ system `brew install ngspice`); all on 3.14. Dev surface:
  `notebooks/pyspice_testbed.ipynb` (gitignored).

### Skill-discovery tools (`skills_list` + `skill_read`) ✅ committed
On-demand skill library + per-tool guidance, so the agent pulls *how/when/scope* guidance
just-in-time instead of always-loading it.
- **The gap (reusable insight).** The agent only ever loads the **3** `fixed_skill_ids`
  (`agent`/`ato`/`planning`) every turn (`context.py`); it **never scans `skills_dir`**.
  Putting a doc in `.claude/skills/` does **nothing** for the agent by itself —
  discoverability comes only from a tool that reads it.
- **Tools (`_ee/tools_skills.py`):** `skills_list()` scans `config.skills_dir`;
  `skill_read(skill_id)` returns the SKILL.md body (truncated ~12 KB), `^[A-Za-z0-9_-]+$`
  path-traversal guard.
- **The bit that makes it fire — three reinforcing awareness layers** (the agent never scans
  a folder): (1) the runner sends *all* tool defs+descriptions every turn; (2) an always-on
  nudge in `agent/SKILL.md` (`## Skill Library`); (3) each guidance-bearing tool's **own
  description** ends with `call skill_read('<tool>') before first use`.
- **Hook for future tools = drop `.claude/skills/<tool>/SKILL.md`** — auto-discovered, no
  code change. Guidance docs exist for `pyspice_run`/`rag_search`/`ipc_check`(seed).

### Agent run-log viewer (Logs tab "Agent" mode) ✅ committed
Surfaces the agent's own run log (`agent_events` in `agent_logs.db`) in the existing Logs
viewer, so you can watch planning / tool calls / `run_failed` live. Motivated by the
silent-"thinking…" debug session (run errors were only in the DB).
- **Design (reusable).** New **`agent` `LogMode`** beside build/test; agent rows mapped onto
  the **shared entry shape** server-side (`Log.agent_row_to_entry`) so the existing
  `LogDisplay` renders them with zero new row UI. Session id optional → blank = **follow-latest**
  (`_push_agent_stream` re-resolves `latest_session_id()` each poll; sends full batch as
  `agent_logs_result`=*replace* on session change, `agent_logs_stream`=*append* steady-state).
- **Gotcha:** every window reload/extension reinstall restarts the backend on a **new port**,
  dropping the panel's WS → stale snapshot; auto-follow + fresh reconnect re-replace with the
  current session. Webview must be **rebuilt & deployed** to
  `src/vscode-atopile/resources/webviews/` + window reload (Python is editable; webview isn't).

### CODE_AUDIT P0–P3 hardening ✅ committed (session 25)
Implemented **every** P0/P1/P2/P3(Q1–Q8) finding from `ee_agent_docs_5_21/CODE_AUDIT.md` —
**per-finding landing spots + divergence notes are inline in that doc** (✅ tags + a top
summary block). Read it there; only the decisions worth carrying forward are kept here:
- **BM25 sidecar is now JSON, not pickle** (removed the RCE surface), rebuilt into `BM25Okapi`
  on load + mtime-cached, atomic write (`.json.tmp`+`os.replace`); old `.pkl` ignored.
- **SPICE wall-clock timeout** `EE_SPICE_TIMEOUT_S` (30s) on both lock-acquire and run; caveat:
  ngspice's C core can't be force-killed, so a timed-out worker lingers (cleanup skipped while
  alive). SPICE params validated before interpolation (injection guard).
- **OpenAI model defaults fixed** → `gpt-4o`/`gpt-4o-mini` (old `gpt-5.4`/`gpt-4.1-nano` were
  non-existent upstream placeholders; Anthropic stays the primary path).
- **`approx_tokens`** uses tiktoken if present else **`ceil(len/3)`** (tiktoken is NOT in the
  env; the `/3` over-count is the safe fallback). `stable_chunk_id` hashes full content.
- **Prompt-cache instrumentation** (`_log_cache_metrics`) logs read/creation tokens per turn so
  per-model cache thrash from routing is observable. (NB the routing-cost concern is partly
  superseded by the session-26 routing floor + spend safeguards.)

### Net-naming overhaul ✅ (session 28, working tree)
Human-readable net names so the emitted schematic labels are readable; all in
`src/faebryk/libs/net_naming.py` (+ one-line solver thread in `build_steps.py`, a small emitter
dedup in `schematic.py`, and `ato`-skill guidance). Four rules, both algorithm + agent guidance:
- **Voltage-aware power rails** → `+5V` / `+3V3` / `+12V`, from the tightest-spec member's solved
  voltage (see Lessons for why tightest, not midpoint). Author adds purpose via
  `override_net_name="+5V_IN"`.
- **Shared `GND`** — all non-isolated ground-role nets collapse to one `GND`; if ≥2 distinct
  grounds remain a warning lists them (no silent `GND-2`). Isolated grounds opt out via
  `override_net_name`.
- **IC-pin fallback** — a net with no purposeful name takes a connected device's pin name, ICs
  beating passives (`has_designator_prefix`): bootstrap node → `SW`, etc. Bare pad numbers and
  single-char lead names are rejected.
- **Passive `.power` skip** — the load-bearing fix (see Lessons): a passive's auto-bonded
  `.power` is not a rail, so the SW node stops masquerading as ground and the real ground keeps
  `GND`.
- **Guidance + tests:** `.claude/skills/ato/SKILL.md` §2.9 gained a "Net naming convention"
  block; +4 unit tests in `net_naming.py` (voltage format, tiers, generic names, GND collapse);
  3 schematic assertions updated for the intentional `lv`→`GND` + deduped `atopile:GND` lib_id.
  56 net/schematic tests green; demo emits `+12V/+5V/+3V3/GND/SW/VFB/EN/SDA/SCL/A_P/B_N/…`.

### Hero addendum + buck/RS-485 integration ✅ (session 28, working tree)
- `ee_agent_docs_5_21/SHOWCASE_PROMPT_ADDENDUM.md`: a lean follow-up prompt extending the
  hero board with a TPS563201 12 V→5 V buck front-end + an SP3485 RS-485 transceiver to an
  output connector (datasheet-driven/loose; no SPICE requirement; voltage/GND naming).
- Finished a prior agent session's handoff: integrated the agent-authored `buck`/`rs485`
  wrappers into `dac-buffer.ato` `App` (12 V input → buck → existing 5 V chain; SP3485 on 3V3,
  `RS485HalfDuplex` A/B + 120 Ω term to a TE 796636-3 terminal block; logic exposed). Needed
  `ato sync` first (see Lessons). Builds clean (18/18); FB divider 54.9k/10k → 4.99 V; inductor
  auto-picked. KiCad-cli ERC violations are all the known cosmetic classes (symbol-lib-not-
  registered + generic-symbol pin types), not real connectivity errors.

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

Sessions 1–21 are condensed to one line each — the durable *why* is in Lessons & gotchas,
the *what/where* in Completed work; only carry-forward notes are kept inline.
- Sessions 1–3 — doc set written; local VSIX + dev-loop established; provider edit surface mapped.
- Session 4 — M1 fork; 4→3 tools (pinmux dropped); `AnthropicProvider` landed additively.
- Sessions 5–6 — M2 wired + tested; fixed the multi-turn freeze (stateful transcript); first commits pushed.
- Sessions 7–9 — schematic emitter: `kicad.dumps` blocker found → text emitter; label mode, then short-free wire mode. Pushed.
- Session 10 — M3 tool-registration plumbing (`ee_ping` + 3 schema'd graceful stubs). Pushed.
- Sessions 11–13 — schematic hierarchical real-symbol mode + power symbols + deterministic filenames. Committed `51b75613` (pushed).
- Session 14 — M4 `rag_search` built end-to-end (`ee_agent_rag/`); cleared LlamaParse-SDK-on-3.14 (REST). Committed `fe5517d2`.
- Session 15 — M5 `pyspice_run` built (`ee_agent_spice/`); cleared "no SPICE netlist" (agent-authored decks) + libngspice dyld path.
- Session 16 — `skills_list`/`skill_read` on-demand skill tools + per-tool guidance docs + the 3-layer awareness nudge.
- Session 17 — diagnosed the silent-"thinking…" freeze (cwd-relative `.env` → openai fallback, no key) + built the Logs-tab "Agent" mode.
- Session 18 — M4 tuned on a real corpus: 14 datasheets, 50-q eval, **recall@5 = 0.98**; every win was ingest fidelity (see RAG Lessons). `RAG_TUNING_SUMMARY.md`.
- Session 19 — schematic human-ready mode: connectivity-clustered `placement.py`, auto-wired power, oriented labels/glyphs.
- Sessions 20–21 — the three LlamaParse table-defect flavors + the instruction→premium→sidecar escalation ladder + `table_fidelity.py` scanner; `OPAMP_IDEAL` added to the spice lib; `DATA_ROOT` cwd-trap fixed. (All in RAG Lessons.)
- Session 22 — **dynamic model routing**: per-turn classifier → Haiku/Sonnet/Opus via a `model` kwarg through `LLMProvider.complete()`; opt-in `EE_AGENT_DYNAMIC_MODEL=1`, anthropic-only. Routes once per turn; telemetry = `model_routed` event. (Session-26 added the floor + mid-turn escalation deferred here.)
- Session 23 — **diode auto-picking** (the "ghost component" fix): `DIODES` endpoint + `is_pickable_by_type` on `Diode`. MOSFET/LED expansion deferred (Open items).
- Session 24 — **research-before-design** prompting across all four awareness surfaces + **app_notes corpus live** (TI SLVA079). Textbook chunker still pending.
- Session 25 — **branch audit** → `CODE_AUDIT.md` (P0–P3, each with `file:line`+fix; revert baseline `f9bcf3a7`); textbook-notebook fidelity scan (caught raw-`\[…\]` equations); **empty-chunk ingest 400 fixed** in `chunk.py`.
- Session 26 — **hero-prompt hardening** (user-driven; 2026-06-17→19). Two stuck hero-prompt
  runs analyzed from `agent_logs.db` (`SHOWCASE_PROMPT.md` against `examples/dac-buffer-demo`),
  each a different failure; both fixed, plus the test target/env cleaned up.
  - **Run 1 → parallel-tool 400.** Died at loop 14: `messages.NN: tool_use ids were found
    without tool_result blocks immediately after` on the 2nd of two parallel `parts_install`.
    Cause: the post-`parts_install` `{role:user}` nudge interleaved between parallel
    `function_call_output`s, packed by the provider as `[tool_result, text, tool_result,
    text]` — Anthropic requires tool_results to lead the turn. Fix in
    `_ee/provider_anthropic.py`: `_flush_user` partitions tool_results to the front +
    `_repair_orphaned_tool_uses` injects synthetic `[no result captured]` for any orphan.
    +4 tests (`test_anthropic_provider.py`).
  - **Run 1 → run-failure UX (the long-standing "perpetual thinking…").** The checklist
    spinner was keyed on `item.status=='doing'`, not run liveness, so the dead run still
    "loaded" at 4/7. Fixes in `ui-server`: doing-spinner only animates while
    `message.pending`; Errored/Stopped chip on the checklist head (from `message.activity`);
    **stall watchdog** in `useAgentChatRuntime` (seconds-since-last-progress; ≥60 s on a
    pending run → "No activity for Ns — may be stuck"). `npm run build` clean.
  - **Run 2 → routing never fired (all Haiku).** 37 turns, zero `model_routed` events:
    backend on stale config (env read once at import → must restart after `.env` edits), and
    the first turn's `has_active_design=False` let the Haiku classifier under-rate a
    board-design prompt as `simple`. Fix: `model_router._min_floor` clamps long/design-dense
    prompts up (new-design phrasing → complex), applied over heuristic and classifier.
    +5 tests.
  - **Run 2 → non-converging build loop + spend.** Whack-a-mole'd 3 ato errors (`within`
    bare; `for` without `#pragma experiment("FOR_LOOP")`; bad pin `package.BYP`) and never
    converged; `build_run` is async so failures were invisible to the circuit breaker.
    Fixes in `runner.py`: per-turn `build_failures` (from `build_logs_search` ERROR/ALERT)
    → escalate model one tier at ≥2, graceful `failure_budget_exceeded` stop at
    `max_build_failures` (4); per-turn token budget `ATOPILE_AGENT_MAX_TURN_TOKENS`
    (default 1.5M, 0=off) → `token_budget_exceeded` stop; tightened defaults
    `max_tool_loops` 240→80, `max_turn_seconds` 7200→1800. +7 tests
    (`test_runner_safeguards.py`). `ato/SKILL.md` gained a "Common build errors → fixes"
    table.
  - **Ops.** `.env` rewritten as the documented hero-test config (provider/keys/dynamic
    routing active, model pin commented, every runner knob present+commented at default with
    a one-line comment); `examples/dac-buffer-demo/` reset to a bare skeleton `ato.yaml`
    (removed agent-written `dac-buffer.ato`, `packages/`, `.ato/`, `build/`, `layout/` + the
    added deps). Agent suites green except the pre-existing `test_dynamic_model_defaults_off`
    (fails only because the repo `.env` sets `EE_AGENT_DYNAMIC_MODEL=1` and `from_env` calls
    `load_dotenv`). **All working tree only — not yet committed.**
- Session 27 — **cost-control + build-reliability** (user-driven; 2026-06-19). Analyzed the
  re-run hero session `d3e13b4b` from `agent_logs.db` (`scorecard.py` 5/8). Findings + fixes:
  - **All-Opus cost blowout → credit exhaustion.** Per-turn routing + single-turn execution
    pinned the whole design to Opus → `run_failed: 400 credit balance too low` (see Lessons).
    Fix: `config.py` default `model_complex` Opus→**Sonnet**, Opus opt-in via
    `ATOPILE_AGENT_MODEL_COMPLEX`; updated `.env` comment, passdown, `test_model_router.py`
    (assertion + new `test_complex_tier_opus_opt_in`).
  - **Diode `.package="SOD-123"` doc bug** (build died at `init-build-context`). Fixed the
    wrong `usage_example` in `Diode.py` + `ato/SKILL.md` (`.package` = `SMDSize` only).
  - **`scorecard.py` upgrades:** new `--trace` flag (per-tool counts + tool-call timeline
    *with parameters* + skills-read section, parsed from `tool_call_completed` payloads);
    robust `check_diode` (picker classifies Schottky as BOM type `other` → also match by
    `forward_voltage` param / `D#` ref); softened `check_rag` wording (logged payloads are
    truncated). `SHOWCASE_README.md` notes the Opus opt-in.
  - **Hero target now builds clean** (user ask). Fixed `dac-buffer-demo/dac-buffer.ato`:
    dropped hand-pinned `lcsc_id` on filter passives (auto-pick), diode constraints →
    intervals + correct bound direction, dropped over-tight `max_current`. `ato build` →
    18/18 stages, auto-picks **`D1=1N5817WS`**, emits 6 `.kicad_sch` + BOM. Scorecard 7/8
    against the project (the one ✗ is the *historical* run's logged `run_failed`).
  - **`ato` skill under-constrain rules** (so the agent stops over-specifying): 4c
    "Constrain loosely" block + GOOD/BAD example; three new 5b error-table rows
    (exact-value-not-interval / `No matching component found` / `LCSC has no footprint`).
  - **Minor UX:** agent composer textarea `max-height` 50vh→160px (`AgentChatPanel.css`) so a
    long prompt scrolls internally instead of swallowing the panel.
  - **All working tree only — not yet committed** (session 26 tree still uncommitted too).
- Session 28 — **hero addendum + buck/RS-485 + net-naming overhaul** (user-driven; 2026-06-19).
  - **Opus-vs-Sonnet reconfirm.** Latest `agent_logs.db` session `d3e13b4b` ran every call on
    Opus (`model_routed: complex` → `claude-opus-4-8`) despite the session-27 Sonnet default —
    a **stale backend** (config read once at import; `.env`/`config.py` changed after start).
    After a window reload the next run routed complex→**Sonnet** (15 calls, clean
    `run_completed`). Reinforces the existing "restart after any `.env`/config edit" lesson; no
    code change.
  - **Hero addendum + integration.** Wrote `SHOWCASE_PROMPT_ADDENDUM.md` (TPS563201 12V→5V buck
    + SP3485 RS-485 to a connector). Finished a prior agent handoff by integrating the
    agent-built `buck`/`rs485` wrappers into `dac-buffer.ato` `App`; needed `ato sync` to
    install the new local packages first (see Lessons). Builds clean (18/18); FB 54.9k/10k →
    4.99 V; ERC-cli violations all cosmetic classes. (Completed work.)
  - **Net-naming overhaul.** Voltage-aware rails (`+5V`/`+3V3`/`+12V`, tightest-spec member),
    shared `GND` + fragment warning, IC-pin fallback, and the load-bearing passive-`.power`
    skip (a bootstrap cap's `power.lv` was mislabeling the buck **SW** node as ground →
    `cboot-power-GND`, demoting the real ground). `ato` skill guidance + 4 unit tests; 3
    schematic assertions updated. 56 tests green. (Completed work + Lessons.)
  - **All working tree only — not yet committed.**
- **Next** — commit the session-26 + session-27 + session-28 working tree (provider/runner/router/config/
  ui-server/skills/scorecard/Diode.py + session-28 `net_naming.py`/`build_steps.py`/`schematic.py`/
  `ato`-skill + the extended demo `dac-buffer.ato` + buck/RS-485 packages + `SHOWCASE_PROMPT_ADDENDUM.md`
  + new tests); re-run the hero prompt
  against the clean target after a backend restart and confirm Sonnet-tier routing + a clean
  `run_completed` (`scorecard.py --trace`); then re-ingest
  `fundamentals_of_electrical_engineering_1.pdf` (now unblocked) + tighten
  `TEXTBOOK_INSTRUCTION` (`$…$` not raw `\[…\]`), reparse textbooks `force=True`; work the
  `CODE_AUDIT.md` P0 items (BM25 pickle→JSON + atomic write, cache the tool defs). Plus M7
  (end-to-end design + eval) and/or expand RAG corpora (Open items); M6 `ipc_check` tabled;
  Q2 schematic image export optional. Latent gap (deferred, user did not pick it): the
  per-turn token budget doesn't count cache reads (`runner.py:778`).
