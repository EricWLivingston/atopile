# 18 · KiCad Schematic Support & Frontend File-Explorer Limitation

> Two findings from a focused source-tree audit (no code modified). These directly affect the EE-agent design discussion:
>
> 1. **KiCad schematic generation does not exist in atopile.** Read/parse exists in the Zig sexp engine; no Python emitter, no `.kicad_sch` written by the build pipeline.
> 2. **`ato serve frontend` cannot load project files** when opened in a plain browser — the file explorer is wired to the VS Code extension's webview message bus and silently no-ops outside it.

---

## 1. KiCad schematic support — what exists, what doesn't

### 1.1 Parser / model (Zig, complete)

The S-expression engine has a full `.kicad_sch` model:

- `src/faebryk/core/zig/src/sexp/kicad/schematic.zig` — typed Zig structs for symbols, sheets, wires, junctions, global labels, properties, etc.
- `src/faebryk/core/zig/gen/sexp/schematic.pyi` — generated Python stubs (`KicadSch`, `KicadSchFile`, …). Importable via `from faebryk.core.zig import schematic`.

This means we *can* parse, mutate, and re-serialize KiCad schematics from Python today via the typed sexp bindings. The machinery exists.

### 1.2 No Python emitter wired into the build

A repo-wide search for places that *write* `.kicad_sch` found **zero** call sites:

```
grep -rn "outputs.kicad_sch = " src/   → 1 hit (server/domains/manufacturing.py — only reads if the file already exists)
grep -rn "KicadSch("            src/   → 0 hits (no Python instantiation)
grep -rn "schematic\.dumps"     src/   → 0 hits
grep -rn "import .* schematic"  src/   → 0 hits outside the Zig README example
```

The Python code path is purely consumer-side:

| Location | What it does |
|---|---|
| `src/atopile/server/domains/manufacturing.py:196-198` | Reads `build/<target>.kicad_sch` *if it exists* into `outputs.kicad_sch`. Never writes it. |
| `src/atopile/server/domains/manufacturing.py:432-434` | Copies that path into manufacturing export tarballs. |
| `src/atopile/server/routes/files.py:31` | Registers `.kicad_sch` MIME type for the file server. |
| `src/atopile/server/routes/manufacturing.py:43,296` | Exposes the path as `kicadSch` on the manufacturing response. |
| `src/atopile/server/domains/actions.py:1260,1734` | File-watcher pattern + action response field. |
| `src/atopile/server/agent/tools.py:90,476` | The `report_manufacturing_outputs` agent tool returns the path if it happens to be there. |
| `src/atopile/server/file_watcher.py` | Watches the file for change events. |

Nothing in `src/atopile/build_steps.py`, `src/atopile/buildutil.py`, `src/atopile/cli/build.py`, or `src/faebryk/exporters/pcb/` emits a schematic. The `pcb/kicad/` exporter package contains only `transformer.py` and `artifacts.py` — both PCB-only.

### 1.3 What atopile *does* offer instead

- **Block diagram via `ato view`** (`src/atopile/cli/view.py`): renders the instance graph in a web visualizer at `localhost:8765`. This is a logical block diagram, not a KiCad schematic, and is read from the live graph via `faebryk.core.graph_export.export_graph_to_json`.
- **KiCad PCB output**: fully supported (`outputs.kicad_pcb` is populated by the build pipeline via the PCB exporter in `src/faebryk/exporters/pcb/kicad/transformer.py`).
- **Layout sync plugin** (`src/atopile/kicad_plugin/`): runs *inside* KiCad to mirror sub-layout placements onto the PCB. The plugin manifest declares a `"schematic"` scope for actions, but the only registered action (`layout-sync`) targets `["pcb"]`.

### 1.4 Implications for the EE-agent plan

- **`08_PROJECT_PLAN.md` does not currently include schematic generation.** Section 5 of `00_ARCHITECTURE.md` "What we explicitly do NOT build" should be amended to call this out: *schematic generation is also out of scope for v1*. KiCad opens `.ato`-produced PCBs without an accompanying schematic — annotation/synchronization features in KiCad that depend on a schematic (ERC, "Update PCB from schematic") simply won't be available, but DRC and layout still work.
- **If we ever want schematic output** — ⚠️ **the "build a Python emitter on the typed `KicadSch` model" path is now known to be broken** (the `kicad.dumps` write path does not produce KiCad-loadable files). A standalone **text emitter** was spiked and validated against `kicad-cli` instead. See **§1.5 below** for the full investigation, the validated approach, and the pick-up checklist. The exporter still belongs in `src/faebryk/exporters/schematic/kicad/` (mirroring the PCB exporter) and is recorded in `07_ATOPILE_GAPS.md` §2.11 as an upstream contribution candidate.
- **For the EE agent's reasoning loop**, the absence of a schematic mostly affects: (a) `ipc_check` cannot reference net names visualized on a schematic page; (b) the model's commentary cannot say "see sheet 3" — it must reference `.ato` source lines and net labels. Neither is a blocker for v1.

