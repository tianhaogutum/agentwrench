"""`agentwrench handoff`: small, offline CLI. JSON on stdout; bounded errors on stderr."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from . import SUBCOMMANDS, __version__
from .core import capture, describe, export_diff, read_manifest, record, verify
from .evidence import load_keys
from .io import HandoffError
from .models import Manifest, Task


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="agentwrench handoff",
        description="Git-state adapter for local in-toto command evidence and task handoffs. "
        "Operational commands print JSON on stdout; errors are JSON on stderr.",
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = result.add_subparsers(dest="action", required=True)

    def add(name: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=SUBCOMMANDS[name], description=SUBCOMMANDS[name])

    repo_help = "Git working tree to read (default: current directory)"
    create = add("capture")
    create.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    create.add_argument("--task", type=Path, required=True, help="task claims JSON (see 'schema task')")
    create.add_argument("--log", type=Path, action="append", default=[],
                        help="in-toto Link file to embed (repeatable; traditional or DSSE)")
    create.add_argument("--output", "-o", type=Path, required=True,
                        help="handoff JSON to write (outside the repository or Git-ignored)")
    create.add_argument("--base", help="Optional committed range base; resolved to a full object ID")
    create.add_argument("--diff", type=Path, help="Optional exact native HEAD-to-worktree binary diff")
    create.add_argument("--force", action="store_true", help="replace an existing output")
    check = add("verify")
    check.add_argument("manifest", type=Path, help="handoff JSON written by capture")
    check.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    check.add_argument("--bundle-only", action="store_true", help="Check the bundle without accessing a repository")
    check.add_argument("--key", type=Path, action="append", default=[], help="External trusted in-toto public key JSON")
    check.add_argument("--require-signed", action="store_true",
                       help="fail unless every receipt is signed by a supplied --key")
    check.add_argument("--require-current-pass", action="store_true",
                       help="fail unless a passing receipt is bound to the current source state")
    run = add("record")
    run.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    run.add_argument("--output", "-o", type=Path, required=True,
                     help="in-toto Link file to write (outside the repository or Git-ignored)")
    run.add_argument("--name", default="check", help="in-toto step name (default: check)")
    run.add_argument("--product", action="append", default=[],
                     help="expected regular-file output, repository-relative (repeatable)")
    run.add_argument("--timeout", type=int, default=30, help="seconds, 1 to 300 (default: 30)")
    run.add_argument("--signing-key", type=Path, help="unencrypted PKCS8 PEM private key")
    run.add_argument("--dsse", action="store_true", help="write a DSSE envelope instead of a Metablock")
    run.add_argument("--force", action="store_true", help="replace an existing output on success")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="command to run, after --")
    inspect = add("inspect")
    inspect.add_argument("manifest", type=Path, help="handoff JSON written by capture")
    diff = add("diff")
    diff.add_argument("--repo", type=Path, default=Path.cwd(), help=repo_help)
    diff.add_argument("--output", "-o", type=Path, required=True,
                      help="patch file to write (outside the repository or Git-ignored)")
    diff.add_argument("--force", action="store_true", help="replace an existing output")
    schema = add("schema")
    schema.add_argument("kind", choices=["manifest", "task"], default="manifest", nargs="?")
    return result


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=True, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "capture":
            manifest = capture(
                repo=args.repo, task_path=args.task, logs=args.log, output=args.output,
                base=args.base, supplied_diff=args.diff, force=args.force,
            )
            emit({"output": str(args.output), "head": manifest.snapshot.head, "source_state_sha256": manifest.snapshot.state_sha256, "command_logs": len(manifest.evidence), "task_completion": "not_assessed"})
            return 0
        if args.action == "verify":
            result = verify(
                read_manifest(args.manifest), repo=None if args.bundle_only else args.repo,
                keys=load_keys(args.key), require_signed=args.require_signed,
                require_current_pass=args.require_current_pass,
            )
            emit(result)
            return 0 if result["ok"] else 1
        if args.action == "record":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            code, result = record(
                repo=args.repo, argv=command, output=args.output, name=args.name,
                products=args.product, timeout=args.timeout, signing_key=args.signing_key,
                dsse=args.dsse, force=args.force,
            )
            emit(result)
            return 0 if code == 0 and not result["missing_requested_products"] else 1
        if args.action == "inspect":
            emit(describe(read_manifest(args.manifest)))
            return 0
        if args.action == "diff":
            emit(export_diff(repo=args.repo, output=args.output, force=args.force))
            return 0
        emit((Manifest if args.kind == "manifest" else Task).model_json_schema())
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"error": "operation interrupted; verify any existing output before using it"}), file=sys.stderr)
        return 130
    except ValidationError as exc:
        # Avoid Pydantic's default repr, which embeds arbitrary input contents.
        # Unknown object keys are input too: report only schema-owned field names.
        schema = Manifest.model_json_schema()
        known = set(schema.get("properties", {}))
        for definition in schema.get("$defs", {}).values():
            known.update(definition.get("properties", {}))
        issues = exc.errors(include_input=False)[:10]
        fields = [
            ".".join(str(part) if isinstance(part, int) or part in known else "<unknown>" for part in item["loc"])[:256]
            for item in issues
        ]
        print(json.dumps({"error": "schema validation failed", "fields": fields, "issue_types": [item["type"] for item in issues]}), file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "recorded command timed out; no completed receipt was written"}), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"error": f"local operation failed ({exc.strerror or 'operating system error'})", "errno": exc.errno}), file=sys.stderr)
        return 2
    except (HandoffError, ValueError) as exc:
        message = str(exc) if isinstance(exc, HandoffError) else "local operation failed (details omitted)"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
