# 12 · Dev & Test Harness for the Agent Loop

> **Purpose.** Concrete plan for *developing* and *testing* `runner.py` (the `while(tool_calls)` agent loop) inside the atopile fork under Option C. Covers the three tiers we run in: (1) **unit tests** with stubbed provider/registry, (2) **replay tests** against canned LLM transcripts, (3) **live dev** via `ato serve backend` + `ato serve frontend` with a tap into the in-process `AgentRunner`. The goal is a fast inner loop that does **not** burn provider credits on every iteration and that still verifies the real wire shape before merge.

---

## 1. What we are testing

`AgentRunner.run_turn(...)` (file: `src/atopile/server/agent/runner.py`) takes:

```python
async def run_turn(
    *,
    ctx: AppContext,
    project_root: str,
    history: list[dict[str, str]],
    user_message: str,
    session_id: str = "",
    selected_targets: list[str] | None = None,
    previous_response_id: str | None = None,
    prior_skill_state: dict[str, Any] | None = None,
    tool_memory: dict[str, dict[str, Any]] | None = None,
    progress_callback: ProgressCallback | None = None,
    consume_steering_messages: SteeringMessagesCallback | None = None,
    consume_interrupt_messages: InterruptMessagesCallback | None = None,
    stop_requested: StopRequestedCallback | None = None,
    message_callback: MessageCallback | None = None,
    trace_callback: TraceCallback | None = None,
) -> AgentTurnResult
```

It depends on three injected collaborators that are the **only** seams worth stubbing:

| Collaborator | Type | Where it lives |
|---|---|---|
| `provider` | `LLMProvider` (Protocol) | `src/atopile/server/agent/provider.py` |
| `registry` | `ToolRegistry` (duck-typed: `definitions()` + `async execute()`) | `src/atopile/server/agent/registry.py` |
| `config`   | `AgentConfig` | `src/atopile/server/agent/config.py` |

Everything else (checklist, circuit breaker, message log, skill loader, observability) is internal state. We exercise it indirectly through the three seams above plus the optional `progress_callback` / `trace_callback`.

There are two free functions that the runner consults during a turn and that pull in real filesystem + LLM-side work — these get **monkeypatched** in tests rather than stubbed via DI:

- `atopile.server.agent.runner.build_system_prompt`
- `atopile.server.agent.runner.build_initial_user_message`

Upstream's in-file `TestRunner` class already follows exactly this pattern (see `runner.py` lines 2124–2891). Our harness inherits its conventions.

---

## 2. Three tiers of test/dev loop

### Tier 1 — Unit tests against a stub provider (fast, free)

**Goal.** Pin loop invariants: checklist transitions, circuit-breaker behavior, work-progress nudges, force-end on `design_questions`, stop/interrupt handoffs, timeout, empty-continuation cap.

**Cost per run.** Zero. No network. No model call.
**Wall clock.** Milliseconds per case.
**Where they live.** `tests/ee/test_runner_loop.py` (new file). The upstream pattern of co-located `TestRunner` in `runner.py` itself is fine but we keep ours in `tests/ee/` so a future upstream rebase does not collide.

**Pattern (boilerplate).**

```python
# tests/ee/test_runner_loop.py
import asyncio
from pathlib import Path

import pytest

from atopile.dataclasses import AppContext
from atopile.server.agent.runner import AgentRunner
from atopile.server.agent.provider import LLMResponse, ToolCall


class _ScriptedProvider:
    """Plays back a deterministic list of LLMResponse objects."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, *, messages, instructions, tools,
                       skill_state, project_path,
                       previous_response_id=None) -> LLMResponse:
        self.calls.append({
            "messages": messages,
            "tools": [t.get("name") for t in tools],
            "previous_response_id": previous_response_id,
        })
        if not self._responses:
            return LLMResponse(id="resp_done", text="", tool_calls=[],
                               phase="final_answer")
        return self._responses.pop(0)


class _ScriptedRegistry:
    """Returns canned tool outputs by tool name."""

    def __init__(self, tool_defs: list[dict], outputs: dict) -> None:
        self._defs = tool_defs
        self._outputs = outputs

    def definitions(self) -> list[dict]:
        return self._defs

    async def execute(self, tool_name, args, project_path, ctx):
        return self._outputs.get(tool_name, {"ok": True, "args": args})


def _config(**overrides):
    base = {
        "model": "test-model",
        "max_tool_loops": 8,
        "max_turn_seconds": 60,
        "max_checklist_continuations": 0,
        "silent_retry_max": 0,
        "trace_enabled": False,
        "trace_preview_max_chars": 200,
        "tool_output_max_chars": 10_000,
        "context_summary_max_chars": 2_000,
        "user_message_max_chars": 2_000,
    }
    base.update(overrides)
    return type("Cfg", (), base)()


def _stub_prompt_builders(monkeypatch, *, skill_state=None):
    monkeypatch.setattr(
        "atopile.server.agent.runner.build_system_prompt",
        lambda **_: ("system prompt", skill_state or {}),
    )

    async def _fake_initial(**_):
        return "user message"

    monkeypatch.setattr(
        "atopile.server.agent.runner.build_initial_user_message",
        _fake_initial,
    )


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    (p / "ato.yaml").write_text("builds: {}\n", encoding="utf-8")
    return p
```

