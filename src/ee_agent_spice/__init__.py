"""EE-agent SPICE runner (Option C, milestone M5).

A framework-agnostic wrapper around ngspice (via PySpice's ``NgSpiceShared``): it takes
an *agent-authored* SPICE netlist body, runs a DC/AC/transient/op analysis, persists raw
waveforms to ``.npz`` and returns compact per-probe summary stats. No PySpice circuit
DSL, no graph translation, no geometry — SPICE is purely topological.

The agent surface (``pyspice_run``) lives in ``atopile.server.agent._ee.tools_pyspice``
and delegates here; this package knows nothing about atopile's agent runner.
"""

from __future__ import annotations

from .errors import NgspiceError
from .runner import simulate

__all__ = ["simulate", "NgspiceError"]
