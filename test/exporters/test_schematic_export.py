# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""Tests for the KiCad connectivity-schematic emitter."""

import re
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

import faebryk.core.faebrykpy as fbrk
import faebryk.core.node as fabll
import faebryk.library._F as F
from faebryk.exporters.schematic.kicad import schematic as S
from faebryk.exporters.schematic.kicad.generic_symbol import (
    SymbolDef,
    build_generic_symbol,
    build_power_symbol,
    build_pwr_flag_symbol,
    build_wire_box,
)
from faebryk.exporters.schematic.kicad.real_symbol import build_real_symbol
from faebryk.exporters.schematic.kicad.schematic import (
    ComponentIR,
    PinIR,
    extract_components,
    render,
    render_wired,
)
from faebryk.libs.kicad.fileformats import kicad
from faebryk.libs.test.fileformats import SYMFILE

_KICAD_CLI = shutil.which("kicad-cli") or (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
)
_HAS_KICAD_CLI = Path(_KICAD_CLI).exists()
requires_kicad_cli = pytest.mark.skipif(
    not _HAS_KICAD_CLI, reason="kicad-cli not available"
)


# --------------------------------------------------------------------------------------
# Generic symbol
# --------------------------------------------------------------------------------------
def test_build_generic_symbol():
    sym = build_generic_symbol("atopile:GEN_0_4", ["1", "2", "3", "4"])
    assert sym.lib_id == "atopile:GEN_0_4"
    assert sym.is_fallback is True
    assert set(sym.pin_xy) == {"1", "2", "3", "4"}
    # lib-symbol name must equal the instance lib_id, child units use the bare name
    assert '(symbol "atopile:GEN_0_4"' in sym.lib_symbol_text
    assert '(symbol "GEN_0_4_0_1"' in sym.lib_symbol_text
    assert '(symbol "GEN_0_4_1_1"' in sym.lib_symbol_text
    # left/right split
    xs = {x for x, _ in sym.pin_xy.values()}
    assert any(x < 0 for x in xs) and any(x > 0 for x in xs)


# --------------------------------------------------------------------------------------
# render() -> loadable schematic
# --------------------------------------------------------------------------------------
def _sample_components() -> list[ComponentIR]:
    # R1.1-R2.1 = VCC ; R1.2-R2.2 = GND
    return [
        ComponentIR(
            "R1", "10kohm", [PinIR("1", "VCC"), PinIR("2", "GND")]
        ),
        ComponentIR(
            "R2", "4.7kohm", [PinIR("1", "VCC"), PinIR("2", "GND")]
        ),
    ]


def test_render_summary_and_reparse(tmp_path: Path):
    comps = _sample_components()
    doc, summary = render(comps, {"VCC", "GND"}, search_dirs=[])

    assert summary.components == 2
    assert summary.nets == 2
    assert summary.labels == 4  # every pin gets a label
    assert summary.fallback_symbols == 1  # both share one generic 2-pin symbol
    assert summary.unmapped_pads == 0

    path = tmp_path / "sample.kicad_sch"
    path.write_text(doc, encoding="utf-8")

    # Re-parses with atopile's own KiCad parser.
    reparsed = kicad.loads(kicad.schematic.SchematicFile, path)
    assert reparsed.kicad_sch is not None


@requires_kicad_cli
def test_render_passes_kicad_erc(tmp_path: Path):
    doc, _ = render(_sample_components(), {"VCC", "GND"}, search_dirs=[])
    path = tmp_path / "sample.kicad_sch"
    path.write_text(doc, encoding="utf-8")

    out = tmp_path / "out"
    out.mkdir()

    # KiCad must be able to load + render the schematic.
    svg = subprocess.run(
        [_KICAD_CLI, "sch", "export", "svg", "-o", str(out), str(path)],
        capture_output=True,
        text=True,
    )
    assert svg.returncode == 0, f"svg export failed:\n{svg.stdout}\n{svg.stderr}"

    # ERC must see connectivity through the global labels (no unconnected pins).
    subprocess.run(
        [_KICAD_CLI, "sch", "erc", "-o", str(out / "erc.rpt"), str(path)],
        capture_output=True,
        text=True,
    )
    report = (out / "erc.rpt").read_text() if (out / "erc.rpt").exists() else ""
    assert "unconnected" not in report.lower(), f"unconnected pins:\n{report}"


