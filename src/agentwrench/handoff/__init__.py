"""Hand work to the next agent with evidence it can check.

Plain packs (``pack``, ``run``, ``render``, ``verify`` of a pack) use only the
standard library: ``pack.py``, ``pack_runs.py``, ``pack_scope.py`` and
``pack_safety.py``. In-toto receipts (``record``, ``capture``, ``inspect``,
``diff``, ``schema``, ``verify`` of a receipt handoff) live in ``core``,
``evidence``, ``git``, ``models`` and ``receipts`` and need the ``handoff``
extra (in-toto and Pydantic); this module imports neither.
"""
from agentwrench import __version__  # noqa: F401

# One-line help per subcommand, plain packs first. Kept here (no in-toto/Pydantic
# import) so that `agentwrench handoff --help` works before the extra is installed.
SUBCOMMANDS = {
    "pack": "write a handoff pack (handoff.json + handoff.md) from a Git worktree and a task",
    "run": "run one command without a shell and save a run record to attach with 'pack --evidence'",
    "verify": "check a pack or an in-toto handoff against the repository now (never runs commands)",
    "render": "print or write the handoff.md of a pack from its handoff.json",
    "record": "in-toto: run one local command under in-toto and add a Git source-state receipt",
    "capture": "in-toto: package task claims, Git state and diffs, and in-toto logs into one JSON",
    "inspect": "in-toto: summarize an in-toto handoff without accessing a repository",
    "diff": "in-toto: export the exact bounded native diff accepted by capture --diff",
    "schema": "in-toto: print the JSON Schema of the in-toto handoff manifest or task file",
}
# Subcommands that always need the `handoff` extra (`verify` needs it for in-toto handoffs only).
RECEIPT_SUBCOMMANDS = ("record", "capture", "inspect", "diff", "schema")
