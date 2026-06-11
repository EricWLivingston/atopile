# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""M3 — EE tool-registration plumbing (offline).

Verifies the EE tools are registered, schema'd, exposed via the shared tool list, and
dispatchable through `execute_tool`, and that the registry consistency guard passes.
No API key, network, or build required.
"""

import asyncio
from pathlib import Path

import pytest

from atopile.server.agent import mediator_catalog, tools

_EE_TOOLS = {
    "ee_ping",
    "rag_search",
    "pyspice_run",
    "ipc_check",
    "skills_list",
    "skill_read",
}
_REAL_TOOLS = {"rag_search", "pyspice_run", "ipc_check"}


def test_ee_tools_have_schemas_and_handlers():
    # get_tool_definitions() runs _ensure_tool_registry_consistency once; it must not
    # raise, which proves every EE schema has a matching handler and vice versa.
    defs = tools.get_tool_definitions()
    schema_names = {
        d["name"] for d in defs if isinstance(d, dict) and d.get("type") == "function"
    }
    assert _EE_TOOLS <= schema_names
    assert _EE_TOOLS <= set(tools.get_tool_names())


def test_real_tools_in_mediator_directory():
    available = set(mediator_catalog.available_tool_names())
    assert _REAL_TOOLS <= available
    # ee_ping is a throwaway smoke tool; intentionally not advertised in the directory.
    assert "ee_ping" not in available


def test_ee_ping_echoes():
    result = asyncio.run(
        tools.execute_tool(
            name="ee_ping",
            arguments={"message": "hi"},
            project_root=Path("."),
            ctx=None,  # ee_ping ignores project_root/ctx
        )
    )
    assert result == {"ok": True, "echo": "hi"}


def test_ee_ping_defaults_empty_message():
    result = asyncio.run(
        tools.execute_tool(
            name="ee_ping", arguments={}, project_root=Path("."), ctx=None
        )
    )
    assert result == {"ok": True, "echo": ""}


@pytest.mark.parametrize(("name", "milestone"), [("ipc_check", "M6")])
def test_real_tool_stubs_return_gracefully(name: str, milestone: str):
    result = asyncio.run(
        tools.execute_tool(
            name=name, arguments={}, project_root=Path("."), ctx=None
        )
    )
    assert result["ok"] is False
    assert milestone in result["error"]


def test_pyspice_run_implemented_degrades_without_analysis():
    # pyspice_run is live (M5); with no analysis it must reject gracefully
    # (success=False), not raise. (Wrapper behaviour in test_ee_pyspice_tool.)
    result = asyncio.run(
        tools.execute_tool(
            name="pyspice_run", arguments={}, project_root=Path("."), ctx=None
        )
    )
    assert result["success"] is False
    assert result["errors"][0]["type"] == "invalid_analysis"


def test_rag_search_implemented_degrades_without_query():
    # rag_search is live (M4); with no query it must reject gracefully, not raise or
    # report a not-implemented stub. (Wrapper behaviour covered in test_ee_rag_tool.py.)
    result = asyncio.run(
        tools.execute_tool(
            name="rag_search", arguments={}, project_root=Path("."), ctx=None
        )
    )
    assert result["ok"] is False
    assert "query" in result["error"]
