# 05 · `pyspice_run` tool spec

> **Replaces the earlier "simulation workflow" doc.** Under Option C, there is no separate workflow — `pyspice_run` is a single tool registered into atopile's `ToolRegistry`. The agent calls it just like it calls `parts_search` or `build_run`. Atopile's runner sequences calls; we don't.

---

## 1. Why this tool exists

Atopile produces a SPICE-compatible netlist at `build/<target>/netlist.cir` as part of every build. It doesn't run analyses on it. This tool consumes that netlist and runs DC, AC, or transient simulations, returning structured results the agent can reason about.

This is one of atopile's documented gaps (`07_ATOPILE_GAPS.md` §2.2). The lighter upstream contribution there is a `sim_export` tool that emits the netlist in multiple SPICE flavors with probe metadata; this tool would consume that. For now we just read the existing netlist directly.

---

## 2. Signature

```python
pyspice_run(
    netlist_path: str,                                # absolute or project-relative path
    analysis: Literal["dc", "ac", "tran"],
    params: dict,                                     # analysis-specific parameters
    probes: list[str],                                # node or device names to record
    project_path: str | None = None,                  # where to write result files
) -> dict
```

### Returns

For small result sets:

```json
{
  "success": true,
  "analysis": "tran",
  "results": [
    {"probe": "VDD_3V3", "summary": {"min": 3.27, "max": 3.34, "mean": 3.30}, "n_samples": 1000},
    {"probe": "i(R1)",   "summary": {"min": 0.001, "max": 0.012, "mean": 0.006}, "n_samples": 1000}
  ],
  "result_file": "build/default/sim/run_a3b1e7.npz",
  "errors": [],
  "duration_ms": 480
}
```

The raw waveforms get written to `result_file` (`.npz`). The agent reads summary stats; if it needs the raw waveform, it can post-process the file in a follow-up step (e.g., via a `python_repl` tool if we later add one, or by inspecting metrics it computes).

For convergence failures or invalid params:

```json
{
  "success": false,
  "analysis": "tran",
  "results": [],
  "errors": [
    {"type": "convergence_failure",
     "rationale": "Newton-Raphson failed at t=2.4e-3 s; gmin stepping suggested",
     "ngspice_log": "..."}
  ],
  "duration_ms": 1200
}
```

---

## 3. Parameter shapes per analysis

### DC operating point

```python
analysis="dc"
params={}                                              # no params for plain op
probes=["VDD_3V3", "vbase", "i(R1)"]
```

For DC sweep:
```python
analysis="dc"
params={"source": "Vin", "start": 0, "stop": 5, "step": 0.1}
probes=["vout"]
```

### AC small-signal

```python
analysis="ac"
params={"variation": "dec",                            # "dec" | "lin" | "oct"
        "n_points": 100,
        "f_start": 1,
        "f_stop": 1e6}
probes=["vout", "vfb"]
```

Results include both magnitude (dB) and phase (degrees).

### Transient

```python
analysis="tran"
params={"t_step": "10us", "t_end": "10ms",
        "uic": False}                                  # use initial conditions
probes=["VDD_3V3", "i(R_LED)"]
```

---

## 4. Implementation

`src/atopile/server/agent/_ee/tools_pyspice.py`:

```python
"""Run PySpice/Ngspice analyses on atopile's netlist output."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

from atopile.server.agent import tools as _atopile_tools

log = logging.getLogger(__name__)


@_atopile_tools._register_tool("pyspice_run")
async def _tool_pyspice_run(
    arguments: dict[str, Any],
    project_root: Path,
    ctx: Any,
) -> dict[str, Any]:
    netlist_path = _resolve_netlist(arguments["netlist_path"], project_root)
    analysis = arguments["analysis"]
    params = arguments.get("params", {})
    probes = arguments.get("probes", [])

    if not netlist_path.exists():
        return {
            "success": False,
            "errors": [{"type": "missing_netlist",
                        "rationale": f"Netlist not found at {netlist_path}"}],
        }

    started = time.monotonic()
    try:
        if analysis == "dc":
            raw = _run_dc(netlist_path, params, probes)
        elif analysis == "ac":
            raw = _run_ac(netlist_path, params, probes)
        elif analysis == "tran":
            raw = _run_tran(netlist_path, params, probes)
        else:
            return {
                "success": False,
                "errors": [{"type": "invalid_analysis",
                            "rationale": f"Unknown analysis {analysis!r}"}],
            }
    except _NgspiceError as exc:
        return {
            "success": False,
            "analysis": analysis,
            "results": [],
            "errors": [{"type": "convergence_failure",
                        "rationale": exc.short_message,
                        "ngspice_log": exc.full_log[-2000:]}],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    # Persist raw data; return summary stats
    result_file = _persist_result(raw, project_root, netlist_path, analysis)
    summary_results = [
        {
            "probe": probe,
            "summary": {
                "min": float(np.min(values)),
                "max": float(np.max(values)),
                "mean": float(np.mean(values)),
            },
            "n_samples": int(len(values)),
        }
        for probe, values in raw.items()
    ]

    return {
        "success": True,
        "analysis": analysis,
        "results": summary_results,
        "result_file": str(result_file.relative_to(project_root)),
        "errors": [],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
```

