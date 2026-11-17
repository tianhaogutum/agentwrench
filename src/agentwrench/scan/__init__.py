"""``agentwrench scan``: read-only audit of Claude Code / Codex context overhead and breakage.

Combines the other subpackages: :mod:`agentwrench.mcpconfig` (MCP server inventory),
:mod:`agentwrench.slim` (optional ``--probe`` discovery and token estimates),
:mod:`agentwrench.agentsmd` (AGENTS.md / CLAUDE.md budgets),
:mod:`agentwrench.doctor` (session tool-call pairing) and the plain-pack half of
:mod:`agentwrench.handoff` (staleness of handoff packs found in the project).
"""

from agentwrench import __version__  # noqa: F401

SCHEMA = "agentwrench.scan/v1"
