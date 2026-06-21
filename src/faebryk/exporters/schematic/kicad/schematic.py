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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from natsort import natsorted

import faebryk.core.node as fabll
import faebryk.library._F as F
from faebryk.exporters.schematic.kicad.generic_symbol import (
    WIRE_PIN_PITCH,
    PinGeo,
    SymbolDef,
    build_generic_symbol,
    build_power_symbol,
    build_pwr_flag_symbol,
    build_wire_box,
    escape,
)
from faebryk.exporters.schematic.kicad.placement import (
    place_components,
    sheet_extent,
)
from faebryk.exporters.schematic.kicad.real_symbol import real_symbol_from_file
from faebryk.libs.kicad.identity import stable_uuid
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

# Hierarchical mode: child-sheet reference boxes are stacked in a left column.
SHEET_X = 12.7
SHEET_Y0 = 25.4
SHEET_DY = 20.32
SHEET_W = 33.02
SHEET_H = 12.7

# Power-glyph stub: short wire drawn from a rail pin outward to its power symbol.
POWER_STUB = 5.08

# Paper sizes (landscape, mm) tried smallest-first for hierarchical sheets.
_PAPER_SIZES = [("A4", 297.0, 210.0), ("A3", 420.0, 297.0), ("A2", 594.0, 420.0)]


def _pick_paper(extent: tuple[float, float]) -> str:
    ex, ey = extent
    for name, w, h in _PAPER_SIZES:
        if ex + 12.7 <= w and ey + 12.7 <= h:
            return name
    return _PAPER_SIZES[-1][0]


# --- pin direction math (screen space: x right, y down) -------------------------------
# A symbol pin's ``angle`` points toward the body, so outward = angle + 180 (symbol
# space, y up). Instantiation flips y, so the screen direction negates the y component.
_OUTWARD_SCREEN: dict[float, tuple[int, int]] = {
    0.0: (1, 0),  # outward right
    90.0: (0, -1),  # outward up (screen)
    180.0: (-1, 0),  # outward left
    270.0: (0, 1),  # outward down (screen)
}


def _screen_outward(pin: PinGeo) -> tuple[int, int] | None:
    """Outward unit direction of a pin on screen, or None for off-axis pins."""
    return _OUTWARD_SCREEN.get((pin.angle + 180.0) % 360.0)


# Instance rotation r is applied CCW in symbol space, then y flips:
# body_screen = (bx*cos r - by*sin r, -(bx*sin r + by*cos r)).
# GND's body extends to symbol -y (screen down at r=0); PWR/PWR_FLAG to symbol +y
# (screen up at r=0). These tables map desired screen body direction -> rotation.
_GND_ROT = {(0, 1): 0, (1, 0): 90, (0, -1): 180, (-1, 0): 270}
_PWR_ROT = {(0, -1): 0, (-1, 0): 90, (0, 1): 180, (1, 0): 270}

# Oriented global labels: (rotation, justify) so text reads outward from the pin.
_LABEL_ORIENT = {
    (1, 0): (0, "left"),
    (0, -1): (90, "left"),
    (-1, 0): (180, "right"),
    (0, 1): (270, "right"),
}


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
    address: str = ""  # atopile address (== footprint ``atopile_address``); "" if unknown


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
    power_symbols: int = 0
    path: str | None = None

    def __str__(self) -> str:
        base = (
            f"{self.components} components, {self.nets} nets, {self.labels} labels, "
            f"{self.fallback_symbols} generic-box fallbacks, "
            f"{self.unmapped_pads} unmapped pads"
        )
        if self.power_symbols:
            base += f", {self.power_symbols} power symbols"
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
        components.append(
            ComponentIR(
                ref=ref,
                value=value,
                pins=pins,
                module=module,
                # Same key the PCB writes as ``atopile_address`` (part_lifecycle.py),
                # so the schematic symbol UUID and the footprint UUID line up.
                address=module.get_full_name(include_uuid=False),
            )
        )

    components = natsorted(components, key=lambda c: c.ref)
    return components, net_names


# --------------------------------------------------------------------------------------
# Net classification (power / ground / signal)
# --------------------------------------------------------------------------------------
class NetRole(str, Enum):
    SIGNAL = "signal"
    POWER = "power"  # the hv side of an ElectricPower rail
    GROUND = "ground"  # the lv side of an ElectricPower rail


# Direct child names of ``ElectricPower`` and the role each implies.
_POWER_EDGES = {"hv": NetRole.POWER, "vcc": NetRole.POWER}
_GROUND_EDGES = {"lv": NetRole.GROUND, "gnd": NetRole.GROUND}


