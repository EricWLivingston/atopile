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
- **If we ever want schematic output**, the cheapest path is:
  1. Build a Python emitter on top of the existing Zig `KicadSch` model.
  2. Walk the instance graph (same source the PCB exporter walks).
  3. Emit symbols + wires + hierarchical sheets; let KiCad's auto-router/auto-place handle aesthetics on first open.
  This belongs in `src/faebryk/exporters/schematic/kicad/` and would mirror the structure of the existing PCB exporter. Plausible 1–2 week scope for a v0 that produces openable-but-ugly schematics. Out of scope for the EE-agent fork; worth recording in `07_ATOPILE_GAPS.md` as an upstream contribution candidate.
- **For the EE agent's reasoning loop**, the absence of a schematic mostly affects: (a) `ipc_check` cannot reference net names visualized on a schematic page; (b) the model's commentary cannot say "see sheet 3" — it must reference `.ato` source lines and net labels. Neither is a blocker for v1.

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