Each test case becomes a script: list of `LLMResponse` + dict of tool outputs.

```python
def test_loop_stops_after_final_answer(monkeypatch, project_root):
    _stub_prompt_builders(monkeypatch)

    provider = _ScriptedProvider([
        LLMResponse(
            id="r1", text="",
            tool_calls=[ToolCall(id="c1", name="project_read_file",
                                 arguments_raw='{"path":"main.ato"}',
                                 arguments={"path": "main.ato"})],
            phase=None,
        ),
        LLMResponse(id="r2", text="Done.", tool_calls=[],
                    phase="final_answer"),
    ])
    registry = _ScriptedRegistry(
        tool_defs=[{"type": "function", "name": "project_read_file"}],
        outputs={"project_read_file": {"ok": True, "content": "module main"}},
    )

    runner = AgentRunner(config=_config(), provider=provider, registry=registry)
    result = asyncio.run(runner.run_turn(
        ctx=AppContext(workspace_paths=[project_root.parent]),
        project_root=str(project_root),
        history=[],
        user_message="What does main.ato contain?",
        session_id="s1",
    ))

    assert result.text == "Done."
    assert len(provider.calls) == 2
    assert provider.calls[1]["previous_response_id"] == "r1"
```

**Invariants worth a dedicated test.**

| Invariant | Why it matters |
|---|---|
| `final_answer` phase ends the loop on the same turn | No runaway loops |
| `previous_response_id` is threaded turn-to-turn | OpenAI Responses chain integrity (Anthropic provider must also accept the kwarg without error) |
| Checklist `doing→done` transitions are valid per `VALID_TRANSITIONS` | Skill-driven planning correctness |
| `max_tool_loops` is enforced | Cost cap |
| `max_turn_seconds` triggers `_build_turn_time_budget_stop_text` and *still* closes any open tool chain | Chain integrity under timeout |
| `stop_requested()` produces a graceful handoff | UI "stop" button works |
| `consume_interrupt_messages()` injects a user message and ends the turn | UI interrupt works |
| `_MAX_EMPTY_CONTINUATIONS` (5) cap fires when model never calls tools | Stuck-model detection |
| `_REGROUND_TOOLS_MSG` is injected when text matches `_STUCK_TOOL_PATTERNS` | Re-grounding works |
| Work-progress nudges fire after N tool calls without a checklist | Planning discipline |
| `design_questions` force-end closes the provider chain (tools=[] on closing call) | Atopile's design-question UX |
| `progress_callback` receives `phase="thinking"` events for commentary preambles | Live UI activity |
| `tool_traces` returned in `AgentTurnResult` match number of executed tools | Caller observability |

Most of these already have one (sometimes incomplete) case in `runner.py`'s embedded `TestRunner`. We port the pattern into `tests/ee/test_runner_loop.py` and add coverage where missing.

### Tier 2 — Replay tests against frozen transcripts (fast, free, regression-grade)

**Goal.** Catch regressions in the *shape* of provider payloads (history serialization, tool definitions, prompt cache key) without paying for a model call. This is also how we get a fair parity test between `OpenAIProvider` and `AnthropicProvider` (M2 in `08_PROJECT_PLAN.md`).

**Mechanism.** Each transcript is a JSON file under `tests/ee/transcripts/<scenario>/`:

```
tests/ee/transcripts/coin_cell_blinky/
├── turn_001.json          # input: user_message + prior state
├── turn_001.expected.json # expected: tool_calls (names+args), final text, key skill-state deltas
├── turn_002.json
└── turn_002.expected.json
```

