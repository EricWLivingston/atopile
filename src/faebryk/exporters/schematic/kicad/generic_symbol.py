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

from dataclasses import dataclass, field

PIN_LENGTH = 2.54
GRID = 2.54
# Lane spacing for wire-mode bottom-edge pins. The ladder renderer assigns global
# x-lanes at this same pitch, so each pin lands in its own lane.
WIRE_PIN_PITCH = 5.08


@dataclass(frozen=True)
class PinGeo:
    """Full pin geometry in symbol coordinates (Y up).

    ``(x, y)`` is the electrical connection point; ``angle`` is the direction the pin
    extends *toward the symbol body* (KiCad convention — a left-edge pin has angle 0),
    so the outward direction is ``angle + 180``. ``length`` is the drawn pin length.
    """

    x: float
    y: float
    angle: float
    length: float


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

    pin_geo: dict[str, PinGeo] = field(default_factory=dict)
    """Pin number -> full geometry (connection point, body angle, length)."""

    bbox: tuple[float, float, float, float] | None = None
    """Symbol-space extent ``(min_x, min_y, max_x, max_y)`` of body + pins (Y up)."""

    @property
    def width(self) -> float:
        return (self.bbox[2] - self.bbox[0]) if self.bbox else 0.0

    @property
    def height(self) -> float:
        return (self.bbox[3] - self.bbox[1]) if self.bbox else 0.0


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
    pin_geo: dict[str, PinGeo] = {}
    pin_blocks: list[str] = []

    def add_pin(num: str, x: float, y: float, rot: int) -> None:
        pin_xy[num] = (x, y)
        pin_geo[num] = PinGeo(x=x, y=y, angle=rot, length=PIN_LENGTH)
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
        lib_id=lib_id,
        lib_symbol_text=text,
        pin_xy=pin_xy,
        is_fallback=True,
        pin_geo=pin_geo,
        bbox=(-(half_w + PIN_LENGTH), -half_h, half_w + PIN_LENGTH, half_h),
    )


# Power-symbol glyphs (KiCad's stock GND triangle / power up-arrow), origin at the pin.
_GND_GLYPH = (
    "(polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54)"
    " (xy -1.27 -1.27) (xy 0 -1.27))\n"
    "          (stroke (width 0) (type default)) (fill (type none)))"
)
_PWR_GLYPH = (
    "(polyline (pts (xy -0.762 1.27) (xy 0 2.54))"
    " (stroke (width 0) (type default)) (fill (type none)))\n"
    "        (polyline (pts (xy 0 0) (xy 0 2.54))"
    " (stroke (width 0) (type default)) (fill (type none)))\n"
    "        (polyline (pts (xy 0 2.54) (xy 0.762 1.27))"
    " (stroke (width 0) (type default)) (fill (type none)))"
)


