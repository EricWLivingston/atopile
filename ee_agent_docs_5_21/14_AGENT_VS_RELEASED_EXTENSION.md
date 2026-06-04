# 14 · Why Your Extension Doesn't Look Like the Released One, and How to Get the Agent Working

> Two related questions, one root cause: **the AI sidebar/agent UI exists in source on `main` but is not yet shipped in the marketplace build, and is also off by default.**

---

## 1. Why your extension UI looks nothing like screenshots

### 1.1 What you have installed

```
~/.vscode/extensions/atopile.atopile-0.12.5/
```

I checked. **`v0.12.5` does not contain a `resources/webviews/` directory at all** — no `sidebar.js`, no React bundle, no agent panel. So whatever you see in the sidebar is *only* the native VS Code TreeView fallback the extension renders before its webview is loaded. There is literally no webview UI in the version installed from the marketplace.

### 1.2 What's on `main`

The agent + redesigned sidebar landed in commit `3152f457` ("UI: agent (#1788)") on **2026-03-09**, *after* `v0.12.5` was published. The current `HEAD` (`619eda7f`, 2026-03-11) includes:

- The new sidebar shell (`src/ui-server/src/components/Sidebar.tsx`)
- The agent panel and its runtime (`src/ui-server/src/agent/AgentChatPanel.tsx` + 17 supporting files)
- The new backend agent (`src/atopile/server/agent/`, full `AgentRunner` you've been working with)
- 5 new `.claude/skills/*/SKILL.md` bundles
- A `package.json` flag `atopile.enableChat` (default **`false`** upstream) to gate the UI

That's why screenshots of the "new" extension show a tabbed sidebar with an agent chat at the bottom, and yours shows nothing comparable — the released bits genuinely don't have it. Two release cycles' worth of UI work hasn't been published yet.

### 1.3 Confirming the gap

```bash
ls ~/.vscode/extensions/atopile.atopile-0.12.5/resources/    # no `webviews/` here
grep -c "AgentChat" ~/.vscode/extensions/atopile.atopile-0.12.5/dist/extension.js  # 0
git log --oneline -- src/ui-server/src/agent/                                       # earliest hit = 3152f457
```

---

## 2. How to get the agent working

The agent's three moving parts:

| Part | Where | What it needs |
|---|---|---|
| Backend `AgentRunner` | `src/atopile/server/agent/runner.py` + FastAPI route at `/api/agent/...` | An LLM API key (`OPENAI_API_KEY` for upstream) and the backend running (`ato serve backend` or auto-spawned by the extension) |
| Frontend agent panel | `src/ui-server/src/agent/AgentChatPanel.tsx`, rendered by `Sidebar.tsx` when `ENABLE_CHAT` is true | The webview must be built and `atopile.enableChat=true` in VS Code settings |
| Wiring between them | Extension webview reads config, injects `__ATOPILE_ENABLE_CHAT__` into the bundle (`src/vscode-atopile/src/providers/sidebar.hbs:18`, `SidebarProvider.ts:437,467`); UI reads `win.__ATOPILE_ENABLE_CHAT__` in `src/ui-server/src/api/config.ts:63` | Both halves must be running from the repo build, not from `v0.12.5` |

### 2.1 Step-by-step (from this repo, no marketplace install)

```bash
# 0. Disable the marketplace extension once you've verified the dev build works.
#    For now, leave it — VS Code will use whichever one you launch.

# 1. Build the webview bundle (React → sidebar.js) into the extension's resources dir.
cd src/vscode-atopile
npm install
npm run build:webviews          # runs `cd ../ui-server && npm install && npm run build`
                                # outputs to src/vscode-atopile/resources/webviews/sidebar.js

# 2. Build the extension itself (TypeScript → dist/extension.js).
npm run compile                 # webpack

# 3. Set the flag. Either edit settings.json or use the UI:
#    "atopile.enableChat": true
#    (Upstream default is false. Your working copy has it patched to true already —
#     see `git diff HEAD -- src/vscode-atopile/package.json`.)

# 4. Provide an API key for the agent. Put one of these in your project's .env
#    (the backend loads .env via python-dotenv on AgentConfig.from_env()):
#        OPENAI_API_KEY=sk-...
#        ATOPILE_AGENT_OPENAI_API_KEY=sk-...
#    Optional model override:
#        ATOPILE_AGENT_MODEL=gpt-5.4         # default upstream
#        ATOPILE_AGENT_MODEL=gpt-4.1-mini    # cheaper for dev

# 5. Launch the Extension Development Host.
#    Open src/vscode-atopile/ in VS Code, press F5.
#    A new VS Code window opens with the freshly-built extension loaded.
#    Inside that window, open your atopile project folder.

# 6. The extension auto-spawns `ato serve backend` on a free port (src/vscode-atopile/src/common/backendServer.ts:607,640).
#    Watch the "atopile Server" output channel to confirm it came up.

# 7. Open the atopile sidebar (ato logo in the activity bar).
#    The agent panel renders at the bottom of the sidebar when ENABLE_CHAT is true.
```

### 2.2 Required env vars (backend reads these via `AgentConfig.from_env()`)

From `src/atopile/server/agent/config.py:99-126`:

| Env var | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` or `ATOPILE_AGENT_OPENAI_API_KEY` | — | **Required.** No fallback. |
| `ATOPILE_AGENT_MODEL` | `gpt-5.4` | Set to a cheaper model for dev. |
| `ATOPILE_AGENT_BASE_URL` | `https://api.openai.com/v1` | Override for proxies. |
| `ATOPILE_AGENT_MAX_TOOL_LOOPS` | `240` | Cost cap. |
| `ATOPILE_AGENT_MAX_TURN_SECONDS` | `7200` | Walltime cap. |
| `ATOPILE_AGENT_TRACE_ENABLED` | `1` | Set to `0` to silence the progress stream. |

There is no Anthropic-side config yet — that's what `11_ANTHROPIC_PROVIDER.md` adds.

### 2.3 Quick smoke test the agent is alive (no UI)

```bash
ato serve backend --workspace ./examples --force &
curl -s http://127.0.0.1:8501/health    # → {"status":"ok"}

# Create a session
curl -s -XPOST http://127.0.0.1:8501/api/agent/sessions \
  -H 'content-type: application/json' \
  -d '{"project_root":"/abs/path/to/project"}' | jq

# Send a turn (uses your real OPENAI_API_KEY, so this costs money)
curl -s -XPOST http://127.0.0.1:8501/api/agent/sessions/<SID>/messages \
  -H 'content-type: application/json' \
  -d '{"project_root":"/abs/path/to/project","message":"List the files in this project","selected_targets":[]}' | jq .text
```

If you get a text response, the backend half works and any frontend issue is purely UI. If this fails (401, 500, etc.), the agent is broken regardless of which extension you load.

### 2.4 Common gotchas

1. **`atopile.enableChat = false`.** Default is `false` upstream. Your working copy has it patched to `true` in `package.json`, but only the dev-host build picks that up — the installed `v0.12.5` ignores it (it doesn't even have the agent code). Make sure your **dev host** is the active VS Code window.
2. **`sidebar.js` not built.** If `src/vscode-atopile/resources/webviews/sidebar.js` is missing, the sidebar renders `_getNotBuiltHtml()` instead (literal "not built" message). Run `npm run build:webviews`.
3. **`OPENAI_API_KEY` missing.** The agent's first turn raises `RuntimeError("No API key configured. Set OPENAI_API_KEY or ATOPILE_AGENT_OPENAI_API_KEY...")`. The UI shows this as a generic error toast. Check the backend stdout.
4. **Two extensions installed.** If both the marketplace `v0.12.5` and your dev-host build are active, VS Code picks one. Disable the marketplace one in the dev host (`code --disable-extension atopile.atopile`) or temporarily uninstall it.
5. **Stale webview bundle.** If you've edited `src/ui-server/src/` and your changes don't appear, you didn't re-run `npm run build:webviews`. There's no hot-reload from the React side into the webview unless you specifically run Vite dev mode and load the iframe URL — VS Code uses the *built* `sidebar.js`.

### 2.5 Tighter inner loop for UI changes

If you're iterating on the agent UI itself, skip the extension rebuild and use the browser:

```bash
ato serve backend --workspace ./examples --force          # terminal 1
ato serve frontend --backend localhost:8501               # terminal 2 → opens http://localhost:5173
```

The agent chat works in browser mode (it talks to `/api/agent/...` over HTTP/WS). What does **not** work in browser mode is the file explorer — see `13_KICAD_SCH_AND_FRONTEND_FILES.md`. For agent-only iteration that's fine; you only need the chat surface.

To force `ENABLE_CHAT=true` in browser mode, set the env var before `ato serve frontend`:

```bash
VITE_ENABLE_CHAT=true ato serve frontend --backend localhost:8501
```

…**only if** `config.ts` reads it from `import.meta.env`. Today (`src/ui-server/src/api/config.ts:63`) it only reads `window.__ATOPILE_ENABLE_CHAT__`, which is injected by the extension webview, not by Vite. So in browser mode the panel is hidden by default. Two-line fix: change line 63 of `config.ts` to `Boolean(win.__ATOPILE_ENABLE_CHAT__) || import.meta.env.VITE_ENABLE_CHAT === 'true'`. Worth doing in our fork for Tier-3 dev (`12_DEV_TEST_HARNESS.md`), upstream might accept it as well.

---

## 3. What to do if you just want the released look back

If the goal is "make my installed extension look like the screenshots":

- **Wait for the next release.** The agent UI is on `main` but not yet tagged/published. Check `https://marketplace.visualstudio.com/items?itemName=atopile.atopile` for the next version > 0.12.5.
- **Or VSIX-install your local build.** `cd src/vscode-atopile && npx vsce package` produces `atopile-0.0.0.vsix`. `code --install-extension atopile-0.0.0.vsix`. This gives you the agent UI in the *real* VS Code (no Extension Development Host needed). Remember to disable the marketplace extension to avoid conflicts.

---

## 4. Relevance to the EE-agent fork

For our Option C plan:

- **The Tier-3 dev loop in `12_DEV_TEST_HARNESS.md` assumed `ato serve frontend` opens a fully-working UI.** It doesn't — file explorer is broken in browser mode (doc 13) and chat is hidden by default in browser mode (this doc). Both are one-line fixes worth carrying in the fork. Quick win for M3.
- **The fork's reference UI experience is the Extension Development Host**, not browser mode. Document that in M3 instructions.
- **The released extension's lag tells us the EE-agent fork must not depend on the marketplace version of the UI.** We build the webview from source as part of our fork's release pipeline.

---

## 5. Cross-references

- `12_DEV_TEST_HARNESS.md` — add a note in Tier 3 ("UI is via Extension Development Host, not the marketplace `v0.12.5`; browser-mode chat requires the `ENABLE_CHAT` fallback patch in §2.5").
- `13_KICAD_SCH_AND_FRONTEND_FILES.md` — sibling doc on the file-explorer-in-browser issue. Together with this doc, they fully explain "the browser-served UI feels broken."
- `06_ATOPILE_INTEGRATION.md` — fork build pipeline; UI rebuild step must be explicit.
