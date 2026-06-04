# 07 · `ipc_check` and `pinmux_check` tool specs

> **Replaces the earlier "verification workflow" doc.** Under Option C, there is no separate workflow — these are individual tools the agent calls when verification is appropriate. Atopile already runs its own checks (`ato_check`, `design_diagnostics`, `post_solve_checks`) during `build_run`; these two new tools cover gaps atopile doesn't fill.
>
> An optional **meta-tool** `run_verification` is sketched at the end of this doc as a possible future addition — it would sequence atopile's built-in checks plus our two new tools and return aggregated findings. Not in v1 scope, but a clean future hook.

---

## 1. The verification picture

Atopile already does (no new tool needed):
- `ato_check` — build with `--strict` to surface assertion failures
- `design_diagnostics` — silent-failure detection (under-constrained nets, multiple electric references, etc.)
- `post_solve_checks` — built-in net/connection sanity checks during `build_run`
- `report_bom` — full BOM dump as `BOMData`; agent can read directly
- `report_variables` — declared currents, voltages, constraint values

This tool spec covers what atopile doesn't:
- **`ipc_check`** — IPC compliance (trace widths, clearances, current capacity) against the ingested standards corpus
- **`pinmux_check`** — firmware/HW co-design validation (MCU pin capabilities vs declared interface assignments)

ERC/DRC against KiCad is already covered by atopile's `layout_run_drc` (which we get for free from the harness). Thermal, BOM enrichment, and other previously-planned checks are explicitly out of v1 scope (see `08_PROJECT_PLAN.md` §6).

---

## 2. `ipc_check`

### 2.1 Signature

```python
ipc_check(
    project_path: str,
    build_target: str,
    standards: list[str] = ["IPC-2221B", "IPC-2152"],
    ambient_temp_rise_c: float = 10.0,        # IPC trace-width rule input
    copper_weight_oz: float = 1.0,            # outer-layer copper weight
) -> dict
```

### 2.2 Returns

```json
{
  "success": true,
  "standards_used": ["IPC-2221B", "IPC-2152"],
  "findings": [
    {
      "severity": "blocker",
      "check": "trace_width",
      "net": "VBAT",
      "current_a": 1.5,
      "observed_mm": 0.20,
      "required_mm": 0.45,
      "rationale": "VBAT carries 1.5A. IPC-2221B 6.2 requires 0.45mm for 10°C rise, 1oz external copper. Observed 0.20mm.",
      "citation": {
        "standard": "IPC-2221B",
        "clause": "6.2",
        "page": 33,
        "rag_id": "ipc_2221b_chunk_142"
      }
    },
    {
      "severity": "minor",
      "check": "clearance",
      "nets": ["VBAT", "GND"],
      "observed_mm": 0.15,
      "required_mm": 0.20,
      "rationale": "IPC-2221B 6.3 Table 6-1 requires 0.20mm clearance at 12V differential below 30V; observed 0.15mm.",
      "citation": {"standard": "IPC-2221B", "clause": "6.3", "page": 35}
    }
  ],
  "summary": {"blocker": 1, "major": 0, "minor": 1, "info": 0}
}
```

### 2.3 What it actually does

1. **Read declared currents/voltages.** Call `report_variables` to get the resolved net states (currents declared via `assert net.current within ...`, voltages via `ElectricPower.voltage`).
2. **Read actual PCB.** Open the `.kicad_pcb` at `build/<target>/layout.kicad_pcb` using `kipy` or `kicad-cli pcb export ...` to extract per-net trace widths and minimum pairwise clearances.
3. **For each net with declared current:**
   - Look up IPC-2221B 6.2 in the `standards` RAG corpus (or use the cached interpretation table — same numbers, faster).
   - Compute required width given current + ambient temp rise + copper weight.
   - Compare to observed width.
   - Emit finding if observed < required.
4. **For each pair of adjacent nets:**
   - Determine differential voltage between them.
   - Look up IPC-2221B 6.3 Table 6-1 for required clearance.
   - Compare to observed minimum clearance.
   - Emit finding if observed < required.
5. **Optional: IPC-2152 current capacity.** Stricter, more accurate than IPC-2221 for high-current traces. Use when net.current > 1A or user explicitly requests.

### 2.4 Why citations are mandatory

Every finding has a `citation` field pointing to the standards corpus. The agent can quote this in design rationale ("per IPC-2221B §6.2, VBAT requires 0.45mm…"). Findings without citations are demoted to `severity: "info"` and marked `"uncited — needs human review"`.

