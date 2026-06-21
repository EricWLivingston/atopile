<h1 align="center">
    <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/atopile/atopile/assets/9785003/00f19584-18a2-4b5f-9ce4-1248798974dd">
    <source media="(prefers-color-scheme: light)" src="https://github.com/atopile/atopile/assets/9785003/d38941c1-d7c1-42e6-9b94-a62a0996bc19">
    <img alt="Shows a black logo in light color mode and a white one in dark color mode." src="https://github.com/atopile/atopile/assets/9785003/d38941c1-d7c1-42e6-9b94-a62a0996bc19" width="260">
    </picture>
</h1>

<p align="center">
  <b>EE Agent fork of atopile</b> — an AI electrical-engineering design agent built on top of the atopile compiler.
</p>

<p align="center">
  <a href="https://github.com/atopile/atopile">Upstream atopile</a> ·
  <a href="https://docs.atopile.io/">atopile docs</a> ·
  <a href="LICENSE">MIT License</a>
</p>

---

> **This is a fork.** It tracks [atopile/atopile](https://github.com/atopile/atopile) and adds
> an EE design agent plus the infrastructure it needs (a dual-provider LLM runner, three custom
> design tools, a KiCad schematic emitter, human-readable net naming, and parametric diode
> picking). For the base language, compiler, and toolchain, see the
> [upstream README](https://github.com/atopile/atopile/blob/main/README.md) and
> [docs.atopile.io](https://docs.atopile.io/). Everything below is **what this fork adds.**

## What this fork adds

atopile lets you describe electronics as code (`.ato`) and compiles them to a BOM, netlist, and
KiCad layout. This fork wraps an **agent** around that compiler: it researches a design against an
indexed knowledge base, authors `.ato`, simulates analog subcircuits, drives the build, and emits a
human-readable KiCad schematic — switchable between OpenAI and Anthropic models, with per-turn
model routing for cost control. The agent runs in the existing atopile VS Code extension sidebar.

This is an **early prototype**: the core agent loop, the three tools, and the schematic/net-naming
pipeline are working and tested, but several pieces are stubs or single-vendor (see
[Known limitations](#known-limitations--status)).

## Features

### Dual-provider agent runner — `src/atopile/server/agent/_ee/`
- **`AnthropicProvider`** (`provider_anthropic.py`) implements atopile's `LLMProvider` protocol
  alongside the existing OpenAI provider. It emulates the OpenAI stateful Responses API on top of
  Anthropic's stateless Messages API via an LRU transcript store, and repairs orphaned
  `tool_use`/`tool_result` pairs (the failure class that froze multi-turn runs).
- **Dynamic model routing** (`model_router.py`) — a per-turn complexity classifier routes calls to
  Haiku / Sonnet / Opus tiers, with a content-based floor and mid-turn escalation on repeated build
  failures. Off by default; opt-in, Anthropic-only.
- **Runner safeguards** — per-turn token budget, build-failure detection (the async build queue
  otherwise hides failures from the circuit breaker), and tightened loop/time caps.

### Custom agent tools — `_ee/tools_*.py` + framework-agnostic packages
- **`rag_search`** — retrieval over a datasheet/app-note corpus: `src/ee_agent_rag/`
  (Chroma · LlamaParse-REST · OpenAI embeddings · Cohere rerank, RRF fusion). Tuned to recall@5 ≈
  0.98 on a 50-question eval; thin wrapper degrades to `{ok: false}` on any failure.
- **`pyspice_run`** — simulates an agent-authored SPICE netlist: `src/ee_agent_spice/`
  (PySpice + libngspice, bundled discrete models). Returns per-probe min/max/mean only;
  persists full vectors to disk.
- **`skills_list` / `skill_read`** (`tools_skills.py`) — on-demand skill discovery so the agent
  pulls per-tool guidance just-in-time instead of always-loading it.
- **`ipc_check`** — registered, schema'd graceful **stub** (body TBD).

### KiCad schematic emitter — `src/faebryk/exporters/schematic/`
atopile had no Python `.kicad_sch` writer; this adds one (emitting S-expression text directly,
because `kicad.dumps` mis-writes schematics). Build step `generate_schematic` writes
`<output>.kicad_sch`. Modes (`export_schematic(mode=…)`): **hierarchical** (default — one sheet per
`.ato` module, global labels = a valid netlist), **wired** (provably short-free ladder routing), and
**labels**. Real cached symbols are regenerated into the schematic's native grammar; rails render as
KiCad power symbols. Sheet grouping is refined by connectivity (`schematic.py`:
`_pull_in_by_connectivity`, `_flatten_small_leaves`) so parent-level passives land on the subcircuit
they belong to.

### Human-readable net naming — `src/faebryk/libs/net_naming.py`
Voltage-aware power rails (`+5V` / `+3V3` / `+12V`, from the tightest-spec member), a single shared
`GND` with a fragmentation warning, and an IC-pin fallback for unnamed nets — so the emitted
schematic labels read like a human drew them.

### Parametric diode auto-picking — `src/faebryk/library/Diode.py`, `Pickable.py`
Adds the `DIODES` query endpoint + `is_pickable_by_type` to `Diode`, fixing the silent "ghost
component" drop where an un-pickable diode vanished from the BOM/netlist/PCB while its nets
remained. (MOSFET/LED auto-pick is scoped but deliberately deferred.)

### Agent run-log viewer — `src/ui-server/`, `src/vscode-atopile/`
A new "Agent" mode in the extension's Logs tab streams the agent's own run log
(`agent_events`), so planning, tool calls, and `run_failed` are visible live.

## Configuration

The provider and agent behaviour are env-driven (read once at process start — **restart the backend
/ reload the VS Code window after changing them**):

| Variable | Purpose |
|---|---|
| `EE_AGENT_PROVIDER` | `openai` (default, upstream behaviour) or `anthropic` |
| `ATOPILE_AGENT_ANTHROPIC_API_KEY` / `ANTHROPIC_API_KEY` | Anthropic credentials |
| `EE_AGENT_DYNAMIC_MODEL` | `1` enables per-turn model routing (Anthropic only; off by default) |
| `ATOPILE_AGENT_MODEL_COMPLEX` | complex-tier model (default Sonnet; set to `claude-opus-4-8` to opt into Opus) |
| `ATOPILE_AGENT_MAX_TURN_TOKENS` / `ATOPILE_AGENT_MAX_BUILD_FAILURES` | per-turn spend / failure budgets |
| `OPENAI_API_KEY` / `LLAMA_CLOUD_API_KEY` / `COHERE_API_KEY` | `rag_search` ingest + query (see `.env.example`) |
| `EE_DATA_ROOT` | RAG corpus/index root (defaults to the source tree) |
| `EE_SPICE_NGSPICE_LIB` / `EE_SPICE_TIMEOUT_S` | libngspice path override / sim wall-clock timeout |

`rag_search` needs a `brew`/pip RAG stack; `pyspice_run` needs `brew install ngspice` (libngspice 46).

## Testing

Run the suite with `ato dev test` (or `pytest -q`). The fork adds **18 test files / ~195 tests**:

- `test/server/agent/` — Anthropic provider, model router, runner safeguards, tool plumbing
- `test/exporters/` — schematic emitter + connectivity placement
- `test/ee_agent_rag/` — RAG pipeline + table-fidelity
- `test/ee_agent_spice/` — SPICE runner (+ a live, auto-skipped tier)

## Known limitations & status

- **`ipc_check` is a stub** — registered and model-callable, but returns a placeholder; it depends on
  a standards corpus (IPC-2221/2152) not yet ingested.
- **Single-vendor auto-pick** — only diodes auto-pick; MOSFETs/LEDs/other actives remain manual.
- **Silent-ghost UX** — an un-pickable, un-footprinted module still logs only `ATTENTION: …` and the
  build reports success (`src/faebryk/libs/picker/picker.py:165`); diodes are fixed, other types are
  not. Promoting that to a visible warning is a one-line policy change with upstream-visible impact.
- **Schematic real-symbol lookup** misses parts whose cached directory name doesn't match the
  sanitized manufacturer string — they fall back to a (electrically complete) generic box.
- **Dynamic routing is Anthropic-only and per-turn** — because the agent runs a whole design as one
  turn, the per-turn route effectively pins the whole run; the OpenAI path is unverified for mid-chain
  model switches, so routing is forced off there. The per-turn token budget does **not** count cache
  reads.
- **RAG corpus is datasheets + app-notes only** — standards/textbook chunkers are pending.
- **`kicad.dumps` for schematics is broken upstream** — worked around by emitting S-expression text;
  a clean upstream fix is a contribution candidate.

## License

MIT — same as upstream atopile. See `LICENSE`.
