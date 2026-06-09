"""EE-agent custom tool handlers (Option C).

Registers the EE tool handlers into atopile's existing ``_TOOL_HANDLERS`` registry via
the ``@_register_tool`` decorator. Imported for its side effects from the bottom of
``tools.py`` so registration happens whenever the tool module loads; the matching
schemas live in ``_ee/tool_definitions_ee.py``.

M3 scope is plumbing only: ``ee_ping`` echoes (proves the path end to end) and the three
real tools (``rag_search``, ``pyspice_run``, ``ipc_check``) return a graceful
not-implemented payload so live runs degrade cleanly until M4–M6 fill in the bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atopile.dataclasses import AppContext
from atopile.server.agent.tools import _register_tool


@_register_tool("ee_ping")
async def _tool_ee_ping(
    arguments: dict[str, Any], project_root: Path, ctx: AppContext
) -> dict[str, Any]:
    """Echo the message back — smoke test for the EE tool plumbing."""
    return {"ok": True, "echo": arguments.get("message", "")}


@_register_tool("rag_search")
async def _tool_rag_search(
    arguments: dict[str, Any], project_root: Path, ctx: AppContext
) -> dict[str, Any]:
    """Hybrid retrieval (dense+BM25 -> RRF -> Cohere rerank) over the EE knowledge base.

    Implemented in M4; delegates to the framework-agnostic retriever in ``ee_agent_rag``.
    """
    from .tools_rag import run_rag_search

    return await run_rag_search(arguments)


@_register_tool("pyspice_run")
async def _tool_pyspice_run(
    arguments: dict[str, Any], project_root: Path, ctx: AppContext
) -> dict[str, Any]:
    """Scaffold: SPICE simulation lands in M5."""
    return {"ok": False, "error": "pyspice_run not implemented yet (M5)"}


@_register_tool("ipc_check")
async def _tool_ipc_check(
    arguments: dict[str, Any], project_root: Path, ctx: AppContext
) -> dict[str, Any]:
    """Scaffold: IPC-2221/2152 checks land in M6."""
    return {"ok": False, "error": "ipc_check not implemented yet (M6)"}
