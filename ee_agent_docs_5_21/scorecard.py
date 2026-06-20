#!/usr/bin/env python3
"""EE-Agent feature showcase scorecard (read-only).

Proves that one agent run exercised every fork feature, by querying the agent run log
(`~/Library/Logs/atopile/agent_logs.db`) and the project build artifacts. Prints a
human-readable PASS/FAIL table — no side effects, no new deps (stdlib only).

Usage:
    python ee_agent_docs_5_21/scorecard.py                 # latest session, default project
    python ee_agent_docs_5_21/scorecard.py --session <id>
    python ee_agent_docs_5_21/scorecard.py --project examples/dac-buffer-demo
    python ee_agent_docs_5_21/scorecard.py --trace         # + tool/param/skill timeline

With --trace, after the PASS/FAIL table it prints a per-tool count summary, the
chronological tool-call timeline (each call's parameters), and the skills read —
so a run is fully auditable, not just scored.

The eight checks map 1:1 to the showcase plan:
    AnthropicProvider · Model routing · rag_search · skills · pyspice_run ·
    diode auto-pick · schematic emit · run health
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Mirrors model/sqlite.py: agent log DB on macOS lives under ~/Library/Logs/atopile/.
DEFAULT_DB = Path.home() / "Library" / "Logs" / "atopile" / "agent_logs.db"
DEFAULT_PROJECT = Path(__file__).resolve().parents[1] / "examples" / "dac-buffer-demo"

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


# ----------------------------------------------------------------------------- log access
def connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(f"agent log DB not found: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def latest_session(con: sqlite3.Connection) -> str | None:
    row = con.execute(
        "SELECT session_id FROM agent_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["session_id"] if row else None


def events(con: sqlite3.Connection, session: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM agent_events WHERE session_id = ? ORDER BY id", (session,)
    ).fetchall()


def tool_calls(rows: list[sqlite3.Row], *names: str) -> list[sqlite3.Row]:
    wanted = set(names)
    return [
        r
        for r in rows
        if r["tool_name"] in wanted and (r["event"] or "").endswith("tool_call_completed")
    ]


# ------------------------------------------------------------------------------- checks
def check_provider(rows) -> Check:
    models = sorted({r["model"] for r in rows if r["model"] and r["model"].startswith("claude-")})
    return Check(
        "AnthropicProvider",
        bool(models),
        ", ".join(models) if models else "no claude-* model on any turn",
    )


def check_routing(rows) -> Check:
    tiers = sorted({r["model"] for r in rows if r["model"]})
    # explicit route telemetry, if it persisted (step_kind column or run_progress payload)
    routed = [
        r
        for r in rows
        if r["step_kind"] == "model_routed"
        or (r["payload"] and '"model_routed"' in r["payload"])
    ]
    # A non-routed run uses one fixed model for the whole session, so >=2 distinct tiers
    # in a single session is itself conclusive proof routing fired. Route events corroborate.
    ok = len(tiers) >= 2 or bool(routed)
    detail = f"{len(tiers)} model tier(s): {', '.join(tiers) or 'none'}; {len(routed)} route event(s)"
    if not ok:
        detail += "  (need >=2 tiers — is EE_AGENT_DYNAMIC_MODEL=1 and the ATOPILE_AGENT_MODEL pin removed?)"
    return Check("Model routing", ok, detail)


def check_rag(rows) -> Check:
    calls = tool_calls(rows, "rag_search")
    cites = 0
    for c in calls:
        if c["payload"]:
            # citations surface as "citation"/"source"/"mpn" keys in the result payload
            cites += c["payload"].count('"citation"') + c["payload"].count('"source"')
    # The logged tool-result payloads are truncated, so the citation count is a
    # best-effort lower bound — a 0 here means "couldn't see them in the log", not
    # "rag returned none". The check passes on the call count alone.
    cite_note = f"~{cites} citation field(s) seen in log" if cites else "citations truncated in log"
    return Check(
        "rag_search",
        bool(calls),
        f"{len(calls)} call(s), {cite_note}",
    )


def check_skills(rows) -> Check:
    calls = tool_calls(rows, "skills_list", "skill_read")
    read = [c for c in calls if c["tool_name"] == "skill_read"]
    return Check(
        "Skill discovery",
        bool(calls),
        f"{len(calls)} call(s) (skill_read x{len(read)})",
    )


def check_pyspice(rows, project: Path) -> Check:
    calls = tool_calls(rows, "pyspice_run")
    npz = sorted(glob.glob(str(project / "build" / "**" / "sim" / "*.npz"), recursive=True))
    npz += sorted(glob.glob(str(project / "**" / "sim" / "*.npz"), recursive=True))
    npz = sorted(set(npz))
    ok = bool(calls) and bool(npz)
    detail = f"{len(calls)} call(s); {len(npz)} sim .npz file(s)"
    if calls and not npz:
        detail += "  (tool ran but no .npz — likely libngspice missing; `brew install ngspice`)"
    return Check("pyspice_run", ok, detail)


def find_boms(project: Path) -> list[Path]:
    return [Path(p) for p in glob.glob(str(project / "build" / "**" / "*.bom.json"), recursive=True)]


def _looks_like_diode(c: dict) -> bool:
    """A BOM row is a diode if typed so, OR (since the picker classifies Schottkys as
    type 'other') if it carries a diode-only `forward_voltage` parameter or a D# ref."""
    if (c.get("type") or "").lower() == "diode":
        return True
    if any((p.get("name") == "forward_voltage") for p in c.get("parameters", [])):
        return True
    desigs = [u.get("designator", "") for u in c.get("usages", [])]
    return any(d[:1] == "D" and d[1:].isdigit() for d in desigs)


