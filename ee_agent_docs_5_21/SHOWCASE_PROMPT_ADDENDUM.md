# EE-Agent Showcase — Hero Prompt Addendum (12 V buck + RS-485)

Run this **after** the hero prompt (`SHOWCASE_PROMPT.md`) has produced a clean build, with
`examples/dac-buffer-demo` open as the workspace folder. It extends the board the agent
already authored (`dac-buffer.ato`, module `App`) with two additions, exercising the same
fork features (RAG research, parts install, schematic emit, skill discovery) under dynamic
model routing.

**Precondition:** the **TPS563201** and **MaxLinear SP3485** datasheets are ingested into
the RAG corpus (`python -m ee_agent_rag.ingest data/datasheets/*.pdf`), so the agent can
research them. Smoke-check with a `rag_search` for "TPS563201 feedback voltage" /
"SP3485 supply" before the run, and reload the VS Code window so the new corpus is picked up.

---

## Prompt (copy from here)

> Extend the DAC output-buffer board you already built (`dac-buffer.ato`, module `App`)
> with two additions. Keep the existing design working; explain your reasoning as you go.
>
> **Additions**
> - **Front-end power:** make the board input **+12 V** and step it down to the existing
>   **+5 V** rail with a **TPS563201 synchronous buck converter**. The buck's +5 V output
>   feeds the existing reverse-protection → LDO chain (everything downstream is unchanged).
> - **RS-485 interface:** add a **MaxLinear SP3485 half-duplex RS-485 transceiver**,
>   powered from the **3.3 V** rail. Expose its logic side (driver input / receiver output /
>   enable) at the board level, and bring its **A/B differential pair out to an output
>   connector**.
>
> **Work method**
> 1. **Research the corpus first** with `rag_search` before choosing values: for the
>    **TPS563201**, the **feedback reference voltage and how to set the output to 5 V**
>    (feedback divider) plus the recommended **inductor and input/output capacitors**; for
>    the **SP3485**, the **supply voltage and bypass cap**, how the **driver/receiver enable
>    pins** are used, and any **A/B bus termination** guidance. **Cite** the part + section
>    for each value you adopt. If you're unsure how to use a tool, read its skill with
>    `skill_read` first.
> 2. **Install the new parts** you need (the TPS563201, SP3485, the buck inductor, and an
>    output connector for the A/B pair). Choose the supporting passives from constraints and
>    let them **auto-pick** — don't hand-pin MPNs or LCSC IDs on passives.
> 3. **Build** the project (`build_run`) so the schematic and BOM regenerate cleanly.
> 4. **Report a final summary**: the buck's **feedback-divider values and resulting Vout**,
>    the SP3485 **supply rail and how A/B reach the connector**, the ERC result, and the
>    list of **sources you cited**.
>
> Use the stdlib `RS485HalfDuplex` / `DifferentialPair` interfaces for the bus and the
> `Inductor` primitive for the buck. Keep constraints loose (intervals, no footprint-name
> `.package`, no hand-pinned LCSC IDs on passives) so the picker resolves real in-stock parts.

---

## Why this wording (tuning notes)

- **Two additions on a known-good board** → bounded scope. The board already builds, so the
  agent is editing a working design rather than synthesizing one from scratch — this avoids
  the over-constrained / non-converging failure modes documented in the passdown.
- **"research the corpus first … cite"** → drives `rag_search` against the freshly-ingested
  TPS563201 + SP3485 datasheets. The buck **feedback divider** (Vref → R1/R2 for 5 V), the
  RS-485 **A/B termination**, and the transceiver **bypass** are concrete, citable values —
  good retrieval targets that gate real design decisions.
- **No SPICE requirement (on purpose).** A switching buck isn't meaningful to simulate with
  the bundled ideal SPICE models, and the Sallen-Key already exercised `pyspice_run` in the
  hero run — forcing a sim here would only add failure surface.
- **Explicit nudge to `RS485HalfDuplex` / `DifferentialPair` / `Inductor`** + **"auto-pick /
  loose constraints"** → steers toward the stdlib interfaces and the parametric picker, away
  from the over-spec build errors (exact-value picks, footprint-name `.package`, hand-pinned
  LCSC IDs) called out in the `ato` skill.
- **"install the new parts"** → exercises `parts_install` for the two ICs, the inductor, and
  a connector (the stdlib has no connector/header primitive), the same path the hero run
  used for the LP5907 / MCP4728 / TLV9002.
- **"report a final summary"** → clean `run_completed` plus a human-readable recap to
  eyeball against the logs.

## What it adds to the showcase

On top of the hero run, this exercises `rag_search`, skill discovery, `parts_install`,
schematic emit, and BOM regeneration again — under dynamic routing — on a more complex board
(12 V front-end buck + RS-485 transceiver to a connector). The existing
`scorecard.py` still applies for the generic checks (provider, routing, rag, skills,
schematic, diode, run-health); it does not add buck/RS-485-specific checks. Eyeball the new
parts in the regenerated `.kicad_sch` and BOM:

- the **TPS563201** with its inductor + feedback divider + in/out caps,
- the **SP3485** powered from 3V3 with A/B routed to the installed connector.
