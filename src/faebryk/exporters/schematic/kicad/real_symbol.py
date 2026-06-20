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

from faebryk.exporters.schematic.kicad.generic_symbol import (
    PinGeo,
    SymbolDef,
    escape,
)
from faebryk.libs.kicad.fileformats import kicad

logger = logging.getLogger(__name__)

_FONT = "(effects (font (size 1.27 1.27)))"

# KiCad's DEFAULT_PIN_NAME_OFFSET (20 mil). Pin names render *inside* the body at this
# offset; an offset of 0 pushes them *outside* onto the pin numbers (overlap bug). Cached
# .kicad_sym files often omit the (pin_names …) token, so we must supply KiCad's real
# default rather than 0 when it is absent.
_DEFAULT_PIN_NAME_OFFSET = 0.508


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
    offset = (
        top.pin_names.offset
        if top.pin_names is not None
        else _DEFAULT_PIN_NAME_OFFSET
    )
    header_bits.append(f"(pin_names (offset {offset}))")
    header_bits.append("(in_bom yes) (on_board yes)")

    pin_xy: dict[str, tuple[float, float]] = {}
    pin_geo: dict[str, PinGeo] = {}
    unit_blocks: list[str] = []
    xs: list[float] = []
    ys: list[float] = []

    def _extend(*points: tuple[float, float]) -> None:
        for px, py in points:
            xs.append(px)
            ys.append(py)

    for unit in top.symbols:
        body: list[str] = []
        for r in unit.rectangles:
            _extend((r.start.x, r.start.y), (r.end.x, r.end.y))
            body.append(_rectangle(r))
        for p in unit.polylines:
            _extend(*((pt.x, pt.y) for pt in p.pts.xys))
            body.append(_polyline(p))
        for c in unit.circles:
            radius = math.hypot(c.end.x - c.center.x, c.end.y - c.center.y)
            _extend(
                (c.center.x - radius, c.center.y - radius),
                (c.center.x + radius, c.center.y + radius),
            )
            body.append(_circle(c))
        for a in unit.arcs:
            _extend((a.start.x, a.start.y), (a.mid.x, a.mid.y), (a.end.x, a.end.y))
            body.append(_arc(a))
        for pin in unit.pins:
            angle = pin.at.r if pin.at.r is not None else 0
            length = pin.length or 0.0
            pin_xy[pin.number.number] = (pin.at.x, pin.at.y)
            pin_geo[pin.number.number] = PinGeo(
                x=pin.at.x, y=pin.at.y, angle=angle, length=length
            )
            # Connection point and the body-side end of the drawn pin.
            rad = math.radians(angle)
            _extend(
                (pin.at.x, pin.at.y),
                (
                    pin.at.x + length * math.cos(rad),
                    pin.at.y + length * math.sin(rad),
                ),
            )
            body.append(_pin(pin))
        unit_blocks.append(
            f'      (symbol "{escape(unit.name)}"\n' + "\n".join(body) + "\n      )"
        )

    if not pin_xy:
        return None

    bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None

    text = (
        f'    (symbol "{lib_id}" {" ".join(header_bits)}\n'
        f"{_property('Reference', ref, 0, 0, 5.08)}\n"
        f"{_property('Value', value, 1, 0, -5.08)}\n"
        + "\n".join(unit_blocks)
        + "\n    )"
    )
    return SymbolDef(
        lib_id=lib_id,
        lib_symbol_text=text,
        pin_xy=pin_xy,
        is_fallback=False,
        pin_geo=pin_geo,
        bbox=bbox,
    )


def real_symbol_from_file(path: Path) -> SymbolDef | None:
    """Parse a ``.kicad_sym`` and build a schematic-embedded ``SymbolDef``."""
    try:
        sym_file = kicad.loads(kicad.symbol.SymbolFile, path)
    except Exception as ex:  # noqa: BLE001 - any parse failure -> generic fallback
        # Warn (not debug): the component drops to a generic box, which is electrically
        # complete but less readable — the user should know (CODE_AUDIT Q4).
        logger.warning(
            "Could not parse symbol %s (%s); using a generic box instead.", path, ex
        )
        return None
    symbols = sym_file.kicad_sym.symbols
    if not symbols:
        logger.warning("Symbol file %s has no symbols; using a generic box.", path)
        return None
    name = symbols[0].name
    if ":" in name:  # renaming would break the child-unit name prefix
        logger.warning(
            "Symbol %s name %r contains ':'; using a generic box.", path, name
        )
        return None
    return build_real_symbol(f"atopile:{name}", sym_file)
