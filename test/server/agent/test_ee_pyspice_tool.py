# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""Offline tests for the pyspice_run agent-tool wrapper (no ngspice / PySpice).

Covers the M5 wrapper contract: analysis validation, netlist/netlist_path resolution,
pass-through to the runner, and graceful degradation when the runner raises (missing
PySpice / libngspice). The agent must never crash on simulation.
"""

from __future__ import annotations

import asyncio

from atopile.server.agent._ee import tools_pyspice


def _call(arguments: dict) -> dict:
    return asyncio.run(tools_pyspice.run_pyspice(arguments))


def test_invalid_analysis_rejected():
    out = _call({"netlist": "R1 a 0 1k", "analysis": "fft"})
    assert out["success"] is False
    assert out["errors"][0]["type"] == "invalid_analysis"


def test_missing_netlist_and_path_rejected():
    out = _call({"analysis": "op"})
    assert out["success"] is False
    assert out["errors"][0]["type"] == "missing_netlist"


def test_missing_netlist_path_file_rejected(tmp_path):
    out = _call(
        {
            "analysis": "op",
            "netlist_path": str(tmp_path / "nope.cir"),
            "project_path": str(tmp_path),
        }
    )
    assert out["success"] is False
    assert out["errors"][0]["type"] == "missing_netlist"


def test_netlist_path_traversal_rejected(tmp_path):
    # A relative path escaping the project root must be refused (CODE_AUDIT B1),
    # before any file read or runner import.
    out = _call(
        {
            "analysis": "op",
            "netlist_path": "../../../../etc/passwd",
            "project_path": str(tmp_path),
        }
    )
    assert out["success"] is False
    assert out["errors"][0]["type"] == "invalid_path"


def test_absolute_netlist_path_outside_project_rejected(tmp_path):
    out = _call(
        {
            "analysis": "op",
            "netlist_path": "/etc/hosts",
            "project_path": str(tmp_path),
        }
    )
    assert out["success"] is False
    assert out["errors"][0]["type"] == "invalid_path"


def test_inline_netlist_passed_through_to_runner(monkeypatch):
    captured = {}

    def fake_simulate(*, netlist, analysis, params, probes, project_root):
        captured.update(
            netlist=netlist, analysis=analysis, params=params, probes=probes
        )
        return {"success": True, "analysis": analysis, "results": []}

    monkeypatch.setattr("ee_agent_spice.simulate", fake_simulate)
    out = _call(
        {
            "netlist": "V1 in 0 DC 5\nR1 in out 1k",
            "analysis": "tran",
            "params": {"t_step": "1us", "t_end": "1ms"},
            "probes": ["out"],
        }
    )
    assert out["success"] is True
    assert captured["analysis"] == "tran"
    assert captured["probes"] == ["out"]
    assert "V1 in 0 DC 5" in captured["netlist"]


def test_netlist_path_is_read(monkeypatch, tmp_path):
    deck = tmp_path / "deck.cir"
    deck.write_text("V1 in 0 DC 5\nR1 in out 1k")
    seen = {}

    def fake_simulate(*, netlist, **kw):
        seen["netlist"] = netlist
        return {"success": True, "results": []}

    monkeypatch.setattr("ee_agent_spice.simulate", fake_simulate)
    out = _call(
        {"analysis": "op", "netlist_path": str(deck), "project_path": str(tmp_path)}
    )
    assert out["success"] is True
    assert "R1 in out 1k" in seen["netlist"]


def test_runner_oserror_degrades_to_dependency_missing(monkeypatch):
    def boom(**kwargs):
        raise OSError("cannot load library 'libngspice.dylib'")

    monkeypatch.setattr("ee_agent_spice.simulate", boom)
    out = _call({"netlist": "R1 a 0 1k", "analysis": "op"})
    assert out["success"] is False
    assert out["errors"][0]["type"] == "dependency_missing"


def test_runner_unexpected_exception_degrades(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("kaboom at /Users/secret/internal.py")

    monkeypatch.setattr("ee_agent_spice.simulate", boom)
    out = _call({"netlist": "R1 a 0 1k", "analysis": "op"})
    assert out["success"] is False
    assert out["errors"][0]["type"] == "unexpected_error"
    # Q1: only the exception type, not the message body (a path here), is surfaced.
    assert out["errors"][0]["rationale"] == "RuntimeError"
    assert "secret" not in out["errors"][0]["rationale"]
