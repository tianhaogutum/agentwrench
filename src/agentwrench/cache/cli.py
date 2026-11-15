"""``agentwrench cache``: offline LLM request prefix diagnostics (port of prefix-drift)."""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from .._text import plural
from . import __version__
from .analyze import CLASSES, analyze
from .canonicalize import RULES, canonicalize, write_capture
from .capture import FORMATS, parse_capture, read_capture
from .errors import CacheError, FsError
from .jscompat import number_to_string, quote, stringify, to_fixed, well_formed
from .sessions import SESSION_LIMITS, analyze_sessions
from .volatile import read_volatile_fields

PROG = "agentwrench cache"

HELP = f"""{PROG} {__version__} — offline LLM request prefix diagnostics
Usage:
  {PROG} analyze CAPTURE [--format FORMAT|jsonl] [--json]
      [--input-format FORMAT (requires --format jsonl)]
      [--fail-on CLASS,...] [--min-stability 0..1]
      [--volatile-fields JSON_FILE] [--policy-view raw|configured]
  {PROG} canonicalize CAPTURE --out NEW_FILE [--rules RULE,...] [--format FORMAT]
  {PROG} compare BEFORE AFTER [--json] [--format FORMAT]
      [--volatile-fields JSON_FILE]
  {PROG} sessions CAPTURE... [--json] [--format FORMAT]
      [--fail-on CLASS,...] [--min-stability 0..1]
      [--volatile-fields JSON_FILE] [--policy-view raw|configured]
Formats: {", ".join(FORMATS)}
Analyze --format jsonl: ordered summary/pair records, schemaVersion 1;
--input-format selects capture dialect (default auto), conflicts with --json.
--input-format is analyze-only and requires --format jsonl. Existing formats unchanged.
Classes: {", ".join(CLASSES)}, usage_break
Rules: {", ".join(RULES)} (both by default)
Exit: 0 success, 1 analyze/sessions policy failure, 2 invalid input/options/I/O.
No network calls or provider replay. --out must not already exist.
Sessions: 1..{SESSION_LIMITS.files} independent files, {SESSION_LIMITS.records} total requests; no cross-file pairs.
Rank earliest breaking paths by summed unshared bytes, pair count, then pointer.
Each ranked path lists contributing files with their pair, byte and class totals.
Per-file raw/configured class counts include zero-loss and append-only pairs.
Session CI gates pooled pair stability and observed classes; usage breaks stay raw.
Volatile fields: JSON array of exact request-body scalar pointers. Reports raw and
configured stability without changing captures. CI uses raw unless explicitly selected.
"""

_ALLOWED = {
    "analyze": ("format", "input-format", "json", "fail-on", "min-stability", "volatile-fields", "policy-view"),
    "canonicalize": ("format", "out", "rules"),
    "compare": ("format", "json", "volatile-fields"),
    "sessions": ("format", "json", "fail-on", "min-stability", "volatile-fields", "policy-view"),
}
_STABILITY = re.compile(r"0(?:\.[0-9]+)?|1(?:\.0+)?")


@dataclass
class Options:
    command: str = ""
    paths: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    format: str = "auto"
    json: bool = False
    output_format: str | None = None
    fail_on: list[str] | None = None
    min_stability: float | None = None
    rules: list[str] | None = None
    help: bool = False
    version: bool = False


