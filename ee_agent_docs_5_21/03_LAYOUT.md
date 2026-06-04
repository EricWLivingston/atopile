# 06 · Layout (atopile-owned in v1)

> **Status: deferred.** Layout in v1 is fully handled by atopile's existing tools and KiCad bridge. We do not add new layout tooling. This doc remains as a stub so the file list stays consistent and so future additions have a place to live.

---

## 1. What atopile already does

When `build_run` succeeds, atopile produces a `layout.kicad_pcb` at `build/<target>/`. The board has:

- All components placed by atopile's footprint emitter (at default positions; not optimized)
- Nets connected per the schematic
- A `.kicad_dru` design-rules file
- Compatible with KiCad's normal workflow for routing

Atopile also exposes these tools through the runner:
- `layout_set_component_position(designator, x, y, rot?)` — programmatic placement
- `layout_get_component_position(designator)` — inspect current placement
- `layout_run_drc()` — DRC against the `.kicad_dru`
- `layout_set_board_shape(...)` — set board outline / mechanical bounds

The agent can call these via atopile's normal skill-driven flow when layout work is appropriate. No new tools needed for v1.

---

## 2. What we explicitly are not building in v1

- No automatic placement optimizer.
- No autorouter integration.
- No human-in-the-loop layout review tool (atopile's FastAPI server can be used as a UI hook if needed; we don't add to it).
- No layout-specific verification (`ipc_check` does what's needed for trace widths; everything else routes through KiCad).

---

## 3. When this might change

Add layout tooling here if the agent reliably hits one of these patterns:

- Repeatedly places mounting holes incorrectly → add a small `layout_place_mounting_holes(corner_offset_mm)` tool
- Struggles with deterministic decoupler placement near IC pins → add `layout_place_decouplers(near_ic, distance_mm)` tool
- Needs RF / high-voltage keepouts → add `layout_set_keepout(region, layer, net_class)` tool

Each of these would be a single registered tool, ~100 LoC. The agent calls them through atopile's runner like any other.

For now, the agent uses atopile's existing `layout_set_component_position` for individual placements and asks the user to handle routing.

---

## 4. References

- atopile's layout tools live in `src/atopile/server/agent/tool_layout.py` (~809 LoC in the upstream repo).
- KiCad DRC integration runs via `kicad-cli pcb drc` or KiCad's pcbnew API.
- `ipc_check` (see `04_VERIFICATION.md`) covers post-layout IPC compliance.
