# 12 · atopile Gaps & Iteration Opportunities

> **Purpose.** Atopile is dense and well-engineered, but the EE agent's scope is broader than atopile's. This doc catalogs where atopile stops, where our work begins, and which gaps are good candidates for **upstream contribution** (PRs to atopile/atopile) versus **downstream additions** in the EE agent's harness only.

---

## 1. Framing

We are not modifying atopile. We are using it as the hardware design backbone and contributing improvements upstream when they make sense for the broader community. Anything that is *EE-agent-specific* (orchestration, RAG, qualified parts, simulation, IPC, thermal, cross-domain) lives in our harness; anything that is *atopile-general* (gaps in the DSL/compiler/MCP) is a candidate PR.

The classification matrix:

| Gap | Where it lives | Why |
|---|---|---|
| **Upstream (PR to atopile)** | atopile/atopile | Generally useful to all atopile users; small surface area; aligned with atopile's mission |
| **Downstream (EE agent harness)** | `src/ee_agent/...` | Specific to AI-agent workflows; brings in external services; opinionated for our use case |
| **Joint** | Both | Upstream adds the hook, downstream adds the data/integration |

---

## 2. Upstream gaps — strong PR candidates

### 2.1 MCP exposes only 9 of 40+ agent tools

**Status.** `src/atopile/mcp/tools/` registers 9 tools (`build_project`, `search_and_install_jlcpcb_part`, `install_package`, `find_packages`, `inspect_package`, `get_library_modules_or_interfaces`, `inspect_library_module_or_interface`, `find_project_from_filepath`, `verify_package`). The richer 40-tool palette lives in `src/atopile/server/agent/` and is only reachable through the FastAPI build server + OpenAI agent runner.

**The gap.** External agents (us, Cursor, Claude Desktop) that connect via MCP can't access:
- `stdlib_list`, `stdlib_get_item` — full library introspection
- `examples_list`, `examples_search`, `examples_read_ato` — canonical patterns
- `package_ato_list`, `package_ato_search`, `package_ato_read` — read installed packages
- `report_bom`, `report_variables` — structured build outputs
- `design_diagnostics` — silent-failure detection
- `build_logs_search` — filtered build log query
- `parts_search` (the agent version, broader than `search_and_install_jlcpcb_part`)
- `workspace_list_targets`
- `layout_*` family

**Why PR.** Trivial — each tool is already a pure Python function in `tools.py`/`tool_definitions_project.py`. Wrapping them with `@mcp_tools.register()` is mechanical. Benefits every MCP client.

**Risk.** Low. Existing MCP surface stays unchanged.

**Estimated effort.** 1–2 days. Includes test coverage in `test/mcp/`.

**Our workaround until then.** Embed atopile as a uv dep and call the tool functions directly in-process. See `06_ATOPILE_INTEGRATION.md` §3.

---

### 2.2 No simulation integration

**Status.** `ato build` produces a SPICE-compatible netlist (`build/<target>/netlist.cir`) but doesn't run any simulation.

**The gap.** Modern EE workflows need DC operating point, AC small-signal, and transient analyses at minimum. The agent should be able to ask "does this LDO meet PSRR > 60 dB at 1 kHz?" and get a real answer, not just a constraint that the picker satisfied.

**Why PR (cautiously).** Aligns with atopile's mission ("Write hardware like software" implies "test hardware like software"). But it pulls in PySpice + ngspice dependencies, which atopile has so far avoided.

**Alternative upstream-light proposal.** Add a `sim_export(target, format)` tool that emits the netlist in standardized SPICE flavors (ngspice, LTspice, PySpice) with metadata about probes/measurements declared in `.ato`. Leave the actual simulation run to external tools. This is the *minimal* upstream contribution and still unblocks our PySpice integration significantly.

**Our work regardless.** PySpice node in `simulation-workflow` reads `build/<target>/netlist.cir` and runs analyses. The schematic agent can annotate `.ato` modules with `sim_probe` comments that the agent reads, but atopile won't natively parse them.

**Estimated effort.** Upstream-light (`sim_export` only): 3–5 days. Full upstream (run PySpice in `ato build sim`): 2–3 weeks.

---

### 2.3 Picker is JLCPCB-only

**Status.** `parts_search` and `parts_install` hit JLCPCB's catalog via atopile's Components API backend. No support for AEC-Q (automotive), MIL-spec, ITAR, or specific distributor-only parts (Mouser-only, DigiKey-only, Arrow-only, Newark-only).

**The gap.** Industrial / automotive / aerospace customers can't use atopile for production designs without qualification metadata.

