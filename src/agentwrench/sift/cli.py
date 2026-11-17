"""Command line for ``agentwrench sift``: summarize, recover and verify evidence envelopes."""

from __future__ import annotations

import argparse
import json
import sys

from .core import (
    ENCODINGS,
    MAX_BYTES,
    BudgetError,
    InputError,
    Rules,
    _strict_json,
    recover,
    summarize,
)
from .verification import verify


def _span(value: str) -> tuple[int, int]:
    try:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use START:END") from exc


def _read(path: str, limit: int = MAX_BYTES) -> bytes:
    if path == "-":
        data = sys.stdin.buffer.read(limit + 1)
    else:
        with open(path, "rb") as stream:
            data = stream.read(limit + 1)
    if len(data) > limit:
        raise InputError(f"input exceeds {limit} bytes")
    return data


def _rules(args: argparse.Namespace) -> Rules:
    values = {}
    if args.rules:
        try:
            values = _strict_json(_read(args.rules, 65_536).decode("utf-8"))
        except InputError as exc:
            raise InputError(f"invalid rules: {exc}") from exc
        except UnicodeError as exc:
            raise InputError("rules file must contain valid UTF-8 JSON") from exc
        if not isinstance(values, dict) or values.keys() - {
            "keep",
            "keep_patterns",
            "keep_lines",
            "priority_keys",
            "context_lines",
        }:
            raise InputError("rules must be an object with only documented rule keys")
        for key in ("keep", "keep_patterns", "keep_lines", "priority_keys"):
            if key in values and not isinstance(values[key], list):
                raise InputError(f"rules.{key} must be a list")
    values["keep"] = tuple(values.get("keep", [])) + tuple(args.keep)
    values["keep_patterns"] = tuple(values.get("keep_patterns", [])) + tuple(args.keep_pattern)
    values["keep_lines"] = tuple(values.get("keep_lines", [])) + tuple(args.keep_line)
    if "priority_keys" in values:
        values["priority_keys"] = tuple(values["priority_keys"])
    if args.context is not None:
        values["context_lines"] = args.context
    return Rules(**values)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="agentwrench sift",
        description="Token-bounded exact-source evidence for JSON results and test logs.",
    )
    sub = root.add_subparsers(dest="command", required=True)
    make = sub.add_parser("summarize", help="emit one compact evidence JSON envelope")
    make.add_argument("input", help="UTF-8 input file, or - for stdin")
    make.add_argument("--format", required=True, choices=("json", "log"))
    make.add_argument(
        "--budget", required=True, type=int, help="whole-envelope token cap (1..32768)"
    )
    make.add_argument("--encoding", choices=ENCODINGS, default="cl100k_base")
    make.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="POINTER",
        help="required exact JSON Pointer; repeatable",
    )
    make.add_argument(
        "--keep-pattern",
        action="append",
        default=[],
        metavar="PATTERN",
        help="required anchored pointer pattern; * matches one path segment",
    )
    make.add_argument(
        "--keep-line",
        action="append",
        type=_span,
        default=[],
        metavar="START:END",
        help="required inclusive one-based log line range",
    )
    make.add_argument("--context", type=int, help="context lines around errors (0..10)")
    make.add_argument("--rules", help="JSON rules file; CLI keep rules are added to it")
    make.add_argument("--stats", action="store_true", help="write measured token count to stderr")
    read = sub.add_parser("recover", help="read original bytes after verifying summary SHA-256")
    read.add_argument("input", help="original input file, or - for stdin")
    read.add_argument("--summary", required=True, help="saved evidence envelope")
    read.add_argument(
        "--bytes",
        required=True,
        type=_span,
        metavar="START:END",
        help="zero-based half-open byte range",
    )
    check = sub.add_parser(
        "verify", help="check saved evidence and its exact serialized token count"
    )
    check.add_argument("input", help="original input file, or - for stdin")
    check.add_argument("--summary", required=True, help="saved evidence envelope")
    check.add_argument("--budget", type=int, help="optional whole-serialization token cap")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "summarize":
            if args.input == "-" and args.rules == "-":
                raise InputError("input and rules cannot both read stdin")
            rules = _rules(args)
            summary = summarize(
                _read(args.input),
                budget=args.budget,
                format=args.format,
                rules=rules,
                encoding=args.encoding,
            )
            # No trailing newline: the exact stdout string is the one that was measured.
            sys.stdout.buffer.write(summary.text.encode("utf-8"))
            if args.stats:
                print(
                    json.dumps({"tokens": summary.tokens, "budget": args.budget}), file=sys.stderr
                )
        elif args.command == "verify":
            if args.input == "-" and args.summary == "-":
                raise InputError("input and summary cannot both read stdin")
            serialized = _read(args.summary)
            tokens = verify(_read(args.input), serialized, budget=args.budget)
            sys.stdout.write(
                json.dumps({"verified": True, "tokens": tokens, "budget": args.budget})
            )
        else:
            if args.input == "-" and args.summary == "-":
                raise InputError("input and summary cannot both read stdin")
            try:
                document = json.loads(_read(args.summary, 2 * MAX_BYTES))
            except (ValueError, UnicodeError) as exc:
                raise InputError("summary must contain valid UTF-8 JSON") from exc
            sys.stdout.buffer.write(recover(_read(args.input), document, *args.bytes))
        sys.stdout.flush()
        return 0
    except BudgetError as exc:
        print(
            json.dumps({"error": "budget_exceeded", "message": str(exc), "needed": exc.needed}),
            file=sys.stderr,
        )
        return 3
    except InputError as exc:
        print(json.dumps({"error": "invalid_input", "message": str(exc)}), file=sys.stderr)
        return 2
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        print(json.dumps({"error": "io_error", "message": "output pipe closed"}), file=sys.stderr)
        return 4
    except OSError as exc:
        print(json.dumps({"error": "io_error", "message": str(exc)}), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
