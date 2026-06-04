"""Anthropic provider for atopile's agent harness.

Implements the same ``LLMProvider`` protocol as the upstream ``OpenAIProvider``
(``atopile.server.agent.provider``) against Anthropic's Messages API. The
runner is provider-agnostic: it hands us OpenAI-Responses-shaped messages +
tool definitions and expects a normalized ``LLMResponse`` back, so this class
translates at the boundary in both directions.

This module is purely additive — nothing in the running app imports it yet.
Provider selection wiring (config flag + instantiation) lands separately.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
)

from atopile.server.agent.config import AgentConfig
from atopile.server.agent.provider import LLMResponse, TokenUsage, ToolCall

log = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

_ANTHROPIC_MAX_OUTPUT_TOKENS = 8192  # required by the API; safe for sonnet/opus
_CONTEXT_LENGTH_PATTERNS = (
    "prompt is too long",
    "exceeds the maximum",
    "context length",
)


class AnthropicProvider:
    """Implements the LLMProvider protocol against the Anthropic Messages API."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._client: AsyncAnthropic | None = None

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            if not self._config.api_key:
                raise RuntimeError(
                    "No API key configured. Set ANTHROPIC_API_KEY or "
                    "ATOPILE_AGENT_ANTHROPIC_API_KEY in your environment "
                    "or in a .env file in the project root."
                )
            self._client = AsyncAnthropic(
                api_key=self._config.api_key,
                base_url=self._config.base_url or None,
                timeout=self._config.timeout_s,
            )
        return self._client

    # ── Public API ────────────────────────────────────────────────────

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        instructions: str,
        tools: list[dict[str, Any]],
        skill_state: dict[str, Any],
        project_path: Any,
        previous_response_id: str | None = None,  # no Anthropic equivalent; ignored
    ) -> LLMResponse:
        # 1) Translate OpenAI-format inputs → Anthropic-format inputs.
        anthropic_messages = _convert_messages_openai_to_anthropic(messages)
        anthropic_tools = [_convert_tool_def(t) for t in tools]
        system_blocks = _build_system_with_caching(instructions, skill_state)

        # 2) Build the request payload.
        payload: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": _ANTHROPIC_MAX_OUTPUT_TOKENS,
            "system": system_blocks,
            "messages": anthropic_messages,
            "tools": anthropic_tools,
        }

        # 3) Call the API with retries + context-overflow handling.
        response = await self._request_with_retries(payload)

        # 4) Normalize Anthropic response → OpenAI-shape dict, then build the
        #    LLMResponse the runner expects (reusing atopile's own extractors).
        openai_shape = _normalize_to_openai_shape(response)
        return _build_llm_response(openai_shape)

    # ── Retry + overflow handling (parallels OpenAIProvider) ──────────

    async def _request_with_retries(self, payload: dict[str, Any]) -> Any:
        cfg = self._config
        client = self._get_client()
        working_payload = dict(payload)

        # Progressive tool-output shrink steps on context overflow; mirrors the
        # OpenAIProvider behavior.
        shrink_steps = (5000, 2500, 1200, 600, 300)
        shrink_index = 0
        compacted_once = False

        for attempt in range(cfg.api_retries + 1):
            try:
                return await client.messages.create(**working_payload)
            except APIStatusError as exc:
                status_code = getattr(exc, "status_code", "unknown")

                # Rate-limited → backoff and retry.
                if status_code == 429 and attempt < cfg.api_retries:
                    await asyncio.sleep(_compute_backoff(attempt, cfg))
                    continue

                # Context overflow → shrink tool outputs, then compact once.
                if _looks_like_context_overflow(exc):
                    if shrink_index < len(shrink_steps):
                        max_chars = shrink_steps[shrink_index]
                        shrink_index += 1
                        reduced = _shrink_tool_outputs_in_payload(
                            working_payload, max_chars=max_chars
                        )
                        if reduced is not None:
                            working_payload = reduced
                            continue

                    if not compacted_once:
                        compacted_once = True
                        compacted = await self._compact_history(
                            working_payload["messages"],
                            working_payload["system"],
                        )
                        if compacted is not None:
                            working_payload = dict(working_payload)
                            working_payload["messages"] = compacted
                            continue

                    raise RuntimeError(
                        "Tool outputs and history are too large for the model "
                        "context window even after shrinking and compaction."
                    ) from exc

                snippet = str(exc)[:500]
                raise RuntimeError(
                    f"Anthropic API request failed ({status_code}): {snippet}"
                ) from exc

            except (APIConnectionError, APITimeoutError) as exc:
                if attempt < cfg.api_retries:
                    await asyncio.sleep(_compute_backoff(attempt, cfg))
                    continue
                raise RuntimeError(f"Anthropic API request failed: {exc}") from exc

        raise RuntimeError("Unreachable")

    # ── Client-side context compaction ────────────────────────────────

    async def _compact_history(
        self,
        messages: list[dict[str, Any]],
        system: list[dict[str, Any]] | str,
    ) -> list[dict[str, Any]] | None:
        """Summarize the older portion of the conversation into a single
        assistant message that preserves decisions, key tool results, and
        outstanding work.

        Returns the compacted message list, or ``None`` if compaction was not
        attempted or failed.
        """
        if len(messages) < 6:
            return None  # not worth compacting; let the caller fail

        # Keep the most recent ~30% verbatim.
        cutoff = max(1, int(len(messages) * 0.3))
        old = messages[:-cutoff]
        recent = messages[-cutoff:]

        summarizer_prompt = (
            "Summarize the following agent conversation into a single concise "
            "paragraph that preserves: (1) all decisions made, (2) key tool "
            "results, (3) outstanding work items, and (4) any user constraints. "
            "Do not invent new details. Output prose only.\n\n"
            f"Conversation:\n{json.dumps(old, ensure_ascii=False)[:50000]}"
        )

        try:
            client = self._get_client()
            response = await client.messages.create(
                model=self._config.summary_model or self._config.model,
                max_tokens=2048,
                messages=[{"role": "user", "content": summarizer_prompt}],
            )
            summary_text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            if not summary_text.strip():
                return None
            summary_message = {
                "role": "assistant",
                "content": (
                    "[CONTEXT SUMMARY — earlier turns compacted to save tokens]\n"
                    + summary_text.strip()
                ),
            }
            return [summary_message] + recent
        except Exception:
            log.exception("Anthropic context compaction failed")
            return None


