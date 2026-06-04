# 15 · Forking atopile to adopt LangChain / LangGraph / deepagents (keep their OpenAI provider)

> **Question.** What if we fork atopile and modify their harness to use LangChain / LangGraph / deepagents — adopting that infrastructure while preserving as much of their runner logic as possible? Keep their OpenAI provider as-is.
>
> **Short answer.** This sounds like a small refactor but is actually a much bigger fork than swapping the provider (Option C in `09_HARNESS_ANALYSIS.md`). The reason is that atopile's runner doesn't *use* an agent framework that we can swap out — **the runner itself IS the agent framework**. There's no clean seam where LangChain inserts. Everything LangChain/LangGraph/deepagents would give us (planning, sub-agents, state machines, observability) atopile already implements differently, deeply embedded in `runner.py`.
>
> So the realistic options are:
>
> - **D1. Replace the runner entirely** with LangGraph + deepagents, keeping atopile's tools, skills, provider, and dataclasses. ~3,000 LoC of new orchestration; throw away ~3,800 LoC of atopile's runner+helpers. Most of their hard-won runner behavior gets lost or rewritten.
> - **D2. Run atopile's runner as a CompiledSubAgent inside deepagents.** Keep their runner untouched. Use deepagents as the outer planner and call `runner.run_turn()` as one of several sub-agents. Adds an outer layer, doesn't modify the inner. ~600 LoC of glue.
> - **D3. Wrap individual atopile tools as LangChain tools.** Don't use their runner at all; just lift their `tools.execute_tool()` function into a LangChain `@tool` decorator and build the agent on LangGraph from scratch. This is basically the original plan from before the deep atopile exploration.
>
> Of these, **D2 is the only one that genuinely "preserves as much as possible while adopting that infrastructure."** D1 throws away the runner's value; D3 throws away everything except tools.
>
> **D2 is also strictly more work than Option C** (Anthropic-swap fork) — you end up running two agent loops (deepagents on the outside, atopile's runner on the inside) instead of one, and you still don't get Anthropic models. Worth considering only if there's a specific deepagents/LangGraph capability we need that atopile's runner can't provide.

---

## 1. Why this isn't a clean swap

Three things make atopile's harness resistant to "just bolt on LangChain":

### 1.1 The runner intercepts managed tools

`runner.py` line 1140 onward: when the LLM emits a tool call, the runner checks whether it's a `_MANAGED_TOOL` (checklist or message-log). If it is, **the runner does not dispatch through `ToolRegistry`** — it intercepts and mutates `_TurnState.checklist` directly. The registered tool body in `tools.py:2019+` is a sentinel that raises an error if you ever reach it:

```python
@_register_tool("checklist_create")
async def _tool_checklist_create(arguments, project_root, ctx):
    raise RuntimeError("checklist_create is intercepted by the runner")
```

This means:
- **Checklist isn't a tool we can disable.** It's a baked-in mechanism with 200+ LoC of state-machine logic across `runner.py`, `checklist.py`, `message_log.py`, and `mediator.py`.
- **`write_todos` and `checklist_create` do the same thing.** Both are "planning state stored in a managed location, surfaced back to the model via system-prompt injection." Running both at once gives the model two competing planning surfaces, which dilutes prompt-engineering work in the skills.
- **The skills assume checklist exists.** `ato/SKILL.md`, `planning/SKILL.md`, `agent/SKILL.md` all reference `checklist_create`, `checklist_update`, `message_acknowledge` explicitly. Removing the runner means rewriting all four skills (~4,341 lines) to use `write_todos` instead.

### 1.2 The runner enforces work-progress nudges

Lines 55–88, 200–222, 492+, 1140+: the runner watches for the model "doing work without a checklist" or "ignoring pending messages" and injects nudge messages into the next turn. This is a major source of the runner's robustness on long sessions. None of this is a tool call — it's runner logic between turns.

deepagents has no equivalent. If we move to deepagents, this behavior disappears unless we reimplement it ourselves.

### 1.3 The runner manages context length defensively

Lines 161–239 in `provider.py` (called from runner): on context-length-exceeded, the runner progressively shrinks tool outputs (5000 → 2500 → 1200 → 600 → 300 chars) **and then** calls `OpenAI responses.compact()` to summarize prior turns. This is wired into the `_request_with_retries` path. LangChain has no equivalent off-the-shelf — deepagents has its own context-management approach (different architecture).

So the surface that looks small ("swap out the runner loop, keep the rest") turns out to be the most load-bearing 3,800 LoC of the codebase.

---

## 2. What's actually portable to LangChain/LangGraph regardless

These parts of atopile are framework-agnostic and lift cleanly into any LangChain/LangGraph-based system:

| atopile module | LoC | Portable as |
|---|---|---|
| `tools.py` + `tool_*.py` | ~4,000 | LangChain `@tool` callables; `execute_tool()` is the entry point |
| `policy.py` | 958 | LINE:HASH safe-edit protocol (pure Python, no framework deps) |
| `dataclasses.py` (atopile/) | 1,300 | `BuildResult`, `BOMData`, `VariablesData`, etc. — typed state for any framework |
| `tool_definitions*.py` schemas | 1,200 | Function-call schemas in OpenAI format; trivial to convert to LangChain `StructuredTool` |
| `.claude/skills/*/SKILL.md` | 4,341 | Prompts loadable into any system prompt |
| `buildutil.build()` | ~1,200 | Build pipeline; called as a Python function |
| `faebryk/library/` (stdlib) | 3,000 | Stdlib metadata; agent-introspectable |
| `provider.py:OpenAIProvider` | 369 | LangChain has `ChatOpenAI`; we'd use that instead, not this |

These are the same artifacts we'd lift in any fork strategy. The question is just *how much of the runner do we keep*.

---

## 3. The three sub-options

### Option D1 — Replace the runner with LangGraph + deepagents

**Architecture.**
- deepagents `create_deep_agent` for the outer loop (planning via `write_todos`, sub-agent spawning, virtual filesystem)
- LangGraph `StateGraph` for inner loops (schematic emit→build→fix→simulate→verify→revise)
- Sub-agents wrap atopile tools as LangChain `StructuredTool` instances
- atopile's `OpenAIProvider` → replaced by LangChain's `ChatOpenAI(model=...)`
- atopile's `runner.py`, `orchestrator_helpers.py`, `checklist.py`, `message_log.py`, `circuit_breaker.py`, `activity_summary.py`, `mediator*.py`, `context.py` → **deleted or unused**
- Skills (`.claude/skills/`) → rewritten to remove checklist references; planning replaced with `write_todos`

**LoC accounting.**
- Throw away: ~3,800 LoC of runner + helpers + planning state machinery (atopile's harness modules above)
- Rewrite: ~4,341 lines of skills to remove checklist tools, message_log references, design_questions tool
- Build new: ~3,000 LoC for our LangGraph workflows, deepagents wiring, tool wrappers, and re-implementation of behaviors we want to keep (work-progress nudges, circuit breaker — both ~150 LoC each from atopile, but we can copy them)

**Net effect.** We're roughly back at the original plan (Option A from `09_HARNESS_ANALYSIS.md`), with the advantage that we have atopile's runner code in front of us as a reference for behaviors to replicate. The skills become a *liability* — they reference tools that no longer exist.

**Pros.**
- Native LangGraph state machines for the deterministic cycles (this is the original-plan benefit).
- deepagents' sub-agent isolation pattern.
- Easier per-node eval in LangSmith.
- Standard LangChain ecosystem (Cohere reranker, Qdrant, voyage embeddings) plugs in idiomatically.

**Cons.**
- We replicate ~600 LoC of runner behavior (work-progress nudges, circuit breaker, message-log handling, context shrinking) from scratch in our own framework. Most of these don't have direct deepagents equivalents.
- We rewrite ~4,341 lines of skills. This is not mechanical — the skills explain *how to think about a problem in terms of the tools available*, and the tool surface is different.
- We carry a fork of atopile but only use ~30% of its code. The rest stays around for `buildutil.build()` and the tools.
- Rebase pain is high — when atopile updates the skills or adds new managed tools, we have to repeat the rewrite.

**Time estimate.** 6–8 weeks for one engineer. Comparable to Option A but with more rewriting of atopile's content vs. building from scratch.

**Verdict.** This *is* "fork atopile and adopt LangChain/LangGraph/deepagents", but it doesn't "preserve as much as possible" — we end up throwing away the most valuable runner logic. Only consider this if specific LangGraph state-machine semantics are essential to the workflow.

---

### Option D2 — Run atopile's runner as a `CompiledSubAgent` inside deepagents

**Architecture.**
- deepagents `create_deep_agent` is the outer orchestrator (planning, virtual FS, sub-agent spawning).
- atopile's `AgentRunner` is wrapped as a single `CompiledSubAgent` called `schematic-workflow`.
- deepagents calls this sub-agent for any schematic-related work; atopile's runner does all schematic emission, building, fixing, etc. internally.
- Other sub-agents (`requirements-agent`, `topology-agent`, `parts-overlay-agent`, `bom-agent`, `verification-meta`) are deepagents-native dict sub-agents that call LangChain-wrapped atopile tools directly when needed.
- RAG, Octopart, PySpice, IPC, thermal, pinmux — all LangChain tools available to the appropriate sub-agents.
- atopile's `OpenAIProvider` runs unmodified inside its own sub-agent.
- LangGraph state machines for verification (where we want explicit ordering of checks).

**The integration shim.**
```python
# src/ee_agent/subagents/atopile_subagent.py
from langgraph.graph import StateGraph, START, END
from deepagents import CompiledSubAgent
from atopile.server.agent import AgentRunner, AgentConfig, ToolRegistry
from atopile.server.agent.provider import OpenAIProvider

# One AgentRunner instance per session (atopile's runner is stateful via skill_state)
class AtopileSubAgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user_message: str
    project_path: str
    skill_state: dict  # persisted across deepagents' invocations of this sub-agent
    history: list[dict]  # atopile's history format

async def atopile_node(state: AtopileSubAgentState) -> dict:
    cfg = AgentConfig.from_env()
    runner = AgentRunner(config=cfg, provider=OpenAIProvider(cfg), registry=ToolRegistry())
    result = await runner.run_turn(
        ctx=AppContext(workspace_paths=[Path(state["project_path"])]),
        project_root=state["project_path"],
        history=state["history"],
        user_message=state["user_message"],
        prior_skill_state=state["skill_state"],
    )
    # Marshal back into deepagents-compatible message
    return {
        "messages": [AIMessage(content=result.text)],
        "skill_state": result.skill_state,  # persist for next invocation
        "history": state["history"] + [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": result.text},
        ],
    }

graph = StateGraph(AtopileSubAgentState)
graph.add_node("atopile_runner", atopile_node)
graph.add_edge(START, "atopile_runner")
graph.add_edge("atopile_runner", END)
compiled_atopile = graph.compile()

atopile_subagent = CompiledSubAgent(
    name="schematic-workflow",
    description=(
        "Generate, build, fix, and verify electronics designs using atopile. "
        "Has its own planning loop (checklist) and full tool palette. "
        "Delegate any .ato authoring, building, or design-iteration task here."
    ),
    runnable=compiled_atopile,
)
```

The deepagents orchestrator now sees `schematic-workflow` as one of its sub-agents. From its perspective, the call is `task("Design a coin-cell blinky", "schematic-workflow")` → atopile's runner does ~30 turns of internal work → returns final summary. deepagents doesn't see the inner tool calls; they don't pollute its context.

**LoC accounting.**
- New: ~600 LoC for the integration shim, sub-agent wrappers for our non-atopile work, LangGraph wrapping
- Modified in atopile: ~50 LoC (config defaults so we can pass our session config in; possibly a `flush_history()` method for clean restarts)
- New tools added to atopile registry: 6 (RAG, Octopart, PySpice, IPC, thermal, pinmux) — same as Option C
- Atopile skill modifications: minimal — maybe add an "ee-agent" skill that explains the wider context, but the existing skills are untouched

**Total: ~1,500 LoC of new code (similar to Option C), zero rewriting of atopile internals, two agent loops running in series.**

**What you get.**
- **Atopile's runner runs unmodified.** All the runner logic (checklist, nudges, circuit breaker, compaction) stays intact for the schematic work.
- **deepagents handles cross-domain orchestration.** Requirements → topology → schematic → parts overlay → BOM enrichment → verification. The schematic step is "delegate to atopile and trust it".
- **LangGraph state machines for the deterministic verification flow** — `ato_check → design_diagnostics → erc → drc → sim_check → bom_check → ipc_check → thermal_check → pinmux_check → summary` — these aren't atopile's job anyway.
- **Custom tools are deepagents-native** for cross-domain work and atopile-native for atopile work. No double-registration.
- **OpenAI everywhere** — both layers use OpenAI (atopile's runner via `OpenAIProvider`, deepagents via `ChatOpenAI`). Per the constraint of the question.

**What you lose.**
- **Two LLM loops per session.** deepagents calls atopile, atopile does ~10–30 turns, deepagents resumes. The atopile turns aren't visible to deepagents' planning; deepagents sees them as one opaque sub-agent call. This is *good* for context budget but *bad* for fine-grained observability.
- **Double model cost on overlapping work.** If deepagents and atopile both want to "look at the requirements", they each do it. Mitigation: pass the requirements as part of the `user_message` so atopile doesn't re-read.
- **deepagents' `write_todos` and atopile's checklist coexist but don't sync.** Two parallel planning surfaces, each authoritative within its own scope. Not catastrophic but worth being aware of.
- **Context conversion overhead.** deepagents' message history (LangChain `BaseMessage` list) ↔ atopile's history (`list[dict]` in OpenAI format) needs translation at the boundary. Trivial code but a place for bugs.

**Time estimate.** 4–5 weeks for one engineer. Faster than Options A or D1 because we're not rewriting atopile; just wrapping it.

**Verdict.** D2 is the *only* sub-option that genuinely "preserves as much as possible while also adopting that infrastructure." It does so by running atopile's runner *inside* deepagents rather than replacing it. The cost is having two loops; the benefit is zero atopile rewrites and full deepagents/LangGraph capability on top.

---

### Option D3 — Skip atopile's harness entirely; just use tools + skills

**Architecture.**
- atopile's tools (`tools.py:execute_tool`) wrapped as LangChain `@tool` callables.
- atopile's skills loaded as prompts into our deepagents sub-agents.
- atopile's runner: not used at all.
- Everything else is LangGraph + deepagents + LangChain, native.

This is essentially the original architecture before exploring atopile's harness depth — the plan from `00_ARCHITECTURE.md`. Now better-informed because we know the runner is there if we want it.

**LoC accounting.** Same as the original plan: ~5,000 LoC of new orchestration. Skills require modification to remove checklist references (only the schematic and planning skills heavily depend on it).

**Verdict.** This is "Option A from `09_HARNESS_ANALYSIS.md`, with atopile-as-library." It's defensible but throws away the runner work. **Only recommend if D2's two-loop architecture turns out to be a problem in practice.**

---

## 4. Side-by-side comparison

| | Option A (build own) | Option C (fork + Anthropic) | Option D1 (fork + LangGraph, replace runner) | **Option D2 (fork + LangGraph, wrap runner)** | Option D3 (atopile library only) |
|---|---|---|---|---|---|
| LLM provider | Anthropic (LangChain) | **Anthropic** (new provider) | OpenAI (LangChain `ChatOpenAI`) | **OpenAI** (atopile's existing) | OpenAI (LangChain) |
| atopile runner reused | No | **Yes, fully** | No (replaced) | **Yes, fully** | No |
| Skills reused as-shipped | Partially | **Yes, fully** | No (rewrite to drop checklist) | **Yes, fully** | Partially |
| deepagents available | Yes | No | **Yes** | **Yes** | Yes |
| LangGraph state machines | **Yes** | No | **Yes** | **Yes, for non-atopile flows** | Yes |
| Sub-agent context isolation | **Yes** | Partial (compaction) | **Yes** | **Yes** | Yes |
| Net new LoC | ~5,000 | ~2,000 | ~3,000 + rewrites | ~1,500 | ~5,000 |
| LoC of atopile thrown away | n/a (not fork) | 0 | ~3,800 (runner+helpers) | 0 | n/a (library) |
| LoC of atopile rewritten | n/a | ~700 (provider) | ~4,341 (skills) | ~50 (config tweaks) | n/a |
| Wall-clock estimate | 6–8 weeks | 6 weeks | 6–8 weeks | **4–5 weeks** | 6–8 weeks |
| Quarterly maintenance | Low (no fork) | Low (clean boundary) | High (skills drift) | Low (untouched runner) | Low (no fork) |
| Cross-domain orchestration | **deepagents native** | atopile checklist | **deepagents native** | **deepagents native** | **deepagents native** |
| Per-node LangSmith eval | **Yes** | No | **Yes** | **Yes, on the outer layer only** | **Yes** |
| Compatible with Anthropic later | n/a | n/a | Yes (swap `ChatOpenAI` → `ChatAnthropic`) | Yes (later: combine with Option C; add `AnthropicProvider` to the inner atopile runner) | Yes |

---

## 5. Honest take on the question

The question is "what would it look like to fork atopile and modify it to use LangChain/LangGraph/deepagents, keeping as much as possible." Looking at the codebase, the most honest answer is:

> **There's no version of this that simultaneously (a) actually modifies atopile's runner to use LangChain/LangGraph/deepagents and (b) preserves "as much as possible".** Modifying the runner *is* throwing it away — the runner doesn't import a framework we can swap; it implements its own. The only path that preserves the runner is to leave it alone and wrap it from outside (Option D2).

If "modify with a fork to use LangChain/LangGraph/deepagents" means specifically that the *runner internals* must call LangChain APIs, then Option D1 is the answer — and the tradeoff is that we throw away ~3,800 LoC of atopile's runner work and rewrite ~4,341 lines of skills.

If "preserve as much as possible while adopting that infrastructure" is the dominant constraint, then Option D2 is the answer — run atopile's runner *as* a sub-agent inside deepagents. The runner stays unmodified; deepagents gets to do its planning/file-system/sub-agent thing outside; LangGraph gets to express the verification flow as a state machine. Cost: two loops, double-track planning.

---

## 6. What Option D2 looks like in practice

If we adopt D2, here's how the project structure changes from the current docs:

```
ee-agent-fork/                          # fork of atopile/atopile
├── (atopile structure unchanged — runner.py, provider.py, etc.)
├── src/atopile/server/agent/
│   ├── _ee_tools/                      # NEW — our tools registered into ToolRegistry
│   │   ├── rag_search.py
│   │   ├── octopart_overlay.py
│   │   ├── pyspice_runner.py
│   │   ├── ipc_check.py
│   │   ├── thermal_check.py
│   │   └── pinmux_check.py
│   └── tool_definitions_ee.py          # NEW — schemas for our tools
└── src/ee_agent/                       # NEW — our deepagents layer
    ├── orchestrator.py                 # deepagents create_deep_agent wiring
    ├── subagents/
    │   ├── atopile_subagent.py         # wraps atopile's AgentRunner as CompiledSubAgent
    │   ├── requirements.py             # NL → YAML
    │   ├── topology.py                 # block-level architecture
    │   ├── parts_overlay.py            # qualified parts via Octopart
    │   └── bom_overlay.py              # BOM enrichment (atopile gives us BOMData, we add columns)
    ├── workflows/
    │   ├── verification.py             # LangGraph: ato_check → design_diagnostics → IPC → thermal → ...
    │   └── simulation.py               # LangGraph: plan → run → interpret
    ├── tools/
    │   ├── rag.py                      # ours, deepagents-callable
    │   ├── parts_apis.py               # Octopart, DigiKey, Mouser
    │   └── pyspice.py
    ├── prompts/                        # only deepagents prompts; atopile skills used as-is
    │   ├── orchestrator.md
    │   ├── requirements.md
    │   ├── topology.md
    │   ├── parts_overlay.md
    │   └── bom_overlay.md
    └── rag/                            # corpus ingestion
        ├── ingest.py
        ├── retriever.py
        └── ato_chunker.py
```

The schematic agent prompt becomes one line:

> "When the user asks for schematic work — generation, building, fixing, verification of `.ato` — call `task('<description>', 'schematic-workflow')`. The schematic-workflow handles everything internally; do not call atopile tools yourself."

deepagents' orchestrator now manages:
- Requirements capture (own sub-agent, no atopile dependency)
- Topology decisions (own sub-agent, queries RAG for reference designs)
- Parts overlay (own sub-agent for qualified parts; atopile's runner picks commodity parts internally)
- Delegating schematic work to atopile's runner via the wrapped sub-agent
- BOM enrichment after atopile's run completes
- Running the verification LangGraph as a separate CompiledSubAgent

The split is clean: **atopile owns "the design itself"; deepagents owns "the design's lifecycle around it."**

---

## 7. Recommendation revisited

If the team is committed to OpenAI (per the question) and wants LangChain/LangGraph/deepagents on top, **Option D2 is the right path.** It preserves atopile's runner unmodified, adopts the full LangChain/LangGraph/deepagents stack on the outside, and costs ~1,500 LoC of glue.

The thing to weigh is whether you actually need deepagents' outer-loop capabilities given what atopile's runner already does. Atopile's checklist already does planning. Atopile's runner already manages multi-turn sessions. The deepagents value-add in D2 is:
- Cross-domain coordination (requirements → topology → parts → schematic → BOM)
- Sub-agent context isolation for non-schematic work (the parts overlay agent doesn't need the full schematic context)
- LangGraph state machines for verification flow
- Native plug-in for LangChain ecosystem (Qdrant, Cohere, voyage)

If those are the value, D2 makes sense. If they're not, **Option C (the Anthropic-swap fork) is materially simpler** — one agent loop, full Anthropic stack, no two-layer coordination overhead.

**If you must keep OpenAI:** D2.
**If you can move to Anthropic:** C.
**If you want both LangGraph/deepagents *and* Anthropic:** D2 + later add `AnthropicProvider` to the inner runner (combines Options C and D2). This is the most flexible long-term position but the biggest near-term scope.

---

## 8. Three concrete things to spike before committing to D2

1. **Two-loop cost.** Run a real design through D2 (using atopile-as-is and a minimal deepagents wrapper around it) and measure token cost vs. running the same design through atopile's runner alone. If the overhead is >30%, the abstraction may not be worth it.
2. **Planning surface confusion.** Watch whether the orchestrator's `write_todos` ever conflicts with atopile's checklist on a multi-step task. Specifically: does the orchestrator's todo list track "deliver final design" while atopile's checklist tracks "create main.ato, add USB-C connector, add LDO, build"? If both are visible to the user, that's confusing.
3. **Sub-agent return-message quality.** atopile's runner returns a `text` field summarizing what it did. Is that summary good enough to feed back into deepagents' orchestrator for downstream sub-agent decisions? If atopile's summary is too terse (it's optimized for a chat UI, not for agent-to-agent handoff), we may need to inject a "summarize for handoff" step at the boundary.

None of these are blockers; they're calibration questions for whether D2 is genuinely better than C in our specific workflow.
