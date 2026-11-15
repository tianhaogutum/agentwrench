"""Section 2: instruction files loaded every turn (Codex AGENTS.md chain, Claude CLAUDE.md + imports).

All discovery and budget logic is :mod:`agentwrench.agentsmd`; this module only
summarizes its reports. Instruction text is never included in the output.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..agentsmd.claude import inspect_claude
from ..agentsmd.core import ScopeError, inspect_codex, load_settings
from .common import Finding, Locations, quote, show_path, tokens

# Claude Code warns about large memory files above ~40k characters.
CLAUDE_LARGE_BYTES = 40_000
CODEX_HIGH_USAGE = 0.8


def _reason(error: str) -> str:
    return {"FileNotFoundError": "file not found", "PermissionError": "permission denied"}.get(error, error)


def _rel(loc: Locations, path: str) -> str:
    """Instruction files read best relative to the project they belong to."""
    shown = show_path(path, loc.project)
    return shown if not shown.startswith(("/", "~")) else show_path(path)


def _file_row(row: dict) -> dict:
    return {"path": row["path"], "scope": row["scope"], "bytes": row["size"],
            "loaded_bytes": row["bytes_loaded"], "status": row["status"]}


def _explain_command(loc: Locations, agent: str) -> str:
    words = ["agentwrench", "agentsmd", "explain"]
    if agent == "claude":
        words += ["--agent", "claude"]
    words += ["--cwd", quote(show_path(loc.project))]
    default_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()  # agentsmd's default
    if agent == "codex" and os.path.abspath(loc.codex_home) != os.path.abspath(default_home):
        words += ["--codex-home", quote(show_path(loc.codex_home))]
    return " ".join(words)


def scan_codex(loc: Locations, findings: list[Finding]) -> dict:
    try:
        settings, warnings = load_settings(loc.codex_home)
        report = inspect_codex(loc.project, loc.codex_home, settings, warnings)
    except (ScopeError, OSError) as exc:
        message = str(exc) if isinstance(exc, ScopeError) else type(exc).__name__
        findings.append(Finding("error", "instructions", "codex", f"cannot inspect AGENTS.md chain: {message}",
                                [_explain_command(loc, "codex")]))
        return {"error": message}
    rendered = len(report.text.encode("utf-8"))
    project_rows = [r for r in report.entries if r["scope"] == "project"]
    dropped = sum((r["size"] or 0) - r["bytes_loaded"] for r in project_rows
                  if r["status"] in ("partial", "dropped-over-budget"))
    cuts = [{"path": c["path"], "line": c["line"], "heading": c["nearest_preceding_heading"],
             "dropped_headings": [h["heading"] for h in c["headings_fully_dropped"]]}
            for c in report.truncations]
    over = [r for r in project_rows if r["status"] == "dropped-over-budget"]
    result = {
        "codex_home": str(loc.codex_home), "root": report.root,
        "files": [_file_row(r) for r in report.entries if r["status"] != "unreadable" or r["size"]],
        "project_bytes": sum((r["size"] or 0) for r in project_rows if r["status"] in
                             ("loaded", "partial", "dropped-over-budget")),
        "project_bytes_loaded": report.project_bytes_used, "limit_bytes": settings.max_bytes,
        "usage": report.usage, "rendered_bytes": rendered, "estimated_tokens": tokens(rendered),
        "truncated": bool(report.truncations or over), "dropped_bytes": dropped, "cuts": cuts,
        "errors": [{"path": d["path"], "error": d["error"]} for d in report.diagnostics],
    }
    command = [_explain_command(loc, "codex")]
    if result["truncated"]:
        where = ""
        if cuts:
            cut = cuts[0]
            where = f" (cut at {_rel(loc, cut['path'])}:{cut['line']}"
            headings = list(dict.fromkeys(h for c in cuts for h in c["dropped_headings"]))
            if headings:
                level = min(len(h) - len(h.lstrip("#")) for h in headings)
                major = [h for h in headings if len(h) - len(h.lstrip("#")) == level]
                names = ", ".join(f'"{h.lstrip("#").strip()}"' for h in major[:3])
                more = len(major) - 3
                where += f"; never seen: {names}" + (f" +{more} more" if more > 0 else "")
            where += ")"
        findings.append(Finding("error", "instructions", "codex",
                                f"Codex silently drops {dropped:,} bytes of AGENTS.md over its "
                                f"{settings.max_bytes:,}-byte project_doc_max_bytes{where}", command))
    elif report.max_bytes and report.usage > CODEX_HIGH_USAGE:
        findings.append(Finding("warning", "instructions", "codex",
                                f"AGENTS.md chain uses {report.usage:.0%} of Codex's {settings.max_bytes:,}-byte "
                                "budget; the next addition may be cut", command))
    for diagnostic in report.diagnostics:
        findings.append(Finding("error", "instructions", "codex",
                                f"unreadable instruction file {_rel(loc, diagnostic['path'])}: {diagnostic['error']}",
                                command))
    return result


def scan_claude(loc: Locations, findings: list[Finding]) -> dict:
    command = [_explain_command(loc, "claude")]
    try:
        report = inspect_claude(loc.project, user_dir=loc.claude_dir)
    except (ScopeError, OSError) as exc:
        message = str(exc) if isinstance(exc, ScopeError) else type(exc).__name__
        findings.append(Finding("error", "instructions", "claude-code",
                                f"cannot inspect CLAUDE.md imports: {message}", command))
        return {"error": message}
    loaded = sum(r["bytes_loaded"] for r in report.entries if r["status"] == "loaded")
    result = {
        "files": [_file_row(r) for r in report.entries],
        "imports": len(report.edges), "loaded_bytes": loaded, "estimated_tokens": tokens(loaded),
        "errors": [{"path": d["path"], "error": d["error"]} for d in report.diagnostics],
    }
    if loaded > CLAUDE_LARGE_BYTES:
        findings.append(Finding("warning", "instructions", "claude-code",
                                f"CLAUDE.md files and imports total {loaded:,} bytes (~{tokens(loaded):,} tokens) "
                                f"every session; Claude Code flags memory over {CLAUDE_LARGE_BYTES:,} characters",
                                command))
    broken = [edge for edge in report.edges if edge["status"] == "error"]
    if broken:
        names = list(dict.fromkeys(f"{_rel(loc, edge['target'])} ({_reason(edge.get('error', 'error'))})"
                                   for edge in broken))
        more = f" +{len(names) - 3} more" if len(names) > 3 else ""
        findings.append(Finding("warning", "instructions", "claude-code",
                                f"{len(broken)} CLAUDE.md @import{'s' * (len(broken) != 1)} silently skipped: "
                                f"{', '.join(names[:3])}{more}", command))
    edge_targets = {edge["target"] for edge in report.edges}
    for diagnostic in report.diagnostics:
        if diagnostic["path"] not in edge_targets:
            findings.append(Finding("error", "instructions", "claude-code",
                                    f"{_rel(loc, diagnostic['path'])}: {_reason(diagnostic['error'])}", command))
    return result


def scan_instructions(loc: Locations, findings: list[Finding]) -> dict:
    return {"codex": scan_codex(loc, findings), "claude-code": scan_claude(loc, findings)}
