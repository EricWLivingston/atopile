# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Connectivity-clustered placement for the hierarchical schematic emitter.

Components are placed so a human can wire the sheet manually:

- **Anchors** (ICs — components with more than ``ANCHOR_MIN_PINS`` symbol pins) are
  shelf-packed left-to-right in designator order, each in a cell sized from its real
  symbol bounding box plus a clearance margin.
- **Satellites** (passives/discretes) orbit the anchor they share the most nets with
  (affinity = sum of ``1/degree(net)`` over shared nets, so a dedicated feedback net
  outweighs a shared rail), stacked in columns left then right of the anchor.
- Satellites with no affinity (or sheets with no anchors) become their own clusters.

The load-bearing **safety invariant** (same style as the wired-mode ladder proof):
every component's *cell* — its schematic-space bbox grown by ``CELL_MARGIN`` per side —
is pairwise disjoint, and everything the emitter attaches to a pin (power stub
``5.08`` mm + glyph ``2.54`` mm) stays inside its own cell margin, so disjoint cells
guarantee no accidental geometric net merges. The netlist tests verify this end to end.

Grid discipline: all extents are rounded **up** to the ``GRID_SNAP`` (2.54 mm) lattice
*at construction*, so positions are sums of lattice values and never need late rounding
(late per-item rounding shifts items independently and can collapse the inter-cell gap).
Lattice origins put every pin on KiCad's wiring grid — required for manual wiring.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from faebryk.exporters.schematic.kicad.generic_symbol import SymbolDef

GRID_SNAP = 2.54
CELL_MARGIN = 15.24  # per side; holds stub(5.08) + glyph(2.54) + label clearance
ANCHOR_MIN_PINS = 4  # strictly more pins than this -> anchor
USABLE_WIDTH = 350.0  # shelf wrap width (fits A3 with margins)
ORIGIN = (25.4, 25.4)  # lattice-aligned sheet origin


def _snap_up(v: float) -> float:
    return math.ceil(v / GRID_SNAP - 1e-9) * GRID_SNAP


@dataclass
class _Item:
    """A component prepared for packing. All fields are lattice-valued.

    The symbol-space bbox (Y up) maps to schematic-space offsets around the instance
    origin: x keeps its sign, y flips (``dy_min`` is the extent *above* the origin on
    screen = symbol ``max_y``).
    """

    ref: str
    dx_min: float
    dx_max: float
    dy_min: float
    dy_max: float

    @property
    def w(self) -> float:
        return self.dx_min + self.dx_max + 2 * CELL_MARGIN

    @property
    def h(self) -> float:
        return self.dy_min + self.dy_max + 2 * CELL_MARGIN


def _item(ref: str, sym: SymbolDef) -> _Item:
    bb = sym.bbox or (-2.54, -2.54, 2.54, 2.54)
    return _Item(
        ref=ref,
        dx_min=_snap_up(max(-bb[0], 0.0)),
        dx_max=_snap_up(max(bb[2], 0.0)),
        dy_min=_snap_up(max(bb[3], 0.0)),
        dy_max=_snap_up(max(-bb[1], 0.0)),
    )


def _nets_of(comp, sym: SymbolDef) -> set[str]:
    mapped = set(sym.pin_xy)
    return {p.net for p in comp.pins if p.net and p.number in mapped}


@dataclass
class _Cluster:
    w: float
    h: float
    members: dict[str, tuple[float, float]]  # ref -> origin relative to cluster's TL


def _column(
    refs: list[str], items: dict[str, _Item]
) -> tuple[float, float, dict[str, tuple[float, float]]]:
    """Stack items vertically, left-aligned; returns (width, height, rel origins)."""
    w = max((items[r].w for r in refs), default=0.0)
    y, rel = 0.0, {}
    for r in refs:
        it = items[r]
        rel[r] = (CELL_MARGIN + it.dx_min, y + CELL_MARGIN + it.dy_min)
        y += it.h
    return w, y, rel


