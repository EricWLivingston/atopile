# 11 · Atopile Fork Mechanics

> **Replaces the earlier "atopile integration" doc.** Under Option C we maintain a fork of atopile and add four custom tools + an `AnthropicProvider`. This doc covers the fork's structure, how we keep it building against upstream, how tools get registered, and how the provider flag is wired through.

---

## 1. Fork structure at a glance

The fork has three kinds of files:

1. **Upstream files we don't touch.** Vast majority. We git-rebase these from upstream periodically.
2. **Upstream files we minimally edit.** Two (and only two): `config.py` (adds provider flag) and `routes/agent/utils.py` (instantiates the right provider). Touch nothing else upstream.
3. **New files we own entirely.** Everything under `src/atopile/server/agent/_ee/`, the whole `src/ee_agent_rag/` package, and our `tests/ee/` directory.

Putting (3) into a single sub-directory of atopile's agent module keeps the rebase delta tiny and makes it obvious to a future contributor (or to upstream reviewers) what's ours.

---

## 2. Files we add

```
src/atopile/server/agent/_ee/
├── __init__.py                  # imports tool modules → triggers registration
├── provider_anthropic.py        # AnthropicProvider class (see 11_ANTHROPIC_PROVIDER.md)
├── tools_rag.py                 # rag_search tool body
├── tools_pyspice.py             # pyspice_run tool body
├── tools_pinmux.py              # pinmux_check tool body
├── tools_ipc.py                 # ipc_check tool body
├── tool_definitions_ee.py       # OpenAI-format schemas for our 4 tools
└── data/
    ├── pinmux_stm32g4.yaml      # vendor pinmux capability tables
    └── pinmux_nrf52840.yaml     # (one per supported MCU family)
```

Tool implementations are thin — most actually live in the out-of-tree `ee_agent_rag` package (for `rag_search`) or in the modules above with helpers. The `_ee/__init__.py` imports them all, triggering registration via atopile's existing `_register_tool` decorator.

---

## 3. Files we minimally edit

### 3.1 `src/atopile/server/agent/config.py`

Add a provider field. **Backwards-compatible:** default behavior is OpenAI, matching upstream.

```python
# additions only:

@dataclass
class AgentConfig:
    # ... upstream fields preserved ...
    provider: str = "openai"                   # NEW — "openai" | "anthropic"

    @classmethod
    def from_env(cls) -> AgentConfig:
        # ... upstream preamble ...

        provider = _env("EE_AGENT_PROVIDER", "openai").strip().lower()
        if provider not in ("openai", "anthropic"):
            raise RuntimeError(
                f"Invalid EE_AGENT_PROVIDER={provider!r}. "
                "Use 'openai' or 'anthropic'."
            )

        if provider == "anthropic":
            api_key = (os.getenv("ATOPILE_AGENT_ANTHROPIC_API_KEY")
                       or os.getenv("ANTHROPIC_API_KEY"))
            base_url = _env("EE_AGENT_ANTHROPIC_BASE_URL", "")
            default_model = "claude-sonnet-4-6"
        else:
            api_key = (os.getenv("ATOPILE_AGENT_OPENAI_API_KEY")
                       or os.getenv("OPENAI_API_KEY"))
            base_url = _env("ATOPILE_AGENT_BASE_URL", "https://api.openai.com/v1")
            default_model = "gpt-5.4"

        return cls(
            provider=provider,
            base_url=base_url,
            model=_env("ATOPILE_AGENT_MODEL", default_model),
            api_key=api_key,
            # ... rest of fields ...
        )
```

About 20 lines of diff.

### 3.2 `src/atopile/server/routes/agent/utils.py`

One change: pick the right provider at startup.