def parse_options(argv: list[str]) -> Options:
    """Parse arguments exactly like the Node tool (no abbreviations, no ``=`` forms)."""
    args = list(argv)
    command = args.pop(0) if args else None
    if command in ("--help", "-h", None):
        return Options(help=True)
    if command == "--version":
        return Options(version=True)
    allowed = _ALLOWED.get(command)
    if allowed is None:
        raise CacheError("unknown command; use --help")
    opts = Options(command=command)
    values = opts.values
    positional_only = False
    while args:
        arg = args.pop(0)
        if arg == "--" and not positional_only:
            positional_only = True
            continue
        if not positional_only and arg.startswith("-"):
            name = arg[2:]
            if not arg.startswith("--") or name not in allowed:
                raise CacheError("unknown option; use --help")
            if name in values or (name == "json" and opts.json):
                raise CacheError(f"duplicate --{name}")
            if name == "json":
                opts.json = True
            else:
                if not args or args[0].startswith("--"):
                    raise CacheError(f"missing value for --{name}")
                values[name] = args.pop(0)
        else:
            opts.paths.append(arg)
    count = len(opts.paths)
    if command == "sessions":
        wrong = count < 1 or count > SESSION_LIMITS.files
    else:
        wrong = count != (2 if command == "compare" else 1)
    if wrong:
        raise CacheError("wrong number of capture paths; use --help")
    opts.format = values.get("format", "auto")
    if "input-format" in values:
        if values["input-format"] not in FORMATS:
            raise CacheError("unknown --input-format")
        if opts.format != "jsonl":
            raise CacheError("--input-format requires --format jsonl")
    if command == "analyze" and opts.format == "jsonl":
        if opts.json:
            raise CacheError("--format jsonl conflicts with --json")
        opts.output_format = "jsonl"
        opts.format = values.get("input-format", "auto")
    if opts.format not in FORMATS:
        raise CacheError("unknown --format")
    policy_view = values.get("policy-view")
    if policy_view is not None and policy_view not in ("raw", "configured"):
        raise CacheError("--policy-view must be raw or configured")
    if policy_view == "configured" and "volatile-fields" not in values:
        raise CacheError("--policy-view configured requires --volatile-fields")
    if command == "canonicalize" and not values.get("out"):
        raise CacheError("--out is required")
    if "fail-on" in values:
        opts.fail_on = values["fail-on"].split(",")
        if any(c not in (*CLASSES, "usage_break") for c in opts.fail_on):
            raise CacheError("unknown --fail-on class")
    if "min-stability" in values:
        if not _STABILITY.fullmatch(values["min-stability"]):
            raise CacheError("--min-stability must be in 0..1")
        opts.min_stability = float(values["min-stability"])
    if "rules" in values:
        opts.rules = values["rules"].split(",")
        if any(r not in RULES for r in opts.rules) or len(set(opts.rules)) != len(opts.rules):
            raise CacheError("unknown or duplicate --rules value")
    return opts


def _score(value: float | None) -> str:
    return "n/a" if value is None else to_fixed(value, 6)


def _num(value: float | int) -> str:
    return number_to_string(value)


def _below(stability: float | None, minimum: float | None) -> bool:
    return stability is not None and minimum is not None and stability < minimum


def _pair_line(row: dict[str, Any]) -> str:
    divergence = row["firstDivergence"]["path"] if row["firstDivergence"] else "-"
    return (
        f"{row['from']}->{row['to']}\t{to_fixed(row['stability'] * 100, 2)}%\t"
        f"{row['sharedSegments']}/{row['previousSegments']}\t{row['sharedBytes']}\t"
        f"{math.ceil(row['sharedEstimatedTokens'])}\t{quote(divergence)}\t{row['class']}"
    )


_PAIR_HEADER = "pair\tshared\tsegments\tbytes\test.tokens\tfirst divergence\tclass"


def _session_ranking(out: list[str], report: dict[str, Any], label: str) -> None:
    if label:
        out.append(f"{label} ranked paths:")
    out.append("rank\tunshared bytes\tpairs\tsessions\tfirst divergence\tclasses\tcontributors")
    for row in report["rankedPaths"]:
        out.append(
            f"{row['rank']}\t{row['unsharedBytes']}\t{row['pairs']}\t{row['sessions']}\t{quote(row['path'])}\t"
            f"{stringify(row['classCounts'])}\t{stringify(row['contributors'])}"
        )
    prefix = f"{label} " if label else ""
    out.append(f"{prefix}stability: {_score(report['stability'])}")
    out.append(f"{prefix}classes: {stringify(report['classCounts'])}")
    out.append(f"breaking pairs: {report['breakingPairs']}; usage breaks: {report['usageBreaks']}")
    out.append(f"captured usage compared pairs: {report['usageComparedPairs']}")


