"""EE-agent custom tool handlers (Option C).

Registers the EE tool handlers into atopile's existing ``_TOOL_HANDLERS`` registry via
the ``@_register_tool`` decorator. Imported for its side effects from the bottom of
``tools.py`` so registration happens whenever the tool module loads; the matching
schemas live in ``_ee/tool_definitions_ee.py``.

``ee_ping`` echoes (proves the path end to end). ``rag_search`` (M4) and ``pyspice_run``
(M5) delegate to their framework-agnostic packages (``ee_agent_rag`` /
``ee_agent_spice``) via thin wrappers; ``ipc_check`` (M6) is still a graceful
not-implemented stub so live runs degrade cleanly until its body lands. ``skills_list``
and ``skill_read`` expose the on-demand skill library (delegating to ``tools_skills``).
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

    Implemented in M4; delegates to the framework-agnostic retriever in ee_agent_rag.
    """
    from .tools_rag import run_rag_search

    return await run_rag_search(arguments)


@_register_tool("pyspice_run")
async def _tool_pyspice_run(
    arguments: dict[str, Any], project_root: Path, ctx: AppContext
) -> dict[str, Any]:
    """Run a SPICE DC/AC/transient/op analysis on an agent-authored netlist.

    Implemented in M5; delegates to the framework-agnostic runner in ``ee_agent_spice``.
    """
    from .tools_pyspice import run_pyspice

    arguments.setdefault("project_path", str(project_root))
    return await run_pyspice(arguments)


@_register_tool("ipc_check")
async def _tool_ipc_check(
    arguments: dict[str, Any], project_root: Path, ctx: AppContext
) -> dict[str, Any]:
    """Scaffold: IPC-2221/2152 checks land in M6."""
    return {"ok": False, "error": "ipc_check not implemented yet (M6)"}


@_register_tool("skills_list")
async def _tool_skills_list(
    arguments: dict[str, Any], project_root: Path, ctx: AppContext
) -> dict[str, Any]:
    """List every available skill (id + description); always-loaded ones are flagged.

    Discovery surface for the on-demand skill library; pair with ``skill_read``.
    """
    from .tools_skills import run_skills_list

    return await run_skills_list(arguments)


@_register_tool("skill_read")
async def _tool_skill_read(
    arguments: dict[str, Any], project_root: Path, ctx: AppContext
) -> dict[str, Any]:
    """Read one skill's full SKILL.md body on demand (``skills_list`` lists the ids)."""
    from .tools_skills import run_skill_read

    return await run_skill_read(arguments)
