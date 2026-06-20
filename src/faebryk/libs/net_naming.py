# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Generator, Iterable, Mapping

import faebryk.core.faebrykpy as fbrk
import faebryk.core.node as fabll
import faebryk.library._F as F
from atopile.errors import UserException
from faebryk.libs.nets import get_named_net
from faebryk.libs.util import FuncDict, groupby

logger = logging.getLogger(__name__)


# FIXME: this belongs at most in the KiCAD netlist generator
# and should likely just return the properties rather than mutating the graph
@dataclass
class _NetName:
    base_name: str | None = None
    prefix: str | None = None
    suffix: int | None = None
    required_prefix: str | None = None
    required_suffix: str | None = None

    @property
    def name(self) -> str:
        """
        Get the name of the net.
        Net names should take the form of: <prefix>-<base_name>-<suffix>
        There must always be some base, and if it's not provided, it's just 'net'
        Prefixes and suffixes are joined with a "-" if they exist.
        """
        base_name = self.base_name or "net"
        prefix = f"{self.prefix}-" if self.prefix else ""
        numeric_suffix = f"-{self.suffix}" if self.suffix is not None else ""
        required_prefix = self.required_prefix or ""
        required_suffix = self.required_suffix or ""

        # Order: prefix + required_prefix + base + numeric_suffix + required_suffix
        # Ensures diff-pair affix (_P/_N) is last, e.g. "line-1_N"
        return f"{prefix}{required_prefix}{base_name}{numeric_suffix}{required_suffix}"


def _conflicts(
    names: Mapping[F.Net, _NetName],
) -> Generator[Iterable[F.Net], None, None]:
    for items in groupby(names.items(), lambda it: it[1].name).values():
        if len(items) > 1:
            yield [net for net, _ in items]


def _name_shittiness(name: str | None) -> float:
    """Caesar says 👎"""

    # These are completely shit names that
    # have no business existing
    if name is None:
        return 0

    if name == "net":
        # By the time we're here, we have a bunch
        # of pads with the name net attached
        return 0

    if name == "":
        return 0

    if name.startswith("unnamed"):
        return 0

    if re.match(r"^(_\d+|p\d+)$", name):
        return 0

    # Here are some shitty patterns, that are
    # fine as a backup, but should be avoided

    # Anything that starts with an underscore
    # is probably a temporary name
    if name.startswith("_"):
        return 0.2

    # "hv" is common from power interfaces, but
    # if there's something else available, prefer that
    if name == "hv":
        return 0.3

    # "line" is common from signal interfaces, but
    # if there's something else available, prefer that
    if name == "line":
        return 0.3

    # Anything with a trailing number is
    # generally less interesting
    if re.match(r".*\d+$", name):
        return 0.5

    return 1


def _get_net_stable_key(net: F.Net) -> tuple[str, ...]:
    """Return a deterministic key for a net based on connected interface paths.

    Using the sorted list of stable module-interface paths ensures that
    operations that depend on iteration order (e.g. suffix assignment)
    are repeatable across runs for the same design.
    """
    try:
        mifs = net.get_connected_interfaces()
    except Exception:
        # In case a backend returns a generator that raises mid-iteration,
        # fall back to an empty key which still sorts deterministically.
        return tuple()

    stable_names = sorted(m.get_full_name(include_uuid=False) for m in mifs)
    return tuple(stable_names)


# ---------------------------------------------------------------------------------------
# Power / ground rail naming (voltage-aware rails, shared GND) + device-pin fallback.
#
# The default implicit naming yields generic, fragmented names for power infrastructure
# ("hv", "lv", "c1-power-hv", "rs485-power-lv"). The helpers below give power rails a
# voltage-derived name ("+5V", "+3V3"), collapse all non-isolated ground nets to a single
# "GND" (flagging fragments), and, when no purposeful name exists, fall back to the pin name
# of a connected device, preferring ICs over passives.
# ---------------------------------------------------------------------------------------

# Mirrors the schematic emitter's classify_nets edge sets (schematic.py).
_POWER_RAIL_EDGES = {"hv", "vcc"}
_GROUND_RAIL_EDGES = {"lv", "gnd"}

# Designator-prefix tiers for the pin-name fallback: ICs win over passives.
_IC_DESIGNATOR_PREFIXES = {"U", "IC"}
_PASSIVE_DESIGNATOR_PREFIXES = {"R", "C", "L", "FB", "RN", "CN", "Y", "X"}

# Generic interface leaves that make poor net names (never used as a pin-name fallback).
_GENERIC_PIN_NAMES = {"line", "hv", "lv", "p", "n", "vcc", "gnd"}


def _rail_marker_is_passive(pnode: fabll.Node) -> bool:
    """True if this ``ElectricPower`` belongs to a 2-pin passive (R/C/L/…).

    ``Capacitor``/``Resistor``/``Inductor`` carry a convenience ``.power`` interface that is
    auto-bonded to their two pins (``power.lv ~ pin2``). It is just a pin alias, **not** an
    authoritative rail — e.g. a bootstrap cap's ``power.lv`` lands on the buck SW node, which
    is not ground. So a passive's ``.power`` must not classify a net as power/ground.
    """
    return _component_designator_prefix(pnode) in _PASSIVE_DESIGNATOR_PREFIXES


def _net_rail_role(net: F.Net) -> str | None:
    """Classify a net as ``"ground"`` / ``"power"`` rail, or None.

    A net is a rail if any connected electrical is the ``lv``/``gnd`` (ground) or
    ``hv``/``vcc`` (power) child of a *non-passive* ``ElectricPower``. Ground wins ties,
    matching the schematic emitter's ``classify_nets``.
    """
    role: str | None = None
    for iface in net.get_connected_interfaces():
        parent = iface.get_parent()
        if parent is None:
            continue
        pnode, ename = parent
        if ename not in _GROUND_RAIL_EDGES and ename not in _POWER_RAIL_EDGES:
            continue
        try:
            if not pnode.isinstance(F.ElectricPower):
                continue
        except Exception:  # noqa: BLE001 - type probe is best-effort
            continue
        if _rail_marker_is_passive(pnode):
            continue  # a passive's .power is a pin alias, not a rail marker
        if ename in _GROUND_RAIL_EDGES:
            return "ground"  # ground is decisive
        role = "power"
    return role


