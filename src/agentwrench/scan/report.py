"""Summary, JSON document (``agentwrench.scan/v1``) and aligned terminal text."""
from __future__ import annotations

import re

from .._text import plural
from . import SCHEMA, __version__
from .common import CLIENT_LABELS, ESTIMATOR, Finding, Locations, show_path
from .mcp import SCOPE_RANK, TOKENS_PER_TOOL_BUDGET, Server

SEVERITY_RANK = {"error": 0, "warning": 1}
COMMAND_WIDTH = 46


def overhead(servers: list[Server], instructions: dict, probed: bool) -> dict:
    clients = {}
    for client in CLIENT_LABELS:
        active = [s for s in servers if s.client == client and s.active]
        measured = [s for s in active if s.status == "measured"]
        mcp = sum(s.tokens or 0 for s in measured)
        instr = instructions.get(client, {}).get("estimated_tokens", 0)
        clients[client] = {
            "fixed_overhead_tokens": mcp + instr,
            "mcp_tokens": mcp if probed else None,
            "instruction_tokens": instr,
            "mcp_servers": len(active),
            "mcp_servers_measured": len(measured),
            "complete": len(measured) == len(active),
        }
    return clients


def top_fixes(findings: list[Finding], limit: int = 3) -> list[Finding]:
    ranked = sorted((f for f in findings if f.command),
                    key=lambda f: (-f.tokens_saved, SEVERITY_RANK[f.severity]))
    seen, result = set(), []
    for finding in ranked:
        key = tuple(finding.command)
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result[:limit]


def build(loc: Locations, *, servers: list[Server], probed: bool, probe_seconds: float | None,
          instructions: dict, sessions: dict, findings: list[Finding], options: dict,
          elapsed: float, fail_over: int | None) -> dict:
    clients = overhead(servers, instructions, probed)
    peak = max(c["fixed_overhead_tokens"] for c in clients.values())
    order = {"mcp": 0, "instructions": 1, "sessions": 2}
    findings.sort(key=lambda f: (order[f.area], SEVERITY_RANK[f.severity], -f.tokens_saved))
    return {
        "schema": SCHEMA,
        "agentwrench_version": __version__,
        "home": str(loc.home),
        "project": str(loc.project),
        "options": options,
        "estimator": ESTIMATOR,
        "elapsed_ms": round(elapsed * 1000),
        "summary": {
            "clients": clients,
            "max_fixed_overhead_tokens": peak,
            "problems": len(findings),
            "errors": sum(f.severity == "error" for f in findings),
            "warnings": sum(f.severity == "warning" for f in findings),
            "fail_over": fail_over,
            "over_limit": fail_over is not None and peak > fail_over,
            "top_fixes": [f.to_dict() for f in top_fixes(findings)],
        },
        "mcp": {
            "probed": probed,
            "elapsed_ms": None if probe_seconds is None else round(probe_seconds * 1000),
            "tokens_per_tool_budget": TOKENS_PER_TOOL_BUDGET,
            "servers": [s.to_dict() for s in servers],
        },
        "instructions": instructions,
        "sessions": sessions,
        "findings": [f.to_dict() for f in findings],
    }


# --------------------------------------------------------------------------- text

class Style:
    CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}

    def __init__(self, color: bool) -> None:
        self.color = color

    def __getattr__(self, name: str):
        code = self.CODES[name]
        return (lambda text: f"\x1b[{code}m{text}\x1b[0m") if self.color else (lambda text: text)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(text: object) -> str:
    """Names and paths come from local files: never let them emit terminal controls."""
    return _CONTROL.sub("?", str(text))


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 3] + "..."


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def width(text: str) -> int:
    return len(_ANSI.sub("", text))