def render_sessions(report: dict[str, Any], policy_view: str | None) -> list[str]:
    out = [
        f"{plural(report['sessionCount'], 'session')}; {plural(report['requests'], 'request')}; "
        f"{plural(report['pairs'], 'within-session pair')}"
    ]
    out.append(
        "session\tformat\tpairs\tstability\traw classes\traw breaking pairs\tcaptured usage breaks\t"
        "captured usage compared pairs"
    )
    for s in report["sessions"]:
        out.append(
            f"{quote(s['path'])}\t{s['format']}\t{s['pairs']}\t{_score(s['stability'])}\t{stringify(s['classCounts'])}\t"
            f"{s['breakingPairs']}\t{s['usageBreaks']}\t{s['usageComparedPairs']}"
        )
    configured = report.get("configured")
    _session_ranking(out, report, "raw" if configured else "")
    if configured:
        out.append(
            "session\tconfigured stability\tconfigured classes\tvolatile fields\tconfigured breaking pairs\t"
            "captured usage breaks\tcaptured usage compared pairs"
        )
        for s in report["sessions"]:
            c = s["configured"]
            out.append(
                f"{quote(s['path'])}\t{_score(c['stability'])}\t{stringify(c['classCounts'])}\t"
                f"{stringify(s['volatility']['fields'])}\t{c['breakingPairs']}\t{c['usageBreaks']}\t"
                f"{c['usageComparedPairs']}"
            )
        _session_ranking(out, configured, "configured")
        out.append(report["volatility"]["note"])
        out.append(f"policy view: {policy_view or 'raw'}")
    out.append(report["note"])
    for s in report["sessions"]:
        if s["ignoredFields"]:
            out.append(f"unmodeled fields in {quote(s['path'])}: {stringify(s['ignoredFields'])}")
    return out


def render_analysis_jsonl(report: dict[str, Any]) -> list[str]:
    raw = {k: v for k, v in report.items() if k not in ("rows", "configured", "volatility")}
    summary: dict[str, Any] = {"schemaVersion": 1, "type": "summary", "raw": raw}
    configured = report.get("configured")
    if configured:
        summary["configured"] = {k: v for k, v in configured.items() if k != "rows"}
        summary["volatility"] = report["volatility"]
    out = [stringify(summary)]
    for index, row in enumerate(report["rows"]):
        pair: dict[str, Any] = {"schemaVersion": 1, "type": "pair", "pairIndex": index, "raw": row}
        if configured:
            pair["configured"] = configured["rows"][index]
        out.append(stringify(pair))
    return out


