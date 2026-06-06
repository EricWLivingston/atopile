# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
KiCad schematic exporter.

Emits a *connectivity schematic* (`.kicad_sch`): grid-placed component symbols with a
net-name ``global_label`` on every connected pin and no drawn wires. KiCad joins
like-named global labels into a single net, so the result is electrically complete
(passes ERC) even though it is not a human-routed schematic.

The file is emitted as s-expression *text* rather than via the typed ``kicad.dumps``
write path, which currently cannot produce a KiCad-loadable schematic (see
``ee_agent_docs_5_21/07_ATOPILE_GAPS.md`` §2.11).
"""

from faebryk.exporters.schematic.kicad.schematic import (
    SchematicSummary,
    export_schematic,
)

__all__ = ["SchematicSummary", "export_schematic"]
