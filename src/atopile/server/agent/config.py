"""Agent configuration — pure data, zero runtime dependencies."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(
    key: str, default: str, *, lo: int | None = None, hi: int | None = None
) -> int:
    v = int(_env(key, default))
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _env_float(
    key: str, default: str, *, lo: float | None = None, hi: float | None = None
) -> float:
    v = float(_env(key, default))
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


_TRACE_DISABLE_VALUES = {"0", "false", "no", "off"}


@dataclass
class AgentConfig:
    provider: str = "openai"  # "openai" | "anthropic"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.4"
    summary_model: str = "gpt-4.1-nano"
    # Dynamic complexity routing (EE addition; anthropic-only, opt-in). When
    # ``dynamic_model`` is true the runner classifies each user turn and picks
    # ``model_simple`` / ``model`` / ``model_complex``; the classifier itself runs
    # on ``router_model``. Off by default so upstream behavior is untouched.
    dynamic_model: bool = False
    model_simple: str = ""
    model_complex: str = ""
    router_model: str = ""
    api_key: str | None = None
    timeout_s: float = 120.0
    summary_timeout_s: float = 8.0
    max_tool_loops: int = 240
    max_turn_seconds: float = 7_200.0
    api_retries: int = 4
    api_retry_base_delay_s: float = 0.5
    api_retry_max_delay_s: float = 8.0
    skills_dir: Path = field(
        default_factory=lambda: (
            Path(__file__).resolve().parents[4] / ".claude" / "skills"
        )
    )
    fixed_skill_ids: list[str] = field(
        default_factory=lambda: ["agent", "ato", "planning"]
    )
    fixed_skill_token_budgets: dict[str, int] = field(default_factory=dict)
    fixed_skill_chars_per_token: float = 4.0
    fixed_skill_total_max_chars: int = 220_000
    prefix_max_chars: int = 220_000
    context_summary_max_chars: int = 8_000
    user_message_max_chars: int = 12_000
    tool_output_max_chars: int = 10_000
    context_hard_max_tokens: int = 1_000_000
    prompt_cache_retention: str = "24h"
    max_checklist_continuations: int = 25
    silent_retry_max: int = 2
    trace_enabled: bool = True
    trace_preview_max_chars: int = 4_000
    activity_summary_enabled: bool = True
    activity_summary_max_events: int = 6
    activity_summary_min_interval_s: float = 1.5

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Build config from env for deployed agents and local tuning.

        The agent runs in several contexts: local development, tests, and hosted
        backends. The env surface is intentionally limited to deployment-facing
        concerns such as credentials, model selection, and coarse timeouts.
        """
        from atopile.server.agent.orchestrator_helpers import (
            _parse_fixed_skill_token_budgets,
        )

        # Load .env so API keys / EE_AGENT_PROVIDER are available. The backend is
        # launched with cwd set to the *opened project* root (see the VS Code
        # extension's backendServer spawn), which is usually NOT inside the atopile
        # checkout — so a cwd-only search misses this fork's gitignored repo-root
        # `.env` and the provider silently falls back to the openai default with no
        # key (run fails instantly; the UI just shows "thinking..."). Search the cwd
        # first (so a project can ship its own `.env`), then fall back to the atopile
        # source-tree root. load_dotenv defaults to override=False, so the cwd `.env`
        # wins on any overlapping key.
        try:
            from dotenv import find_dotenv, load_dotenv

            load_dotenv(find_dotenv(usecwd=True))
            # src/atopile/server/agent/config.py -> repo root is 4 parents up.
            repo_env = Path(__file__).resolve().parents[4] / ".env"
            if repo_env.is_file():
                load_dotenv(repo_env)
        except ImportError:
            pass

        # Provider selection (EE addition). Default "openai" preserves upstream
        # behavior; "anthropic" swaps credentials, endpoint, and model defaults.
        provider = _env("EE_AGENT_PROVIDER", "openai").strip().lower()
        if provider not in ("openai", "anthropic"):
            raise RuntimeError(
                f"Invalid EE_AGENT_PROVIDER={provider!r}. "
                "Use 'openai' or 'anthropic'."
            )

        if provider == "anthropic":
            api_key = os.getenv("ATOPILE_AGENT_ANTHROPIC_API_KEY") or os.getenv(
                "ANTHROPIC_API_KEY"
            )
            # Empty base_url → the Anthropic SDK uses its own default endpoint
            # (provider_anthropic does ``base_url=self._config.base_url or None``).
            base_url = _env("EE_AGENT_ANTHROPIC_BASE_URL", "")
            default_model = "claude-sonnet-4-6"
            # Reuse the (known-valid) main model for summaries; override with
            # ATOPILE_AGENT_SUMMARY_MODEL for a cheaper summarizer.
            default_summary_model = "claude-sonnet-4-6"
            default_model_simple = "claude-haiku-4-5-20251001"
            default_model_complex = "claude-opus-4-8"
        else:
            api_key = os.getenv("ATOPILE_AGENT_OPENAI_API_KEY") or os.getenv(
                "OPENAI_API_KEY"
            )
            base_url = _env("ATOPILE_AGENT_BASE_URL", "https://api.openai.com/v1")
            default_model = "gpt-5.4"
            default_summary_model = "gpt-4.1-nano"
            default_model_simple = ""
            default_model_complex = ""

        # Dynamic complexity routing is anthropic-only in v1: the Anthropic
        # provider is stateless-emulated (full transcript rebuilt per call), so a
        # per-call model switch is provably safe; the OpenAI Responses chain
        # references server-side state created under one model and is unverified.
        dynamic_model = (
            provider == "anthropic"
            and _env("EE_AGENT_DYNAMIC_MODEL", "0").strip().lower()
            not in _TRACE_DISABLE_VALUES | {""}
        )
        model_simple = _env("ATOPILE_AGENT_MODEL_SIMPLE", default_model_simple)
        model_complex = _env("ATOPILE_AGENT_MODEL_COMPLEX", default_model_complex)

        fixed_skill_ids = ["agent", "ato", "planning"]
        return cls(
            provider=provider,
            base_url=base_url,
            model=_env("ATOPILE_AGENT_MODEL", default_model),
            summary_model=_env("ATOPILE_AGENT_SUMMARY_MODEL", default_summary_model),
            dynamic_model=dynamic_model,
            model_simple=model_simple,
            model_complex=model_complex,
            router_model=_env("ATOPILE_AGENT_ROUTER_MODEL", model_simple),
            api_key=api_key,
            timeout_s=_env_float("ATOPILE_AGENT_TIMEOUT_S", "120"),
            summary_timeout_s=_env_float(
                "ATOPILE_AGENT_SUMMARY_TIMEOUT_S", "8", lo=1.0, hi=30.0
            ),
            max_tool_loops=_env_int("ATOPILE_AGENT_MAX_TOOL_LOOPS", "240"),
            max_turn_seconds=_env_float(
                "ATOPILE_AGENT_MAX_TURN_SECONDS", "7200", lo=30.0, hi=7_200.0
            ),
            fixed_skill_ids=fixed_skill_ids,
            fixed_skill_token_budgets=_parse_fixed_skill_token_budgets(
                _env(
                    "ATOPILE_AGENT_FIXED_SKILL_TOKEN_BUDGETS",
                    "agent:10000,ato:40000,planning:5000",
                ),
                default_skill_ids=fixed_skill_ids,
            ),
            trace_enabled=_env("ATOPILE_AGENT_TRACE_ENABLED", "1").strip().lower()
            not in _TRACE_DISABLE_VALUES,
            activity_summary_enabled=_env("ATOPILE_AGENT_ACTIVITY_SUMMARY_ENABLED", "1")
            .strip()
            .lower()
            not in _TRACE_DISABLE_VALUES,
        )
