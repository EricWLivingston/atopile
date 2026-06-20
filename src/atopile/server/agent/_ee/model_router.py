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
import re
from typing import Any, Literal

from atopile.server.agent.config import AgentConfig

logger = logging.getLogger(__name__)

Tier = Literal["simple", "standard", "complex"]

_VALID_TIERS: tuple[Tier, ...] = ("simple", "standard", "complex")

# Tier ordering for clamping (low → high).
_TIER_ORDER: dict[Tier, int] = {"simple": 0, "standard": 1, "complex": 2}


def _max_tier(a: Tier, b: Tier) -> Tier:
    """Return the higher of two tiers."""
    return a if _TIER_ORDER[a] >= _TIER_ORDER[b] else b

# Hard cap on the classification call. The router runs before the first real model
# call of every turn; a slow router is pure added latency, so we'd rather fall back
# to "standard" than wait.
_ROUTER_TIMEOUT_S = 5.0

_ROUTER_MAX_TOKENS = 8  # one word

# Keep the prompt small: the router sees a truncated user message, not the project.
_USER_MESSAGE_MAX_CHARS = 2_000

# Cheap pre-filter so we don't spend a classification round-trip on obviously-trivial
# turns (CODE_AUDIT T3). A short message with none of these design-intent tokens is
# routed "simple" with no API call; anything longer or design-shaped falls through to
# the model classifier. Conservative on purpose: it only short-circuits the clear cases.
_SHORT_TURN_MAX_CHARS = 140
_DESIGN_KEYWORDS = (
    "design",
    "board",
    "schematic",
    "pcb",
    "layout",
    "route",
    "build",
    "compile",
    "add ",
    "remove",
    "component",
    "resistor",
    "capacitor",
    "inductor",
    "diode",
    "transistor",
    "mosfet",
    "regulator",
    "module",
    "net",
    "footprint",
    "pick",
    "simulate",
    "spice",
    "refactor",
    "debug",
    "fix",
    "error",
    "solver",
    "connect",
    "voltage",
    "current",
    "circuit",
)


# A long, design-keyword-dense first turn must never route below ``standard`` — the
# weak router model under-rated a board-design prompt as ``simple`` and the whole design
# then ran on Haiku. The floor doesn't trust the classifier; it clamps its output up.
_FLOOR_MIN_CHARS = 400
_FLOOR_DESIGN_KEYWORD_HITS = 3  # distinct design tokens → clearly a design turn
# Phrases that signal a brand-new design/board/subsystem → floor at ``complex``.
_NEW_DESIGN_RE = re.compile(
    r"design\s+(a|an|the|my|this|me)?\s*[\w\- ]*?"
    r"(board|module|circuit|subsystem|schematic|pcb|amplifier|supply|regulator|filter)",
    re.IGNORECASE,
)


def _min_floor(user_message: str) -> Tier:
    """Minimum tier a turn may route to, independent of the classifier.

    Returns ``simple`` when nothing forces a floor (the classifier/heuristic decides
    freely), ``standard`` for design-shaped turns, and ``complex`` for prompts that read
    like designing a new board/subsystem. Applied as a clamp so a misclassification can
    only ever route *up*, never below the floor.
    """
    msg = user_message.strip()
    low = msg.lower()
    keyword_hits = sum(1 for kw in _DESIGN_KEYWORDS if kw in low)
    long_and_design_dense = (
        len(msg) >= _FLOOR_MIN_CHARS and keyword_hits >= _FLOOR_DESIGN_KEYWORD_HITS
    )
    if _NEW_DESIGN_RE.search(low) and long_and_design_dense:
        return "complex"
    if long_and_design_dense or keyword_hits >= _FLOOR_DESIGN_KEYWORD_HITS:
        return "standard"
    return "simple"


def _heuristic_tier(user_message: str, *, has_active_design: bool) -> Tier | None:
    """Resolve obvious turns without an API call; ``None`` means "ask the classifier".

    Only fires for short messages with no design-intent keyword while *no* design is
    active — the conversational/factual turns the classifier would call ``simple``
    anyway. Mid-design we always ask the classifier (the no-downshift phase is where a
    misroute is costly, so it's not worth saving a call there)."""
    if has_active_design:
        return None
    msg = user_message.strip()
    if not msg or len(msg) > _SHORT_TURN_MAX_CHARS:
        return None
    low = msg.lower()
    if any(kw in low for kw in _DESIGN_KEYWORDS):
        return None
    return "simple"

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
    """Classify one user turn. Fail-open: any failure returns ``"standard"``.

    A content-based floor (``_min_floor``) clamps the result up so a long design prompt
    can never route to a weaker tier than the task warrants — even on the first turn,
    where ``has_active_design`` is False and the no-downshift rule doesn't apply, and
    even if the (weak) classifier under-rates it.
    """
    floor = _min_floor(user_message)
    heuristic = _heuristic_tier(user_message, has_active_design=has_active_design)
    if heuristic is not None:
        tier = _max_tier(heuristic, floor)
        logger.debug(
            "model router short-circuited to %r (floor=%r, no API call)", tier, floor
        )
        return tier
    try:
        classified = await asyncio.wait_for(
            _classify(
                user_message,
                has_active_design=has_active_design,
                history_len=history_len,
                config=config,
            ),
            timeout=_ROUTER_TIMEOUT_S,
        )
        return _max_tier(classified, floor)
    except Exception as exc:  # noqa: BLE001 - routing must never fail a run
        logger.warning("model router fell back to 'standard': %s", exc)
        return _max_tier("standard", floor)


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