def _format_rail_voltage(volts: float) -> str:
    """Format a rail voltage as a net name, e.g. ``5.0 -> "+5V"``, ``3.3 -> "+3V3"``.

    Engineering style: the decimal point is replaced by the unit letter (``3V3``), the sign
    is always explicit. This ``+3V3`` spelling is the single cosmetic choice — switch the
    body construction here for the dotted ``+3.3V`` form if preferred.
    """
    sign = "-" if volts < 0 else "+"
    whole, frac = f"{round(abs(volts), 2):.2f}".split(".")
    frac = frac.rstrip("0")
    body = f"{whole}V{frac}" if frac else f"{whole}V"
    return f"{sign}{body}"


def _extract_voltage_stat(
    voltage_param: fabll.Node, solver: object | None
) -> tuple[float, float] | None:
    """Read a rail voltage as ``(nominal_volts, relative_width)``, or None if unknown.

    ``nominal`` is the midpoint of the extracted subset; ``relative_width`` =
    ``(max-min)/|nominal|`` measures how tightly the rail is specified. A near-zero nominal
    is treated as unknown (``+0V`` is meaningless).
    """
    values: list[float] | None = None
    try:
        values = voltage_param.get_values()
    except Exception:  # noqa: BLE001
        values = None
    if not values and solver is not None and hasattr(solver, "try_extract_superset"):
        try:
            param_trait = voltage_param.get_trait(F.Parameters.is_parameter)
            lit = solver.try_extract_superset(param_trait)  # type: ignore[attr-defined]
            if lit is not None:
                disp = voltage_param.force_get_display_units()
                values = lit.convert_to_unit(
                    disp, g=voltage_param.g, tg=voltage_param.tg
                ).get_values()
        except Exception:  # noqa: BLE001 - degrade to no voltage name
            values = None
    values = [v for v in (values or []) if math.isfinite(v)]
    if not values:
        return None
    nominal = sum(values) / len(values)
    if abs(nominal) < 0.05:
        return None
    rel_width = (max(values) - min(values)) / abs(nominal)
    return nominal, rel_width


def _rail_voltage_name(net: F.Net, solver: object | None) -> str | None:
    """Voltage-derived base name (``+5V``) for a power-rail net, or None if unconstrained.

    A rail net usually spans several bus-connected ``ElectricPower``s. Some are loosely
    constrained (a sink that merely tolerates 1.7..5.5 V), so their midpoints drift off the
    rail's nominal. Pick the member with the **tightest** relative spec — that is the
    author's actual rail assert (e.g. ``3.3V ±2%`` beats a downstream ``1.7..5.5 V`` input).
    """
    best: tuple[float, float] | None = None  # (relative_width, nominal_volts)
    for iface in net.get_connected_interfaces():
        parent = iface.get_parent()
        if parent is None:
            continue
        pnode, ename = parent
        if ename not in _POWER_RAIL_EDGES:
            continue
        try:
            if not pnode.isinstance(F.ElectricPower):
                continue
            if _rail_marker_is_passive(pnode):
                continue  # a passive's .power voltage is meaningless ({ℝ+})
            voltage_param = pnode.cast(F.ElectricPower).voltage.get()
        except Exception:  # noqa: BLE001
            continue
        stat = _extract_voltage_stat(voltage_param, solver)
        if stat is None:
            continue
        nominal, rel_width = stat
        if best is None or (rel_width, -abs(nominal)) < (best[0], -abs(best[1])):
            best = (rel_width, nominal)
    if best is None:
        return None
    return _format_rail_voltage(best[1])


def _component_designator_prefix(mif: F.Electrical) -> str | None:
    """Designator prefix (``U``/``R``/``C``…) of the component owning this interface."""
    try:
        for node, _name in mif.get_hierarchy():
            if dp := node.try_get_trait(F.has_designator_prefix):
                return dp.get_prefix()
    except fabll.NodeNoParent:
        pass
    return None


def _designator_tier(prefix: str | None) -> int:
    """0 = IC, 1 = other active (Q/D/…), 2 = passive — lower wins the pin-name fallback."""
    if prefix in _IC_DESIGNATOR_PREFIXES:
        return 0
    if prefix in _PASSIVE_DESIGNATOR_PREFIXES:
        return 2
    return 1


def _pin_name_fallback(net: F.Net) -> str | None:
    """Derive a base name from the connected device pin names, ICs prioritized over passives.

    Used only when no suggested/voltage/explicit name exists. An IC+resistor net resolves to
    the IC pin name. Returns None for nets with no footprinted device (e.g. pure-electrical
    unit-test graphs), leaving the existing implicit naming untouched.
    """
    candidates: list[tuple[int, str]] = []
    for mif in net.get_connected_interfaces():
        prefix = _component_designator_prefix(mif)
        if prefix is None:
            continue
        try:
            name = mif.get_name()
        except fabll.NodeNoParent:
            continue
        if not name or name.isdigit() or len(name) <= 1:
            continue  # skip bare pad numbers ("6") and single-char lead names ("a")
        low = name.lower()
        if low in _GENERIC_PIN_NAMES or _name_shittiness(low) < 0.5:
            continue  # skip generic leaves (line/hv/lv/p/n/unnamed/_\d+)
        candidates.append((_designator_tier(prefix), name))
    if not candidates:
        return None
    # Lowest tier (IC) wins; lexicographic tie-break for determinism.
    return min(candidates, key=lambda c: (c[0], c[1]))[1]


def _collect_unnamed_nets(nets: Iterable[F.Net]) -> dict[F.Net, list[F.Electrical]]:
    """Collect nets without overridden names and their connected interfaces."""
    unnamed_nets = [n for n in nets if not n.has_trait(F.has_net_name)]

    nets_with_interfaces = {n: n.get_connected_interfaces() for n in unnamed_nets}

    # Sort nets by stable node names for deterministic ordering
    return dict(
        sorted(
            nets_with_interfaces.items(),
            key=lambda it: [m.get_full_name(include_uuid=False) for m in it[1]],
        )
    )


def _register_named_nets(
    nets: Iterable[F.Net], names: FuncDict[F.Net, _NetName]
) -> None:
    """Register nets that already have overridden names."""
    for net in nets:
        if net.has_trait(F.has_net_name):
            names[net] = _NetName(base_name=net.get_trait(F.has_net_name).get_name())


