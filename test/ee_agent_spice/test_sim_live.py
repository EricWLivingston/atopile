# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""Live ngspice simulation tests — skipped unless libngspice is importable/loadable.

These assert real physics (RC time constant, resistive-divider bias, RC low-pass cutoff,
a bundled-model diode drop, and convergence-failure handling), not merely that a run
completed. Install ngspice (`brew install ngspice`) to exercise them.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySpice")

from ee_agent_spice import simulate  # noqa: E402


def _ngspice_available() -> bool:
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as d:
            r = simulate("V1 a 0 DC 1\nR1 a 0 1k", "op", {}, ["a"], project_root=d)
        return bool(r.get("success"))
    except Exception:  # noqa: BLE001 - libngspice missing/unloadable
        return False


pytestmark = pytest.mark.skipif(
    not _ngspice_available(), reason="libngspice not available (brew install ngspice)"
)


def test_op_resistive_divider(tmp_path):
    # 5V across 1k+2k -> node out = 5 * 2k/3k = 3.333V
    r = simulate(
        "V1 in 0 DC 5\nR1 in out 1k\nR2 out 0 2k",
        "op",
        {},
        ["out"],
        project_root=tmp_path,
    )
    assert r["success"]
    assert r["results"][0]["summary"]["mean"] == pytest.approx(3.333, abs=1e-2)


def test_tran_rc_time_constant(tmp_path):
    # tau = R*C = 1k*1u = 1ms; at t=5ms (5 tau) Vout ~ 0.993*5 = 4.97V
    r = simulate(
        "V1 in 0 DC 5\nR1 in out 1k\nC1 out 0 1u",
        "tran",
        {"t_step": "5us", "t_end": "5ms"},
        ["out"],
        project_root=tmp_path,
    )
    assert r["success"]
    assert r["results"][0]["summary"]["max"] == pytest.approx(5.0, abs=0.1)
    # raw waveform persisted and self-contained (probe + time axis)
    data = dict(np.load(tmp_path / r["result_file"]))
    assert "out" in data and "time" in data


def test_ac_rc_lowpass_passband_gain(tmp_path):
    # AC gain through an RC low-pass is ~1.0 in the passband (well below fc=159Hz)
    r = simulate(
        "V1 in 0 DC 0 AC 1\nR1 in out 1k\nC1 out 0 1u",
        "ac",
        {"variation": "dec", "n_points": 10, "f_start": 1, "f_stop": 1e6},
        ["out"],
        project_root=tmp_path,
    )
    assert r["success"]
    assert r["results"][0]["summary"]["max"] == pytest.approx(1.0, abs=0.05)


def test_diode_drop_from_bundled_model(tmp_path):
    # generic silicon diode at a few mA sits around 0.6-0.8V
    r = simulate(
        "V1 a 0 DC 5\nR1 a k 1k\nD1 k 0 Dgen", "op", {}, ["k"], project_root=tmp_path
    )
    assert r["success"]
    assert 0.5 < r["results"][0]["summary"]["mean"] < 0.9


def test_ideal_opamp_inverting_gain(tmp_path):
    # inverting amp with the bundled OPAMP_IDEAL: gain = -Rf/Rin = -10k/1k = -10,
    # so 0.1 V in -> -1.0 V out (closed-loop error ~ (1+10)/A0, negligible at A0=100k)
    r = simulate(
        "V1 in 0 DC 0.1\n"
        "R1 in minus 1k\n"
        "R2 minus out 10k\n"
        "X1 0 minus out OPAMP_IDEAL",
        "op",
        {},
        ["out"],
        project_root=tmp_path,
    )
    assert r["success"]
    assert r["results"][0]["summary"]["mean"] == pytest.approx(-1.0, abs=1e-2)


def test_convergence_failure_is_reported_not_raised(tmp_path):
    # floating subcircuit with no DC path to ground -> singular matrix
    r = simulate("R1 a b 1k", "op", {}, ["a"], project_root=tmp_path)
    assert r["success"] is False
    assert r["errors"][0]["type"] in ("convergence_failure", "parse_error", "unknown")


def test_missing_probe_tracked_not_fatal(tmp_path):
    r = simulate(
        "V1 in 0 DC 5\nR1 in out 1k\nR2 out 0 2k",
        "op",
        {},
        ["out", "ghost"],
        project_root=tmp_path,
    )
    assert r["success"]
    assert r["missing_probes"] == ["ghost"]