# --------------------------------------------------------------------------------------
# extract_components() from a synthetic graph
# --------------------------------------------------------------------------------------
def _build_synthetic_app() -> fabll.Node:
    """A 2-resistor-like design: two modules sharing two nets, with footprints."""
    from faebryk.libs.net_naming import attach_net_names
    from faebryk.libs.nets import bind_electricals_to_fbrk_nets

    g = fabll.graph.GraphView.create()
    tg = fbrk.TypeGraph.create(g=g)

    class _Comp(fabll.Node):
        a = F.Electrical.MakeChild()
        b = F.Electrical.MakeChild()
        lead_a = fabll.Traits.MakeEdge(F.Lead.is_lead.MakeChild(), [a])
        lead_b = fabll.Traits.MakeEdge(F.Lead.is_lead.MakeChild(), [b])
        a.add_dependant(lead_a)
        b.add_dependant(lead_b)
        _is_interface = fabll.Traits.MakeEdge(fabll.is_interface.MakeChild())
        _is_module = fabll.Traits.MakeEdge(fabll.is_module.MakeChild())
        _can_attach = fabll.Traits.MakeEdge(
            F.Footprints.can_attach_to_footprint.MakeChild()
        )

    class _App(fabll.Node):
        c1 = _Comp.MakeChild()
        c2 = _Comp.MakeChild()
        _is_module = fabll.Traits.MakeEdge(fabll.is_module.MakeChild())

    app = _App.bind_typegraph(tg).create_instance(g=g)

    comps = [app.c1.get(), app.c2.get()]
    for idx, comp in enumerate(comps, start=1):
        # designator
        fabll.Traits.create_and_add_instance_to(comp, F.has_designator).setup(
            designator=f"R{idx}"
        )
        # pads, associated to leads
        fp_node = fabll.Node.bind_typegraph(tg).create_instance(g=g)
        fp_trait = fabll.Traits.create_and_add_instance_to(
            fp_node, F.Footprints.is_footprint
        )
        for lead_iface, num in ((comp.a.get(), "1"), (comp.b.get(), "2")):
            pad_node = fabll.Node.bind_typegraph(tg).create_instance(g=g)
            pad = fabll.Traits.create_and_add_instance_to(
                pad_node, F.Footprints.is_pad
            ).setup(pad_name=num, pad_number=num)
            fabll.Traits.create_and_add_instance_to(
                node=lead_iface.get_trait(F.Lead.is_lead),
                trait=F.Lead.has_associated_pads,
            ).setup(pad)
            fp_node.add_child(pad_node)
        fabll.Traits.create_and_add_instance_to(
            comp, F.Footprints.has_associated_footprint
        ).setup(fp_trait)

    # connect c1.a-c2.a and c1.b-c2.b into two buses
    for left, right in pairwise(comps):
        left.a.get()._is_interface.get().connect_to(right.a.get())
        left.b.get()._is_interface.get().connect_to(right.b.get())

    nets = bind_electricals_to_fbrk_nets(tg, g)
    attach_net_names(nets)
    return app


def test_extract_components_from_graph():
    app = _build_synthetic_app()
    components, net_names = extract_components(app)

    assert {c.ref for c in components} == {"R1", "R2"}
    assert len(net_names) == 2
    for comp in components:
        assert {p.number for p in comp.pins} == {"1", "2"}
        # both pins are on named nets, and both components share the same nets
        assert all(p.net is not None for p in comp.pins)
    # the two components must agree on the shared nets
    nets_r1 = {p.number: p.net for p in components[0].pins}
    nets_r2 = {p.number: p.net for p in components[1].pins}
    assert nets_r1 == nets_r2


# --------------------------------------------------------------------------------------
# Schematic <-> PCB instance-path linkage (KiCad cross-probe)
# --------------------------------------------------------------------------------------
def test_stable_uuid_deterministic():
    import uuid as _uuid

    from faebryk.libs.kicad.identity import stable_uuid

    # Stable across calls, distinct per key, and a valid UUID.
    assert stable_uuid("filter.opamp.package") == stable_uuid("filter.opamp.package")
    assert stable_uuid("a.b") != stable_uuid("a.c")
    _uuid.UUID(stable_uuid("anything"))


def test_compute_instance_paths_match_symbol_instances():
    """Each component's computed instance path must equal its schematic symbol path.

    This is the guarantee the PCB linkage relies on: the transformer stamps footprints
    with ``compute_instance_paths`` output, and the schematic emits the same paths, so
    KiCad cross-probes symbol <-> footprint.
    """
    app = _build_synthetic_app()
    comps, nets = extract_components(app)

    # Every component carries an atopile address (the footprint key).
    assert all(c.address for c in comps)

    paths = S.compute_instance_paths(comps, app, root_stem="syn")
    # Deterministic across calls.
    again = S.compute_instance_paths(comps, app, root_stem="syn")
    assert {k: v.full_path for k, v in paths.items()} == {
        k: v.full_path for k, v in again.items()
    }

    files, _ = S.render_hierarchical(comps, nets, app, root_stem="syn", search_dirs=[])
    sym_inst = files["syn.kicad_sch"].split("(symbol_instances")[-1]
    sch_paths = set(re.findall(r'\(path "(/[^"]+)"', sym_inst))
    for comp in comps:
        assert paths[comp.address].full_path in sch_paths


