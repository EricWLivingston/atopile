"""Offline tests for AnthropicProvider conversation-state management.

Anthropic's Messages API is stateless, but the runner is built for OpenAI's
stateful Responses API and only hands the provider the per-turn *delta* plus a
``previous_response_id``. These tests pin the behavior that fixes the "freezes
right after the checklist" bug: the provider must rebuild the full transcript on
every call so a follow-up ``tool_result`` pairs with the assistant ``tool_use``
that produced it (instead of being sent orphaned and 400-ing).

The Anthropic client is stubbed — no network or API key required.
"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from atopile.server.agent._ee.provider_anthropic import AnthropicProvider


# ── Fake async Anthropic client ───────────────────────────────────────


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []  # captured payloads, deep-copied

    async def create(self, **payload: Any) -> Any:
        self.calls.append(copy.deepcopy(payload))
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _FakeMessages(responses)


def _provider_with(anthropic_config, responses: list[Any]) -> AnthropicProvider:
    provider = AnthropicProvider(config=anthropic_config)
    # Inject the stub directly; _get_client() only builds a real client when
    # self._client is None, so this bypasses the API-key requirement.
    provider._client = _FakeClient(responses)  # type: ignore[assignment]
    return provider


def _complete(provider: AnthropicProvider, **kwargs: Any):
    base: dict[str, Any] = {
        "instructions": "sys",
        "tools": [],
        "skill_state": {},
        "project_path": ".",
    }
    base.update(kwargs)
    return asyncio.run(provider.complete(**base))


# ── the core fix: follow-up tool_result pairs with the prior tool_use ──


def test_followup_tool_result_pairs_with_stored_tool_use(
    anthropic_config, make_anthropic_response, tool_use_block, text_block
):
    provider = _provider_with(
        anthropic_config,
        responses=[
            # Turn 1: model creates a checklist (tool_use).
            make_anthropic_response(
                content=[tool_use_block("call_1", "checklist_create", {"items": []})],
                stop_reason="tool_use",
                id="msg_1",
            ),
            # Turn 2: model replies with text after seeing the tool result.
            make_anthropic_response(
                content=[text_block("done")], stop_reason="end_turn", id="msg_2"
            ),
        ],
    )

    # Turn 1 — first call of the session.
    llm1 = _complete(
        provider,
        messages=[{"role": "user", "content": "build me X"}],
        previous_response_id=None,
    )
    assert llm1.id == "msg_1"
    assert [c.id for c in llm1.tool_calls] == ["call_1"]

    sent1 = provider._client.messages.calls[0]["messages"]  # type: ignore[attr-defined]
    # First request carries only the user turn — nothing orphaned.
    assert sent1 == [{"role": "user", "content": [{"type": "text", "text": "build me X"}]}]

    # Turn 2 — runner hands back ONLY the tool output delta + previous id.
    llm2 = _complete(
        provider,
        messages=[
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "checklist created: 3 items",
            }
        ],
        previous_response_id="msg_1",
    )
    assert llm2.id == "msg_2"

    sent2 = provider._client.messages.calls[1]["messages"]  # type: ignore[attr-defined]
    # Full transcript was rebuilt: original user, the assistant tool_use, then the
    # paired tool_result as the final user turn.
    assert sent2[0] == {"role": "user", "content": [{"type": "text", "text": "build me X"}]}
    assert sent2[-2]["role"] == "assistant"
    assert any(
        blk.get("type") == "tool_use" and blk.get("id") == "call_1"
        for blk in sent2[-2]["content"]
    )
    assert sent2[-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "checklist created: 3 items",
            }
        ],
    }


# ── unknown previous_response_id → chain-integrity signal ──────────────


def test_unknown_previous_response_id_raises_chain_integrity_error(
    anthropic_config, make_anthropic_response, text_block
):
    provider = _provider_with(
        anthropic_config,
        responses=[make_anthropic_response(content=[text_block("hi")], id="msg_1")],
    )

    with pytest.raises(RuntimeError) as excinfo:
        _complete(
            provider,
            messages=[{"role": "user", "content": "continue"}],
            previous_response_id="msg_does_not_exist",
        )

    # Message must contain "previous_response_id" so utils.is_chain_integrity_error
    # triggers run_turn_with_chain_recovery (retry from full local history).
    assert "previous_response_id" in str(excinfo.value)


# ── empty delta gets a user nudge so the request is valid ──────────────


def test_empty_delta_appends_continue_user_turn(
    anthropic_config, make_anthropic_response, text_block
):
    provider = _provider_with(
        anthropic_config,
        responses=[
            make_anthropic_response(
                content=[text_block("step one")], stop_reason="end_turn", id="msg_1"
            ),
            make_anthropic_response(
                content=[text_block("step two")], stop_reason="end_turn", id="msg_2"
            ),
        ],
    )

    _complete(
        provider,
        messages=[{"role": "user", "content": "go"}],
        previous_response_id=None,
    )

    # Commentary/silent-retry path: runner re-prompts with an empty delta.
    _complete(provider, messages=[], previous_response_id="msg_1")

    sent2 = provider._client.messages.calls[1]["messages"]  # type: ignore[attr-defined]
    # The stored transcript ended on an assistant turn; a user "Continue." was
    # appended so Anthropic has a non-empty, user-terminated message list.
    assert sent2[-1] == {"role": "user", "content": [{"type": "text", "text": "Continue."}]}
    assert sent2[-2]["role"] == "assistant"


# ── stored transcript is not mutated by later turns (deepcopy) ─────────


def test_stored_transcript_not_mutated_across_turns(
    anthropic_config, make_anthropic_response, tool_use_block, text_block
):
    provider = _provider_with(
        anthropic_config,
        responses=[
            make_anthropic_response(
                content=[tool_use_block("call_1", "t", {})],
                stop_reason="tool_use",
                id="msg_1",
            ),
            make_anthropic_response(
                content=[text_block("ok")], stop_reason="end_turn", id="msg_2"
            ),
        ],
    )

    _complete(
        provider,
        messages=[{"role": "user", "content": "x"}],
        previous_response_id=None,
    )
    len_after_turn1 = len(provider._transcripts["msg_1"])  # type: ignore[attr-defined]

    _complete(
        provider,
        messages=[{"type": "function_call_output", "call_id": "call_1", "output": "r"}],
        previous_response_id="msg_1",
    )

    # The msg_1 transcript is unchanged; the appended turn lives under msg_2.
    assert len(provider._transcripts["msg_1"]) == len_after_turn1  # type: ignore[attr-defined]
    assert "msg_2" in provider._transcripts  # type: ignore[attr-defined]
    assert len(provider._transcripts["msg_2"]) > len_after_turn1  # type: ignore[attr-defined]


# silence "imported but unused" if json trimming changes later
_ = json
