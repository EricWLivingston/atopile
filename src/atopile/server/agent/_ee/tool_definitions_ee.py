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
                "Search the engineering knowledge base (datasheets, app notes/white "
                "papers, textbooks) and return ranked, cited chunks. It is the best "
                "source for application guidance, theory, and worked examples. Use it "
                "(1) BEFORE designing a circuit/subsystem — look up design guidance "
                "for the topology (e.g. 'LDO output capacitor ESR stability') during "
                "planning; (2) to ground any spec/claim in a source instead of "
                "guessing. Prefer it over web_search for anything the corpus may "
                "cover; web_search may still be used to find designs, but only on "
                "reputable sites (see the rag_search skill). Call "
                "skill_read('rag_search') before first use for scope/when-to-use rules."
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
                "Run an ngspice analysis on a SPICE netlist you author and return "
                "summary stats per probe. ONLY for analog subcircuits with a definable "
                "spec (oscillator frequency, LDO/RC transient, filter cutoff, bias "
                "point). Do NOT simulate purely-digital logic (use "
                "design_diagnostics), datasheet-answerable specs (use rag_search), or "
                "whole boards — scope each run to the minimal subcircuit. Call "
                "skill_read('pyspice_run') "
                "before your first simulation for netlist/model/scope rules. "
                "Pass the circuit body in 'netlist' (device lines only; ground is node "
                "0 — do NOT add .tran/.ac/.op or .end, they are generated from "
                "'analysis'/'params'). Bundled models you may reference without an "
                ".include: Dgen, Dschottky, DLED, Q2N3904, Q2N3906, NMOS_GEN, PMOS_GEN,"
                " OPAMP_IDEAL (X1 inp inn out OPAMP_IDEAL; GBW ~1 MHz, no rails/no "
                "clipping); for accurate parts inline a vendor .model. Raw waveforms "
                "go to "
                "result_file (.npz); you get min/max/mean only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "netlist": {
                        "type": "string",
                        "description": (
                            "SPICE circuit body (device lines, plus any .model/.subckt/"
                            ".param). No analysis cards or .end."
                        ),
                    },
                    "netlist_path": {
                        "type": ["string", "null"],
                        "description": "Alternative to 'netlist': path to a deck.",
                    },
                    "analysis": {
                        "type": "string",
                        "enum": ["op", "dc", "ac", "tran"],
                    },
                    "params": {
                        "type": "object",
                        "description": (
                            "Analysis params. tran: {t_step,t_end,uic?}; ac: "
                            "{variation(dec|lin|oct),n_points,f_start,f_stop}; dc: "
                            "{source,start,stop,step}; op: {}."
                        ),
                        "additionalProperties": True,
                    },
                    "probes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": (
                            "Nodes/currents to record: a node name ('out' or 'v(out)') "
                            "or a source current ('i(v1)'). Empty = all nodes."
                        ),
                    },
                    "project_path": {"type": ["string", "null"]},
                },
                "required": ["analysis"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "ipc_check",
            "description": (
                "Check the built design against IPC standards (IPC-2221B trace "
                "width/clearance, IPC-2152 current capacity) using declared net "
                "currents and the PCB layout. Returns findings cited to clauses. Call "
                "skill_read('ipc_check') before first use for scope/when-to-use rules."
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
        {
            "type": "function",
            "name": "skills_list",
            "description": (
                "List every available skill (id + one-line description). Skills are "
                "specialized guidance docs you can load on demand; the always-loaded "
                "core skills are flagged. Call this to discover guidance before a "
                "specialized task, then load one with skill_read."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "skill_read",
            "description": (
                "Read one skill's full guidance doc on demand. Use skills_list to find "
                "the id. For EE tools, read the matching skill before first use (e.g. "
                "skill_read('pyspice_run') before simulating) to get scope/when-to-use "
                "rules and avoid wasted work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Skill id from skills_list, e.g. 'pyspice_run'.",
                    },
                },
                "required": ["skill_id"],
                "additionalProperties": False,
            },
        },
    ]
