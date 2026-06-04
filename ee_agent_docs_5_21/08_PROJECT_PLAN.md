# 13 · Project Implementation Plan (Option C — atopile fork + dual provider + 4 custom tools)

> **Purpose.** Concrete build order for the EE agent given the Option C decision (see `09_HARNESS_ANALYSIS.md`): fork atopile, support both `OpenAIProvider` and a new `AnthropicProvider`, register four custom tools (`rag_search`, `pyspice_run`, `pinmux_check`, `ipc_check`) into atopile's `ToolRegistry`. No deepagents, no LangGraph workflows, no separate sub-agents. Atopile's runner is the orchestrator.

---

## 0. Philosophy

We work in vertical slices. Each milestone produces something testable end-to-end before the next is started. The earliest slices run on atopile-as-is (one provider) before adding our changes. Each milestone has:

- **Done definition** — concrete checkable result
- **Touches** — files modified, new files created
- **Cost ceiling** — rough LLM-spend cap

---

## 1. Milestones at a glance

| # | Milestone | Done definition | Cost |
|---|---|---|---|
| 1 | Fork bootstrap | `git clone` upstream, deps installed, upstream test suite green, sample `.ato` builds | $0 |
| 2 | `AnthropicProvider` | Class implemented; parity tests pass; trivial design runs end-to-end on Claude | <$20 |
| 3 | Tool registration plumbing | `_ee` module, one stub tool registered, agent calls it through the runner | <$5 |
| 4 | `rag_search` + corpora | 6 corpora ingested (10 datasheets + standards + textbook excerpts + atopile examples + atopile docs), tool returns cited chunks, 30-question eval ≥80% recall@5 | <$30 |
| 5 | `pyspice_run` | DC + transient analyses on a coin-cell blinky netlist, results returned to agent | <$10 |
| 6 | `pinmux_check` | Validates STM32G4 (or nRF52) pin assignments; flags 5 known-bad mappings | <$15 |
| 7 | `ipc_check` | IPC-2221 trace-width + clearance, IPC-2152 current capacity; findings cited to clauses | <$20 |
| 8 | End-to-end real design + eval suite | First real design (coin-cell BLE sensor or similar) completes with all 4 tools used + frozen eval gate | <$100 |

**Total to first real design: <$200 in LLM credits.**

---

## 2. Milestone details

### Milestone 1 — Fork bootstrap (1–2 days)

```bash
# Fork via GitHub UI to <org>/atopile, then:
git clone git@github.com:<org>/atopile.git ee-agent
cd ee-agent
git remote add upstream https://github.com/atopile/atopile.git
git remote set-url --push upstream no_push

# Install with our extra deps
uv sync
uv add anthropic voyageai cohere qdrant-client pyspice llama-parse
uv add --dev pytest pytest-asyncio

# Sanity check
ato --version                         # confirm atopile installed
uv run pytest tests/ -x -q            # upstream test suite green
cd examples/esp32_minimal
ato build                             # confirm build works end-to-end
```

**Done.** Forked repo runs upstream tests green and produces a working build for `esp32_minimal`. We've added our extra deps without breaking anything. Branch `main` tracks upstream + minor `pyproject.toml` additions.

---

### Milestone 2 — `AnthropicProvider` + dual-provider support (1 week)

The big one. See `11_ANTHROPIC_PROVIDER.md` for the implementation guide.

**Files created/modified:**

- `src/atopile/server/agent/_ee/__init__.py` — empty for now
- `src/atopile/server/agent/_ee/provider_anthropic.py` — the new provider (~400 LoC)
- `src/atopile/server/agent/provider.py` — small edit: import & re-export `AnthropicProvider`
- `src/atopile/server/agent/config.py` — add `EE_AGENT_PROVIDER` env var; default `openai` to preserve upstream behavior
- `src/atopile/server/routes/agent/utils.py` — small edit: instantiate either provider based on config
- `tests/ee/test_anthropic_provider.py` — unit tests for the provider in isolation
- `tests/ee/test_provider_parity.py` — same prompt+tools, both providers, normalized responses match shape

**Key implementation points** (see `11_ANTHROPIC_PROVIDER.md` for code):

