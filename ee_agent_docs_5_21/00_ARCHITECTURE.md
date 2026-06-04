# EE Design Agent — Architecture

> **Stack:** Forked **atopile** harness (runner, tools, skills, build pipeline, picker, stdlib) + **dual LLM providers** (`OpenAIProvider` upstream + new `AnthropicProvider`) + three custom tools registered into atopile's `ToolRegistry`: **`rag_search`, `pyspice_run`, `ipc_check`**. Custom RAG corpus is our differentiator.
>
> **Models:** Claude Opus 4.7 or Claude Sonnet 4.6 (when running under `AnthropicProvider`); gpt-5.4 family (when running under `OpenAIProvider`). Config flag picks. Both supported in CI.

---

## 1. Core decision: fork atopile, don't replace its harness

After deep exploration of atopile's source (see `14_HARNESS_ANALYSIS.md`), the architecture decision is:

1. **Fork atopile.** ~13,269 LoC of production agent runner sits inside `src/atopile/server/agent/`. ~81% is provider-agnostic — checklist-driven planning, circuit breaker, context shrinking, message-log nudges, work-progress detection, skill loader, full observability. We don't rebuild any of this.
2. **Add `AnthropicProvider` next to `OpenAIProvider`.** Both providers live in the fork. A config flag (`EE_AGENT_PROVIDER=openai|anthropic`) picks. Both must pass CI on every commit. See `16_ANTHROPIC_PROVIDER.md` for the implementation.
3. **Add three custom tools to atopile's `ToolRegistry`.** No separate orchestrator, no separate sub-agents, no deepagents, no LangGraph state machines. The agent calls our tools just like it calls `parts_install` or `build_run`.
4. **Custom RAG corpus** remains the project's differentiator. It feeds the `rag_search` tool, which the agent uses to ground designs in datasheets, app notes, standards, textbooks, and canonical `.ato` patterns.

The previously-proposed layers (deepagents orchestrator, LangGraph workflows, separate sub-agents for requirements/topology/parts/BOM/verification) are **gone**. Atopile's runner handles all of this through its checklist mechanism and skill bundle.

See `14_HARNESS_ANALYSIS.md` for the full rationale. See `15_LANGCHAIN_FORK_ANALYSIS.md` for why we didn't replace the runner with LangGraph/deepagents instead.

---

## 2. What stays atopile (the harness)

Untouched. We don't modify these — we depend on them.

| Component | Location in fork | What it does |
|---|---|---|
| Agent runner | `src/atopile/server/agent/runner.py` | The `while(tool_calls)` loop. Checklist transitions, circuit breaker, context shrinking, work-progress nudges, message-log linkage. |
| Provider protocol | `src/atopile/server/agent/provider.py` | `LLMProvider` protocol + `OpenAIProvider` impl. |
| Tool registry | `src/atopile/server/agent/registry.py` | `ToolRegistry.execute(name, arguments, project_root, ctx)`. We register four new tools into this. |
| Tool implementations | `src/atopile/server/agent/tools.py` + `tool_*.py` | All 40+ existing tools: `parts_search`, `parts_install`, `project_read_file`, `project_edit_file`, `build_run`, `design_diagnostics`, `report_bom`, `report_variables`, `layout_*`, etc. |
| Skills | `.claude/skills/{agent,ato,planning,code-review,library,package-agent,ato-language}/SKILL.md` | 4,341 lines of LLM-targeted prompts. Loaded into system prompt by atopile's skill loader. |
| Build pipeline | `src/atopile/buildutil.py`, `src/atopile/build_steps.py` | Compiler, picker, KiCad bridge, BOM generator, netlist emitter. Run via `build_run` tool. |
| Stdlib | `src/faebryk/library/` | 102 modules/interfaces (`ElectricPower`, `I2C`, `Resistor`, etc.). |
| LINE:HASH safe-edit | `src/atopile/server/agent/policy.py` | Anchored file edits the runner uses for `project_edit_file`. |
| Dataclasses | `src/atopile/dataclasses.py`, `src/atopile/server/agent/dataclasses.py` | `BuildResult`, `BOMData`, `Checklist`, etc. |
| FastAPI server | `src/atopile/server/routes/agent/` | For optional IDE / web-UI deployment (we may use it; not required). |

---

## 3. What we add (the fork's value)

Three new tool files under `src/atopile/server/agent/_ee/`:

