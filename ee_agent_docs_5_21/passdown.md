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
| M3 — tool-registration plumbing (`ee_ping` smoke + 3 real-tool scaffolds) | ✅ done & pushed |
| M4 `rag_search` · M5 `pyspice_run` · M6 `ipc_check` — *registered stubs; bodies TBD* | ⏳ next (M4) |
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

---

## Open items

- **M4–M6 — implement the registered tool bodies (next):** `rag_search` (M4),
  `pyspice_run` (M5), `ipc_check` (M6) are already registered, schema'd, and
  model-callable as graceful stubs in `_ee/tools_ee.py` — fill in the logic there. The
  `ee_ping` smoke tool can be removed once a real tool proves the path in production.
- **Deferred deps**, re-add at their milestones with `uv lock --refresh` and a 3.14-wheel
  check: voyageai / cohere / qdrant-client / llama-parse (M4 RAG), pyspice (M5 sim).
- **Schematic follow-ups (optional):** agent `schematic_export` tool (deferred; pattern
  verified in `tools.py` / `tool_definitions_project.py`); real symbols in wire mode;
  nicer placement / power symbols. Upstream: fix the `kicad.dumps` schematic write path
  (`07` §2.11) so the emitter could use the typed model.
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
- **Next** — M4: implement `rag_search` body.
