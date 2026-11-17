"""Top-level ``agentwrench`` command: dispatches to one tool per subcommand.

Each tool lives in its own subpackage exposing ``cli.main(argv) -> int``. Tools
are imported lazily, so ``agentwrench --help`` stays fast and a missing optional
dependency only affects the command that needs it.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import importlib
import importlib.util
import sys

from . import __version__

PROG = "agentwrench"
REPO_URL = "https://github.com/tianhaogutum/agentwrench"


@dataclass(frozen=True)
class Command:
    name: str
    module: str
    summary: str
    group: str
    extra: str | None = None
    requires: tuple[str, ...] = ()
    # False: the command runs without its extra and gates only the subcommands that need it.
    gated: bool = True


GROUPS = ("Tokens & cost", "Sessions", "MCP", "Instructions", "Review", "Other")

COMMANDS: tuple[Command, ...] = (
    Command("scan", "agentwrench.scan.cli",
            "Audit context overhead and breakage: MCP tools, AGENTS.md/CLAUDE.md, sessions",
            "Tokens & cost"),
    Command("slim", "agentwrench.slim.cli",
            "Shrink the tool definitions an MCP server exposes", "Tokens & cost"),
    Command("cache", "agentwrench.cache.cli",
            "Find where consecutive LLM requests stop sharing a cacheable prefix", "Tokens & cost"),
    Command("sift", "agentwrench.sift.cli",
            "Shrink one large tool result or test log into token-bounded, source-located evidence",
            "Tokens & cost", extra="sift",
            requires=("shrinkfit", "tiktoken", "json_source_map", "jsonpointer", "strip_ansi")),
    Command("doctor", "agentwrench.doctor.cli",
            "Validate and repair tool-call pairing in Codex/Claude Code session JSONL", "Sessions"),
    Command("export", "agentwrench.export.cli",
            "Normalize sessions to one trajectory format; redacted hand-off Markdown", "Sessions"),
    Command("gate", "agentwrench.gate.cli",
            "Normalize headless agent streams and enforce CI policy gates", "Sessions"),
    Command("mcp-config", "agentwrench.mcpconfig.cli",
            "Convert MCP server configs between Claude Code, Codex, Cline, Continue, VS Code", "MCP"),
    Command("cassette", "agentwrench.cassette.cli",
            "Record and replay MCP stdio sessions (includes a pytest plugin)", "MCP"),
    Command("agentsmd", "agentwrench.agentsmd.cli",
            "Show which AGENTS.md bytes Codex loads, truncation, and CLAUDE.md imports", "Instructions"),
    Command("fence", "agentwrench.fence.cli",
            "Check that a unified diff to TypeScript only edits the symbols you allowed", "Review",
            extra="fence", requires=("tree_sitter", "tree_sitter_typescript")),
    Command("handoff", "agentwrench.handoff.cli",
            "Hand work to the next agent: a task pack that detects when it went stale; in-toto receipts",
            "Review", extra="handoff", requires=("in_toto", "pydantic"), gated=False),
    Command("patch", "agentwrench.patch.cli",
            "Parse, apply, convert and explain Codex apply_patch (V4A) patches", "Other"),
    Command("checkpoints", "agentwrench.checkpoints.cli",
            "Inspect, diff and prune LangGraph SQLite checkpoints", "Other",
            extra="langgraph", requires=("langgraph.checkpoint.sqlite",)),
)
BY_NAME = {command.name: command for command in COMMANDS}

# Former standalone tool names, used only to point users at the new subcommand.
LEGACY = {
    "rollout-doctor": "doctor", "lgckpt": "checkpoints", "agentsmd-scope": "agentsmd",
    "mcpconf": "mcp-config", "trajport": "export", "mcp-cassette": "cassette",
    "trajgate": "gate", "v4a": "patch", "mcp-slim": "slim", "prefix-drift": "cache",
    "tool-sift": "sift", "structured-truncate": "sift", "symbol-fence": "fence",
    "anchored-handoff": "handoff", "relayproof": "handoff",
}


def _present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _installed(command: Command) -> bool:
    return _present(command.module.rpartition(".")[0])


def _missing_requirements(command: Command) -> bool:
    return any(not _present(name) for name in command.requires)


def _install_hint(command: Command) -> str:
    return (f"{PROG} {command.name}: missing optional dependencies; "
            f"install them with: pip install '{PROG}[{command.extra}]'")


def extra_hint(name: str, *, force: bool = False) -> str | None:
    """The install hint of command ``name`` when its extra is missing (or ``force``), else None.

    Used by commands with ``gated=False`` for the subcommands that need the extra.
    """
    command = BY_NAME[name]
    if command.extra and (force or _missing_requirements(command)):
        return _install_hint(command)
    return None


def _help_without_extra(command: Command) -> str:
    """A usage summary that needs only the command's package, not its optional extra."""
    lines = [f"usage: {PROG} {command.name} <subcommand> [args...]", "", command.summary + "."]
    try:
        subcommands = getattr(importlib.import_module(command.module.rpartition(".")[0]), "SUBCOMMANDS", {})
    except ImportError:
        subcommands = {}
    if subcommands:
        width = max(map(len, subcommands)) + 2
        lines += ["", "subcommands:", *(f"  {name.ljust(width)}{text}" for name, text in subcommands.items())]
    return "\n".join(lines) + "\n\n"


