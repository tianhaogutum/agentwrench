"""CLI with stable 0/pass, 1/gate-failure, 2/input-error exit codes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

from . import __version__
from . import sidecar as sidecar_integrity
from .compare import compare
from .export import export_agentevals
from .model import InputError
from .policy import check, load_policy
from .sidecar import inspect_sidecar, verified_original, verify_sidecar
from .streams import normalize


def write_new_reports(outputs: list[tuple[str, str | bytes]]) -> None:
    """Stage private files, then exclusively link them; roll back on I/O failure.

    A group is not crash-atomic across directories. Keep temporaries until all
    links succeed so rollback can identify exactly the files this call created.
    """
    staged = []
    created = []
    try:
        for path, content in outputs:
            target = Path(path)
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                             prefix=".trajgate-", delete=False) as handle:
                temporary = Path(handle.name)
                staged.append((temporary, target))
                handle.write(content.encode("utf-8") if isinstance(content, str) else content)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, target in staged:
            os.link(temporary, target)
            created.append((temporary, target))
    except BaseException:
        for temporary, target in reversed(created):
            try:
                if os.path.samestat(temporary.stat(), target.lstat()):
                    target.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def write_new(path: str, content: str | bytes) -> None:
    """Create a new report without overwriting inputs or following symlinks."""
    write_new_reports([(path, content)])


def write_stdout(content: bytes) -> None:
    """Write verified bytes without owning or closing the binary stdout stream."""
    binary = sys.stdout.buffer
    # Bypass Python's buffer when available: a broken pipe must not leave
    # pending bytes that fail again during interpreter shutdown (exit 120).
    handle = getattr(binary, "raw", binary)
    remaining = memoryview(content)
    while remaining:
        written = handle.write(remaining)
        if written is None or written <= 0:
            raise OSError("stdout write made no progress")
        remaining = remaining[written:]
    handle.flush()


def _xml_text(value: str) -> str:
    # JSON command output can contain XML 1.0-invalid control characters.
    return "".join(c if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF else "\ufffd" for c in value)


def _console_text(value: str) -> str:
    return "".join(c if c.isprintable() else ascii(c)[1:-1] for c in value)


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected nonnegative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("expected nonnegative integer")
    return number


def junit(report: dict) -> str:
    root = ET.Element("testsuite", name="trajgate", tests=str(report["summary"]["total"]),
                      failures=str(report["summary"]["failed"]), errors="0")
    for r in report["results"]:
        case = ET.SubElement(root, "testcase", classname="trajgate.policy", name=_xml_text(r["id"]))
        text = _xml_text(r["message"] + "; evidence event indexes=" + json.dumps(r["evidence"]))
        if not r["passed"]:
            ET.SubElement(case, "failure", message=_xml_text(r["message"])).text = text
        else:
            ET.SubElement(case, "system-out").text = text
    ET.SubElement(root, "system-out").text = json.dumps({"ignored": report["ignored"], "complete": report["complete"]})
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentwrench gate", description="Offline trajectory normalization and policy gates")
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="action", required=True)
    for name in ("normalize", "export-agentevals", "check"):
        sub = subs.add_parser(name)
        sub.add_argument("stream", help="UTF-8 JSONL file; - reads binary stdin; ./- names a literal file (never executed)")
        sub.add_argument("--from", dest="source", choices=("auto", "codex-exec", "claude-stream"), default="auto")
        sub.add_argument("--root", help="absolute POSIX repository root for lexical path mapping")
        if name == "check":
            sub.add_argument("--policy", required=True,
                             help="YAML policy file; - reads binary stdin (only one input); ./- names a literal file")
            sub.add_argument("--junit", help="create a new JUnit XML file; existing paths are refused")
            sub.add_argument("--json", action="store_true")
            sub.add_argument("-o", "--output", help="create a new private JSON report (requires --json); default stdout")
            sub.add_argument("--allow-incomplete", action="store_true")
        else:
            sub.add_argument("-o", "--output", help="create a new JSON file; default stdout")
            sub.add_argument("--sidecar", help="create a private original-log and loss-accounting JSON file")
    sub = subs.add_parser("compare", help="compare paired task evaluations under one policy")
    sub.add_argument("baseline", help="baseline task manifest (JSON)")
    sub.add_argument("candidate", help="candidate task manifest (JSON)")
    sub.add_argument("--policy", required=True,
                     help="YAML policy file; - reads binary stdin; ./- names a literal file")
    sub.add_argument("--regression-unit", choices=("task", "rule"), default="task",
                     help="count pass-to-fail tasks or task/rule pairs (default task)")
    sub.add_argument("--max-regressions", type=_nonnegative, default=0,
                     help="allowed regressions in the selected unit, inclusively (default 0)")
    sub.add_argument("-o", "--output", help="create a new JSON report; default stdout")
    sub = subs.add_parser("inspect-sidecar", help="inspect sidecar metadata without the primary output")
    sub.add_argument("sidecar", help="version-1 original-log sidecar JSON; - reads binary stdin; ./- names a literal file")
    for name, help_text in (
        ("restore", "verify sidecar hashes and restore exact original JSONL"),
        ("verify-sidecar", "verify sidecar hashes without writing files"),
    ):
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("sidecar", help="version-1 original-log sidecar JSON; - reads binary stdin (only one input may use stdin)")
        sub.add_argument("primary", help="exact primary JSON output paired with the sidecar; - reads binary stdin")
        if name == "restore":
            destination = sub.add_mutually_exclusive_group(required=True)
            destination.add_argument("-o", "--output", help="create a private JSONL file; existing paths are refused")
            destination.add_argument("--stdout", action="store_true",
                                     help="write verified original bytes to binary stdout; success diagnostic goes to stderr")
    args = parser.parse_args(argv)
    if args.action == "check" and args.output is not None and not args.json:
        parser.error("check -o/--output requires --json")
    try:
        if args.action == "inspect-sidecar":
            sidecar = sys.stdin.buffer if args.sidecar == "-" else args.sidecar
            report = inspect_sidecar(sidecar)
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False))
            return 0
        if args.action in ("verify-sidecar", "restore"):
            if args.sidecar == args.primary == "-":
                raise InputError("only one input may use stdin")
            primary = sys.stdin.buffer if args.primary == "-" else args.primary
            sidecar = sys.stdin.buffer if args.sidecar == "-" else args.sidecar
        if args.action == "verify-sidecar":
            report = verify_sidecar(sidecar, primary).as_dict()
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False))
            return 0
        if args.action == "restore":
            original = verified_original(sidecar, primary)
            if args.stdout:
                write_stdout(original)
            else:
                write_new(args.output, original)
            print("Restored verified original JSONL.", file=sys.stderr if args.stdout else sys.stdout)
            return 0
        if args.action == "compare":
            policy = sys.stdin.buffer if args.policy == "-" else args.policy
            report = compare(args.baseline, args.candidate, policy, args.max_regressions,
                             regression_unit=args.regression_unit)
            text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
            if args.output:
                write_new(args.output, text)
            else:
                print(text, end="")
            return 0 if report["passed"] else 1
        rules = None
        if args.action == "check":
            if args.stream == args.policy == "-":
                raise InputError("only one input may use stdin")
            policy = sys.stdin.buffer if args.policy == "-" else args.policy
            rules = load_policy(policy)
        stream = sys.stdin.buffer if args.stream == "-" else args.stream
        trajectory = normalize(stream, args.source, args.root,
                               preserve_original=bool(getattr(args, "sidecar", None)))
        if args.action == "check":
            report = check(trajectory, rules, args.allow_incomplete)
            # Finish both serializations before staging either report or printing.
            try:
                text = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n" if args.json else None
                xml = junit(report) if args.junit else None
            except (TypeError, ValueError) as exc:
                raise InputError(f"cannot serialize check report: {exc}") from exc
            outputs = [(args.output, text)] if args.output is not None else []
            if args.junit:
                outputs.append((args.junit, xml))
            write_new_reports(outputs)
            if args.json:
                if args.output is None:
                    print(text, end="")
            else:
                for result in report["results"]:
                    evidence = ", ".join(f"#{i}" for i in result["evidence"]) or "none"
                    print(_console_text(f"{'PASS' if result['passed'] else 'FAIL'} {result['id']}: {result['message']} (events: {evidence})"))
                print(f"{report['summary']['failed']} failed, {report['summary']['passed']} passed; ignored {report['ignored_count']} unknown/partial events")
            return 0 if report["passed"] else 1
        result = trajectory.as_dict() if args.action == "normalize" else export_agentevals(trajectory)
        if args.action == "export-agentevals" and trajectory.ignored:
            print("agentwrench gate: warning: ignored events: " + json.dumps(trajectory.ignored, ensure_ascii=True), file=sys.stderr)
        text = json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        losses = trajectory.losses if args.action == "normalize" else trajectory.export_losses
        outputs = [(args.output, text)] if args.output else []
        if args.sidecar:
            primary_bytes = text.encode("utf-8")
            if len(primary_bytes) > sidecar_integrity.MAX_OUTPUT_BYTES:
                raise InputError("primary output exceeds 64 MiB")
            original = trajectory.original_jsonl
            sidecar = {"schema_version": 1, "source": trajectory.source,
                       "target": "normalize" if args.action == "normalize" else "agentevals",
                       "original": {"encoding": "utf-8", "jsonl": original,
                                    "sha256": hashlib.sha256(original.encode("utf-8")).hexdigest()},
                       "output_sha256": hashlib.sha256(primary_bytes).hexdigest(),
                       "losses": losses}
            sidecar_text = json.dumps(sidecar, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
            if len(sidecar_text.encode("utf-8")) > sidecar_integrity.MAX_SIDECAR_BYTES:
                raise InputError("sidecar exceeds 64 MiB")
            outputs.append((args.sidecar, sidecar_text))
        write_new_reports(outputs)
        if args.action == "export-agentevals" and losses:
            print("agentwrench gate: warning: loss accounting: " + json.dumps(losses, ensure_ascii=True), file=sys.stderr)
            if not args.sidecar:
                print("agentwrench gate: use --sidecar PATH to preserve the original log", file=sys.stderr)
        if not args.output:
            print(text, end="")
        return 0
    except (InputError, OSError, UnicodeError, RecursionError) as exc:
        print(_console_text(f"agentwrench gate: {exc}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
