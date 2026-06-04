# 16 · `AnthropicProvider` Implementation Guide

> **Purpose.** Concrete reference for implementing the `AnthropicProvider` class that mirrors atopile's existing `OpenAIProvider`. Both providers live in the fork and are switchable via `EE_AGENT_PROVIDER=openai|anthropic`. Both must pass the parity test suite on every commit.

---

## 1. The protocol we must satisfy

Atopile defines `LLMProvider` as a runtime-checkable `Protocol` in `src/atopile/server/agent/provider.py`:

```python
@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        instructions: str,                       # system prompt
        tools: list[dict[str, Any]],             # OpenAI-format tool definitions
        skill_state: dict[str, Any],
        project_path: Any,
        previous_response_id: str | None = None,
    ) -> LLMResponse: ...
```

And the response type:

```python
@dataclass
class LLMResponse:
    id: str | None
    text: str
    tool_calls: list[ToolCall]
    phase: str | None = None                     # "commentary" | "final_answer" | None
    usage: TokenUsage | None = None
    raw: dict[str, Any] = field(default_factory=dict)

@dataclass
class ToolCall:
    id: str
    name: str
    arguments_raw: str                           # JSON string
    arguments: dict[str, Any]                    # parsed
```

Our `AnthropicProvider` returns the *same* `LLMResponse` type — atopile's runner doesn't care which provider produced it.

---

## 2. Differences between OpenAI Responses API and Anthropic Messages API

This table is the working map for the implementation:

| Concern | OpenAI (upstream `OpenAIProvider`) | Anthropic (new `AnthropicProvider`) |
|---|---|---|
| SDK | `openai.AsyncOpenAI` | `anthropic.AsyncAnthropic` |
| Auth env | `OPENAI_API_KEY` / `ATOPILE_AGENT_OPENAI_API_KEY` | `ANTHROPIC_API_KEY` / `ATOPILE_AGENT_ANTHROPIC_API_KEY` |
| Endpoint | `client.responses.create(...)` | `client.messages.create(...)` |
| System prompt | `instructions=` param | `system=` param |
| Conversation continuation | `previous_response_id=` (server keeps state) | None — pass full history each turn |
| Prompt caching | `prompt_cache_key=` (server hashes key) | `cache_control={"type": "ephemeral"}` on content blocks |
| Tool definition shape | `{type: "function", name, description, parameters: {...}}` | `{name, description, input_schema: {...}}` |
| Tool calls in response | `output: [{type: "function_call", call_id, name, arguments}]` | `content: [{type: "tool_use", id, name, input}]` |
| Tool results back in | `{role: "user"}` with `{type: "function_call_output", call_id, output}` | `{role: "user"}` with `{type: "tool_result", tool_use_id, content}` |
| Reasoning tokens | OpenAI o-series field | Anthropic extended thinking (`thinking` blocks) |
| Streaming | SSE | SSE (same shape after normalization) |
| Context overflow signal | `APIStatusError` with specific string match | `BadRequestError`, similar match needed |
| Server-side context compaction | `client.responses.compact(model, previous_response_id)` | **No equivalent — implement client-side** |
| Token counting | `client.responses.input_tokens.count(...)` | `client.messages.count_tokens(...)` |

The structural pattern is the same: send messages + tools + system prompt, get back text + tool_calls. The wire format differs. Our `AnthropicProvider` translates at the boundary and returns the same `LLMResponse` as `OpenAIProvider`.

---

## 3. Implementation outline

`src/atopile/server/agent/_ee/provider_anthropic.py` (~400 LoC):