```
src/atopile/server/agent/_ee/
├── provider_anthropic.py        # AnthropicProvider class
├── tools_rag.py                 # rag_search tool body
├── tools_pyspice.py             # pyspice_run tool body
├── tools_ipc.py                 # ipc_check tool body
└── tool_definitions_ee.py       # OpenAI-format schemas for the 3 tools above
```

Plus one configuration change in `src/atopile/server/agent/config.py` (provider selection flag), one registration hook in `src/atopile/server/agent/registry.py` (or a small `_ee/__init__.py` that registers via `_register_tool` decorators), and an optional skill addendum under `.claude/skills/ee-agent/SKILL.md`.

Plus an out-of-tree RAG ingestion package:

```
src/ee_agent_rag/                # ingestion lives outside atopile's agent module
├── ingest/
│   ├── datasheet.py
│   ├── standards.py
│   ├── textbooks.py
│   ├── ato_examples.py          # per-module chunking for atopile_examples corpus
│   └── ato_docs.py              # heading-aware chunking for atopile_docs corpus
├── retriever.py                 # hybrid retrieval + Cohere rerank
└── corpus_config.yaml
```

The agent calls into `ee_agent_rag.retriever` via the `rag_search` tool. RAG ingestion runs offline ahead of agent sessions.

---

## 4. The three new tools

### 4.1 `rag_search`

The differentiator. Atopile knows its own examples and the JLCPCB catalog; it knows nothing about datasheet sections, IPC clauses, app notes, or textbook chapters. RAG fills that gap.

**Signature:**
```python
rag_search(query: str, corpus: list[str] | None = None, top_k: int = 5,
           filter: dict | None = None) -> list[dict]
```

**Corpora:**
- `datasheets` — vendor PDFs, ingested per-section
- `app_notes` — manufacturer app notes, white papers
- `standards` — IPC-2221, IPC-7351, IPC-A-610, IPC-2152, MIL-STD-461/810, JEDEC, etc.
- `textbooks` — Art of Electronics, Razavi, Sedra/Smith, Kularatna
- `internal_standards` — company design rules, Approved Parts List
- `atopile_examples` — `.ato` from `examples/`, `nonos`, `ai-pin`, `dsp`, `cellsim`, `hyperion`; chunked per-module
- `atopile_docs` — `docs.atopile.io/llms.txt`, package registry, `.claude/skills/*/SKILL.md`

Returns chunks with `{text, score, citation}` where citation has `source`, `page`, `section`/`clause`/`mpn`/`module_name` (depending on corpus).

See `09_rag.md` and `RAG_IMPLEMENTATION_PLAN.md` for full details.

### 4.2 `pyspice_run`

Simulation is a gap atopile doesn't fill — it emits a SPICE netlist at `build/<target>/netlist.cir` but doesn't run analyses. This tool consumes that netlist.

**Signature:**
```python
pyspice_run(netlist_path: str, analysis: Literal["dc", "ac", "tran"],
            params: dict, probes: list[str]) -> dict
```

Returns `{success, results: list[{probe, time?, freq?, value}], errors[], duration_ms}`.

See `05_simulation.md` for full spec.

### 4.3 `ipc_check`

IPC compliance. Reads atopile's `report_variables` output (declared net currents, voltages) and the produced PCB, then queries `rag_search(corpus="standards")` to evaluate against IPC-2221 (trace widths), IPC-7351 (land patterns), IPC-2152 (current capacity), etc. Every finding cites a clause.

**Signature:**
```python
ipc_check(project_path: str, build_target: str,
          standards: list[str] = ["IPC-2221B", "IPC-2152"]) -> dict
```

Returns `{success, findings: list[{severity, check, net?, observed, required, rationale, citation: {standard, clause, page}}]}`.

See `07_verification.md` for full spec.

---

## 5. What we explicitly do NOT build

These were in earlier versions of this plan and are now out of scope:

- **No separate orchestrator.** Atopile's runner is the orchestrator.
- **No separate sub-agents.** Atopile's runner is one agent loop. Skills tell it how to reason about each kind of task.
- **No deepagents.** See `15_LANGCHAIN_FORK_ANALYSIS.md`.
- **No LangGraph state machines.** Atopile's skill-driven checklist replaces these.
- **No BOM tool.** Atopile's `report_bom` returns structured `BOMData` directly. The agent can read and reason about it without a separate BOM agent. Multi-distributor enrichment (Octopart) is a future addition if needed.
- **No thermal tool.** Out of scope for v1. Thermal reasoning can happen via `rag_search(corpus="datasheets")` reading R_θJA values and the agent computing junction temps in its head or via `python_repl`-style tools if we later add one.

These can be added later. The architecture supports it — each is just another tool registered into `ToolRegistry`.

