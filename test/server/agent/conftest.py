"""Shared fixtures for EE-agent provider tests (Milestone 2).

The unit and parity suites run fully offline by stubbing the network layer.
The integration suite is marked ``integration`` and auto-skipped unless
``ANTHROPIC_API_KEY`` is present.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from atopile.server.agent.config import AgentConfig


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: real-API tests; require ANTHROPIC_API_KEY and network.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.getenv("ANTHROPIC_API_KEY"):
        return
    skip = pytest.mark.skip(reason="ANTHROPIC_API_KEY not set; skipping live tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def anthropic_config() -> AgentConfig:
    """A minimal Anthropic-flavored config (no real key needed for stubbed tests)."""
    return AgentConfig(
        provider="anthropic",
        base_url="",
        model="claude-sonnet-4-6",
        summary_model="claude-sonnet-4-6",
        api_key="test-key",
    )


@pytest.fixture
def openai_config() -> AgentConfig:
    return AgentConfig(provider="openai", api_key="test-key")


# ── Fake Anthropic SDK objects ────────────────────────────────────────
# The provider only touches attribute access (block.type/.text/.id/.name/.input,
# response.content/.stop_reason/.id/.usage), so SimpleNamespace stand-ins are
# sufficient and avoid depending on the SDK's concrete model classes.


@pytest.fixture
def text_block():
    def _make(text: str) -> SimpleNamespace:
        return SimpleNamespace(type="text", text=text)

    return _make


@pytest.fixture
def tool_use_block():
    def _make(id: str, name: str, input: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(type="tool_use", id=id, name=name, input=input)

    return _make


@pytest.fixture
def make_anthropic_response():
    def _make(
        *,
        content: list[Any],
        stop_reason: str = "end_turn",
        id: str = "msg_1",
        input_tokens: int = 10,
        output_tokens: int = 5,
        cache_read_input_tokens: int = 0,
    ) -> SimpleNamespace:
        usage = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
        return SimpleNamespace(
            id=id, content=content, stop_reason=stop_reason, usage=usage
        )

    return _make