```python
# upstream:
from atopile.server.agent.provider import OpenAIProvider

# becomes:
from atopile.server.agent.provider import OpenAIProvider
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

About 8 lines of diff.

---

## 4. Files we leave untouched in upstream's agent module

Critical: we do **not** modify any of these. If we're tempted to, that's a signal to push the change into `_ee/` instead.

- `runner.py` — the main agent loop. Heavily complex, well-tested by atopile. Hands-off.
- `provider.py` — the `LLMProvider` protocol and `OpenAIProvider`. Hands-off; we add a new file next to it.
- `registry.py` — `ToolRegistry`. Hands-off; we register via `_register_tool` decorator.
- `tools.py` — the existing 40+ tool implementations. Hands-off. Our tools live in `_ee/`.
- `tool_definitions*.py` — upstream schemas. Hands-off; ours live in `_ee/tool_definitions_ee.py`.
- `policy.py` — LINE:HASH safe-edit. Hands-off.
- `checklist.py`, `message_log.py`, `circuit_breaker.py`, `activity_summary.py`, `mediator*.py`, `context.py`, `orchestrator_helpers.py` — all hands-off. Our `AnthropicProvider` normalizes responses to the shape these expect; they don't change.
- `.claude/skills/{agent,ato,planning,code-review,library,package-agent,ato-language}/SKILL.md` — hands-off. We add a new skill at `.claude/skills/ee-agent/SKILL.md` (see §6) but don't edit existing ones.

If a future change requires editing any of these, treat that change as an upstream PR candidate — fix it in atopile proper, not in our fork.

---

## 5. Tool registration mechanism

Atopile uses a `_register_tool` decorator in `tools.py` that adds entries to a module-level registry, plus a separate schema registry. We piggy-back on this.

In `src/atopile/server/agent/_ee/tools_rag.py`:

```python
"""RAG search tool implementation."""
from pathlib import Path
from typing import Any

from atopile.server.agent.tools import _register_tool

# Import our retriever (out-of-tree package)
from ee_agent_rag.retriever import search as _rag_search

@_register_tool("rag_search")
async def _tool_rag_search(
    arguments: dict[str, Any],
    project_root: Path,
    ctx: Any,
) -> dict[str, Any]:
    """Search the engineering knowledge base."""
    query = arguments["query"]
    corpus = arguments.get("corpus")  # None = all corpora
    top_k = arguments.get("top_k", 5)
    filter_ = arguments.get("filter")

    results = await _rag_search(
        query=query, corpus=corpus, top_k=top_k, filter=filter_,
    )
    return {"results": results, "count": len(results)}
```

The tool definition (OpenAI-format schema) goes into a separate file that contributes to `get_tool_definitions()`:

```python
# src/atopile/server/agent/_ee/tool_definitions_ee.py
"""Tool definitions for EE-agent custom tools."""

def get_ee_tool_definitions() -> list[dict]:
    return [
        {
            "type": "function",
            "name": "rag_search",
            "description": (
                "Search engineering knowledge: datasheets, app notes, standards "
                "(IPC, MIL, JEDEC), textbooks, atopile examples, atopile docs. "
                "Returns ranked, cited chunks. Always cite the source when "
                "using retrieved content in design rationale."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "corpus": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "string",
                            "enum": ["datasheets", "app_notes", "standards",
                                     "textbooks", "internal_standards",
                                     "atopile_examples", "atopile_docs"],
                        },
                    },
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    "filter": {"type": ["object", "null"]},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        # ... pyspice_run, pinmux_check, ipc_check ...
    ]
```

And in `_ee/__init__.py`:

```python
"""EE-agent tools — registered into atopile's ToolRegistry on import."""
from . import tools_rag, tools_pyspice, tools_pinmux, tools_ipc  # noqa: F401
```

This needs to be imported once at runtime to trigger the decorators. The cleanest place is to add a single line near the bottom of upstream's `tools.py`:

```python
# At the bottom of src/atopile/server/agent/tools.py:
try:
    from atopile.server.agent import _ee  # noqa: F401  — register EE-agent tools
except ImportError:
    pass  # _ee module not present (running against vanilla atopile) — OK
```

The `try/except` keeps the upstream import working even if someone uses our codebase without the `_ee` directory. The diff to `tools.py` is 3 lines.

Tool definitions get appended via a minimal edit to `get_tool_definitions()`:

```python
# Near the top of src/atopile/server/agent/tool_definitions.py:
def get_tool_definitions() -> list[dict]:
    defs = [
        *get_project_tool_definitions(),
        # ... upstream definitions ...
    ]
    try:
        from atopile.server.agent._ee.tool_definitions_ee import (
            get_ee_tool_definitions,
        )
        defs.extend(get_ee_tool_definitions())
    except ImportError:
        pass
    return defs