def check_diode(project: Path) -> Check:
    diodes = []
    for bom in find_boms(project):
        try:
            data = json.loads(bom.read_text())
        except Exception:
            continue
        for c in data.get("components", []):
            if c.get("source") == "picked" and _looks_like_diode(c):
                des = ",".join(u.get("designator", "?") for u in c.get("usages", []))
                diodes.append(f"{des}={c.get('mpn')}")
    return Check(
        "Diode auto-pick",
        bool(diodes),
        ("picked: " + "; ".join(diodes)) if diodes else "no auto-picked (source=picked) diode in BOM",
    )


def check_schematic(project: Path) -> Check:
    schs = sorted(glob.glob(str(project / "build" / "**" / "*.kicad_sch"), recursive=True))
    return Check(
        "Schematic emit",
        bool(schs),
        f"{len(schs)} .kicad_sch file(s): {', '.join(Path(s).name for s in schs)}" if schs else "no .kicad_sch generated",
    )


def check_health(rows) -> Check:
    failed = [r for r in rows if r["event"] == "run_failed"]
    completed = [r for r in rows if r["event"] == "run_completed"]
    ok = bool(completed) and not failed
    if failed:
        detail = f"{len(failed)} run_failed event(s) — check the log"
    elif completed:
        detail = f"{len(completed)} run_completed, no failures"
    else:
        detail = "no run_completed yet (still running or interrupted?)"
    return Check("Run health", ok, detail)


# -------------------------------------------------------------------------------- render
def render(session: str, checks: list[Check]) -> None:
    width = max(len(c.name) for c in checks)
    print(f"\n{BOLD}EE-Agent feature scorecard{RESET}  {DIM}session {session}{RESET}")
    print("-" * 72)
    for c in checks:
        mark = f"{GREEN}✓{RESET}" if c.passed else f"{RED}✗{RESET}"
        print(f"  {mark} {c.name.ljust(width)}  {DIM}{c.detail}{RESET}")
    print("-" * 72)
    n = sum(c.passed for c in checks)
    color = GREEN if n == len(checks) else RED
    print(f"  {color}{BOLD}{n}/{len(checks)} features demonstrated{RESET}\n")


# ------------------------------------------------------------------------- trace view
def _compact_args(arguments: object, *, max_value_chars: int = 60) -> str:
    """Render a tool's ``arguments`` dict as compact ``k=v`` pairs.

    Long string values (e.g. a SPICE ``netlist`` or a search ``query``) are truncated
    so a single call never floods the terminal. Non-dict / missing args render empty.
    """
    if not isinstance(arguments, dict) or not arguments:
        return ""
    parts: list[str] = []
    for key, value in arguments.items():
        if isinstance(value, str):
            shown = value.replace("\n", " ")
            if len(shown) > max_value_chars:
                shown = shown[: max_value_chars - 1] + "…"
            rendered = repr(shown)
        elif isinstance(value, (list, dict)):
            rendered = f"<{type(value).__name__}:{len(value)}>"
        else:
            rendered = repr(value)
        parts.append(f"{key}={rendered}")
    return ", ".join(parts)


