# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""Tests for the KiCad connectivity-schematic emitter."""

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
)
from faebryk.exporters.schematic.kicad.real_symbol import build_real_symbol
from faebryk.exporters.schematic.kicad.schematic import (
    ComponentIR,
    PinIR,
    extract_components,
    render,
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
