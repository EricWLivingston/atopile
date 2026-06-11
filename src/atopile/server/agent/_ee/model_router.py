"""Per-turn complexity router for dynamic model selection (EE addition).

Classifies each incoming user turn as ``simple`` / ``standard`` / ``complex`` with one
cheap Anthropic call (``config.router_model``), so the runner can match the model tier
to the task: Haiku-class for conversational/Q&A turns, the standard model for ordinary
work, an Opus-class model for genuinely hard design tasks.

Design rules:
- **Fail-open.** Routing must never block or fail a run: any exception, timeout, or
  unparseable output falls back to ``"standard"`` (today's behavior).
- **Never downshift an active design.** When a checklist/design is in progress the
  classifier is told (and the parser enforces) that ``simple`` is not available — a
  cheap model mid-design is the costly failure mode, not an expensive model on a
  cheap turn.
- Anthropic-only: gated upstream by ``AgentConfig.dynamic_model`` (see config.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from atopile.server.agent.config import AgentConfig

logger = logging.getLogger(__name__)

Tier = Literal["simple", "standard", "complex"]

_VALID_TIERS: tuple[Tier, ...] = ("simple", "standard", "complex")

# Hard cap on the classification call. The router runs before the first real model
# call of every turn; a slow router is pure added latency, so we'd rather fall back
# to "standard" than wait.
_ROUTER_TIMEOUT_S = 5.0

_ROUTER_MAX_TOKENS = 8  # one word

# Keep the prompt small: the router sees a truncated user message, not the project.
_USER_MESSAGE_MAX_CHARS = 2_000

_SYSTEM = """\
You route requests to an EDA (electronics design) agent onto a model tier.
Reply with exactly one word: simple, standard, or complex.

simple — conversational replies, factual questions, single-value lookups,
explaining existing code/design, reading one file. No design mutation.
standard — ordinary design edits, adding/configuring a component, fixing a
build error, running builds/tools, small refactors.
complex — designing a new board or multi-module subsystem, debugging solver
contradictions or cross-module issues, large refactors, anything requiring
long multi-step planning.

If a design task is already in progress, never answer simple.
When unsure, answer standard."""


def tier_to_model(tier: Tier, config: AgentConfig) -> str:
    """Map a tier onto a concrete model id, falling back to the standard model."""
    if tier == "simple":
        return config.model_simple or config.model
    if tier == "complex":
        return config.model_complex or config.model
    return config.model


async def classify_turn(
    user_message: str,
    *,
    has_active_design: bool,
    history_len: int,
    config: AgentConfig,
) -> Tier:
    """Classify one user turn. Fail-open: any failure returns ``"standard"``."""
    try:
        return await asyncio.wait_for(
            _classify(
                user_message,
                has_active_design=has_active_design,
                history_len=history_len,
                config=config,
            ),
            timeout=_ROUTER_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - routing must never fail a run
        logger.warning("model router fell back to 'standard': %s", exc)
        return "standard"


async def _classify(
    user_message: str,
    *,
    has_active_design: bool,
    history_len: int,
    config: AgentConfig,
) -> Tier:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(
        api_key=config.api_key,
        base_url=config.base_url or None,
        timeout=_ROUTER_TIMEOUT_S,
    )
    context_bits = []
    if has_active_design:
        context_bits.append("A design task is already in progress in this session.")
    if history_len:
        context_bits.append(f"The conversation has {history_len} prior turns.")
    prompt = "\n".join(
        [*context_bits, "User request:", user_message[:_USER_MESSAGE_MAX_CHARS]]
    )

    response = await client.messages.create(
        model=config.router_model or config.model_simple or config.model,
        max_tokens=_ROUTER_MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_tier(response, has_active_design=has_active_design)


def _parse_tier(response: Any, has_active_design: bool) -> Tier:
    """Strictly parse the one-word reply; enforce the no-downshift rule."""
    text = ""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = (getattr(block, "text", "") or "").strip().lower()
            break
    word = text.split()[0].strip(".,:;!\"'") if text else ""
    tier: Tier = word if word in _VALID_TIERS else "standard"  # type: ignore[assignment]
    if tier == "simple" and has_active_design:
        return "standard"
    return tier