def classify_nets(app: fabll.Node) -> dict[str, NetRole]:
    """Map each named net to POWER (rail hv), GROUND (rail lv), or SIGNAL.

    A net is a rail if any electrical on it is a direct ``hv``/``lv`` (or the deprecated
    ``vcc``/``gnd`` aliases) child of an ``ElectricPower``. Ground wins ties. Lets the
    emitter drop power symbols instead of bare labels on rails.
    """
    tg, g = app.tg, app.g
    roles: dict[str, NetRole] = {}
    for net in F.Net.bind_typegraph(tg).get_instances(g):
        name = net.get_name()
        if not name:
            continue
        role = NetRole.SIGNAL
        for iface in net.get_connected_interfaces():
            parent = iface.get_parent()
            if parent is None:
                continue
            pnode, ename = parent
            try:
                if not pnode.isinstance(F.ElectricPower):
                    continue
            except Exception:  # noqa: BLE001 - type probe is best-effort cosmetic
                continue
            if ename in _GROUND_EDGES:
                role = NetRole.GROUND
                break  # ground is decisive
            if ename in _POWER_EDGES:
                role = NetRole.POWER
        roles[name] = role
    return roles


# --------------------------------------------------------------------------------------
# Hierarchy -> sheet tree
# --------------------------------------------------------------------------------------
@dataclass
class SheetIR:
    """One schematic sheet, mirroring an ``.ato`` module in the design hierarchy.

    The root sheet (``parent is None``) maps to the app module; each child maps to an
    ``is_ato_module`` instance that (transitively) contains designated components.
    ``uuid`` / ``file_stem`` are assigned by the emitter at render time.
    """

    name: str
    node_id: str  # stable id of the backing module node ("" only for a synthetic root)
    components: list[ComponentIR] = field(default_factory=list)
    children: list["SheetIR"] = field(default_factory=list)
    uuid: str = ""  # sheet-instance uuid; "" => this is the root sheet
    file_stem: str = ""  # child .kicad_sch filename stem; "" => root file

    def walk(self) -> "list[SheetIR]":
        """Pre-order: self then descendants (root first)."""
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


def _ato_module_ids(app: fabll.Node) -> set[str]:
    """Stable ids of every ``is_ato_module`` instance (empty if unavailable).

    ``is_ato_module`` lives in the atopile compiler layer; importing it lazily keeps the
    exporter importable without atopile and degrades to a single flat sheet if the trait
    is missing (e.g. a graph built directly via fabll rather than from ``.ato``).
    """
    try:
        from atopile.compiler.ast_visitor import is_ato_module
    except Exception:  # noqa: BLE001 - no atopile layer -> flat sheet
        return set()

    tg, g = app.tg, app.g
    return {
        fabll.Traits.bind(t).get_obj_raw().get_root_id()
        for t in fabll.Traits.get_implementors(is_ato_module.bind_typegraph(tg), g=g)
    }


def build_sheet_tree(components: list[ComponentIR], app: fabll.Node) -> SheetIR:
    """Group ``components`` into a sheet tree following the ``.ato`` module hierarchy.

    Each component lands on the sheet of its **nearest enclosing ``is_ato_module``**;
    nested modules become nested sheets. Components with no module ancestry (or when the
    ``is_ato_module`` trait is unavailable) fall onto the root sheet, so the result is
    always a valid single-root tree.
    """
    ato_ids = _ato_module_ids(app)
    root = SheetIR(name=app.get_name(), node_id=app.get_root_id())
    if not ato_ids:
        root.components = list(components)
        return root

    for comp in components:
        chain: list[fabll.Node] = []
        if comp.module is not None:
            chain = [
                node
                for node, _name in comp.module.get_hierarchy()
                if node.get_root_id() in ato_ids
            ]
        sheet = root
        for node in chain:
            nid = node.get_root_id()
            if nid == root.node_id:
                continue  # app root is the root sheet itself
            existing = next((c for c in sheet.children if c.node_id == nid), None)
            if existing is None:
                existing = SheetIR(name=node.get_name(), node_id=nid)
                sheet.children.append(existing)
            sheet = existing
        sheet.components.append(comp)

    # Refine the purely-structural grouping with connectivity (see the two helpers).
    # Flatten FIRST: dissolving a tiny leaf wrapper (e.g. an op-amp package) into its
    # functional parent removes it as a pull-in target, so the parent's own passives stay
    # on the parent instead of being dragged down into the wrapper. Then pull parent-level
    # orphans down onto the descendant sheet their nets are dominated by.
    _flatten_small_leaves(root)
    _pull_in_by_connectivity(root)

    # Deterministic order for stable output.
    for sheet in root.walk():
        sheet.children = natsorted(sheet.children, key=lambda s: (s.name, s.node_id))
    return root


