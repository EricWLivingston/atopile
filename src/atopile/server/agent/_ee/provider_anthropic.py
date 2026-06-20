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
import copy
import json
import logging
import threading
import uuid
from collections import OrderedDict
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

# How many recent conversation transcripts to retain in the in-memory store.
# Each minted response_id maps to one full transcript; older ones are evicted.
_MAX_TRANSCRIPTS = 64


class AnthropicProvider:
    """Implements the LLMProvider protocol against the Anthropic Messages API."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._client: AsyncAnthropic | None = None
        # Anthropic's Messages API is stateless: there is no ``previous_response_id``.
        # The runner, however, is built for OpenAI's stateful Responses API and only
        # hands us the per-turn *delta* each call. We therefore keep the full
        # conversation ourselves, keyed by the response_id we mint and return, so we
        # can rebuild the complete transcript Anthropic requires on every request.
        self._transcripts: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        # The provider is a process-wide singleton shared by all sessions; guard the LRU
        # so concurrent turns can't race on move_to_end/popitem (CODE_AUDIT Q7).
        self._transcripts_lock = threading.Lock()

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
        previous_response_id: str | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        # 1) Translate this turn's delta (OpenAI-format) → Anthropic-format.
        delta = _convert_messages_openai_to_anthropic(messages)

        # 2) Rebuild the full conversation. Anthropic keeps no server-side state,
        #    so we reconstruct it from the transcript stored under the id we minted
        #    on the previous call, then append this turn's delta.
        conversation = self._rebuild_conversation(previous_response_id, delta)

        anthropic_tools = [_convert_tool_def(t) for t in tools]
        # Cache the whole tools block: Anthropic caches everything up to the last
        # ``cache_control`` breakpoint, so marking only the final tool def caches all of
        # them. Tools + system are stable across a session, so this is the biggest
        # per-turn token win (CODE_AUDIT T1). Breakpoint order: tools -> system.
        if anthropic_tools:
            anthropic_tools[-1] = {
                **anthropic_tools[-1],
                "cache_control": {"type": "ephemeral"},
            }
        system_blocks = _build_system_with_caching(instructions, skill_state)

        # Per-call model override (dynamic complexity routing). Safe mid-chain:
        # the transcript store doesn't condition on model, and every request
        # rebuilds the full conversation anyway.
        payload: dict[str, Any] = {
            "model": model or self._config.model,
            "max_tokens": _ANTHROPIC_MAX_OUTPUT_TOKENS,
            "system": system_blocks,
            "messages": conversation,
            "tools": anthropic_tools,
        }

        # 3) Call the API with retries + context-overflow handling.
        response = await self._request_with_retries(payload)
        _log_cache_metrics(response, payload["model"])

        # 4) Persist the assistant turn so the *next* delta's tool_result pairs with
        #    a real preceding tool_use, and mint the id the runner hands back to us
        #    as previous_response_id.
        conversation.append(_assistant_message_from_response(response))
        new_id = getattr(response, "id", None) or uuid.uuid4().hex
        self._store_transcript(new_id, conversation)

        # 5) Normalize Anthropic response → OpenAI-shape dict, then build the
        #    LLMResponse the runner expects (reusing atopile's own extractors).
        openai_shape = _normalize_to_openai_shape(response)
        # Ensure LLMResponse.id matches our transcript key (covers the uuid
        # fallback when Anthropic omits an id).
        openai_shape["id"] = new_id
        return _build_llm_response(openai_shape)

    # ── Conversation-state management (emulates previous_response_id) ──

    def _rebuild_conversation(
        self,
        previous_response_id: str | None,
        delta: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return the full Anthropic message list for this request.

        Starts from the transcript stored under ``previous_response_id`` (a deep
        copy so retries/shrinking never mutate the stored history), appends this
        turn's ``delta``, and guarantees the result is a non-empty list whose final
        message is a ``user`` turn (the API rejects anything else).
        """
        if previous_response_id is not None:
            # Grab the stored list + LRU-touch under the lock, then deep-copy outside:
            # stored transcripts are never mutated in place (each id maps to a fresh
            # list), so the reference stays valid even if evicted after we release.
            with self._transcripts_lock:
                base = self._transcripts.get(previous_response_id)
                if base is not None:
                    self._transcripts.move_to_end(previous_response_id)  # LRU touch
            if base is None:
                # We minted ids but lost this one (process restart or LRU
                # eviction). Raise a message containing "previous_response_id" so
                # the route layer's chain-recovery (utils.is_chain_integrity_error)
                # retries this turn from full local history with no prior id.
                raise RuntimeError(
                    "Anthropic conversation state for previous_response_id "
                    f"{previous_response_id!r} was not found; cannot continue "
                    "without re-sending full history."
                )
            conversation = copy.deepcopy(base)
        else:
            conversation = []

        conversation.extend(delta)

        # Empty deltas (commentary / silent-retry / closing calls) or a transcript
        # that ends on an assistant turn need a user turn to elicit a response.
        if not conversation or conversation[-1].get("role") != "user":
            conversation.append(
                {"role": "user", "content": [{"type": "text", "text": "Continue."}]}
            )

        _repair_orphaned_tool_uses(conversation)
        return conversation

    def _store_transcript(
        self, response_id: str, conversation: list[dict[str, Any]]
    ) -> None:
        with self._transcripts_lock:
            self._transcripts[response_id] = conversation
            self._transcripts.move_to_end(response_id)
            while len(self._transcripts) > _MAX_TRANSCRIPTS:
                self._transcripts.popitem(last=False)

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
            f"Conversation:\n{_messages_to_text(old, max_chars=50_000)}"
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


