# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Deterministic KiCad UUIDs shared between the schematic emitter and the PCB transformer.

KiCad links a schematic symbol to its PCB footprint by matching the footprint's
``(path "/<sheet-uuid…>/<symbol-uuid>")`` to the symbol's instance path. atopile generates
both files from the graph, so for cross-probing to work both emitters must mint the *same*
UUID for the same component/sheet, stably across rebuilds.

We derive every UUID deterministically from the node's atopile address
(``Node.get_full_name(include_uuid=False)`` — the same value written as the footprint
``atopile_address`` property), via ``uuid5`` under a fixed namespace.
"""

import uuid

# Fixed namespace so derived UUIDs are stable across processes/rebuilds. Do not change —
# changing it re-randomises every component/sheet UUID (breaks existing project linkage).
_NS = uuid.UUID("a7f3c2d1-9e84-4b6a-8c1f-2d5e6f7a8b90")


def stable_uuid(key: str) -> str:
    """Return a deterministic UUID string for ``key`` (e.g. an atopile address)."""
    return str(uuid.uuid5(_NS, key))
