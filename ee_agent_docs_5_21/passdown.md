# EE Agent — Option C Passdown

> **Purpose.** Track progress of EE agent docs and implementation under **Option C** from `09_HARNESS_ANALYSIS.md`: fork atopile, add `AnthropicProvider` next to `OpenAIProvider` (both supported, switchable via config), reuse atopile's harness as-is, and add four custom tools: **`rag_search`, `pyspice_run`, `pinmux_check`, `ipc_check`**. Out of scope: BOM tool, thermal tool, deepagents, LangGraph state machines.

---

## Current state (compressed history of Sessions 1–2)

### Decisions locked in

- **Fork atopile, don't replace its harness.** Runner (`src/atopile/server/agent/runner.py`, 2,891 LoC) is ~81% provider-agnostic; we keep it. `LLMProvider` is already a `Protocol`, so swapping providers is the natural extension point.
- **Dual provider support.** `AnthropicProvider` lives at `src/atopile/server/agent/_ee/provider_anthropic.py`. Switch via `EE_AGENT_PROVIDER=openai|anthropic` (default openai). Both must stay green in CI.
- **Skills work as-shipped** — no changes to `.claude/skills/*/SKILL.md`.
- **Four custom tools** register through the existing `_register_tool` decorator in `tools.py`, surfaced via `ToolRegistry()` in `routes/agent/utils.py`.

### Doc set (`ee_agent_docs_5_21/`) — all written

| File | Role |
|---|---|
| `00_ARCHITECTURE.md` | Option C overview |
| `01_ORCHESTRATOR.md` | Runner IS orchestrator (short) |
| `02_SIMULATION.md` | `pyspice_run` spec |
| `03_LAYOUT.md` | Deprecation note |
| `04_VERIFICATION.md` | `ipc_check` + `pinmux_check` |
| `05_RAG.md`, `RAG_IMPLEMENTATION_PLAN.md`, `INGESTION_*.md` | RAG corpus + pipeline |
| `06_ATOPILE_INTEGRATION.md` | Fork mechanics (2 minimal upstream edits planned in M2) |
| `07_ATOPILE_GAPS.md`, `09_HARNESS_ANALYSIS.md`, `10_LANGCHAIN_FORK_ANALYSIS.md` | Background analysis |
| `08_PROJECT_PLAN.md` | 8 milestones (M1 fork → M8 release) |
| `11_ANTHROPIC_PROVIDER.md` | ~400 LoC `AnthropicProvider` reference impl, config diff, route diff, parity test plan |
| `12_DEV_TEST_HARNESS.md` | 3-tier test plan (unit / replay / live) + `LoggingProvider` decorator |
| `13_KICAD_SCH_AND_FRONTEND_FILES.md` | Audit: no KiCad schematic emitter today; `ato serve frontend` file explorer is no-op in browser |
| `14_AGENT_VS_RELEASED_EXTENSION.md` | Why marketplace `v0.12.5` lacks the agent UI + how to run source build |
| `passdown.md` | This file |

### Key source-code seams identified

- **Runner DI**: `provider`, `registry`, `config`. Monkeypatch seams: `runner.build_system_prompt`, `runner.build_initial_user_message`. Upstream's in-file `TestRunner` (`runner.py:2124–2891`) gives us 7 reusable invariant tests via `_StubProvider`/`_StubRegistry`.
- **Runner instantiated as module-level singleton** in `routes/agent/utils.py:62–67` from `AgentConfig.from_env()` at import time. Implication: env changes after server start don't propagate; tests must not import `routes.agent.utils`.
- **Agent logs**: every turn writes structured events to `~/.atopile/agent_logs.sqlite` (`model/sqlite.py::AgentLogs`) — primary post-mortem tool.
- **Test runner**: `ato dev test --llm` produces `artifacts/test-report.{json,html,llm.json}`. Supports `-k`, `--baseline`, `--direct`, `--reuse`.
- **Backend boot**: `ato serve backend` (port 8501) → `atopile.server.server.run_server` → FastAPI + Pydantic→TS type gen + event bus. `ato serve frontend` (port 5173) runs Vite from `src/ui-server/`.

### Known limitations (carry into Session 3+ planning)