# Connectivity-grouping heuristics (tune here if a board groups oddly).
PULL_IN_DOMINANCE = 0.5  # min fraction of a component's net-affinity that one descendant
#                          sheet must hold to claim the component from an ancestor sheet.
FLATTEN_MAX_COMPONENTS = 3  # a non-root leaf sheet this small is merged into its parent.


def _net_degree(components: list[ComponentIR]) -> dict[str, int]:
    """Pin count per net — the affinity denominator (a high-degree rail is discounted)."""
    deg: dict[str, int] = {}
    for c in components:
        for p in c.pins:
            if p.net:
                deg[p.net] = deg.get(p.net, 0) + 1
    return deg


def _pull_in_by_connectivity(root: SheetIR) -> None:
    """Move each component onto the descendant sheet its connectivity is dominated by.

    The structural pass puts a component on the sheet of the ``.ato`` module it is
    *declared* in, which strands passives declared at a parent level that really belong
    to one child subcircuit (e.g. an RS-485 termination resistor declared in ``App`` but
    wired only to the transceiver). For each component we score every **strict descendant**
    of its current sheet by net affinity — ``Σ 1/degree(net)`` over the component's nets
    that touch a component owned by that descendant (same weighting as
    :func:`placement.place_components`, so shared rails like ``GND``/``+3V3`` barely
    count) — and relocate it when one descendant holds at least ``PULL_IN_DOMINANCE`` of
    its total affinity. A component spanning two subcircuits has no dominant descendant
    and stays put. Only *downward* moves are allowed, so the hierarchy is never crossed.
    """
    all_components = [c for s in root.walk() for c in s.components]
    degree = _net_degree(all_components)

    # nets owned by each sheet (its own components only).
    own_nets: dict[int, set[str]] = {
        id(s): {p.net for c in s.components for p in c.pins if p.net} for s in root.walk()
    }

    def _descendants(sheet: SheetIR) -> list[SheetIR]:
        out: list[SheetIR] = []
        for ch in sheet.children:
            out.append(ch)
            out.extend(_descendants(ch))
        return out

    # depth per sheet, for tie-breaking toward the most specific sheet.
    depth: dict[int, int] = {}

    def _set_depth(sheet: SheetIR, d: int) -> None:
        depth[id(sheet)] = d
        for ch in sheet.children:
            _set_depth(ch, d + 1)

    _set_depth(root, 0)

    for sheet in root.walk():
        descendants = _descendants(sheet)
        if not descendants:
            continue
        # Iterate a snapshot; we mutate sheet.components as we relocate.
        for comp in list(sheet.components):
            nets = {p.net for p in comp.pins if p.net}
            total = sum(1.0 / max(degree.get(n, 1), 1) for n in nets)
            if total <= 0:
                continue
            best: SheetIR | None = None
            best_key: tuple[float, int, str] | None = None
            for d in descendants:
                shared = nets & own_nets[id(d)]
                if not shared:
                    continue
                score = sum(1.0 / max(degree.get(n, 1), 1) for n in shared)
                key = (score, depth[id(d)], d.name)
                if best_key is None or key > best_key:
                    best, best_key = d, key
            # Strict: a 50/50 interconnect (best == remaining) stays on the ancestor.
            if best is not None and best_key[0] > PULL_IN_DOMINANCE * total:
                sheet.components.remove(comp)
                best.components.append(comp)
                own_nets[id(sheet)] = {
                    p.net for c in sheet.components for p in c.pins if p.net
                }
                own_nets[id(best)].update(nets)


def _flatten_small_leaves(root: SheetIR) -> None:
    """Merge tiny leaf sub-module sheets into their parent (keep functional blocks whole).

    A nested wrapper module (e.g. an op-amp *package* inside a filter module) otherwise
    gets its own sheet, splitting one functional block in two. Post-order, a **leaf**
    sheet with ``≤ FLATTEN_MAX_COMPONENTS`` components is absorbed into its parent — but
    only when the parent is **not the root**, so top-level functional modules
    (``buck``/``ldo``/``dac``/…) always keep their own sheet.
    """

    def _recurse(sheet: SheetIR, is_root: bool) -> None:
        for ch in sheet.children:
            _recurse(ch, False)
        kept: list[SheetIR] = []
        for ch in sheet.children:
            if (
                not is_root
                and not ch.children
                and len(ch.components) <= FLATTEN_MAX_COMPONENTS
            ):
                sheet.components.extend(ch.components)
            else:
                kept.append(ch)
        sheet.children = kept

    _recurse(root, True)


@dataclass
class InstancePath:
    """A component's KiCad instance path + sheet metadata, shared by both emitters."""

    full_path: str  # "/<sheet-uuid>/…/<comp-uuid>" (root-sheet parts are just "/<uuid>")
    sheet_name: str
    sheet_file: str  # "<stem>.kicad_sch"


