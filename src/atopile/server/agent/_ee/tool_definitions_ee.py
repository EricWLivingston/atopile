"""EE-agent custom tool schemas (Option C).

Function-tool definitions for the three custom EE tools (`rag_search`, `pyspice_run`,
`ipc_check`) plus an `ee_ping` smoke tool. These are spliced into the runtime tool list
by ``tool_definitions.get_tool_definitions``; the matching handlers live in
``_ee/tools_ee.py``. Schemas mirror the documented signatures in
``ee_agent_docs_5_21/`` (``05_RAG``, ``02_SIMULATION``, ``04_VERIFICATION``).

Pure data — imports nothing from ``tools`` (avoids an import cycle).
"""

from __future__ import annotations

from typing import Any


def get_ee_tool_definitions() -> list[dict[str, Any]]:
    """OpenAI Responses API function-tool definitions for the EE tools."""
    return [
        {
            "type": "function",
            "name": "ee_ping",
            "description": (
                "Connectivity smoke-test tool for the EE agent toolchain. Echoes the "
                "given message back. Use only to verify tool plumbing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "default": ""},
                },
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "rag_search",
            "description": (
                "Search the engineering knowledge base (datasheets, standards, app "
                "notes, atopile examples/docs) and return ranked, cited chunks. Use to "
                "ground design decisions in sources rather than guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "corpus": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "Subset of collections to search; null = all."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "filter": {
                        "type": ["object", "null"],
                        "description": (
                            "Optional metadata filter, e.g. {\"mpn\": \"TLV713P\"}."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "pyspice_run",
            "description": (
                "Run a SPICE analysis (DC operating point, transient, or AC small-"
                "signal) on a netlist and return probed results. Use to verify "
                "circuit behaviour against spec."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "netlist_path": {"type": "string"},
                    "analysis": {
                        "type": "string",
                        "enum": ["dc", "ac", "tran"],
                    },
                    "params": {
                        "type": "object",
                        "description": (
                            "Analysis-specific parameters, e.g. "
                            "{\"t_end\": \"10ms\", \"t_step\": \"10us\"}."
                        ),
                        "additionalProperties": True,
                    },
                    "probes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": "Node or device names to record.",
                    },
                    "project_path": {"type": ["string", "null"]},
                },
                "required": ["netlist_path", "analysis"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "ipc_check",
            "description": (
                "Check the built design against IPC standards (IPC-2221B trace "
                "width/clearance, IPC-2152 current capacity) using declared net "
                "currents and the PCB layout. Returns findings cited to clauses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "build_target": {"type": "string", "default": "default"},
                    "standards": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["IPC-2221B", "IPC-2152"],
                    },
                    "ambient_temp_rise_c": {"type": "number", "default": 10.0},
                    "copper_weight_oz": {"type": "number", "default": 1.0},
                    "project_path": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
    ]