- No KiCad schematic emitter on disk — Zig sexp model is complete but no Python emitter exists. Add to `00_ARCHITECTURE.md` §5 exclusions; flag in `07_ATOPILE_GAPS.md` as upstream contribution.
- Browser `ato serve frontend` file explorer is a no-op (calls `postToExtension` which only works inside a VS Code webview). Documented as Tier-3 caveat in `12_DEV_TEST_HARNESS.md`.
- These three follow-up doc edits were queued at end of Session 2 and **still pending**:
  1. Amend `00_ARCHITECTURE.md` §5 with schematic exclusion.
  2. Append schematic-emitter candidate to `07_ATOPILE_GAPS.md`.
  3. Append Tier-3 file-explorer caveat to `12_DEV_TEST_HARNESS.md` §2.

---

## Session 3 (2026-06-02) — Local VS Code extension install + dev-loop calibration

Goal: get the unreleased agent sidebar UI running in the user's real VS Code (not the F5 Extension Development Host), and clarify which kinds of source changes flow through which build path.

### What was done

**Built and installed the VSIX locally.** Followed `14_AGENT_VS_RELEASED_EXTENSION.md` §3. Sequence:

1. `cd src/vscode-atopile && npm install` → idempotent, deps already present (`@vscode/vsce@^3.2.1` confirmed in devDeps).
2. `npm run build:webviews` → React/Vite bundle to `src/vscode-atopile/resources/webviews/sidebar.js` (1.18 MB).
3. `npm run compile` → webpack to `dist/extension.js` (1 benign warning re `vscode-languageserver-types` dynamic require).
4. `npx vsce package` → `src/vscode-atopile/atopile-0.0.0.vsix` (91 files, 4.75 MB). Confirmed `extension/resources/webviews/sidebar.js` ships in the archive (validates that `.vscodeignore` line 27 `webviews/**` does *not* strip `resources/webviews/`).
5. `code --install-extension atopile-0.0.0.vsix` → succeeded. `code --list-extensions --show-versions` now shows `atopile.atopile@0.0.0`.

**Marketplace conflict turned out to be a non-issue.** The `~/.vscode/extensions/atopile.atopile-0.12.5/` directory exists but is *not* registered in `extensions.json` — it's an orphan. CLI saw no installed atopile extension pre-install. The orphan dir is harmless; user can `rm -rf` whenever.

**Environment quirks discovered, not yet fixed**:

- `/usr/local/bin/code` is a broken symlink pointing at a stale `/private/var/folders/.../AppTranslocation/<old-id>/...` path. The currently-running VS Code is at a *different* translocation ID, so each launch could in principle reshuffle.
- Root cause: `Visual Studio Code.app` is not in `/Applications`. macOS Gatekeeper translocates quarantined apps run from outside `/Applications` to ephemeral mounts.
- Permanent fix (documented for user): quit VS Code → move `.app` into `/Applications` → `xattr -dr com.apple.quarantine` → relaunch → run "Shell Command: Install 'code' command in PATH" from the command palette.
- For this session, used the running translocation path directly to call `code --install-extension`.

**Verified Python install mode**: `uv pip show atopile` →
```
Location: /Users/ericlivingston/atopile/.venv/lib/python3.14/site-packages
Editable project location: /Users/ericlivingston/atopile
```
Editable install confirmed. Edits under `src/atopile/**` will take effect on next backend restart with no reinstall. Caveat: `ato` is not on the shell PATH (`.venv` not auto-activated) — fine for the extension (it spawns its own backend) but smoke-testing from the terminal needs `source .venv/bin/activate` or `uv run ato …`.

**Skimmed `11_ANTHROPIC_PROVIDER.md` against current source** to map the actual edit surface for the upcoming `AnthropicProvider` work. Three files, all Python:

| File | Change | LoC |
|---|---|---|
| `src/atopile/server/agent/_ee/provider_anthropic.py` | **New**, implements `LLMProvider` Protocol; translates OpenAI ↔ Anthropic message/tool shapes; client-side compaction (Anthropic has no `responses.compact`) | ~400 |
| `src/atopile/server/agent/config.py` | Add `provider: str = "openai"`; branch on provider for `api_key`/`base_url`/`default_model`; new env vars `ANTHROPIC_API_KEY` / `ATOPILE_AGENT_ANTHROPIC_API_KEY` | ~30 |
| `src/atopile/server/routes/agent/utils.py` | Replace `OpenAIProvider(...)` with `_make_provider(_config)` that switches on `config.provider` | ~10 |

Default model: `claude-sonnet-4-6`. Defaults preserve OpenAI behavior, so M2 is backward-compatible.