**Why PR.** Architecturally clean — the picker already has a `PickSupplier` ABC in `src/faebryk/libs/picker/picker.py`. Additional backends slot in. The community contribution is the JLCPCB-equivalent ingest for other distributors.

**Risk.** Higher than 2.1. Pricing data licenses vary by distributor. Octopart has TOS restrictions on caching that need legal review.

**Alternative downstream.** EE-agent-only `parts-agent` keeps Octopart + DigiKey + Mouser + jlcparts fallbacks. atopile picks commodity passives; we pick qualified ICs. This is current plan.

**Estimated effort.** Upstream: 3–4 weeks per additional backend. Downstream supplement: already in our roadmap.

---

### 2.4 No IPC standards integration

**Status.** `design_diagnostics` runs internal atopile checks (constraint satisfaction, missing references, single-electric-reference violations). It does not run IPC-2221 trace-width sanity checks, IPC-7351 land-pattern verification, or any standards-bound design rules.

**The gap.** Manufacturing-readiness signoff requires IPC compliance. Currently the EE agent runs these checks externally against the standards RAG corpus.

**Why partial PR.** atopile is a *design capture* tool, not a *manufacturing signoff* tool — by design. But it could expose hooks: when the schematic agent declares `power.max_current = 10A`, an external IPC checker should be able to read that and compute minimum trace width.

**Proposed PR.** Add `design_export(format="ipc_input")` — a structured JSON dump of nets, currents, voltages, and clearance domains. Standards-compliance tools (ours, KiCad-IPC plugin, or other) consume that.

**Our work.** `ipc_check` node in `verification-workflow` reads this export, queries `rag_search(corpus="standards")` for relevant clauses, and computes/validates.

**Estimated effort.** Upstream PR: 1 week. Downstream IPC checker: 2 weeks.

---

### 2.5 No thermal reasoning

**Status.** atopile tracks `max_power` parameters on `ElectricPower` and components, and the picker uses thermal package specs (`R_θJA`, etc) when available. But there's no propagation: "given this junction temperature limit and this ambient, derate continuous current."

**The gap.** Thermal derating is a critical sanity check for power electronics. Doing it by hand in `assert` statements works for one component but doesn't compose across a board.

**Why PR.** Aligns with atopile's constraint-solver model. Thermal is a propagation problem like voltage.

**Proposed PR.** Extend the solver to support `thermal_node` interfaces (analogous to `ElectricPower`) and `R_θJA`, `R_θCS`, `R_θSA` traits on components. The solver computes junction temperatures and warns on derating violations.

**Risk.** Significant — touches the solver core.

**Our work regardless.** `thermal_check` node in `verification-workflow` computes derating from BOM + ambient assumption, flags overheating. No atopile changes required for our v1.

**Estimated effort.** Upstream: 4–6 weeks. Downstream check: 1 week.

---

### 2.6 No firmware / pin-capability co-design

**Status.** A `STM32G474` package wrapper exposes pins like `pwm_a[0..2]` and `can.tx/rx`. The schematic agent can choose which physical MCU pin each interface maps to. But there's no validation that those mappings are valid (e.g. PA8 must be on a TIM1-CH1-capable pin, not just any pin).

**The gap.** Pin assignment errors are caught at firmware bringup, not at design time. Cross-domain bug.

**Why PR.** atopile's `package` system could carry pin-capability metadata. When you write `pwm_a[0].line ~ package.PA8`, the compiler could verify PA8 is timer-capable.

**Proposed PR.** Add `has_pin_capabilities` trait that maps physical pins to capability sets `{TIM, USART, SPI, CAN, ADC, ...}`. Compiler checks assignments against capability set.

**Our work.** Not implemented in v1. This gap is handled by the upstream PR path above; downstream tooling for this capability is deferred.

**Estimated effort.** Upstream: 6+ weeks (data ingestion for all MCU families is the long tail). Downstream: 2 weeks per MCU family.

---

### 2.7 Skills are statically loaded — no dynamic context

**Status.** `AgentConfig.fixed_skill_ids = ["agent", "ato", "planning"]` is hardcoded. Every turn loads the full ~5,000 word skill bundle even if the task is "rename a variable."

**The gap.** Wasted tokens on simple tasks, missing skills on complex ones. Code review needs `code-review/SKILL.md`. Package authoring needs `package-agent/SKILL.md`. Layout work needs more `frontend/SKILL.md`-style detail (not currently structured for layout).

**Why PR.** Dynamic skill loading by task classification is widely useful — Cursor and Claude Code both do something analogous.

