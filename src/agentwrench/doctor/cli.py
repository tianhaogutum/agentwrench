"""Command-line interface. Diagnostics on stderr never include session contents."""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import sys
import time

from . import __version__
from .audit import audit
from .audit_migration import migrate_audit
from .audit_verification import verify_audit
from .engine import DoctorError, Report, UndetectedFormat, scan
from .files import assert_unchanged, open_session, publish
from .manifest import apply, preview
from .repair import make_plan, transform, validate_plan
from .recovery import PairingRefusal, recover, verify_backup, verify_output
from .._text import plural


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DoctorError(message)


def parser() -> argparse.ArgumentParser:
    root = Parser(prog="agentwrench doctor", description="Offline tool-call pairing checks and conservative JSONL repair")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    check = commands.add_parser("check", help="check one file or recursively check a directory's *.jsonl files")
    repair = commands.add_parser("repair", help="repair a file; choose --out, --in-place --backup, or --dry-run")
    preview_command = commands.add_parser("preview", help="save a SHA-256-bound repair manifest without writing the output")
    preview_destinations = preview_command.add_mutually_exclusive_group(required=True)
    preview_destinations.add_argument("--out", type=Path, help="new repaired-file destination bound in the manifest")
    preview_destinations.add_argument("--in-place", action="store_true", help="bind replacement of the source file")
    preview_command.add_argument("--backup", action="store_true", help="required acknowledgment for --in-place")
    preview_command.add_argument("--manifest", type=Path, required=True, help="new private JSON repair manifest")
    apply_command = commands.add_parser("apply", help="apply a saved manifest, or verify it is already applied")
    apply_command.add_argument("path", type=Path, help="previously saved repair manifest")
    apply_command.add_argument("--expect-manifest-sha256", metavar="HASH",
                               help="require this independently retained full manifest-byte SHA-256 (64 lowercase hex)")
    apply_command.add_argument("--json", action="store_true", help="machine-readable apply status")
    recovery = commands.add_parser("recover", help="verify an explicit v2 backup into a new file; never restore in place")
    backup_verification = commands.add_parser("verify-backup", help="verify an explicit v2 backup without creating files")
    for command in (recovery, backup_verification):
        command.add_argument("path", type=Path, help="trusted v2 in-place repair manifest")
        command.add_argument("--backup", type=Path, required=True, help="explicit original-byte backup (no discovery)")
        command.add_argument("--expect-manifest-sha256", metavar="HASH",
                             help="require this independently retained full manifest-byte SHA-256 (64 lowercase hex)")
        command.add_argument("--json", action="store_true", help="metadata-only status and hashes")
    recovery.add_argument("--out", type=Path, required=True, help="new recovery destination; never the current session")
    output_verification = commands.add_parser("verify-output", help="verify explicit v1/v2 repaired bytes (source only with explicit v2 candidate)")
    output_verification.add_argument("path", type=Path, help="trusted v1 new-file or v2 in-place repair manifest")
    output_verification.add_argument("--candidate", type=Path, required=True, help="explicit immutable regular output or copy")
    output_verification.add_argument("--expect-manifest-sha256", metavar="HASH",
                                     help="require this independently retained full manifest-byte SHA-256 (64 lowercase hex)")
    output_verification.add_argument("--check-pairing", action="store_true",
                                     help="also require clean pairing using the saved detected format")
    output_verification.add_argument("--diagnostic-summary", action="store_true",
                                     help="with --check-pairing, explain pairing refusals using only reason/code counts")
    output_verification.add_argument("--json", action="store_true", help="metadata-only status and hashes")
    audit_command = commands.add_parser("audit", help="read-only, resumable single-file audit with byte offsets")
    audit_command.add_argument("--checkpoint", type=Path, required=True, help="private JSON cursor; reused on subsequent runs")
    audit_command.add_argument("--allow-incomplete-final", action="store_true",
                               help="defer a syntactically incomplete final record without a newline")
    migration = commands.add_parser("migrate-audit", help="replay a v1 accepted prefix into a new v2 checkpoint")
    migration.add_argument("--checkpoint", type=Path, required=True, help="existing v1 audit checkpoint (unchanged)")
    migration.add_argument("--out", type=Path, required=True, help="new v2 audit checkpoint destination")
    verification = commands.add_parser("verify-audit", help="verify v2 cached metadata by read-only accepted-prefix replay")
    verification.add_argument("--checkpoint", type=Path, required=True, help="existing v2 checkpoint (unchanged)")
    for command in (check, repair, audit_command, preview_command, migration, verification):
        command.add_argument("path", type=Path)
        command.add_argument("--format", choices=("auto", "codex", "claude"), default="auto")
        command.add_argument("--json", action="store_true", help="machine-readable report (no payload text)")
    for command in (repair, preview_command):
        command.add_argument("--strategy", choices=("stub", "drop"), default="stub")
        command.add_argument("--drop-invalid", action="store_true", help="also discard invalid JSON/structurally invalid records")
    repair.add_argument("--dry-run", action="store_true", help="validate and show a diff without any writes")
    destinations = repair.add_mutually_exclusive_group()
    destinations.add_argument("--out", type=Path)
    destinations.add_argument("--in-place", action="store_true")
    repair.add_argument("--backup", action="store_true", help="required acknowledgment for --in-place")
    return root


