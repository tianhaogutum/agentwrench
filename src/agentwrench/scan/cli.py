"""``agentwrench scan`` command line. Exit 0=scanned, 1=--fail-over exceeded, 2=usage/I/O error."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

from . import __version__
from .common import Finding, Locations
from .instructions import scan_instructions
from .mcp import collect_servers, mcp_findings, probe
from .report import build, render_text
from .sessions import scan_sessions

DESCRIPTION = """\
Show where your Claude Code and Codex setup spends context tokens on every turn,
and what is broken: MCP tool definitions, AGENTS.md / CLAUDE.md instructions and
recent session files. Each problem comes with the agentwrench command that fixes it.

Read-only and offline: scan reads config, instruction and session files under
--home and --project, never writes a file and never uses the network.
--probe is the only option that executes anything: it starts each configured
stdio MCP server locally (its own command, args, env and cwd, as the client
would), lists its tools, and stops it. Those servers run your configured code,
which may itself do anything that code does (an `npx` server may download).
"""

EPILOG = """\
exit status: 0 scan completed (even with findings), 1 --fail-over exceeded,
2 usage or I/O error. Docs: docs/scan.md (JSON schema agentwrench.scan/v1).
"""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse exits 2 on usage errors
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a number of seconds") from None
    if not math.isfinite(value) or not 0 < value <= 600:
        raise argparse.ArgumentTypeError("expected seconds in (0, 600]")
    return value


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a non-negative integer") from None
    if value < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return value


def parser() -> argparse.ArgumentParser:
    root = Parser(prog="agentwrench scan", description=DESCRIPTION, epilog=EPILOG,
                  formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--project", type=Path, default=None, metavar="DIR",
                      help="project directory whose .mcp.json and instruction files apply (default: cwd)")
    root.add_argument("--home", type=Path, default=None, metavar="DIR",
                      help="use DIR instead of ~ for ~/.claude.json, ~/.claude and ~/.codex "
                           "(default: ~, honoring CLAUDE_CONFIG_DIR and CODEX_HOME)")
    root.add_argument("--probe", action="store_true",
                      help="EXECUTES each configured stdio MCP server locally to count its tool-definition "
                           "tokens; remote servers are never contacted")
    root.add_argument("--timeout", type=_positive_seconds, default=10.0, metavar="SECONDS",
                      help="per-server limit for --probe (default: 10)")
    root.add_argument("--sessions", type=_count, default=50, metavar="N",
                      help="check the N most recent session files per client (default: 50; 0 skips)")
    root.add_argument("--fail-over", type=_count, default=None, metavar="TOKENS",
                      help="exit 1 if any client's estimated fixed overhead per turn exceeds TOKENS (for CI)")
    root.add_argument("--json", action="store_true", help="print the agentwrench.scan/v1 JSON document")
    root.add_argument("--no-color", action="store_true", help="disable color (also: NO_COLOR, or non-TTY stdout)")
    return root


def _use_color(args: argparse.Namespace) -> bool:
    if args.no_color or args.json or os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _directory(path: Path, option: str) -> Path:
    resolved = Path(os.path.abspath(path.expanduser()))
    if not resolved.is_dir():
        raise ValueError(f"{option}: not a directory: {path}")
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    started = time.monotonic()
    try:
        project = _directory(args.project if args.project is not None else Path.cwd(), "--project")
        home = _directory(args.home, "--home") if args.home is not None else None
    except (ValueError, OSError) as exc:
        print(f"agentwrench scan: {json.dumps(str(exc), ensure_ascii=True)[1:-1]}", file=sys.stderr)
        return 2
    loc = Locations.resolve(home, project)
    findings: list[Finding] = []
    try:
        servers = collect_servers(loc, findings)
        probe_seconds = probe(servers, args.timeout) if args.probe else None
        findings += mcp_findings(servers, project, args.probe)
        instructions = scan_instructions(loc, findings)
        sessions = scan_sessions(loc, args.sessions, findings)
    except KeyboardInterrupt:
        print("agentwrench scan: interrupted", file=sys.stderr)
        return 130
    options = {"probe": args.probe, "timeout_s": args.timeout, "sessions": args.sessions}
    doc = build(loc, servers=servers, probed=args.probe, probe_seconds=probe_seconds,
                instructions=instructions, sessions=sessions, findings=findings, options=options,
                elapsed=time.monotonic() - started, fail_over=args.fail_over)
    try:
        if args.json:
            sys.stdout.write(json.dumps(doc, ensure_ascii=True, indent=2) + "\n")
        else:
            sys.stdout.write(render_text(doc, servers, findings, _use_color(args)))
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    return 1 if doc["summary"]["over_limit"] else 0
