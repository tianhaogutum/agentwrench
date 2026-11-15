"""Command line entry points. Exit 0=success, 1=CI policy failure, 2=input/I/O error."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from .claude import inspect_claude
from . import __version__
from .core import ScopeError, absolute, inspect_codex, load_settings
from .impact import explain_impact, inspect_impact
from .relocation import read_cwd_map
from .snapshot import create_manifest, create_snapshot, diff_artifacts, explain_diff, read_snapshot
from .._text import plural


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="agentwrench agentsmd", description="Offline Codex byte-budget and Claude import audit")
    root.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = root.add_subparsers(dest="command", required=True)
    helps = {"explain": "show which instruction files load, the byte budget, where the cut falls and imports",
             "check": "one-line PASS/FAIL: exit 1 on truncation, dropped files, high usage or import errors",
             "render": "print the exact Codex instruction text that would be loaded",
             "snapshot": "save a JSON snapshot of the effective instructions for a later diff"}
    for command in ("explain", "check", "render", "snapshot"):
        cmd = sub.add_parser(command, help=helps[command], description=helps[command])
        if command == "snapshot":
            cmd.add_argument("--cwd", type=Path, action="append")
            cmd.add_argument("--manifest", action="store_true", help="capture 1-64 explicit working directories")
            cmd.add_argument("--base-dir", type=Path, help="portable manifest cwd identity base")
            cmd.add_argument("--snapshot-version", type=int, choices=(1, 2), default=1,
                             help="artifact schema version; v2 includes exact project cut metadata")
        else:
            cmd.add_argument("--cwd", type=Path, default=Path.cwd())
        cmd.add_argument("--agent", choices=("codex", "claude"), default="codex")
        cmd.add_argument("--codex-home", type=Path)
        cmd.add_argument("--max-bytes", type=int)
        cmd.add_argument("--fallback", nargs="*", metavar="NAME")
        cmd.add_argument("--root-marker", action="append", metavar="NAME")
        cmd.add_argument("--no-root-markers", action="store_true")
        cmd.add_argument("--allow-import-root", type=Path, action="append", default=[])
        if command == "explain":
            cmd.add_argument("--json", action="store_true")
        if command == "check":
            cmd.add_argument("--max-usage", type=float)
    impact = sub.add_parser("impact", help="compare Codex configurations across working directories")
    impact.add_argument("--cwd", type=Path, action="append", required=True,
                        help="working directory to inspect; repeat up to 64 times")
    impact.add_argument("--before-home", type=Path, required=True,
                        help="Codex home containing the baseline config.toml and global instructions")
    impact.add_argument("--after-home", type=Path, required=True,
                        help="Codex home containing the proposed config.toml and global instructions")
    impact.add_argument("--before-max-bytes", type=int, help="replace the baseline project byte budget")
    impact.add_argument("--after-max-bytes", type=int, help="replace the proposed project byte budget")
    impact.add_argument("--fail-on-loss", action="store_true",
                        help="exit 1 only for newly lost instruction bytes/files relative to baseline")
    impact.add_argument("--json", action="store_true")
    diff = sub.add_parser("diff", help="compare saved snapshots; exit 1 only for effective instruction changes")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--include-file-changes", action="store_true",
                      help="include per-cwd file/context details for saved manifests")
    diff.add_argument("--fail-on-loss", action="store_true",
                      help="gate new retention loss in saved v2 artifacts instead of all effective edits")
    diff.add_argument("--cwd-map", type=Path, metavar="FILE",
                      help="explicit JSON old-to-new cwd relocation map for two saved v2 manifests")
    diff.add_argument("--json", action="store_true")
    return root


def explain(report) -> str:
    lines = [f"{report.agent} scope: {report.cwd}", f"root: {report.root or '(none; cwd only)'}",
             "#  file  size  loaded  status  cumulative(project)"]
    for index, row in enumerate(report.entries, 1):
        lines.append(f"{index}  {json.dumps(row['path'], ensure_ascii=True)}  {row['size']}  "
                     f"{row['bytes_loaded']}  {row['status']}  {row['project_cumulative_bytes']}")
    if report.agent == "codex":
        lines.append(f"budget {report.max_bytes} B; project used {report.project_bytes_used} B "
                     f"({report.usage:.1%}); rendered {len(report.text.encode('utf-8'))} B (global/separators excluded from budget)")
    for point in report.truncations:
        lines.append(f"cut {json.dumps(point['path'])} at byte {point['byte_offset']} "
                     f"(line {point['line']}, under {json.dumps(point['nearest_preceding_heading'])})")
        for heading in point["headings_fully_dropped"]:
            lines.append(f"  dropped heading line {heading['line']}: {json.dumps(heading['heading'])}")
    for edge in report.edges:
        lines.append(f"import {json.dumps(edge['source'])}:{edge['line']} -> {json.dumps(edge['target'])} [{edge['status']}]")
    for message in report.warnings:
        lines.append(f"note: {message}")
    for diagnostic in report.diagnostics:
        lines.append(f"error: {json.dumps(diagnostic['path'])}: {json.dumps(diagnostic['error'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "diff":
            result = diff_artifacts(read_snapshot(args.before), read_snapshot(args.after),
                                    include_file_changes=args.include_file_changes, fail_on_loss=args.fail_on_loss,
                                    cwd_map=read_cwd_map(args.cwd_map) if args.cwd_map is not None else None)
            print(json.dumps(result, ensure_ascii=True, indent=2) if args.json else explain_diff(result))
            if args.fail_on_loss:
                return 0 if result["loss_policy"]["passed"] else 1
            return 1 if result["effective_changed"] else 0
        if args.command == "impact":
            impact = inspect_impact(args.cwd, args.before_home, args.after_home,
                                    args.before_max_bytes, args.after_max_bytes,
                                    fail_on_loss=args.fail_on_loss)
            print(json.dumps(impact, ensure_ascii=True, indent=2) if args.json else explain_impact(impact))
            if impact["summary"]["error_count"]:
                return 2
            return 1 if args.fail_on_loss and not impact["loss_policy"]["passed"] else 0
        if args.command == "snapshot":
            if args.manifest:
                if args.base_dir is None or args.codex_home is None:
                    raise ScopeError("manifest requires explicit --base-dir and --codex-home")
                if not args.cwd:
                    raise ScopeError("manifest requires 1 to 64 --cwd arguments")
            else:
                if args.base_dir is not None:
                    raise ScopeError("--base-dir requires --manifest")
                if args.cwd and len(args.cwd) != 1:
                    raise ScopeError("multiple --cwd arguments require --manifest")
                args.cwd = args.cwd[0] if args.cwd else Path.cwd()
        if args.agent == "claude":
            if args.command == "snapshot":
                raise ScopeError("snapshot requires --agent codex")
            if args.command == "render":
                raise ScopeError("Claude render unsupported: docs describe imports, not exact prompt serialization")
            if any((args.codex_home is not None, args.max_bytes is not None, args.fallback is not None,
                    args.root_marker is not None, args.no_root_markers, getattr(args, "max_usage", None) is not None)):
                raise ScopeError("Codex budget/config options cannot be used with --agent claude")
            report = inspect_claude(args.cwd, args.allow_import_root)
        else:
            if args.allow_import_root:
                raise ScopeError("--allow-import-root requires --agent claude")
            if args.no_root_markers and args.root_marker is not None:
                raise ScopeError("--no-root-markers conflicts with --root-marker")
            home = args.codex_home
            if home is None:
                home = Path(os.environ.get("CODEX_HOME", "~/.codex"))
            home = absolute(home)
            markers = [] if args.no_root_markers else args.root_marker
            settings, warnings = load_settings(home, args.max_bytes, args.fallback, markers)
            if args.command == "snapshot":
                result = (create_manifest(args.cwd, args.base_dir, home, settings, warnings,
                                          snapshot_version=args.snapshot_version) if args.manifest
                          else create_snapshot(args.cwd, home, settings, warnings,
                                               snapshot_version=args.snapshot_version))
                print(json.dumps(result, ensure_ascii=True, indent=2))
                return 0
            report = inspect_codex(args.cwd, home, settings, warnings)
        if args.command == "check":
            threshold = 0.8 if args.max_usage is None else args.max_usage
            if not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise ScopeError("--max-usage must be a finite number in [0, 1]")
            if report.diagnostics:
                print(f"FAIL: {plural(len(report.diagnostics), 'file/import error')}; run explain for details")
                return 1
            if report.truncations or any(e["status"] == "dropped-over-budget" for e in report.entries):
                print(f"FAIL: project instructions truncated/dropped ({report.project_bytes_used}/{report.max_bytes} B)")
                return 1
            if report.agent == "codex" and report.usage > threshold:
                print(f"FAIL: project usage {report.usage:.2%} exceeds {threshold:.2%}")
                return 1
            print(f"PASS: {plural(len(report.entries), 'file')}; " + (f"project usage {report.usage:.2%}" if report.agent == "codex" else "imports resolved (docs-derived)"))
            return 0
        if args.command == "render":
            if report.diagnostics:
                raise ScopeError("cannot render an exact instruction string after file errors; run explain")
            sys.stdout.buffer.write(report.text.encode("utf-8"))
        elif args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=True, indent=2))
        else:
            print(explain(report))
        # File/import errors are findings in a complete report (as for `check`): exit 1.
        return 1 if report.diagnostics else 0
    except (ScopeError, OSError, RuntimeError) as exc:
        # I/O exceptions can include hostile paths; JSON escaping prevents terminal controls.
        print("agentwrench agentsmd: " + json.dumps(str(exc), ensure_ascii=True), file=sys.stderr)
        return 2