def warn_live(source, path: Path) -> None:
    if time.time() - os.fstat(source.fileno()).st_mtime < 5:
        print(f"warning: {path}: modified less than 5 seconds ago; a live session may be writing; stop the agent first", file=sys.stderr)


def show(report: Report, repairing: bool = False, dry_run: bool = False) -> None:
    if repairing:
        print(f"{report.file} [{report.format}]: {plural(len(report.changes), 'change')}" + (" (dry-run)" if dry_run else ""))
        for change in report.changes:
            print(f"  L{change['line']} {change['action']}: {change['detail']}")
    else:
        print(f"{report.file} [{report.format}]: " + (plural(len(report.issues), "issue") if report.issues else "clean"))
        for issue in report.issues:
            cid = " " + json.dumps(issue.call_id, ensure_ascii=True) if issue.call_id is not None else ""
            print(f"  L{issue.line} {issue.code}{cid}: {issue.detail}")


def _line_ops(raw: bytes, changed: list[bytes]):
    """Diff one input line against the lines the plan replaces it with."""
    if raw in changed:  # unchanged record with inserted neighbours (e.g. a stub output)
        at = changed.index(raw)
        return [*(("+", value) for value in changed[:at]), (" ", raw), *(("+", value) for value in changed[at + 1:])]
    return [("-", raw), *(("+", value) for value in changed)]


def _hunks(ops, context: int):
    """Group a stream of (tag, line) ops into unified-diff hunks with ``context`` lines.

    Streams: only lines near a change are kept in memory."""
    old_no = new_no = 1
    hunk: list[tuple[str, bytes]] = []
    start = (1, 1)
    lead: deque[tuple[int, int, bytes]] = deque(maxlen=context)  # equal lines before the next hunk
    tail: list[tuple[int, int, bytes]] = []  # equal lines after the open hunk's last change
    for tag, content in ops:
        if tag == " ":
            if not hunk:
                lead.append((old_no, new_no, content))
            else:
                tail.append((old_no, new_no, content))
                if len(tail) > 2 * context:
                    hunk.extend((" ", line) for _, _, line in tail[:context])
                    yield start, hunk
                    hunk = []
                    lead.clear()
                    lead.extend(tail[len(tail) - context:] if context else [])
                    tail = []
            old_no += 1
            new_no += 1
            continue
        if not hunk:
            start = (lead[0][0], lead[0][1]) if lead else (old_no, new_no)
            hunk = [(" ", line) for _, _, line in lead]
            lead.clear()
        else:
            hunk.extend((" ", line) for _, _, line in tail)
            tail = []
        hunk.append((tag, content))
        if tag == "-":
            old_no += 1
        else:
            new_no += 1
    if hunk:
        hunk.extend((" ", line) for _, _, line in tail[:context])
        yield start, hunk


def _range(start: int, length: int) -> str:
    # Same convention as difflib.unified_diff / GNU diff.
    if length == 1:
        return str(start)
    return f"{start - 1 if not length else start},{length}"


