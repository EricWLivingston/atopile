# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""Tests for clustered placement, symbol geometry, and power-stub auto-wiring."""

import re

from faebryk.exporters.schematic.kicad import schematic as S
from faebryk.exporters.schematic.kicad.generic_symbol import (
    PinGeo,
    build_generic_symbol,
    build_power_symbol,
)
from faebryk.exporters.schematic.kicad.placement import (
    CELL_MARGIN,
    GRID_SNAP,
    place_components,
    sheet_extent,
)
from faebryk.exporters.schematic.kicad.real_symbol import build_real_symbol
from faebryk.exporters.schematic.kicad.schematic import ComponentIR, PinIR
from faebryk.libs.kicad.fileformats import kicad
from faebryk.libs.test.fileformats import SYMFILE


# --------------------------------------------------------------------------------------
# Geometry surface (pin_geo + bbox)
# --------------------------------------------------------------------------------------
def test_real_symbol_pin_geo_and_bbox():
    sym_file = kicad.loads(kicad.symbol.SymbolFile, SYMFILE)
    sym = build_real_symbol("atopile:test", sym_file)
    assert sym is not None
    assert sym.bbox is not None
    min_x, min_y, max_x, max_y = sym.bbox
    assert min_x < max_x and min_y < max_y
    # every pin has full geometry and sits inside the bbox
    assert set(sym.pin_geo) == set(sym.pin_xy)
    for num, geo in sym.pin_geo.items():
        assert (geo.x, geo.y) == sym.pin_xy[num]
        assert geo.angle % 90 == 0
        assert min_x <= geo.x <= max_x and min_y <= geo.y <= max_y


def test_generic_symbol_pin_geo_and_bbox():
    sym = build_generic_symbol("atopile:GEN_0_4", ["1", "2", "3", "4"])
    assert sym.bbox is not None
    # left pins extend rightwards toward the body (angle 0), right pins leftwards
    for geo in sym.pin_geo.values():
        assert geo.angle == (0 if geo.x < 0 else 180)
    # bbox covers the pin connection points
    for x, y in sym.pin_xy.values():
        assert sym.bbox[0] <= x <= sym.bbox[2]
        assert sym.bbox[1] <= y <= sym.bbox[3]


def test_screen_outward_direction():
    # angle points toward the body; outward = angle + 180, y flipped on screen.
    assert S._screen_outward(PinGeo(0, 0, 0, 2.54)) == (-1, 0)  # left-edge pin
    assert S._screen_outward(PinGeo(0, 0, 180, 2.54)) == (1, 0)  # right-edge pin
    assert S._screen_outward(PinGeo(0, 0, 90, 2.54)) == (0, 1)  # bottom pin -> down
    assert S._screen_outward(PinGeo(0, 0, 270, 2.54)) == (0, -1)  # top pin -> up
    assert S._screen_outward(PinGeo(0, 0, 45, 2.54)) is None  # off-axis -> legacy


# --------------------------------------------------------------------------------------
# Clustered placement
# --------------------------------------------------------------------------------------
def _comp(ref: str, nets: list[str | None]) -> ComponentIR:
    return ComponentIR(
        ref, "", [PinIR(str(i), n) for i, n in enumerate(nets, start=1)]
    )


def _setup(comps: list[ComponentIR]):
    syms = {
        c.ref: build_generic_symbol(
            f"atopile:GEN_{c.ref}", [p.number for p in c.pins]
        )
        for c in comps
    }
    deg: dict[str, int] = {}
    for c in comps:
        for p in c.pins:
            if p.net:
                deg[p.net] = deg.get(p.net, 0) + 1
    return syms, deg


def _cells(positions, syms):
    out = {}
    for ref, (x, y) in positions.items():
        bb = syms[ref].bbox
        out[ref] = (
            x + bb[0] - CELL_MARGIN,
            y - bb[3] - CELL_MARGIN,
            x + bb[2] + CELL_MARGIN,
            y - bb[1] + CELL_MARGIN,
        )
    return out


