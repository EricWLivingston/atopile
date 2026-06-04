"""EE-agent additions to atopile's agent harness.

This package holds the fork's net-new code that sits alongside the upstream
atopile agent without modifying it: the Anthropic LLM provider and (later) the
custom EE tools registered into atopile's ToolRegistry.

Importing this package is intentionally side-effect-free. Modules here are
wired into the runtime explicitly (e.g. provider selection in
``routes/agent/utils.py``); nothing is activated merely by importing ``_ee``.
"""
