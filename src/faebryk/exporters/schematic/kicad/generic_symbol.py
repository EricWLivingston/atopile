# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Generic rectangular "box" symbols for the connectivity-schematic emitter.

When a component has no resolvable KiCad symbol, we synthesise a generic box: a
rectangle with the component's pads laid out as pins on the left/right edges. This
guarantees every component renders and that a net label can be placed on every pin.

The geometry mirrors the kicad-cli-validated spike in
``ee_agent_docs_5_21/13_KICAD_SCH_AND_FRONTEND_FILES.md`` §1.5: symbol pin ``(at x y)``
positions are the pin *connection points* (the Y-flip on instantiation is applied by
the caller), the lib-symbol name equals the instance ``lib_id``, and child unit symbols
use the bare (library-less) name prefix.
"""

from dataclasses import dataclass

PIN_LENGTH = 2.54
GRID = 2.54


@dataclass
class SymbolDef:
    """A resolved schematic symbol ready to embed and instantiate."""

    lib_id: str
    """Instance ``lib_id``, e.g. ``"atopile:GEN_2"``. Equals the lib-symbol name."""

    lib_symbol_text: str
    """The ``(symbol "<lib_id>" …)`` block to place inside ``(lib_symbols …)``."""

    pin_xy: dict[str, tuple[float, float]]
    """Pin number -> connection point ``(x, y)`` in symbol coordinates (Y up)."""

    is_fallback: bool = False
    """True if this is a synthesised generic box rather than a real symbol."""


def escape(s: str) -> str:
    """Escape a string for embedding inside a KiCad s-expression quoted token."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_generic_symbol(lib_id: str, pin_numbers: list[str]) -> SymbolDef:
    """
    Build a generic rectangular symbol whose pins are ``pin_numbers``.

    Pins are split across the left and right edges; ``lib_id`` must be of the form
    ``"<lib>:<name>"`` and is used verbatim as the lib-symbol name. Child unit symbols
    use the bare ``<name>`` prefix (KiCad's convention).
    """
    nums = list(dict.fromkeys(pin_numbers))  # de-dupe, preserve order
    bare = lib_id.split(":", 1)[-1]

    half = (len(nums) + 1) // 2
    left, right = nums[:half], nums[half:]
    rows = max(len(left), len(right), 1)

    half_w = 5.08
    half_h = (rows - 1) * GRID / 2 + GRID

    pin_xy: dict[str, tuple[float, float]] = {}
    pin_blocks: list[str] = []

    def add_pin(num: str, x: float, y: float, rot: int) -> None:
        pin_xy[num] = (x, y)
        pin_blocks.append(
            f"        (pin passive line (at {x} {y} {rot}) (length {PIN_LENGTH})\n"
            f'          (name "~" (effects (font (size 1.27 1.27))))\n'
            f'          (number "{escape(num)}" (effects (font (size 1.27 1.27)))))'
        )

    top = half_h - GRID
    for i, num in enumerate(left):
        add_pin(num, -(half_w + PIN_LENGTH), top - i * GRID, 0)
    for i, num in enumerate(right):
        add_pin(num, half_w + PIN_LENGTH, top - i * GRID, 180)

    rect = (
        f'      (symbol "{bare}_0_1"\n'
        f"        (rectangle (start {-half_w} {half_h}) (end {half_w} {-half_h})\n"
        f"          (stroke (width 0.254) (type default)) (fill (type background))))"
    )
    pins_unit = f'      (symbol "{bare}_1_1"\n' + "\n".join(pin_blocks) + "\n      )"

    text = (
        f'    (symbol "{lib_id}" (pin_numbers hide) (pin_names (offset 0))'
        f" (in_bom yes) (on_board yes)\n"
        f'      (property "Reference" "U" (id 0) (at 0 {half_h + GRID} 0)'
        f" (effects (font (size 1.27 1.27))))\n"
        f'      (property "Value" "{escape(bare)}" (id 1) (at 0 {-half_h - GRID} 0)'
        f" (effects (font (size 1.27 1.27))))\n"
        f"{rect}\n"
        f"{pins_unit}\n"
        f"    )"
    )

    return SymbolDef(
        lib_id=lib_id, lib_symbol_text=text, pin_xy=pin_xy, is_fallback=True
    )
