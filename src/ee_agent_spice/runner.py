"""SPICE deck assembly + ngspice execution (the core of M5).

Agent-authored model: the agent passes a SPICE *circuit body* (device lines, plus any
``.model``/``.subckt``/``.param`` it wants); this module wraps it into a runnable deck:
a title line, an auto-``.include`` of the bundled device library, the analysis control
card built from ``params``, ``.save`` cards for ``probes``, and ``.end`` — then drives
ngspice via PySpice's ``NgSpiceShared.load_circuit``/``run`` (no DSL, no geometry).

SPICE is purely topological: nodes are bare labels (``0`` is ground). Result vectors
come back keyed by node name (voltage) or ``<src>#branch`` (source current);
:func:`_resolve` maps the agent's probe spelling (``out``/``v(out)``/``i(v1)``) to them.
"""

from __future__ import annotations

import ctypes.util
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .errors import NgspiceError, classify_log
from .persist import persist, summarise

# Bundled model library the runner always makes available to the deck.
DEFAULT_MODELS = Path(__file__).parent / "models" / "ee_agent.lib"

# Independent-axis vector name ngspice uses per analysis (persisted, never summarised).
_AXIS = {"tran": "time", "ac": "frequency", "dc": "v-sweep"}


# --------------------------------------------------------------------------------------
# Deck assembly
# --------------------------------------------------------------------------------------
def _strip_terminators(body: str) -> str:
    """Drop any trailing ``.end``/``.endc`` the agent added; we own the terminator."""
    terminators = (".end", ".endc")
    lines = [ln for ln in body.splitlines() if ln.strip().lower() not in terminators]
    return "\n".join(lines).strip()


_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# A SPICE number: optional sign, mantissa, optional exponent, optional engineering
# scale/unit suffix (k, meg, u, ms, ...). The crucial bit is the anchored ^...$ with no
# whitespace/newline class: it accepts "10us"/"4.7k"/"1e3" but rejects anything carrying
# a space or newline that could smuggle a second directive into the control card.
_SPICE_NUM_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[A-Za-z%]*$")
_AC_VARIATIONS = frozenset({"dec", "oct", "lin"})


def _num(params: dict[str, Any], key: str) -> str:
    """Return ``params[key]`` as a validated SPICE number string, or raise.

    Control-card fields are interpolated straight into the deck, so a malformed value
    could break it or inject a directive. Accepts finite numbers and SPICE-number
    strings (engineering suffixes like ``10us``/``4.7k``); rejects anything else,
    including values with whitespace/newlines (CODE_AUDIT B3)."""
    if key not in params:
        raise KeyError(key)  # caller maps this to a "missing required param" failure
    val = params[key]
    if isinstance(val, bool):  # bool is an int subclass — never a valid sweep value
        raise NgspiceError(f"param {key!r} must be numeric", "", "invalid_params")
    if isinstance(val, (int, float)):
        if not math.isfinite(val):
            raise NgspiceError(f"param {key!r} must be finite", "", "invalid_params")
        return str(val).strip()
    if not _SPICE_NUM_RE.match(str(val).strip()):
        raise NgspiceError(
            f"param {key!r} must be a number (optionally with a SPICE unit suffix), "
            f"got {val!r}",
            "",
            "invalid_params",
        )
    return str(val).strip()


def _ident(value: Any, what: str) -> str:
    """Validate a SPICE identifier (source/element name) — no spaces/newlines that could
    smuggle extra directives into the control card."""
    s = str(value).strip()
    if not _IDENT_RE.match(s):
        raise NgspiceError(
            f"{what} must be a simple identifier, got {value!r}", "", "invalid_params"
        )
    return s


def _control_card(analysis: str, params: dict[str, Any]) -> str:
    """Build the analysis dot-card from structured params; raises on missing/invalid."""
    if analysis == "op":
        return ".op"
    if analysis == "dc":
        src = params.get("source")
        if not src:  # a DC analysis with no sweep source is just an operating point
            return ".op"
        src = _ident(src, "dc sweep source")
        return f".dc {src} {_num(params, 'start')} {_num(params, 'stop')} " + _num(
            params, "step"
        )
    if analysis == "ac":
        variation = str(params.get("variation", "dec")).strip().lower()
        if variation not in _AC_VARIATIONS:
            raise NgspiceError(
                f"ac variation must be one of {sorted(_AC_VARIATIONS)}, "
                f"got {params.get('variation')!r}",
                "",
                "invalid_params",
            )
        n, f0, f1 = (
            _num(params, "n_points"),
            _num(params, "f_start"),
            _num(params, "f_stop"),
        )
        return f".ac {variation} {n} {f0} {f1}"
    if analysis == "tran":
        card = f".tran {_num(params, 't_step')} {_num(params, 't_end')}"
        if params.get("t_start") is not None:
            card += f" {_num(params, 't_start')}"
        if params.get("uic"):
            card += " uic"
        return card
    raise NgspiceError(f"Unknown analysis {analysis!r}", "", "invalid_analysis")


