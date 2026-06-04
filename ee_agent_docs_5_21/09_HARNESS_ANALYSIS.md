# 14 · Atopile Harness — Build vs. Buy Analysis

> **Question.** Should we build our own harness (deepagents + LangGraph + custom orchestration) or fork atopile and modify their existing harness to use Anthropic? Or just use their harness as-is and add tools?
>
> **Recommendation. Fork atopile. Add an `AnthropicProvider` (~400 LoC, ~1 week). Augment with custom tools and the RAG corpus. Skip deepagents + LangGraph entirely for the schematic/build/fix loop.**
>
> This collapses the project from ~14,000 LoC of new orchestration code to ~2,000 LoC of provider + tool overlay. It also gives us atopile's hard-won runner logic (checklist transitions, circuit breaker, context compaction, message-log nudges, work-vs-meta tool detection) for free.

---

## 1. What's actually inside atopile's harness

After full source exploration, here's the LoC breakdown of `src/atopile/server/agent/`:

| Module | LoC | What it does | Coupled to OpenAI? |
|---|---|---|---|
| `runner.py` | **2,891** | The main `while(tool_calls)` loop, checklist continuation, circuit breaker integration, message-log nudges, work-progress detection, compaction on context overflow | No — uses `LLMProvider` protocol |
| `tools.py` | 2,094 | Tool implementations (`execute_tool` dispatch + body) | No |
| `orchestrator_helpers.py` | 968 | Response extraction (`output → text + function_calls`), payload shrinking, prompt cache keys, steering inputs, worker-loop guards | **Yes** — knows OpenAI Responses API JSON shape |
| `policy.py` | 958 | LINE:HASH safe-edit protocol | No |
| `tool_layout.py` | 809 | Layout tool implementations | No |
| `tool_definitions_project.py` | 765 | Project-tool JSON schemas | **Yes** — OpenAI function-call schema |
| `mediator_catalog.py` | 666 | Tool metadata for sidebar UI | No |
| `tool_references.py` | 509 | Reference-resolution tools | No |
| `policy_datasheet.py` | 486 | Datasheet policy | No |
| `tool_definitions.py` | 435 | More tool JSON schemas | **Yes** — OpenAI function-call schema |
| `provider.py` | **369** | `LLMProvider` protocol + `OpenAIProvider` impl | **Yes** — uses `openai` SDK directly |
| `activity_summary.py` | 368 | Progress UI events | No |
| `mediator.py` | 361 | Tool directory + tool-memory updates | No |
| `context.py` | 287 | System prompt + initial user message builders | No |
| `mediator_inference.py` | 237 | Tool-result summarization for tool-memory | No |
| `tool_build_helpers.py` | 202 | Build pipeline glue | No |
| `policy_scope.py` | 182 | Scope policy enforcement | No |
| `tool_web_helpers.py` | 159 | Web search helpers | No |
| `message_log.py` | 126 | Persistent message-log tracking | No |
| `config.py` | 126 | `AgentConfig` dataclass | Partial — defaults reference OpenAI |
| `dataclasses.py` | 91 | `Checklist`, `ChecklistItem` | No |
| `checklist.py` | 67 | Checklist transitions | No |
| `circuit_breaker.py` | 51 | Identical-failure detection | No |
| `registry.py` | 35 | `ToolRegistry` thin wrapper | No |

**Total: 13,269 LoC. OpenAI-coupled: ~2,540 LoC (19%). Provider-agnostic: ~10,729 LoC (81%).**

The harness is a *much* bigger and more sophisticated piece of software than was implied by the original architecture's "atopile is the DSL/compiler layer" framing. It's a production agent runner with:

