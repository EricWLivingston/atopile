# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""Offline unit tests for the SPICE runner's pure logic (no ngspice / PySpice).

Covers deck assembly, probe save-target spelling, probe→vector-key resolution, summary
stats, and ngspice-log error classification. The live simulation path is in
``test_sim_live.py`` (skipped without libngspice).
"""

from __future__ import annotations

import numpy as np
import pytest

from ee_agent_spice.errors import classify_log
from ee_agent_spice.persist import run_id, summarise
from ee_agent_spice.runner import (
    _control_card,
    _resolve,
    _save_target,
    _strip_terminators,
    build_deck,
)


# --- deck assembly -------------------------------------------------------------------
def test_build_deck_has_title_models_control_and_end():
    deck = build_deck("V1 in 0 DC 5\nR1 in out 1k", "op", {}, ["out"])
    lines = deck.splitlines()
    assert lines[0].startswith("*")  # ngspice swallows the first line as a title
    assert "V1 in 0 DC 5" in deck
    assert ".include" in deck and "ee_agent.lib" in deck
    assert ".save v(out)" in deck
    assert lines[-1] == ".end"
    assert ".op" in deck


def test_build_deck_strips_agent_supplied_terminator():
    # the agent must not add .end; if it does we drop it and own the terminator.
    deck = build_deck("V1 in 0 DC 5\n.end", "op", {}, [])
    assert deck.count(".end") == 1


@pytest.mark.parametrize(
    ("analysis", "params", "expected"),
    [
        ("op", {}, ".op"),
        ("dc", {}, ".op"),  # dc with no sweep source degrades to operating point
        ("dc", {"source": "V1", "start": 0, "stop": 5, "step": 1}, ".dc V1 0 5 1"),
        (
            "ac",
            {"variation": "dec", "n_points": 10, "f_start": 1, "f_stop": 1e6},
            ".ac dec 10 1 1000000.0",
        ),
        ("tran", {"t_step": "10us", "t_end": "10ms"}, ".tran 10us 10ms"),
        ("tran", {"t_step": "1us", "t_end": "1ms", "uic": True}, ".tran 1us 1ms uic"),
    ],
)
def test_control_card(analysis, params, expected):
    assert _control_card(analysis, params) == expected


def test_strip_terminators_drops_end_and_endc():
    assert _strip_terminators("R1 a b 1k\n.end\n") == "R1 a b 1k"
    assert _strip_terminators("R1 a b 1k\n.ENDC") == "R1 a b 1k"


# --- probe spelling ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("probe", "target"),
    [
        ("out", "v(out)"),
        ("v(out)", "v(out)"),
        ("i(v1)", "i(v1)"),
        ("v1#branch", "v1#branch"),
    ],
)
def test_save_target(probe, target):
    assert _save_target(probe) == target


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ("out", "out"),            # bare node, exact key
        ("v(out)", "out"),         # voltage spelling -> node key
        ("V(OUT)", "out"),         # case-insensitive
        ("i(v1)", "v1#branch"),    # source current -> branch key
        ("nope", None),            # unknown -> not resolved
    ],
)
def test_resolve(probe, expected):
    keys = ["out", "in", "v1#branch", "time"]
    assert _resolve(probe, keys) == expected


# --- summaries -----------------------------------------------------------------------
def test_summarise_real_vectors():
    out = summarise({"out": np.array([1.0, 2.0, 3.0])})
    assert out == [
        {
            "probe": "out",
            "summary": {"min": 1.0, "max": 3.0, "mean": 2.0},
            "n_samples": 3,
        }
    ]


def test_summarise_complex_uses_magnitude():
    out = summarise({"vout": np.array([3 + 4j])})  # |3+4j| = 5
    assert out[0]["summary"]["max"] == pytest.approx(5.0)


def test_run_id_is_deterministic_and_analysis_sensitive():
    assert run_id("deck", "tran") == run_id("deck", "tran")
    assert run_id("deck", "tran") != run_id("deck", "ac")


# --- error classification ------------------------------------------------------------
@pytest.mark.parametrize(
    ("log", "err_type"),
    [
        ("Warning: singular matrix: check node b", "convergence_failure"),
        ("Error: no convergence in transient analysis", "convergence_failure"),
        ("timestep too small", "convergence_failure"),
        ("unknown subckt OPAMP", "model_error"),
        ("syntax error in line 3", "parse_error"),
        ("everything is fine", "unknown"),
    ],
)
def test_classify_log(log, err_type):
    assert classify_log(log)[0] == err_type