```python
"""Anthropic provider for atopile's agent harness."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    BadRequestError,
)

from atopile.server.agent.config import AgentConfig
from atopile.server.agent.provider import LLMResponse, ToolCall, TokenUsage

log = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

_ANTHROPIC_MAX_OUTPUT_TOKENS = 8192     # safe default for sonnet/opus
_CONTEXT_LENGTH_PATTERNS = (
    "prompt is too long",
    "exceeds the maximum",
    "context length",
)


class AnthropicProvider:
    """Implements LLMProvider against Anthropic Messages API."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._client: AsyncAnthropic | None = None

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            if not self._config.api_key:
                raise RuntimeError(
                    "No API key configured. Set ANTHROPIC_API_KEY or "
                    "ATOPILE_AGENT_ANTHROPIC_API_KEY in your environment "
                    "or a .env file in the project root."
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
        previous_response_id: str | None = None,   # ignored; Anthropic has no equivalent
    ) -> LLMResponse:
        # 1) Translate OpenAI-format inputs → Anthropic-format inputs
        anthropic_messages = _convert_messages_openai_to_anthropic(messages)
        anthropic_tools = [_convert_tool_def(t) for t in tools]
        system_blocks = _build_system_with_caching(instructions, skill_state)

        # 2) Build the request payload
        payload = {
            "model": self._config.model,
            "max_tokens": _ANTHROPIC_MAX_OUTPUT_TOKENS,
            "system": system_blocks,
            "messages": anthropic_messages,
            "tools": anthropic_tools,
        }

        # 3) Call API with retries + context-overflow handling
        response = await self._request_with_retries(payload)

        # 4) Normalize Anthropic response → OpenAI-shape dict for orchestrator_helpers
        #    Then build LLMResponse the runner expects.
        openai_shape = _normalize_to_openai_shape(response)
        return _build_llm_response(openai_shape, response)

    # ── Retry + overflow handling (parallels OpenAIProvider) ──────────

    async def _request_with_retries(
        self, payload: dict[str, Any],
    ) -> Any:
        cfg = self._config
        client = self._get_client()
        working_payload = dict(payload)

        # Progressive tool-output shrink steps on context overflow.
        # Mirrors OpenAIProvider's behavior.
        shrink_steps = (5000, 2500, 1200, 600, 300)
        shrink_index = 0
        compacted_once = False

        for attempt in range(cfg.api_retries + 1):
            try:
                response = await client.messages.create(**working_payload)
                return response
            except APIStatusError as exc:
                status_code = getattr(exc, "status_code", "unknown")

                # Rate-limited
                if status_code == 429 and attempt < cfg.api_retries:
                    delay_s = _compute_backoff(attempt, cfg)
                    await asyncio.sleep(delay_s)
                    continue

                # Context overflow → try shrinking tool outputs
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

                    # Shrinking exhausted → try client-side compaction once
                    if not compacted_once:
                        compacted_once = True
                        compacted = await self._compact_history(
                            working_payload["messages"], working_payload["system"]
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
                    delay_s = _compute_backoff(attempt, cfg)
                    await asyncio.sleep(delay_s)
                    continue
                raise RuntimeError(f"Anthropic API request failed: {exc}") from exc

        raise RuntimeError("Unreachable")

    # ── Client-side context compaction ────────────────────────────────

    async def _compact_history(
        self,
        messages: list[dict[str, Any]],
        system: list[dict[str, Any]] | str,
    ) -> list[dict[str, Any]] | None:
        """Summarize the first ~70% of messages into a single assistant
        message that captures decisions, tool results, and outstanding work.

        Returns the compacted message list, or None if compaction failed.
        """
        if len(messages) < 6:
            return None  # not worth compacting; just fail

        # Keep the most recent ~30% verbatim
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
                block.text for block in response.content
                if hasattr(block, "text")
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
            log.exception("Compaction failed")
            return None


# ─────────────────────────────────────────────────────────────────────
#   Translation helpers
# ─────────────────────────────────────────────────────────────────────


def _convert_tool_def(openai_def: dict[str, Any]) -> dict[str, Any]:
    """OpenAI {type, name, description, parameters} → Anthropic {name, description, input_schema}."""
    return {
        "name": openai_def["name"],
        "description": openai_def.get("description", ""),
        "input_schema": openai_def.get("parameters", {"type": "object", "properties": {}}),
    }


def _convert_messages_openai_to_anthropic(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Atopile passes messages in OpenAI Responses API format. We translate to
    Anthropic Messages API format.

    Mapping rules:
    - {"role": "user", "content": "..."}                           → {"role": "user", "content": [{"type": "text", "text": "..."}]}
    - {"role": "assistant", "content": "..."}                      → {"role": "assistant", "content": [{"type": "text", "text": "..."}]}
    - {"type": "function_call", "call_id", "name", "arguments"}    → assistant message with tool_use block
    - {"type": "function_call_output", "call_id", "output"}        → user message with tool_result block
    """
    out: list[dict[str, Any]] = []
    pending_assistant_blocks: list[dict[str, Any]] = []
    pending_user_blocks: list[dict[str, Any]] = []

    def _flush_assistant() -> None:
        if pending_assistant_blocks:
            out.append({"role": "assistant", "content": list(pending_assistant_blocks)})
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
            pending_assistant_blocks.append({
                "type": "tool_use",
                "id": item["call_id"],
                "name": item["name"],
                "input": args,
            })
            continue

        if item_type == "function_call_output":
            _flush_assistant()
            pending_user_blocks.append({
                "type": "tool_result",
                "tool_use_id": item["call_id"],
                "content": item.get("output", ""),
            })
            continue

        if role == "user":
            _flush_assistant()
            content = item.get("content", "")
            if isinstance(content, str):
                pending_user_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                # nested content blocks — translate input_text → text
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type in ("text", "input_text"):
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
    instructions: str, skill_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Construct the system prompt with prompt-caching for the skill block.

    Anthropic's cache_control field on a content block tells the API to
    cache that block. We mark the skill bundle (stable across turns) as
    ephemeral-cached. The trailing project-specific instructions are not
    cached.
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
    """
    Translate Anthropic's Message response into the OpenAI Responses API
    shape that atopile's orchestrator_helpers expects:

      {"output": [{"type": "message", "content": [...]}, {"type": "function_call", ...}]}
    """
    output_items: list[dict[str, Any]] = []
    text_pieces: list[str] = []

    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_pieces.append(block.text)
        elif block_type == "tool_use":
            output_items.append({
                "type": "function_call",
                "call_id": block.id,
                "name": block.name,
                "arguments": json.dumps(block.input, ensure_ascii=False),
            })

    if text_pieces:
        phase = "final_answer" if response.stop_reason == "end_turn" else None
        output_items.insert(0, {
            "type": "message",
            "content": [{"type": "output_text", "text": "\n\n".join(text_pieces)}],
            "phase": phase,
        })

    return {
        "id": response.id,
        "output": output_items,
        "output_text": "\n\n".join(text_pieces),
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cached_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
        },
    }


def _build_llm_response(
    openai_shape: dict[str, Any], raw_response: Any,
) -> LLMResponse:
    """Wrap the normalized response in the runner's expected LLMResponse type."""
    from atopile.server.agent.orchestrator_helpers import (
        _extract_function_calls, _extract_output_phase, _extract_text,
    )
    text = _extract_text(openai_shape)
    function_calls = _extract_function_calls(openai_shape)
    phase = _extract_output_phase(openai_shape)

    tool_calls: list[ToolCall] = []
    for call in function_calls:
        raw_args = call.get("arguments", "{}")
        try:
            parsed = json.loads(raw_args)
        except Exception:
            parsed = {}
        tool_calls.append(ToolCall(
            id=call.get("call_id", ""),
            name=call.get("name", ""),
            arguments_raw=raw_args,
            arguments=parsed,
        ))

    usage = TokenUsage(
        input_tokens=openai_shape["usage"]["input_tokens"],
        output_tokens=openai_shape["usage"]["output_tokens"],
        total_tokens=(
            openai_shape["usage"]["input_tokens"]
            + openai_shape["usage"]["output_tokens"]
        ),
        cached_input_tokens=openai_shape["usage"]["cached_input_tokens"],
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
    delay = cfg.api_retry_base_delay_s * (2 ** attempt)
    return min(delay, cfg.api_retry_max_delay_s)


def _shrink_tool_outputs_in_payload(
    payload: dict[str, Any], max_chars: int,
) -> dict[str, Any] | None:
    """Walk the message list, truncate each tool_result content to max_chars.
    Returns a new payload, or None if nothing changed.
    """
    new_messages = []
    changed = False
    for msg in payload.get("messages", []):
        if not isinstance(msg.get("content"), list):
            new_messages.append(msg)
            continue
        new_blocks = []
        for block in msg["content"]:
            if (isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
                and len(block["content"]) > max_chars):
                truncated = (
                    block["content"][:max_chars]
                    + f"\n\n... [truncated, original {len(block['content'])} chars]"
                )
                new_blocks.append({**block, "content": truncated})
                changed = True
            else:
                new_blocks.append(block)
        new_messages.append({**msg, "content": new_blocks})

    if not changed:
        return None
    return {**payload, "messages": new_messages}
```

