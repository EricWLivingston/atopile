---
name: pyspice_run
description: "When and how to use the pyspice_run tool: simulate only analog subcircuits with a definable spec, author a minimal SPICE netlist, pick probes, read summary stats. Read before your first simulation in a session."
---

# pyspice_run — SPICE simulation guidance

`pyspice_run` runs an ngspice DC/AC/transient/operating-point analysis on a **SPICE netlist
you author** and returns per-probe summary stats (min/max/mean). It exists to verify analog
behaviour whose correctness isn't obvious from part selection alone.

It is **not** a build step. A failed or wrong simulation never breaks `ato build` and never
crashes the run — it returns `{"success": false, "errors": [...]}`, which is just feedback.
The only real cost of misuse is wasted tokens/turns, so the discipline below is about not
simulating things that don't need it.

## When to use

Simulate when an analog subcircuit has a **definable spec** you can check a number against:

- Oscillator / timer frequency (astable, relaxation, RC oscillator) → `tran`
- Switching-regulator or LDO transient / step response stays in spec → `tran`
- Filter cutoff or response (−3 dB point, passband gain, roll-off) → `ac`
- Bias point sanity (BJT/MOSFET operating point, divider voltage) → `op`
- DC transfer / sweep (transfer curve, threshold) → `dc`

## When NOT to use (use the right tool instead)

- **Purely-digital logic** (buses, MCU I/O, logic-level interconnect) → there's nothing
  analog to solve. Use `design_diagnostics`.
- **A spec the datasheet already states** (quiescent current, dropout, absolute max, typical
  R_θJA) → don't simulate it, look it up with `rag_search`.
- **Mechanical / thermal / EMC** questions → out of scope.
- **The whole board.** Never dump the full netlist into ngspice. Most parts (MCUs, sensors,
  connectors) have no SPICE model and will fail or be meaningless.

If you can't name the analog quantity and the pass/fail threshold, don't simulate.

## Scope: the minimal subcircuit

Author the **smallest subcircuit that answers the question**, not the design. Replace the
surrounding board with ideal sources and loads:

- Model the supply as a `V` source; model a downstream block as a resistor/current load.
- Include only the components that shape the behaviour you're checking.
- Give nodes short, readable names (`vin`, `vout`, `fb`); node `0` is ground (required).

## How to author the netlist

Pass only the **circuit body** in `netlist` — device lines plus any `.model`/`.subckt`/
`.param` you need. **Do not** add `.tran`/`.ac`/`.op`/`.dc` or `.end`; those are generated
from `analysis` + `params`. Example body:

```
V1 vcc 0 DC 5
R1 vcc out 1k
C1 out 0 1u
```

### Bundled models (reference by name, no .include needed)

- Diodes: `Dgen` (silicon ~0.7 V), `Dschottky` (~0.3 V), `DLED` (~1.8 V)
- BJTs: `Q2N3904` (NPN), `Q2N3906` (PNP)
- MOSFETs: `NMOS_GEN` (Vth ~2 V), `PMOS_GEN` (Vth ~−2 V)
- Op-amps: `OPAMP_IDEAL` (subckt — instantiate with `X1 inp inn out OPAMP_IDEAL`;
  single-pole, A0 100k / GBW ~1 MHz, Rin 10 Meg, Rout 10. **No supply rails — the
  output never clips**; for comparator/railing behaviour inline a vendor `.subckt`
  or clamp the output)

These are first-order/topology-accurate, not vendor-accurate. For an accurate part, inline a
vendor `.model` line (find it via `rag_search`/datasheet) in your netlist body.

### params per analysis

- `op`: `{}` (operating point)
- `dc`: `{"source": "V1", "start": 0, "stop": 5, "step": 0.1}`
- `ac`: `{"variation": "dec", "n_points": 100, "f_start": 1, "f_stop": 1e6}`
- `tran`: `{"t_step": "10us", "t_end": "10ms", "uic": false}`

Numeric params accept SPICE engineering suffixes (`10us`, `4.7k`, `1e6`) but must be plain
values — no spaces or extra tokens. **Each run has a ~30 s wall-clock budget** (overridable
via `EE_SPICE_TIMEOUT_S`); pick a `t_end`/`n_points` that finishes well under it. A run that
exceeds it returns `{success:false, errors:[{type:"timeout"}]}` — shorten the sweep
(`t_end`), coarsen `t_step`, or simplify the circuit rather than retrying as-is.

### probes

List nodes/currents to record: a node voltage as `out` or `v(out)`, a source current as
`i(v1)`. Empty = all nodes. Probe by the node names you wrote in the netlist.

## Reading results

You get `results: [{probe, summary:{min,max,mean}, n_samples}]`. Raw waveforms are written to
`result_file` (`.npz`) — you normally only need the summary. Compare the summary to your spec
(e.g. transient `max`/`min` within tolerance; for an oscillator, derive frequency from the
persisted waveform if you need it). `missing_probes` lists any probe name that didn't resolve.

## Common pitfalls

- **Oscillators may not start in SPICE** without a nudge. Add a small asymmetry (e.g. one
  timing cap 5% different) and set `params.uic = true`.
- **Singular matrix / no convergence** usually means a floating node or no DC path to
  ground — every node needs a DC path to `0`. The error comes back classified, not as a
  crash.
- **Don't over-resolve the timestep.** A 1 s transient at 1 µs is 1e6 points; keep `t_step`
  coarse enough for the feature you're measuring.
- **Vendor accuracy:** the bundled models verify topology/behaviour, not exact specs. Inline
  a real `.model` when the number must match a datasheet.

## Worked example — verify a ~1 Hz astable oscillator

```
netlist:
  V1 vcc 0 DC 5
  RC1 vcc c1 1k
  RC2 vcc c2 1k
  RB1 vcc b1 68k
  RB2 vcc b2 68k
  Q1 c1 b1 0 Q2N3904
  Q2 c2 b2 0 Q2N3904
  C1 c1 b2 10u
  C2 c2 b1 10.5u        ; slight asymmetry to start oscillation
analysis: tran
params: {"t_step": "1ms", "t_end": "4s", "uic": true}
probes: ["c1"]
```

Then derive the frequency from the `c1` waveform and check it against the spec band.