def render_analysis(report: dict[str, Any]) -> list[str]:
    configured = report.get("configured")
    out = [f"format: {report['format']}; {plural(report['requests'], 'request')}"]
    if not report["pairs"]:
        out.append("nothing to compare")
    else:
        out.append(_PAIR_HEADER)
        for row in report["rows"]:
            # JSON-encode pointers so control characters from untrusted keys cannot affect a terminal.
            out.append(_pair_line(row))
            if row.get("differingSpan"):
                out.append(f"  differing span: {stringify(row['differingSpan'])}")
            if row.get("reason"):
                out.append(f"  {row['reason']}")
            if row.get("volatileChanges"):
                out.append(f"  volatile fields: {stringify(row['volatileChanges'])}")
            usage = row["usage"]
            if usage:
                status = "usage_break" if usage["usageBreak"] else "usage_chain_ok"
                out.append(
                    f"  {status}: expected={_num(usage['expected'])} actual={_num(usage['actual'])} "
                    f"delta={_num(usage['delta'])}; correlated={row['class']}; {usage['basis']}"
                )
            if usage is None:
                out.append(f"  captured usage evidence: {quote(row['usageNote'])}")
    out.append(f"{'raw stability' if configured else 'stability'}: {_score(report['stability'])}")
    out.append(f"{'raw classes' if configured else 'classes'}: {stringify(report['classCounts'])}")
    out.append(f"captured usage compared pairs: {report['usageComparedPairs']}")
    out.append(f"captured usage breaks: {report['usageBreaks']}")
    out.append("UTF-8 serialized bytes / 4 is an estimate, not provider tokens or measured savings.")
    if report["ignoredFields"]:
        out.append(f"unmodeled request fields: {stringify(report['ignoredFields'])}")
    if configured:
        out.append(f"volatile allowlist: {stringify(report['volatility']['fields'])}")
        out.append("configured analysis (scalar values masked):")
        if configured["pairs"]:
            out.append(_PAIR_HEADER)
        for row in configured["rows"]:
            out.append(_pair_line(row))
            if row.get("differingSpan"):
                out.append(f"  differing span: {stringify(row['differingSpan'])}")
            if row.get("reason"):
                out.append(f"  reason: {quote(row['reason'])}")
        out.append(f"configured stability: {_score(configured['stability'])}")
        out.append(f"configured classes: {stringify(configured['classCounts'])}")
        out.append(f"captured usage compared pairs: {configured['usageComparedPairs']}")
        out.append(f"captured usage breaks: {configured['usageBreaks']}")
        out.append(report["volatility"]["note"])
    return out


def _compare(opts: Options, before: dict[str, Any], volatile_fields: list[Any]) -> list[str]:
    after = analyze(read_capture(opts.paths[1], opts.format), volatile_fields)
    if before["format"] != after["format"] or before["requests"] != after["requests"]:
        raise CacheError("compare requires the same format and request count")
    result: dict[str, Any] = {
        "format": before["format"],
        "requests": before["requests"],
        "pairs": before["pairs"],
        "before": before["stability"],
        "after": after["stability"],
        "improvement": None if before["stability"] is None else after["stability"] - before["stability"],
        "metric": "mean shared serialized-prefix bytes / previous-request bytes",
        "note": "Offline replay of the analyzer only. Historical usage is not a measurement of the variant.",
        "ignoredFields": list(dict.fromkeys([*before["ignoredFields"], *after["ignoredFields"]])),
    }
    if "configured" in before:
        b, a = before["configured"]["stability"], after["configured"]["stability"]
        result["configured"] = {"before": b, "after": a, "improvement": None if b is None else a - b}
        result["volatility"] = {
            "before": before["volatility"]["fields"],
            "after": after["volatility"]["fields"],
            "note": before["volatility"]["note"],
        }
    if opts.json:
        return [stringify(result, 2)]
    configured = result.get("configured")
    out = ["metric\tbefore\tafter\tdelta"]
    out.append(
        f"{'raw stability' if configured else 'stability'}\t{_score(result['before'])}\t"
        f"{_score(result['after'])}\t{_score(result['improvement'])}"
    )
    if configured:
        out.append(
            f"configured stability\t{_score(configured['before'])}\t{_score(configured['after'])}\t"
            f"{_score(configured['improvement'])}"
        )
        out.append(f"volatile fields before: {stringify(result['volatility']['before'])}")
        out.append(f"volatile fields after: {stringify(result['volatility']['after'])}")
        out.append(result["volatility"]["note"])
    out.append(result["note"])
    if result["ignoredFields"]:
        out.append(f"unmodeled request fields: {stringify(result['ignoredFields'])}")
    return out