---

## 4. Config integration

`src/atopile/server/agent/config.py` adds a provider field. **Backwards-compatible:** default behavior is OpenAI, matching upstream.

```python
# Add to AgentConfig dataclass:
provider: str = "openai"             # "openai" | "anthropic"

# Add to AgentConfig.from_env():
provider = _env("EE_AGENT_PROVIDER", "openai").strip().lower()
if provider not in ("openai", "anthropic"):
    raise RuntimeError(
        f"Invalid EE_AGENT_PROVIDER={provider!r}. Use 'openai' or 'anthropic'."
    )

# Update api_key logic:
if provider == "anthropic":
    api_key = (os.getenv("ATOPILE_AGENT_ANTHROPIC_API_KEY")
               or os.getenv("ANTHROPIC_API_KEY"))
    base_url = _env("EE_AGENT_ANTHROPIC_BASE_URL", "")  # empty = SDK default
    default_model = "claude-sonnet-4-6"
else:
    api_key = (os.getenv("ATOPILE_AGENT_OPENAI_API_KEY")
               or os.getenv("OPENAI_API_KEY"))
    base_url = _env("ATOPILE_AGENT_BASE_URL", "https://api.openai.com/v1")
    default_model = "gpt-5.4"

# (then the rest of from_env reads these)
```

---

## 5. Provider instantiation at runtime

`src/atopile/server/routes/agent/utils.py` — the only place atopile picks a provider currently:

