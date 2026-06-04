# 00 · Orchestrator (atopile's runner)

> **There is no separate orchestrator.** Atopile's `AgentRunner` is the orchestrator. This doc exists to document that decision and point at the right places in the codebase for anyone expecting a separate orchestration layer.

---

## 1. What plays the orchestrator role

`src/atopile/server/agent/runner.py` (`AgentRunner` class) is the agent loop. It handles:

- **Turn lifecycle.** `run_turn(...)` accepts a user message, threads it through tool-call loops, returns an `AgentTurnResult`.
- **Planning.** The model uses managed tools (`checklist_create`, `checklist_update`, `checklist_add_items`) which the runner intercepts and stores as `_TurnState.checklist`. The skill bundle (`agent`, `ato`, `planning`) instructs the model on when and how to use these.
- **Continuation.** The runner checks the checklist between turns and emits nudges if the model abandoned work mid-task or didn't acknowledge a pending user message.
- **Tool dispatch.** `ToolRegistry.execute(...)` routes calls. Atopile's ~40 tools plus our 4 (`rag_search`, `pyspice_run`, `pinmux_check`, `ipc_check`) all live in the same registry.
- **Circuit breaker.** Identical tool failures trigger a non-retryable error after N attempts.
- **Context management.** Progressive tool-output shrinking on context overflow; client-side compaction (Anthropic) or server-side compaction (OpenAI) when shrinking exhausts.
- **Observability.** Progress callbacks, trace callbacks, message log, activity summary.

We don't reimplement any of this. We use `AgentRunner` as-shipped.

---

## 2. Why no separate orchestrator

Earlier plans imagined a deepagents-driven outer orchestrator with sub-agents for requirements, topology, parts, BOM, schematic, simulation, verification. After deep exploration of atopile's harness (see `09_HARNESS_ANALYSIS.md`), we found:

- Atopile's runner already implements robust planning (checklist-driven), tool dispatch, and context management — to a degree it would take significant effort to replicate.
- Atopile's skills (`.claude/skills/`) are written for atopile's runner specifically; they reference `checklist_*` and `message_*` tools. Using them requires that runner.
- Adding a deepagents layer on top of atopile's runner creates two parallel planning surfaces (`write_todos` vs `checklist`), which dilutes the prompt engineering atopile has invested in.

So the architecture is: **atopile's runner runs everything, sub-tasks are addressed through skills + tools, not sub-agents.**

The full reasoning is in `09_HARNESS_ANALYSIS.md`. `10_LANGCHAIN_FORK_ANALYSIS.md` documents why we didn't replace the runner with LangGraph/deepagents instead.

---

## 3. Where the work happens

Without a separate orchestrator, the agent's behavior is driven by:

| Concern | Where | What it does |
|---|---|---|
| Planning / checklist | atopile `runner.py` + `checklist.py` + skill bundle | The model creates a checklist, marks items doing/done, runner enforces |
| Requirements capture | skill `planning/SKILL.md` + tool `design_questions` | Model asks user for missing info, captures spec |
| Topology / architecture | skill `planning/SKILL.md` + skill `ato/SKILL.md` | Model proposes block-level design; lifted patterns from `examples_search` |
| Part selection | skill `ato/SKILL.md` §4 + tools `parts_search`, `parts_install`, `packages_search` | Atopile's picker resolves passives; agent picks ICs |
| Schematic | skill `ato/SKILL.md` + tools `project_read_file`, `project_edit_file`, `build_run` | Model emits `.ato`, builds, fixes errors |
| Simulation | skill `ee-agent/SKILL.md` + tool `pyspice_run` | Optional; model runs analyses where physics matters |
| Verification | skill `ee-agent/SKILL.md` + tools `ipc_check`, `pinmux_check`, `design_diagnostics` | Model runs checks before declaring "done" |
| BOM | tool `report_bom` | Returns structured data; model reads and reasons. No separate enrichment in v1. |

This is intentionally not a state machine. The model reasons about what to do next; the skills tell it the rough order; the checklist tracks progress.

---

## 4. What we control

Three levers shape the agent's behavior:

1. **Skill bundle.** Loaded into the system prompt via `ATOPILE_AGENT_FIXED_SKILL_IDS`. Default: `agent,ato,planning`. We add `ee-agent` to that.
2. **Tool palette.** Whatever's in `ToolRegistry`. Upstream + our 4.
3. **Model + provider.** `EE_AGENT_PROVIDER` + `ATOPILE_AGENT_MODEL`. Smarter models follow skill instructions better; we tune cost vs. capability per design.

Nothing else.

---

## 5. Optional FastAPI server

Atopile ships a FastAPI server (`atopile.server.routes.agent`) that exposes the runner as an HTTP service. We use it as-is for any:

- IDE integration (Cursor, Claude Desktop talk to it via MCP)
- Web UI (atopile's existing one, or our own)
- Multi-tenant deployment (one server, many sessions)

CLI-only deployment doesn't need the server; use `ato chat` instead.

---

## 6. If we ever need a separate orchestrator

The most plausible reasons to add one later:

- **Multi-design coordination.** "Compare BOM cost across these 3 designs." Atopile's runner is single-design; multi-design needs an outer layer.
- **Cross-team workflows.** Hardware agent triggering firmware agent, mechanical agent, etc. Atopile knows nothing about firmware.
- **Long-running async jobs.** "Run worst-case simulation overnight and report tomorrow." Atopile's runner is synchronous per turn.

In each case, the outer orchestrator would call atopile's runner as one of several capabilities. That's a future architecture (similar to Option D2 in `10_LANGCHAIN_FORK_ANALYSIS.md`). For v1, we don't need it.