### 1.5 UPDATE (2026-06-05): typed `dumps` is broken; standalone text emitter validated

> ✅ **BUILT in Session 8 (2026-06-06).** The exporter + build step shipped using the
> validated text-emitter approach below. `ato build` now writes
> `build_dir/<target>.kicad_sch`; verified loadable + ERC-clean (0 errors, 0
> unconnected) in KiCad on `examples/i2c`. Real picked-part symbols are embedded
> (regenerated into the schematic's native `20211123` form from the typed model);
> components without a cached symbol get a generic box. Code:
> `src/faebryk/exporters/schematic/kicad/{schematic,generic_symbol,real_symbol}.py`,
> build step `generate_schematic` in `src/atopile/build_steps.py`, tests
> `test/exporters/test_schematic_export.py`. The agent `schematic_export` tool was
> deliberately deferred (build-step/CLI scope this milestone). See passdown Session 8.
>
> ✅ **Session 9 (2026-06-06) — drawn net wires (ladder routing).** The build now
> defaults to **wire mode** (`export_schematic(draw_wires=True)` → `render_wired`):
> generic bottom-pin boxes in one row, each pin in a globally unique x-lane, every net
> drawn as a horizontal **trunk** + vertical **drops** + **junctions** in the empty
> channel below — no per-pin label soup (one net label per trunk). Unique lanes + empty
> channel make it **provably short-free** (a drop can only cross other nets, never tap
> them). New: `build_wire_box` in `generic_symbol.py`, `render_wired` + wire/junction
> emitters in `schematic.py`. Label mode (real symbols, `draw_wires=False`) is retained.
> Verified on `examples/i2c`: loads + ERC 0 errors, and `kicad-cli sch export netlist`
> reproduces the exact intended nets (hv=6, lv=4, SDA/SCL/Alert=2). 11 exporter tests
> pass (incl. netlist-membership no-shorts checks).

> An EE-agent task asked: *"what would it look like to add a tool that outputs a KiCad
> `.sch` from the `.ato` code?"* This section captures the investigation + a validated
> spike. **Status: PAUSED after the spike (proven viable); full feature not built.**
> Plan file: `/Users/ericlivingston/.claude/plans/add-a-more-robust-transient-blum.md`
> (Context + BLOCKER + revised options). Tooling: `kicad-cli 10.0.3` at
> `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`. Fixtures:
> `test/common/resources/fileformats/kicad/v8|v9/sch/test.kicad_sch`.

#### Decision (user-confirmed)
- **Fidelity:** a **label-based "connectivity schematic"** — grid-placed component
  symbols with a **net-name global label on every pin, no drawn wires**. Valid
  `.kicad_sch`, opens in KiCad, electrically complete (ERC sees connectivity through
  like-named global labels). A *readable* auto-placed/auto-routed schematic is
  research-grade and explicitly out of scope.
- **Surface:** exporter core + build step + agent tool (the layered design from the
  plan). Output path `build_dir/<target>.kicad_sch` is **already surfaced** by
  `domains/manufacturing.py:196-198` as `outputs.kicad_sch` (file-watcher watches
  `*.kicad_sch`) — so writing the file auto-integrates; no route/frontend changes.

#### ⚠️ BLOCKER: the typed `KicadSch` model + `kicad.dumps` cannot emit a loadable file
Proven empirically (this supersedes §1.4's "build a Python emitter on the typed model"):
- **Control passes:** `kicad-cli sch export svg` loads the repo's own v8/v9 fixtures.
- **Round-trip fails:** load a known-good fixture → `kicad.dumps` → `kicad-cli` returns
  **"Failed to load schematic."** So the *write* path is broken independent of any
  graph logic. (The model is validated only for sexp round-trip *equality through
  atopile's own parser* — never for KiCad-loadability of the output.)
- The typed `KicadSch` model has **no fields** for the root `(symbol_instances …)` /
  `(sheet_instances …)` tables, so it silently drops them. (These turned out **not**
  load-critical for KiCad 10 — stripping them from a good fixture still loads — so
  there is at least one *additional* `dumps` defect.)
- Token diff of original vs re-dumped fixture: `(symbol …)` blocks differ **52 → 30**
  while `lib_id`/`pin`/`property` counts match → `dumps` mis-emits/merges `symbol`
  structure. Exact defect not isolated. Recorded as `07_ATOPILE_GAPS.md` §2.11.

#### ✅ Validated approach: standalone TEXT emitter (Option 1)
A spike that writes the `.kicad_sch` **sexp text directly** (bypassing `kicad.dumps`)
**passed both** `kicad-cli sch export svg` (KiCad loads + renders) **and**
`kicad-cli sch erc` (0 errors, **0 unconnected-pin violations** → labels land on pins).
Reusable facts nailed down by the spike:
1. **The lib-symbol name MUST equal the instance `lib_id`** (e.g. both `"atopile:GEN2"`).
   Mismatch ⇒ KiCad can't resolve the symbol ⇒ "Failed to load schematic."
2. **Coordinate transform (the gotcha):** a symbol's Y axis is **flipped** when
   instantiated. For an instance at `(ix, iy)` with angle 0, a symbol-space pin
   connection point `(px, py)` maps to schematic `(ix + px, iy − py)`. Place the net's
   `global_label` there. ERC confirmed connectivity (no unconnected pins) ⇒ correct.
3. **Connectivity = same-named `global_label`s** (no wires). KiCad joins like-named
   global labels into one net.
4. Target **version `20211123`**, `generator "eeschema"`, `paper "A4"`, **unquoted
   uuids** (mirrors the proven fixture; KiCad 10 reads it).
5. **Benign warning:** ERC emits `[lib_symbol_issues]: … does not include the symbol
   library 'atopile'` — cosmetic (the symbol is **embedded** in `lib_symbols`; renders
   fine). Quiet later via a generated sym-lib-table or a blank nickname if desired.

**Validation commands:**
```
KCLI=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
"$KCLI" sch export svg -o OUT/ FILE.kicad_sch      # must print "Plotted… Done."
"$KCLI" sch erc -o OUT/erc.rpt FILE.kicad_sch        # 0 Errors; no "unconnected"
```

#### Pick-up checklist (what's left to turn the spike into the feature)
1. **Graph → IR extraction** (emit-independent, unit-testable): components (refdes via
   `faebryk/libs/app/designators.py`, value), pins/pads, and nets via
   `F.Net.bind_typegraph(tg).get_instances(g)`, `net.get_connected_pads()`,
   `net.get_name()`, and the `F.Footprints.is_pad` trait's `pad_number`. Produce
   `list[Component(ref, value, pins=[(number, net_name|None)])]`.
2. **Generalize the writer:** N-pin generic boxes (split pins L/R for large parts),
   dedup `lib_symbols`, string escaping, and a summary (components/nets/labels/
   fallbacks/unmapped pads). Real `.kicad_sym` symbol embedding is a *later* enhancement
   (the model's *read* side works); generic boxes already give a complete valid schematic.
3. **Build step:** `@muster.register("schematic", description="Exporting schematic",
   dependencies=[prepare_nets], produces_artifact=True)` in `src/atopile/build_steps.py`
   → write `build_dir/<target>.kicad_sch`.
4. **Agent tool:** `@_register_tool("schematic_export")` in
   `src/atopile/server/agent/tools.py` + schema in `tool_definitions_project.py`
   (mirror `_tool_build_run` / `_resolve_build_target`).
5. **Tests:** synthetic-IR → emit → `kicad-cli sch erc` clean (gate on the KiCad binary
   being present); integration `ato build` on `examples/i2c` asserting
   `build_dir/<target>.kicad_sch` exists + reparses with atopile's own parser.

**Files (planned):** new `src/faebryk/exporters/schematic/{__init__.py, kicad/schematic.py,
kicad/generic_symbol.py}`; edits to `build_steps.py`, `server/agent/tools.py`,
`tool_definitions_project.py`; test `test/exporters/test_schematic_export.py`.
**Risks:** symbol availability (generic-box fallback), pad↔symbol-pin numbering for
multi-unit/BGA/exposed-pad (`EP`) parts, power/gnd as plain labels, flat single sheet.

#### The validated spike emitter (reproducible — this exact script passed kicad-cli)
```python
import uuid
from pathlib import Path
def U(): return str(uuid.uuid4())

LIB_ID = "atopile:GEN2"                      # lib-symbol name MUST equal instance lib_id
PINS = [("1", -5.08, 2.54), ("2", -5.08, -2.54)]   # (number, x, y) in SYMBOL coords (Y up)

def lib_symbol():
    pins = "\n".join(
        f'''      (pin passive line (at {x} {y} 0) (length 2.54)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "{n}" (effects (font (size 1.27 1.27)))))'''
        for (n, x, y) in PINS)
    return f'''    (symbol "{LIB_ID}" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
      (property "Reference" "U" (id 0) (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GEN2" (id 1) (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "GEN2_0_1"
        (rectangle (start -2.54 5.08) (end 2.54 -5.08)
          (stroke (width 0.254) (type default)) (fill (type background))))
      (symbol "GEN2_1_1"
{pins}
      )
    )'''

def sym_instance(ref, ix, iy):
    iu = U()
    pins_txt = "\n".join(f'    (pin "{n}" (uuid {U()}))' for (n, _, _) in PINS)
    block = f'''  (symbol (lib_id "{LIB_ID}") (at {ix} {iy} 0) (unit 1)
    (in_bom yes) (on_board yes) (fields_autoplaced)
    (uuid {iu})
    (property "Reference" "{ref}" (id 0) (at {ix} {iy-7.62} 0) (effects (font (size 1.27 1.27))))
    (property "Value" "GEN2" (id 1) (at {ix} {iy+7.62} 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (id 2) (at {ix} {iy} 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "" (id 3) (at {ix} {iy} 0) (effects (font (size 1.27 1.27)) hide))
{pins_txt}
  )'''
    return block, iu

def glabel(net, x, y):                        # connection point = pin point (Y already flipped)
    return f'''  (global_label "{net}" (shape bidirectional) (at {x} {y} 180) (fields_autoplaced)
    (effects (font (size 1.27 1.27)) (justify right))
    (uuid {U()}))'''

# design: R1, R2 ; R1.1<->R2.1 = VCC ; R1.2<->R2.2 = GND
comps = [("R1", 50.8, 50.8, {"1": "VCC", "2": "GND"}),
         ("R2", 88.9, 50.8, {"1": "VCC", "2": "GND"})]
inst_blocks, labels, sym_paths = [], [], []
for ref, ix, iy, netmap in comps:
    b, iu = sym_instance(ref, ix, iy)
    inst_blocks.append(b)
    sym_paths.append(f'    (path "/{iu}" (reference "{ref}") (unit 1) (value "GEN2") (footprint ""))')
    for (n, px, py) in PINS:
        labels.append(glabel(netmap[n], ix + px, iy - py))   # <-- the Y-flip transform

doc = f'''(kicad_sch (version 20211123) (generator eeschema)
  (uuid {U()})
  (paper "A4")
  (lib_symbols
{lib_symbol()}
  )
{chr(10).join(inst_blocks)}
{chr(10).join(labels)}
  (sheet_instances
    (path "/" (page "1"))
  )
  (symbol_instances
{chr(10).join(sym_paths)}
  )
)
'''
Path("test.kicad_sch").write_text(doc)
```

---

## 2. Why `ato serve frontend` doesn't load project files in a browser

### 2.1 Symptom

Run:

```bash
ato serve backend  --workspace ./examples --force        # FastAPI on 8501
ato serve frontend --backend localhost:8501              # Vite on 5173
open http://localhost:5173
```

The frontend connects to the backend WebSocket and shows projects, but the **file explorer panel stays empty** — no tree, no files. No error toast.

### 2.2 Root cause

The file explorer asks the **VS Code extension** for the file tree, not the backend. From `src/ui-server/src/components/FileExplorerPanel.tsx:505-517`:

```tsx
// Request files from the VS Code extension (not the backend)
useEffect(() => {
  if (!projectRoot) return;
  if (files && files.length > 0) return;

  useStore.getState().setLoadingFiles(true);
  postToExtension({
    type: 'listFiles',
    projectRoot,
    includeAll: true,
  });
}, [projectRoot, files]);
```

`postToExtension` (`src/ui-server/src/api/vscodeApi.ts:46-51`) is a **no-op** outside a VS Code webview:

```ts
export function postToExtension(message: unknown): void {
  const api = getVsCodeApi();
  if (api) {
    api.postMessage(message);
  }
}
```

`getVsCodeApi()` calls the globally-injected `acquireVsCodeApi()`, which only exists inside a VS Code webview iframe. In a plain browser, the call is silently dropped and **no `filesListed` event ever arrives**, so the loading spinner stops on the first render (because of the `if (files && files.length > 0) return` short-circuit, which only fires after a prior listing — see below) and the UI is left with `loadingFiles=true` indefinitely or empty state.

The receiving side that *would* return data lives in the extension only:

- `src/vscode-atopile/src/providers/SidebarProvider.ts:326-327` — handler:
  ```ts
  case 'listFiles':
    this._fileOps.listFiles(message.projectRoot, message.includeAll ?? true);
  ```
- `src/vscode-atopile/src/providers/sidebar/file-operations.ts:172-…` — uses `vscode.workspace.fs.readDirectory(...)` and posts a `filesListed` message back.

Both paths require the VS Code API. None of this code is reachable from `ato serve frontend`.

### 2.3 The backend has no equivalent HTTP endpoint

A grep over `src/atopile/server/routes/`:

| Endpoint | Purpose |
|---|---|
| `GET /api/projects` | List projects discovered in `workspace_paths`. |
| `GET /api/modules?project_root=…` | List `.ato` module definitions. |
| `GET /api/dependencies?project_root=…` | Read `ato.yaml` deps. |
| `GET /api/file?path=…` | **Serve a single file by absolute path** (with `_is_path_allowed` guard). |
| `GET /api/file/zip-contents`, `GET /api/file/zip-list` | Zip helpers. |

There is no `GET /api/files?project_root=…` that returns a tree, and there is no `loadDirectory` analog. So even if the frontend wanted to fall back to HTTP, the route doesn't exist server-side.

### 2.4 Other things that also depend on the extension bus

A grep for `postToExtension(` callers in `src/ui-server/src/` shows the same no-op problem affects, at minimum:

- File explorer (this section)
- "Open in editor" / `openSignals` (jump-to-source clicks)
- `openDiff` (agent edit previews)
- `browseAtopilePath` / `browseProjectPath` / `browseExportDirectory` (native file dialogs)
- `openSourceControl`, `showProblems`, `showLogs`, `reloadWindow`
- `openInSimpleBrowser`

All of these are reasonable in a desktop IDE host but degrade silently in a plain browser. The file explorer is just the most visible casualty because it is the primary navigation surface.

### 2.5 Fix options (ranked by effort)

1. **Document the limitation.** Cheapest. Add a paragraph to `README.md` and the `ato serve frontend` CLI help: "browser mode does not include the file explorer; use the VS Code extension."
2. **Add a backend `/api/files` tree endpoint + a fallback in `FileExplorerPanel`.** Medium effort (≈100 LoC backend + 50 LoC frontend). Detect "no VS Code API" with `isVsCodeWebview()` and pivot to HTTP. Reuses the same `_is_path_allowed` guard already in `routes/files.py`. Same approach for `loadDirectory`. This is the right long-term fix and would unlock browser-only deployments of the agent.
3. **Wrap the file explorer in an opt-out flag** so the panel renders an explicit "file explorer unavailable in browser mode — open in VS Code" message instead of a perpetual spinner. Cheap; better UX than silent failure.

For the **EE-agent fork** under Option C, the relevance is:

- Tier-3 dev loop (`12_DEV_TEST_HARNESS.md` Section 2) describes hitting the agent via the frontend. The chat panel works fine in a browser (it talks to `/api/agent/...` over HTTP and WebSocket), but the file explorer does not. Document this in the harness doc.
- If we want a fully browser-served EE agent (no VS Code), Option (2) above goes onto the M3-or-later list. Until then, the developer uses the VS Code extension or the CLI to navigate files.

---

## 3. Concrete follow-ups (no code written this session)

- [ ] **Update `00_ARCHITECTURE.md` §5** to add "no schematic generation in v1" alongside the existing no-BOM-tool / no-thermal-tool exclusions.
- [ ] **Append to `07_ATOPILE_GAPS.md`**: KiCad schematic emitter as an upstream contribution candidate; the Zig sexp model is already there.
- [ ] **Append a paragraph to `12_DEV_TEST_HARNESS.md` §2 Tier 3** noting the file-explorer limitation in browser mode and pointing developers to the VS Code extension for navigation during agent dev.
- [ ] **Open a discussion (not in this doc set)** on whether the EE-agent fork ships a small `/api/files` tree endpoint + browser fallback, or stays VS-Code-only for now. Recommend deferring until M8 if at all — the agent itself doesn't depend on the explorer.

---

## 4. Cross-references

- `00_ARCHITECTURE.md` — section 5 ("What we explicitly do NOT build") should be amended per Section 3 above.
- `07_ATOPILE_GAPS.md` — destination for the schematic-emitter contribution note.
- `12_DEV_TEST_HARNESS.md` — Tier 3 dev loop is the place to call out the frontend file-explorer caveat.
- `06_ATOPILE_INTEGRATION.md` — confirms our fork avoids touching upstream UI code; this finding doesn't change that.
