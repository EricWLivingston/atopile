---
name: ipc_check
description: "When and how to use ipc_check: verify a laid-out board against IPC trace-width/clearance and current-capacity rules. SEED — the tool is not yet implemented (M6); returns a stub for now."
---

# ipc_check — IPC verification guidance (seed / forthcoming)

> **Status: not yet implemented (M6).** Calling `ipc_check` today returns a graceful
> not-implemented stub (`{"ok": false, "error": "... not implemented yet (M6)"}`). Don't rely
> on it for verification yet; this doc captures the intended contract so guidance is ready
> when the body lands.

`ipc_check` will verify a **built, laid-out** design against IPC standards and return findings
cited to clauses.

## Intended scope (v1)

- **IPC-2221B** — trace width and clearance for the declared net currents.
- **IPC-2152** — current-carrying capacity.

For each declared net (current from `report_variables`), it computes the required trace width
given current + temperature rise + copper weight, checks it against the actual PCB trace
widths, and flags any net below the requirement.

## When to use (once implemented)

- After layout/manufacturing data exists for the target — it reads the PCB, not just the
  schematic.
- When you need to confirm power/high-current nets meet trace-width/clearance rules before
  release.

## When NOT to use

- Before a board is laid out (nothing to measure).
- For analog behaviour (use `pyspice_run`) or datasheet facts (use `rag_search`).

## Intended signature

`ipc_check(build_target, standards=["IPC-2221B","IPC-2152"], ambient_temp_rise_c=10,
copper_weight_oz=1, project_path=None)` → `{success, findings:[{severity, check, net,
current_a, observed_mm, required_mm, rationale, citation}]}`.

## Note

The IPC standards corpus (IPC-2221B / IPC-2152) that the cited rationales will draw from is
part of the RAG standards-corpus expansion, which is still pending (the corpus is
datasheets-only today).
