# EE-Agent Showcase — Hero Prompt

Paste the block below into the **atopile extension Agent panel** with
`examples/dac-buffer-demo` open as the workspace folder. It is written to make the
agent naturally exercise every fork feature (RAG research, SPICE simulation, diode
auto-pick, schematic emit, skill discovery) under dynamic model routing.

See `SHOWCASE_README.md` for preconditions and how to score the run afterward.

---

## Prompt (copy from here)

> Design a **precision DAC output-buffer board** in this project (`dac-buffer.ato`,
> module `App`). Build it carefully and in this order; explain your reasoning as you go.
>
> **Requirements**
> - Input is a nominal **5 V** supply. Protect against reverse polarity with a **Schottky
>   diode** in series — define it from constraints (forward voltage and reverse working
>   voltage) and let the part be **auto-picked**, do not hand-pick an MPN.
> - Regulate the protected input down to a clean **3.3 V** rail using an **LDO suited to
>   low-noise analog supplies**.
> - Add a **quad 12-bit I²C DAC that has an internal voltage reference**.
> - Buffer one DAC output through a **second-order (Sallen-Key) low-pass reconstruction
>   filter with a cutoff near 1 kHz**, built on a **rail-to-rail op-amp**. Pick real,
>   in-stock passives for the filter and for all bypass/output capacitors.
>
> **Work method (do these — don't skip):**
> 1. **Research the corpus first** with `rag_search` before choosing values. Specifically
>    look up: the LDO's required **input/output bypass capacitors for stability** and its
>    **noise-reduction / bypass pin**; the DAC's **internal reference voltage and how LDAC
>    updates the outputs**; and the op-amp's **capacitive-load handling and filter
>    guidance**. **Cite** the source (part/app-note + section) for each value you adopt.
>    If you're unsure how to use a tool, read its skill with `skill_read` first.
> 2. **Simulate** the Sallen-Key low-pass with `pyspice_run` before you commit component
>    values: confirm the **DC gain is ≈ unity** and the **AC −3 dB cutoff is near 1 kHz**.
>    Adjust R/C values if the simulated cutoff is off, then re-simulate.
> 3. **Build** the project (`build_run`) so the **schematic and BOM are generated**.
> 4. **Report a final summary**: the ERC result, the **auto-picked diode** (type + MPN +
>    designator), the **simulated −3 dB cutoff**, and the list of cited sources you used.

---

## Why this wording (tuning notes)

- **"auto-picked, do not hand-pick"** + constraint-only diode → forces the diode picker
  path (the ghost-component fix), provable in the BOM as `source: "picked"`.
- **"research the corpus first … cite"** + **"read its skill with `skill_read`"** →
  triggers `rag_search` and `skills_list`/`skill_read` early; the parts named loosely
  (not by MPN) make RAG do real retrieval against `SPX3819`, `MCP4728`, `lmv324`
  datasheets and the `slva079_ldo` / `high_precision_for_dac_buffering` app notes.
- **"simulate … before you commit … re-simulate"** → forces at least one (often two)
  `pyspice_run` calls and writes a `.npz`.
- **multi-phase, reasoning-heavy** → the dynamic router should spread turns across
  Haiku (trivial), Sonnet (design), and Opus (hard topology) tiers.
- **"report a final summary"** → clean `run_completed`, and gives a human-readable
  recap that the scorecard cross-checks against the logs.