`turn_NNN.json` carries the **scripted provider responses** for that turn. The replay harness wires them into `_ScriptedProvider` from Tier 1, runs `run_turn`, then asserts the captured `provider.calls` and `result` against `turn_NNN.expected.json`.

Capturing transcripts is a one-time cost: run the real provider once (Tier 3, live dev), dump the inputs+responses, freeze. After that, the suite is offline.

**Use cases for Tier 2.**

1. **Provider parity** — same scripted-response transcript replayed against `OpenAIProvider`'s normalizer and `AnthropicProvider`'s normalizer must yield identical `LLMResponse` shapes.
2. **Tool-definition snapshot** — the OpenAI-format tool definitions our `_ee/tool_definitions_ee.py` emits get snapshotted; any drift triggers a deliberate review.
3. **End-to-end loop scenarios** — script a 6-turn coin-cell-blinky design and assert no checklist invariants regress.
4. **Prompt-cache-key stability** — the cache key from `_build_prompt_cache_key` must be stable across runs for the same project state (changes invalidate cache and cost money).

**Don't snapshot.** Token counts, response IDs, timestamps, internal UUIDs. Only assert on shape and the fields we care about.

### Tier 3 — Live dev via `ato serve` (slow, paid, real)

**Goal.** Smoke-test the real provider, the real registry, the real frontend wiring before merging. Find tool/prompt issues only visible against a real model.

**Two-process workflow.**

```bash
# Terminal 1: backend (FastAPI, port 8501)
source .venv/bin/activate
ATOPILE_AGENT_MODEL=claude-sonnet-4-6 \
EE_AGENT_PROVIDER=anthropic \
ATOPILE_AGENT_ANTHROPIC_API_KEY=sk-ant-... \
ATOPILE_AGENT_TRACE_ENABLED=1 \
ato serve backend --workspace ./examples --force

# Terminal 2: frontend (Vite, port 5173, proxies to backend)
ato serve frontend --backend localhost:8501
```

What happens under the hood:

- `ato serve backend` runs `src/atopile/server/server.py::run_server`, which boots FastAPI and mounts `routes/agent/main.py`.
- `routes/agent/utils.py` instantiates a **module-level singleton** `AgentRunner(config=AgentConfig.from_env(), provider=OpenAIProvider(...), registry=ToolRegistry())`. Under our fork's `config.py` patch (M2), the provider becomes `AnthropicProvider` when `EE_AGENT_PROVIDER=anthropic`.
- `ato serve frontend` builds (if needed) and runs the Vite dev server in `src/ui-server/`, with `VITE_API_URL` and `VITE_WS_URL` pointing at the backend.
- The frontend opens a WebSocket to `/ws/state` for progress events and POSTs `/api/agent/sessions/{sid}/messages` to send a user turn.
- Every turn writes structured events to `~/.atopile/agent_logs.sqlite` via `AgentLogs.append_chunk(...)` — that DB is the post-mortem you tail when something goes wrong.

**Tightening the inner loop.**

| Pain | Fix |
|---|---|
| Backend reloads slowly on every Python edit | Wrap `run_server` in `uvicorn --reload` via a thin `ato dev serve --reload` shim (proposed below) |
| Frontend rebuilds Pydantic→TS types on every backend start | Pass `--no-gen` to `ato serve backend` once your schemas are stable for the session |
| Re-running the same prompt costs money | `ATOPILE_AGENT_MODEL=gpt-4.1-nano` or the cheapest Claude tier for chrome-only checks |
| Need to inspect mid-turn state | Tail `~/.atopile/agent_logs.sqlite`: `sqlite3 ~/.atopile/agent_logs.sqlite 'select event, phase, tool_name, summary from agent_events where run_id="..." order by id;'` |
| Need to see the exact payload sent to the model | Wrap the provider in a logging decorator (see Section 4) |

**Note on the singleton.** `utils.py` constructs `_config` and `orchestrator` at import time. That means:

- Env-var changes after import don't take effect — always restart the backend after `export EE_AGENT_PROVIDER=...`.
- Tier 1/2 tests must **not** import `routes.agent.utils` (it would try to read env at collection time). Tests construct their own `AgentRunner` directly.

---

## 3. Where each test class of test lives (proposed layout)