def table(rows: list[list[str]], right: set[int], indent: str = "  ") -> list[str]:
    widths = [max(width(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            pad = " " * (widths[i] - width(cell))
            cells.append(pad + cell if i in right else cell + pad)
        lines.append((indent + "  ".join(cells)).rstrip())
    return lines


def n(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _seconds(value: float) -> str:
    return f"{value:.2f}s" if value < 10 else f"{value:.0f}s"


class Text:
    def __init__(self, doc: dict, servers: list[Server], findings: list[Finding], style: Style) -> None:
        self.doc, self.servers, self.findings, self.s = doc, servers, findings, style
        self.fixes = top_fixes(findings)
        self.lines: list[str] = []

    def out(self, line: str = "") -> None:
        self.lines.append(line)

    def finding(self, finding: Finding, indent: str = "  ") -> None:
        mark = self.s.red("x") if finding.severity == "error" else self.s.yellow("!")
        ref = ""
        if finding in self.fixes:
            ref = " " + self.s.dim(f"(fix #{self.fixes.index(finding) + 1} below)")
        self.out(f"{indent}{mark} {clean(finding.message)}{ref}")
        if not ref:
            for command in finding.command:
                self.out(f"{indent}    {self.s.cyan(clean(command))}")

    def area_findings(self, area: str) -> None:
        for finding in self.findings:
            if finding.area == area:
                self.finding(finding)

    def header(self) -> None:
        s = self.s
        self.out(s.bold("agentwrench scan") + s.dim("  -  context overhead and breakage for Claude Code & Codex "
                                                 "(read-only, offline)"))
        self.out(f"  home     {clean(show_path(self.doc['home']))}")
        self.out(f"  project  {clean(show_path(self.doc['project']))}")

    def mcp(self) -> None:
        s, mcp = self.s, self.doc["mcp"]
        self.out()
        if not self.servers:
            self.out(s.bold("1. MCP tool definitions"))
            self.out("  No MCP servers configured for Claude Code or Codex.")
            return
        if not mcp["probed"]:
            self.out(s.bold("1. MCP servers") + s.dim("  (not started)"))
            rows = [["CLIENT", "SCOPE", "SERVER", "TRANSPORT", "COMMAND / URL"]]
            for e in sorted(self.servers, key=lambda e: (list(CLIENT_LABELS).index(e.client),
                                                         -SCOPE_RANK[e.scope], e.name)):
                target = " ".join(e.command) if e.command else (e.url or "")
                note = {"disabled": "  (disabled)", "invalid": "  (invalid config)"}.get(e.status, "")
                if e.status == "shadowed":
                    note = f"  (overridden by {e.shadowed_by} scope)"
                if e.env_keys and e.status not in ("disabled", "shadowed"):
                    note += f"  env: {', '.join(e.env_keys)}"
                rows.append([CLIENT_LABELS[e.client], e.scope, clean(e.name), clean(fit(e.transport, 16)),
                             clean(fit(target, COMMAND_WIDTH)) + clean(note)])
            header, *body = table(rows, set())
            self.out(s.dim(header))
            for line in body:
                self.out(line)
            stdio = sum(e.transport == "stdio" and e.active for e in self.servers)
            self.out(f"  {plural(stdio, 'stdio server')} not measured. Run with {s.cyan('--probe')} to measure their "
                     "tool-definition tokens")
            self.out("  (starts each configured stdio server locally, lists its tools, then stops it).")
            self.area_findings("mcp")
            return
        measured = [e for e in self.servers if e.status == "measured"]
        took = mcp["elapsed_ms"] / 1000 if mcp["elapsed_ms"] is not None else 0.0
        unique = len({e.launch.key() for e in self.servers if e.launch and e.status in ("measured", "failed")})
        self.out(s.bold("1. MCP tool definitions") + s.dim(f"  (probed {plural(unique, 'server')} in {_seconds(took)})"))
        rows = [["CLIENT", "SCOPE", "SERVER", "TOOLS", "EST. TOKENS", "NOTE"]]
        status_rank = {"measured": 0, "failed": 1, "remote": 2, "listed": 3, "disabled": 4, "shadowed": 5, "invalid": 6}
        for e in sorted(self.servers, key=lambda e: (status_rank[e.status], -(e.tokens or 0),
                                                     list(CLIENT_LABELS).index(e.client), e.name)):
            note = ""
            if e.status == "measured":
                if e.slim and not e.duplicate_of:
                    note = s.yellow(f"slim: -{e.slim['estimated_tokens_saved']:,}")
                elif e.duplicate_of:
                    note = s.dim(f"same server as {clean(e.duplicate_of)}")
            elif e.status == "failed":
                note = s.red(f"failed: {clean(e.error)}")
            elif e.status == "remote":
                note = s.dim(f"remote ({e.transport}), not measured")
            elif e.status == "shadowed":
                note = s.dim(f"overridden by {e.shadowed_by} scope")
            elif e.status == "invalid":
                note = s.red("invalid config")
            else:
                note = s.dim(e.status)
            rows.append([CLIENT_LABELS[e.client], e.scope, clean(e.name), n(e.tools), n(e.tokens), note])
        header, *body = table(rows, {3, 4})
        self.out(s.dim(header))
        for line in body:
            self.out(line)
        tops = sorted(((t["estimated_tokens"], e.name, t["name"]) for e in measured if not e.duplicate_of
                       for t in e.top_tools), key=lambda item: (-item[0], item[1], item[2]))[:3]
        if tops:
            self.out("  Largest tools: " + ", ".join(f"{clean(server)}/{clean(tool)} {tokens:,}"
                                                     for tokens, server, tool in tops))
        self.out(s.dim(f"  slim estimate: `agentwrench slim plan` at {TOKENS_PER_TOOL_BUDGET} tokens/tool "
                       "(strip schema annotations, then shorten descriptions; no tool removed)"))
        self.area_findings("mcp")

    def instructions(self) -> None:
        s, doc = self.s, self.doc["instructions"]
        self.out()
        self.out(s.bold("2. Instructions loaded every turn"))
        codex, claude = doc["codex"], doc["claude-code"]
        rows = []
        if "error" in codex:
            rows.append(["Codex", "AGENTS.md", "-", s.red("not inspected")])
        else:
            files = sum(1 for f in codex["files"] if f["loaded_bytes"])
            usage = f"{codex['project_bytes_loaded']:,} / {codex['limit_bytes']:,} B project budget ({codex['usage']:.0%})"
            if codex["truncated"]:
                usage = s.red(usage + ", truncated")
            rows.append(["Codex", f"AGENTS.md ({plural(files, 'file')})",
                         f"~{codex['estimated_tokens']:,} tokens", usage])
        if "error" in claude:
            rows.append(["Claude Code", "CLAUDE.md", "-", s.red("not inspected")])
        else:
            memory = sum(1 for f in claude["files"] if f["scope"] != "import" and f["loaded_bytes"])
            rows.append(["Claude Code", f"CLAUDE.md ({plural(memory, 'file')} + {plural(claude['imports'], 'import')})",
                         f"~{claude['estimated_tokens']:,} tokens", f"{claude['loaded_bytes']:,} B"])
        for line in table(rows, {2}):
            self.out(line)
        self.area_findings("instructions")
        if not any(f.area == "instructions" for f in self.findings):
            self.out(s.green("  ok") + " no truncation or broken imports")

    def sessions(self) -> None:
        s, doc = self.s, self.doc["sessions"]
        limit = self.doc["options"]["sessions"]
        took = sum(c["elapsed_ms"] for c in doc.values()) / 1000
        self.out()
        timing = f", {_seconds(took)}" if took >= 1 else ""
        if not limit:
            self.out(s.bold("3. Sessions") + s.dim("  (skipped: --sessions 0)"))
            return
        self.out(s.bold("3. Sessions") + s.dim(f"  (tool-call pairing in the {limit} most recent files per client{timing})"))
        rows = []
        for client, c in doc.items():
            if not c["checked"]:
                rows.append([CLIENT_LABELS[client], "no session files", "", clean(show_path(c["root"]))])
                continue
            parts = []
            if c["broken"]:
                parts.append(s.red(f"{c['broken']} broken"))
            if c["live"]:
                parts.append(s.dim(f"{c['live']} in progress (skipped)"))
            if c["errors"]:
                parts.append(s.yellow(f"{c['errors']} unreadable"))
            status = ", ".join(parts) if parts else s.green("all clean")
            rows.append([CLIENT_LABELS[client], f"{c['checked']} checked", status, clean(show_path(c["root"]))])
        for line in table(rows, set()):
            self.out(line)
        self.area_findings("sessions")

    def summary(self) -> None:
        s, summary = self.s, self.doc["summary"]
        self.out()
        self.out(s.bold("Summary"))
        self.out("  Fixed context overhead per turn (estimated tokens, before you type anything):")
        rows = []
        for client, c in summary["clients"].items():
            mcp = c["mcp_tokens"]
            if not c["mcp_servers"]:
                mcp_text = "no MCP servers"
            elif mcp is None:
                mcp_text = f"MCP not measured ({plural(c['mcp_servers'], 'server')}; use --probe)"
            else:
                mcp_text = f"MCP {mcp:,} ({c['mcp_servers_measured']}/{plural(c['mcp_servers'], 'server')} measured)"
            instr = "CLAUDE.md" if client == "claude-code" else "AGENTS.md"
            rows.append([CLIENT_LABELS[client], s.bold(f"{c['fixed_overhead_tokens']:,}"),
                         f"= {mcp_text} + {instr} {c['instruction_tokens']:,}"])
        for line in table(rows, {1}, indent="    "):
            self.out(line)
        problems = summary["problems"]
        if problems:
            self.out(f"  {s.red(str(problems)) if summary['errors'] else s.yellow(str(problems))} "
                     f"{'problem' if problems == 1 else 'problems'}: {plural(summary['errors'], 'error')}, "
                     f"{plural(summary['warnings'], 'warning')}")
        else:
            self.out(f"  {s.green('No problems found.')}")
        if self.fixes:
            self.out("  Top fixes:")
            for index, fix in enumerate(self.fixes, 1):
                gain = f"-{fix.tokens_saved:,} tokens/turn  " if fix.tokens_saved else ""
                self.out(f"    {index}. {s.bold(gain) if gain else ''}{clean(fix.message)}")
                for command in fix.command:
                    self.out(f"         {s.cyan(clean(command))}")
        if summary["fail_over"] is not None:
            verdict = s.red("FAIL") if summary["over_limit"] else s.green("ok")
            self.out(f"  --fail-over {summary['fail_over']:,}: {verdict} "
                     f"(max fixed overhead {summary['max_fixed_overhead_tokens']:,})")
        self.out(s.dim(f"  Estimates: {ESTIMATOR}. Scanned in {_seconds(self.doc['elapsed_ms'] / 1000)}."))

    def render(self) -> str:
        self.header()
        self.mcp()
        self.instructions()
        self.sessions()
        self.summary()
        return "\n".join(self.lines) + "\n"


def render_text(doc: dict, servers: list[Server], findings: list[Finding], color: bool) -> str:
    return Text(doc, servers, findings, Style(color)).render()