def test_placement_cells_disjoint_and_on_grid():
    comps = [
        _comp("U1", ["vcc", "gnd", "sda", "scl", "io1", "io2", "io3", "io4"]),
        _comp("U2", ["vcc", "gnd", "sda", "scl", "a1"]),
        _comp("C1", ["vcc", "gnd"]),
        _comp("C2", ["vcc", "gnd"]),
        _comp("R1", ["sda", "vcc"]),
        _comp("R2", ["a1", "gnd"]),
        _comp("R9", ["nc1", "nc2"]),
    ]
    syms, deg = _setup(comps)
    pos = place_components(comps, syms, deg)
    assert set(pos) == {c.ref for c in comps}

    # all origins on the 2.54 mm lattice (pins land on KiCad's wiring grid)
    for x, y in pos.values():
        assert abs(x / GRID_SNAP - round(x / GRID_SNAP)) < 1e-9
        assert abs(y / GRID_SNAP - round(y / GRID_SNAP)) < 1e-9

    # safety invariant: margin-padded cells are pairwise disjoint
    cells = _cells(pos, syms)
    refs = list(cells)
    for i, a in enumerate(refs):
        for b in refs[i + 1 :]:
            ca, cb = cells[a], cells[b]
            overlaps = (
                ca[0] < cb[2] - 1e-9
                and cb[0] < ca[2] - 1e-9
                and ca[1] < cb[3] - 1e-9
                and cb[1] < ca[3] - 1e-9
            )
            assert not overlaps, f"cells of {a} and {b} overlap"


def test_placement_overlap_check_warns(caplog):
    # B7: the disjointness invariant is now checked in code. Feed deliberately
    # overlapping positions to the checker and assert it warns (never crashes).
    from faebryk.exporters.schematic.kicad import placement as P

    comps = [_comp("U1", ["a", "b", "c", "d", "e"]), _comp("C1", ["a", "b"])]
    syms, _ = _setup(comps)
    items = {c.ref: P._item(c.ref, syms[c.ref]) for c in comps}
    positions = {"U1": (50.8, 50.8), "C1": (50.8, 50.8)}  # coincident -> overlap
    with caplog.at_level("WARNING"):
        P._warn_if_cells_overlap(positions, items)
    assert any("overlap" in r.message for r in caplog.records)


def test_placement_no_overlap_warning_for_real_layout(caplog):
    comps = [
        _comp("U1", ["vcc", "gnd", "sda", "scl", "io1"]),
        _comp("C1", ["vcc", "gnd"]),
        _comp("R1", ["sda", "vcc"]),
    ]
    syms, deg = _setup(comps)
    with caplog.at_level("WARNING"):
        place_components(comps, syms, deg)
    assert not any("overlap" in r.message for r in caplog.records)


def test_placement_satellites_orbit_their_anchor():
    comps = [
        _comp("U1", ["vcc", "gnd", "sda", "scl", "io1", "io2"]),
        _comp("U2", ["vcc", "gnd", "b1", "b2", "b3", "b4"]),
        _comp("C1", ["vcc", "gnd"]),  # rail-only decap; tiebreaks to U1 (designator)
        _comp("R1", ["sda", "vcc"]),  # shares low-degree net with U1
        _comp("R2", ["b1", "gnd"]),  # shares low-degree net with U2
    ]
    syms, deg = _setup(comps)
    pos = place_components(comps, syms, deg)
    # each satellite is nearer its own anchor than the other one
    def d(a, b):
        return abs(pos[a][0] - pos[b][0]) + abs(pos[a][1] - pos[b][1])

    assert d("R1", "U1") < d("R1", "U2")
    assert d("R2", "U2") < d("R2", "U1")


def test_placement_deterministic():
    comps = [
        _comp("U1", ["vcc", "gnd", "a", "b", "c"]),
        _comp("C1", ["vcc", "gnd"]),
    ]
    syms, deg = _setup(comps)
    assert place_components(comps, syms, deg) == place_components(comps, syms, deg)


def test_sheet_extent_covers_cells():
    comps = [_comp("U1", ["a", "b", "c", "d", "e"]), _comp("C1", ["a", "b"])]
    syms, deg = _setup(comps)
    pos = place_components(comps, syms, deg)
    ex, ey = sheet_extent(pos, syms)
    for _, (x1, y1, x2, y2) in _cells(pos, syms).items():
        assert x2 <= ex + 1e-9 and y2 <= ey + 1e-9


