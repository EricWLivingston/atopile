# EE-Agent Feature Showcase — Runbook

A single agent run that exercises every fork feature on one coherent design — a
**precision DAC output-buffer board** — grounded in the real RAG corpus, then scored
with a read-only PASS/FAIL table.

```
5V in ──[Schottky diode D1]──[SPX3819 3V3 LDO]── 3V3 rail
                                                   │
        MCP4728 quad 12-bit DAC ── LMV324 Sallen-Key low-pass filter ── Vout
```

Files: `SHOWCASE_PROMPT.md` (the hero prompt), `scorecard.py` (the scorer), this runbook.
Target project: `examples/dac-buffer-demo/` (skeleton `ato.yaml` only — the agent writes
`dac-buffer.ato`).

---

## 1. Preconditions (one-time)

| # | Action | Status |
|---|---|---|
| 1 | `.env` (repo root): `EE_AGENT_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `COHERE_API_KEY` | already set ✅ |
| 2 | `.env`: **add `EE_AGENT_DYNAMIC_MODEL=1`** | **action** |
| 3 | `.env`: **remove / comment `ATOPILE_AGENT_MODEL=...`** (it pins one model and defeats routing → standard tier becomes Sonnet) | **action** |
| 3b | *(optional)* To demonstrate the **Opus** tier, set `ATOPILE_AGENT_MODEL_COMPLEX=claude-opus-4-8`. By default the complex tier is **Sonnet** (cost: a design runs as one turn, so routing to Opus would pin the whole run to Opus). Without this, the routing check passes on Haiku+Sonnet — Opus just won't appear. | optional |
| 4 | **Install ngspice** for live SPICE: `brew install ngspice` | **action — not currently installed** |
| 5 | RAG store ingested (`data/.chroma/chroma.sqlite3` + BM25 sidecar + caches) | already present ✅ |
| 6 | **Reload the VS Code window** after `.env` edits (backend reads env at import) | after edits |

> Without step 4, `pyspice_run` degrades gracefully (`success:false`) and the SPICE check
> fails — the agent still runs, but the sim feature won't be demonstrated.

## 2. (Optional) Pre-flight tool smoke

Prove each EE tool body works before the live run, so any miss is the agent's choice not a
broken tool. From the repo root with `.env` sourced (`set -a; . ./.env; set +a`):

```bash
uv run python -c "
import asyncio
from atopile.server.agent.tools import execute_tool
print(asyncio.run(execute_tool('rag_search', {'query':'SPX3819 output capacitor stability','top_k':5}, None, None)))
print(asyncio.run(execute_tool('pyspice_run', {'analysis':'ac',
  'netlist':'V1 in 0 AC 1\nR1 in out 1.6k\nC1 out 0 100n',
  'probes':['out']}, None, None)))
print(asyncio.run(execute_tool('skills_list', {}, None, None)))
"
```
(Adjust the `execute_tool` signature to the local one if it differs — see
`test/ee_agent_spice/test_sim_live.py` and `test/server/agent/test_ee_rag_tool.py` for the
canonical call shape.)

## 3. Run the showcase

1. Open `examples/dac-buffer-demo` as the workspace folder in VS Code.
2. Open the **Logs tab → Agent mode** (leave Session blank = follow-latest) to watch
   events stream live: `model_routed` (tier per turn), `rag_search`, `skill_read`,
   `pyspice_run`, `build_run`.
3. Paste the prompt from `SHOWCASE_PROMPT.md` into the **Agent panel** and send.
4. Let it run to a final summary (`run_completed`).

## 4. Score it

```bash
python ee_agent_docs_5_21/scorecard.py
```

Defaults to the latest session and the `dac-buffer-demo` project. Flags:
`--session <id>`, `--project <path>`, `--db <path>`, and `--trace` (after the
table, print a per-tool count summary, the chronological tool-call timeline with
each call's parameters, and the skills read — for auditing exactly what the agent
did). Expected output:

```
EE-Agent feature scorecard  session <id>
------------------------------------------------------------------------
  ✓ AnthropicProvider  claude-sonnet-4-6, claude-opus-4-8, claude-haiku-4-5-...
  ✓ Model routing      3 model tier(s): ...; N route event(s)
  ✓ rag_search         4 call(s), ~5 citation field(s) returned
  ✓ Skill discovery    2 call(s) (skill_read x2)
  ✓ pyspice_run        2 call(s); 1 sim .npz file(s)
  ✓ Diode auto-pick    picked: D1=<MPN>
  ✓ Schematic emit     2 .kicad_sch file(s): default.kicad_sch, default-...kicad_sch
  ✓ Run health         1 run_completed, no failures
------------------------------------------------------------------------
  8/8 features demonstrated
```

Any `✗` row prints the concrete reason (e.g. *"tool ran but no .npz — libngspice missing"*),
so a partial result tells you exactly what to fix.

## 5. Eyeball the artifacts

- `examples/dac-buffer-demo/build/builds/default/default.kicad_sch` → open in KiCad; confirm
  the diode, LDO, DAC, and op-amp appear with real symbols.
- `examples/dac-buffer-demo/build/builds/default/default.bom.{json,csv}` → the auto-picked
  diode is the row with `type:"diode"`, `source:"picked"`.
- `examples/dac-buffer-demo/build/**/sim/*.npz` → the persisted SPICE waveforms.
- Optional ERC: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli sch erc
  examples/dac-buffer-demo/build/builds/default/default.kicad_sch` → expect 0 errors.

## What each check proves

| Check | Feature |
|---|---|
| AnthropicProvider | the fork's `AnthropicProvider` served the run (`claude-*` models) |
| Model routing | per-turn Haiku/Sonnet/Opus tiering (>=2 tiers in one session) |
| rag_search | hybrid retrieval over the ingested corpus, with citations |
| Skill discovery | `skills_list`/`skill_read` just-in-time guidance |
| pyspice_run | agent-authored SPICE sim ran and persisted a `.npz` |
| Diode auto-pick | constraint-only `new Diode` resolved a real part (ghost-fix) |
| Schematic emit | `generate_schematic` build step produced `.kicad_sch` |
| Run health | clean `run_completed`, no `run_failed` |