The DC / AC / transient runners use PySpice's `NgSpiceShared` backend. Implementation details (~250 LoC total) are in the file but elided here for brevity.

---

## 5. Why the result file separation

Returning full waveforms in the tool result inflates the agent's context window dramatically. A 10ms transient at 10µs step is 1000 samples per probe — feasible. A 1s transient at 1µs step is 1,000,000 samples per probe — catastrophic for context.

So:
- **Always persist** raw waveforms to disk under `build/<target>/sim/<run_id>.npz`
- **Return summary stats** (min/max/mean + sample count) in the tool result
- The agent reads summaries by default
- If the agent needs to inspect a specific time range or compute a derived metric (e.g., FFT, settling time), it gets a follow-up tool — see §7 below

---

## 6. Skill / prompt guidance

The agent needs to know when to call `pyspice_run`. We add a section to `.claude/skills/ee-agent/SKILL.md`:

```markdown
## When to run simulation

Run `pyspice_run` when the design has analog behavior whose correctness
isn't obviously implied by part selection:

- Switching regulator stability (run AC small-signal, check phase margin)
- Filter cutoff verification (run AC, check -3dB point)
- Transient response (LDO step response, capacitor charge time)
- Bias point sanity (run DC op, check that BJT/MOSFET bias matches design)

DO NOT run simulation for:
- Pure digital logic (use ato's design_diagnostics instead)
- Mechanical / thermal questions (out of scope)
- "Does this part work?" questions answered by the datasheet (use rag_search)

Always specify probes by node name (e.g., `VDD_3V3`) or device current
(`i(R1)`, `i(M_LDO)`). The netlist is at `build/<target>/netlist.cir`.

After receiving results, compare summary stats to spec. If you need raw
waveforms (rare), inspect `result_file` via post-processing tools.
```

---

## 7. Future extensions

- **Convergence helpers.** If a tran fails convergence, retry with `.options gmin=1e-12` or `.options reltol=1e-3` automatically before reporting failure.
- **Probe expansion.** Allow probe names like `VDD_3V3@0..10ms` to slice time windows in the result summary without round-tripping through the result file.
- **Monte Carlo.** Tolerance analyses across parts. Useful for "robust design" gates. Out of v1 scope.
- **Temperature sweeps.** Run the same analysis at -40, +25, +85 °C. Easy add once base simulation is solid.
- **Result inspection tool.** A separate `sim_inspect(result_file, expr)` tool that evaluates an expression (e.g., `"fft(VDD_3V3)"`, `"max(VDD_3V3) - min(VDD_3V3)"`) against a persisted result. Avoids the agent having to load `.npz` files itself.

---

## 8. Eval ideas

- **Convergence rate.** % of generated netlists that simulate without errors on each analysis type.
- **Accuracy on known designs.** For 5 reference designs with hand-computed expected results, agent's reported summary matches within 5%.
- **Probe selection appropriateness.** Did the agent pick probes that actually tell it whether spec is met? Manual rubric on 10 traces.
- **Result-file size.** Average and p95. If we ever exceed 10MB per result we need to revisit the persistence strategy.

---

## 9. Gotchas

- **Ngspice is finicky.** Some atopile-generated netlists may have device models the bundled ngspice doesn't carry. Document the model libraries we depend on and check them at install time.
- **Probe node naming.** Atopile names nets predictably but synthesized internal nodes (within compound modules) may have machine-generated names. The agent reads the netlist to find the right name; we don't translate.
- **`uic=True` quirks.** Initial-condition transient runs sometimes blow up the bias point. Default `uic=False` and let ngspice compute the op-point first.
- **PySpice version pinning matters.** Some versions don't play nicely with ngspice-side updates. Pin both.