def _build_cluster(
    anchor: str, sats: list[str], items: dict[str, _Item]
) -> _Cluster:
    it = items[anchor]
    half = (len(sats) + 1) // 2
    lw, lh, lrel = _column(sats[:half], items)
    rw, rh, rrel = _column(sats[half:], items)
    h = max(it.h, lh, rh)
    members: dict[str, tuple[float, float]] = {}
    # Vertical centering shifts are snapped once per group; columns and the anchor are
    # horizontally disjoint bands, so an extra <=GRID_SNAP shift cannot cross bands.
    # One GRID_SNAP of slack on the cluster height absorbs the snapped shift.
    members[anchor] = (
        lw + CELL_MARGIN + it.dx_min,
        _snap_up((h - it.h) / 2) + CELL_MARGIN + it.dy_min,
    )
    l_shift = _snap_up((h - lh) / 2)
    r_shift = _snap_up((h - rh) / 2)
    for r, (rx, ry) in lrel.items():
        members[r] = (rx, ry + l_shift)
    for r, (rx, ry) in rrel.items():
        members[r] = (lw + it.w + rx, ry + r_shift)
    return _Cluster(w=lw + it.w + rw, h=h + GRID_SNAP, members=members)


def place_components(
    components: list,
    sym_by_ref: dict[str, SymbolDef],
    net_degree: dict[str, int],
    *,
    origin: tuple[float, float] = ORIGIN,
    usable_width: float = USABLE_WIDTH,
) -> dict[str, tuple[float, float]]:
    """Return ``{ref: (x, y)}`` instance origins (on the 2.54 mm lattice).

    ``components`` are ``ComponentIR``-shaped (``.ref`` plus ``.pins`` with
    ``.number``/``.net``); ``net_degree`` is the global pin-count per net.
    """
    if not components:
        return {}

    items = {c.ref: _item(c.ref, sym_by_ref[c.ref]) for c in components}
    by_ref = {c.ref: c for c in components}

    anchor_set = {
        c.ref for c in components if len(sym_by_ref[c.ref].pin_xy) > ANCHOR_MIN_PINS
    }
    anchors = [c.ref for c in components if c.ref in anchor_set]
    satellites = [c.ref for c in components if c.ref not in anchor_set]

    # --- affinity: satellite -> best anchor --------------------------------------
    anchor_nets = {a: _nets_of(by_ref[a], sym_by_ref[a]) for a in anchors}
    orbit: dict[str, list[str]] = {a: [] for a in anchors}
    loose: list[str] = []
    for s in satellites:
        s_nets = _nets_of(by_ref[s], sym_by_ref[s])
        best, best_key = None, None
        for a in anchors:
            shared = s_nets & anchor_nets[a]
            if not shared:
                continue
            score = sum(1.0 / max(net_degree.get(n, 1), 1) for n in shared)
            key = (score, len(shared))
            if best_key is None or key > best_key:
                best, best_key = a, key
        (loose if best is None else orbit[best]).append(s)

    clusters = [_build_cluster(a, orbit[a], items) for a in anchors]
    for r in loose:  # each loose satellite is its own minimal cluster
        it = items[r]
        clusters.append(
            _Cluster(
                w=it.w,
                h=it.h,
                members={r: (CELL_MARGIN + it.dx_min, CELL_MARGIN + it.dy_min)},
            )
        )

    # --- shelf packing ---------------------------------------------------------------
    positions: dict[str, tuple[float, float]] = {}
    ox, oy = origin
    x, y, row_h = 0.0, 0.0, 0.0
    for cl in clusters:
        if x > 0 and x + cl.w > usable_width:
            x, y = 0.0, y + row_h
            row_h = 0.0
        for ref, (mx, my) in cl.members.items():
            positions[ref] = (ox + x + mx, oy + y + my)
        x += cl.w
        row_h = max(row_h, cl.h)

    return positions


def sheet_extent(
    positions: dict[str, tuple[float, float]], sym_by_ref: dict[str, SymbolDef]
) -> tuple[float, float]:
    """Lower-right extent (max x, max y) of all placed cells, margins included."""
    mx = my = 0.0
    for ref, (x, y) in positions.items():
        sym = sym_by_ref.get(ref)
        bb = sym.bbox if sym and sym.bbox else (-2.54, -2.54, 2.54, 2.54)
        mx = max(mx, x + bb[2] + CELL_MARGIN)
        my = max(my, y - bb[1] + CELL_MARGIN)
    return mx, my