# ─────────────────────────────────────────────────────────────────────
#   Translation helpers
# ─────────────────────────────────────────────────────────────────────


def _convert_tool_def(openai_def: dict[str, Any]) -> dict[str, Any]:
    """OpenAI ``{type, name, description, parameters}`` → Anthropic
    ``{name, description, input_schema}``."""
    return {
        "name": openai_def["name"],
        "description": openai_def.get("description", ""),
        "input_schema": openai_def.get(
            "parameters", {"type": "object", "properties": {}}
        ),
    }


def _convert_messages_openai_to_anthropic(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate OpenAI-Responses-format messages → Anthropic Messages format.

    Mapping rules:
    - ``{"role": "user"/"assistant", "content": str | [blocks]}``
        → role message with ``text`` content blocks
    - ``{"type": "function_call", call_id, name, arguments}``
        → assistant message with a ``tool_use`` block
    - ``{"type": "function_call_output", call_id, output}``
        → user message with a ``tool_result`` block
    """
    out: list[dict[str, Any]] = []
    pending_assistant_blocks: list[dict[str, Any]] = []
    pending_user_blocks: list[dict[str, Any]] = []

    def _flush_assistant() -> None:
        if pending_assistant_blocks:
            out.append(
                {"role": "assistant", "content": list(pending_assistant_blocks)}
            )
            pending_assistant_blocks.clear()

    def _flush_user() -> None:
        if pending_user_blocks:
            out.append({"role": "user", "content": list(pending_user_blocks)})
            pending_user_blocks.clear()

    for item in messages:
        item_type = item.get("type")
        role = item.get("role")

        if item_type == "function_call":
            _flush_user()
            try:
                args = json.loads(item.get("arguments", "{}"))
            except Exception:
                args = {}
            pending_assistant_blocks.append(
                {
                    "type": "tool_use",
                    "id": item["call_id"],
                    "name": item["name"],
                    "input": args,
                }
            )
            continue

        if item_type == "function_call_output":
            _flush_assistant()
            pending_user_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": item["call_id"],
                    "content": item.get("output", ""),
                }
            )
            continue

        if role == "user":
            _flush_assistant()
            content = item.get("content", "")
            if isinstance(content, str):
                pending_user_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in ("text", "input_text"):
                        pending_user_blocks.append(
                            {"type": "text", "text": block.get("text", "")}
                        )
            continue

        if role == "assistant":
            _flush_user()
            content = item.get("content", "")
            if isinstance(content, str):
                pending_assistant_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        pending_assistant_blocks.append(
                            {"type": "text", "text": block.get("text", "")}
                        )
            continue

    _flush_assistant()
    _flush_user()
    return out


def _build_system_with_caching(
    instructions: str, skill_state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Build the system prompt as a single cache-marked content block.

    Anthropic's ``cache_control`` field tells the API to cache the block. The
    system prompt (skill bundle + instructions) is stable across turns within a
    session, so marking it ephemeral-cached is a cost optimization, not a
    correctness requirement.
    """
    return [
        {
            "type": "text",
            "text": instructions,
            "cache_control": {"type": "ephemeral"},
        }
    ]


# ─────────────────────────────────────────────────────────────────────
#   Response normalization (Anthropic → OpenAI-shape dict)
# ─────────────────────────────────────────────────────────────────────


def _normalize_to_openai_shape(response: Any) -> dict[str, Any]:
    """Translate an Anthropic ``Message`` into the OpenAI-Responses-shaped dict
    that atopile's ``orchestrator_helpers`` extractors understand:

        {"id", "output": [{type: "message", content: [...]},
                          {type: "function_call", ...}], "usage": {...}}
    """
    output_items: list[dict[str, Any]] = []
    text_pieces: list[str] = []

    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_pieces.append(block.text)
        elif block_type == "tool_use":
            output_items.append(
                {
                    "type": "function_call",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input, ensure_ascii=False),
                }
            )
        # "thinking" / other internal blocks are intentionally dropped.

    if text_pieces:
        phase = "final_answer" if response.stop_reason == "end_turn" else None
        output_items.insert(
            0,
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "\n\n".join(text_pieces)}
                ],
                "phase": phase,
            },
        )

    usage = response.usage
    return {
        "id": response.id,
        "output": output_items,
        "output_text": "\n\n".join(text_pieces),
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": (usage.input_tokens or 0) + (usage.output_tokens or 0),
            "cached_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
        },
    }


