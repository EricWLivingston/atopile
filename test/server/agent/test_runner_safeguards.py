"""Spend/failure safeguards in the agent runner: per-turn token budget, repeated
build-failure escalation, and the failure-budget early stop.

All offline — a configurable stub provider/registry drives ``run_turn`` and we assert
on the turn result + emitted progress events. Mirrors the harness setup in
``runner.TestRunner`` (fakes ``build_system_prompt`` / ``build_initial_user_message``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from atopile.dataclasses import AppContext
from atopile.server.agent.provider import LLMResponse, TokenUsage, ToolCall
from atopile.server.agent.runner import (
    AgentRunner,
    _build_attempt_failed,
    _escalate_model,
)

_BUILD_ERROR_RESULT = {
    "truncated": True,
    "preview": (
        '{"build_id": "abc", "logs": [{"stage": "init-build-context", '
        '"level": "ERROR", "message": "Field `package.BYP` could not be resolved"}]}'
    ),
}


# ── pure helpers ──────────────────────────────────────────────────────


def test_escalate_model_ladder():
    cfg = type(
        "Cfg", (), {"model": "std", "model_simple": "small", "model_complex": "big"}
    )()
    assert _escalate_model("small", cfg) == "std"
    assert _escalate_model("std", cfg) == "big"
    assert _escalate_model("big", cfg) == "big"  # capped at top


def test_escalate_model_noop_without_tiers():
    cfg = type("Cfg", (), {"model": "std", "model_simple": "", "model_complex": ""})()
    assert _escalate_model("std", cfg) == "std"


def test_build_attempt_failed_detects_error_logs():
    assert _build_attempt_failed("build_logs_search", _BUILD_ERROR_RESULT) is True
    # clean build logs → not a failure
    assert _build_attempt_failed("build_logs_search", {"logs": []}) is False
    # other tools are never build failures
    assert _build_attempt_failed("project_read_file", _BUILD_ERROR_RESULT) is False


# ── run_turn harness ──────────────────────────────────────────────────


@pytest.fixture
def stub_project(monkeypatch, tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "ato.yaml").write_text("builds: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "atopile.server.agent.runner.build_system_prompt",
        lambda **_: ("system prompt", {}),
    )

    async def _fake_initial_user_message(**_: object) -> str:
        return "user message"

    monkeypatch.setattr(
        "atopile.server.agent.runner.build_initial_user_message",
        _fake_initial_user_message,
    )
    return project_root


def _cfg(**overrides: Any):
    attrs: dict[str, Any] = {
        "model": "standard-model",
        "model_simple": "simple-model",
        "model_complex": "complex-model",
        "dynamic_model": False,
        "max_tool_loops": 20,
        "max_turn_seconds": 60,
        "max_turn_tokens": 0,
        "max_build_failures": 4,
        "max_checklist_continuations": 0,
        "silent_retry_max": 0,
        "trace_enabled": False,
        "trace_preview_max_chars": 200,
        "tool_output_max_chars": 10_000,
        "context_summary_max_chars": 2_000,
        "user_message_max_chars": 2_000,
    }
    attrs.update(overrides)
    return type("Cfg", (), attrs)()


class _BuildLoopProvider:
    """Emits a ``build_logs_search`` call each loop until ``stop_after`` loops, then a
    final answer. ``tokens_per_call`` drives cumulative telemetry for the budget."""

    def __init__(
        self, *, stop_after: int = 100, tokens_per_call: int = 0
    ) -> None:
        self.calls = 0
        self.models: list[str | None] = []
        self._stop_after = stop_after
        self._tokens = tokens_per_call

    async def complete(self, *, model=None, **_: Any) -> LLMResponse:
        self.calls += 1
        self.models.append(model)
        usage = (
            TokenUsage(
                input_tokens=self._tokens,
                output_tokens=0,
                total_tokens=self._tokens,
            )
            if self._tokens
            else None
        )
        if self.calls > self._stop_after:
            return LLMResponse(
                id=f"resp_{self.calls}", text="Done.", tool_calls=[],
                phase="final_answer", usage=usage,
            )
        return LLMResponse(
            id=f"resp_{self.calls}",
            text="",
            tool_calls=[
                ToolCall(
                    id=f"call_{self.calls}",
                    name="build_logs_search",
                    arguments_raw='{"build_id":"abc","log_levels":["ERROR","ALERT"]}',
                    arguments={"build_id": "abc", "log_levels": ["ERROR", "ALERT"]},
                )
            ],
            usage=usage,
        )


class _BuildErrorRegistry:
    def definitions(self) -> list[dict[str, object]]:
        return [{"type": "function", "name": "build_logs_search"}]

    async def execute(self, tool_name, args, project_path, ctx):
        _ = tool_name, args, project_path, ctx
        return dict(_BUILD_ERROR_RESULT)


def _run(
    runner: AgentRunner,
    project_root: Path,
    events: list[dict[str, Any]] | None = None,
):
    async def _progress(payload: dict[str, Any]) -> None:
        if events is not None:
            events.append(payload)

    return asyncio.run(
        runner.run_turn(
            ctx=AppContext(workspace_paths=[project_root.parent]),
            project_root=str(project_root),
            history=[],
            user_message="Design a board",
            session_id="",
            progress_callback=_progress if events is not None else None,
        )
    )


# ── escalation + failure budget ───────────────────────────────────────


def test_repeated_build_failures_escalate_then_stop(stub_project: Path):
    provider = _BuildLoopProvider(stop_after=100)  # never finishes on its own
    runner = AgentRunner(
        config=_cfg(dynamic_model=True, model="simple-model", max_build_failures=4),
        provider=provider,
        registry=_BuildErrorRegistry(),
    )
    events: list[dict[str, Any]] = []
    result = _run(runner, stub_project, events)

    # Failure-budget stop fired (graceful — a normal AgentTurnResult, no exception).
    assert "build attempts kept failing" in result.text
    # Model escalated mid-turn: started simple, climbed before the stop.
    escalations = [
        e for e in events
        if e.get("step_kind") == "model_routed"
        and e.get("reason") == "build_failure_escalation"
    ]
    assert escalations, "expected at least one build_failure_escalation event"
    assert provider.models[0] == "simple-model"
    assert "complex-model" in provider.models or "standard-model" in provider.models


def test_failure_budget_disabled_when_zero(stub_project: Path):
    # With the failure budget off, the run is bounded only by max_tool_loops.
    provider = _BuildLoopProvider(stop_after=100)
    runner = AgentRunner(
        config=_cfg(max_build_failures=0, max_tool_loops=5),
        provider=provider,
        registry=_BuildErrorRegistry(),
    )
    result = _run(runner, stub_project)
    assert "build attempts kept failing" not in result.text


# ── token budget ──────────────────────────────────────────────────────


def test_token_budget_stops_turn(stub_project: Path):
    # 1000 tokens/call, budget 1500 → stop on the 2nd loop's budget check.
    provider = _BuildLoopProvider(stop_after=100, tokens_per_call=1000)
    runner = AgentRunner(
        config=_cfg(max_turn_tokens=1500, max_build_failures=0),
        provider=provider,
        registry=_BuildErrorRegistry(),
    )
    result = _run(runner, stub_project)
    assert "token budget" in result.text
    # stopped early, well before max_tool_loops
    assert provider.calls <= 4


def test_token_budget_disabled_when_zero(stub_project: Path):
    provider = _BuildLoopProvider(stop_after=2, tokens_per_call=10_000)
    runner = AgentRunner(
        config=_cfg(max_turn_tokens=0, max_build_failures=0),
        provider=provider,
        registry=_BuildErrorRegistry(),
    )
    result = _run(runner, stub_project)
    assert "token budget" not in result.text