# --------------------------------------------------------------------------------------
# Power stubs + oriented glyphs/labels in the hierarchical renderer
# --------------------------------------------------------------------------------------
def _power_tree() -> S.SheetIR:
    return S.SheetIR(
        name="root",
        node_id="r",
        components=[
            ComponentIR(
                "U1", "", [PinIR("1", "VCC"), PinIR("2", "GND"), PinIR("3", "SIG")]
            )
        ],
    )


_ROLES = {
    "VCC": S.NetRole.POWER,
    "GND": S.NetRole.GROUND,
    "SIG": S.NetRole.SIGNAL,
}


def test_power_pins_get_stub_wires_to_oriented_glyphs():
    files, summary = S.render_sheet_tree(
        _power_tree(), {"VCC", "GND", "SIG"}, root_stem="p", search_dirs=[],
        net_roles=_ROLES,
    )
    doc = files["p.kicad_sch"]
    assert summary.power_symbols == 2
    assert summary.wires == 2  # one stub per rail pin

    wires = {
        (float(a), float(b), float(c), float(d))
        for a, b, c, d in re.findall(
            r"\(wire \(pts \(xy ([\d.-]+) ([\d.-]+)\) \(xy ([\d.-]+) ([\d.-]+)\)\)",
            doc,
        )
    }
    assert len(wires) == 2
    glyph_at = {
        (float(x), float(y), int(r))
        for x, y, r in re.findall(
            r'\(lib_id "atopile:(?:PWR|GND)(?:_(?!FLAG)[^"]*)?"\)'
            r" \(at ([\d.-]+) ([\d.-]+) (\d+)\)",
            doc,
        )
    }
    # every stub is exactly POWER_STUB long and ends on a glyph pin
    for x1, y1, x2, y2 in wires:
        assert abs(abs(x2 - x1) + abs(y2 - y1) - S.POWER_STUB) < 1e-9
        assert any(
            abs(gx - x2) < 1e-9 and abs(gy - y2) < 1e-9 for gx, gy, _ in glyph_at
        )
    # glyphs are rotated to the pin's outward direction (generic box: left/right pins)
    assert all(r in (90, 270) for _, _, r in glyph_at)
    # net-name text stays horizontal: property angle compensates instance rotation
    assert re.search(
        r'\(property "Value" "VCC" \(id 1\) \(at [\d.-]+ [\d.-]+ (90|270)\)', doc
    )


def test_signal_labels_are_oriented_outward():
    files, _ = S.render_sheet_tree(
        _power_tree(), {"VCC", "GND", "SIG"}, root_stem="p", search_dirs=[],
        net_roles=_ROLES,
    )
    doc = files["p.kicad_sch"]
    # SIG is pin 3 -> right edge of the generic box -> label points right (rot 0)
    m = re.search(
        r'\(global_label "SIG" \(shape bidirectional\)'
        r" \(at [\d.-]+ [\d.-]+ (\d+)\)",
        doc,
    )
    assert m and m.group(1) == "0"
    sig_block = doc.split('(global_label "SIG"')[1].split("(uuid")[0]
    assert "(justify left)" in sig_block


def test_sheet_has_title_block_and_paper():
    files, _ = S.render_sheet_tree(
        _power_tree(), {"VCC", "GND", "SIG"}, root_stem="p", search_dirs=[],
        net_roles=_ROLES,
    )
    doc = files["p.kicad_sch"]
    assert '(title_block (title "root")' in doc
    assert '(comment 1 "generated by atopile")' in doc
    assert re.search(r'\(paper "A[234]"\)', doc)


def test_power_symbol_bbox_and_pin_geo():
    gnd = build_power_symbol("atopile:GND_g", "g", ground=True)
    pwr = build_power_symbol("atopile:PWR_p", "p", ground=False)
    assert gnd.bbox[1] < 0 <= gnd.bbox[3]  # triangle hangs below the pin
    assert pwr.bbox[3] > 0 >= pwr.bbox[1]  # arrow rises above the pin
    assert gnd.pin_geo["1"].length == 0.0