def _save_target(probe: str) -> str:
    """`out` -> `v(out)`; pass through anything already in functional form (`i(v1)`)."""
    p = probe.strip()
    return p if "(" in p or "#" in p else f"v({p})"


def build_deck(
    netlist: str,
    analysis: str,
    params: dict[str, Any],
    probes: list[str],
    models_path: Path | None = DEFAULT_MODELS,
) -> str:
    """Assemble the full runnable ngspice deck from an agent-authored circuit body."""
    lines = ["* ee_agent_spice generated deck", _strip_terminators(netlist)]
    if models_path is not None:
        lines.append(f".include {models_path}")
    if probes:
        lines.append(".save " + " ".join(_save_target(p) for p in probes))
    lines.append(_control_card(analysis, params))
    lines.append(".end")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Probe resolution (agent spelling -> ngspice vector key)
# --------------------------------------------------------------------------------------
def _resolve(probe: str, keys: list[str]) -> str | None:
    keymap = {k.lower(): k for k in keys}
    p = probe.strip().lower()
    if p in keymap:
        return keymap[p]
    if m := re.fullmatch(r"v\((.+)\)", p):  # v(out) -> out
        if m.group(1) in keymap:
            return keymap[m.group(1)]
    if m := re.fullmatch(r"i\((.+)\)", p):  # i(v1) -> v1#branch
        if (cand := f"{m.group(1)}#branch") in keymap:
            return keymap[cand]
    return None


# --------------------------------------------------------------------------------------
# ngspice execution
# --------------------------------------------------------------------------------------
# ngspice's C core is a process-global singleton and not thread-safe, and PySpice needs
# a distinctly-named shared lib per *simultaneous* instance (libngspiceN.dylib). So we
# keep ONE instance (id 0 -> plain libngspice) behind a lock and reset it between runs,
# rather than minting a new instance per call.
_LOCK = threading.Lock()
_SHARED: Any = None

# Wall-clock budget for a single ngspice run. A non-converging/stiff deck can otherwise
# spin forever inside the C core, wedging the worker and (via the singleton lock) every
# later sim. We bound the run AND the lock wait so the agent always gets control back
# with a structured `timeout` error instead of hanging (CODE_AUDIT B2). The pyspice_run
# skill tells the agent to keep t_end/n_points modest so legitimate runs finish well
# under this.
_SIM_TIMEOUT_S = float(os.environ.get("EE_SPICE_TIMEOUT_S", "30"))


def _ngspice_library_path() -> str | None:
    """Locate libngspice as an absolute path (brew dirs aren't on the dyld path).

    Returns a PySpice ``LIBRARY_PATH`` template (with a ``{}`` instance-id slot), or
    ``None`` to fall back to PySpice's own ``find_library`` resolution (e.g. on Linux).
    """
    if override := os.environ.get("EE_SPICE_NGSPICE_LIB"):
        return override
    suffixes = (".dylib", ".so")
    search_dirs = ["/opt/homebrew/lib", "/usr/local/lib", "/usr/lib", "/usr/lib64"]
    if prefix := os.environ.get("HOMEBREW_PREFIX"):
        search_dirs.insert(0, str(Path(prefix) / "lib"))
    for directory in search_dirs:
        for suffix in suffixes:
            if (Path(directory) / f"libngspice{suffix}").exists():
                return str(Path(directory) / f"libngspice{{}}{suffix}")
    if ctypes.util.find_library("ngspice"):
        return None  # PySpice's default resolution will find it
    return None


def _shared():
    """The locked singleton capturing NgSpiceShared (created on first use)."""
    global _SHARED
    if _SHARED is not None:
        return _SHARED
    from PySpice.Spice.NgSpice.Shared import NgSpiceShared

    if (path := _ngspice_library_path()) is not None:
        NgSpiceShared.LIBRARY_PATH = path

    class _Capturing(NgSpiceShared):
        def __init__(self, **kwargs):
            self._log: list[str] = []
            super().__init__(**kwargs)

        def send_char(self, message, ngspice_id):  # ngspice stdout/stderr lines
            self._log.append(message)
            return 0

    _SHARED = _Capturing.new_instance()
    return _SHARED


def _latest_plot_name(plot_names: list[str]) -> str | None:
    """Newest non-``const`` plot (ngspice lists current/newest first)."""
    for name in plot_names:
        if not name.startswith("const"):
            return name
    return None


def _load_and_run(ng: Any, deck: str) -> None:
    ng.load_circuit(deck)
    ng.run()