def _calculate_suggested_name_rank(mif: fabll.Node, base_depth: int) -> int:
    """Calculate rank for a suggested name based on hierarchy."""
    rank = base_depth

    owner_iface = mif.get_parent_of_type(fabll.Node)
    if owner_iface and not owner_iface.isinstance(F.Electrical):
        rank -= 1

    if fabll.Node.nearest_common_ancestor(mif):
        rank -= 1

    return rank


def _extract_net_name_info(
    electrical: F.Electrical,
) -> tuple[set[str], list[tuple[str, int]], dict[str, float]]:
    """Extract naming information from an interface."""
    required_names: set[str] = set()
    suggested_names: list[tuple[str, int]] = []
    implicit_candidates: dict[str, float] = {}

    hierarchy = electrical.get_hierarchy()
    elec_depth = len(hierarchy)

    # Process all found trait instances, prioritizing EXPECTED
    def check_suggested_name(node: fabll.Node, depth: int):
        if has_net_name_suggestion := node.try_get_trait(F.has_net_name_suggestion):
            owner_node = fabll.Traits(has_net_name_suggestion).get_obj_raw()
            if (
                has_net_name_suggestion.level
                == F.has_net_name_suggestion.Level.EXPECTED
            ):
                required_names.add(has_net_name_suggestion.name)
            elif (
                has_net_name_suggestion.level
                == F.has_net_name_suggestion.Level.SUGGESTED
            ):
                # Rank based on hierarchy depth: lower depth = higher priority
                rank = _calculate_suggested_name_rank(owner_node, depth)
                suggested_names.append((has_net_name_suggestion.name, rank))

    check_suggested_name(electrical, 0)

    # TODO not sure it makes sense to have net names on nodes that arent electricals
    # TODO this will just fail all the time
    # Also consider traits on ancestor interfaces in the hierarchy
    try:
        for idx, (node, _name_in_parent) in enumerate(hierarchy):
            if not node.get_parent():
                continue
            if not node.has_trait(fabll.is_interface):
                continue
            # has_net_name is always a required/expected name (no level property)
            if has_net_name := node.try_get_trait(F.has_net_name):
                required_names.add(has_net_name.get_name())
            else:
                check_suggested_name(
                    node, idx
                )  # idx increases as we go further from the electrical

    except fabll.NodeNoParent:
        pass

    # Handle implicit names
    try:
        name = electrical.get_name()
    except fabll.NodeNoParent:
        return required_names, suggested_names, implicit_candidates

    # Adjust depth for interfaces on the same level
    if electrical.get_parent_of_type(fabll.Node):
        elec_depth -= 1

    # Calculate implicit name score
    score = (1 / (elec_depth + 1)) * _name_shittiness(name.lower())
    implicit_candidates[name] = score

    return required_names, suggested_names, implicit_candidates


def _collect_naming_info_from_interfaces(
    mifs: list[F.Electrical],
) -> tuple[set[str], list[tuple[str, int]], dict[str, float]]:
    """Aggregate naming information from multiple interfaces."""
    all_required: set[str] = set()
    all_suggested: list[tuple[str, int]] = []
    all_implicit: dict[str, float] = defaultdict(float)

    for mif in mifs:
        required, suggested, implicit = _extract_net_name_info(mif)
        all_required.update(required)
        all_suggested.extend(suggested)
        for name, score in implicit.items():
            all_implicit[name] += score

    return all_required, all_suggested, all_implicit


def _determine_base_name(
    suggested: list[tuple[str, int]], implicit: dict[str, float]
) -> str | None:
    """Determine the best base name from suggested and implicit candidates."""
    if suggested:
        # Use the suggestion with the lowest rank (highest priority),
        # break ties by lexicographically smallest name for determinism
        return min(suggested, key=lambda x: (x[1], x[0]))[0]

    if implicit:
        # Use the implicit name with the highest score; break ties by name
        best_score = max(implicit.values())
        candidates = [name for name, score in implicit.items() if score == best_score]
        return min(candidates)

    return None


def _process_unnamed_nets(
    unnamed_nets: dict[F.Net, list[F.Electrical]],
    names: FuncDict[F.Net, _NetName],
    solver: object | None,
    ground_nets: list[F.Net],
) -> None:
    """Process nets without names, determining their base names.

    Power rails are named by voltage (``+5V``), ground rails are deferred to
    :func:`_resolve_ground_nets` (collected into ``ground_nets``), and otherwise unnamed nets
    fall back to a connected device pin name (IC over passive) when the implicit name is
    generic.
    """
    for net, mifs in unnamed_nets.items():
        # Collect all naming information
        required, suggested, implicit = _collect_naming_info_from_interfaces(mifs)

        # Handle required names (highest priority)
        if required:
            if len(required) > 1:
                raise UserException(
                    f"Multiple conflicting required net names: {required}"
                )
            fabll.Traits.create_and_add_instance_to(net, F.has_net_name).setup(
                name=required.pop()
            )
            continue

        names[net] = _NetName()

        # Power / ground rails get special treatment.
        role = _net_rail_role(net)
        if role == "ground":
            # Defer to the ground-collapse pass (needs to see all grounds at once).
            ground_nets.append(net)
            continue
        if role == "power":
            # Power rail: the solved voltage is the rail's identity, so it wins over the
            # library pin/interface hints on the net (generic "hv", a diode "anode", …). An
            # explicit author EXPECTED override still wins — it is handled above as a
            # ``required`` name before we get here. Rails without a resolvable voltage fall
            # back to a device pin name, then the implicit name.
            if vname := _rail_voltage_name(net, solver):
                base = vname
            else:
                base = _determine_base_name(suggested, implicit)
                if base is None or _is_generic_name(base):
                    base = _pin_name_fallback(net) or base
            names[net].base_name = base
            continue

        # Plain net: implicit name, falling back to a connected device pin name when generic.
        base = _determine_base_name(suggested, implicit)
        if base is None or _is_generic_name(base):
            if pin := _pin_name_fallback(net):
                base = pin
        names[net].base_name = base


def _describe_ground(net: F.Net) -> str:
    """Short human identifier for a ground net, for the fragmentation warning."""
    return _get_fallback_prefix(net) or "GND"


