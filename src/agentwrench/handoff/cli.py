"""`agentwrench handoff`: hand work to the next agent with evidence it can check.

Two levels of evidence share one command:

* plain packs (default, standard library only): ``pack``, ``run``, ``render`` and
  ``verify`` of a pack;
* in-toto receipts (needs ``agentwrench[handoff]``): ``record``, ``capture``,
  ``inspect``, ``diff``, ``schema`` and ``verify`` of a receipt handoff.

This module imports neither in-toto nor Pydantic. The receipt subcommands load
them only after their arguments are parsed, so ``--help`` and the pack workflow
work without the extra. JSON on stdout; errors are one JSON object on stderr.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from pathlib import Path

from . import RECEIPT_SUBCOMMANDS, SUBCOMMANDS, __version__

DESCRIPTION = """\
Hand work to the next agent (or person) with evidence it can check.

Plain packs need no extra packages: 'pack' writes handoff.json and handoff.md
from a Git worktree and a task, 'run' records one real command run to attach,
'verify' reports whether the pack has gone stale, 'render' prints handoff.md.
In-toto receipts are the stronger, optional mode: 'record' runs a command under
in-toto, 'capture' packages the receipts with Git state and task claims, and
'verify' checks them (signatures, current pass).
"""

EPILOG = """\
'verify' detects the format: a pack directory or its handoff.json is checked
without extra packages; an in-toto handoff JSON needs the extra, as do record,
capture, inspect, diff and schema: pip install 'agentwrench[handoff]'
Docs: docs/handoff.md
"""


def _add(commands: argparse._SubParsersAction, name: str) -> argparse.ArgumentParser:
    return commands.add_parser(name, help=SUBCOMMANDS[name], description=SUBCOMMANDS[name])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="agentwrench handoff", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = result.add_subparsers(dest="action", required=True)
    repo_help = "Git working tree to read (default: current directory)"

    # ---- plain packs (standard library only)
    pack = _add(commands, "pack")
    pack.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    task = pack.add_mutually_exclusive_group(required=True)
    task.add_argument("--goal", help="task goal text (stored as an unverified user assertion)")
    task.add_argument("--task", type=Path,
                      help="task JSON: goal and optional completed/pending/blockers/next lists of strings")
    pack.add_argument("--evidence", type=Path, action="append", default=[],
                      help="run record written by 'run' to include (repeatable)")
    pack.add_argument("--output", "-o", type=Path, required=True,
                      help="new pack directory to create (outside the repository or Git-ignored)")
    run = _add(commands, "run")
    run.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    run.add_argument("--output", "-o", type=Path, required=True,
                     help="new run record JSON to write (outside the repository or Git-ignored)")
    run.add_argument("--timeout-ms", type=int, default=30000,
                     help="milliseconds, 1 to 3600000 (default: 30000); then the process group is killed")
    run.add_argument("--label", help="short description stored with the record (default: 'test command')")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="command to run without a shell, after --")
    check = _add(commands, "verify")
    check.add_argument("handoff", type=Path,
                       help="pack directory, its handoff.json, or an in-toto handoff JSON written by capture")
    check.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    check.add_argument("--bundle-only", action="store_true",
                       help="in-toto handoffs: check the bundle without accessing a repository")
    check.add_argument("--key", type=Path, action="append", default=[],
                       help="in-toto handoffs: external trusted in-toto public key JSON (repeatable)")
    check.add_argument("--require-signed", action="store_true",
                       help="in-toto handoffs: fail unless every receipt is signed by a supplied --key")
    check.add_argument("--require-current-pass", action="store_true",
                       help="in-toto handoffs: fail unless a passing receipt is bound to the current source state")
    render = _add(commands, "render")
    render.add_argument("pack", type=Path, help="pack directory or its handoff.json")
    render.add_argument("--output", "-o", type=Path,
                        help="new Markdown file to write (default: print to stdout)")

    # ---- in-toto receipts (agentwrench[handoff])
    receipt = _add(commands, "record")
    receipt.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    receipt.add_argument("--output", "-o", type=Path, required=True,
                         help="in-toto Link file to write (outside the repository or Git-ignored)")
    receipt.add_argument("--name", default="check", help="in-toto step name (default: check)")
    receipt.add_argument("--product", action="append", default=[],
                         help="expected regular-file output, repository-relative (repeatable)")
    receipt.add_argument("--timeout", type=int, default=30, help="seconds, 1 to 300 (default: 30)")
    receipt.add_argument("--signing-key", type=Path, help="unencrypted PKCS8 PEM private key")
    receipt.add_argument("--dsse", action="store_true", help="write a DSSE envelope instead of a Metablock")
    receipt.add_argument("--force", action="store_true", help="replace an existing output on success")
    receipt.add_argument("argv", nargs=argparse.REMAINDER, help="command to run, after --")
    create = _add(commands, "capture")
    create.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    create.add_argument("--task", type=Path, required=True, help="task claims JSON (see 'schema task')")
    create.add_argument("--log", type=Path, action="append", default=[],
                        help="in-toto Link file to embed (repeatable; traditional or DSSE)")
    create.add_argument("--output", "-o", type=Path, required=True,
                        help="handoff JSON to write (outside the repository or Git-ignored)")
    create.add_argument("--base", help="Optional committed range base; resolved to a full object ID")
    create.add_argument("--diff", type=Path, help="Optional exact native HEAD-to-worktree binary diff")
    create.add_argument("--force", action="store_true", help="replace an existing output")
    inspect = _add(commands, "inspect")
    inspect.add_argument("manifest", type=Path, help="in-toto handoff JSON written by capture")
    diff = _add(commands, "diff")
    diff.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    diff.add_argument("--output", "-o", type=Path, required=True,
                      help="patch file to write (outside the repository or Git-ignored)")
    diff.add_argument("--force", action="store_true", help="replace an existing output")
    schema = _add(commands, "schema")
    schema.add_argument("kind", choices=["manifest", "task"], default="manifest", nargs="?")
    return result


# --------------------------------------------------------------------------- format detection


def handoff_format(path: Path) -> str:
    """``"pack"`` (a directory or a ``relayproof-handoff/1`` JSON), ``"receipts"`` (an in-toto handoff), or ``"unknown"``."""
    from .pack import BUNDLE_VERSION

    try:
        if stat.S_ISDIR(os.stat(path).st_mode):
            return "pack"
        with open(path, "rb") as handle:
            head = handle.read(4096).decode("utf-8", "replace")
    except (OSError, ValueError):
        return "unknown"
    if f'"schema": "{BUNDLE_VERSION}"' in head or f'"schema":"{BUNDLE_VERSION}"' in head:
        return "pack"
    return "receipts" if '"schema_version"' in head else "unknown"


def is_pack(path: Path) -> bool:
    """Whether ``verify`` checks ``path`` as a plain pack (no extra needed); unknown input is read as a pack."""
    return handoff_format(path) != "receipts"


def _wants_receipts(args: argparse.Namespace) -> bool:
    if args.action in RECEIPT_SUBCOMMANDS:
        return True
    if args.action != "verify":
        return False
    kind = handoff_format(args.handoff)
    if kind == "unknown":
        # Receipt-only options name the format; otherwise the extra-free loader reports the error.
        return bool(args.bundle_only or args.key or args.require_signed or args.require_current_pass)
    return kind == "receipts"


# --------------------------------------------------------------------------- plain packs


def _out(text: str) -> None:
    from .pack_safety import utf8

    sys.stdout.flush()
    sys.stdout.buffer.write(utf8(text))
    sys.stdout.buffer.flush()


def _error(message: str, **extra: object) -> None:
    print(json.dumps({"error": message, **extra}, ensure_ascii=True), file=sys.stderr)


def _plain(args: argparse.Namespace) -> int:
    from ..cache.jscompat import stringify
    from .io import HandoffError
    from .pack import capture_pack, load_pack, render_markdown, task_input, verify_pack
    from .pack_runs import result_status, run_evidence
    from .pack_safety import new_output, read_json, redact, utf8, write_new
    from .pack_scope import output_path, repository

    try:
        if args.action == "verify":
            if args.bundle_only or args.key or args.require_signed or args.require_current_pass:
                raise HandoffError("--bundle-only, --key, --require-signed and --require-current-pass apply to "
                                   "in-toto handoffs only; a plain pack has no receipts")
            repo = repository(str(args.repo))
            report = verify_pack(repo, load_pack(str(args.handoff)))
            _out(stringify(report, 2) + "\n")
            return 0 if report["status"] == "matches_captured_scope" else 1
        if args.action == "render":
            text = render_markdown(load_pack(str(args.pack)))
            if args.output is None:
                _out(text)
            else:
                write_new(new_output(str(args.output)), text)
                _out(stringify({"output": str(args.output), "bytes": len(utf8(text))}) + "\n")
            return 0
        repo = repository(str(args.repo))
        output = output_path(str(args.output), repo)
        if args.action == "run":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            options = {} if args.label is None else {"label": args.label}
            record = run_evidence(repo, command, output, timeout_ms=args.timeout_ms, **options)
            _out(stringify({"result": result_status(record), "exitCode": record["exitCode"],
                            "signal": record["signal"], "evidenceSaved": True}) + "\n")
            if record["timedOut"]:
                return 124
            if record["spawnError"]:
                return 127
            if record["signal"]:
                return 128
            return record["exitCode"]
        task = task_input(read_json(str(args.task), 1024 * 1024) if args.task is not None else {"goal": args.goal})
        bundle = capture_pack(repo, task, [str(path) for path in args.evidence], output)
        _out(stringify({"handoffCreated": True, "files": len(bundle["repository"]["snapshot"]["files"]),
                        "evidence": len(bundle["evidence"])}) + "\n")
        return 0
    except KeyboardInterrupt:
        _error("operation interrupted; check any output before using it")
        return 130
    except HandoffError as exc:
        _error(redact(str(exc)))
        return 2
    except OSError as exc:
        # OS errors can contain private paths: report only the error code.
        code = errno.errorcode.get(exc.errno or 0, "UNKNOWN")
        _error(f"Filesystem/process error ({code})", errno=exc.errno)
        return 2
    except (ValueError, TypeError, KeyError, RecursionError):
        _error("local operation failed (details omitted)")
        return 2


# --------------------------------------------------------------------------- dispatch


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not _wants_receipts(args):
        return _plain(args)
    from agentwrench.cli import extra_hint

    hint = extra_hint("handoff")
    if hint is None:
        try:
            from . import receipts
        except ModuleNotFoundError:
            hint = extra_hint("handoff", force=True)
        else:
            return receipts.run(args)
    sys.stderr.write(hint + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