def _completed_tool_calls(rows: list[sqlite3.Row]) -> list[dict]:
    """Chronological list of completed tool calls with parsed payload fields.

    Falls back to the row's own ``tool_name`` column when the payload is missing or
    truncated (logged payloads can be cut off), so the timeline is never empty.
    """
    calls: list[dict] = []
    for r in rows:
        if not (r["event"] or "").endswith("tool_call_completed"):
            continue
        info: dict = {
            "tool_name": r["tool_name"],
            "loop": r["loop"],
            "ok": None,
            "args": "",
            "args_ok": True,
        }
        if r["payload"]:
            try:
                p = json.loads(r["payload"])
                info["tool_name"] = p.get("tool_name", info["tool_name"])
                info["loop"] = p.get("loop", info["loop"])
                info["ok"] = p.get("ok")
                info["args"] = _compact_args(p.get("arguments"))
            except (ValueError, TypeError):
                info["args_ok"] = False
        calls.append(info)
    return calls


def render_trace(rows: list[sqlite3.Row]) -> None:
    """Detailed audit of one run: per-tool counts, the call timeline, and skills read."""
    calls = _completed_tool_calls(rows)

    print(f"\n{BOLD}Tool-call trace{RESET}  {DIM}{len(calls)} completed call(s){RESET}")
    print("-" * 72)

    # Per-tool summary count.
    counts: dict[str, int] = {}
    for c in calls:
        counts[c["tool_name"] or "?"] = counts.get(c["tool_name"] or "?", 0) + 1
    summary = "  ".join(
        f"{name}×{n}" for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    print(f"  {DIM}{summary or 'no tool calls'}{RESET}\n")

    # Chronological timeline with parameters.
    for c in calls:
        loop = f"L{c['loop']:>3}" if c["loop"] is not None else "L  ?"
        if c["ok"] is True:
            status = f"{GREEN}ok{RESET}"
        elif c["ok"] is False:
            status = f"{RED}err{RESET}"
        else:
            status = f"{DIM}·{RESET}"
        args = c["args"] if c["args_ok"] else "(args unavailable)"
        print(f"  {DIM}{loop}{RESET} {status} {c['tool_name']}({DIM}{args}{RESET})")

    # Skills called — surfaced explicitly (the table only shows the count).
    skill_reads = [c for c in calls if c["tool_name"] == "skill_read"]
    listed = any(c["tool_name"] == "skills_list" for c in calls)
    print(f"\n{BOLD}Skills called{RESET}")
    print("-" * 72)
    print(f"  skills_list: {'yes' if listed else 'no'}")
    if skill_reads:
        skill_counts: dict[str, int] = {}
        for c in skill_reads:
            # skill id lives in the args string as skill_id='<id>'
            m = re.search(r"skill_id='([^']*)'", c["args"])
            sid = m.group(1) if m else "?"
            skill_counts[sid] = skill_counts.get(sid, 0) + 1
        for sid, n in sorted(skill_counts.items()):
            suffix = f" ×{n}" if n > 1 else ""
            print(f"  skill_read: {sid}{suffix}")
    else:
        print("  skill_read: none")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", help="session_id (default: latest)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    ap.add_argument(
        "--trace",
        action="store_true",
        help="also print the tool-call timeline (with parameters) and skills called",
    )
    args = ap.parse_args()

    if not os.isatty(1):  # disable color when piped
        globals().update(GREEN="", RED="", DIM="", BOLD="", RESET="")

    con = connect(args.db)
    session = args.session or latest_session(con)
    if not session:
        raise SystemExit("no sessions found in the agent log")
    rows = events(con, session)

    checks = [
        check_provider(rows),
        check_routing(rows),
        check_rag(rows),
        check_skills(rows),
        check_pyspice(rows, args.project),
        check_diode(args.project),
        check_schematic(args.project),
        check_health(rows),
    ]
    render(session, checks)
    if args.trace:
        render_trace(rows)
    return 0 if all(c.passed for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
