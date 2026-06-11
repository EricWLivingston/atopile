"""Dynamic model routing: classifier parsing/fail-open, config gating, and the
per-call ``model`` override through the runner and both providers.

All offline — the Anthropic seam is faked at ``anthropic.AsyncAnthropic`` (router)
or ``provider._client`` (provider), matching the other provider tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atopile.dataclasses import AppContext
from atopile.server.agent._ee import model_router
from atopile.server.agent._ee.provider_anthropic import AnthropicProvider
from atopile.server.agent.config import AgentConfig
from atopile.server.agent.runner import AgentRunner, _StubProvider, _StubRegistry

# ── tier map ──────────────────────────────────────────────────────────


def test_tier_to_model_maps_and_falls_back():
    cfg = AgentConfig(model="std", model_simple="small", model_complex="big")
    assert model_router.tier_to_model("simple", cfg) == "small"
    assert model_router.tier_to_model("standard", cfg) == "std"
    assert model_router.tier_to_model("complex", cfg) == "big"
    # unset tiers fall back to the standard model
    bare = AgentConfig(model="std")
    assert model_router.tier_to_model("simple", bare) == "std"
    assert model_router.tier_to_model("complex", bare) == "std"


# ── reply parsing ─────────────────────────────────────────────────────


def _reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("simple", "simple"),
        ("Complex.", "complex"),
        ("  STANDARD\n", "standard"),
        ("simple — just a lookup", "simple"),
        ("medium", "standard"),  # unknown word -> fallback
        ("", "standard"),
    ],
)
def test_parse_tier(text: str, expected: str):
    assert (
        model_router._parse_tier(_reply(text), has_active_design=False) == expected
    )


def test_parse_tier_never_downshifts_active_design():
    assert (
        model_router._parse_tier(_reply("simple"), has_active_design=True)
        == "standard"
    )


# ── classify_turn (fake SDK client) ───────────────────────────────────


class _FakeMessages:
    def __init__(self, reply_text: str):
        self._reply_text = reply_text
        self.calls: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> Any:
        self.calls.append(payload)
        return _reply(self._reply_text)


def _patch_anthropic(monkeypatch, reply_text: str) -> _FakeMessages:
    messages = _FakeMessages(reply_text)

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            self.messages = messages

    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeClient)
    return messages


def test_classify_turn_happy_path(monkeypatch):
    messages = _patch_anthropic(monkeypatch, "simple")
    cfg = AgentConfig(api_key="k", router_model="router-model")
    tier = asyncio.run(
        model_router.classify_turn(
            "what is the VF of this diode?",
            has_active_design=False,
            history_len=0,
            config=cfg,
        )
    )
    assert tier == "simple"
    payload = messages.calls[0]
    assert payload["model"] == "router-model"
    assert "what is the VF" in payload["messages"][0]["content"]


def test_classify_turn_passes_design_context(monkeypatch):
    messages = _patch_anthropic(monkeypatch, "simple")
    cfg = AgentConfig(api_key="k", model_simple="small")
    tier = asyncio.run(
        model_router.classify_turn(
            "ok continue",
            has_active_design=True,
            history_len=4,
            config=cfg,
        )
    )
    # parser enforces the no-downshift rule even if the model says "simple"
    assert tier == "standard"
    assert "already in progress" in messages.calls[0]["messages"][0]["content"]


def test_classify_turn_fails_open_on_error(monkeypatch):
    class _Boom:
        def __init__(self, **_: Any) -> None:
            raise RuntimeError("no network")

    monkeypatch.setattr("anthropic.AsyncAnthropic", _Boom)
    tier = asyncio.run(
        model_router.classify_turn(
            "design a 4-layer motor controller",
            has_active_design=False,
            history_len=0,
            config=AgentConfig(api_key="k"),
        )
    )
    assert tier == "standard"


def test_classify_turn_truncates_long_messages(monkeypatch):
    messages = _patch_anthropic(monkeypatch, "complex")
    asyncio.run(
        model_router.classify_turn(
            "x" * 50_000,
            has_active_design=False,
            history_len=0,
            config=AgentConfig(api_key="k"),
        )
    )
    sent = messages.calls[0]["messages"][0]["content"]
    assert len(sent) < 3_000


# ── config gating ─────────────────────────────────────────────────────


def test_dynamic_model_env_is_anthropic_only(monkeypatch):
    monkeypatch.setenv("EE_AGENT_DYNAMIC_MODEL", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    monkeypatch.setenv("EE_AGENT_PROVIDER", "anthropic")
    cfg = AgentConfig.from_env()
    assert cfg.dynamic_model is True
    assert cfg.model_simple.startswith("claude-haiku")
    assert cfg.model_complex.startswith("claude-opus")
    assert cfg.router_model == cfg.model_simple

    monkeypatch.setenv("EE_AGENT_PROVIDER", "openai")
    cfg = AgentConfig.from_env()
    assert cfg.dynamic_model is False


def test_dynamic_model_defaults_off(monkeypatch):
    monkeypatch.delenv("EE_AGENT_DYNAMIC_MODEL", raising=False)
    monkeypatch.setenv("EE_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert AgentConfig.from_env().dynamic_model is False


# ── provider payload override ─────────────────────────────────────────


def test_openai_payload_honors_model_override(openai_config):
    from atopile.server.agent.provider import OpenAIProvider

    provider = OpenAIProvider(config=openai_config)
    payload = provider._build_payload(
        messages=[],
        instructions="sys",
        tools=[],
        skill_state={},
        project_path=".",
        model="routed-model",
    )
    assert payload["model"] == "routed-model"
    # default: config model
    payload = provider._build_payload(
        messages=[], instructions="sys", tools=[], skill_state={}, project_path="."
    )
    assert payload["model"] == openai_config.model


def test_anthropic_payload_honors_model_override(
    anthropic_config, make_anthropic_response, text_block
):
    provider = AnthropicProvider(config=anthropic_config)
    fake = _FakeMessages("")

    async def _create(**payload: Any) -> Any:
        fake.calls.append(payload)
        return make_anthropic_response(content=[text_block("ok")])

    fake.create = _create  # type: ignore[method-assign]
    provider._client = SimpleNamespace(messages=fake)  # type: ignore[assignment]

    asyncio.run(
        provider.complete(
            messages=[{"role": "user", "content": "hi"}],
            instructions="sys",
            tools=[],
            skill_state={},
            project_path=".",
            model="routed-model",
        )
    )
    assert fake.calls[0]["model"] == "routed-model"

    asyncio.run(
        provider.complete(
            messages=[{"role": "user", "content": "hi"}],
            instructions="sys",
            tools=[],
            skill_state={},
            project_path=".",
        )
    )
    assert fake.calls[1]["model"] == anthropic_config.model


# ── runner threads the routed model through ──────────────────────────


def _make_runner(provider: _StubProvider, **cfg_overrides: Any) -> AgentRunner:
    cfg_attrs: dict[str, Any] = {
        "model": "standard-model",
        "max_tool_loops": 4,
        "max_turn_seconds": 60,
        "max_checklist_continuations": 0,
        "silent_retry_max": 0,
        "trace_enabled": False,
        "trace_preview_max_chars": 200,
        "tool_output_max_chars": 10_000,
        "context_summary_max_chars": 2_000,
        "user_message_max_chars": 2_000,
        "dynamic_model": False,
        "model_simple": "simple-model",
        "model_complex": "complex-model",
        "router_model": "simple-model",
    }
    cfg_attrs.update(cfg_overrides)
    return AgentRunner(
        config=type("Cfg", (), cfg_attrs)(),
        provider=provider,
        registry=_StubRegistry(),
    )


def _run_one_turn(runner: AgentRunner, project_root: Path):
    return asyncio.run(
        runner.run_turn(
            ctx=AppContext(workspace_paths=[project_root.parent]),
            project_root=str(project_root),
            history=[],
            user_message="Build a robot controller",
            session_id="session_1",
        )
    )


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


def test_runner_routes_model_when_enabled(monkeypatch, stub_project: Path):
    async def _fake_classify(*_args: Any, **_kwargs: Any) -> str:
        return "simple"

    monkeypatch.setattr(
        "atopile.server.agent._ee.model_router.classify_turn", _fake_classify
    )
    provider = _StubProvider()
    runner = _make_runner(provider, dynamic_model=True)
    result = _run_one_turn(runner, stub_project)
    assert provider.calls, "provider was never called"
    assert all(c["model"] == "simple-model" for c in provider.calls)
    assert result.model == "simple-model"


def test_runner_uses_config_model_when_disabled(stub_project: Path):
    provider = _StubProvider()
    runner = _make_runner(provider, dynamic_model=False)
    result = _run_one_turn(runner, stub_project)
    assert provider.calls
    assert all(c["model"] == "standard-model" for c in provider.calls)
    assert result.model == "standard-model"


# ── live (integration-marked; auto-skips without a key) ──────────────


@pytest.mark.integration
def test_live_router_classifies_both_ends():
    import os

    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    cfg = AgentConfig(
        provider="anthropic",
        base_url="",
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        model="claude-sonnet-4-6",
        model_simple="claude-haiku-4-5-20251001",
        model_complex="claude-opus-4-8",
        router_model="claude-haiku-4-5-20251001",
    )
    easy = asyncio.run(
        model_router.classify_turn(
            "What does the temp_sensor module in this project do?",
            has_active_design=False,
            history_len=0,
            config=cfg,
        )
    )
    hard = asyncio.run(
        model_router.classify_turn(
            "Design a new 4-layer BLDC motor controller board: STM32G4, "
            "3-phase gate driver, current sensing on all phases, CAN-FD, "
            "and a 48 V to 3.3 V power tree. Plan the module structure too.",
            has_active_design=False,
            history_len=0,
            config=cfg,
        )
    )
    assert easy == "simple"
    assert hard == "complex"