**Proposed PR.** Add a Haiku-class classifier that picks 1–3 skills per turn from a directory of available skills. Falls back to `["agent", "ato", "planning"]` if classification fails. Token budget unchanged in worst case, savings of 60–70% on routine turns.

**Our work.** Our orchestrator already does this — we won't get atopile's improvements but we don't depend on them either.

**Estimated effort.** Upstream PR: 2 weeks.

---

### 2.8 No artifact provenance / lot traceability

**Status.** `parts_install lcsc_id="C2286"` ties to an LCSC ID but not to a specific lot, datecode, or supplier batch. For a board that goes to production, this is a real gap.

**The gap.** Reproducibility. Two builds 6 months apart against the same `.ato` source may produce different physical boards because the picker found a different stock part.

**Why PR.** Aligns with atopile's "hardware like software" mission. Software has `package.lock` / `uv.lock`. Hardware should too.

**Proposed PR.** `ato build --freeze` produces an `ato.lock` that records exact LCSC IDs, stocks, datasheets, and pricing snapshots. Subsequent builds with `--frozen` fail if anything has changed.

**Our work.** We can shadow this with a `provenance` field in the BOM enrichment, but real lockfile support belongs upstream.

**Estimated effort.** Upstream PR: 2–3 weeks.

---

### 2.9 No RAG / citation hooks

**Status.** When the agent picks a part via `parts_install`, the response includes manufacturer/description but no link to its datasheet or design guide. The schematic agent has to call `web_search` separately to ground its decisions.

**The gap.** No first-class citations. The agent emits `.ato` code but can't cite "per TLV75901 datasheet figure 5, V_drop is 200mV at 500mA load."

**Why PR.** atopile's `has_datasheet` trait exists but is rarely populated. A datasheet URL field on `parts_install` results, populated from LCSC/EasyEDA metadata, would unlock a lot.

**Proposed PR.** Add `datasheet_url` and `application_notes_urls` to `Component` model in `dataclasses.py`; populate from JLCPCB API where available; expose via `parts_install` and `inspect_library_module`.

**Our work.** Our `parts-agent` calls `fetch_datasheet(mpn)` to populate our datasheet RAG corpus, and the schematic agent retrieves cited chunks. This is more powerful than just a URL but more expensive per part.

**Estimated effort.** Upstream PR: 1 week (just plumbing existing metadata through).

---

### 2.10 No interaction with cross-domain agents

**Status.** atopile assumes one agent edits the design at a time. No notion of "this peripheral is reserved by firmware" or "this region of the board is constrained by mechanical."

**The gap.** Multi-agent / multi-disciplinary coordination. In our orchestrator, the topology agent might reserve a CAN peripheral while the schematic agent is still emitting code. There's no way to tell atopile "do not assign anything to PA11/PA12."

**Proposed PR.** Add `is_reserved_by(domain, reason)` trait on pin nodes. Compiler refuses to assign reserved pins to interfaces.

**Risk.** Touches the solver. Probably needs design discussion in their Discord first.

**Our work.** We layer this on top: orchestrator maintains a reservation table; topology agent reads it before assigning; schematic agent's prompt includes "the following pins are reserved: ..." in the system prompt. Brittle but functional.

**Estimated effort.** Upstream design + PR: 3+ weeks.

---

### 2.11 Schematic emit is broken — `kicad.dumps(SchematicFile)` produces non-loadable files

**Status.** The Zig sexp engine has a full `.kicad_sch` *read* model, but the *write* path is broken. Verified with `kicad-cli 10.0.3` (2026-06-05): re-dumping a known-good fixture through `kicad.dumps` yields a file KiCad refuses to load ("Failed to load schematic"), even though the original loads. The typed `KicadSch` model drops the root `(symbol_instances)`/`(sheet_instances)` tables on load and mis-emits `(symbol …)` blocks (52→30 occurrences on round-trip). So no Python code can currently emit a loadable schematic via the typed model. Full investigation: `13_KICAD_SCH_AND_FRONTEND_FILES.md` §1.5.

**Why PR.** A genuine upstream bug in the sexp schematic serializer — the model is validated only for round-trip *equality through atopile's own parser*, never for KiCad-loadability of the output. Fixing it makes `.kicad_sch` a first-class output for everyone and is the prerequisite for any typed-model schematic emitter.

**Our work regardless.** ✅ **Shipped in Session 8 (2026-06-06).** The EE-agent schematic emitter sidesteps this `dumps` bug entirely: it writes the `.kicad_sch` as sexp *text* (`src/faebryk/exporters/schematic/`) and, for real picked-part symbols, regenerates the embedded `(symbol …)` block in the schematic's native `20211123` form from the *parsed* typed model (the read path works). Wired as the `generate_schematic` build step; verified loadable + ERC-clean in KiCad. So the "no Python schematic emitter exists" gap is now closed for our fork; the upstream `dumps` fix remains a separate nice-to-have (would let an emitter use `kicad.dumps` directly instead of text).