def _run_ngspice(deck: str, analysis: str) -> tuple[dict[str, np.ndarray], str, str]:
    """Run the deck on the locked singleton; return (vectors, log, plot) or raise.

    Bounded by ``_SIM_TIMEOUT_S`` on both the lock wait and the run itself so a hung
    deck can never block the worker indefinitely.
    """
    if not _LOCK.acquire(timeout=_SIM_TIMEOUT_S):
        raise NgspiceError(
            "ngspice is busy with another simulation that exceeded its time budget; "
            "try again shortly.",
            "",
            "timeout",
        )
    timed_out = False
    try:
        ng = _shared()
        ng._log = []  # reset capture for this run
        try:
            # Run in a watchdog thread so a non-converging deck times out instead of
            # hanging. ngspice's C core can't be force-killed mid-call, so on timeout we
            # surface the error and let the (daemon) thread unwind on its own.
            worker_exc: dict[str, BaseException] = {}

            def _target() -> None:
                try:
                    _load_and_run(ng, deck)
                except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
                    worker_exc["e"] = exc

            t = threading.Thread(target=_target, daemon=True)
            t.start()
            t.join(_SIM_TIMEOUT_S)
            if t.is_alive():
                timed_out = True
                raise NgspiceError(
                    f"ngspice exceeded the {_SIM_TIMEOUT_S:.0f}s time budget — reduce "
                    "t_end/n_points or simplify the circuit.",
                    "".join(ng._log),
                    "timeout",
                )
            if (exc := worker_exc.get("e")) is not None:
                if isinstance(exc, NgspiceError):
                    raise exc
                log = "".join(ng._log)
                err_type, rationale = classify_log(log)
                raise NgspiceError(rationale or str(exc), log, err_type) from exc
            log = "".join(ng._log)
            plot_name = _latest_plot_name(ng.plot_names)
            if plot_name is None:
                err_type, rationale = classify_log(log)
                raise NgspiceError(rationale, log, err_type)
            plot = ng.plot(None, plot_name)
            vectors = {k: np.asarray(plot[k]._data) for k in plot.keys()}
            if not vectors or all(v.size == 0 for v in vectors.values()):
                err_type, rationale = classify_log(log)
                raise NgspiceError(rationale, log, err_type)
            return vectors, log, plot_name
        finally:
            # Don't touch the singleton while a timed-out worker may still be inside the
            # C core (concurrent access corrupts state); cleanup on the next successful
            # run handles cleanup. Otherwise free plots so memory doesn't grow.
            if not timed_out:
                try:
                    ng.exec_command("destroy all")
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
    finally:
        _LOCK.release()


# --------------------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------------------
def simulate(
    netlist: str,
    analysis: str,
    params: dict[str, Any] | None = None,
    probes: list[str] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Run one analysis on an agent-authored netlist and return a structured result.

    Persists all raw vectors (probes + independent axis) to an ``.npz`` and returns only
    per-probe summary stats, so the agent never sees megabyte waveforms
    (``02_SIMULATION`` §5). Circuit-level failures come back as ``success: False``, not
    as exceptions.
    """
    params = params or {}
    probes = probes or []
    project_root = Path(project_root) if project_root else Path.cwd()
    started = time.monotonic()

    try:
        deck = build_deck(netlist, analysis, params, probes)
    except NgspiceError as exc:
        return _failure(analysis, exc, started)
    except KeyError as exc:
        return _failure(
            analysis,
            NgspiceError(f"missing required param {exc}", "", "invalid_params"),
            started,
        )

    try:
        vectors, log, _ = _run_ngspice(deck, analysis)
    except NgspiceError as exc:
        return _failure(analysis, exc, started, deck=deck)

    # Resolve requested probes; default to all node vectors if none requested.
    keys = list(vectors.keys())
    axis_key = _AXIS.get(analysis)
    if probes:
        resolved = {p: _resolve(p, keys) for p in probes}
        probe_vectors = {p: vectors[k] for p, k in resolved.items() if k is not None}
        missing = [p for p, k in resolved.items() if k is None]
    else:
        probe_vectors = {k: vectors[k] for k in keys if k != axis_key}
        missing = []

    # Persist everything (probes + axis) for post-processing; summarise probes only.
    to_persist = dict(probe_vectors)
    if axis_key and axis_key in vectors:
        to_persist[axis_key] = vectors[axis_key]
    result_file = persist(to_persist, project_root, deck, analysis)

    return {
        "success": True,
        "analysis": analysis,
        "results": summarise(probe_vectors),
        "result_file": _rel(result_file, project_root),
        "missing_probes": missing,
        "errors": [],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _failure(
    analysis: str, exc: NgspiceError, started: float, deck: str | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"type": exc.err_type, "rationale": exc.short_message}
    if exc.full_log:
        err["ngspice_log"] = exc.log_tail
    return {
        "success": False,
        "analysis": analysis,
        "results": [],
        "errors": [err],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