def _resolve_ground_nets(
    ground_nets: list[F.Net], names: FuncDict[F.Net, _NetName]
) -> None:
    """Collapse all ground-role nets to ``GND``; warn when grounds are not tied together.

    Every ground net is named ``GND``; if more than one electrically-distinct ground net
    exists (a likely missing tie), the rest are de-conflicted by the normal prefixing pass
    and a warning lists them — surfacing the fragmentation rather than silently emitting
    ``GND-2``. An author can opt a net out by giving it an explicit name (``override_net_name``).
    """
    if not ground_nets:
        return
    for net in ground_nets:
        names[net].base_name = "GND"
    if len(ground_nets) > 1:
        ordered = sorted(ground_nets, key=_get_net_stable_key)
        logger.warning(
            "Multiple electrically-distinct ground nets detected (%d); they likely should "
            "be tied to a single reference. Naming the primary 'GND' and de-conflicting the "
            "rest: %s",
            len(ground_nets),
            ", ".join(_describe_ground(n) for n in ordered),
        )


def _is_generic_name(name: str | None) -> bool:
    """Check if a name is generic and should be replaced."""
    if name is None:
        return True

    if name.isdigit():  # bare pad/pin numbers ("7")
        return True

    generic_names = {"line", "hv", "lv", "p", "n", "vcc", "gnd"}
    generic_patterns: list[Callable[[str], bool]] = [
        lambda n: n.startswith("unnamed"),
        lambda n: re.match(r"^(_\d+|p\d+)$", n) is not None,
    ]

    return name in generic_names or any(pattern(name) for pattern in generic_patterns)


def _is_power_rail_name(name: str | None) -> bool:
    """Check if a name is a well-known power rail."""
    if name is None:
        return False
    return name.lower() in {"gnd", "vcc", "vdd", "vss"}


def _extract_interface_candidate(mif: F.Electrical) -> tuple[str, int] | None:
    """Extract a naming candidate from a single interface."""
    try:
        for node, name_in_parent in mif.get_hierarchy():
            if not node.get_parent():
                continue

            is_interface = node.has_trait(fabll.is_interface)
            is_not_electrical = not node.isinstance(F.Electrical)

            if is_interface and is_not_electrical:
                return (name_in_parent, len(node.get_hierarchy()))
    except fabll.NodeNoParent:
        pass

    return None


def _find_best_interface_name(mifs: Iterable[F.Electrical]) -> str | None:
    """Find the best interface name from connected interfaces."""
    candidates = [
        candidate
        for mif in mifs
        if (candidate := _extract_interface_candidate(mif)) is not None
    ]

    if not candidates:
        return None

    # Return the name with the smallest depth (highest in hierarchy),
    # and lexicographically smallest name as deterministic tie-breaker
    return min(candidates, key=lambda x: (x[1], x[0]))[0]


def _collect_affixes(mifs: list[F.Electrical]) -> tuple[str | None, str | None]:
    """Collect prefix and suffix from interfaces."""
    prefixes: list[str] = []
    suffixes: list[str] = []

    for mif in mifs:
        affix = mif.try_get_trait(F.has_net_name_affix)
        if not affix:
            continue

        if prefix := affix.get_prefix():
            prefixes.append(str(prefix))
        if suffix := affix.get_suffix():
            suffixes.append(str(suffix))

    # Return first prefix and suffix found
    return prefixes[0] if prefixes else None, suffixes[0] if suffixes else None


def _should_replace_generic_name(net_name: _NetName) -> bool:
    """Check if a generic base name should be replaced."""
    has_affixes = bool(net_name.required_suffix or net_name.required_prefix)
    return _is_generic_name(net_name.base_name) and has_affixes


def _apply_affixes(
    unnamed_nets: dict[F.Net, list[F.Electrical]], names: FuncDict[F.Net, _NetName]
) -> None:
    """Apply prefixes and suffixes from net name affixes."""
    for net, mifs in unnamed_nets.items():
        if net not in names:
            continue

        net_name = names[net]

        # Apply affixes
        prefix, suffix = _collect_affixes(mifs)
        net_name.required_prefix = prefix
        net_name.required_suffix = suffix

        # Replace generic names when affixes are present
        if _should_replace_generic_name(net_name):
            if better_name := _find_best_interface_name(mifs):
                net_name.base_name = better_name

        # Protect power rails from affixes
        if _is_power_rail_name(net_name.base_name):
            net_name.required_prefix = None
            net_name.required_suffix = None


def _find_anchor_interface(
    hierarchy: list[tuple[fabll.Node, str]],
) -> tuple[int, tuple[fabll.Node, str]] | None:
    """Find the first non-Electrical fabll.Node in hierarchy."""
    for idx, (node, name) in enumerate(hierarchy):
        is_interface = node.has_trait(fabll.is_interface)
        is_not_electrical = not node.isinstance(F.Electrical)

        if is_interface and is_not_electrical:
            return idx, (node, name)

    return None


def _find_owner_module(hierarchy: list[tuple], before_idx: int) -> str | None:
    """Find the nearest owning Module before the given index."""
    for j in range(before_idx - 1, -1, -1):
        node, name = hierarchy[j]
        if node.has_trait(fabll.is_module):
            return name
    return None


def _score_interface_path(anchor_node, anchor_name: str, has_owner: bool) -> int:
    """Calculate score for an interface path."""
    score = 0

    if anchor_node.isinstance(F.ElectricPower):
        score += 2

    if has_owner:
        score += 1

    if anchor_name.startswith("pins") or anchor_name.startswith("unnamed"):
        score -= 2

    return score


def _process_single_interface(mif: F.Electrical) -> tuple[int, list[str]] | None:
    """Process a single interface to get its path and score."""
    try:
        hierarchy = [
            (node, name) for node, name in mif.get_hierarchy() if node.get_parent()
        ]

        # Find anchor interface
        anchor_result = _find_anchor_interface(hierarchy)
        if not anchor_result:
            return None

        anchor_idx, (anchor_node, anchor_name) = anchor_result

        # Find owner module
        owner_name = _find_owner_module(hierarchy, anchor_idx)

        # Build path
        path = [owner_name, anchor_name] if owner_name else [anchor_name]

        # Calculate score
        score = _score_interface_path(anchor_node, anchor_name, bool(owner_name))

        return score, path

    except fabll.NodeNoParent:
        return None