**Estimated effort.** Upstream diagnosis + fix in the Zig sexp serializer: unknown until the `(symbol)` defect is isolated; treat as open-ended.

---

## 3. Downstream-only gaps — never upstream

These are domain-specific to our agent workflow and don't belong in atopile.

### 3.1 Topology decisions

Picking *which blocks* a design needs is RAG-driven (reference designs, app notes) and orchestrator-driven (user intent). atopile is downstream of this — it executes the topology, doesn't choose it. **Stays in `subagents/topology.py`.**

### 3.2 Natural-language requirements capture

"5V to 3.3V LDO with low-Iq for coin-cell BLE sensor" → structured requirements YAML. Pure NLU task. **Stays in `subagents/requirements.py`.**

### 3.3 Qualified parts (AEC-Q, MIL, ITAR)

See §2.3. Even if atopile someday supports multi-distributor, qualified-parts logic is opinionated per company. **Stays in `subagents/parts.py` overlay.**

### 3.4 IPC compliance evaluation against RAG

§2.4 above proposes an export hook upstream. The evaluation logic — querying `standards` corpus, citing clauses, applying judgment — stays in `workflows/verification.py`.

### 3.5 Datasheet RAG citations in design rationale

The agent should annotate `.ato` modules with "per AN-1234 §3, 100µF bulk cap required for transient response." atopile has no opinion on this — it's our value-add.

### 3.6 Cost optimization across BOMs

"This BOM is $4.20; can we get under $3.50?" Multi-supplier optimization, lifecycle-aware substitution. **Stays in `subagents/bom.py`.**

### 3.7 Multi-design coordination

"What's the BOM cost across all 3 designs in this product line?" "Which parts are common?" Stays in the orchestrator layer.

### 3.8 Conversational memory / preference learning

"I prefer Texas Instruments LDOs." atopile has no user model. Lives in our `MemorySaver` checkpointer.

---

## 4. Joint gaps — both layers participate

### 4.1 Simulation

- **Upstream (proposed):** `sim_export` tool that produces clean SPICE in multiple flavors, with probe metadata.
- **Downstream:** PySpice node that consumes the export and runs DC/AC/transient.

### 4.2 IPC standards

- **Upstream (proposed):** `design_export(format="ipc_input")` that dumps nets/currents/voltages.
- **Downstream:** `ipc_check` node queries RAG, computes, cites.

### 4.3 Pin-mux / capability validation

- **Upstream (proposed):** `has_pin_capabilities` trait + compiler check.
- **Downstream:** Deferred; no downstream tool planned for v1.

### 4.4 Provenance / lockfile

- **Upstream (proposed):** `ato.lock` and `--frozen` build mode.
- **Downstream:** BOM enrichment shadows the lockfile with pricing/stock snapshots from the parts-agent's APIs.

---

## 5. Recommended contribution order

If we have bandwidth to upstream a few of these, in order of best ROI for us *and* for the atopile community:

1. **§2.1 — Expose 40 tools via MCP.** Trivial, lifts every MCP user. Removes our biggest awkwardness (needing two transport mechanisms).
2. **§2.9 — Datasheet URL in part metadata.** Cheap, useful for everyone. Unblocks RAG citation in our schematic-agent rationale.
3. **§2.8 — `ato.lock`.** Production-readiness win for everyone, removes a real BOM/provenance gap.
4. **§2.2 — `sim_export`.** Light upstream version; we own the runner.
5. **§2.4 — `design_export(format="ipc_input")`.** Light upstream version; we own the IPC checker.
6. **§2.7 — Dynamic skill loading.** Token-cost win for every user of the atopile agent.

Hold off on:
- **§2.3** — multi-distributor picker. Big legal review. We can do this entirely downstream.
- **§2.5** — thermal. Touches the solver. High-risk PR.
- **§2.6, §2.10** — pin-mux capability validation, cross-domain coordination. Need design discussion in atopile's Discord before any code.

---

## 6. Tracking

Each gap above gets an issue in our repo under `docs/atopile-gaps/`. When we upstream one, we link the PR back here. The list is alive — as atopile evolves and our use cases sharpen, we add and prune.

Last reviewed: at project setup. Next review: after first end-to-end design completes (post §13.5 milestone in `08_PROJECT_PLAN.md`).