# --------------------------------------------------------------------------------------
# Real symbol regeneration
# --------------------------------------------------------------------------------------
def _load_first_real_symbol() -> SymbolDef:
    sym_file = kicad.loads(kicad.symbol.SymbolFile, SYMFILE)
    name = sym_file.kicad_sym.symbols[0].name
    sym = build_real_symbol(f"atopile:{name}", sym_file)
    assert sym is not None
    return sym


def test_build_real_symbol_structure():
    sym = _load_first_real_symbol()
    assert sym.is_fallback is False
    assert sym.lib_id.startswith("atopile:")
    assert f'(symbol "{sym.lib_id}"' in sym.lib_symbol_text
    assert sym.pin_xy  # at least one pin with geometry
    # Pin names must render *inside* the body (positive offset) so they don't overlap the
    # pin numbers. The cached test symbol omits (pin_names …), exercising the default.
    assert "(pin_names (offset 0))" not in sym.lib_symbol_text
    assert "(pin_names (offset 0.508))" in sym.lib_symbol_text


def _wrap_two_instances(sym: SymbolDef) -> str:
    """Build a minimal schematic embedding ``sym`` with two pin-paired instances."""
    nums = list(sym.pin_xy)
    instances, labels, sym_paths = [], [], []
    for ix in (80.0, 160.0):
        comp = ComponentIR(
            f"U{int(ix)}", "", [PinIR(n, f"N{n}") for n in nums], inst_uuid=S._u()
        )
        instances.append(S._instance_block(comp, sym, ix, 100.0))
        sym_paths.append(
            f'    (path "/{comp.inst_uuid}" (reference "{comp.ref}") (unit 1)'
            f' (value "") (footprint ""))'
        )
        for n in nums:
            px, py = sym.pin_xy[n]
            labels.append(S._label_block(f"N{n}", ix + px, 100.0 - py))
    return (
        f"(kicad_sch (version {S.SCH_VERSION}) (generator eeschema)\n"
        f"  (uuid {S._u()})\n"
        f'  (paper "{S.PAPER}")\n'
        f"  (lib_symbols\n{sym.lib_symbol_text}\n  )\n"
        f"{chr(10).join(instances)}\n{chr(10).join(labels)}\n"
        f'  (sheet_instances\n    (path "/" (page "1"))\n  )\n'
        f"  (symbol_instances\n{chr(10).join(sym_paths)}\n  )\n)\n"
    )