def _get_interface_path_for_net(net: F.Net) -> list[str] | None:
    """Get the best interface path for prefixing a net."""
    candidates = [
        result
        for mif in net.get_connected_interfaces()
        if (result := _process_single_interface(mif)) is not None
    ]

    if not candidates:
        return None

    # Return the path with the highest score; break ties deterministically by
    # preferring shorter paths and then lexicographical order
    _, best_path = max(candidates, key=lambda x: (x[0], -len(x[1]), tuple(x[1])))
    return best_path


def _get_owner_module_name(net: F.Net) -> str | None:
    """Get the name of the owning module for a net."""
    owner_names: set[str] = set()
    for mif in net.get_connected_interfaces():
        try:
            hierarchy = mif.get_hierarchy()
            for node, name_in_parent in hierarchy:
                if node.get_parent() and node.has_trait(fabll.is_module):
                    owner_names.add(name_in_parent)
        except fabll.NodeNoParent:
            continue
    if not owner_names:
        return None
    # Choose a deterministic owner module name
    return min(owner_names)


def _compute_minimal_unique_prefixes(
    paths: dict[F.Net, list[str] | None], conflict_nets: list[F.Net]
) -> dict[F.Net, int]:
    """Compute minimal suffix lengths to make paths unique."""
    suffix_len: dict[F.Net, int] = {net: 1 for net in conflict_nets if paths[net]}

    def get_keys() -> dict[F.Net, tuple[str, ...]]:
        return {net: tuple(p[-suffix_len[net] :]) for net, p in paths.items() if p}

    keys = get_keys()

    # Increase suffix length until all keys are unique
    while True:
        # Group nets by their current key
        groups: dict[tuple[str, ...], list[F.Net]] = {}
        for net, key in keys.items():
            groups.setdefault(key, []).append(net)

        # Check if any groups have conflicts
        has_conflict = False
        for key, nets_in_key in groups.items():
            if len(nets_in_key) <= 1:
                continue

            # Increase suffix length for conflicting nets
            for net in nets_in_key:
                p = paths[net]
                if p and suffix_len[net] < len(p):
                    suffix_len[net] += 1
                    has_conflict = True

        if not has_conflict:
            break

        keys = get_keys()

    return suffix_len


def _is_generic_path_leaf(path: list[str] | None) -> bool:
    """Check if the leaf of a path is generic."""
    if not path:
        return False

    leaf = path[-1]
    return leaf.startswith("pins") or leaf.startswith("unnamed")


def _get_fallback_prefix(net: F.Net) -> str | None:
    """Get a fallback prefix for a net without a path."""
    # Try owner module name
    if owner_name := _get_owner_module_name(net):
        return owner_name

    # Try best interface name across connected interfaces
    interfaces = net.get_connected_interfaces()
    if best := _find_best_interface_name(interfaces):
        return best

    # Try lowest common ancestor
    if lcn := fabll.Node.nearest_common_ancestor(*interfaces):
        return lcn[0].get_full_name(include_uuid=False)

    return None


def _assign_prefix_for_net(
    net: F.Net,
    path: list[str] | None,
    suffix_len: dict[F.Net, int],
    names: FuncDict[F.Net, _NetName],
) -> None:
    """Assign a prefix to a single net."""
    net_name = names[net]

    if not path:
        # No path available, use fallback
        net_name.prefix = _get_fallback_prefix(net)
        return

    # Check if we should use owner name instead of generic path
    should_use_owner = (
        _is_generic_path_leaf(path)
        and not net_name.required_prefix
        and not net_name.required_suffix
    )

    if should_use_owner:
        if owner_name := _get_owner_module_name(net):
            net_name.prefix = owner_name
            return

    # Use the computed suffix from the path
    prefix_parts = path[-suffix_len[net] :]
    net_name.prefix = "-".join(prefix_parts)


def _resolve_conflicts_with_prefixes(names: FuncDict[F.Net, _NetName]) -> None:
    """Resolve naming conflicts by adding interface-based prefixes."""
    conflicts = _conflicts(names)
    for (
        conflict_nets
    ) in conflicts:  # Sort nets deterministically within the conflict group
        ordered_conflict_nets = sorted(conflict_nets, key=_get_net_stable_key)

        # Get interface paths for all conflicting nets
        paths = {net: _get_interface_path_for_net(net) for net in ordered_conflict_nets}

        # Skip if no paths available
        if not any(paths.values()):
            continue

        # Compute minimal unique prefixes
        suffix_len = _compute_minimal_unique_prefixes(
            paths, list(ordered_conflict_nets)
        )

        # Assign prefixes to each net except the first one (highest in hierarchy)
        for net in ordered_conflict_nets[1:]:
            _assign_prefix_for_net(net, paths[net], suffix_len, names)


def _resolve_conflicts_with_lca(names: FuncDict[F.Net, _NetName]) -> None:
    """Resolve remaining conflicts using lowest common ancestor."""
    for conflict_nets in _conflicts(names):
        # Sort nets deterministically and skip the first one (highest in hierarchy)
        ordered_conflict_nets = sorted(conflict_nets, key=_get_net_stable_key)
        for net in ordered_conflict_nets[1:]:
            # Skip if already has a prefix
            if names[net].prefix is not None:
                continue

            # Try to use lowest common ancestor
            interfaces = net.get_connected_interfaces()
            lcn = fabll.Node.nearest_common_ancestor(*interfaces)

            if lcn:
                lca_name = lcn[0].get_full_name(include_uuid=False)

                # Strip the last component if it matches the base name to
                # avoid duplication.
                # e.g., if LCA is "some_module.electrical_base_0" and base is
                # "electrical_base_0", use "some_module" as the prefix
                if "." in lca_name:
                    parts = lca_name.rsplit(".", 1)
                    if parts[-1] == names[net].base_name:
                        lca_name = parts[0]

                names[net].prefix = lca_name


def _resolve_conflicts_with_suffixes(names: FuncDict[F.Net, _NetName]) -> None:
    """Resolve remaining conflicts by adding numeric suffixes."""
    for conflict_nets in _conflicts(names):
        # Assign suffixes in a deterministic order within a conflict group
        # Skip the first net (highest in hierarchy) to preserve its name
        ordered_conflict_nets = sorted(conflict_nets, key=_get_net_stable_key)
        for i, net in enumerate(ordered_conflict_nets[1:], start=1):
            names[net].suffix = i


