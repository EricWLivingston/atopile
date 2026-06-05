"""Provider parity tests (M2).

The same OpenAI-format prompt + tool definitions are pushed through both
``OpenAIProvider`` and ``AnthropicProvider`` with their network layers stubbed,
and the resulting normalized ``LLMResponse`` objects are asserted equivalent.
This catches drift between the two providers as the runner evolves.

Both providers expose an async ``_request_with_retries(payload)`` seam:
- OpenAIProvider returns an OpenAI-Responses-shaped ``dict`` body.
- AnthropicProvider returns an Anthropic ``Message``-like object.

We replace that seam so no real HTTP call happens. The canned OpenAI body and
the canned Anthropic message are constructed to be semantically identical, so a
correct pair of providers must normalize them to matching shapes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from atopile.server.agent._ee.provider_anthropic import AnthropicProvider
from atopile.server.agent.provider import OpenAIProvider

# Shared logical content: one line of text + one tool call.
_TEXT = "Use the voltage divider."
_TOOL_NAME = "rag_search"
_TOOL_ARGS = {"query": "resistor divider"}
_CALL_ID = "call_42"

_MESSAGES = [{"role": "user", "content": "design a 12V to 3.3V divider"}]
_TOOLS = [
    {
        "type": "function",
        "name": _TOOL_NAME,
        "description": "search the corpus",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


def _canned_openai_body() -> dict:
    """An OpenAI-Responses body equivalent to the canned Anthropic message."""
    return {
        "id": "resp_1",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": _TEXT}],
                "phase": "final_answer",
            },
            {
                "type": "function_call",
                "call_id": _CALL_ID,
                "name": _TOOL_NAME,
                "arguments": json.dumps(_TOOL_ARGS),
            },
        ],
        "output_text": _TEXT,
        "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42},
    }


def _run_openai(monkeypatch, config_factory):
    provider = OpenAIProvider(config=config_factory)

    async def fake_request(payload):
        return _canned_openai_body()

    monkeypatch.setattr(provider, "_request_with_retries", fake_request)
    return asyncio.run(
        provider.complete(
            messages=_MESSAGES,
            instructions="system prompt",
            tools=_TOOLS,
            skill_state={},
            project_path=Path("."),
        )
    )


def _run_anthropic(monkeypatch, config_factory, make_anthropic_response,
                   text_block, tool_use_block):
    provider = AnthropicProvider(config=config_factory)
    canned = make_anthropic_response(
        content=[
            text_block(_TEXT),
            tool_use_block(_CALL_ID, _TOOL_NAME, _TOOL_ARGS),
        ],
        stop_reason="end_turn",
        input_tokens=30,
        output_tokens=12,
    )

    async def fake_request(payload):
        return canned

    monkeypatch.setattr(provider, "_request_with_retries", fake_request)
    return asyncio.run(
        provider.complete(
            messages=_MESSAGES,
            instructions="system prompt",
            tools=_TOOLS,
            skill_state={},
            project_path=Path("."),
        )
    )


def test_providers_agree_on_tool_calls_and_text(
    monkeypatch,
    openai_config,
    anthropic_config,
    make_anthropic_response,
    text_block,
    tool_use_block,
):
    openai_resp = _run_openai(monkeypatch, openai_config)
    anthropic_resp = _run_anthropic(
        monkeypatch, anthropic_config, make_anthropic_response, text_block,
        tool_use_block,
    )

    # Text outputs are non-empty and equal.
    assert openai_resp.text
    assert anthropic_resp.text
    assert openai_resp.text == anthropic_resp.text

    # Tool calls match on count, name, and parsed arguments.
    assert len(openai_resp.tool_calls) == len(anthropic_resp.tool_calls) == 1
    assert openai_resp.tool_calls[0].name == anthropic_resp.tool_calls[0].name
    assert (
        openai_resp.tool_calls[0].arguments
        == anthropic_resp.tool_calls[0].arguments
        == _TOOL_ARGS
    )

    # Phase mapping matches.
    assert openai_resp.phase == anthropic_resp.phase == "final_answer"

    # Token usage is populated on both.
    assert openai_resp.usage is not None and anthropic_resp.usage is not None
    assert openai_resp.usage.input_tokens == anthropic_resp.usage.input_tokens == 30
    assert openai_resp.usage.output_tokens == anthropic_resp.usage.output_tokens == 12
