"""Offline unit tests for the AnthropicProvider translation helpers (M2).

These exercise the pure functions that translate between atopile's
OpenAI-Responses message shape and Anthropic's Messages API shape. No network
or API key is required.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from atopile.server.agent._ee.provider_anthropic import (
    AnthropicProvider,
    _build_llm_response,
    _convert_messages_openai_to_anthropic,
    _convert_tool_def,
    _messages_to_text,
    _normalize_to_openai_shape,
    _repair_orphaned_tool_uses,
    _shrink_tool_outputs_in_payload,
)
from atopile.server.agent.config import AgentConfig

# ── tool definition translation ───────────────────────────────────────


def test_convert_tool_def_basic():
    openai_def = {
        "type": "function",
        "name": "rag_search",
        "description": "search the corpus",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    out = _convert_tool_def(openai_def)
    assert out == {
        "name": "rag_search",
        "description": "search the corpus",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }


def test_convert_tool_def_defaults_missing_fields():
    out = _convert_tool_def({"name": "ping"})
    assert out["name"] == "ping"
    assert out["description"] == ""
    assert out["input_schema"] == {"type": "object", "properties": {}}


# ── message translation ───────────────────────────────────────────────


def test_convert_messages_text_only():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    out = _convert_messages_openai_to_anthropic(messages)
    assert out == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
    ]


def test_convert_messages_nested_input_text_blocks():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "part a"},
                {"type": "text", "text": "part b"},
            ],
        }
    ]
    out = _convert_messages_openai_to_anthropic(messages)
    assert out == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "part a"},
                {"type": "text", "text": "part b"},
            ],
        }
    ]


def test_convert_messages_with_tool_calls():
    """function_call → assistant tool_use; function_call_output → user tool_result."""
    messages = [
        {"role": "user", "content": "find an LDO"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "rag_search",
            "arguments": json.dumps({"query": "ldo"}),
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "TLV713 datasheet ...",
        },
        {"role": "assistant", "content": "Use the TLV713."},
    ]
    out = _convert_messages_openai_to_anthropic(messages)
    assert out == [
        {"role": "user", "content": [{"type": "text", "text": "find an LDO"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "rag_search",
                    "input": {"query": "ldo"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "TLV713 datasheet ...",
                }
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Use the TLV713."}],
        },
    ]


def test_convert_messages_parallel_tool_results_lead_user_turn():
    """Interleaved nudges between parallel tool results must not push a tool_result
    behind a text block — Anthropic requires tool_results to lead the user turn.

    Mirrors the parallel-``parts_install`` delta that produced a run-killing 400:
    ``[fco1, user-nudge, fco2, user-nudge]`` was packed as
    ``[tool_result1, text, tool_result2, text]``.
    """
    messages = [
        {"type": "function_call_output", "call_id": "call_1", "output": "ok1"},
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "parts_install completed. 1"}],
        },
        {"type": "function_call_output", "call_id": "call_2", "output": "ok2"},
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "parts_install completed. 2"}],
        },
    ]
    out = _convert_messages_openai_to_anthropic(messages)
    # Single user turn, with both tool_results before any text block.
    assert len(out) == 1
    blocks = out[0]["content"]
    types = [b["type"] for b in blocks]
    assert types == ["tool_result", "tool_result", "text", "text"]
    assert [b["tool_use_id"] for b in blocks[:2]] == ["call_1", "call_2"]


def test_repair_orphaned_tool_uses_injects_synthetic_result():
    """A tool_use with no matching tool_result in the next turn gets a synthetic one."""
    conversation = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "parts", "input": {}},
                {"type": "tool_use", "id": "tu_2", "name": "parts", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
            ],
        },
    ]
    _repair_orphaned_tool_uses(conversation)
    results = {
        b["tool_use_id"]: b["content"]
        for b in conversation[1]["content"]
        if b["type"] == "tool_result"
    }
    assert set(results) == {"tu_1", "tu_2"}
    assert results["tu_2"] == "[no result captured]"
    # Synthetic result leads the user turn.
    assert conversation[1]["content"][0]["type"] == "tool_result"


def test_repair_orphaned_tool_uses_creates_missing_user_turn():
    """When the assistant tool_use turn is last, a user turn is created to hold it."""
    conversation = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "rag_search", "input": {}},
            ],
        },
    ]
    _repair_orphaned_tool_uses(conversation)
    assert len(conversation) == 2
    assert conversation[1]["role"] == "user"
    assert conversation[1]["content"][0]["tool_use_id"] == "tu_1"


def test_repair_orphaned_tool_uses_noop_when_paired():
    """Fully-paired conversations are left unchanged."""
    conversation = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "x", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
            ],
        },
    ]
    before = [dict(m) for m in conversation]
    _repair_orphaned_tool_uses(conversation)
    assert conversation == before


def test_convert_messages_malformed_arguments_default_to_empty():
    messages = [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "t",
            "arguments": "not json",
        }
    ]
    out = _convert_messages_openai_to_anthropic(messages)
    assert out[0]["content"][0]["input"] == {}


# ── response normalization ────────────────────────────────────────────


def test_normalize_to_openai_shape_text(make_anthropic_response, text_block):
    resp = make_anthropic_response(
        content=[text_block("the answer")],
        stop_reason="end_turn",
        input_tokens=11,
        output_tokens=4,
    )
    shape = _normalize_to_openai_shape(resp)
    assert shape["id"] == "msg_1"
    assert shape["output_text"] == "the answer"
    msg = shape["output"][0]
    assert msg["type"] == "message"
    assert msg["phase"] == "final_answer"
    assert msg["content"] == [{"type": "output_text", "text": "the answer"}]
    assert shape["usage"] == {
        "input_tokens": 11,
        "output_tokens": 4,
        "total_tokens": 15,
        "cached_input_tokens": 0,
    }


def test_normalize_phase_none_when_not_end_turn(make_anthropic_response, text_block):
    resp = make_anthropic_response(
        content=[text_block("partial")], stop_reason="tool_use"
    )
    shape = _normalize_to_openai_shape(resp)
    assert shape["output"][0]["phase"] is None


def test_normalize_to_openai_shape_tool_calls(
    make_anthropic_response, text_block, tool_use_block
):
    resp = make_anthropic_response(
        content=[
            text_block("calling tool"),
            tool_use_block("call_9", "ipc_check", {"net": "VBAT"}),
        ],
        stop_reason="tool_use",
    )
    shape = _normalize_to_openai_shape(resp)
    calls = [i for i in shape["output"] if i["type"] == "function_call"]
    assert len(calls) == 1
    assert calls[0]["name"] == "ipc_check"
    assert calls[0]["call_id"] == "call_9"
    assert json.loads(calls[0]["arguments"]) == {"net": "VBAT"}


def test_normalize_reports_cached_tokens(make_anthropic_response, text_block):
    resp = make_anthropic_response(
        content=[text_block("x")], cache_read_input_tokens=42
    )
    shape = _normalize_to_openai_shape(resp)
    assert shape["usage"]["cached_input_tokens"] == 42


# ── building the LLMResponse ──────────────────────────────────────────


def test_build_llm_response_text_and_tool_calls(
    make_anthropic_response, text_block, tool_use_block
):
    resp = make_anthropic_response(
        content=[
            text_block("done"),
            tool_use_block("c1", "pyspice_run", {"analysis": "tran"}),
        ],
        stop_reason="end_turn",
        input_tokens=20,
        output_tokens=8,
    )
    shape = _normalize_to_openai_shape(resp)
    llm = _build_llm_response(shape)

    assert llm.id == "msg_1"
    assert llm.text == "done"
    assert llm.phase == "final_answer"
    assert len(llm.tool_calls) == 1
    call = llm.tool_calls[0]
    assert call.name == "pyspice_run"
    assert call.id == "c1"
    assert call.arguments == {"analysis": "tran"}
    assert llm.usage is not None
    assert llm.usage.input_tokens == 20
    assert llm.usage.output_tokens == 8
    assert llm.usage.total_tokens == 28


# ── progressive tool-output shrinking ─────────────────────────────────


def test_shrink_tool_outputs_truncates_oversized_results():
    big = "x" * 1000
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": big}
                ],
            }
        ]
    }
    reduced = _shrink_tool_outputs_in_payload(payload, max_chars=100)
    assert reduced is not None
    new_content = reduced["messages"][0]["content"][0]["content"]
    assert new_content.startswith("x" * 100)
    assert "truncated, original 1000 chars" in new_content


def test_shrink_tool_outputs_noop_when_within_budget():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "small"}
                ],
            }
        ]
    }
    assert _shrink_tool_outputs_in_payload(payload, max_chars=100) is None


# ── client-side compaction guard ──────────────────────────────────────


def test_compaction_skips_short_histories(anthropic_config):
    provider = AnthropicProvider(config=anthropic_config)
    short_history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    result = asyncio.run(provider._compact_history(short_history, system="sys"))
    assert result is None


# ── prompt caching (T1) ───────────────────────────────────────────────


def _capturing_provider(make_anthropic_response, text_block):
    """An AnthropicProvider whose client records the request payload."""
    provider = AnthropicProvider(
        config=AgentConfig(
            provider="anthropic", base_url="", model="claude-sonnet-4-6",
            api_key="k",
        )
    )
    calls: list[dict[str, Any]] = []

    async def _create(**payload: Any) -> Any:
        calls.append(payload)
        return make_anthropic_response(content=[text_block("ok")])

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    return provider, calls


def test_last_tool_def_gets_cache_control(make_anthropic_response, text_block):
    provider, calls = _capturing_provider(make_anthropic_response, text_block)
    tools = [
        {"name": "a", "description": "x", "parameters": {"type": "object"}},
        {"name": "b", "description": "y", "parameters": {"type": "object"}},
    ]
    asyncio.run(
        provider.complete(
            messages=[{"role": "user", "content": "hi"}],
            instructions="sys",
            tools=tools,
            skill_state={},
            project_path=".",
        )
    )
    sent_tools = calls[0]["tools"]
    # Only the final tool carries the cache breakpoint (Anthropic caches up to it).
    assert sent_tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent_tools[0]


def test_no_tools_no_cache_control(make_anthropic_response, text_block):
    provider, calls = _capturing_provider(make_anthropic_response, text_block)
    asyncio.run(
        provider.complete(
            messages=[{"role": "user", "content": "hi"}],
            instructions="sys",
            tools=[],
            skill_state={},
            project_path=".",
        )
    )
    assert calls[0]["tools"] == []  # nothing to mark, no crash


# ── compaction summarizer input from message tail (T6) ────────────────


def test_messages_to_text_renders_roles_and_blocks():
    msgs = [
        {"role": "user", "content": "design a divider"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "calling tool"},
                {
                    "type": "tool_use",
                    "name": "pyspice_run",
                    "input": {"analysis": "op"},
                },
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "content": "Vout=3.3"}],
        },
    ]
    out = _messages_to_text(msgs, max_chars=10_000)
    assert "user: design a divider" in out
    assert "tool_use pyspice_run" in out
    assert "tool_result Vout=3.3" in out


def test_messages_to_text_keeps_recent_tail_within_budget():
    msgs = [{"role": "user", "content": f"msg {i} " + "x" * 100} for i in range(50)]
    out = _messages_to_text(msgs, max_chars=300)
    assert len(out) <= 300 + 120  # bounded (one line of slack)
    # the most recent message survives; the oldest is dropped
    assert "msg 49" in out
    assert "msg 0 " not in out
