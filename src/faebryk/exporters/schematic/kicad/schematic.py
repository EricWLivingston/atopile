# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
KiCad connectivity-schematic emitter (text output).

``export_schematic`` walks a built faebryk graph and extracts components and the net
each pin sits on, then emits a valid ``.kicad_sch`` as s-expression text. Two modes:

- **wire mode** (``draw_wires=True``, default, ``render_wired``): generic bottom-pin
  boxes in one row; each net is drawn as a horizontal trunk + vertical drops + junctions
  in the empty channel below. Real drawn nets, provably short-free (``render_wired``).
- **label mode** (``render``): grid-placed symbols (real cached ``.kicad_sym`` where
  available, generic box otherwise) with a ``global_label`` per pin and no wires.

We emit text rather than using ``kicad.dumps`` because the typed schematic write path
cannot currently produce a KiCad-loadable file (``07_ATOPILE_GAPS.md`` §2.11). The
typed model is still used to *read* cached symbol files for pin geometry (label mode).
"""

import logging
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path

from natsort import natsorted

import faebryk.core.node as fabll
import faebryk.library._F as F
from faebryk.exporters.schematic.kicad.generic_symbol import (
    WIRE_PIN_PITCH,
    SymbolDef,
    build_generic_symbol,
    build_wire_box,
    escape,
)
from faebryk.exporters.schematic.kicad.real_symbol import real_symbol_from_file
from faebryk.libs.util import sanitize_filepath_part

logger = logging.getLogger(__name__)

# Schematic format constants (mirrors the kicad-cli-validated spike).
SCH_VERSION = "20211123"
PAPER = "A4"
LIB_PREFIX = "atopile"

# Grid placement (mm). Generous pitch keeps generic boxes from overlapping; for a
# connectivity view, exact placement does not matter (labels carry connectivity).
ORIGIN_X = 50.8
ORIGIN_Y = 50.8
COL_PITCH = 50.8
ROW_PITCH = 63.5
DEFAULT_COLS = 8

# Wire (ladder) mode layout, mm. Components sit in one row at ROW_Y; nets are routed in
# the empty channel below as a horizontal trunk + vertical drops. LANE_PITCH matches
# generic_symbol.WIRE_PIN_PITCH so each bottom pin lands in its own global lane.
LANE0_X = 25.4
LANE_PITCH = WIRE_PIN_PITCH
COMPONENT_GAP_LANES = 1  # blank lanes between adjacent components
ROW_Y = 25.4
CHANNEL_GAP = 12.7  # gap between the pin row and the first trunk
TRUNK_PITCH = 5.08  # vertical spacing between net trunks


def _u() -> str:
    return str(_uuid.uuid4())


# --------------------------------------------------------------------------------------
# Intermediate representation
# --------------------------------------------------------------------------------------
@dataclass
class PinIR:
    number: str
    net: str | None


@dataclass
class ComponentIR:
    ref: str
    value: str
    pins: list[PinIR]
    module: fabll.Node | None = None
    inst_uuid: str = ""


@dataclass
class SchematicSummary:
    components: int = 0
    nets: int = 0
    labels: int = 0
    fallback_symbols: int = 0
    unmapped_pads: int = 0
    wires: int = 0
    junctions: int = 0
    trunks: int = 0
    path: str | None = None

    def __str__(self) -> str:
        base = (
            f"{self.components} components, {self.nets} nets, {self.labels} labels, "
            f"{self.fallback_symbols} generic-box fallbacks, "
            f"{self.unmapped_pads} unmapped pads"
        )
        if self.wires or self.trunks:
            base += (
                f", {self.wires} wires, {self.junctions} junctions, "
                f"{self.trunks} trunks"
            )
        return base


# --------------------------------------------------------------------------------------
# Graph -> IR
# --------------------------------------------------------------------------------------
def extract_components(app: fabll.Node) -> tuple[list[ComponentIR], set[str]]:
    """
    Extract placeable components (with refdes) and their per-pin net assignments.

    Components are nodes carrying ``has_designator``; their pads come from the attached
    footprint. Each pad's net is resolved via a reverse map built from every ``Net``'s
    ``get_connected_pads()`` (``is_pad`` traits hash by node identity, so the same pad
    seen via a net and via a footprint compares equal).
    """
    tg, g = app.tg, app.g

    pad_to_net: dict[F.Footprints.is_pad, str | None] = {}
    net_names: set[str] = set()
    for net in F.Net.bind_typegraph(tg).get_instances(g):
        name = net.get_name()
        if name:
            net_names.add(name)
        for pad in net.get_connected_pads():
            pad_to_net[pad] = name

    components: list[ComponentIR] = []
    for des in fabll.Traits.get_implementors(F.has_designator.bind_typegraph(tg), g=g):
        module = fabll.Traits.bind(des).get_obj_raw()
        ref = des.get_designator()

        value = ""
        if vrep := module.try_get_trait(F.has_simple_value_representation):
            try:
                value = vrep.get_value()
            except Exception:  # noqa: BLE001 - value is best-effort cosmetic
                value = ""

        fp_trait = module.try_get_trait(F.Footprints.has_associated_footprint)
        if fp_trait is None:
            logger.debug("Skipping %s: no associated footprint", ref)
            continue

        pins = [
            PinIR(number=pad.pad_number, net=pad_to_net.get(pad))
            for pad in fp_trait.get_footprint().get_pads()
        ]
        components.append(ComponentIR(ref=ref, value=value, pins=pins, module=module))

    components = natsorted(components, key=lambda c: c.ref)
    return components, net_names


# --------------------------------------------------------------------------------------
# Symbol resolution
# --------------------------------------------------------------------------------------
def _find_symbol_file(module: fabll.Node, search_dirs: list[Path]) -> Path | None:
    """Find the cached ``.kicad_sym`` for a picked/atomic component, if any."""
    # Atomic parts name their symbol file explicitly.
    atomic = module.try_get_trait(F.is_atomic_part)
    if atomic is not None:
        try:
            sym_name = atomic.symbol.get().extract_singleton()
        except Exception:  # noqa: BLE001 - symbol param may be unset
            sym_name = None
        if sym_name:
            for directory in search_dirs:
                if not directory.exists():
                    continue
                match = next(directory.rglob(sym_name), None)
                if match is not None:
                    return match

    # Picked parts cache their symbol under a "<Mfr>_<Partno>" directory.
    picked = module.try_get_trait(F.Pickable.has_part_picked)
    if picked is not None:
        try:
            mfr = picked.manufacturer.get().try_extract_singleton()
            partno = picked.partno.get().try_extract_singleton()
        except Exception:  # noqa: BLE001 - params may be unset
            mfr = partno = None
        if mfr and partno:
            dirname = (
                f"{sanitize_filepath_part(mfr)}_{sanitize_filepath_part(partno)}"
            )
            for directory in search_dirs:
                if not directory.exists():
                    continue
                for part_dir in directory.rglob(dirname):
                    if not part_dir.is_dir():
                        continue
                    sym = next(part_dir.glob("*.kicad_sym"), None)
                    if sym is not None:
                        return sym
    return None


class _SymbolRegistry:
    """De-duplicates resolved symbols and assigns generic-box lib_ids."""

    def __init__(self, search_dirs: list[Path]) -> None:
        self._search_dirs = search_dirs
        self._by_lib_id: dict[str, SymbolDef] = {}
        self._generic_cache: dict[tuple[str, ...], SymbolDef] = {}
        self.fallback_count = 0

    def resolve(self, comp: ComponentIR) -> SymbolDef:
        if comp.module is not None:
            sym_path = _find_symbol_file(comp.module, self._search_dirs)
            if sym_path is not None:
                real = real_symbol_from_file(sym_path)
                if real is not None:
                    self._by_lib_id.setdefault(real.lib_id, real)
                    return real

        key = tuple(dict.fromkeys(p.number for p in comp.pins))
        if key in self._generic_cache:
            return self._generic_cache[key]
        lib_id = f"{LIB_PREFIX}:GEN_{len(self._generic_cache)}_{len(key)}"
        generic = build_generic_symbol(lib_id, list(key))
        self._generic_cache[key] = generic
        self._by_lib_id[lib_id] = generic
        self.fallback_count += 1
        return generic

    def lib_symbols_text(self) -> str:
        return "\n".join(
            self._by_lib_id[k].lib_symbol_text for k in sorted(self._by_lib_id)
        )


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------
def _instance_block(comp: ComponentIR, sym: SymbolDef, ix: float, iy: float) -> str:
    pin_lines = "\n".join(
        f'    (pin "{escape(num)}" (uuid {_u()}))' for num in sym.pin_xy
    )
    return (
        f'  (symbol (lib_id "{sym.lib_id}") (at {ix} {iy} 0) (unit 1)\n'
        f"    (in_bom yes) (on_board yes) (fields_autoplaced)\n"
        f"    (uuid {comp.inst_uuid})\n"
        f'    (property "Reference" "{escape(comp.ref)}" (id 0) (at {ix} {iy - 12.7} 0)'
        f" (effects (font (size 1.27 1.27))))\n"
        f'    (property "Value" "{escape(comp.value)}" (id 1) (at {ix} {iy + 12.7} 0)'
        f" (effects (font (size 1.27 1.27))))\n"
        f'    (property "Footprint" "" (id 2) (at {ix} {iy} 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f'    (property "Datasheet" "" (id 3) (at {ix} {iy} 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f"{pin_lines}\n"
        f"  )"
    )


def _label_block(net: str, x: float, y: float) -> str:
    return (
        f'  (global_label "{escape(net)}" (shape bidirectional) (at {x} {y} 180)'
        f" (fields_autoplaced)\n"
        f"    (effects (font (size 1.27 1.27)) (justify right))\n"
        f"    (uuid {_u()}))"
    )


def _trunk_label_block(net: str, x: float, y: float) -> str:
    """A net label anchored at the left end of a trunk (points left, justified)."""
    return (
        f'  (global_label "{escape(net)}" (shape input) (at {x} {y} 180)'
        f" (fields_autoplaced)\n"
        f"    (effects (font (size 1.27 1.27)) (justify right))\n"
        f"    (uuid {_u()}))"
    )


def _wire_block(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f"  (wire (pts (xy {x1} {y1}) (xy {x2} {y2}))\n"
        f"    (stroke (width 0) (type default) (color 0 0 0 0))\n"
        f"    (uuid {_u()}))"
    )


def _junction_block(x: float, y: float) -> str:
    return (
        f"  (junction (at {x} {y}) (diameter 0) (color 0 0 0 0)\n"
        f"    (uuid {_u()}))"
    )


def render(
    components: list[ComponentIR], net_names: set[str], search_dirs: list[Path]
) -> tuple[str, SchematicSummary]:
    registry = _SymbolRegistry(search_dirs)
    summary = SchematicSummary(components=len(components), nets=len(net_names))

    instances: list[str] = []
    labels: list[str] = []
    sym_paths: list[str] = []

    for i, comp in enumerate(components):
        sym = registry.resolve(comp)
        comp.inst_uuid = _u()
        col, row = i % DEFAULT_COLS, i // DEFAULT_COLS
        ix = ORIGIN_X + col * COL_PITCH
        iy = ORIGIN_Y + row * ROW_PITCH

        instances.append(_instance_block(comp, sym, ix, iy))
        sym_paths.append(
            f'    (path "/{comp.inst_uuid}" (reference "{escape(comp.ref)}") (unit 1)'
            f' (value "{escape(comp.value)}") (footprint ""))'
        )

        net_by_num = {p.number: p.net for p in comp.pins}
        for num, (px, py) in sym.pin_xy.items():
            net = net_by_num.get(num)
            if net is None:
                continue
            # Y-flip on instantiation: symbol (px, py) -> schematic (ix+px, iy-py).
            labels.append(_label_block(net, ix + px, iy - py))
            summary.labels += 1
        # Pads present on the component but absent from the symbol.
        summary.unmapped_pads += sum(
            1 for num in net_by_num if num not in sym.pin_xy
        )

    summary.fallback_symbols = registry.fallback_count

    doc = (
        f"(kicad_sch (version {SCH_VERSION}) (generator eeschema)\n"
        f"  (uuid {_u()})\n"
        f'  (paper "{PAPER}")\n'
        f"  (lib_symbols\n"
        f"{registry.lib_symbols_text()}\n"
        f"  )\n"
        f"{chr(10).join(instances)}\n"
        f"{chr(10).join(labels)}\n"
        f"  (sheet_instances\n"
        f'    (path "/" (page "1"))\n'
        f"  )\n"
        f"  (symbol_instances\n"
        f"{chr(10).join(sym_paths)}\n"
        f"  )\n"
        f")\n"
    )
    return doc, summary


def render_wired(
    components: list[ComponentIR], net_names: set[str]
) -> tuple[str, SchematicSummary]:
    """
    Ladder-routed renderer: real drawn wires for nets, generic bottom-pin boxes only.

    Every pin gets its own global x-lane in a single component row; each net is a
    horizontal trunk in the empty channel below, joined to its pins by straight vertical
    drops with junctions at interior taps. Because lanes are globally unique and the
    channel holds no pins, drops can only *cross* other nets (never tap them), so the
    routing is short-free. See the plan / `13` §1.5 for the argument.
    """
    summary = SchematicSummary(components=len(components), nets=len(net_names))

    # Dedupe wire boxes by their (ordered) pin-number tuple, like the label-mode box.
    box_by_pins: dict[tuple[str, ...], SymbolDef] = {}

    def _box(nums: list[str]) -> SymbolDef:
        key = tuple(nums)
        if key not in box_by_pins:
            lib_id = f"{LIB_PREFIX}:WBOX_{len(box_by_pins)}_{len(nums)}"
            box_by_pins[key] = build_wire_box(lib_id, nums)
        return box_by_pins[key]

    instances: list[str] = []
    sym_paths: list[str] = []
    # net name -> list of (lane_x, pin_bottom_y) tap points
    taps: dict[str, list[tuple[float, float]]] = {}

    lane = 0
    for comp in components:
        nums = natsorted(p.number for p in comp.pins)
        n = len(nums)
        sym = _box(nums)
        comp.inst_uuid = _u()

        # Contiguous lanes for this component's pins; instance centred over them.
        first_lane = lane
        lane_xs = [LANE0_X + (first_lane + k) * LANE_PITCH for k in range(n)]
        origin_x = LANE0_X + (first_lane + (n - 1) / 2) * LANE_PITCH
        lane += n + COMPONENT_GAP_LANES

        instances.append(_instance_block(comp, sym, origin_x, ROW_Y))
        sym_paths.append(
            f'    (path "/{comp.inst_uuid}" (reference "{escape(comp.ref)}") (unit 1)'
            f' (value "{escape(comp.value)}") (footprint ""))'
        )

        net_by_num = {p.number: p.net for p in comp.pins}
        for num, lane_x in zip(nums, lane_xs):
            net = net_by_num.get(num)
            if net is None:
                continue
            _, py = sym.pin_xy[num]
            pin_y = ROW_Y - py  # Y-flip; bottom pin -> below the row
            taps.setdefault(net, []).append((lane_x, pin_y))

    summary.fallback_symbols = len(box_by_pins)

    # Trunks sit in the empty channel below the (uniform) pin row.
    channel_y0 = (
        max((y for pts in taps.values() for _, y in pts), default=ROW_Y) + CHANNEL_GAP
    )

    wires: list[str] = []
    junctions: list[str] = []
    labels: list[str] = []
    for idx, net in enumerate(sorted(taps)):
        points = sorted(taps[net])  # by x
        if len(points) < 2:
            # Single-pin net: just a label at the pin.
            x, y = points[0]
            labels.append(_label_block(net, x, y))
            summary.labels += 1
            continue
        trunk_y = channel_y0 + idx * TRUNK_PITCH
        xs = [x for x, _ in points]
        # Vertical drops from each pin to the trunk.
        for x, y in points:
            wires.append(_wire_block(x, y, x, trunk_y))
            summary.wires += 1
        # Horizontal trunk.
        wires.append(_wire_block(min(xs), trunk_y, max(xs), trunk_y))
        summary.wires += 1
        summary.trunks += 1
        # Junctions where interior drops tap the trunk (endpoints connect end-to-end).
        for x in xs[1:-1]:
            junctions.append(_junction_block(x, trunk_y))
            summary.junctions += 1
        # One net label at the trunk's left end.
        labels.append(_trunk_label_block(net, min(xs), trunk_y))
        summary.labels += 1

    lib_symbols_text = "\n".join(
        box_by_pins[k].lib_symbol_text
        for k in sorted(box_by_pins, key=lambda t: box_by_pins[t].lib_id)
    )
    doc = (
        f"(kicad_sch (version {SCH_VERSION}) (generator eeschema)\n"
        f"  (uuid {_u()})\n"
        f'  (paper "{PAPER}")\n'
        f"  (lib_symbols\n{lib_symbols_text}\n  )\n"
        f"{chr(10).join(instances)}\n"
        f"{chr(10).join(wires)}\n"
        f"{chr(10).join(junctions)}\n"
        f"{chr(10).join(labels)}\n"
        f'  (sheet_instances\n    (path "/" (page "1"))\n  )\n'
        f"  (symbol_instances\n{chr(10).join(sym_paths)}\n  )\n"
        f")\n"
    )
    return doc, summary


def export_schematic(
    app: fabll.Node,
    *,
    target_name: str,
    out_path: Path,
    parts_search_dirs: list[Path] | None = None,
    draw_wires: bool = True,
) -> SchematicSummary:
    """
    Export a connectivity ``.kicad_sch`` for ``app`` to ``out_path``.

    ``draw_wires`` (default) routes each net as a ladder of real drawn wires using
    generic bottom-pin boxes. Set it ``False`` for the label mode: grid-placed symbols
    (real cached ``.kicad_sym`` where available, generic box otherwise — searched in
    ``parts_search_dirs``) with a net-name ``global_label`` on every pin and no wires.
    """
    components, net_names = extract_components(app)
    if draw_wires:
        doc, summary = render_wired(components, net_names)
    else:
        doc, summary = render(components, net_names, parts_search_dirs or [])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    summary.path = str(out_path)
    logger.info("Exported schematic %s (%s)", out_path, summary)
    return summary