@requires_kicad_cli
def test_real_symbol_loads_in_kicad(tmp_path: Path):
    sym = _load_first_real_symbol()
    path = tmp_path / "real.kicad_sch"
    path.write_text(_wrap_two_instances(sym), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    svg = subprocess.run(
        [_KICAD_CLI, "sch", "export", "svg", "-o", str(out), str(path)],
        capture_output=True,
        text=True,
    )
    assert svg.returncode == 0, (
        f"KiCad failed to load regenerated symbol:\n{svg.stderr}"
    )


@requires_kicad_cli
def test_extract_then_render_passes_erc(tmp_path: Path):
    app = _build_synthetic_app()
    components, net_names = extract_components(app)
    doc, summary = render(components, net_names, search_dirs=[])
    assert summary.labels == 4

    path = tmp_path / "synthetic.kicad_sch"
    path.write_text(doc, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    erc = subprocess.run(
        [_KICAD_CLI, "sch", "erc", "-o", str(out / "erc.rpt"), str(path)],
        capture_output=True,
        text=True,
    )
    report = (out / "erc.rpt").read_text() if (out / "erc.rpt").exists() else ""
    assert erc.returncode == 0 or "Found" in (erc.stdout + report)
    assert "unconnected" not in report.lower(), report


# --------------------------------------------------------------------------------------
# Wire (ladder) mode
# --------------------------------------------------------------------------------------
def test_build_wire_box():
    sym = build_wire_box("atopile:WBOX_0_3", ["1", "2", "3"])
    assert sym.is_fallback is True
    assert set(sym.pin_xy) == {"1", "2", "3"}
    ys = {y for _, y in sym.pin_xy.values()}
    assert len(ys) == 1 and next(iter(ys)) < 0  # all pins on the bottom edge
    xs = [sym.pin_xy[n][0] for n in ("1", "2", "3")]
    assert xs == sorted(xs) and len(set(xs)) == 3  # unique, increasing lanes


def _wired_components() -> list[ComponentIR]:
    return [
        ComponentIR("R1", "10k", [PinIR("1", "VCC"), PinIR("2", "GND")]),
        ComponentIR("R2", "4k7", [PinIR("1", "VCC"), PinIR("2", "GND")]),
        ComponentIR(
            "U1",
            "MCU",
            [
                PinIR("1", "VCC"),
                PinIR("2", "GND"),
                PinIR("3", "SDA"),
                PinIR("4", "SCL"),
            ],
        ),
        ComponentIR("R3", "1k", [PinIR("1", "SDA"), PinIR("2", "SCL")]),
    ]


def test_render_wired_summary_and_reparse(tmp_path: Path):
    comps = _wired_components()
    doc, summary = render_wired(comps, {"VCC", "GND", "SDA", "SCL"})

    # VCC{R1.1,R2.1,U1.1}, GND{R1.2,R2.2,U1.2}, SDA{U1.3,R3.1}, SCL{U1.4,R3.2}
    assert summary.trunks == 4  # all four nets have >=2 pins
    # interior junctions: 3-pin nets (VCC, GND) have 1 each; 2-pin nets have 0
    assert summary.junctions == 2
    path = tmp_path / "wired.kicad_sch"
    path.write_text(doc, encoding="utf-8")
    assert kicad.loads(kicad.schematic.SchematicFile, path).kicad_sch is not None


def _expected_nets(comps: list[ComponentIR]) -> dict[str, set[str]]:
    nets: dict[str, set[str]] = {}
    for c in comps:
        for p in c.pins:
            if p.net is not None:
                nets.setdefault(p.net, set()).add(f"{c.ref}.{p.number}")
    return nets


def _kicad_netlist_membership(path: Path, out: Path) -> dict[str, set[str]]:
    """Run kicad-cli netlist export and return {net_name: {"REF.PIN", ...}}."""
    netfile = out / "net.net"
    subprocess.run(
        [_KICAD_CLI, "sch", "export", "netlist", "--format", "kicadsexpr",
         "-o", str(netfile), str(path)],
        capture_output=True,
        text=True,
    )
    text = netfile.read_text()
    nets_blob = text[text.index("(nets"):]
    membership: dict[str, set[str]] = {}
    for block in re.split(r"\(net\b", nets_blob)[1:]:
        name_m = re.search(r'\(name "?([^")]+)"?\)', block)
        if not name_m:
            continue
        name = name_m.group(1)
        nodes = re.findall(r'\(ref "?([^")]+)"?\)\s*\(pin "?([^")]+)"?\)', block)
        if nodes:
            membership[name] = {f"{r}.{p}" for r, p in nodes}
    return membership


@requires_kicad_cli
def test_wired_loads_and_no_shorts(tmp_path: Path):
    comps = _wired_components()
    doc, _ = render_wired(comps, {"VCC", "GND", "SDA", "SCL"})
    path = tmp_path / "wired.kicad_sch"
    path.write_text(doc, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    # Loads + renders in KiCad.
    svg = subprocess.run(
        [_KICAD_CLI, "sch", "export", "svg", "-o", str(out), str(path)],
        capture_output=True,
        text=True,
    )
    assert svg.returncode == 0, f"load failed:\n{svg.stderr}"

    # Definitive: the drawn wires reproduce exactly the intended nets (no shorts).
    got = _kicad_netlist_membership(path, out)
    expected = _expected_nets(comps)
    for net, pins in expected.items():
        assert got.get(net) == pins, f"net {net}: expected {pins}, got {got.get(net)}"


@requires_kicad_cli
def test_extract_then_render_wired_no_shorts(tmp_path: Path):
    app = _build_synthetic_app()
    components, net_names = extract_components(app)
    doc, summary = render_wired(components, net_names)
    assert summary.wires > 0

    path = tmp_path / "syn_wired.kicad_sch"
    path.write_text(doc, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    got = _kicad_netlist_membership(path, out)
    expected = _expected_nets(components)
    for net, pins in expected.items():
        assert got.get(net) == pins, f"net {net}: expected {pins}, got {got.get(net)}"


# --------------------------------------------------------------------------------------
# Hierarchical mode (real symbols + sheets per .ato module, labels for connectivity)
# --------------------------------------------------------------------------------------
def test_build_sheet_tree_flat_without_ato_modules():
    # The synthetic app is built directly via fabll, so it carries no is_ato_module
    # trait -> everything falls onto a single root sheet.
    app = _build_synthetic_app()
    comps, _ = extract_components(app)
    root = S.build_sheet_tree(comps, app)
    assert root.children == []
    assert {c.ref for c in root.components} == {"R1", "R2"}
    assert len(root.walk()) == 1


# --------------------------------------------------------------------------------------
# Connectivity-aware sheet grouping (pull-in + flatten)
# --------------------------------------------------------------------------------------
def test_pull_in_moves_orphan_to_dominant_child():
    """A parent-level passive wired only to a child's IC is pulled onto the child sheet."""
    root = S.SheetIR(name="root", node_id="r")
    child = S.SheetIR(
        name="rs485",
        node_id="b",
        components=[
            ComponentIR("U1", "", [PinIR("1", "A"), PinIR("2", "B"), PinIR("3", "GND")])
        ],
    )
    root.children = [child]
    # 120R termination declared at the parent level, touching only the dedicated A/B nets.
    root.components = [ComponentIR("R9", "120", [PinIR("1", "A"), PinIR("2", "B")])]

    S._pull_in_by_connectivity(root)

    assert [c.ref for c in root.components] == []
    assert {c.ref for c in child.components} == {"U1", "R9"}


def test_pull_in_keeps_rail_only_component_on_parent():
    """A component sharing only a high-degree rail with a child is NOT pulled in."""
    root = S.SheetIR(name="root", node_id="r")
    child = S.SheetIR(
        name="c",
        node_id="c",
        components=[
            ComponentIR(
                "U1",
                "",
                [PinIR(str(i), "GND") for i in range(1, 4)] + [PinIR("4", "SIG")],
            )
        ],
    )
    root.children = [child]
    root.components = [ComponentIR("C1", "", [PinIR("1", "GND"), PinIR("2", "VCC")])]

    S._pull_in_by_connectivity(root)

    assert {c.ref for c in root.components} == {"C1"}


def test_pull_in_keeps_interconnect_on_ancestor():
    """A series element bridging two child sheets stays on the common ancestor."""
    root = S.SheetIR(name="root", node_id="r")
    a = S.SheetIR(name="a", node_id="a", components=[ComponentIR("U1", "", [PinIR("1", "X")])])
    b = S.SheetIR(name="b", node_id="b", components=[ComponentIR("U2", "", [PinIR("1", "Y")])])
    root.children = [a, b]
    root.components = [ComponentIR("R1", "", [PinIR("1", "X"), PinIR("2", "Y")])]

    S._pull_in_by_connectivity(root)

    assert {c.ref for c in root.components} == {"R1"}


def test_flatten_merges_small_leaf_into_parent():
    """A tiny leaf sub-module collapses into its (non-root) functional parent."""
    root = S.SheetIR(name="root", node_id="r")
    parent = S.SheetIR(
        name="filter", node_id="f", components=[ComponentIR("R1", "", [PinIR("1", "N")])]
    )
    leaf = S.SheetIR(
        name="opamp",
        node_id="o",
        components=[
            ComponentIR("U1", "", [PinIR("1", "N")]),
            ComponentIR("C1", "", [PinIR("1", "N")]),
        ],
    )
    parent.children = [leaf]
    root.children = [parent]

    S._flatten_small_leaves(root)

    assert parent.children == []
    assert {c.ref for c in parent.components} == {"R1", "U1", "C1"}


def test_flatten_keeps_top_level_module():
    """A leaf module parented by the root keeps its own sheet (never flattened)."""
    root = S.SheetIR(name="root", node_id="r")
    leaf = S.SheetIR(name="dac", node_id="d", components=[ComponentIR("U1", "", [])])
    root.children = [leaf]

    S._flatten_small_leaves(root)

    assert len(root.children) == 1


def test_render_hierarchical_labels_every_pin(tmp_path: Path):
    app = _build_synthetic_app()
    comps, nets = extract_components(app)
    files, summary = S.render_hierarchical(
        comps, nets, app, root_stem="syn", search_dirs=[]
    )
    # No sub-modules -> single root file, a label on every connected pin.
    assert set(files) == {"syn.kicad_sch"}
    assert summary.labels == 4
    root_doc = files["syn.kicad_sch"]
    assert "(symbol_instances" in root_doc  # instances centralised in the root
    assert root_doc.count("(global_label") == 4

    path = tmp_path / "syn.kicad_sch"
    path.write_text(root_doc, encoding="utf-8")
    assert kicad.loads(kicad.schematic.SchematicFile, path).kicad_sch is not None


def _two_sheet_tree() -> "S.SheetIR":
    """Root with two child sheets sharing a net across the sheet boundary."""
    root = S.SheetIR(name="root", node_id="r")
    root.children = [
        S.SheetIR(
            name="blockA",
            node_id="a",
            components=[
                ComponentIR("R1", "1k", [PinIR("1", "NET_A"), PinIR("2", "SHARED")])
            ],
        ),
        S.SheetIR(
            name="blockB",
            node_id="b",
            components=[
                ComponentIR("R2", "2k", [PinIR("1", "SHARED"), PinIR("2", "NET_B")])
            ],
        ),
    ]
    return root


def test_render_sheet_tree_structure():
    root = _two_sheet_tree()
    files, summary = S.render_sheet_tree(
        root, {"NET_A", "SHARED", "NET_B"}, root_stem="m", search_dirs=[]
    )
    assert len(files) == 3  # root + two children
    assert summary.components == 2

    root_doc = files["m.kicad_sch"]
    # Root references both child sheets + centralises both instances (nested paths).
    assert root_doc.count("(sheet (at") == 2
    assert root_doc.count("(reference") == 2
    assert re.search(r'\(path "/[0-9a-f-]+/[0-9a-f-]+"', root_doc), root_doc

    # Children carry their symbols but no instances/sheet bookkeeping.
    child_docs = [v for k, v in files.items() if k != "m.kicad_sch"]
    for doc in child_docs:
        assert "(symbol_instances" not in doc
        assert "(sheet_instances" not in doc
        assert "(symbol (lib_id" in doc


@requires_kicad_cli
def test_hierarchical_cross_sheet_netlist(tmp_path: Path):
    # The load-bearing invariant: same-named global labels connect across sheets.
    root = _two_sheet_tree()
    files, _ = S.render_sheet_tree(
        root, {"NET_A", "SHARED", "NET_B"}, root_stem="m", search_dirs=[]
    )
    for fname, doc in files.items():
        (tmp_path / fname).write_text(doc, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    got = _kicad_netlist_membership(tmp_path / "m.kicad_sch", out)
    assert got.get("SHARED") == {"R1.2", "R2.1"}  # spans both child sheets
    assert got.get("NET_A") == {"R1.1"}
    assert got.get("NET_B") == {"R2.2"}


def test_export_schematic_mode_dispatch(tmp_path: Path):
    app = _build_synthetic_app()
    out = tmp_path / "d.kicad_sch"

    # Default is hierarchical (centralised symbol_instances, no drawn wires).
    S.export_schematic(app, target_name="d", out_path=out)
    text = out.read_text()
    assert "(symbol_instances" in text and "(wire " not in text

    # Legacy draw_wires=True still selects the ladder (wire) renderer.
    S.export_schematic(app, target_name="d", out_path=out, draw_wires=True)
    assert "(wire " in out.read_text()


# --------------------------------------------------------------------------------------
# Net classification (power / ground / signal)
# --------------------------------------------------------------------------------------
def _build_power_app() -> fabll.Node:
    """Two components whose pads tie to the hv (power) and lv (ground) of a rail."""
    from faebryk.libs.net_naming import attach_net_names
    from faebryk.libs.nets import bind_electricals_to_fbrk_nets

    g = fabll.graph.GraphView.create()
    tg = fbrk.TypeGraph.create(g=g)

    class _Comp(fabll.Node):
        a = F.Electrical.MakeChild()
        b = F.Electrical.MakeChild()
        lead_a = fabll.Traits.MakeEdge(F.Lead.is_lead.MakeChild(), [a])
        lead_b = fabll.Traits.MakeEdge(F.Lead.is_lead.MakeChild(), [b])
        a.add_dependant(lead_a)
        b.add_dependant(lead_b)
        _is_interface = fabll.Traits.MakeEdge(fabll.is_interface.MakeChild())
        _is_module = fabll.Traits.MakeEdge(fabll.is_module.MakeChild())

    class _App(fabll.Node):
        c1 = _Comp.MakeChild()
        c2 = _Comp.MakeChild()
        pwr = F.ElectricPower.MakeChild()
        _is_module = fabll.Traits.MakeEdge(fabll.is_module.MakeChild())

    app = _App.bind_typegraph(tg).create_instance(g=g)
    pwr = app.pwr.get()
    comps = [app.c1.get(), app.c2.get()]
    for idx, comp in enumerate(comps, start=1):
        fabll.Traits.create_and_add_instance_to(comp, F.has_designator).setup(
            designator=f"R{idx}"
        )
        fp_node = fabll.Node.bind_typegraph(tg).create_instance(g=g)
        fp_trait = fabll.Traits.create_and_add_instance_to(
            fp_node, F.Footprints.is_footprint
        )
        for lead_iface, num in ((comp.a.get(), "1"), (comp.b.get(), "2")):
            pad_node = fabll.Node.bind_typegraph(tg).create_instance(g=g)
            pad = fabll.Traits.create_and_add_instance_to(
                pad_node, F.Footprints.is_pad
            ).setup(pad_name=num, pad_number=num)
            fabll.Traits.create_and_add_instance_to(
                node=lead_iface.get_trait(F.Lead.is_lead),
                trait=F.Lead.has_associated_pads,
            ).setup(pad)
            fp_node.add_child(pad_node)
        fabll.Traits.create_and_add_instance_to(
            comp, F.Footprints.has_associated_footprint
        ).setup(fp_trait)
        # a -> rail hv (power), b -> rail lv (ground)
        comp.a.get()._is_interface.get().connect_to(pwr.hv.get())
        comp.b.get()._is_interface.get().connect_to(pwr.lv.get())

    nets = bind_electricals_to_fbrk_nets(tg, g)
    attach_net_names(nets)
    return app


def test_classify_nets_signal_only():
    app = _build_synthetic_app()
    roles = S.classify_nets(app)
    assert roles and set(roles.values()) == {S.NetRole.SIGNAL}


def test_classify_nets_power_and_ground():
    app = _build_power_app()
    roles = S.classify_nets(app)
    assert S.NetRole.POWER in roles.values()
    assert S.NetRole.GROUND in roles.values()


# --------------------------------------------------------------------------------------
# Power symbols (M3b)
# --------------------------------------------------------------------------------------
def test_build_power_symbol():
    gnd = build_power_symbol("atopile:GND_GND", "GND", ground=True)
    assert gnd.pin_xy == {"1": (0.0, 0.0)}
    assert "(power)" in gnd.lib_symbol_text
    # connects by name: a power_in pin whose name is the net
    assert "power_in" in gnd.lib_symbol_text
    assert '(name "GND"' in gnd.lib_symbol_text

    pwr = build_power_symbol("atopile:PWR_V3", "V3", ground=False)
    assert "(power)" in pwr.lib_symbol_text and '(name "V3"' in pwr.lib_symbol_text


def test_build_pwr_flag_symbol():
    flag = build_pwr_flag_symbol("atopile:PWR_FLAG")
    # a power_out driver (clears power_pin_not_driven)
    assert "(power)" in flag.lib_symbol_text and "power_out" in flag.lib_symbol_text


def test_render_sheet_tree_power_symbols_replace_labels():
    root = S.SheetIR(
        name="root",
        node_id="r",
        components=[
            ComponentIR(
                "U1", "", [PinIR("1", "VCC"), PinIR("2", "GND"), PinIR("3", "SIG")]
            )
        ],
    )
    roles = {
        "VCC": S.NetRole.POWER,
        "GND": S.NetRole.GROUND,
        "SIG": S.NetRole.SIGNAL,
    }
    files, summary = S.render_sheet_tree(
        root, {"VCC", "GND", "SIG"}, root_stem="p", search_dirs=[], net_roles=roles
    )
    doc = files["p.kicad_sch"]
    assert summary.power_symbols == 2  # VCC + GND pins
    assert summary.labels == 1  # only the signal pin is labelled
    assert doc.count("(global_label") == 1
    # The ground net is already named "GND", so its symbol id is not doubled (no "GND_GND").
    assert "atopile:PWR_VCC" in doc and "atopile:GND" in doc and "atopile:GND_GND" not in doc
    # exactly one PWR_FLAG driver per rail net
    assert doc.count('(reference "#FLG') == 2


@requires_kicad_cli
def test_power_app_rails_connect_via_power_symbols(tmp_path: Path):
    app = _build_power_app()
    comps, nets = extract_components(app)
    files, summary = S.render_hierarchical(
        comps, nets, app, root_stem="pwr", search_dirs=[]
    )
    # Every rail pin became a power symbol; no labels left.
    assert summary.power_symbols == 4
    assert summary.labels == 0

    for fname, doc in files.items():
        (tmp_path / fname).write_text(doc, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    # Rails connect through the power symbols (by power-pin name). The ground rail (lv) is
    # auto-named "GND" by the net-naming pass; the hv rail has no solved voltage here so it
    # keeps the generic "hv".
    got = _kicad_netlist_membership(tmp_path / "pwr.kicad_sch", out)
    assert got.get("hv") == {"R1.1", "R2.1"}
    assert got.get("GND") == {"R1.2", "R2.2"}

    # The PWR_FLAG drivers keep ERC error-free.
    root_path = tmp_path / "pwr.kicad_sch"
    erc = subprocess.run(
        [_KICAD_CLI, "sch", "erc", "-o", str(out / "erc.rpt"), str(root_path)],
        capture_output=True,
        text=True,
    )
    report = (out / "erc.rpt").read_text() if (out / "erc.rpt").exists() else erc.stdout
    assert "Errors 0" in report, report


# --------------------------------------------------------------------------------------
# Deterministic filenames + stale-file cleanup (M3c)
# --------------------------------------------------------------------------------------
def _named_two_sheet_tree() -> "S.SheetIR":
    root = S.SheetIR(name="root", node_id="r")
    root.children = [
        S.SheetIR(
            name="power", node_id="p",
            components=[ComponentIR("U1", "", [PinIR("1", "A")])],
        ),
        S.SheetIR(
            name="sensor", node_id="s",
            components=[ComponentIR("U2", "", [PinIR("1", "B")])],
        ),
    ]
    return root


def test_hierarchical_child_filenames_deterministic():
    # Readable, hex-free, and identical across renders (no per-build random suffix).
    f1, _ = S.render_sheet_tree(
        _named_two_sheet_tree(), {"A", "B"}, root_stem="default", search_dirs=[]
    )
    f2, _ = S.render_sheet_tree(
        _named_two_sheet_tree(), {"A", "B"}, root_stem="default", search_dirs=[]
    )
    assert set(f1) == {
        "default.kicad_sch",
        "default-power.kicad_sch",
        "default-sensor.kicad_sch",
    }
    assert set(f1) == set(f2)


def test_hierarchical_filename_collision_gets_suffix():
    # Two sheets whose names collide get a deterministic "-2".
    root = S.SheetIR(name="root", node_id="r")
    root.children = [
        S.SheetIR(
            name="dup", node_id="1",
            components=[ComponentIR("U1", "", [PinIR("1", "A")])],
        ),
        S.SheetIR(
            name="dup", node_id="2",
            components=[ComponentIR("U2", "", [PinIR("1", "B")])],
        ),
    ]
    files, _ = S.render_sheet_tree(
        root, {"A", "B"}, root_stem="default", search_dirs=[]
    )
    assert "default-dup.kicad_sch" in files
    assert "default-dup-2.kicad_sch" in files


def test_export_schematic_cleans_stale_children(tmp_path: Path):
    app = _build_synthetic_app()
    out = tmp_path / "default.kicad_sch"
    stale = tmp_path / "default-oldmodule.kicad_sch"
    stale.write_text("stale", encoding="utf-8")

    S.export_schematic(app, target_name="default", out_path=out)  # hierarchical default

    assert out.exists()
    assert not stale.exists()  # leftover from a prior build is removed


# --------------------------------------------------------------------------------------
# Q4/Q5: symbol-parse warning + per-path real-symbol cache
# --------------------------------------------------------------------------------------
def test_real_symbol_parse_failure_warns(tmp_path, caplog):
    # A file that isn't a valid .kicad_sym must fall back to None *and* warn the user
    # (the component otherwise silently drops to a generic box) — CODE_AUDIT Q4.
    from faebryk.exporters.schematic.kicad.real_symbol import real_symbol_from_file

    bad = tmp_path / "broken.kicad_sym"
    bad.write_text("this is not an s-expression symbol file")
    with caplog.at_level("WARNING"):
        assert real_symbol_from_file(bad) is None
    assert any("generic box" in r.message for r in caplog.records)


def test_symbol_registry_caches_real_symbol_by_path(monkeypatch, tmp_path):
    # Two components backed by the same .kicad_sym must parse the file once (Q5).
    sym_path = tmp_path / "part.kicad_sym"
    sym = build_generic_symbol("atopile:CACHED", ["1", "2"])
    calls = {"n": 0}

    def fake_real(path):
        calls["n"] += 1
        return sym

    monkeypatch.setattr(S, "_find_symbol_file", lambda module, dirs: sym_path)
    monkeypatch.setattr(S, "real_symbol_from_file", fake_real)

    reg = S._SymbolRegistry(search_dirs=[])
    sentinel = object()
    c1 = ComponentIR(ref="R1", value="", pins=[PinIR("1", "a")], module=sentinel)
    c2 = ComponentIR(ref="R2", value="", pins=[PinIR("1", "a")], module=sentinel)
    assert reg.resolve(c1) is sym
    assert reg.resolve(c2) is sym
    assert calls["n"] == 1  # parsed once, served from cache the second time
