"""Result persistence + summarisation.

Raw waveforms blow up the agent's context (a 1 s @ 1 µs transient is 1e6 samples per
probe), so we always write the full vectors to a ``.npz`` and hand the agent only
min/max/mean summaries. The agent reads summaries; if it ever needs the raw waveform it
post-processes the ``.npz`` (see ``02_SIMULATION.md`` §5/§7).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np


def summarise(vectors: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    """Per-probe ``{min, max, mean, n_samples}``; complex (AC) summed by magnitude."""
    out: list[dict[str, Any]] = []
    for name, values in vectors.items():
        arr = np.asarray(values)
        mag = np.abs(arr) if np.iscomplexobj(arr) else arr
        n = int(mag.size)
        summary = (
            {
                "min": float(np.min(mag)),
                "max": float(np.max(mag)),
                "mean": float(np.mean(mag)),
            }
            if n
            else {"min": None, "max": None, "mean": None}
        )
        out.append({"probe": name, "summary": summary, "n_samples": n})
    return out


def run_id(deck: str, analysis: str) -> str:
    """Deterministic short id for a (deck, analysis) pair — stable across reruns."""
    h = hashlib.sha256(f"{analysis}\n{deck}".encode()).hexdigest()
    return h[:12]


def persist(
    vectors: dict[str, np.ndarray], project_root: Path, deck: str, analysis: str
) -> Path:
    """Write all vectors to ``<project_root>/build/sim/<run_id>.npz``; return path."""
    sim_dir = project_root / "build" / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    path = sim_dir / f"{run_id(deck, analysis)}.npz"
    np.savez_compressed(path, **{name: np.asarray(v) for name, v in vectors.items()})
    return path