### 2.5 Severity tiers

- **`blocker`** — design will not pass manufacturing-house inspection or violates safety-critical clearances. Block design release.
- **`major`** — meaningful manufacturability or reliability concern. Should fix before release.
- **`minor`** — best-practice violation, low real-world risk. Note but don't block.
- **`info`** — observation; no action needed.

Initial calibration is conservative (more blockers). We tune down based on real designs.

### 2.6 Implementation

`src/atopile/server/agent/_ee/tools_ipc.py`. ~400 LoC. Key dependencies:

- `kipy` (KiCad python bindings) or `kicad-cli` for PCB introspection
- `ee_agent_rag.retriever` for standards lookups
- A small `ipc_2221_tables.py` with pre-computed lookup tables for the common cases (avoids round-tripping through RAG for trivial lookups)

### 2.7 Skill / prompt guidance

Add to `.claude/skills/ee-agent/SKILL.md`:

```markdown
## When to run ipc_check

Run `ipc_check` after a successful `build_run` produces a layout. Always
include it before declaring the design "done" for any production-bound
work. The check is fast (~5s) and produces structured, cited findings.

If `ipc_check` reports blockers:
1. Read the finding's `citation.clause` to understand the rule.
2. Use `project_edit_file` to widen the offending traces or update
   net classes in the PCB.
3. Rebuild with `build_run`.
4. Re-run `ipc_check`. Iterate until 0 blockers.

For research / prototyping work where the design isn't going to fab,
ipc_check findings can be acknowledged but don't have to be fixed.
```

---

## 3. `pinmux_check`

### 3.1 Signature

```python
pinmux_check(
    project_path: str,
    mcu_designator: str,                      # e.g., "U1"
) -> dict
```

### 3.2 Returns

```json
{
  "success": true,
  "mcu": {
    "designator": "U1",
    "part_family": "STM32G4",
    "specific_part": "STM32G474RCT6",
    "package": "LQFP48"
  },
  "findings": [
    {
      "severity": "blocker",
      "designator": "U1",
      "pin": "PA8",
      "package_pin_number": "29",
      "signal_declared": "pwm_a[0]",
      "signal_capability_required": "TIM3_CH1",
      "actual_capability": "TIM1_CH1, MCO, USART1_CK",
      "rationale": "Pin PA8 is assigned to pwm_a[0] which the .ato file declares as TIM3 capable, but on STM32G474 PA8 only supports TIM1_CH1. Reassign to PB4 (TIM3_CH1) or use TIM1.",
      "citation": {
        "vendor": "ST",
        "document": "STM32G474xx Datasheet (DS12288 rev 8)",
        "table": "Alternate function mapping (Table 12)",
        "page": 56
      }
    }
  ],
  "summary": {"blocker": 1, "major": 0, "minor": 0, "info": 0}
}
```

### 3.3 What it does

1. **Identify the MCU.** Read `report_bom`, find the designator, look up its MPN and package.
2. **Load pinmux table.** For supported MCU families we ship a YAML capability table at `_ee/data/pinmux_<family>.yaml`. For unsupported families, return `success: false` with a clear message.
3. **For each declared interface on the MCU module:**
   - Find the .ato declaration (e.g., `pwm_a[0].line ~ pin.PA8`).
   - Look up PA8's capabilities in the pinmux table.
   - Compare declared signal type (e.g., TIM3) to capabilities.
   - Emit finding if there's a mismatch.

### 3.4 Pinmux table format

`_ee/data/pinmux_stm32g4.yaml`:

```yaml
family: STM32G4
parts:
  STM32G474RCT6:
    package: LQFP48
    pins:
      PA8:
        package_pin: 29
        capabilities:
          - TIM1_CH1
          - MCO
          - USART1_CK
          - LPTIM2_OUT
      PB4:
        package_pin: 40
        capabilities:
          - TIM3_CH1
          - TIM16_CH1N
          - I2C3_SDA
          - JTRST
      # ... full pin table
```

These tables come from vendor datasheets. We ingest them once per MCU family. For v1, scope is one family (STM32G4 or nRF52840).

### 3.5 Vendor coverage strategy

Vendor pinmux is the long tail. Each new MCU family is roughly 1 week of work:
- Parse the datasheet's alternate function table
- Validate against vendor reference designs
- Ship the YAML
- Add to CI eval

We don't try to cover everything. The agent gracefully handles unsupported MCUs:

