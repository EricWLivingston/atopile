"""Error types + ngspice-log classification for the SPICE runner.

ngspice reports failures by printing to its log, not by raising. The runner captures the
log and uses :func:`classify_log` to turn it into a structured ``{type, rationale}`` the
agent can reason about (convergence vs. parse vs. unknown) instead of a wall of text.
"""

from __future__ import annotations

import re

_TAIL_CHARS = 2000  # cap the log we hand back so a tool result stays prompt-sized


class NgspiceError(Exception):
    """Raised when ngspice fails to produce results.

    Carries a short, classified message plus the (tail of the) raw ngspice log so the
    wrapper can surface ``{"type", "rationale", "ngspice_log"}`` without re-parsing.
    """

    def __init__(
        self, short_message: str, full_log: str, err_type: str = "ngspice_error"
    ):
        super().__init__(short_message)
        self.short_message = short_message
        self.err_type = err_type
        self.full_log = full_log

    @property
    def log_tail(self) -> str:
        return self.full_log[-_TAIL_CHARS:]


# (regex, error type, rationale) — first match wins; ordered most-specific first.
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"singular matrix|matrix is singular", re.I),
        "convergence_failure",
        "ngspice hit a singular matrix (often a floating node or no path to ground).",
    ),
    (
        re.compile(
            r"no convergence|failed to converge|iteration limit|gmin stepping", re.I
        ),
        "convergence_failure",
        "Newton-Raphson failed to converge; try gmin/source stepping or looser reltol.",
    ),
    (
        re.compile(r"timestep too small|time step too small", re.I),
        "convergence_failure",
        "Transient timestep shrank below the minimum (stiff circuit or discontinuity).",
    ),
    (
        re.compile(
            r"(?:fewer|less) than 2 connections|has no (?:dc path to ground|"
            r"path to ground)|node .*is (?:not connected|floating)|"
            r"no path to ground", re.I
        ),
        "floating_node",
        "A node is floating / has no DC path to ground — every node needs >=2 "
        "connections and a resistive path to node 0; add a load or a high-value "
        "resistor to ground.",
    ),
    (
        re.compile(r"can(?:'|no)?t open|cannot find (?:include|file)|"
                   r"could not open|no such file", re.I),
        "include_error",
        "An .include/.lib file could not be opened — check the path; the bundled "
        "models are auto-included, so a custom .include must point to a real file.",
    ),
    (
        re.compile(r"unknown (?:subckt|model)|unable to find (?:definition|model)|"
                   r"could not find|undefined", re.I),
        "model_error",
        "A referenced model/subckt is undefined — inline a .model/.subckt or check "
        "the name against .include'd libraries (the bundled lib defines Dgen/"
        "Q2N3904/NMOS_GEN etc.).",
    ),
    (
        re.compile(r"unknown (?:parameter|param)|too few parameters|"
                   r"bad parameter|out of range", re.I),
        "param_error",
        "A device/analysis parameter is missing, malformed, or out of range — check "
        "the element line and the analysis params.",
    ),
    (
        re.compile(r"\.ic|initial condition", re.I),
        "ic_error",
        "An initial-condition (.ic) directive failed — name an existing node and pair "
        "transient .ic with `uic` in the analysis params.",
    ),
    (
        re.compile(r"error|syntax|unrecognized|illegal", re.I),
        "parse_error",
        "ngspice rejected the deck — likely a malformed netlist line.",
    ),
]


def classify_log(log: str) -> tuple[str, str]:
    """Return ``(err_type, rationale)`` for an ngspice log; ``unknown`` if no match."""
    for pattern, err_type, rationale in _PATTERNS:
        if pattern.search(log):
            return err_type, rationale
    return "unknown", "ngspice did not return results; see the log for details."
