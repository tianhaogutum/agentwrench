"""Inspect LangGraph SQLite checkpoints without loading user graph code."""
from agentwrench import __version__  # noqa: F401

# One-line help per subcommand. Kept here (no LangGraph import) so that
# `agentwrench checkpoints --help` works before the `langgraph` extra is installed.
SUBCOMMANDS = {
    "threads": "list threads with checkpoint/write counts, bytes and timestamps",
    "stats": "rank channels by size and show growth per checkpoint step",
    "diff": "show channels added, removed or changed between two checkpoints of a thread",
    "prune": "keep the latest N checkpoints per thread and delete older ones (explicit backup choice)",
    "snapshot": "print a retained-history byte snapshot (--json) for the budget gate",
    "plan-status": "read-only: report whether a saved prune plan is still current (exit 3 if stale)",
    "budget": "compare saved snapshots against per-group byte growth limits",
    "compare-plans": "compare saved checkpoint actions without database access",
}