```json
{
  "success": false,
  "mcu": {"designator": "U1", "part_family": "ESP32-C6"},
  "rationale": "pinmux table for ESP32-C6 not yet available. Skipping pin-capability validation. Add table at src/atopile/server/agent/_ee/data/pinmux_esp32c6.yaml.",
  "findings": []
}
```

### 3.6 Implementation

`src/atopile/server/agent/_ee/tools_pinmux.py`. ~300 LoC. Key dependencies:

- atopile's `report_bom` + `report_variables` for MCU identification + declared pin assignments
- YAML pinmux tables under `_ee/data/`
- atopile's `project_read_file` for reading the `.ato` source where pin assignments live

### 3.7 Skill / prompt guidance

Add to `.claude/skills/ee-agent/SKILL.md`:

```markdown
## When to run pinmux_check

Run `pinmux_check` when an MCU is in the design (any module with declared
package pins). The check catches firmware/HW co-design errors at design
time, before they become "I bricked the board" stories.

Run it after `parts_install` has resolved the MCU's specific MPN — early
runs against a generic "STM32" pick may fail because the tool needs the
specific part to look up its capabilities.

If pinmux_check returns blockers, reassign interfaces to compatible pins
using project_edit_file, then rebuild and re-check.
```

---

## 4. Optional: `run_verification` meta-tool

Not in v1, but the right future addition. A single tool that sequences:

1. `build_run` (idempotent re-build under strict mode)
2. `design_diagnostics`
3. `layout_run_drc` (if a PCB exists)
4. `ipc_check`
5. `pinmux_check` (if MCU present in BOM)
6. Aggregate findings, sort by severity, return summary

Why later, not now: it's premature optimization. The agent in v1 should call each check as it sees fit, with skill guidance steering it. Once we observe that the agent always-or-never runs the same sequence, we collapse it into a meta-tool.

When we add it:

```python
run_verification(
    project_path: str,
    build_target: str,
    skip: list[str] = [],                     # opt out of specific checks
) -> dict
```

Returns aggregated findings across all sub-checks with a single severity rollup.

---

## 5. Eval ideas

For `ipc_check`:
- **False-positive rate** on known-good reference designs (should be near zero for blockers).
- **Catch rate** on intentionally-broken designs (e.g., a design where VBAT is 0.20mm at 1.5A — must catch).
- **Citation accuracy** — given a finding, does its cited clause actually contain the relevant rule? Random spot-check on 20 findings.
- **Standards-revision sensitivity** — IPC-2221A vs IPC-2221B differ slightly; tool should use the cited revision consistently.

For `pinmux_check`:
- **STM32G4 coverage** — for the supported family, 100 valid and 100 invalid assignments, expect 100% correct classification.
- **Graceful failure** for unsupported families.
- **Specific-MPN sensitivity** — STM32G474RC and STM32G474RB share a package but differ in some peripherals; verify the table distinguishes them.

---

## 6. Gotchas

### `ipc_check`
- **KiCad PCB introspection requires KiCad installed.** Document this; CI runs the tests inside a KiCad-bearing container.
- **Net classes are the recommended path for trace-width specs.** If the design doesn't set them, `ipc_check` falls back to per-trace measurement; this is slower and noisier.
- **Clearance computation is O(n²) in net count.** For boards with 500+ nets this gets slow. We can spatial-index later if it matters.
- **IPC-2152 vs IPC-2221 disagreement.** IPC-2152 gives smaller required widths than IPC-2221 for the same current. By default we use IPC-2221 (conservative). If the user explicitly requests IPC-2152 (e.g., `standards=["IPC-2152"]`), we use it instead.

### `pinmux_check`
- **`.ato` syntax for pin assignment varies.** Some packages use `interface.signal ~ pin.PA8`, others use `interface ~ package.PA8`. The tool needs to handle both — and possibly future variants. We use atopile's parser via `project_read_file` rather than regex.
- **Multi-port peripherals.** Some pins support multiple TIM channels (e.g., PA8 → TIM1_CH1 *and* TIM1_BKIN2). The table has to capture all capabilities for a pin, and matching has to permit any.
- **MCU revision differences.** STM32G474xx datasheet rev 7 vs rev 8 had small pinmux corrections. Tag the table with the datasheet revision used.
- **The agent may want to use this proactively.** Add a skill hint: "if you're about to assign an MCU pin to an interface, you can call `pinmux_check` first to verify the assignment is valid before writing the `.ato`."