---

## 6. Top-level architecture diagram

```
                     ┌────────────────────────────────────┐
                     │  User (CLI / FastAPI / IDE client) │
                     └─────────────────┬──────────────────┘
                                       │
                     ┌─────────────────▼──────────────────┐
                     │  Atopile AgentRunner (the harness) │
                     │  - checklist-driven planning       │
                     │  - circuit breaker                 │
                     │  - context shrinking + compaction  │
                     │  - skill loader (system prompt)    │
                     │  - tool dispatch via ToolRegistry  │
                     └─────────┬─────────────────┬────────┘
                               │                 │
                ┌──────────────▼──┐        ┌─────▼──────────────┐
                │ OpenAIProvider  │  OR    │ AnthropicProvider  │
                │ (upstream)      │ (cfg)  │ (new in fork)      │
                └────────┬────────┘        └──────────┬─────────┘
                         │                            │
                         └──────────┬─────────────────┘
                                    ▼
                          ┌─────────────────────┐
                          │ gpt-5.4 / claude-…  │
                          └─────────────────────┘

                     ┌────────────────────────────────────┐
                     │  ToolRegistry (atopile)            │
                     │                                    │
                     │  Atopile tools (~40, unchanged):   │
                     │  - parts_search / parts_install    │
                     │  - packages_search / packages_install
                     │  - project_read_file / edit_file   │
                     │  - build_run, build_logs_search    │
                     │  - design_diagnostics              │
                     │  - report_bom, report_variables    │
                     │  - layout_*                        │
                     │  - stdlib_list / get_item          │
                     │  - examples_search / read_ato      │
                     │  - web_search                      │
                     │  - checklist_* (managed)           │
                     │  - message_* (managed)             │
                     │                                    │
                     │  EE-agent tools (3, new):          │
                     │  ┌──────────────────────────────┐  │
                     │  │ rag_search                   │  │
                     │  │ pyspice_run                  │  │
                     │  │ ipc_check                    │  │
                     │  └──────────────────────────────┘  │
                     └─────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼────────────────────────┐
              ▼                    ▼                        ▼
      ┌──────────────┐    ┌────────────────┐    ┌─────────────────────┐
      │ ee_agent_rag │    │ atopile build  │    │ IPC standards in    │
      │ Qdrant +     │    │ pipeline,      │    │ RAG corpus,         │
      │ Cohere +     │    │ KiCad bridge,  │    │ PySpice/ngspice     │
      │ voyage-3     │    │ JLCPCB picker  │    │                     │
      └──────────────┘    └────────────────┘    └─────────────────────┘
```

The agent loop is *unchanged from upstream atopile*. The provider is config-selectable. The tool palette is upstream + 3.

---

## 7. Fork repository structure

```
ee-agent-fork/                                # fork of atopile/atopile
├── (entire upstream atopile structure preserved)
├── src/atopile/server/agent/
│   ├── _ee/                                  # NEW — our additions live here
│   │   ├── __init__.py                       # imports & registers our tools
│   │   ├── provider_anthropic.py             # AnthropicProvider class
│   │   ├── tools_rag.py                      # rag_search tool body
│   │   ├── tools_pyspice.py                  # pyspice_run tool body
│   │   ├── tools_ipc.py                      # ipc_check tool body
│   │   └── tool_definitions_ee.py            # 3 OpenAI-format schemas
│   ├── provider.py                           # MINIMAL EDIT — re-export AnthropicProvider
│   ├── config.py                             # MINIMAL EDIT — provider selection flag
│   └── (everything else upstream, untouched)
├── .claude/skills/
│   ├── (upstream skills unchanged)
│   └── ee-agent/                             # NEW — optional skill addendum
│       └── SKILL.md                          # when to use rag_search / pyspice_run / etc.
├── src/ee_agent_rag/                         # NEW — out-of-tree RAG package
│   ├── ingest/
│   │   ├── datasheet.py
│   │   ├── standards.py
│   │   ├── textbooks.py
│   │   ├── ato_examples.py
│   │   └── ato_docs.py
│   ├── retriever.py
│   ├── corpus_config.yaml
│   └── pyproject.toml
├── pyproject.toml                            # adds anthropic, voyageai, cohere,
│                                             # qdrant-client, pyspice, llama-parse
├── docs/                                     # this folder
│   └── (the markdown documents)
└── tests/
    ├── (upstream tests preserved)
    └── ee/                                   # NEW
        ├── test_anthropic_provider.py
        ├── test_provider_parity.py           # same prompt, both providers, equivalent behavior
        ├── test_rag_search_tool.py
        ├── test_pyspice_run_tool.py
        └── test_ipc_check_tool.py
```