### Dev-loop reference (established this session)

| Change type | Rebuild required | How to apply |
|---|---|---|
| Python: provider, tools, config, routes (`src/atopile/**`) | None — editable install picks up edits | Reload VS Code window (extension respawns backend on activation) |
| Extension host TypeScript (`src/vscode-atopile/src/**`) | `npm run compile` | Reload window |
| Webview React (`src/ui-server/src/**`) | `npm run build:webviews` | Reload window |
| Ship a new VSIX (persists across machines / shells) | All of the above, then `npx vsce package` + `code --install-extension` | One-time per release |

### What this session did NOT do

- **No source files modified.** Only artifact created: `src/vscode-atopile/atopile-0.0.0.vsix` (gitignored build output) and corresponding install in `~/.vscode/extensions/atopile.atopile-0.0.0/`.
- Did not move `Visual Studio Code.app` to `/Applications` or re-link the `code` CLI — left for user to perform.
- Did not delete the orphan `~/.vscode/extensions/atopile.atopile-0.12.5/` directory.
- Did not trace `ToolRegistry` registration site — flagged as the next investigation if user wants to start adding agent tools.
- Did not start M1/M2/M3 implementation work. Plan-only.
- Did not apply the three pending Session-2 doc follow-up edits (still queued — see "Known limitations" above).

### Plan file written

`/Users/ericlivingston/.claude/plans/i-want-the-vsix-shimmering-minsky.md` — full VSIX install plan with rollback steps. Approved and executed.

---

## Pick-up checklist for next session

1. **User-side environment cleanup** (one-time, not blocking implementation):
   - Move `Visual Studio Code.app` to `/Applications`, strip quarantine xattr, re-link `code` CLI.
   - Optionally `rm -rf ~/.vscode/extensions/atopile.atopile-0.12.5/`.
2. **Apply the three queued Session-2 doc edits** (schematic exclusion in `00_ARCHITECTURE.md` §5, schematic-emitter candidate in `07_ATOPILE_GAPS.md`, Tier-3 caveat in `12_DEV_TEST_HARNESS.md` §2).
3. **Decide where the fork lives** (`08_PROJECT_PLAN.md` M1): GitHub fork vs in-repo branch.
4. **Start M2 — `AnthropicProvider`** per `11_ANTHROPIC_PROVIDER.md`:
   - Create `src/atopile/server/agent/_ee/__init__.py` + `provider_anthropic.py` skeleton.
   - Patch `config.py` + `routes/agent/utils.py` per Session-3 edit-surface table above.
   - Add `anthropic` to `pyproject.toml` deps if not present.
5. **Start M3 — test plumbing** per `12_DEV_TEST_HARNESS.md` §3:
   - `tests/ee/conftest.py` (paste from doc §2).
   - `tests/ee/test_runner_loop.py` with one passing `final_answer` smoke test.
   - Add `tests/ee` to `pyproject.toml::testpaths`.
   - Confirm `ato dev test --llm -k ee` discovers it.
   - Port remaining 11 invariants from upstream's `TestRunner`.
6. **Wire `LoggingProvider`** (doc §4) into `routes/agent/utils.py` behind `EE_AGENT_LOG_PROVIDER=1` for Tier-2 transcript capture.
7. **First captured transcript**: tiny "read main.ato" prompt against `OpenAIProvider`, drop into `tests/ee/transcripts/smoke/turn_001.json`, write replay test. Validates the round-trip before M4–M7 generate dozens.
8. **Tools investigation** (deferred from this session): trace `ToolRegistry` + `_register_tool` to identify the exact files where the four custom tools (`rag_search`, `pyspice_run`, `pinmux_check`, `ipc_check`) get added.
9. **Open questions still pending**:
   - Keep singleton `AgentRunner` or move to per-request? (Default: keep.)
   - Git-track transcripts under `tests/ee/transcripts/`? (Default: yes.)
   - Build `ato dev replay <transcript>` CLI? (Defer.)

---

## Progress log (cumulative)

- [done] Sessions 1–2 — full doc set written (`00`–`14` + RAG + ingestion + passdown). No source modified.
- [done] Session 3 — local VSIX built and installed; dev-loop reference table established; AnthropicProvider edit surface verified against current source. No source modified.
- [next] M1 fork decision → M2 `AnthropicProvider` → M3 test plumbing.