```
tests/ee/
├── conftest.py                          # shared fixtures: project_root, _config, prompt-builder stubs
├── test_runner_loop.py                  # Tier 1 — invariants on AgentRunner.run_turn
├── test_anthropic_provider.py           # M2 — provider unit tests
├── test_provider_parity.py              # Tier 2 — same scripted transcript, both providers
├── test_rag_search_tool.py              # M4
├── test_pyspice_run_tool.py             # M5
├── test_pinmux_check_tool.py            # M6
├── test_ipc_check_tool.py               # M7
├── test_tool_definitions_snapshot.py    # Tier 2 — JSON-schema snapshot guard
├── replay/
│   ├── _harness.py                      # generic transcript player (uses _ScriptedProvider)
│   └── test_coin_cell_blinky.py         # replays transcripts/coin_cell_blinky/*
└── transcripts/
    └── coin_cell_blinky/
        ├── turn_001.json
        ├── turn_001.expected.json
        └── ...
```

Discovery is via `ato dev test --llm -k ee` (the orchestrated runner already picks up anything under `src/` and `test/`; we add `tests/ee/` to the default pytest paths in `pyproject.toml` so the same flag picks them up).

```toml
# pyproject.toml additions for the fork
[tool.pytest.ini_options]
testpaths = ["src", "test", "tests/ee"]
```

---

## 4. The provider logging decorator (for live dev)

When debugging a real-model turn, we want every payload + response on disk. A tiny decorator does this without modifying `OpenAIProvider` or `AnthropicProvider`:

```python
# src/atopile/server/agent/_ee/provider_logging.py
"""Wrap any LLMProvider to dump payload+response JSON for offline replay."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from atopile.server.agent.provider import LLMResponse


class LoggingProvider:
    def __init__(self, inner, dump_dir: Path) -> None:
        self._inner = inner
        self._dir = dump_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._n = 0

    async def complete(self, **kwargs) -> LLMResponse:
        self._n += 1
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        prefix = self._dir / f"{stamp}-{self._n:04d}"

        # Dump request (drop unpickleable values)
        req = {k: _safe(v) for k, v in kwargs.items()}
        prefix.with_suffix(".req.json").write_text(json.dumps(req, indent=2))

        resp = await self._inner.complete(**kwargs)

        resp_dump = {
            "id": resp.id,
            "text": resp.text,
            "tool_calls": [
                {"id": tc.id, "name": tc.name,
                 "arguments_raw": tc.arguments_raw,
                 "arguments": tc.arguments}
                for tc in resp.tool_calls
            ],
            "phase": resp.phase,
            "raw": resp.raw,
        }
        prefix.with_suffix(".resp.json").write_text(json.dumps(resp_dump, indent=2))
        return resp


def _safe(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except TypeError:
        return repr(v)
```

Enabled via env flag (handled in `routes/agent/utils.py` after the M2 edit):

```python
if os.getenv("EE_AGENT_LOG_PROVIDER"):
    from atopile.server.agent._ee.provider_logging import LoggingProvider
    provider = LoggingProvider(provider, Path("~/.atopile/provider_dumps").expanduser())
```

The dumps double as Tier-2 transcript seeds: copy a turn's `.req.json` into `tests/ee/transcripts/...` and we have a replay case.

---

## 5. Test/dev recipes

### Recipe — write a new loop invariant test

1. Reproduce the bug or scenario as a sequence of `LLMResponse` + tool outputs in your head.
2. Drop into `tests/ee/test_runner_loop.py`, copy a similar `_ScriptedProvider`-based case.
3. Run only that test: `ato dev test --llm --direct -k test_my_new_case`.
4. Make it pass; commit.

### Recipe — capture a real-model transcript for replay

```bash
EE_AGENT_LOG_PROVIDER=1 \
EE_AGENT_PROVIDER=anthropic \
ATOPILE_AGENT_MODEL=claude-sonnet-4-6 \
ato serve backend --workspace ./examples --force
# (drive a turn from the frontend)

ls ~/.atopile/provider_dumps/   # *.req.json / *.resp.json
# Move the .resp.json files into tests/ee/transcripts/<scenario>/turn_NNN.json
# Write turn_NNN.expected.json by hand (just the fields you care about).
```

### Recipe — debug a stuck loop live

1. Backend up with `ATOPILE_AGENT_TRACE_ENABLED=1`.
2. Trigger the bad turn from the frontend.
3. Tail the SQLite event log:
   ```bash
   sqlite3 ~/.atopile/agent_logs.sqlite \
     "select id, event, phase, tool_name, summary
        from agent_events
       where run_id='<run_id_from_url>'
       order by id;"
   ```
4. Look for the last `tool.executed` followed by an empty model response, then check whether any of the nudge events (`runner.checklist_nudge`, `runner.work_progress_nudge`, `runner.reground_tools`) fired.
5. Reproduce as a Tier-1 unit test with the matching `LLMResponse` script.

