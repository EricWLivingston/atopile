# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""Offline tests for the on-demand skill tools (`skills_list` / `skill_read`).

Covers discovery (skills listed, fixed three flagged, descriptions parsed), reading a
skill body on demand, path-traversal safety, and graceful degradation on unknown ids.
No API key, network, or build required.
"""

import asyncio
from pathlib import Path

from atopile.server.agent import tools
from atopile.server.agent._ee import tools_skills


def _list() -> dict:
    return asyncio.run(
        tools.execute_tool(
            name="skills_list", arguments={}, project_root=Path("."), ctx=None
        )
    )


def _read(skill_id: str) -> dict:
    return asyncio.run(
        tools.execute_tool(
            name="skill_read",
            arguments={"skill_id": skill_id},
            project_root=Path("."),
            ctx=None,
        )
    )


def test_skills_list_includes_known_and_new_skills():
    out = _list()
    assert out["ok"] is True
    ids = {s["id"] for s in out["skills"]}
    # always-loaded core, an existing domain skill, and our new guidance docs
    assert {"agent", "ato", "planning", "compiler", "pyspice_run"} <= ids
    assert out["count"] == len(out["skills"])


def test_skills_list_flags_always_loaded_and_has_descriptions():
    skills = {s["id"]: s for s in _list()["skills"]}
    assert skills["agent"]["always_loaded"] is True
    assert skills["compiler"]["always_loaded"] is False
    assert skills["pyspice_run"]["always_loaded"] is False
    # frontmatter description is parsed (non-empty for a well-formed skill)
    assert skills["pyspice_run"]["description"]


def test_skill_read_returns_pyspice_guidance_body():
    out = _read("pyspice_run")
    assert out["ok"] is True
    assert out["id"] == "pyspice_run"
    # the governor content is present
    assert "When NOT to use" in out["body"]
    assert "minimal subcircuit" in out["body"]


def test_skill_read_existing_core_skill():
    out = _read("agent")
    assert out["ok"] is True
    assert out["always_loaded"] is True
    assert out["body"]


def test_skill_read_unknown_degrades_with_available_list():
    out = _read("does_not_exist")
    assert out["ok"] is False
    assert "pyspice_run" in out["available"]


def test_skill_read_rejects_path_traversal():
    out = _read("../config")
    assert out["ok"] is False
    assert "invalid" in out["error"]


def test_parse_description_frontmatter_and_fallback():
    fm = '---\nname: x\ndescription: "Hello world."\n---\n\n# Title\nbody'
    assert tools_skills._parse_description(fm) == "Hello world."
    # no frontmatter -> first heading text
    assert tools_skills._parse_description("# Package Agent\n\ntext") == "Package Agent"
