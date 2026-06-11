"""On-demand skill discovery for the agent (skills_list + skill_read).

The agent always-loads only the three fixed_skill_ids (agent/ato/planning); every other
skill under config.skills_dir is otherwise invisible to it. These two tools expose the
whole library on demand: skills_list enumerates what exists (id + description, the
always-loaded three flagged); skill_read fetches one body when the agent needs it.
Zero per-turn cost; guidance is pulled just-in-time.

This is also the extension point for per-tool guidance: dropping
.claude/skills/<tool>/SKILL.md makes it discoverable and readable here, no code change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from atopile.server.agent.config import AgentConfig
from atopile.server.agent.orchestrator_helpers import _truncate_middle

# Skill ids are directory names; constrain to a safe charset so ``skill_read`` can't be
# steered outside the skills dir (no ``/`` or ``..``).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_BODY_MAX_CHARS = 12_000


def _skills_dir() -> Path:
    return AgentConfig().skills_dir


def _fixed_ids() -> set[str]:
    return set(AgentConfig().fixed_skill_ids)


def _parse_description(text: str) -> str:
    """One-line description from a SKILL.md: YAML frontmatter, else first heading."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.lower().startswith("description:"):
                value = line.split(":", 1)[1].strip()
                return value.strip().strip('"').strip("'").strip()
    # No frontmatter description — fall back to the first heading or non-empty line.
    for line in lines:
        stripped = line.strip()
        if stripped and stripped != "---":
            return stripped.lstrip("#").strip()
    return ""


def list_skills() -> list[dict[str, Any]]:
    """All ``<id>/SKILL.md`` under the skills dir, sorted; fixed ids flagged."""
    skills_dir = _skills_dir()
    fixed = _fixed_ids()
    out: list[dict[str, Any]] = []
    if not skills_dir.is_dir():
        return out
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        skill_md = child / "SKILL.md"
        if not (child.is_dir() and skill_md.is_file()):
            continue
        try:
            description = _parse_description(skill_md.read_text(encoding="utf-8"))
        except OSError:
            description = ""
        out.append(
            {
                "id": child.name,
                "description": description,
                "always_loaded": child.name in fixed,
            }
        )
    return out


def read_skill(skill_id: str) -> dict[str, Any]:
    """Return one skill's body, or a graceful error listing the available ids."""
    skill_id = (skill_id or "").strip()
    if not skill_id or not _SAFE_ID.match(skill_id):
        return {
            "ok": False,
            "error": f"invalid skill_id {skill_id!r}",
            "available": [s["id"] for s in list_skills()],
        }
    skill_md = _skills_dir() / skill_id / "SKILL.md"
    if not skill_md.is_file():
        return {
            "ok": False,
            "error": f"skill {skill_id!r} not found",
            "available": [s["id"] for s in list_skills()],
        }
    body = skill_md.read_text(encoding="utf-8").strip()
    return {
        "ok": True,
        "id": skill_id,
        "always_loaded": skill_id in _fixed_ids(),
        "body": _truncate_middle(body, _BODY_MAX_CHARS),
    }


async def run_skills_list(arguments: dict[str, Any]) -> dict[str, Any]:
    """Async entry for the ``skills_list`` handler."""
    try:
        skills = list_skills()
        return {"ok": True, "count": len(skills), "skills": skills}
    except Exception as e:  # noqa: BLE001 - tool must not crash the run
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "skills": []}


async def run_skill_read(arguments: dict[str, Any]) -> dict[str, Any]:
    """Async entry for the ``skill_read`` handler."""
    try:
        return read_skill(arguments.get("skill_id", ""))
    except Exception as e:  # noqa: BLE001 - tool must not crash the run
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