def _assistant_message_from_response(response: Any) -> dict[str, Any]:
    """Rebuild a storable Anthropic ``assistant`` message from a response.

    Captures ``text`` and ``tool_use`` blocks as plain dicts (so the stored
    transcript serializes cleanly on the next request) and drops empty text and
    internal ``thinking`` blocks. The ``tool_use`` blocks are what let the next
    turn's ``tool_result`` delta pair correctly — fixing the orphaned-tool_result
    400 that previously froze the agent right after the checklist.
    """
    content_blocks: list[dict[str, Any]] = []
    for block in getattr(response, "content", []):
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", "")
            if text:
                content_blocks.append({"type": "text", "text": text})
        elif block_type == "tool_use":
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    if not content_blocks:
        # Anthropic always returns at least one block; guard so we never store an
        # assistant message with empty content (the API rejects that on resend).
        content_blocks.append({"type": "text", "text": " "})
    return {"role": "assistant", "content": content_blocks}


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
            # Anthropic requires every ``tool_result`` block to lead the user turn
            # (contiguous, before any text). Upstream nudges (e.g. the post-
            # ``parts_install`` user message in orchestrator_helpers) can interleave
            # text *between* the tool_results of parallel tool calls, producing
            # ``[tool_result, text, tool_result, …]`` — which the API rejects with a
            # 400 ("tool_use ids found without tool_result blocks immediately after").
            # Stable-partition tool_results to the front so the order is always valid.
            tool_results = [
                b for b in pending_user_blocks if b.get("type") == "tool_result"
            ]
            others = [
                b for b in pending_user_blocks if b.get("type") != "tool_result"
            ]
            out.append({"role": "user", "content": tool_results + others})
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


def _repair_orphaned_tool_uses(conversation: list[dict[str, Any]]) -> None:
    """Guarantee every ``tool_use`` has a matching ``tool_result`` in the next turn.

    Anthropic rejects the whole request (400) if any ``tool_use`` id in an assistant
    turn lacks a ``tool_result`` in the immediately following user turn. A dropped or
    mis-routed result (parallel tool calls, partial deltas, eviction) would otherwise
    kill the entire run. This walks each assistant turn and injects a synthetic
    ``tool_result`` for any orphan into the following user turn (creating that turn if
    needed), so a stray orphan degrades to one missing result instead of a dead run.

    Mutates ``conversation`` in place.
    """
    i = 0
    while i < len(conversation):
        msg = conversation[i]
        if msg.get("role") != "assistant":
            i += 1
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            i += 1
            continue
        tool_use_ids = [
            b["id"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        ]
        if not tool_use_ids:
            i += 1
            continue

        # Ensure a following user turn exists to hold the tool_results.
        nxt = conversation[i + 1] if i + 1 < len(conversation) else None
        if nxt is None or nxt.get("role") != "user":
            nxt = {"role": "user", "content": []}
            conversation.insert(i + 1, nxt)
        if not isinstance(nxt.get("content"), list):
            nxt["content"] = (
                [{"type": "text", "text": nxt["content"]}]
                if isinstance(nxt.get("content"), str) and nxt["content"]
                else []
            )

        present = {
            b["tool_use_id"]
            for b in nxt["content"]
            if isinstance(b, dict)
            and b.get("type") == "tool_result"
            and b.get("tool_use_id")
        }
        missing = [tid for tid in tool_use_ids if tid not in present]
        if missing:
            log.warning(
                "Repairing %d orphaned tool_use id(s) with synthetic tool_result "
                "to avoid an Anthropic 400: %s",
                len(missing),
                ", ".join(missing),
            )
            # tool_results must lead the user turn (see _flush_user); prepend them.
            synthetic = [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": "[no result captured]",
                }
                for tid in missing
            ]
            nxt["content"] = synthetic + nxt["content"]
        i += 2


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


def _messages_to_text(messages: list[dict[str, Any]], max_chars: int) -> str:
    """Render Anthropic messages as readable ``role: text`` lines for the summarizer,
    walking the *tail* so the most recent (most relevant) turns survive the budget.

    Cheaper and more faithful than ``json.dumps(messages)[:max_chars]`` (CODE_AUDIT T6):
    no JSON envelope/escaping bloat in the budget, and truncation drops the *oldest*
    lines rather than slicing mid-token at an arbitrary char offset.
    """
    lines: list[str] = []
    used = 0
    for msg in reversed(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        else:
            parts: list[str] = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    parts.append(
                        f"[tool_use {block.get('name', '')} "
                        f"{json.dumps(block.get('input', {}), ensure_ascii=False)}]"
                    )
                elif btype == "tool_result":
                    inner = block.get("content", "")
                    parts.append(f"[tool_result {inner}]")
            text = " ".join(p for p in parts if p)
        line = f"{role}: {text}".strip()
        if not line:
            continue
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    lines.reverse()
    return "\n".join(lines)


def _log_cache_metrics(response: Any, model: str) -> None:
    """Log prompt-cache effectiveness so caching can be verified (CODE_AUDIT T2).

    The cached prefix (tools + system) only pays off if it is byte-identical AND
    served by the same model turn-to-turn — Anthropic keys the cache per model, so
    dynamic routing that alternates Haiku/Sonnet/Opus shows ``creation`` (a miss +
    re-bill) rather than ``read`` on the swapped turn. A steady ``read >> creation``
    confirms the cache holds.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    log.debug(
        "anthropic cache [model=%s]: read=%d creation=%d input=%d",
        model,
        read,
        creation,
        getattr(usage, "input_tokens", 0) or 0,
    )


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
