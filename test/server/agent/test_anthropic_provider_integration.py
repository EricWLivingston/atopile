"""Live integration tests for AnthropicProvider (M2).

Marked ``integration`` and auto-skipped unless ``ANTHROPIC_API_KEY`` is set
(see ``conftest.py``). These hit the real Anthropic API and are intended to run
nightly / on demand, not in fast CI.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from atopile.server.agent._ee.provider_anthropic import AnthropicProvider
from atopile.server.agent.config import AgentConfig

pytestmark = pytest.mark.integration


def _live_config(model: str = "claude-sonnet-4-6") -> AgentConfig:
    return AgentConfig(
        provider="anthropic",
        base_url="",
        model=model,
        summary_model=model,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
    )


def test_trivial_completion_returns_text():
    provider = AnthropicProvider(config=_live_config())
    resp = asyncio.run(
        provider.complete(
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            instructions="You are a terse assistant.",
            tools=[],
            skill_state={},
            project_path=".",
        )
    )
    assert resp.text.strip()
    assert resp.usage is not None
    assert resp.usage.input_tokens and resp.usage.output_tokens


def test_tool_call_round_trips():
    """The model should emit a tool_use that we normalize into a ToolCall."""
    provider = AnthropicProvider(config=_live_config())
    tools = [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get the weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    resp = asyncio.run(
        provider.complete(
            messages=[
                {"role": "user", "content": "Use get_weather for Paris."},
            ],
            instructions="Call the provided tool when asked.",
            tools=tools,
            skill_state={},
            project_path=".",
        )
    )
    # Either the model called the tool (preferred) or at least produced text.
    assert resp.tool_calls or resp.text.strip()
    if resp.tool_calls:
        assert resp.tool_calls[0].name == "get_weather"
        assert isinstance(resp.tool_calls[0].arguments, dict)


def test_tool_use_then_tool_result_round_trip():
    """Reproduce the freeze end-to-end: turn 1 emits a tool_use, turn 2 sends only
    the tool_result delta + previous_response_id (exactly what the runner does).

    Before the stateful fix this 400'd with an orphaned tool_result and the run
    died right after the checklist. It must now complete coherently.
    """
    provider = AnthropicProvider(config=_live_config())
    tools = [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get the weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    instructions = "Call get_weather, then tell the user the weather in one sentence."

    r1 = asyncio.run(
        provider.complete(
            messages=[{"role": "user", "content": "What's the weather in Paris?"}],
            instructions=instructions,
            tools=tools,
            skill_state={},
            project_path=".",
        )
    )
    if not r1.tool_calls:
        pytest.skip("model did not call the tool; cannot exercise the follow-up path")

    call = r1.tool_calls[0]
    # Hand back ONLY the tool output delta + previous_response_id, like the runner.
    r2 = asyncio.run(
        provider.complete(
            messages=[
                {
                    "type": "function_call_output",
                    "call_id": call.id,
                    "output": "Sunny, 22°C.",
                }
            ],
            instructions=instructions,
            tools=tools,
            skill_state={},
            project_path=".",
            previous_response_id=r1.id,
        )
    )
    # Reaching here means no orphaned-tool_result 400; the model continued.
    assert r2.text.strip() or r2.tool_calls


def test_compaction_handles_long_history():
    """A synthetic long history should compact without raising."""
    provider = AnthropicProvider(config=_live_config())
    messages = []
    for i in range(40):
        messages.append({"role": "user", "content": f"fact #{i}: x={i}"})
        messages.append({"role": "assistant", "content": f"noted fact #{i}"})

    converted = [
        {"role": m["role"], "content": [{"type": "text", "text": m["content"]}]}
        for m in messages
    ]
    compacted = asyncio.run(provider._compact_history(converted, system="sys"))
    # With a real key the summarizer should return a compacted list.
    assert compacted is None or (
        isinstance(compacted, list) and len(compacted) < len(converted)
    )