```

About 6 lines of diff.

---

## 6. Skill addendum

We add one new skill — `.claude/skills/ee-agent/SKILL.md` — covering when to use the four new tools. It's loaded by adding `"ee-agent"` to atopile's `fixed_skill_ids` config list:

```bash
export ATOPILE_AGENT_FIXED_SKILL_IDS="agent,ato,planning,ee-agent"
```

The skill is ~300 lines of markdown covering:
- When to call `rag_search` (and how to use citations)
- When to call `pyspice_run`
- When to call `pinmux_check`
- When to call `ipc_check`
- How to cite RAG findings in module docstrings and design rationale

This is purely additive — atopile's existing skills (`agent`, `ato`, `planning`, etc.) are untouched.

---

## 7. CI strategy

`.github/workflows/ci.yml` in our fork extends upstream's:

```yaml
jobs:
  upstream-tests:
    name: "Upstream atopile tests"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run pytest tests/  # the upstream test suite — must stay green

  ee-tests-openai:
    name: "EE tests on OpenAI provider"
    runs-on: ubuntu-latest
    env:
      EE_AGENT_PROVIDER: openai
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run pytest tests/ee/ -m "not integration"

  ee-tests-anthropic:
    name: "EE tests on Anthropic provider"
    runs-on: ubuntu-latest
    env:
      EE_AGENT_PROVIDER: anthropic
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run pytest tests/ee/ -m "not integration"

  parity:
    name: "Provider parity"
    runs-on: ubuntu-latest
    needs: [ee-tests-openai, ee-tests-anthropic]
    steps:
      # ... compare outputs from both providers on a fixed prompt+tool set
```

Plus a nightly job that runs integration tests against real APIs.

---

## 8. Rebasing from upstream

Quarterly cadence. Process:

```bash
# Set up upstream once
git remote add upstream https://github.com/atopile/atopile.git
git remote set-url --push upstream no_push

# At rebase time
git fetch upstream
git checkout main
git rebase upstream/main

# Common conflict points (small, predictable):
#   - src/atopile/server/agent/config.py    (provider flag)
#   - src/atopile/server/routes/agent/utils.py (provider instantiation)
#   - src/atopile/server/agent/tools.py       (3-line _ee import)
#   - src/atopile/server/agent/tool_definitions.py (6-line _ee schema append)

# Resolve, re-test
uv sync
uv run pytest tests/                       # upstream tests
uv run pytest tests/ee/ -m "not integration"  # our tests, both providers

# Push
git push origin main --force-with-lease
```

Expected resolution time: 30 minutes when there are no large reorgs upstream, several hours if the agent module restructures. The `_ee/` directory and the `try/except` import pattern make the patch surface unusually small.

---

## 9. Running the agent locally

After installation (`uv sync`), set environment variables:

```bash
# Provider selection
export EE_AGENT_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export ATOPILE_AGENT_MODEL=claude-sonnet-4-6   # or claude-opus-4-7

# Or alternatively:
export EE_AGENT_PROVIDER=openai
export OPENAI_API_KEY=sk-...
export ATOPILE_AGENT_MODEL=gpt-5.4

# Skill bundle (includes our new skill)
export ATOPILE_AGENT_FIXED_SKILL_IDS="agent,ato,planning,ee-agent"

# RAG corpus location
export EE_AGENT_RAG_CORPUS_DIR=~/ee-agent-corpus

# Run the agent server (atopile's existing FastAPI)
ato serve
# Or use the CLI:
ato chat
```

The agent now runs with the chosen provider, has access to all 40+ upstream tools plus our 4 custom tools, and loads the EE-agent skill alongside atopile's bundle.

---

## 10. Versioning the fork

Our fork's version is `<upstream_version>+ee.<short_sha>`. E.g. `0.15.4+ee.a3b1e7`. This makes pinning explicit in downstream `pyproject.toml`:

```toml
dependencies = [
    "atopile @ git+https://github.com/<org>/atopile.git@v0.15.4+ee.a3b1e7",
]
```

Or, more typical, depend on a tagged release of our fork:

```toml
dependencies = [
    "ee-agent @ git+https://github.com/<org>/atopile.git@v0.15.4-ee-2",
]
```

We tag releases as `v<upstream>-ee-<n>` where `n` increments each time we ship a new fork release against the same upstream version.

---

## 11. When (if) we upstream the provider

Once `AnthropicProvider` is stable on real designs for ~2–3 months, we PR it back to atopile. After it lands upstream:

- Delete `_ee/provider_anthropic.py` from our fork
- Delete the provider-selection logic from `config.py` (now upstream)
- Delete the `_make_provider` helper in `routes/agent/utils.py`
- Keep `_ee/tools_*.py` and `_ee/tool_definitions_ee.py` — these are EE-agent-specific
- Our fork becomes thinner; mostly just custom tools and RAG

This is the long-term endgame for the provider. Custom tools stay downstream forever (they're our value-add, not generally useful for atopile users).

See `07_ATOPILE_GAPS.md` for other upstream contribution candidates.