def _run(argv: list[str]) -> tuple[list[str], int, str | None]:
    """Output lines, exit status and an optional stderr reason for a policy failure."""
    opts = parse_options(argv)
    if opts.help:
        return [HELP], 0, None
    if opts.version:
        return [__version__], 0, None
    policy_view = opts.values.get("policy-view")
    fields_path = opts.values.get("volatile-fields")
    volatile_fields = [] if fields_path is None else read_volatile_fields(fields_path)
    if policy_view == "configured" and not volatile_fields:
        raise CacheError("--policy-view configured requires a nonempty volatile fields allowlist")

    if opts.command == "sessions":
        report = analyze_sessions(opts.paths, opts.format, volatile_fields)
        out = [stringify(report, 2)] if opts.json else render_sessions(report, policy_view)
        policy = report["configured"] if policy_view == "configured" else report
        counts = {
            c: report["usageBreaks"] if c == "usage_break" else policy["classCounts"].get(c, 0)
            for c in opts.fail_on or ()
        }
        return (out, *_policy_failure(opts, counts, policy["stability"], policy_view))

    records = read_capture(opts.paths[0], opts.format)
    if opts.command == "canonicalize":
        entries = canonicalize(records, opts.rules if opts.rules is not None else RULES)
        # Validate the exact serialized variant before installing it. No upstream steps.
        parse_capture("\n".join(stringify(e) for e in entries) + "\n", opts.format)
        try:
            write_capture(opts.values["out"], entries)
        except FsError as error:
            if error.code == "EEXIST" and error.syscall == "link":
                raise CacheError("--out already exists (or is a symlink); choose a new file") from None
            raise
        return [f"wrote {plural(len(entries), 'request')}; historical usage preserved, not replayed"], 0, None
    report = analyze(records, volatile_fields)
    if opts.command == "compare":
        return _compare(opts, report, volatile_fields), 0, None
    if opts.output_format == "jsonl":
        out = render_analysis_jsonl(report)
    elif opts.json:
        out = [stringify(report, 2)]
    else:
        out = render_analysis(report)
    if not opts.json and opts.output_format != "jsonl" and "configured" in report:
        out.append(f"policy view: {policy_view or 'raw'}")
    policy = report["configured"] if policy_view == "configured" else report
    counts = {
        c: sum(
            1 for row in policy["rows"]
            if (bool(row["usage"] and row["usage"]["usageBreak"]) if c == "usage_break" else row["class"] == c)
        )
        for c in opts.fail_on or ()
    }
    return (out, *_policy_failure(opts, counts, policy["stability"], policy_view))


def _policy_failure(
    opts: Options, counts: dict[str, int], stability: float | None, policy_view: str | None
) -> tuple[int, str | None]:
    """Exit status and a one-line stderr reason for --fail-on / --min-stability."""
    matched = ", ".join(f"{c} ({plural(n, 'pair')})" for c, n in counts.items() if n)
    reasons = [f"--fail-on matched {matched}"] if matched else []
    if _below(stability, opts.min_stability):
        reasons.append(f"stability {_score(stability)} is below --min-stability {opts.values['min-stability']}")
    if not reasons:
        return 0, None
    view = f" ({policy_view} view)" if policy_view else ""
    return 1, f"{PROG}: policy failed{view}: " + "; ".join(reasons)


def _write(stream: TextIO, text: str) -> None:
    """Write UTF-8 like Node's console, regardless of the locale's encoding."""
    text = well_formed(text)
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        stream.flush()
        return
    stream.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


def main(argv: list[str] | None = None) -> int:
    """Run ``agentwrench cache`` with ``argv`` (excluding the program and group name)."""
    try:
        lines, status, reason = _run(sys.argv[1:] if argv is None else list(argv))
    except Exception as error:  # noqa: BLE001 - like the Node tool, report every failure as exit 2
        message = error.message if isinstance(error, CacheError) else str(error)
        # JSON parse errors never echo input content. Avoid serializing raw requests or secrets.
        _write(sys.stderr, f"{PROG}: {quote(message)}\n")
        return 2
    try:
        _write(sys.stdout, "".join(line + "\n" for line in lines))
    except BrokenPipeError:
        # The reader went away (e.g. `| head`); exit quietly with the computed status.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    if reason:
        # Explain a policy exit 1 on stderr only; stdout (text/JSON/JSONL) is unchanged.
        _write(sys.stderr, reason + "\n")
    return status
