"""Offline unit tests for the AnthropicProvider translation helpers (M2).

These exercise the pure functions that translate between atopile's
OpenAI-Responses message shape and Anthropic's Messages API shape. No network
or API key is required.
"""

from __future__ import annotations

import asyncio
import json

from atopile.server.agent._ee.provider_anthropic import (
    AnthropicProvider,
    _build_llm_response,
    _convert_messages_openai_to_anthropic,
    _convert_tool_def,
    _normalize_to_openai_shape,
    _shrink_tool_outputs_in_payload,
)


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
