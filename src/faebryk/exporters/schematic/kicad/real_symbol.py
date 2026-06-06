# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Regenerate a real KiCad symbol as a schematic-embedded ``(symbol …)`` block.

Cached ``.kicad_sym`` files are written in a newer standalone-library grammar
(``version 20241229``) that a ``20211123`` schematic's parser rejects when embedded
verbatim. Rather than splice that text, we parse the symbol with atopile's typed model
and re-emit it in the schematic's own ``20211123`` embedded form (the same primitives
used by the generic box): properties, ``pin_names``/``pin_numbers``, and per-unit
rectangles / polylines / circles / arcs / pins.

The top-level symbol name becomes the instance ``lib_id`` (``atopile:<name>``); child
unit names keep their bare ``<name>`` prefix, matching KiCad's convention.
"""

import logging
import math
from pathlib import Path

from faebryk.exporters.schematic.kicad.generic_symbol import SymbolDef, escape
from faebryk.libs.kicad.fileformats import kicad

logger = logging.getLogger(__name__)

_FONT = "(effects (font (size 1.27 1.27)))"


def _stroke(stroke) -> str:
    width = getattr(stroke, "width", 0.0) or 0.0
    stype = getattr(stroke, "type", None) or "default"
    return f"(stroke (width {width}) (type {stype}))"


def _fill(fill) -> str:
    ftype = getattr(fill, "type", None) or "none"
    return f"(fill (type {ftype}))"


def _rectangle(rect) -> str:
    return (
        f"        (rectangle (start {rect.start.x} {rect.start.y})"
        f" (end {rect.end.x} {rect.end.y})\n"
        f"          {_stroke(rect.stroke)} {_fill(rect.fill)})"
    )


def _polyline(poly) -> str:
    pts = " ".join(f"(xy {p.x} {p.y})" for p in poly.pts.xys)
    return (
        f"        (polyline (pts {pts})\n"
        f"          {_stroke(poly.stroke)} {_fill(poly.fill)})"
    )


def _circle(circle) -> str:
    radius = math.hypot(circle.end.x - circle.center.x, circle.end.y - circle.center.y)
    return (
        f"        (circle (center {circle.center.x} {circle.center.y})"
        f" (radius {round(radius, 4)})\n"
        f"          {_stroke(circle.stroke)} {_fill(circle.fill)})"
    )


def _arc(arc) -> str:
    return (
        f"        (arc (start {arc.start.x} {arc.start.y})"
        f" (mid {arc.mid.x} {arc.mid.y}) (end {arc.end.x} {arc.end.y})\n"
        f"          {_stroke(arc.stroke)} {_fill(arc.fill)})"
    )


def _pin(pin) -> str:
    rot = pin.at.r if pin.at.r is not None else 0
    ptype = pin.type or "passive"
    pstyle = pin.style or "line"
    return (
        f"        (pin {ptype} {pstyle} (at {pin.at.x} {pin.at.y} {rot})"
        f" (length {pin.length})\n"
        f'          (name "{escape(pin.name.name)}" {_FONT})\n'
        f'          (number "{escape(pin.number.number)}" {_FONT}))'
    )


def _property(name: str, value: str, pid: int, x: float, y: float) -> str:
    return (
        f'      (property "{escape(name)}" "{escape(value)}" (id {pid})'
        f" (at {x} {y} 0) {_FONT})"
    )


def build_real_symbol(lib_id: str, sym_file) -> SymbolDef | None:
    """Build a ``SymbolDef`` from a parsed ``kicad.symbol.SymbolFile``."""
    symbols = sym_file.kicad_sym.symbols
    if not symbols:
        return None
    top = symbols[0]

    props = {p.name: p.value for p in top.propertys}
    ref = props.get("Reference", "U")
    value = props.get("Value", top.name)

    header_bits: list[str] = []
    if top.pin_numbers is not None:
        header_bits.append("(pin_numbers hide)")
    offset = top.pin_names.offset if top.pin_names is not None else 0
    header_bits.append(f"(pin_names (offset {offset}))")
    header_bits.append("(in_bom yes) (on_board yes)")

    pin_xy: dict[str, tuple[float, float]] = {}
    unit_blocks: list[str] = []
    for unit in top.symbols:
        body: list[str] = []
        body += [_rectangle(r) for r in unit.rectangles]
        body += [_polyline(p) for p in unit.polylines]
        body += [_circle(c) for c in unit.circles]
        body += [_arc(a) for a in unit.arcs]
        for pin in unit.pins:
            pin_xy[pin.number.number] = (pin.at.x, pin.at.y)
            body.append(_pin(pin))
        unit_blocks.append(
            f'      (symbol "{escape(unit.name)}"\n' + "\n".join(body) + "\n      )"
        )

    if not pin_xy:
        return None

    text = (
        f'    (symbol "{lib_id}" {" ".join(header_bits)}\n'
        f"{_property('Reference', ref, 0, 0, 5.08)}\n"
        f"{_property('Value', value, 1, 0, -5.08)}\n"
        + "\n".join(unit_blocks)
        + "\n    )"
    )
    return SymbolDef(
        lib_id=lib_id, lib_symbol_text=text, pin_xy=pin_xy, is_fallback=False
    )


def real_symbol_from_file(path: Path) -> SymbolDef | None:
    """Parse a ``.kicad_sym`` and build a schematic-embedded ``SymbolDef``."""
    try:
        sym_file = kicad.loads(kicad.symbol.SymbolFile, path)
    except Exception as ex:  # noqa: BLE001 - any parse failure -> generic fallback
        logger.debug("Could not load symbol %s: %s", path, ex)
        return None
    symbols = sym_file.kicad_sym.symbols
    if not symbols:
        return None
    name = symbols[0].name
    if ":" in name:  # renaming would break the child-unit name prefix
        return None
    return build_real_symbol(f"atopile:{name}", sym_file)
