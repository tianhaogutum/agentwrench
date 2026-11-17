"""Command line entry point. Never writes, applies or executes candidate code."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys

from . import SUBCOMMANDS, __version__
from .errors import FenceError, SystemCallError, error_message

MAX_PATCH_INPUT_BYTES = 8 * 1024 * 1024
MAX_ALLOW_FILE_BYTES = 256 * 1024
_SURROGATE = re.compile("[\ud800-\udfff]")

DESCRIPTION = "Check a TypeScript patch against exact symbol boundaries."
EPILOG = """\
Default: outside text, including whitespace, must be unchanged. Never writes or
executes candidate code. Exit 0: accepted; 1: scope rejected; 2: invalid evidence.
Renames, removals, additions of files, mode changes and binary patches fail closed.

examples:
  agentwrench fence symbols --base-dir DIR --file path.ts [--json]
  agentwrench fence symbols --repo DIR --base REV --file path.ts [--json]
  agentwrench fence check --base-dir DIR --patch FILE --allow 'path.ts::symbol' [--json]
  agentwrench fence check --repo DIR --base REV --patch FILE --allow 'path.ts::Class.method'
"""


class _Parser(argparse.ArgumentParser):
    """Usage errors become ``usage`` reports (JSON with --json) instead of argparse's exit.

    --help and --version still exit through SystemExit(0), like every agentwrench tool.
    """

    def error(self, message):
        raise FenceError("usage", f"{message}; run '{self.prog} --help' for usage")


def _baseline_options(command: argparse.ArgumentParser) -> None:
    group = command.add_argument_group("baseline (choose one)")
    group.add_argument("--base-dir", metavar="DIR", help="baseline directory (keep it stable while checking)")
    group.add_argument("--repo", metavar="DIR", help="Git repository; requires --base")
    group.add_argument("--base", metavar="REV", help="Git revision, resolved once to an immutable tree")


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + "."


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="agentwrench fence", description=DESCRIPTION, epilog=EPILOG, allow_abbrev=False,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="{check,symbols}")
    check = sub.add_parser("check", help=SUBCOMMANDS["check"], description=_sentence(SUBCOMMANDS["check"]),
                           epilog=EPILOG, allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter)
    _baseline_options(check)
    check.add_argument("--patch", metavar="FILE", help="unified patch; - reads stdin (8 MiB limit)")
    check.add_argument("--allow", metavar="VALUE", action="append",
                       help="exact selector 'path.ts::qualified.symbol'; repeat for more; no globs")
    check.add_argument("--allow-file", metavar="FILE", help="JSON array of exact symbol selectors")
    check.add_argument("--ignore-formatting", action="store_true",
                       help="permit whitespace with unchanged CST/comment line boundaries")
    check.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    symbols = sub.add_parser("symbols", help=SUBCOMMANDS["symbols"],
                             description=_sentence(SUBCOMMANDS["symbols"]), allow_abbrev=False)
    _baseline_options(symbols)
    symbols.add_argument("--file", metavar="PATH", help="repository-relative .ts/.tsx/.mts/.cts file")
    symbols.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    return parser


def read_limited(file: str, limit: int) -> str:
    """Read a regular file or stdin ('-') of at most ``limit`` bytes as strict UTF-8."""
    stdin = file == "-"
    if not stdin:
        try:
            fd = os.open(file, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except OSError as error:
            raise SystemCallError(error, "open", file) from None
    try:
        if not stdin:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise FenceError("input_limit", f"Expected a regular file no larger than {limit} bytes: {file}")
        chunks = []
        total = 0
        while True:
            try:
                chunk = sys.stdin.buffer.read(64 * 1024) if stdin else os.read(fd, 64 * 1024)
            except OSError as error:
                raise SystemCallError(error, "read", file) from None
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise FenceError("input_limit", f"Input exceeds {limit} bytes")
            chunks.append(chunk)
        try:
            return b"".join(chunks).decode("utf-8")  # strict; a leading BOM is kept
        except UnicodeDecodeError:
            raise FenceError("invalid_encoding", f"Input is not UTF-8: {file}") from None
    finally:
        if not stdin:
            os.close(fd)


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _write(stream, text: str) -> None:
    text = _SURROGATE.sub("\ufffd", text)  # as Node writes unpaired surrogates
    data = (text + "\n").encode("utf-8")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
        buffer.write(data)
        buffer.flush()
    else:
        stream.write(text + "\n")


def _json(value) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False)
    # JSON.stringify escapes unpaired surrogates; keep the output valid UTF-8 likewise.
    return _SURROGATE.sub(lambda match: f"\\u{ord(match.group(0)):04x}", text)


def _run(args) -> int:
    from .baseline import directory_baseline, git_baseline
    from .check import check_patch
    from .patch import is_typescript, safe_relative
    from .syntax import list_symbols, parse_source

    if bool(args.base_dir) == bool(args.repo) or (args.repo and not args.base) or (args.base_dir and args.base):
        raise FenceError("usage", "Choose --base-dir DIR, or --repo DIR together with --base REV")
    read_base = git_baseline(args.repo, args.base) if args.repo else directory_baseline(args.base_dir)
    if args.command == "symbols":
        if not args.file or not is_typescript(args.file):
            raise FenceError("usage", "symbols requires --file path.ts (or .tsx/.mts/.cts)")
        file = safe_relative(args.file)
        symbols = list_symbols(parse_source(read_base(file), file))
        if args.json:
            _write(sys.stdout, _json({"schemaVersion": 1, "file": file, "symbols": symbols}))
        else:
            for symbol in symbols:
                start, end = symbol["range"]["start"], symbol["range"]["end"]
                _write(sys.stdout, f"{file}::{symbol['selector']}\t{symbol['kind']}\t"
                                   f"{start['line']}:{start['column']}-{end['line']}:{end['column']}")
        return 0
    if not args.patch:
        raise FenceError("usage", "check requires --patch FILE (use - for stdin)")
    allow = list(args.allow or [])
    if args.allow_file:
        try:
            entries = json.loads(read_limited(args.allow_file, MAX_ALLOW_FILE_BYTES), parse_constant=_reject_constant)
        except Exception as error:
            raise FenceError("invalid_allow", f"Cannot read allow-file: {error_message(error)}") from None
        if not isinstance(entries, list):
            raise FenceError("invalid_allow", "allow-file must contain a JSON array")
        allow.extend(entries)
    report = check_patch(patch=read_limited(args.patch, MAX_PATCH_INPUT_BYTES), allow=allow, read_base=read_base,
                         ignore_formatting=bool(args.ignore_formatting))
    if args.json:
        _write(sys.stdout, _json(report))
    else:
        lines = [f"{'PASS' if report['ok'] else 'REJECT'}: {len(report['accepted'])} accepted symbol edit(s), "
                 f"{len(report['violations'])} outside change(s)"]
        lines += [f"  allowed {edit['file']}::{edit['selector']}" for edit in report["accepted"]]
        for change in report["violations"]:
            where = f":{change['range']['start']['line']}:{change['range']['start']['column']}" if change.get("range") else ""
            lines.append(f"  outside {change['file']}{where} {change['reason']} "
                         f"{change.get('kind') or ''} {change.get('side') or ''}")
        lines += [f"  ignored whitespace {ignored['file']}" for ignored in report["ignoredFormatting"]]
        for entry in report["files"]:
            if entry.get("truncated"):
                lower = "at least " if entry.get("countIsLowerBound") else ""
                limit = f"; {entry['diagnosticLimit']}" if entry.get("diagnosticLimit") else " in total"
                lines.append(f"  {entry['file']}: output truncated; {lower}{entry['outsideChanges']} outside changes{limit}")
        for line in lines:
            _write(sys.stdout, line)
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in args_list
    try:
        parser = build_parser()
        args = parser.parse_args(args_list)
        json_output = bool(getattr(args, "json", False))
        if args.command is None:
            raise FenceError("usage", parser.format_help().rstrip("\n"))
        return _run(args)
    except Exception as error:  # every failure is a typed exit-2 report, never a traceback
        details = error.details if isinstance(error, FenceError) else {}
        code = error.code if isinstance(error, (FenceError, SystemCallError)) else "input_error"
        report = {"schemaVersion": 1, "ok": False, "error": {"code": code, "message": error_message(error), **details}}
        if json_output:
            _write(sys.stdout, _json(report))
        else:
            _write(sys.stderr, f"ERROR [{code}]: {report['error']['message']}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