Every commit must pass:
1. Upstream's full test suite (we don't break atopile).
2. Our `tests/ee/` suite with `EE_AGENT_PROVIDER=openai`.
3. Our `tests/ee/` suite with `EE_AGENT_PROVIDER=anthropic`.

---

## 8. Build order

See `13_PROJECT_PLAN.md` for the 8-milestone plan. Headline order:

1. **Fork bootstrap** — clone, add deps, baseline atopile build runs.
2. **`AnthropicProvider`** — implementation + parity tests against `OpenAIProvider`.
3. **Tool registration plumbing** — `_ee` module skeleton, one stub tool registered and callable.
4. **RAG corpus + `rag_search`** — ingest 10 datasheets + a standards doc + atopile examples; tool returns cited chunks.
5. **`pyspice_run`** — runs DC + transient on a coin-cell blinky netlist.
6. **`ipc_check`** — IPC-2221 trace-width + clearance checks with cited findings.
7. **End-to-end design + eval suite** — first real design with all three tools used, regression-gated.

Estimated wall-clock: 5 weeks for one engineer.

---

## 9. Observability

Atopile already integrates a trace/progress callback system in the runner. We hook into it for our four tools — every `rag_search`, `pyspice_run`, etc. emits a trace event. If we want LangSmith, we add a trace callback that forwards to LangSmith's API. No changes to the runner needed.

Per-tool metrics we care about:
- `rag_search`: recall@5 on the eval set, citation completeness rate
- `pyspice_run`: convergence rate, time per analysis
- `ipc_check`: clause-citation accuracy, severity calibration

These get rolled up in CI against a frozen benchmark of designs (see `13_PROJECT_PLAN.md` milestone 8).

---

## 10. Cost notes

The biggest cost lever is **provider choice** — Claude Opus 4.7 vs Claude Sonnet 4.6 vs gpt-5.4 will differ by 3–5× on the same workload. Operationally:

- **Default to Sonnet for dev**, Opus for tricky designs. Config:
  ```bash
  ATOPILE_AGENT_MODEL=claude-sonnet-4-6  EE_AGENT_PROVIDER=anthropic ato serve
  ```
- **Atopile's prompt caching** (`prompt_cache_key`) cuts repeat-system-prompt costs significantly. Anthropic supports this too via `cache_control: {type: "ephemeral"}` on content blocks. The `AnthropicProvider` handles this — see `16_ANTHROPIC_PROVIDER.md`.
- **`build_run` is free** — deterministic compiler, no LLM.
- **`rag_search` is cheap** — voyage embeddings + Cohere rerank, ~$0.01 per query.

Expected cost to first real end-to-end design (M8 in `13_PROJECT_PLAN.md`): **<$50** on either provider, given a coin-cell-blinky-class test design.

---

## 11. Files in this folder

| File | Purpose |
|---|---|
| `00_ARCHITECTURE.md` | This document |
| `00_orchestrator.md` | Short note: atopile's runner IS the orchestrator; no separate one |
| `05_simulation.md` | `pyspice_run` tool spec |
| `06_layout.md` | Deprecation note — atopile owns layout in v1 |
| `07_verification.md` | `ipc_check` tool spec + optional `run_verification` meta-tool |
| `09_rag.md` | RAG retriever (shared resource, feeds `rag_search` tool) |
| `11_ATOPILE_INTEGRATION.md` | Fork mechanics, tool registration, config flag |
| `12_ATOPILE_GAPS.md` | Upstream contribution candidates |
| `13_PROJECT_PLAN.md` | 8-milestone implementation roadmap |
| `14_HARNESS_ANALYSIS.md` | Decision record: why Option C |
| `15_LANGCHAIN_FORK_ANALYSIS.md` | Decision record: why not D1/D2 |
| `16_ANTHROPIC_PROVIDER.md` | `AnthropicProvider` implementation guide |
| `RAG_IMPLEMENTATION_PLAN.md` | RAG-first build plan |
| `INGESTION_PIPELINE.md` | RAG ingestion — design |
| `INGESTION_CODE_SKELETON.md` | RAG ingestion — concrete starter code |

Files **deleted** during this pivot (kept as tombstones for reference): `01_requirements.md`, `02_topology.md`, `03_schematic.md`, `04_parts.md`, `08_bom.md`, `10_DEEPAGENTS_PATTERN.md`. See each tombstone for what replaced it.