def overview() -> str:
    width = max(len(command.name) for command in COMMANDS) + 2
    lines = [
        f"{PROG} {__version__} - offline dev tools for AI coding agents (Claude Code, Codex, MCP)",
        "",
        f"usage: {PROG} <command> [args...]",
        f"       {PROG} <command> --help",
    ]
    for group in GROUPS:
        lines += ["", f"{group}:"]
        for command in COMMANDS:
            if command.group != group:
                continue
            note = ""
            if not _installed(command):
                note = "  (coming soon)"
            elif command.extra and _missing_requirements(command):
                note = (f"  (needs {PROG}[{command.extra}])" if command.gated
                        else f"  (some subcommands need {PROG}[{command.extra}])")
            lines.append(f"  {command.name.ljust(width)}{command.summary}{note}")
    lines += [
        "",
        "options:",
        "  -h, --help     show this overview",
        "  -V, --version  show the version and exit",
        "",
        f"Docs and issues: {REPO_URL}",
    ]
    return "\n".join(lines) + "\n"


def _unknown(name: str) -> int:
    message = f"{PROG}: unknown command {name!r}"
    if name in LEGACY:
        message += f"; {name} is now '{PROG} {LEGACY[name]}'"
    else:
        match = difflib.get_close_matches(name, [*BY_NAME, *LEGACY], n=1, cutoff=0.6)
        if match:
            suggestion = LEGACY.get(match[0], match[0])
            message += f"; did you mean '{suggestion}'?"
    sys.stderr.write(message + f"\nRun '{PROG} --help' to list commands.\n")
    return 2


def run(command: Command, argv: list[str]) -> int:
    if not _installed(command):
        sys.stderr.write(f"{PROG}: command {command.name!r} is not available in "
                         f"{PROG} {__version__} yet; see {REPO_URL}\n")
        return 2
    if command.extra and command.gated and _missing_requirements(command):
        if any(arg in ("-h", "--help") for arg in argv):
            sys.stdout.write(_help_without_extra(command))
            sys.stdout.write(_install_hint(command) + "\n")
            return 0
        sys.stderr.write(_install_hint(command) + "\n")
        return 2
    try:
        module = importlib.import_module(command.module)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if command.extra and any(req == missing or req.startswith(missing + ".") for req in command.requires):
            sys.stderr.write(_install_hint(command) + "\n")
            return 2
        raise
    result = module.main(argv)
    return 0 if result is None else result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(overview())
        return 0
    first, rest = args[0], args[1:]
    if first in ("-V", "--version"):
        sys.stdout.write(f"{PROG} {__version__}\n")
        return 0
    if first == "help":
        if not rest:
            sys.stdout.write(overview())
            return 0
        first, rest = rest[0], ["--help"]
    if first.startswith("-"):
        sys.stderr.write(f"{PROG}: unknown option {first!r}\nRun '{PROG} --help' for usage.\n")
        return 2
    command = BY_NAME.get(first)
    if command is None:
        return _unknown(first)
    return run(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
