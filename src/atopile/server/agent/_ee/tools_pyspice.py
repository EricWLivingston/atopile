"""Thin agent-side wrapper for the ``pyspice_run`` tool (M5).

Bridges atopile's async tool handler to the synchronous, framework-agnostic SPICE runner
in ``ee_agent_spice``. The agent authors a SPICE netlist body (inline ``netlist`` text,
or a path it wrote); we run the requested analysis and return per-probe summary stats.
Every failure mode (no netlist, ngspice missing, libngspice absent, convergence failure)
degrades to a structured ``{"success": False, ...}`` payload so a run never crashes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _err(err_type: str, rationale: str) -> dict[str, Any]:
    return {
        "success": False,
        "results": [],
        "errors": [{"type": err_type, "rationale": rationale}],
    }


def _run(arguments: dict[str, Any]) -> dict[str, Any]:
    """Synchronous core — runs in a worker thread."""
    analysis = (arguments.get("analysis") or "").strip().lower()
    if analysis not in ("op", "dc", "ac", "tran"):
        return _err(
            "invalid_analysis", f"analysis must be op|dc|ac|tran, got {analysis!r}"
        )

    # Resolve the netlist: inline text wins; otherwise read the path the agent wrote.
    netlist = arguments.get("netlist")
    if not netlist:
        raw_path = arguments.get("netlist_path")
        if not raw_path:
            return _err(
                "missing_netlist", "provide either 'netlist' text or 'netlist_path'"
            )
        project_root = Path(arguments.get("project_path") or Path.cwd()).resolve()
        path = Path(raw_path)
        if not path.is_absolute():
            path = project_root / path
        path = path.resolve()
        # Containment guard: the agent supplies the path, so confine reads to the
        # project tree — reject ``..`` escapes / absolute paths outside it.
        if not path.is_relative_to(project_root):
            return _err(
                "invalid_path",
                "netlist_path must stay within the project directory",
            )
        if not path.exists():
            return _err("missing_netlist", f"netlist file not found: {path}")
        netlist = path.read_text()

    from ee_agent_spice import simulate

    project_root = Path(arguments.get("project_path") or Path.cwd())
    return simulate(
        netlist=netlist,
        analysis=analysis,
        params=arguments.get("params") or {},
        probes=arguments.get("probes") or [],
        project_root=project_root,
    )


async def run_pyspice(arguments: dict[str, Any]) -> dict[str, Any]:
    """Async entry called by the registered tool handler."""
    try:
        return await asyncio.to_thread(_run, arguments)
    except ImportError as e:  # PySpice not installed
        return _err("dependency_missing", f"PySpice unavailable: {e}")
    except OSError as e:  # libngspice not loadable
        return _err("dependency_missing", f"libngspice unavailable: {e}")
    except Exception as e:  # noqa: BLE001 - tool must not crash the run
        # Surface only the exception type; the message body may leak paths/internals
        # into the transcript. Full detail to the server log (CODE_AUDIT Q1).
        log.exception("pyspice_run failed")
        return _err("unexpected_error", type(e).__name__)