- **Checklist-driven planning** (atopile's equivalent of `write_todos`) — `checklist_create`, `checklist_update`, `checklist_add_items` are first-class tools the model must use; the runner nudges the model to use them if it starts doing work without one.
- **Message-log nudges** — pending user messages are surfaced back to the model if it ignores them.
- **Circuit breaker** — identical tool failures trigger a non-retryable error after N attempts, forcing the model to change approach.
- **Worker-loop guards** — detects when the model is making no concrete progress and intervenes with explicit guidance.
- **Skill loading** — fixed skill bundle (`agent`, `ato`, `planning`) loaded into system prompt, with per-skill char budgets and token-aware allocation.
- **Tool-output shrinking on context overflow** — progressive shrink steps (5000 → 2500 → 1200 → 600 → 300 chars per tool result) before bailing.
- **Context compaction on overflow** — calls OpenAI's `responses.compact()` to summarize prior conversation (this is OpenAI-specific and would need a different approach for Anthropic).
- **Skill state persistence** — across turns the model gets its skill state restored.
- **Progress callbacks, trace callbacks, message callbacks, interrupt/steering callbacks** — full observability + human-in-loop hooks.
- **Pre-built test scaffolding** — `_StubProvider`, `_StubRegistry`, `TestRunner` class for unit tests without API calls.

---

## 2. The OpenAI coupling — exactly what needs to change for Anthropic

The 19% of code coupled to OpenAI lives in four places:

### 2.1 `provider.py` — the LLM client itself

The whole point of `LLMProvider` being a `Protocol` is that this is the swap point. The protocol:

```python
@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        instructions: str,                     # system prompt
        tools: list[dict[str, Any]],           # tool definitions
        skill_state: dict[str, Any],
        project_path: Any,
        previous_response_id: str | None = None,  # ← OpenAI Responses API specific
    ) -> LLMResponse: ...
```

And `LLMResponse`:

```python
@dataclass
class LLMResponse:
    id: str | None
    text: str
    tool_calls: list[ToolCall]      # normalized, provider-agnostic
    phase: str | None = None        # "commentary" | "final_answer" | None
    usage: TokenUsage | None = None
    raw: dict[str, Any] = field(default_factory=dict)
```

The existing `OpenAIProvider` (369 LoC) handles:
- API key + base URL config
- Payload construction (system prompt, tool definitions, prompt cache keys)
- Retry-with-backoff on rate limits and network errors
- Context-length-exceeded handling (shrinking tool outputs, then compacting prior responses)
- Response normalization (OpenAI Responses API output array → flat `text + tool_calls`)

An `AnthropicProvider` would replicate this surface. Estimated 400 LoC. Notable differences:

| Concern | OpenAI Responses API | Anthropic Messages API |
|---|---|---|
| Auth | `OPENAI_API_KEY` | `ANTHROPIC_API_KEY` |
| Conversation continuation | `previous_response_id` (server-side) | Full message history passed each turn (client-side) |
| Prompt caching | `prompt_cache_key` (server hashes) | `cache_control: {type: "ephemeral"}` on content blocks |
| System prompt | `instructions` parameter | `system` parameter |
| Tool definitions | `{type, name, description, parameters}` | `{name, description, input_schema}` (translate inline) |
| Tool calls in response | `output: [{type: "function_call", call_id, name, arguments}]` | `content: [{type: "tool_use", id, name, input}]` |
| Tool results back to model | `{type: "function_call_output", call_id, output}` in next turn's `input` | `{type: "tool_result", tool_use_id, content}` in next turn's `user` message |
| Reasoning tokens | OpenAI o-series | Anthropic extended thinking (different but similar) |
| Context compaction | `client.responses.compact()` server-side | **No equivalent — must implement client-side** |
| Streaming | Yes via SSE | Yes via SSE (compatible shape after normalization) |

The hardest part is **context compaction**. atopile's `OpenAIProvider._compact_previous_response()` is one API call. For Anthropic, we have to implement our own — call Claude with a summarization prompt over the prior messages, then continue from the summary. ~80 LoC.

### 2.2 `tool_definitions.py` + `tool_definitions_project.py` — schema translation

Tools are defined in OpenAI's function-calling schema. Anthropic's schema is similar but the wrapping differs:

```python
# OpenAI (atopile's current format)
{
    "type": "function",
    "name": "parts_search",
    "description": "Search physical LCSC/JLC parts.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
}

# Anthropic
{
    "name": "parts_search",
    "description": "Search physical LCSC/JLC parts.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
    }
}
```

A 30-line `_translate_tool_def(openai_def) -> anthropic_def` function does it. **No changes needed to atopile's tool definition files** — just translation at the provider boundary.

### 2.3 `orchestrator_helpers.py` — OpenAI JSON shape readers

Functions like `_extract_function_calls`, `_extract_output_phase`, `_extract_text`, `_build_function_call_outputs_for_model`, `_shrink_function_call_outputs_payload`, `_payload_has_function_call_outputs` read/write OpenAI's specific JSON structure (`output: [{type, …}]`, `function_call_output`, etc).

Options:
- **A. Translate at the provider boundary.** Have `AnthropicProvider` normalize incoming Anthropic responses to the OpenAI-shape `dict` the runner expects. Helpers don't change. Simpler. (~150 LoC of normalization.)
- **B. Refactor helpers to take normalized objects.** Cleaner long-term but touches the runner. (~300 LoC of refactor.)

Go with **A**. Anthropic responses become OpenAI-shaped dicts before they hit the runner. Provider is the only thing that knows about either API's wire format.

### 2.4 `config.py` — defaults

Easy. Change defaults to Anthropic, gate on `ANTHROPIC_API_KEY`. Keep OpenAI fields for fallback. ~30 LoC diff.

---

## 3. The three options, compared

### Option A — Build our own harness (the original plan)

**What it is.** deepagents + LangGraph + custom orchestrator + 40 lifted tool wrappers + custom prompts. Use atopile only as a Python library for tools and the build pipeline.

**LoC budget.** ~3,500 LoC of new orchestration code (orchestrator, sub-agents, workflows, state machines, prompt loaders, session management) + ~1,500 LoC of integration (tool wrappers, build pipeline glue, lifecycle management) + RAG. **~5,000 new LoC of harness/orchestration, on top of which RAG is the differentiator.**

**Pros.**
- Total control over the orchestrator's reasoning model.
- deepagents' `write_todos`/sub-agent pattern is more discoverable than checklist-based.
- LangGraph state machines (schematic emit→build→fix→simulate→verify→revise) are very explicit and easy to evaluate per node.
- Native Anthropic SDK throughout.

**Cons.**
- **We rebuild 10,729 LoC of harness logic atopile already shipped.** Most of it is hard-won lessons: circuit breaker, context shrinking, work-progress detection, checklist-nudges, message-log integration. We will hit the same problems and reinvent the same solutions.
- The skills under `.claude/skills/` are written *for atopile's harness specifically* — they reference checklist tools, message_log, design_questions. We'd have to rewrite them to use `write_todos` and deepagents' patterns. ~4,341 lines of skill rewriting.
- Two parallel agent systems exist: atopile's own (for Cursor/Claude Desktop users) and ours (for the EE agent). They will drift; bugs found in one won't be fixed in the other.
- Every atopile release breaks something on our side, *and* we have to manually port any harness improvements they ship.

**Best for.** Projects where the orchestration logic is the central value-add, and where atopile's runner doesn't fit the workflow shape (e.g. multi-board fleet management, cost-optimization across designs).

### Option B — Use atopile's harness as-is, add tools

**What it is.** Run `ato mcp serve` (or atopile's FastAPI server). Connect to it from a thin client. Augment with extra MCP tools we contribute as a separate server (RAG, Octopart, PySpice, IPC, thermal, pinmux). atopile's own agent (OpenAI-backed) does all the orchestration.

**LoC budget.** ~500 LoC of new MCP server code for our custom tools + RAG corpus + integration. Almost zero harness work.

**Pros.**
- Smallest possible code surface.
- Atopile improvements flow to us automatically on upgrade.
- We never write or maintain agent loop logic.

**Cons.**
- **OpenAI-only.** No Anthropic. The whole point of the conversation is that we want Claude as the model.
- We can't change the planning model (Opus vs Sonnet vs Haiku routing).
- We can't add LangGraph state machines for the deterministic cycles (build → fix → simulate → verify). These would still go through the agent's planning loop, which is slower and more expensive.
- We can't customize the system prompt beyond what atopile's skill loader allows.
- Our custom tools are exposed via MCP as a *peer* of atopile's tools — the model has to learn when to call ours vs theirs, which dilutes the prompt-engineering work atopile did.

**Best for.** Users who want to plug an EE assistant into Cursor / Claude Desktop / VS Code as-is. Not for what we're building.

### Option C — Fork atopile, swap to Anthropic, add tools (recommended)

**What it is.** Fork the atopile repo. Add `AnthropicProvider` next to `OpenAIProvider`. Add a config flag to choose. Register our custom tools (RAG, Octopart, PySpice, IPC, thermal, pinmux) into atopile's `ToolRegistry`. Use atopile's harness as the agent loop. Run as a long-running session (the existing FastAPI server) or as a CLI command.

**LoC budget.**
- `AnthropicProvider`: ~400 LoC
- Tool-def translator (OpenAI → Anthropic schema): ~30 LoC
- Response normalizer (Anthropic → OpenAI-shape dict): ~150 LoC
- Anthropic context compaction (no server-side equivalent): ~80 LoC
- Custom tools registered into `ToolRegistry`: ~800 LoC (RAG search, Octopart overlay, PySpice runner, IPC checker, thermal derate, pinmux validator — these are tools we'd have to write either way)
- Custom skill additions under `.claude/skills/ee-agent/` for cross-domain reasoning: ~300 LoC of markdown
- Build/CI/test config: ~200 LoC

**Total: ~1,960 LoC of new code, ~300 LoC of new skill markdown, ~0 LoC of harness rewriting.** Vs. ~5,000 LoC in option A.

**Pros.**
- **Anthropic models throughout.** Full Opus 4.7 / Sonnet 4.6 / Haiku 4.5 routing.
- **We inherit atopile's entire runner.** Checklist, circuit breaker, context shrinking, message log, worker-loop guards, skill loading, observability — all free.
- **Skills work as-shipped.** No rewriting `.claude/skills/ato/SKILL.md` for a different harness.
- **Easy to maintain.** Periodic rebase against upstream atopile is mostly a merge in `provider.py` and `config.py`. Tools we contributed live under `tools/ee_agent_*.py` which atopile doesn't touch.
- **Upstream contribution path is natural.** When `AnthropicProvider` matures, PR it back to atopile/atopile — they probably want it (their roadmap mentions multi-provider support). Then we drop the fork and live on main.
- **Our differentiator (RAG corpus) plugs in as a tool.** No special integration needed. The schematic agent calls `rag_search(query, corpus="datasheets")` just like it calls `packages_search`.

**Cons.**
- We carry a fork. Rebase work each atopile release (estimated 2–4 hours quarterly given the boundary is small).
- **No LangGraph state machines** for the build→fix→simulate→verify cycle. atopile's runner is a single ReAct-style loop. The cycles still happen, but as agent-driven decisions rather than deterministic edges. This is fine for first-pass design but loses the per-node eval granularity LangGraph would give.
- We commit to atopile's planning model (checklist-based) rather than deepagents' (write_todos-based). Both are good; this is a preference question, not a correctness question.
- Anthropic doesn't have a native equivalent to OpenAI's server-side `responses.compact()`. Our client-side compaction will be slower (one extra LLM call per compaction) and may not match OpenAI's quality. Manageable but worth knowing.

**Best for.** Exactly what we're building. Maximum reuse of atopile's investment, full Anthropic stack, our differentiator plugged in cleanly.

---

## 4. What we lose vs. Option A

Worth being explicit:

### 4.1 No LangGraph state machines

The original plan had deterministic LangGraph cycles for schematic (emit→build→fix→…) and verification (ato_check→design_diagnostics→erc→drc→sim_check→bom_check→ipc_check→thermal_check→pinmux_check→summary). Under Option C, these become agent-driven sequences — the LLM decides when to call `build_run`, when to call `design_diagnostics`, when to call our `rag_search`, when to declare done.

In practice atopile already encodes most of this in skills. The `ato/SKILL.md` §5 (Troubleshooting) is the equivalent of our `fix_errors_node` prompt. The checklist mechanism encodes the sequencing. We lose:
- Hard caps per node (e.g. "5 fix iterations max"). atopile uses `max_tool_loops: 240` per turn instead, which is per-turn not per-cycle.
- Per-node eval datasets in LangSmith. Eval becomes per-skill or per-tool instead.
- Visual workflow diagrams from LangGraph's renderer.

Workarounds:
- Wrap our verification flow as a custom *meta-tool* called `run_verification` that internally sequences the checks and returns aggregated findings. The agent calls one tool; under the hood it's a LangGraph node. Best of both worlds. ~200 LoC.
- Same pattern for any other multi-step deterministic flow we want hardcoded.

### 4.2 No deepagents virtual filesystem

deepagents' `ls`/`read_file`/`write_file`/`edit_file` write to a sandboxed virtual FS. atopile writes directly to disk in `project_root`. For our use case (each session has its own workspace directory), this is fine — we just `mkdir -p /workspace/<session_id>/elec/` per session and set that as `project_root`.

### 4.3 No sub-agent context isolation

deepagents spawns sub-agents in fresh contexts. atopile runs one agent throughout. This means:
- More tokens per session (no context-shedding boundary between requirements/topology/parts/schematic).
- Risk of cross-task pollution (e.g. a parts question dragging in the full schematic context).

Mitigations:
- atopile's runner *does* compact context on overflow (server-side for OpenAI, client-side for our Anthropic version).
- Skills are loaded per-turn into the system prompt; they don't bloat per-turn cost.
- Tool outputs that exceed budget are shrunk progressively before the call fails.

For most designs the context budget is fine. For very large designs we may need to add session-boundary sub-agent spawning later — but that can be a v2 problem.

---

## 5. The migration

If we go Option C, the project becomes:

```
ee-agent-fork/                          # fork of atopile/atopile
├── (upstream atopile structure unchanged)
├── src/atopile/server/agent/
│   ├── provider.py                     # ← add AnthropicProvider class here
│   ├── provider_anthropic.py           # ← OR put it in a separate file
│   ├── config.py                       # ← add Anthropic config knobs
│   ├── _ee_tools/                      # ← our custom tools live here
│   │   ├── __init__.py
│   │   ├── rag_search.py               # RAG search tool
│   │   ├── octopart_overlay.py         # qualified parts
│   │   ├── pyspice_runner.py           # simulation
│   │   ├── ipc_check.py                # IPC compliance
│   │   ├── thermal_check.py            # thermal derating
│   │   ├── pinmux_check.py             # firmware/HW co-design
│   │   └── verification_meta.py        # sequenced verification meta-tool
│   └── tool_definitions_ee.py          # ← schemas for our tools, registered in registry
├── .claude/skills/
│   ├── (upstream skills unchanged)
│   └── ee-agent/                       # ← our cross-domain skill
│       └── SKILL.md
├── src/ee_agent_rag/                   # ← RAG ingestion lives outside agent/
│   ├── ingest.py
│   ├── retriever.py
│   └── ...
└── pyproject.toml                      # ← add anthropic, voyageai, cohere, etc deps
```

Implementation order (vs. the 14 milestones in `08_PROJECT_PLAN.md`, condensed):

1. **Fork + setup.** `git clone https://github.com/atopile/atopile && git remote rename origin upstream && git remote add origin <our_fork>`. Add Anthropic/Voyage/Cohere/PySpice deps. (1 day)
2. **AnthropicProvider stub.** Just enough to pass atopile's own test suite using a stub registry. (2 days)
3. **AnthropicProvider full.** Tool-def translation, response normalization, retry logic, client-side compaction. Tests passing against real Anthropic API on a trivial design. (3 days)
4. **RAG corpus + `rag_search` tool.** Existing plan in `RAG_IMPLEMENTATION_PLAN.md`. Register as an EE-agent tool. (2 weeks)
5. **Custom tools.** Octopart, PySpice, IPC, thermal, pinmux. Each is a tool registered into atopile's `ToolRegistry`. (3 weeks parallelizable)
6. **EE-agent skill.** New `.claude/skills/ee-agent/SKILL.md` covering cross-domain reasoning, RAG citation contract, qualified-parts logic. (3 days)
7. **First real design.** End-to-end test on a real design with citations. (1 week, iterative)
8. **Eval suite.** Use atopile's test harness pattern; add EE-agent specific scenarios. (1 week)

Wall-clock: ~6 weeks for one engineer, ~4 weeks with two in parallel. Comparable to Option A but with a much smaller LoC surface and less rewriting.

---

## 6. The kicker: upstream-friendly path

Atopile's `LLMProvider` being a `Protocol` strongly suggests **they explicitly designed for multi-provider support but didn't ship it yet**. The path from "we have a working AnthropicProvider" to "upstream merges our PR" is short:

1. Build AnthropicProvider in our fork.
2. Stabilize it on real designs over 2–3 months.
3. PR to atopile/atopile with tests and a config-driven provider switch.
4. They merge it (high probability — it's purely additive, well-defined boundary).
5. We delete the fork, depend on upstream atopile, keep only our custom tools as a downstream addition.

Per `07_ATOPILE_GAPS.md`, this is exactly the kind of upstream contribution we should be making.

---

## 7. Decision

**Recommended: Option C — fork atopile, add AnthropicProvider, register custom tools, plug in RAG.**

This collapses the project from ~14,000 LoC of new orchestration code to ~2,000 LoC of provider + tool overlay, gives us atopile's hard-won runner logic for free, keeps skills working as shipped, and sets up a natural upstream-contribution path.

The earlier deepagents + LangGraph plan (Option A) is still appropriate if we discover atopile's harness can't handle our workload — but we should start with C and only fall back to A if a concrete blocker appears.

---

## 8. Implications for other docs

If we adopt Option C:

- **`00_ARCHITECTURE.md`** — rewrite around atopile-harness-with-Anthropic-provider as the foundation. Drop deepagents + LangGraph from the stack list.
- **`01_ORCHESTRATOR.md`** — replace with "atopile runner + AnthropicProvider + EE-agent skill". Much shorter doc.
- **`04_VERIFICATION.md`** — keep as a meta-tool (`run_verification`) that internally sequences the checks. ~30% the size.
- **`06_ATOPILE_INTEGRATION.md`** — rewrite as the fork-and-provider plan.
- **`07_ATOPILE_GAPS.md`** — §2.1 (MCP exposes only 9 of 40 tools) becomes irrelevant since we don't use MCP at all. Other gaps remain.
- **`08_PROJECT_PLAN.md`** — milestone list compresses significantly. ~6 weeks instead of 6–8 weeks, with smaller scope per milestone.

If we **don't** adopt Option C, the existing docs stand as-is.

---

## 9. Open questions for the team

Before committing to a path, three open questions:

1. **Anthropic context compaction.** We need to verify our client-side compaction (one Claude call to summarize prior context) is good enough on real designs. Cheap to spike — try it on a 50-tool-call session and inspect.
2. **Skill compatibility.** atopile's skills reference checklist tools and message_log explicitly. We need to verify Claude follows them as well as the OpenAI model atopile was trained against. Cheap to spike — run atopile's existing test designs against a stub AnthropicProvider that returns hard-coded responses.
3. **Long-running session model.** atopile assumes a long-lived FastAPI process (the agent server) with WebSocket clients connecting in. Our deployment might prefer one-shot CLI invocations. atopile's `runner.run_turn()` already supports one-shot use (it takes full `history` as input and returns `AgentTurnResult`), so this is fine — but worth confirming on a real workload.

None of these are blockers. They're worth checking before committing.