def compute_instance_paths(
    components: list[ComponentIR], app: fabll.Node, *, root_stem: str
) -> dict[str, InstancePath]:
    """Map each component **atopile address** to its KiCad instance path.

    Authoritative source of the schematic⇆PCB linkage: the hierarchical schematic emitter
    and the PCB transformer both call this so a footprint's ``(path …)`` is byte-identical
    to its symbol's ``symbol_instances`` path (KiCad cross-probes by matching them). Built
    on the same :func:`build_sheet_tree`, with deterministic UUIDs (:func:`stable_uuid`)
    derived from the sheet node id / component address, so it is stable across rebuilds.
    """
    root = build_sheet_tree(components, app)
    result: dict[str, InstancePath] = {}
    seen_stems: set[str] = set()

    def _stem(base: str) -> str:  # mirror render_sheet_tree's _unique_stem
        stem, n = base, 2
        while stem in seen_stems:
            stem = f"{base}-{n}"
            n += 1
        seen_stems.add(stem)
        return stem

    def _walk(
        sheet: SheetIR, chain: list[str], names: list[str], is_root: bool
    ) -> None:
        if is_root:
            file_stem, name_path = root_stem, names
        else:
            chain = chain + [stable_uuid(sheet.node_id)]
            name_path = names + [sanitize_filepath_part(sheet.name) or "sheet"]
            file_stem = _stem(f"{root_stem}-{'-'.join(name_path)}")
        sheet_file = f"{file_stem}.kicad_sch"
        for comp in sheet.components:
            key = comp.address or comp.ref
            full = "/" + "/".join(chain + [stable_uuid(key)])
            result[key] = InstancePath(full, sheet.name, sheet_file)
        for ch in sheet.children:
            _walk(ch, chain, name_path, False)

    _walk(root, [], [], True)
    return result


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
        # Parsed-symbol cache keyed by file path: a design reuses the same .kicad_sym
        # across many components (every 0603 cap), so parse each file once. ``None`` is
        # cached too, so a known-bad file isn't re-parsed per component (CODE_AUDIT Q5).
        self._real_cache: dict[Path, SymbolDef | None] = {}
        self.fallback_count = 0

    def _real_symbol(self, sym_path: Path) -> SymbolDef | None:
        if sym_path not in self._real_cache:
            self._real_cache[sym_path] = real_symbol_from_file(sym_path)
        return self._real_cache[sym_path]

    def resolve(self, comp: ComponentIR) -> SymbolDef:
        if comp.module is not None:
            sym_path = _find_symbol_file(comp.module, self._search_dirs)
            if sym_path is not None:
                real = self._real_symbol(sym_path)
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

    def add(self, sym: SymbolDef) -> SymbolDef:
        """Register an externally-built symbol (e.g. a power symbol) for lib text."""
        return self._by_lib_id.setdefault(sym.lib_id, sym)

    def lib_symbols_text(self) -> str:
        return "\n".join(
            self._by_lib_id[k].lib_symbol_text for k in sorted(self._by_lib_id)
        )

    def lib_symbols_text_for(self, lib_ids: set[str]) -> str:
        """``lib_symbols`` text for just the symbols used on one sheet (sorted)."""
        return "\n".join(
            self._by_lib_id[k].lib_symbol_text
            for k in sorted(lib_ids)
            if k in self._by_lib_id
        )


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------
def _instance_block(comp: ComponentIR, sym: SymbolDef, ix: float, iy: float) -> str:
    pin_lines = "\n".join(
        f'    (pin "{escape(num)}" (uuid {stable_uuid(f"{comp.inst_uuid}:pin:{num}")}))'
        for num in sym.pin_xy
    )
    # Anchor ref above / value below the symbol's real extent (legacy fixed offsets
    # collide on anything bigger or smaller than the old one-size grid cell).
    bb = sym.bbox or (-7.62, -12.7, 7.62, 12.7)
    ref_y = iy - bb[3] - 1.27
    val_y = iy - bb[1] + 1.27
    return (
        f'  (symbol (lib_id "{sym.lib_id}") (at {ix} {iy} 0) (unit 1)\n'
        f"    (in_bom yes) (on_board yes) (fields_autoplaced)\n"
        f"    (uuid {comp.inst_uuid})\n"
        f'    (property "Reference" "{escape(comp.ref)}" (id 0)'
        f" (at {ix + bb[0]} {ref_y} 0)"
        f" (effects (font (size 1.27 1.27)) (justify left)))\n"
        f'    (property "Value" "{escape(comp.value)}" (id 1)'
        f" (at {ix + bb[0]} {val_y} 0)"
        f" (effects (font (size 1.27 1.27)) (justify left)))\n"
        f'    (property "Footprint" "" (id 2) (at {ix} {iy} 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f'    (property "Datasheet" "" (id 3) (at {ix} {iy} 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f"{pin_lines}\n"
        f"  )"
    )


def _power_instance_block(
    lib_id: str,
    value: str,
    ref: str,
    x: float,
    y: float,
    inst_uuid: str,
    rot: int = 0,
    value_at: tuple[float, float, str] | None = None,
) -> str:
    """A single-pin power-symbol (or PWR_FLAG) instance at ``(x, y)``, hidden ref.

    ``value_at`` overrides the net-name text position/justify (kept horizontal at
    angle 0 regardless of ``rot`` so rotated glyphs never get vertical text).
    """
    vx, vy, vjust = value_at if value_at else (x, y - 3.81, "")
    vj = f" (justify {vjust})" if vjust else ""
    veffects = f"(effects (font (size 1.27 1.27)){vj})"
    # Property text angle is relative to the instance rotation; compensate so the
    # net-name text stays horizontal whatever way the glyph points.
    vrot = (360 - rot) % 360
    return (
        f'  (symbol (lib_id "{lib_id}") (at {x} {y} {rot}) (unit 1)\n'
        f"    (in_bom yes) (on_board yes) (fields_autoplaced)\n"
        f"    (uuid {inst_uuid})\n"
        f'    (property "Reference" "{escape(ref)}" (id 0) (at {x} {y - 5.08} 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f'    (property "Value" "{escape(value)}" (id 1) (at {vx} {vy} {vrot})'
        f" {veffects})\n"
        f'    (property "Footprint" "" (id 2) (at {x} {y} 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f'    (property "Datasheet" "" (id 3) (at {x} {y} 0)'
        f" (effects (font (size 1.27 1.27)) hide))\n"
        f'    (pin "1" (uuid {stable_uuid(f"{inst_uuid}:pin:1")}))\n'
        f"  )"
    )


def _label_block(
    net: str, x: float, y: float, rot: int = 180, justify: str = "right"
) -> str:
    return (
        f'  (global_label "{escape(net)}" (shape bidirectional) (at {x} {y} {rot})'
        f" (fields_autoplaced)\n"
        f"    (effects (font (size 1.27 1.27)) (justify {justify}))\n"
        f"    (uuid {stable_uuid(f'label:{net}:{x}:{y}:{rot}')}))"
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
        f"    (uuid {stable_uuid(f'wire:{x1}:{y1}:{x2}:{y2}')}))"
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


def _sheet_ref_block(sheet: SheetIR, x: float, y: float) -> str:
    """A ``(sheet …)`` reference block placed in a parent sheet's file."""
    fname = f"{sheet.file_stem}.kicad_sch"
    return (
        f"  (sheet (at {x} {y}) (size {SHEET_W} {SHEET_H}) (fields_autoplaced)\n"
        f"    (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0000))\n"
        f"    (uuid {sheet.uuid})\n"
        f'    (property "Sheetname" "{escape(sheet.name)}" (id 0) (at {x} {y - 0.7} 0)'
        f" (effects (font (size 1.27 1.27)) (justify left bottom)))\n"
        f'    (property "Sheetfile" "{escape(fname)}" (id 1)'
        f" (at {x} {y + SHEET_H + 0.7} 0)"
        f" (effects (font (size 1.27 1.27)) (justify left top)))\n"
        f"  )"
    )


def render_hierarchical(
    components: list[ComponentIR],
    net_names: set[str],
    app: fabll.Node,
    *,
    root_stem: str,
    search_dirs: list[Path],
    net_roles: dict[str, NetRole] | None = None,
) -> tuple[dict[str, str], SchematicSummary]:
    """Render a hierarchical, real-symbol schematic: one sheet per ``.ato`` module.

    Returns ``{filename: sexp_text}`` — the root file is ``f"{root_stem}.kicad_sch"``;
    each module becomes a child ``.kicad_sch`` beside it. Real cached symbols are used
    where available (generic box otherwise) and every pin carries a ``global_label`` so
    connectivity rides on label names: the netlist is complete with no wires, and a
    human can rearrange/wire freely without breaking it. All instances are centralised
    in the root's ``(symbol_instances)`` with their full ``/<sheet…>/<symbol>`` paths.
    """
    if net_roles is None:
        net_roles = classify_nets(app)
    root = build_sheet_tree(components, app)
    return render_sheet_tree(
        root,
        net_names,
        root_stem=root_stem,
        search_dirs=search_dirs,
        net_roles=net_roles,
    )


_PWR_FLAG_LIB_ID = f"{LIB_PREFIX}:PWR_FLAG"


def render_sheet_tree(
    root: SheetIR,
    net_names: set[str],
    *,
    root_stem: str,
    search_dirs: list[Path],
    net_roles: dict[str, NetRole] | None = None,
) -> tuple[dict[str, str], SchematicSummary]:
    """Emit ``{filename: sexp_text}`` for a prebuilt :class:`SheetIR` tree.

    Separated from :func:`render_hierarchical` so emission (sheet files, instance paths,
    cross-sheet label connectivity) can be exercised without a built ``.ato`` app. When
    ``net_roles`` marks a pin's net ``POWER``/``GROUND``, that pin gets a power-symbol
    glyph (and the net one shared ``PWR_FLAG`` driver) instead of a ``global_label``;
    otherwise every pin is labelled. Default ``None`` -> all labels.
    """
    roles = net_roles or {}
    registry = _SymbolRegistry(search_dirs)
    flag_def = build_pwr_flag_symbol(_PWR_FLAG_LIB_ID)
    power_sym_cache: dict[str, SymbolDef] = {}
    flagged_nets: set[str] = set()
    pwr_n = [0]
    flg_n = [0]
    n_components = sum(len(s.components) for s in root.walk())
    summary = SchematicSummary(components=n_components, nets=len(net_names))

    # Global net degree (pin count per net) for placement affinity scoring.
    net_degree: dict[str, int] = {}
    for s in root.walk():
        for comp in s.components:
            for p in comp.pins:
                if p.net:
                    net_degree[p.net] = net_degree.get(p.net, 0) + 1

    # Pass 1: assign each non-root sheet a uuid + filename and its root->sheet path. The
    # filename is **deterministic** — derived from the module's sanitized name path, not
    # the (per-build random) uuid — so rebuilds reuse the same files. ``seen_stems``
    # keeps filenames unique if two name paths sanitize to the same string.
    path_by_sheet: dict[int, list[str]] = {}
    sheet_instances: list[str] = ['    (path "/" (page "1"))']
    page = [1]
    seen_stems: set[str] = set()

    def _unique_stem(base: str) -> str:
        stem, n = base, 2
        while stem in seen_stems:
            stem = f"{base}-{n}"
            n += 1
        seen_stems.add(stem)
        return stem

    def _assign(
        sheet: SheetIR, prefix: list[str], names: list[str], is_root: bool
    ) -> None:
        if is_root:
            sheet.file_stem = root_stem
            path_by_sheet[id(sheet)] = []
            name_path = names
        else:
            # Deterministic, stable across rebuilds and shared with the PCB footprint
            # paths (see compute_instance_paths) so the two files cross-probe.
            sheet.uuid = stable_uuid(sheet.node_id)
            name_path = names + [sanitize_filepath_part(sheet.name) or "sheet"]
            sheet.file_stem = _unique_stem(f"{root_stem}-{'-'.join(name_path)}")
            path = prefix + [sheet.uuid]
            path_by_sheet[id(sheet)] = path
            page[0] += 1
            sheet_instances.append(
                f'    (path "/{"/".join(path)}" (page "{page[0]}"))'
            )
        for ch in sheet.children:
            _assign(ch, path_by_sheet[id(sheet)], name_path, False)

    _assign(root, [], [], True)

    # Pass 2: emit each sheet file; collect all symbol instances for the root file.
    symbol_instances: list[str] = []
    files: dict[str, str] = {}

    def _emit(sheet: SheetIR, is_root: bool) -> None:
        path = path_by_sheet[id(sheet)]
        instances: list[str] = []
        labels: list[str] = []
        wires: list[str] = []
        used: set[str] = set()

        def _place_power(
            net: str,
            ground: bool,
            lx: float,
            ly: float,
            outward: tuple[int, int] | None,
        ) -> None:
            """Auto-wire a rail pin: stub wire outward to an oriented power glyph
            (+ one shared PWR_FLAG per net at the first stub end).

            With no usable direction (off-axis pin) the glyph sits directly on the
            pin point — the legacy, still-correct form (connection is by pin name).
            """
            # Symbol-library id, e.g. "atopile:GND_<net>" — internal only (the displayed
            # Value/net is ``net``). Avoid doubling (``GND_GND``) when the net is already
            # named for its rail (the shared "GND", or a "+5V" power net).
            _pfx = "GND" if ground else "PWR"
            _suffix = sanitize_filepath_part(net) or "net"
            _name = _suffix if _suffix.upper().startswith(_pfx) else f"{_pfx}_{_suffix}"
            lib_id = f"{LIB_PREFIX}:{_name}"
            psym = power_sym_cache.get(lib_id)
            if psym is None:
                psym = build_power_symbol(lib_id, net, ground=ground)
                power_sym_cache[lib_id] = psym
            registry.add(psym)
            used.add(lib_id)

            sx, sy, rot, value_at = lx, ly, 0, None
            flag_rot, flag_value_at = 0, None
            if outward is not None:
                dx, dy = outward
                sx, sy = lx + dx * POWER_STUB, ly + dy * POWER_STUB
                rot = (_GND_ROT if ground else _PWR_ROT)[outward]
                wires.append(_wire_block(lx, ly, sx, sy))
                summary.wires += 1
                # Net-name text just past the glyph tip, horizontal, reading outward
                # (adjacent 2.54-pitch rail pins would otherwise collide vertically).
                glyph = 2.54
                tx, ty = sx + dx * (glyph + 0.64), sy + dy * (glyph + 1.6)
                value_at = (tx, ty, "left" if dx > 0 else ("right" if dx < 0 else ""))
                # PWR_FLAG body sits perpendicular to the stub so it never overlaps
                # the glyph; its text goes at the flag tip.
                fdx, fdy = ((0, -1) if dy == 0 else (1, 0))
                flag_rot = _PWR_ROT[(fdx, fdy)]
                ftx, fty = sx + fdx * (glyph + 0.64), sy + fdy * (glyph + 1.6)
                flag_value_at = (
                    ftx,
                    fty,
                    "left" if fdx > 0 else ("right" if fdx < 0 else ""),
                )

            pwr_n[0] += 1
            # Deterministic (power symbols have no footprint, but stable uuids keep the
            # emitted schematic byte-stable across rebuilds -> no git churn).
            inst = stable_uuid(f"pwr:{sheet.node_id}:{net}:{pwr_n[0]}")
            instances.append(
                _power_instance_block(
                    lib_id, net, f"#PWR{pwr_n[0]:04d}", sx, sy, inst, rot, value_at
                )
            )
            symbol_instances.append(
                f'    (path "/{"/".join(path + [inst])}"'
                f' (reference "#PWR{pwr_n[0]:04d}") (unit 1)'
                f' (value "{escape(net)}") (footprint ""))'
            )
            summary.power_symbols += 1
            if net in flagged_nets:
                return
            # First sighting of this rail net -> add one PWR_FLAG driver here.
            flagged_nets.add(net)
            registry.add(flag_def)
            used.add(flag_def.lib_id)
            flg_n[0] += 1
            finst = stable_uuid(f"flag:{sheet.node_id}:{net}:{flg_n[0]}")
            instances.append(
                _power_instance_block(
                    flag_def.lib_id,
                    "PWR_FLAG",
                    f"#FLG{flg_n[0]:04d}",
                    sx,
                    sy,
                    finst,
                    flag_rot,
                    flag_value_at,
                )
            )
            symbol_instances.append(
                f'    (path "/{"/".join(path + [finst])}"'
                f' (reference "#FLG{flg_n[0]:04d}") (unit 1)'
                f' (value "PWR_FLAG") (footprint ""))'
            )

        # Resolve symbols first, then place the whole sheet by connectivity:
        # ICs anchor, passives orbit the anchor they share the most nets with.
        sym_by_ref = {c.ref: registry.resolve(c) for c in sheet.components}
        # Keep clear of the child-sheet reference column when one exists.
        place_origin = (58.42, 25.4) if sheet.children else (25.4, 25.4)
        positions = place_components(
            sheet.components, sym_by_ref, net_degree, origin=place_origin
        )

        for comp in sheet.components:
            sym = sym_by_ref[comp.ref]
            used.add(sym.lib_id)
            # Deterministic UUID derived from the atopile address so the matching PCB
            # footprint can be stamped with the identical instance path (cross-probe).
            comp.inst_uuid = stable_uuid(comp.address or comp.ref)
            ix, iy = positions[comp.ref]
            instances.append(_instance_block(comp, sym, ix, iy))

            net_by_num = {p.number: p.net for p in comp.pins}
            for num, (px, py) in sym.pin_xy.items():
                net = net_by_num.get(num)
                if net is None:
                    continue
                lx, ly = ix + px, iy - py
                geo = sym.pin_geo.get(num)
                outward = _screen_outward(geo) if geo else None
                role = roles.get(net, NetRole.SIGNAL)
                if role is NetRole.POWER or role is NetRole.GROUND:
                    _place_power(net, role is NetRole.GROUND, lx, ly, outward)
                else:
                    rot, justify = _LABEL_ORIENT.get(outward, (180, "right"))
                    labels.append(_label_block(net, lx, ly, rot, justify))
                    summary.labels += 1
            summary.unmapped_pads += sum(
                1 for num in net_by_num if num not in sym.pin_xy
            )

            inst_path = "/".join(path + [comp.inst_uuid])
            symbol_instances.append(
                f'    (path "/{inst_path}" (reference "{escape(comp.ref)}") (unit 1)'
                f' (value "{escape(comp.value)}") (footprint ""))'
            )

        # Child-sheet reference blocks live in *this* sheet's file.
        sheet_refs = [
            _sheet_ref_block(ch, SHEET_X, SHEET_Y0 + ci * SHEET_DY)
            for ci, ch in enumerate(sheet.children)
        ]
        # Recurse first so the root's doc (assembled below) sees every symbol instance.
        for ch in sheet.children:
            _emit(ch, False)

        # Content-aware paper size: component extent + the child-sheet ref column.
        ex, ey = sheet_extent(positions, sym_by_ref)
        if sheet.children:
            ex = max(ex, SHEET_X + SHEET_W)
            ey = max(ey, SHEET_Y0 + len(sheet.children) * SHEET_DY)
        paper = _pick_paper((ex, ey))
        title_block = (
            f'  (title_block (title "{escape(sheet.name)}")'
            f' (comment 1 "generated by atopile"))\n'
        )

        body = (
            f"  (lib_symbols\n{registry.lib_symbols_text_for(used)}\n  )\n"
            f"{chr(10).join(instances)}\n"
            f"{chr(10).join(sheet_refs)}\n"
            f"{chr(10).join(wires)}\n"
            f"{chr(10).join(labels)}\n"
        )
        # Deterministic per-file uuid (stable across rebuilds -> less git churn).
        file_uuid = stable_uuid("file:" + sheet.node_id)
        if is_root:
            doc = (
                f"(kicad_sch (version {SCH_VERSION}) (generator eeschema)\n"
                f"  (uuid {file_uuid})\n"
                f'  (paper "{paper}")\n'
                f"{title_block}"
                f"{body}"
                f"  (sheet_instances\n{chr(10).join(sheet_instances)}\n  )\n"
                f"  (symbol_instances\n{chr(10).join(symbol_instances)}\n  )\n"
                f")\n"
            )
        else:
            doc = (
                f"(kicad_sch (version {SCH_VERSION}) (generator eeschema)\n"
                f"  (uuid {file_uuid})\n"
                f'  (paper "{paper}")\n'
                f"{title_block}"
                f"{body}"
                f")\n"
            )
        files[f"{sheet.file_stem}.kicad_sch"] = doc

    _emit(root, True)
    summary.fallback_symbols = registry.fallback_count
    return files, summary


class SchematicMode(str, Enum):
    HIERARCHICAL = "hierarchical"  # real symbols, sheet per module, labels (default)
    WIRED = "wired"  # provably short-free ladder of generic boxes
    LABELS = "labels"  # flat grid of real symbols with a label per pin


def export_schematic(
    app: fabll.Node,
    *,
    target_name: str,
    out_path: Path,
    parts_search_dirs: list[Path] | None = None,
    mode: SchematicMode = SchematicMode.HIERARCHICAL,
    draw_wires: bool | None = None,
) -> SchematicSummary:
    """
    Export a connectivity schematic for ``app`` to ``out_path``.

    ``mode`` selects the view:

    - ``hierarchical`` (default): real cached symbols (generic box otherwise) placed on
      one ``.kicad_sch`` **sheet per ``.ato`` module**, with a net-name ``global_label``
      on every pin. Connectivity rides on the labels, so the netlist is complete without
      wires and a human can rearrange/route it without breaking it. Child sheet files
      are written beside ``out_path`` (the root).
    - ``wired``: a provably short-free ladder of generic bottom-pin boxes, drawn nets.
    - ``labels``: a flat grid of real symbols with a label per pin (single file).

    ``draw_wires`` is the legacy boolean (``True`` -> ``wired``, else ``labels``) and
    overrides ``mode`` when given.
    """
    if draw_wires is not None:
        mode = SchematicMode.WIRED if draw_wires else SchematicMode.LABELS

    components, net_names = extract_components(app)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if mode is SchematicMode.HIERARCHICAL:
        files, summary = render_hierarchical(
            components,
            net_names,
            app,
            root_stem=out_path.stem,
            search_dirs=parts_search_dirs or [],
        )
        for fname, doc in files.items():
            (out_path.parent / fname).write_text(doc, encoding="utf-8")
        # Remove stale child sheets from earlier builds (the root has no "-", so the
        # glob never matches it). Filenames are deterministic, so this only deletes
        # sheets no longer emitted (renamed/removed modules, old random-suffixed ones).
        for old in out_path.parent.glob(f"{out_path.stem}-*.kicad_sch"):
            if old.name not in files:
                old.unlink()
    else:
        if mode is SchematicMode.WIRED:
            doc, summary = render_wired(components, net_names)
        else:
            doc, summary = render(components, net_names, parts_search_dirs or [])
        out_path.write_text(doc, encoding="utf-8")

    summary.path = str(out_path)
    logger.info("Exported schematic %s (%s)", out_path, summary)
    return summary