```python
# upstream:
orchestrator = AgentRunner(
    config=_config,
    provider=OpenAIProvider(config=_config),
    registry=ToolRegistry(),
)

# becomes:
from atopile.server.agent._ee.provider_anthropic import AnthropicProvider

def _make_provider(config: AgentConfig):
    if config.provider == "anthropic":
        return AnthropicProvider(config=config)
    return OpenAIProvider(config=config)

orchestrator = AgentRunner(
    config=_config,
    provider=_make_provider(_config),
    registry=ToolRegistry(),
)
```

Same pattern for any other instantiation site. Generally there's only one in atopile's codebase.

---

## 6. Test strategy

Three test categories, all in `tests/ee/`:

### 6.1 `test_anthropic_provider.py` — unit tests in isolation

- `test_convert_tool_def_basic` — known input/output pairs
- `test_convert_messages_text_only` — round-trip
- `test_convert_messages_with_tool_calls` — `function_call` + `function_call_output` translate to `tool_use` + `tool_result`
- `test_normalize_to_openai_shape_text` — Anthropic response with text-only content
- `test_normalize_to_openai_shape_tool_calls` — Anthropic response with `tool_use` blocks
- `test_shrink_tool_outputs` — verifies progressive shrink shrinks content as expected
- `test_compaction_skips_short_histories` — doesn't compact <6 messages
- `test_compaction_summary_inserted` — uses a mocked Anthropic client to confirm shape

### 6.2 `test_provider_parity.py` — same inputs, both providers, equivalent shapes

The most important suite. Uses atopile's existing test fixtures with both providers and asserts:
- Tool calls extracted match (same names, same arguments)
- Text outputs are non-empty
- `LLMResponse.tool_calls` list lengths match (within a tolerance for nondeterminism)
- Token usage fields populated
- No exceptions raised

This catches behavioral drift between the two providers as the runner evolves.

### 6.3 `test_anthropic_provider_integration.py` — real API calls (slow, optional)

Marked `pytest.mark.integration`, skipped in fast CI. Runs:
- A trivial schematic emit on Sonnet — must produce a buildable `.ato`
- The same task on Opus — must produce a buildable `.ato`
- A long-context test (synthetic, 100+ tool calls) — must trigger client-side compaction without crashing

Run nightly against real APIs to catch upstream API changes.

---

## 7. Known gotchas

1. **`previous_response_id` is provider-state.** Atopile threads it through every call. Anthropic has no such concept. We accept and ignore it. Downside: each turn re-sends the full history. Atopile's prompt cache key (which we map to `cache_control`) compensates.

2. **`response.stop_reason` mapping.** Anthropic returns `"end_turn"`, `"tool_use"`, `"max_tokens"`, `"stop_sequence"`. Map `end_turn` → `phase="final_answer"`. Anything else: `phase=None`.

3. **Tool definitions with `additionalProperties=False`.** Anthropic's JSON Schema validator is stricter than OpenAI's. Some atopile tool definitions may need minor relaxation. If a parity test fails on schema rejection, this is the cause.

4. **`thinking` blocks (extended thinking).** Some Claude models emit `thinking` content blocks. Filter these out from text aggregation — they're internal, not for the agent's conversation history.

5. **Prompt cache TTL.** Anthropic's ephemeral cache is short-lived (~5 min). For long sessions, the same prompt may not hit cache every turn. This is fine — it's a cost optimization, not a correctness requirement.

6. **`max_tokens` is required.** Anthropic requires `max_tokens` in every call. We hardcode 8192 for now; could make it configurable.

7. **Empty tool input.** If the model emits `{"type": "tool_use", "input": {}}` and the tool expects required parameters, atopile's runner will fail tool execution and the circuit breaker may trip. This is the same behavior as the OpenAI path; nothing provider-specific to fix.

---

## 8. Migration / rollout

1. Implement the provider with all the unit tests passing.
2. Run the parity test suite in `tests/ee/`. Fix any divergence.
3. Run atopile's full upstream test suite — confirm we haven't broken anything when `EE_AGENT_PROVIDER=openai` (default).
4. Run a real, end-to-end design (the coin-cell blinky from `08_PROJECT_PLAN.md` M2) on Anthropic. Manually inspect the build, BOM, and traces.
5. Optionally: run the same design on OpenAI for cost/quality comparison.
6. Set CI to require both providers green on every PR.

---

## 9. Optional: upstream contribution

Once `AnthropicProvider` is stable on real designs for 2–3 months, we should PR it back to atopile/atopile. They've already structured `LLMProvider` as a `Protocol`, which strongly suggests they expected this addition. The PR includes:

- The provider class
- The config flag
- The tests
- Updated docs

This isn't required for our use case (we can carry the fork indefinitely) but it reduces our long-term maintenance burden by collapsing the patch surface.

See `07_ATOPILE_GAPS.md` for other upstream-contribution candidates.