def diff(source, path: Path, plan, context: int = 1) -> None:
    """Print a unified diff of the planned repair (one context line: records can be long)."""
    print(f"--- {path}\n+++ {path} (planned)")
    source.seek(0)
    ops = (op for line, raw in enumerate(source, 1) for op in _line_ops(raw, transform(plan, line, raw)))
    for (old_start, new_start), hunk in _hunks(ops, context):
        old_len = sum(tag != "+" for tag, _ in hunk)
        new_len = sum(tag != "-" for tag, _ in hunk)
        print(f"@@ -{_range(old_start, old_len)} +{_range(new_start, new_len)} @@")
        for prefix, content in hunk:
            # Invalid UTF-8 is escaped; do not fail while previewing --drop-invalid.
            text = content.decode("utf-8", errors="backslashreplace")
            print(prefix + text.rstrip("\n"))
            if not content.endswith(b"\n"):
                print("\\ No newline at end of file")


def paths_in(directory: Path) -> list[Path]:
    result: list[Path] = []
    def fail(error):
        raise error
    for root, dirs, files in os.walk(directory, followlinks=False, onerror=fail):
        dirs[:] = sorted(d for d in dirs if not (Path(root) / d).is_symlink())
        result.extend(Path(root) / name for name in sorted(files) if name.endswith(".jsonl"))
    return result