def _truncate_long_name(name: str, max_length: int = 255) -> str:
    """Truncate a long name to fit within the maximum length."""
    if len(name) <= max_length:
        return name

    # Keep first 200 chars and last 50 chars
    return name[:200] + "..." + name[-50:]


def _apply_names_to_nets(names: FuncDict[F.Net, _NetName]) -> None:
    """Apply the computed names to nets, with length limiting."""
    for net, net_name in names.items():
        final_name = _truncate_long_name(net_name.name)
        fabll.Traits.create_and_add_instance_to(net, F.has_net_name).setup(
            name=final_name
        )


def attach_net_names(nets: Iterable[F.Net], solver: object | None = None) -> None:
    """
    Generate good net names for all nets in a design.

    This function assigns meaningful names to nets based on:
    1. Required names from has_net_name traits (highest priority)
    2. Suggested names from has_net_name traits
    3. Voltage-derived names for power rails ("+5V"); shared "GND" for ground rails
    4. Implicit names from connected interfaces, falling back to a connected device pin
       name (ICs prioritized over passives)
    5. Conflict resolution through prefixing and suffixing

    ``solver`` (the build's parameter solver) is optional: it is used only to read solved
    rail voltages. Without it (e.g. in unit tests) rails degrade to their generic names.

    The naming process follows these steps:
    - Collect and sort unnamed nets
    - Register already-named nets
    - Process unnamed nets to determine base names (power rails, ground rails, pin fallback)
    - Collapse ground rails to a single "GND"
    - Apply affixes (prefixes/suffixes) from traits
    - Resolve conflicts through hierarchical prefixing
    - Apply numeric suffixes for remaining conflicts
    - Apply final names to nets
    """
    names = FuncDict[F.Net, _NetName]()

    # Work on a deterministically ordered view of nets throughout
    nets_list = list(nets)
    nets_ordered = sorted(nets_list, key=_get_net_stable_key)

    # Collect unnamed nets
    unnamed_nets = _collect_unnamed_nets(nets_ordered)

    # Register already-named nets
    _register_named_nets(nets_ordered, names)

    # Process unnamed nets
    ground_nets: list[F.Net] = []
    _process_unnamed_nets(unnamed_nets, names, solver, ground_nets)

    # Collapse ground rails to a shared "GND"
    _resolve_ground_nets(ground_nets, names)

    # Apply affixes
    _apply_affixes(unnamed_nets, names)

    # Resolve conflicts through prefixing
    _resolve_conflicts_with_prefixes(names)

    # Resolve remaining conflicts with lowest common ancestor
    _resolve_conflicts_with_lca(names)

    # Final conflict resolution with numeric suffixes
    _resolve_conflicts_with_suffixes(names)

    # Apply the computed names to nets
    _apply_names_to_nets(names)

    assert all(n.has_trait(F.has_net_name) for n in nets)