### Recipe — run only EE tests, fast

```bash
ato dev test --llm -k ee        # everything under tests/ee/
ato dev test --reuse            # rebuild reports without rerunning
```

### Recipe — switch providers mid-session

You can't (singleton at import). Restart `ato serve backend` with a different `EE_AGENT_PROVIDER`. Frontend reconnects automatically via the WebSocket reconnect logic.

---

## 6. Optional — proposed `ato dev serve` shim

Upstream `ato serve backend` does **not** auto-reload Python. For our fork's inner loop we'd add (under `src/atopile/cli/dev.py`):

```python
@dev_app.command()
def serve(
    reload: bool = typer.Option(True, "--reload/--no-reload"),
    workspace: Optional[list[Path]] = typer.Option(None, "--workspace", "-w"),
    port: int = typer.Option(8501),
) -> None:
    """Backend with uvicorn --reload for fast dev."""
    import uvicorn
    from atopile.server.server import create_app
    os.environ.setdefault("ATOPILE_AGENT_TRACE_ENABLED", "1")
    uvicorn.run(
        "atopile.server.server:create_app",
        factory=True,
        host="127.0.0.1",
        port=port,
        reload=reload,
        reload_dirs=["src/atopile/server", "src/atopile/server/agent"],
    )
```

This is a *fork* convenience; not required to be upstream-mergeable. Behind a flag in `08_PROJECT_PLAN.md` M3.

---

## 7. CI matrix

```yaml
# .github/workflows/ee.yml
- run: ato dev test --llm -k ee --ci          # Tier 1 + Tier 2
  env:
    EE_AGENT_PROVIDER: openai                 # no key needed; scripted provider
- run: ato dev test --llm -k provider_parity --ci
  env:
    EE_AGENT_PROVIDER: anthropic              # ditto; parity test uses scripted dumps
```

Live-model tests (Tier 3) are **never** in PR CI. They run on `cron` against the eval suite (M8) and gate releases, not PRs.

---

## 8. Acceptance criteria for this harness

We consider Section 1 of `08_PROJECT_PLAN.md` Milestone 3 ("Tool registration plumbing") done only when:

- [ ] `tests/ee/test_runner_loop.py` exists with at least the 12 invariants from Section 2 Tier 1 table.
- [ ] `tests/ee/conftest.py` provides `project_root`, `_config`, `_stub_prompt_builders`, `_ScriptedProvider`, `_ScriptedRegistry` as fixtures importable from any EE test.
- [ ] `ato dev test --llm -k ee` passes on a fresh clone, with no env vars set, in <30s.
- [ ] `provider_logging.LoggingProvider` is wired behind `EE_AGENT_LOG_PROVIDER=1` and a captured dump round-trips through a Tier-2 replay case.
- [ ] `ato serve backend` + `ato serve frontend` together send and receive a turn from the EE-tools-registered fork using **one** registered stub tool (`_ee_ping`).

---

## 9. Open questions to resolve before M4

1. **Singleton vs per-request `AgentRunner`.** Module-level singleton in `routes/agent/utils.py` is simple but blocks env-var changes between turns. Worth a per-request constructor? — Trade-off: slower per-turn instantiation vs hot-swappable provider. Default: leave alone, document the restart-to-change rule.
2. **Are `build_system_prompt` / `build_initial_user_message` reasonable monkeypatch seams?** They are the de-facto seams in upstream tests. If we add a new test that needs the real system prompt, we'd pay the file-read cost — acceptable.
3. **Where do we keep the captured transcripts?** Git-tracked under `tests/ee/transcripts/`. Size is small (KB per turn). Avoid LFS unless a single transcript grows past a few hundred KB.
4. **Do we add a CLI for replaying a transcript outside pytest?** Convenience only. Defer until we have ≥3 transcripts.

---

## 10. Cross-references

- `00_ARCHITECTURE.md` — overall stack picture; the harness sits between sections 2 and 9.
- `08_PROJECT_PLAN.md` — Milestone 3 is gated by the acceptance criteria in Section 8 above.
- `06_ATOPILE_INTEGRATION.md` — `_ee/` module layout and the two minimal upstream edits this harness depends on.
- `11_ANTHROPIC_PROVIDER.md` — provider parity (Tier 2) is the chief verifier for that work.
- `09_HARNESS_ANALYSIS.md` — rationale for keeping the upstream runner intact (i.e., why this harness exists at all).