def build_power_symbol(lib_id: str, net_name: str, *, ground: bool) -> SymbolDef:
    """Build a KiCad **power symbol** for ``net_name`` (ground triangle or power arrow).

    A power symbol connects *by name, wirelessly*: the ``(power)`` flag plus a hidden
    ``power_in`` pin at the origin whose ``name`` equals ``net_name`` make instances
    of that name one global net (all sheets). Place the instance at a component
    pin's connection point and the length-0 pin lands on it — no wire needed. The single
    pin is at ``(0, 0)``; the caller positions the instance.
    """
    bare = lib_id.split(":", 1)[-1]
    glyph = _GND_GLYPH if ground else _PWR_GLYPH
    pin_rot = 270 if ground else 90
    ref_y, val_y = (-6.35, -3.81) if ground else (-3.81, 3.556)

    text = (
        f'    (symbol "{lib_id}" (power) (pin_names (offset 0))'
        f" (in_bom yes) (on_board yes)\n"
        f'      (property "Reference" "#PWR" (id 0) (at 0 {ref_y} 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f'      (property "Value" "{escape(net_name)}" (id 1) (at 0 {val_y} 0)'
        f" (effects (font (size 1.27 1.27))))\n"
        f'      (symbol "{bare}_0_1"\n'
        f"        {glyph})\n"
        f'      (symbol "{bare}_1_1"\n'
        f"        (pin power_in line (at 0 0 {pin_rot}) (length 0) hide\n"
        f'          (name "{escape(net_name)}" (effects (font (size 1.27 1.27))))\n'
        f'          (number "1" (effects (font (size 1.27 1.27)))))\n'
        f"      )\n"
        f"    )"
    )
    return SymbolDef(
        lib_id=lib_id,
        lib_symbol_text=text,
        pin_xy={"1": (0.0, 0.0)},
        is_fallback=True,
        pin_geo={"1": PinGeo(x=0.0, y=0.0, angle=pin_rot, length=0.0)},
        bbox=(-1.27, -2.54, 1.27, 0.0) if ground else (-0.762, 0.0, 0.762, 2.54),
    )


def build_pwr_flag_symbol(lib_id: str) -> SymbolDef:
    """Build a ``PWR_FLAG``: a ``power_out`` driver that marks a power net as driven.

    KiCad errors (``power_pin_not_driven``) on a power net whose only pins are
    ``power_in``. One ``PWR_FLAG`` on the net (atop a rail pin) supplies the needed
    ``power_out`` and clears the error. Its pin is named ``~`` so it connects by
    geometry, not name; pin at ``(0, 0)``.
    """
    bare = lib_id.split(":", 1)[-1]
    text = (
        f'    (symbol "{lib_id}" (power) (pin_numbers hide)'
        f" (pin_names (offset 0) hide) (in_bom yes) (on_board yes)\n"
        f'      (property "Reference" "#FLG" (id 0) (at 0 1.905 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f'      (property "Value" "PWR_FLAG" (id 1) (at 0 3.81 0)'
        f" (effects (font (size 1.27 1.27))))\n"
        f'      (symbol "{bare}_0_0"\n'
        f"        (pin power_out line (at 0 0 90) (length 0) hide\n"
        f'          (name "~" (effects (font (size 1.27 1.27))))\n'
        f'          (number "1" (effects (font (size 1.27 1.27)))))\n'
        f"      )\n"
        f'      (symbol "{bare}_0_1"\n'
        f"        (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54)"
        f" (xy 1.016 1.905) (xy 0 1.27))\n"
        f"          (stroke (width 0) (type default)) (fill (type none))))\n"
        f"    )"
    )
    return SymbolDef(
        lib_id=lib_id,
        lib_symbol_text=text,
        pin_xy={"1": (0.0, 0.0)},
        is_fallback=True,
        pin_geo={"1": PinGeo(x=0.0, y=0.0, angle=90, length=0.0)},
        bbox=(-1.016, 0.0, 1.016, 2.54),
    )


def build_wire_box(lib_id: str, pin_numbers: list[str]) -> SymbolDef:
    """
    Build a generic box for *wire mode* with all pins on the **bottom edge**.

    Pins are evenly spaced at ``WIRE_PIN_PITCH`` and point straight down (rotation 90,
    so the connection point is below the body). Their relative x are symmetric about the
    centre, so the ladder renderer can place the instance over a contiguous block of
    global x-lanes and have each pin land in its own lane (no two pins share an x).
    """
    nums = list(dict.fromkeys(pin_numbers))  # de-dupe, preserve order
    bare = lib_id.split(":", 1)[-1]
    n = len(nums)

    half_w = max(n, 1) * WIRE_PIN_PITCH / 2
    half_h = GRID
    pin_y = -(half_h + PIN_LENGTH)  # connection point below the body

    pin_xy: dict[str, tuple[float, float]] = {}
    pin_geo: dict[str, PinGeo] = {}
    pin_blocks: list[str] = []
    for k, num in enumerate(nums):
        x = (k - (n - 1) / 2) * WIRE_PIN_PITCH
        pin_xy[num] = (x, pin_y)
        pin_geo[num] = PinGeo(x=x, y=pin_y, angle=90, length=PIN_LENGTH)
        pin_blocks.append(
            f"        (pin passive line (at {x} {pin_y} 90) (length {PIN_LENGTH})\n"
            f'          (name "~" (effects (font (size 1.27 1.27))))\n'
            f'          (number "{escape(num)}" (effects (font (size 1.27 1.27)))))'
        )

    rect = (
        f'      (symbol "{bare}_0_1"\n'
        f"        (rectangle (start {-half_w} {half_h}) (end {half_w} {-half_h})\n"
        f"          (stroke (width 0.254) (type default)) (fill (type background))))"
    )
    pins_unit = f'      (symbol "{bare}_1_1"\n' + "\n".join(pin_blocks) + "\n      )"

    text = (
        f'    (symbol "{lib_id}" (pin_numbers hide) (pin_names (offset 0))'
        f" (in_bom yes) (on_board yes)\n"
        f'      (property "Reference" "U" (id 0) (at {-half_w} {half_h + GRID} 0)'
        f" (effects (font (size 1.27 1.27)) (justify left)))\n"
        f'      (property "Value" "{escape(bare)}" (id 1)'
        f" (at {-half_w} {half_h + GRID * 2} 0)"
        f" (effects (font (size 1.27 1.27)) (justify left)))\n"
        f"{rect}\n"
        f"{pins_unit}\n"
        f"    )"
    )

    return SymbolDef(
        lib_id=lib_id,
        lib_symbol_text=text,
        pin_xy=pin_xy,
        is_fallback=True,
        pin_geo=pin_geo,
        bbox=(-half_w, -(half_h + PIN_LENGTH), half_w, half_h),
    )