def _build_llm_response(openai_shape: dict[str, Any]) -> LLMResponse:
    """Wrap the normalized response in the runner's ``LLMResponse`` type, reusing
    atopile's own extraction helpers so behavior matches the OpenAI path."""
    from atopile.server.agent.orchestrator_helpers import (
        _extract_function_calls,
        _extract_output_phase,
        _extract_text,
    )

    text = _extract_text(openai_shape)
    phase = _extract_output_phase(openai_shape)

    tool_calls: list[ToolCall] = []
    for call in _extract_function_calls(openai_shape):
        call_id = call.get("call_id") or call.get("id")
        if not call_id:
            continue
        raw_args = str(call.get("arguments", ""))
        try:
            parsed = json.loads(raw_args) if raw_args else {}
            if not isinstance(parsed, dict):
                parsed = {}
        except Exception:
            parsed = {}
        tool_calls.append(
            ToolCall(
                id=str(call_id),
                name=str(call.get("name", "")),
                arguments_raw=raw_args,
                arguments=parsed,
            )
        )

    usage_raw = openai_shape.get("usage", {})
    usage = TokenUsage(
        input_tokens=usage_raw.get("input_tokens"),
        output_tokens=usage_raw.get("output_tokens"),
        total_tokens=usage_raw.get("total_tokens"),
        cached_input_tokens=usage_raw.get("cached_input_tokens"),
    )

    return LLMResponse(
        id=openai_shape.get("id"),
        text=text,
        tool_calls=tool_calls,
        phase=phase,
        usage=usage,
        raw=openai_shape,
    )


# ─────────────────────────────────────────────────────────────────────
#   Misc helpers
# ─────────────────────────────────────────────────────────────────────


def _looks_like_context_overflow(exc: APIStatusError) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _CONTEXT_LENGTH_PATTERNS)


def _compute_backoff(attempt: int, cfg: AgentConfig) -> float:
    delay = cfg.api_retry_base_delay_s * (2**attempt)
    return min(delay, cfg.api_retry_max_delay_s)


def _shrink_tool_outputs_in_payload(
    payload: dict[str, Any], max_chars: int
) -> dict[str, Any] | None:
    """Truncate each ``tool_result`` block's content to ``max_chars``.

    Returns a new payload, or ``None`` if nothing changed.
    """
    new_messages = []
    changed = False
    for msg in payload.get("messages", []):
        if not isinstance(msg.get("content"), list):
            new_messages.append(msg)
            continue
        new_blocks = []
        for block in msg["content"]:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
                and len(block["content"]) > max_chars
            ):
                original_len = len(block["content"])
                truncated = (
                    block["content"][:max_chars]
                    + f"\n\n... [truncated, original {original_len} chars]"
                )
                new_blocks.append({**block, "content": truncated})
                changed = True
            else:
                new_blocks.append(block)
        new_messages.append({**msg, "content": new_blocks})

    if not changed:
        return None
    return {**payload, "messages": new_messages}
