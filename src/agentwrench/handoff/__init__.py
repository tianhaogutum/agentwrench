"""Git-anchored, scope-aware task handoffs built on in-toto command receipts.

The library modules (``core``, ``evidence``, ``git``, ``models``) need the
``handoff`` extra (in-toto and Pydantic); this module does not import them.
"""
from agentwrench import __version__  # noqa: F401

# One-line help per subcommand. Kept here (no in-toto/Pydantic import) so that
# `agentwrench handoff --help` works before the `handoff` extra is installed.
SUBCOMMANDS = {
    "capture": "package task claims, Git state and diffs, and local in-toto logs into one JSON",
    "verify": "check integrity, current Git/file state and command evidence (never runs commands)",
    "record": "run one local command under in-toto and add a Git source-state receipt",
    "inspect": "summarize a handoff without accessing a repository",
    "diff": "export the exact bounded native diff accepted by capture --diff",
    "schema": "print the JSON Schema of the handoff manifest or task file",
}