class TestNetNaming:
    def test_expected_net_name(self):
        import faebryk.core.node as fabll
        from faebryk.core.faebrykpy import EdgeInterfaceConnection as interface

        g = fabll.graph.GraphView.create()
        tg = fbrk.TypeGraph.create(g=g)

        class App(fabll.Node):
            i2c = F.I2C.MakeChild()
            power = F.ElectricPower.MakeChild()

            fabll.MakeEdge(
                [power],
                [i2c, "reference"],
                edge=interface.build(shallow=False),
            )

        app = App.bind_typegraph(tg=tg).create_instance(g=g)

        scl_names = _extract_net_name_info(app.i2c.get().scl.get().line.get())
        sda_names = _extract_net_name_info(app.i2c.get().sda.get().line.get())
        power_hv_names = _extract_net_name_info(app.power.get().hv.get())
        power_lv_names = _extract_net_name_info(app.power.get().lv.get())

        print(f"scl_names: {scl_names}")
        print(f"sda_names: {sda_names}")
        print(f"power_lv_names: {power_lv_names}")
        print(f"power_hv_names: {power_hv_names}")

        assert scl_names[0] == set()
        assert scl_names[1] == [("SCL", 0)]
        assert scl_names[2] == {"line": 0.075}

        assert sda_names[0] == set()
        assert sda_names[1] == [("SDA", 0)]
        assert sda_names[2] == {"line": 0.075}

        assert power_lv_names[0] == set()
        assert power_lv_names[1] == [("lv", -1), ("lv", 1)]
        assert power_lv_names[2] == {"lv": 0.3333333333333333}

        assert power_hv_names[0] == set()
        assert power_hv_names[1] == [("hv", -1), ("hv", 1)]
        assert power_hv_names[2] == {"hv": 0.09999999999999999}

    def _bind_nets_for_test(
        self,
        electricals: Iterable[F.Electrical],
        tg: fbrk.TypeGraph,
        g: fabll.graph.GraphView,
    ) -> set[F.Net]:
        fbrk_nets: set[F.Net] = set()
        # collect buses in a sorted manner
        buses = sorted(
            fabll.is_interface.group_into_buses(electricals),
            key=lambda node: node.get_full_name(include_uuid=False),
        )

        # find or generate nets
        for bus in buses:
            if fbrk_net := get_named_net(bus):
                fbrk_nets.add(fbrk_net)
            else:
                fbrk_net = F.Net.bind_typegraph(tg).create_instance(g=g)
                fbrk_net.part_of.get()._is_interface.get().connect_to(bus)
                fbrk_nets.add(fbrk_net)

        return fbrk_nets

    def test_hierarchical_net_name_suggestions(self):
        import faebryk.core.node as fabll
        from faebryk.core.faebrykpy import EdgeInterfaceConnection as interface

        g = fabll.graph.GraphView.create()
        tg = fbrk.TypeGraph.create(g=g)

        class SomeModule(fabll.Node):
            electrical_base = F.Electrical.MakeChild()
            electrical_base.add_dependant(
                fabll.Traits.MakeEdge(
                    F.has_net_name_suggestion.MakeChild(
                        name="E_BASE",
                        level=F.has_net_name_suggestion.Level.SUGGESTED,
                    ),
                    owner=[electrical_base],
                )
            )

        class App(fabll.Node):
            some_module = SomeModule.MakeChild()

            electrical_app = F.Electrical.MakeChild()
            electrical_app.add_dependant(
                fabll.Traits.MakeEdge(
                    F.has_net_name_suggestion.MakeChild(
                        name="E_APP", level=F.has_net_name_suggestion.Level.SUGGESTED
                    ),
                    owner=[electrical_app],
                )
            )

            _elec_connection = fabll.MakeEdge(
                [electrical_app],
                [some_module, "electrical_base"],
                edge=interface.build(shallow=False),
            )

        app = App.bind_typegraph(tg=tg).create_instance(g=g)

        some_module_names = _extract_net_name_info(
            app.some_module.get().electrical_base.get()
        )
        electrical_app_names = _extract_net_name_info(app.electrical_app.get())

        assert (
            app.some_module.get()
            .electrical_base.get()
            ._is_interface.get()
            .is_connected_to(app.electrical_app.get())
        )

        print(f"some_module_names: {some_module_names}")
        print(f"electrical_app_names: {electrical_app_names}")

        assert some_module_names[0] == set()
        assert some_module_names[1] == [("E_BASE", -1), ("E_BASE", 1)]
        assert some_module_names[2] == {"electrical_base": 0.3333333333333333}

        assert electrical_app_names[0] == set()
        assert electrical_app_names[1] == [("E_APP", -1), ("E_APP", 0)]
        assert electrical_app_names[2] == {"electrical_app": 0.5}

        nets = self._bind_nets_for_test(
            electricals=[
                app.some_module.get().electrical_base.get(),
                app.electrical_app.get(),
            ],
            tg=tg,
            g=g,
        )

        assert len(nets) == 1

        attach_net_names(nets)

        base_net = get_named_net(app.some_module.get().electrical_base.get())
        app_net = get_named_net(app.electrical_app.get())

        assert base_net is not None
        assert app_net is not None

        assert base_net.get_name() == "E_APP"
        assert app_net.get_name() == "E_APP"
        # name of the highest in the hierarchy should be chosen

        assert base_net == app_net

    def test_net_name_conflicts(self):
        import faebryk.core.node as fabll
        from faebryk.core.faebrykpy import EdgeInterfaceConnection as interface

        g = fabll.graph.GraphView.create()
        tg = fbrk.TypeGraph.create(g=g)

        class SomeModule(fabll.Node):
            electrical_base = F.Electrical.MakeChild()
            electrical_base.add_dependant(
                fabll.Traits.MakeEdge(
                    F.has_net_name_suggestion.MakeChild(
                        name="E_BASE",
                        level=F.has_net_name_suggestion.Level.EXPECTED,
                    ),
                    owner=[electrical_base],
                )
            )
            electrical_base_0 = F.Electrical.MakeChild()
            electrical_base_1 = F.Electrical.MakeChild()
            electrical_base_2 = F.Electrical.MakeChild()
            electrical_base_3 = F.Electrical.MakeChild()

        class App(fabll.Node):
            some_module = SomeModule.MakeChild()

            electrical_app = F.Electrical.MakeChild()
            electrical_app.add_dependant(
                fabll.Traits.MakeEdge(
                    F.has_net_name_suggestion.MakeChild(
                        name="E_APP", level=F.has_net_name_suggestion.Level.SUGGESTED
                    ),
                    owner=[electrical_app],
                )
            )

            _elec_connection = fabll.MakeEdge(
                [electrical_app],
                [some_module, "electrical_base"],
                edge=interface.build(shallow=False),
            )

            electrical_base_0 = F.Electrical.MakeChild()
            electrical_app_0 = F.Electrical.MakeChild()

        app = App.bind_typegraph(tg=tg).create_instance(g=g)

        nets = self._bind_nets_for_test(
            electricals=app.get_children(direct_only=False, types=F.Electrical),
            tg=tg,
            g=g,
        )

        assert len(nets) == 7

        attach_net_names(nets)

        net_names = sorted(
            [net_name for net in nets if (net_name := net.get_name()) is not None]
        )
        print(f"net_names: {net_names}")
        assert net_names == [
            "E_BASE",
            "electrical_app_0",
            "electrical_base_0",
            "electrical_base_1",
            "electrical_base_2",
            "electrical_base_3",
            "some_module-electrical_base_0",
        ]

    def test_all_conflict_resolution_strategies(self):
        """Test that demonstrates all three conflict resolution strategies:
        1. Prefix resolution (using interface paths)
        2. LCA resolution (using lowest common ancestor)
        3. Suffix resolution (using numeric suffixes)
        """
        import faebryk.core.node as fabll
        from faebryk.core.faebrykpy import EdgeInterfaceConnection as interface

        g = fabll.graph.GraphView.create()
        tg = fbrk.TypeGraph.create(g=g)

        # Module with named interfaces that will create paths for prefix resolution
        class SensorModule(fabll.Node):
            i2c = F.I2C.MakeChild()
            power = F.ElectricPower.MakeChild()

            fabll.MakeEdge(
                [power],
                [i2c, "reference"],
                edge=interface.build(shallow=False),
            )

        class ControllerModule(fabll.Node):
            i2c = F.I2C.MakeChild()
            power = F.ElectricPower.MakeChild()

            fabll.MakeEdge(
                [power],
                [i2c, "reference"],
                edge=interface.build(shallow=False),
            )

        class SimpleModule(fabll.Node):
            # Plain electricals without interface hierarchy (will use LCA resolution)
            signal_a = F.Electrical.MakeChild()
            signal_b = F.Electrical.MakeChild()

        # Module that contains identically named electricals
        class IdenticalNetsModule(fabll.Node):
            line = F.Electrical.MakeChild()

        # Container module - create multiple electricals with same suggested name
        # This forces suffix resolution since they'll all want the same name
        class ContainerModule(fabll.Node):
            clk1 = F.Electrical.MakeChild()
            clk1.add_dependant(
                fabll.Traits.MakeEdge(
                    F.has_net_name_suggestion.MakeChild(
                        name="CLK",
                        level=F.has_net_name_suggestion.Level.SUGGESTED,
                    ),
                    owner=[clk1],
                )
            )

            clk2 = F.Electrical.MakeChild()
            clk2.add_dependant(
                fabll.Traits.MakeEdge(
                    F.has_net_name_suggestion.MakeChild(
                        name="CLK",
                        level=F.has_net_name_suggestion.Level.SUGGESTED,
                    ),
                    owner=[clk2],
                )
            )

            clk3 = F.Electrical.MakeChild()
            clk3.add_dependant(
                fabll.Traits.MakeEdge(
                    F.has_net_name_suggestion.MakeChild(
                        name="CLK",
                        level=F.has_net_name_suggestion.Level.SUGGESTED,
                    ),
                    owner=[clk3],
                )
            )

        class AppWithConflicts(fabll.Node):
            sensor = SensorModule.MakeChild()
            controller = ControllerModule.MakeChild()
            simple1 = SimpleModule.MakeChild()
            simple2 = SimpleModule.MakeChild()

            identical1 = IdenticalNetsModule.MakeChild()
            identical2 = IdenticalNetsModule.MakeChild()
            identical3 = IdenticalNetsModule.MakeChild()

            container = ContainerModule.MakeChild()

        app = AppWithConflicts.bind_typegraph(tg=tg).create_instance(g=g)

        all_electricals = app.get_children(direct_only=False, types=F.Electrical)

        nets = self._bind_nets_for_test(
            electricals=all_electricals,
            tg=tg,
            g=g,
        )

        attach_net_names(nets)

        # Get all net names sorted for consistent checking
        net_names_dict = {
            net_name: net for net in nets if (net_name := net.get_name()) is not None
        }
        net_names = sorted(net_names_dict)

        assert len(net_names) == 30

        print(f"\nAll net names ({len(net_names)}):")
        for name in net_names:
            print(f"  - {name}")

        # prefix resolution: I2C interfaces should have prefixes based on their
        # parent module name.
        # e.g. sensor.i2c.scl and controller.i2c.scl
        scl_nets = [name for name in net_names if "SCL" in name.upper()]
        assert len(scl_nets) == 2, f"Expected 2 SCL nets, got {scl_nets}"
        # only one should get a prefix
        assert any("SCL" == name for name in scl_nets)
        assert any("-" in name and "SCL" in name for name in scl_nets), (
            "Expected one prefixed SCL"
        )

        # lca resolution: signal_a from simple1 and simple2 should use module
        # names as prefixes
        # e.g. simple1.signal_a and simple2.signal_a
        signal_a_nets = [name for name in net_names if "signal_a" in name]
        assert len(signal_a_nets) == 2, f"Expected 2 signal_a nets, got {signal_a_nets}"
        # only one should get a prefix
        assert "signal_a" in signal_a_nets
        prefixed_signal = [n for n in signal_a_nets if n != "signal_a"]
        assert len(prefixed_signal) == 1
        assert "simple" in prefixed_signal[0]

        # lca resolution: "line" nets from different identical modules get LCA prefixes
        # e.g. identical1-line, identical2-line and identical3-line
        line_nets = [name for name in net_names if "line" in name]
        assert len(line_nets) == 3, f"Expected 3 line nets, got {line_nets}"
        assert len(set(line_nets)) == 3, (
            f"Expected 3 distinct line net names, got {line_nets}"
        )
        # should have different LCA-based prefixes
        # e.g. identical1, identical2 and identical3
        regex = r"identical\d"
        assert any(re.match(regex, name) for name in line_nets), (
            f"Expected line nets to have LCA-based prefixes, got {line_nets}"
        )

        # suffix resolution: multiple CLK nets with same suggested name
        # e.g. container-clk1, container-clk2 and container-clk3
        clk_nets = [name for name in net_names if "CLK" in name]
        assert len(clk_nets) == 3, f"Expected 3 CLK nets, got {clk_nets}"
        assert len(set(clk_nets)) == 3
        # one should get a plain name, others get suffixes or prefixes
        has_plain_clk = "CLK" in clk_nets
        has_modifications = any(
            ("-" in name or "container" in name.lower())
            for name in clk_nets
            if name != "CLK"
        )

        assert has_plain_clk or has_modifications, (
            "Expected CLK nets to show conflict resolution (plain + suffixes/prefixes)"
            f", got {clk_nets}"
        )

    def test_ground_nets_collapse_to_gnd(self):
        """Two electrically-distinct ground rails collapse to a single plain ``GND``;
        the second is de-conflicted (not ``GND-2``)."""
        import faebryk.core.node as fabll

        g = fabll.graph.GraphView.create()
        tg = fbrk.TypeGraph.create(g=g)

        class App(fabll.Node):
            power_a = F.ElectricPower.MakeChild()
            power_b = F.ElectricPower.MakeChild()

        app = App.bind_typegraph(tg=tg).create_instance(g=g)

        nets = self._bind_nets_for_test(
            electricals=app.get_children(direct_only=False, types=F.Electrical),
            tg=tg,
            g=g,
        )
        attach_net_names(nets)

        net_names = sorted(n.get_name() for n in nets if n.get_name() is not None)
        gnd_nets = [n for n in net_names if "GND" in n]
        # both ground rails named for GND, exactly one keeps the plain "GND"
        assert len(gnd_nets) == 2, f"expected 2 ground nets, got {gnd_nets}"
        assert sum(1 for n in net_names if n == "GND") == 1, net_names
        # the de-conflicted one is not a numeric suffix
        assert all(n == "GND" or not re.fullmatch(r"GND-\d+", n) for n in gnd_nets)


def test_format_rail_voltage():
    assert _format_rail_voltage(5.0) == "+5V"
    assert _format_rail_voltage(3.3) == "+3V3"
    assert _format_rail_voltage(12.0) == "+12V"
    assert _format_rail_voltage(1.8) == "+1V8"
    assert _format_rail_voltage(-5.0) == "-5V"
    assert _format_rail_voltage(-12.0) == "-12V"


def test_designator_tier_orders_ic_before_passive():
    # ICs win the pin-name fallback over passives; other actives sit between.
    assert _designator_tier("U") < _designator_tier("Q")
    assert _designator_tier("Q") < _designator_tier("R")
    assert _designator_tier("IC") == _designator_tier("U")


def test_generic_name_includes_pad_numbers_and_vcc():
    assert _is_generic_name("7")  # bare pad number
    assert _is_generic_name("vcc")
    assert _is_generic_name("hv")
    assert not _is_generic_name("VFB")
    assert not _is_generic_name("+5V")