def main(argv: list[str] | None = None) -> int:
    args = None
    try:
        args = parser().parse_args(argv)
        if args.command in ("preview", "apply"):
            payload = (preview(args.path, args.out, args.manifest, args.format, args.strategy, args.drop_invalid,
                               in_place=args.in_place, backup=args.backup)
                       if args.command == "preview" else apply(args.path, expect_manifest_sha256=args.expect_manifest_sha256))
            if args.json:
                print(json.dumps(payload, ensure_ascii=True))
            else:
                print(f"{payload['status']}: {payload['manifest']}")
                print(f"source: {payload['source']['path']} SHA-256 {payload['source']['sha256']}")
                print(f"output: {payload['output']['path']} SHA-256 {payload['output']['sha256']}")
                for change in payload["report"]["changes"]:
                    print(f"  L{change['line']} {change['action']}: {change['detail']}")
                if payload.get("backup"):
                    print(f"backup: {payload['backup']}")
            return 0
        if args.command in ("verify-backup", "verify-output"):
            payload = (verify_backup(args.path, args.backup, expect_manifest_sha256=args.expect_manifest_sha256)
                       if args.command == "verify-backup"
                       else verify_output(args.path, args.candidate, check_pairing=args.check_pairing,
                                          diagnostic_summary=args.diagnostic_summary,
                                          expect_manifest_sha256=args.expect_manifest_sha256))
            if args.json:
                print(json.dumps(payload, ensure_ascii=True))
            else:
                path = payload["backup"] if args.command == "verify-backup" else payload["candidate"]
                pairing = f"; pairing clean [{payload['pairing']['format']}]" if "pairing" in payload else ""
                print(f"verified: {path} ({payload['verified_bytes']} bytes) SHA-256 {payload['actual_sha256']}{pairing}")
            return 0
        if args.command == "recover":
            payload = recover(args.path, args.backup, args.out, expect_manifest_sha256=args.expect_manifest_sha256)
            if args.json:
                print(json.dumps(payload, ensure_ascii=True))
            else:
                print(f"recovered: {payload['output']['path']} ({payload['restored_bytes']} bytes) "
                      f"SHA-256 {payload['output']['sha256']}")
            return 0
        if args.command == "verify-audit":
            payload = verify_audit(args.path, args.checkpoint, args.format)
            if args.json:
                print(json.dumps(payload, ensure_ascii=True))
            else:
                print(f"{args.path} [{payload['format']}]: {payload['status']} at byte {payload['offset']}; "
                      "diagnostic history not verified")
                if payload["mismatches"]:
                    print("  differing cached fields: " + ", ".join(payload["mismatches"]))
            return 1 if payload["mismatches"] else 0
        if args.command == "migrate-audit":
            payload = migrate_audit(args.path, args.checkpoint, args.out, args.format)
            if args.json:
                print(json.dumps(payload, ensure_ascii=True))
            else:
                print(f"{args.path} [{payload['format']}]: migrated v1 to v2 at byte {payload['offset']}; "
                      f"{plural(payload['emitted_count'], 'diagnostic hash', 'diagnostic hashes')} preserved; checkpoint: {args.out}")
            return 0
        if args.command == "audit":
            payload = audit(args.path, args.checkpoint, args.format, args.allow_incomplete_final)
            if args.json:
                print(json.dumps(payload, ensure_ascii=True))
            else:
                print(f"{args.path} [{payload['format']}]: {plural(payload['issue_count'], 'current issue')}, "
                      f"{len(payload['issues'])} new; cursor byte {payload['offset']}")
                for issue in payload["issues"]:
                    print(f"  L{issue['line']} byte {issue['byte_offset']} {issue['code']}: {issue['detail']}")
                if payload["incomplete_final"]:
                    print(f"  deferred incomplete final record at byte {payload['incomplete_final']['byte_offset']}")
            return 1 if payload["issue_count"] else 0
        if args.command == "check":
            if args.path.is_symlink():
                raise DoctorError("symlink inputs are not supported")
            directory = args.path.is_dir()
            paths = paths_in(args.path) if directory else [args.path]
            reports: list[Report] = []
            errors: list[dict] = []
            skipped: list[dict] = []
            for path in paths:
                try:
                    with open_session(path) as source:
                        warn_live(source, path)
                        report = scan(source, str(path), args.format)
                    if not args.json:
                        show(report)
                    reports.append(report)
                except UndetectedFormat:
                    if not directory:
                        raise
                    # Other JSONL under a sessions tree (e.g. Claude Code subagents/workflows
                    # journals) is not a session: skip it like `agentwrench scan` does.
                    skipped.append({"file": str(path), "reason": "not a Codex or Claude Code session"})
                    if not args.json:
                        print(f"{path}: skipped (not a Codex or Claude Code session)")
                except (OSError, DoctorError) as exc:
                    if not directory:
                        raise
                    errors.append({"file": str(path), "error": str(exc)})
                    print(f"error: {path}: {exc}", file=sys.stderr)
            if args.json:
                payload = ({"files": [r.as_dict() for r in reports], "errors": errors, "skipped": skipped}
                           if directory else reports[0].as_dict())
                print(json.dumps(payload, ensure_ascii=True))
            elif directory and not paths:
                print(f"{args.path}: no *.jsonl files")
            return 2 if errors else int(any(r.issues for r in reports))
        if args.in_place and not args.backup:
            raise DoctorError("--in-place requires --backup; stop the agent before editing")
        if args.backup and not args.in_place:
            raise DoctorError("--backup is only valid with --in-place")
        if not args.dry_run and args.out is None and not args.in_place:
            raise DoctorError("repair requires --out PATH, --in-place --backup, or --dry-run")
        with open_session(args.path, write=not args.dry_run) as source:
            warn_live(source, args.path)
            report = scan(source, str(args.path), args.format)
            plan = make_plan(report, args.strategy, args.drop_invalid)
            validate_plan(source, plan)
            assert_unchanged(source, args.path, plan)
            destination = args.path if args.in_place else args.out
            backup = None
            if args.out is not None and os.path.lexists(args.out):
                raise DoctorError(f"{args.out}: --out must be a new path (refusing overwrite)")
            if args.dry_run:
                if not args.json:
                    show(report, True, True)
                    diff(source, args.path, plan)
            else:
                if args.out is not None or report.changes:
                    backup = publish(source, args.path, destination, plan, args.in_place)
                if not args.json:
                    show(report, True)
                    print(f"validated clean: {destination}")
                    if backup:
                        print(f"backup: {backup}")
            if args.json:
                payload = report.as_dict()
                payload.update({"dry_run": args.dry_run, "clean_after": True,
                                "output": str(destination) if destination else None, "backup": backup})
                print(json.dumps(payload, ensure_ascii=True))
        return 0
    except (OSError, DoctorError, ValueError, RecursionError) as exc:
        summary = exc.diagnostic_summary if isinstance(exc, PairingRefusal) else None
        if args is not None and args.json:
            payload = {"file": str(args.path), "error": str(exc)}
            if summary is not None:
                payload["diagnostic_summary"] = summary
            print(json.dumps(payload, ensure_ascii=True))
        print(f"error: {exc}", file=sys.stderr)
        if summary is not None and not args.json:
            counts = ", ".join(f"{code}={count}" for code, count in summary["issue_counts"].items()) or "none"
            print(f"pairing summary: {summary['reason']}; issue counts: {counts}", file=sys.stderr)
        return 2