1. Translate tool definitions from OpenAI `{type, name, description, parameters}` → Anthropic `{name, description, input_schema}`.
2. Convert OpenAI-format history (`role: user/assistant/function_call_output`) to Anthropic-format messages (`role: user/assistant` with content blocks for `tool_use`, `tool_result`).
3. Normalize Anthropic responses back to the OpenAI-shape dict atopile's `orchestrator_helpers` expects.
4. Handle `previous_response_id` gracefully — Anthropic has no equivalent; the field is accepted but ignored (or used as a cache key for prompt caching).
5. Implement client-side context compaction (one Claude call to summarize older turns when context overflows) — Anthropic has no server-side `responses.compact()`.
6. Implement progressive tool-output shrinking on context overflow (mirrors OpenAIProvider's behavior).

**Done.** Both providers pass parity tests on identical prompts and tool sets. A trivial design ("voltage divider 12V→3.3V") runs end-to-end on `EE_AGENT_PROVIDER=anthropic` with `ATOPILE_AGENT_MODEL=claude-sonnet-4-6`. Same design also works on `EE_AGENT_PROVIDER=openai` (regression check).

---

### Milestone 3 — Tool registration plumbing (1–2 days)

Skeleton for the four new tools. Get one stub registered and callable through the runner so we know the plumbing works before doing real tool work.

**Files created:**

- `src/atopile/server/agent/_ee/tool_definitions_ee.py` — placeholder schemas
- `src/atopile/server/agent/_ee/tools_stub.py` — one trivial stub tool `_ee_ping`
- `src/atopile/server/agent/_ee/__init__.py` — registers `_ee_ping` via the existing `_register_tool` decorator
- `tests/ee/test_tool_registration.py` — confirms the runner sees the new tool and can call it

`_ee/__init__.py` runs at import time and uses atopile's existing `@_register_tool("_ee_ping")` decorator to add our tool to the same registry the runner queries. No core changes needed.

```python
# src/atopile/server/agent/_ee/__init__.py
"""EE-agent additions: custom tools registered into atopile's ToolRegistry."""
from . import tools_stub  # noqa: F401  — import triggers registration
# (future: from . import tools_rag, tools_pyspice, tools_pinmux, tools_ipc)
```

Then `src/atopile/server/agent/tools.py` (or a similar import hub) gets one line: `from . import _ee  # noqa` near the bottom so registration happens on package load.

**Done.** Running `ato serve` and asking the agent "call _ee_ping with message=hello" results in the tool firing and returning `{ok: true, echo: "hello"}` through the normal runner path.

---

### Milestone 4 — RAG corpus + `rag_search` tool (1.5 weeks)

This is the existing RAG plan from `RAG_IMPLEMENTATION_PLAN.md`, now wired in as a real tool.

**Files created:**

- `src/ee_agent_rag/` — the whole RAG package (ingestion + retriever) lives here, outside atopile's agent module
  - `ingest/datasheet.py`, `ingest/standards.py`, `ingest/textbooks.py`, `ingest/ato_examples.py`, `ingest/ato_docs.py`
  - `retriever.py` — hybrid BM25 + voyage-3 dense + Cohere rerank
  - `corpus_config.yaml`
- `src/atopile/server/agent/_ee/tools_rag.py` — thin wrapper: imports `ee_agent_rag.retriever` and exposes it as a registered tool
- `src/atopile/server/agent/_ee/tool_definitions_ee.py` — add `rag_search` schema
- `tests/ee/test_rag_search_tool.py` — agent-side tests
- `tests/ee_agent_rag/test_retriever.py` — retrieval-side tests with frozen eval set

**Corpora (run in parallel; the agent doesn't care about the order):**

1. **`datasheets`** — start with 10 PDFs (LDOs, MCUs, MLCCs, common ICs)
2. **`standards`** — IPC-2221B, IPC-2152 (the two we'll use most in milestone 7)
3. **`textbooks`** — Art of Electronics excerpts where licensing allows (or substitute internal materials)
4. **`app_notes`** — 5–10 vendor app notes related to the test designs
5. **`atopile_examples`** — clone atopile bundled examples + `nonos` + `ai-pin`; chunk per-module via `src/ee_agent_rag/ingest/ato_examples.py`
6. **`atopile_docs`** — fetch `docs.atopile.io/llms.txt` + key skill markdowns; chunk by heading

**Tool spec:**
```python
rag_search(query: str,
           corpus: list[str] | None = None,    # subset filter, None = all
           top_k: int = 5,
           filter: dict | None = None,         # e.g., {"mpn": "TLV713P"} or {"standard": "IPC-2221B"}
           ) -> list[dict]                      # [{text, score, citation}]
```

Citation always includes `source`, `page` (where applicable), and one of `section`/`clause`/`mpn`/`module_name`+`project`.

**Done.** `rag_search("LDO low Iq selection", corpus=["datasheets"])` returns ranked, cited chunks. 30-question eval set hits ≥80% recall@5. Agent uses the tool naturally when grounding decisions in datasheets ("per TLV713 datasheet electrical-characteristics table, Iq is 3.2 µA").

---

### Milestone 5 — `pyspice_run` (1 week)

See `02_SIMULATION.md` for the full spec.

**Files created:**

- `src/atopile/server/agent/_ee/tools_pyspice.py` — tool implementation
- Schema added to `tool_definitions_ee.py`
- `tests/ee/test_pyspice_run_tool.py`
- A test fixture: build the coin-cell blinky design, capture the netlist, drive `pyspice_run` against it

**Scope for v1:**

- DC operating point
- Transient analysis
- AC small-signal (stretch)

Skip Monte Carlo, temperature sweeps, and noise analysis until needed.

**Signature:**
```python
pyspice_run(netlist_path: str,
            analysis: Literal["dc", "ac", "tran"],
            params: dict,                 # e.g. {"t_end": "10ms", "t_step": "10us"}
            probes: list[str],            # node or device names to monitor
            ) -> dict                     # {success, results, errors, duration_ms}
```

Results format:
```python
{
    "analysis": "tran",
    "results": [
        {"probe": "VDD_3V3", "time": [...], "value": [...]},
        {"probe": "i(R1)", "time": [...], "value": [...]},
    ],
    "errors": [],
    "duration_ms": 480,
}
```

For large time series, store results to disk under `build/<target>/sim/<run_id>.npz` and return a path + summary stats. The agent doesn't read the raw waveforms; it reads min/max/mean and reasons about them.

**Done.** Coin-cell blinky design's NE555-like 1 Hz oscillator simulates; agent verifies 0.8–1.3 Hz per the spec, no human intervention.

---

### Milestone 6 — `pinmux_check` (1 week)

See `04_VERIFICATION.md` for the full spec.

**Scope for v1:** one MCU family — pick one of STM32G4 or nRF52840 based on test-design needs. Vendor pinmux tables are the long tail; we add families one at a time.

**Files created:**

- `src/atopile/server/agent/_ee/tools_pinmux.py` — tool implementation
- `src/atopile/server/agent/_ee/data/pinmux_stm32g4.yaml` (or `pinmux_nrf52840.yaml`) — capability table
- Schema in `tool_definitions_ee.py`
- `tests/ee/test_pinmux_check_tool.py` — 5 known-good designs, 5 known-bad

**Signature:**
```python
pinmux_check(project_path: str, mcu_designator: str) -> dict
```

Returns:
```python
{
    "success": True,
    "mcu": {"designator": "U1", "part_family": "STM32G4", "package": "LQFP48"},
    "findings": [
        {
            "severity": "blocker",
            "designator": "U1",
            "pin": "PA8",
            "signal": "TIM1_CH1",
            "interface": "pwm_a[0]",
            "rationale": "Pin PA8 is assigned to pwm_a[0] but only supports TIM1_CH1, not the TIM3 capability declared.",
            "citation": {"vendor": "ST", "doc": "DS12288 rev 5", "table": "Pin definitions", "page": 56}
        }
    ]
}
```

**Done.** Tool catches the 5 known-bad mappings, passes the 5 known-good. Citations point to vendor pinmux docs ingested into `app_notes` corpus.

---

### Milestone 7 — `ipc_check` (1 week)

See `04_VERIFICATION.md` for the full spec.

**Scope for v1:** two standards — IPC-2221B (trace widths, clearances) and IPC-2152 (current capacity). Both ingested into the `standards` corpus in milestone 4.

**Files created:**

- `src/atopile/server/agent/_ee/tools_ipc.py` — tool implementation
- Schema in `tool_definitions_ee.py`
- `tests/ee/test_ipc_check_tool.py`

**Signature:**
```python
ipc_check(project_path: str, build_target: str,
          standards: list[str] = ["IPC-2221B", "IPC-2152"]) -> dict
```

For each declared net (read from `report_variables()` — atopile already gives us declared currents), compute IPC-2221 trace width given net current + temp rise + copper weight, then check against the actual PCB trace widths via KiCad's PCB API or `kicad-cli pcb export drill-table` + similar. Flag any net below required width.

Returns:
```python
{
    "success": True,
    "findings": [
        {
            "severity": "blocker",
            "check": "trace_width",
            "net": "VBAT",
            "current_a": 1.5,
            "observed_mm": 0.20,
            "required_mm": 0.45,
            "rationale": "VBAT carries 1.5A; IPC-2221 6.2 requires 0.45mm at 10°C rise for external 1oz copper.",
            "citation": {"standard": "IPC-2221B", "clause": "6.2", "page": 33}
        }
    ]
}
```

**Done.** Run against a known-bad design (VBAT traces hand-set to 0.2mm at 1.5A) — produces the expected finding. Run against a known-good design — no findings.

---

### Milestone 8 — End-to-end real design + eval suite (1.5 weeks)

A real design taken through the full workflow with all four tools used in some form.

Candidate test design: **coin-cell powered BLE sensor**
- requirements: spec sheet captured in YAML
- topology: BLE SoC + sensor + voltage regulation (where needed)
- schematic: emitted by atopile, built, picker resolves parts
- citations: pulled from datasheets via `rag_search`
- simulation: `pyspice_run` confirms LDO transient response stays in spec
- verification: `pinmux_check` confirms BLE SoC pin assignments; `ipc_check` confirms trace widths

**Files created:**

- `tests/evals/coin_cell_blinky/` — input requirements, expected outputs
- `tests/evals/coin_cell_ble_sensor/` — bigger, end-to-end
- `tests/evals/run_evals.py` — runner script, posts results to LangSmith if configured
- CI workflow: nightly eval suite run, regression alerts

Eval metrics:
- Routing/skill adherence (agent uses `rag_search` for grounded claims rather than hallucinating datasheet specs)
- Tool-call success rate per tool
- `ato build` convergence (revisions before first pass)
- Citation grounding (every claim has a `citation` field)
- End-to-end task completion (human-graded on a 1–5 rubric)
- Cost per task per provider

**Done.** Coin-cell BLE sensor design completes end-to-end with no human intervention. Eval suite is a CI gate that flags >5% regressions on any metric. Both providers tested.

---

## 3. Critical path

```
M1 ─► M2 ─► M3 ─► M4 ─► M8
              ├─► M5 ─┤
              ├─► M6 ─┤
              └─► M7 ─┘
```

M4–M7 are parallelizable. M2 is the long pole (must come before any tool work since tool tests must run on both providers). M3 is small but blocks M4–M7.

**Wall-clock to M8 (first real design):**
- One engineer: **6 weeks**
- Two engineers in parallel from M4: **4 weeks**

---

## 4. Risks

| Risk | Mitigation |
|---|---|
| atopile breaks API on minor version bump | Pin upstream commit in fork; rebase quarterly with diff review |
| Anthropic client-side compaction is slower than OpenAI's server-side | Profile early; budget 2–3 sec/compaction. If it's slower we may compact less aggressively. |
| Skills implicitly assume OpenAI output format quirks | Parity tests in M2 catch this. Tune system-prompt wrapping in the `AnthropicProvider` if needed. |
| RAG quality plateaus under 80% recall@5 | Iterate on chunkers, run rerank A/B tests; this is a known long-tail of the RAG project |
| `pyspice_run` ngspice doesn't converge on some real netlists | Add convergence helpers (initial guess, .ic statements); document known limits |
| Pinmux tables are huge (one MCU family at a time) | Scope M6 to one family; treat subsequent families as separate deliverables |
| IPC checks produce false-positive noise | Severity tiers (blocker/major/minor/info); start conservative; tune based on real designs |
| Forking atopile means we own keeping it building on their release schedule | Rebase quarterly. Boundary is small (config + 1 file + tools), so merge conflicts should be rare. |

---

## 5. Where to start tomorrow

If you're a Claude Code session picking up this work:

1. Read `00_ARCHITECTURE.md`, `09_HARNESS_ANALYSIS.md`, `11_ANTHROPIC_PROVIDER.md`.
2. Run M1 setup (clone, deps, upstream tests green). 30 minutes.
3. Move to M2 — implement `AnthropicProvider`. Use `OpenAIProvider` as the reference. ~1 week.
4. M3 (tool plumbing) is small and unblocks M4–M7, which run in parallel.

When in doubt, prefer the smallest possible thing that proves the next milestone's contract.

---

## 6. What we're explicitly NOT doing (for now)

These were in earlier plans but are out of scope:

- **No BOM tool.** Atopile's `report_bom` is sufficient for v1. Multi-distributor enrichment is a possible future addition.
- **No thermal tool.** Junction-temp reasoning can be done by the agent reading R_θJA from datasheets via `rag_search`. A dedicated tool can be added later if accuracy needs it.
- **No deepagents.** Atopile's checklist handles planning.
- **No LangGraph state machines.** Atopile's runner handles control flow.
- **No separate orchestrator.** Atopile's runner IS the orchestrator (see `01_ORCHESTRATOR.md`).
- **No web UI / Streamlit / human-in-the-loop.** Atopile's FastAPI server is available if needed.
- **No multi-design / fleet management.** One design at a time.
- **No long-term memory across sessions.** Atopile already persists skill state per-turn via `prior_skill_state`; cross-session memory is future work.
- **No upstream PRs to atopile** (yet). See `07_ATOPILE_GAPS.md`. Bandwidth comes after M8.
