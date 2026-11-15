"""Section 1: configured MCP servers (Claude Code, Codex) and the opt-in ``--probe``.

Configs are parsed and validated with :mod:`agentwrench.mcpconfig`; discovery and
token estimates come from :mod:`agentwrench.slim`. Displayed commands and URLs are
redacted; environment variable and header *values* are never retained for output.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..mcpconfig.core import SECRET_KEY, TOKEN, redacted, to_ir
from ..mcpconfig.formats import ConfigError, Document, loads, plain
from ..slim import DiscoveryError, InputError, plan
from ..slim import measure as slim_measure
from ..slim.discover import PROTOCOL_VERSION as SLIM_PROTOCOL_VERSION
from ..slim.discover import run_discover
from .common import CLIENT_LABELS, Finding, Locations, quote, show_path

# Every published MCP revision; slim's own default accepts only its pinned one.
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
# Default `slim plan` target: tool definitions rarely need more than this per tool.
TOKENS_PER_TOOL_BUDGET = 200
MIN_SAVING_TOKENS = 500
MAX_CONCURRENT_PROBES = 8
MAX_CLAUDE_JSON_BYTES = 64 * 1024 * 1024
STDERR_TAIL_BYTES = 8192   # keep only the end of a probed server's stderr, in memory
STDERR_LINE_CHARS = 160
EXITED = "server exited before listing its tools"
SCOPE_RANK = {"global": 0, "project": 1, "local": 2}  # Claude: local > project > user
_ARG_SECRET = re.compile(r"(auth|(^|[-_])key($|[-_=])|pat($|[-_=]))", re.I)
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_OPAQUE = re.compile(r"[A-Za-z0-9_+=.~-]{24,}")
_URLISH = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass
class Launch:
    """Private launch spec for --probe; never serialized or printed."""
    command: list[str]
    env: dict[str, str]
    cwd: str
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] | None = None

    def key(self) -> str:
        return json.dumps([self.command, sorted(self.env.items()), self.cwd])


@dataclass
class Server:
    client: str
    scope: str
    source: str
    name: str
    transport: str = "unknown"
    enabled: bool = True
    status: str = "listed"  # listed|measured|failed|remote|disabled|shadowed|invalid
    shadowed_by: str | None = None
    command: list[str] | None = None
    url: str | None = None
    env_keys: list[str] = field(default_factory=list)
    header_keys: list[str] = field(default_factory=list)
    error: str | None = None
    tools: int | None = None
    bytes: int | None = None
    tokens: int | None = None
    top_tools: list[dict] = field(default_factory=list)
    slim: dict | None = None
    duplicate_of: str | None = None
    launch: Launch | None = field(default=None, repr=False)
    # Set by --probe when the server exited before listing its tools: exit code or signal
    # and its last non-empty stderr line (redacted, truncated).
    exit: dict | None = None
    # Negotiated MCP revision from --probe; used for suggested commands, not serialized.
    protocol_version: str | None = field(default=None, repr=False)
    # Same command, cwd and tool definitions => same server, even if env values differ.
    identity: str | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.status not in ("disabled", "shadowed", "invalid")

    def label(self) -> str:
        return f"{CLIENT_LABELS[self.client]} {self.scope} {self.name}"

    def to_dict(self) -> dict:
        return {
            "client": self.client, "scope": self.scope, "source": self.source, "name": self.name,
            "transport": self.transport, "enabled": self.enabled, "status": self.status,
            "shadowed_by": self.shadowed_by, "command": self.command, "url": self.url,
            "env_keys": self.env_keys, "header_keys": self.header_keys, "error": self.error,
            "tools": self.tools, "bytes": self.bytes, "estimated_tokens": self.tokens,
            "top_tools": self.top_tools, "slim": self.slim, "duplicate_of": self.duplicate_of,
            "exit": self.exit,
        }

    def failure_detail(self) -> str:
        """``error`` plus the exit status, e.g. ``server exited ... (exit status 1)``."""
        text = self.error or ""
        if self.exit is not None:
            if self.exit.get("signal"):
                text += f" (killed by {self.exit['signal']})"
            elif self.exit.get("code") is not None:
                text += f" (exit status {self.exit['code']})"
        return text


# --------------------------------------------------------------------------- display

def _opaque(word: str) -> bool:
    """A long random-looking word (API key, embedded token) rather than a name or path."""
    if not _OPAQUE.fullmatch(word) or not any(c.isdigit() for c in word):
        return False
    hexish = re.fullmatch(r"[0-9a-fA-F-]+", word) is not None
    return hexish or (any(c.islower() for c in word) and any(c.isupper() for c in word))


def _redact_args(args: list[str]) -> list[str]:
    out: list[str] = []
    hide_next = False
    for arg in args:
        if hide_next:
            out.append("[REDACTED]")
            hide_next = False
        elif arg == "[REDACTED]" or TOKEN.search(arg) or _opaque(arg):
            out.append("[REDACTED]")
        elif _URLISH.match(arg):
            out.append(display_url(arg))
        elif arg.startswith("-") and (SECRET_KEY.search(arg) or _ARG_SECRET.search(arg.lstrip("-"))):
            if "=" in arg:
                out.append(arg.split("=", 1)[0] + "=[REDACTED]")
            else:
                out.append(arg)
                hide_next = True
        else:
            out.append(arg)
    return out


def display_command(raw: dict) -> list[str]:
    safe = redacted({"command": raw.get("command", ""), "args": list(raw.get("args") or [])})
    words = [str(safe["command"]), *map(str, safe["args"])]
    words = [words[0], *_redact_args(words[1:])] if TOKEN.search(words[0]) is None else ["[REDACTED]"]
    home = str(Path.home())
    return [("~" + w[len(home):]) if w == home or w.startswith(home + "/") else w for w in words]


def display_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[REDACTED]"
    if not parts.scheme or not parts.hostname:
        return "[REDACTED]" if TOKEN.search(url) else url.split("?", 1)[0]
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    path = "/".join("[REDACTED]" if TOKEN.search(seg) or _opaque(seg) else seg for seg in parts.path.split("/"))
    return f"{parts.scheme}://{parts.hostname}{port}{path}"


def redact_text(line: str, secrets: list[str] = ()) -> str:
    """Mask secrets in one line of free text (server stderr), word by word like commands.

    Configured env values are removed verbatim first; then tokens, long opaque words,
    ``secret=value`` / ``--token value`` pairs and URL credentials/queries are masked.
    """
    for value in sorted((v for v in secrets if len(v) >= 6), key=len, reverse=True):
        line = line.replace(value, "[REDACTED]")
    out: list[str] = []
    hide_next = False
    for word in line.split():
        bare = word.strip("'\"()[]{},;")
        if hide_next:
            out.append("[REDACTED]")
            hide_next = False
        elif bare.lower() == "bearer" or bare.lower() == "basic":
            out.append(word)
            hide_next = True
        elif TOKEN.search(word) or _opaque(bare):
            out.append("[REDACTED]")
        elif _URLISH.search(bare):
            out.append(display_url(bare[_URLISH.search(bare).start():]))
        elif re.match(r"[-\w.]*[=:]", bare) and SECRET_KEY.search(re.split(r"[=:]", bare, maxsplit=1)[0]):
            key, sep, value = re.split(r"([=:])", bare, maxsplit=1)
            if value:
                out.append(f"{key}{sep}[REDACTED]")
            else:
                out.append(word)
                hide_next = True
        elif bare.startswith("-") and (SECRET_KEY.search(bare) or _ARG_SECRET.search(bare.lstrip("-"))):
            out.append(word)
            hide_next = True
        else:
            out.append(word)
    return " ".join(out)


# --------------------------------------------------------------------------- configs

def _read_json_config(path: Path, limit: int) -> Any:
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ConfigError(f"file exceeds {limit} bytes")
    try:
        return plain(json.loads(data.decode("utf-8")))
    except (ValueError, RecursionError):
        raise ConfigError("invalid JSON") from None


def _expand(value: str) -> str:
    """Claude Code ``${VAR}`` / ``${VAR:-default}`` expansion (probe only, never displayed)."""
    return _ENV_REF.sub(lambda m: os.environ.get(m[1], m[2] if m[2] is not None else m[0]), value)


def _servers_from(fmt: str, servers: Any, *, client: str, scope: str, source: str,
                  project: Path, disabled: set[str] = frozenset()) -> list[Server]:
    result: list[Server] = []
    if not isinstance(servers, dict):
        return result
    for name, raw in servers.items():
        entry = Server(client, scope, source, str(name))
        result.append(entry)
        if not isinstance(raw, dict):
            entry.status, entry.error = "invalid", "server entry must be an object"
            continue
        try:
            (ir,) = to_ir(Document(fmt, "", {}, {name: raw}))
        except ConfigError as exc:
            entry.status, entry.error = "invalid", str(exc)
            continue
        entry.transport = ir.transport
        entry.env_keys = sorted(map(str, (ir.fields.get("env") or {}).keys()))
        entry.header_keys = sorted(map(str, (ir.fields.get("headers") or {}).keys()))
        if ir.fields.get("enabled") is False or name in disabled:
            entry.enabled = False
            entry.status = "disabled"
        if ir.transport == "stdio":
            entry.command = display_command(raw)
            expand = _expand if fmt == "claude" else (lambda value: value)
            env = {str(k): expand(str(v)) for k, v in (ir.fields.get("env") or {}).items() if v is not None}
            cwd = ir.fields.get("cwd")
            cwd = str(project / expand(cwd)) if cwd else str(project)
            entry.launch = Launch([expand(ir.fields["command"]), *map(expand, ir.fields.get("args") or [])],
                                  env, cwd, ir.fields.get("enabled_tools"), ir.fields.get("disabled_tools"))
        else:
            entry.url = display_url(str(ir.fields.get("url", "")))
            if entry.enabled:
                entry.status = "remote"
    return result


def _string_set(value: Any) -> set[str]:
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def collect_servers(loc: Locations, findings: list[Finding]) -> list[Server]:
    servers: list[Server] = []
    project = loc.project

    def config_error(client: str, path: Path, exc: Exception) -> None:
        detail = str(exc) if isinstance(exc, ConfigError) else type(exc).__name__
        findings.append(Finding("error", "mcp", client,
                                f"cannot read {show_path(path)}: {detail}; its MCP servers were skipped",
                                kind="config_unreadable"))

    # Claude Code: ~/.claude.json (user + per-project "local" scope) and <project>/.mcp.json.
    claude_data: Any = {}
    if loc.claude_json.is_file():
        try:
            claude_data = _read_json_config(loc.claude_json, MAX_CLAUDE_JSON_BYTES)
        except (OSError, ConfigError) as exc:
            config_error("claude-code", loc.claude_json, exc)
    if not isinstance(claude_data, dict):
        claude_data = {}
    projects = claude_data.get("projects") if isinstance(claude_data.get("projects"), dict) else {}
    keys = dict.fromkeys([str(project), os.path.realpath(project)])
    local = next((projects[k] for k in keys if isinstance(projects.get(k), dict)), {})
    disabled_any = _string_set(local.get("disabledMcpServers"))
    disabled_json = _string_set(local.get("disabledMcpjsonServers")) | disabled_any
    label = show_path(loc.claude_json)
    servers += _servers_from("claude", claude_data.get("mcpServers"), client="claude-code", scope="global",
                             source=label, project=project, disabled=disabled_any)
    servers += _servers_from("claude", local.get("mcpServers"), client="claude-code", scope="local",
                             source=f"{label} (projects entry)", project=project, disabled=disabled_any)
    mcp_json = project / ".mcp.json"
    if mcp_json.is_file():
        try:
            document = loads(mcp_json.read_text(encoding="utf-8"), "claude")
            servers += _servers_from("claude", document.servers, client="claude-code", scope="project",
                                     source=show_path(mcp_json), project=project, disabled=disabled_json)
        except (OSError, UnicodeError, ConfigError) as exc:
            config_error("claude-code", mcp_json, exc)

    # Codex: $CODEX_HOME/config.toml and the project's .codex/config.toml.
    for scope, path in (("global", loc.codex_home / "config.toml"), ("project", project / ".codex" / "config.toml")):
        if not path.is_file():
            continue
        try:
            document = loads(path.read_text(encoding="utf-8"), "codex")
        except (OSError, UnicodeError, ConfigError) as exc:
            config_error("codex", path, exc)
            continue
        servers += _servers_from("codex", document.servers, client="codex", scope=scope,
                                 source=show_path(path), project=project)

    # Same name in several scopes of one client: the most specific scope wins.
    for client in CLIENT_LABELS:
        by_name: dict[str, list[Server]] = {}
        for entry in servers:
            if entry.client == client and entry.status != "invalid":
                by_name.setdefault(entry.name, []).append(entry)
        for group in by_name.values():
            winner = max(group, key=lambda e: SCOPE_RANK[e.scope])
            for entry in group:
                if entry is not winner:
                    entry.status, entry.shadowed_by = "shadowed", winner.scope
    for entry in servers:
        if entry.status == "invalid":
            findings.append(Finding("error", "mcp", entry.client,
                                    f"{entry.label()}: invalid config ({entry.error}); {CLIENT_LABELS[entry.client]} "
                                    "may refuse to start it"))
    return servers


# --------------------------------------------------------------------------- probe

def _short_error(message: str, timeout: float) -> str:
    if message in ("discovery request timed out", "discovery duration limit exceeded"):
        return f"timed out after {timeout:g}s"
    if message == "cannot start server for discovery":
        return "cannot start (command not found or not executable)"
    if message in ("discovery server exited before completion", "discovery server exited unsuccessfully",
                   "discovery server stdout closed before completion"):
        return EXITED
    if message.startswith("discovery requires tools capability"):
        return "no tools capability or unsupported protocol version"
    return message.removeprefix("discovery ")


class _StderrTail:
    """Drain a probed server's stderr through a pipe, keeping only its last bytes in memory."""

    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self.data = b""
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            while chunk := os.read(self.read_fd, 65536):
                self.data = (self.data + chunk)[-STDERR_TAIL_BYTES:]
        except OSError:
            pass
        finally:
            os.close(self.read_fd)

    def close_writer(self) -> None:
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1

    async def last_line(self, secrets: list[str]) -> str | None:
        """Last non-empty line once the pipe is closed (a lingering grandchild may hold it)."""
        self.close_writer()
        await asyncio.get_running_loop().run_in_executor(None, self.thread.join, 0.5)
        lines = [line.strip() for line in self.data.decode("utf-8", "replace").splitlines()]
        last = next((line for line in reversed(lines) if line), None)
        if last is None:
            return None
        from .report import clean  # local import: report imports this module
        text = redact_text(clean(last), secrets)
        return text if len(text) <= STDERR_LINE_CHARS else text[:STDERR_LINE_CHARS - 3] + "..."


async def _discover(launch: Launch, timeout: float, semaphore: asyncio.Semaphore) -> dict:
    ms = max(1, int(timeout * 1000))
    versions: list[str] = []
    exits: list[tuple[int | None, str | None]] = []
    async with semaphore:
        tail = _StderrTail()
        try:
            snapshot = await run_discover(
                launch.command, {}, server_env=[f"{k}={v}" for k, v in launch.env.items()] or None,
                server_cwd=launch.cwd, request_timeout_ms=ms, discovery_timeout_ms=ms,
                protocol_versions=PROTOCOL_VERSIONS, server_stderr=tail.write_fd,
                on_protocol_version=versions.append, on_exit=lambda code, sig: exits.append((code, sig)))
        except (DiscoveryError, InputError) as exc:
            error = _short_error(str(exc), timeout)
            if error != EXITED:
                tail.close_writer()
                return {"error": error}
            code, sig = exits[0] if exits else (None, None)
            return {"error": error, "exit": {"code": code, "signal": sig,
                                             "stderr": await tail.last_line(list(launch.env.values()))}}
        except Exception as exc:  # never let one server abort the scan
            tail.close_writer()
            return {"error": f"discovery failed ({type(exc).__name__})"}
        tail.close_writer()
    return {"tools": json.loads(snapshot)["tools"], "protocol_version": versions[0] if versions else None}


async def _discover_all(launches: dict[str, Launch], timeout: float) -> dict[str, dict]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
    keys = list(launches)
    results = await asyncio.gather(*(_discover(launches[k], timeout, semaphore) for k in keys))
    return dict(zip(keys, results))


def _slim_suggestion(tools: list[dict], before: int) -> dict | None:
    budget = max(1, TOKENS_PER_TOOL_BUDGET * len(tools))
    if before <= budget:
        return None
    result = plan(tools, budget)
    after = result["after"]["estimatedTokens"]
    if before - after < MIN_SAVING_TOKENS:
        return None
    return {"budget": budget, "estimated_tokens_after": after, "estimated_tokens_saved": before - after,
            "meets_budget": result["meetsBudget"], "config": result["config"]}


def probe(servers: list[Server], timeout: float) -> float:
    """Discover every active stdio server once (deduplicated); returns elapsed seconds."""
    started = time.monotonic()
    launches = {s.launch.key(): s.launch for s in servers
                if s.active and s.transport == "stdio" and s.launch is not None}
    if not launches:
        return 0.0
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        results = asyncio.run(_discover_all(launches, timeout))
    finally:
        for sig, handler in saved.items():  # concurrent discoveries restore handlers out of order
            if handler is not None:
                signal.signal(sig, handler)
    first: dict[str, Server] = {}
    for entry in servers:
        if entry.launch is None or entry.launch.key() not in results or not entry.active:
            continue
        outcome = results[entry.launch.key()]
        if "error" in outcome:
            entry.status, entry.error, entry.exit = "failed", outcome["error"], outcome.get("exit")
            continue
        tools = outcome["tools"]
        entry.protocol_version = outcome.get("protocol_version")
        if entry.launch.enabled_tools is not None:
            tools = [t for t in tools if t["name"] in entry.launch.enabled_tools]
        if entry.launch.disabled_tools:
            tools = [t for t in tools if t["name"] not in entry.launch.disabled_tools]
        measured = slim_measure(tools)
        entry.status = "measured"
        entry.tools, entry.bytes, entry.tokens = measured["tools"], measured["bytes"], measured["estimatedTokens"]
        entry.top_tools = [{"name": t["name"], "estimated_tokens": t["estimatedTokens"]} for t in measured["top"][:3]]
        entry.slim = _slim_suggestion(tools, entry.tokens)
        # Identical command, cwd and tool definitions are one server for display and fixes,
        # even when the clients pass it different env (e.g. Claude ${VAR} vs a Codex literal).
        key = entry.identity = json.dumps([entry.launch.command, entry.launch.cwd, tools], sort_keys=True)
        if key in first:
            entry.duplicate_of = first[key].label()
        else:
            first[key] = entry
    return time.monotonic() - started


def slim_commands(entry: Server, project: Path) -> list[str]:
    """The two `agentwrench slim` steps that reproduce a server's suggestion."""
    catalogue = re.sub(r"[^A-Za-z0-9_.-]+", "-", entry.name).strip("-.") or "server"
    catalogue += ".tools.json"
    words = ["agentwrench", "slim", "discover", "--output", catalogue]
    if os.path.abspath(project) != os.path.abspath(os.getcwd()):
        words += ["--server-cwd", quote(show_path(project))]
    for key in entry.env_keys:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            words += ["--server-env", f'{key}="${key}"']
    if entry.protocol_version and entry.protocol_version != SLIM_PROTOCOL_VERSION:
        # slim discover is strict by default; the probed server negotiated an older revision.
        words += ["--accept-protocol-version", entry.protocol_version]
    words += ["--", *(quote(w) for w in entry.command or [])]
    budget = entry.slim["budget"] if entry.slim else 0
    return [" ".join(words), f"agentwrench slim plan {catalogue} --budget {budget}"]


def mcp_findings(servers: list[Server], project: Path, probed: bool) -> list[Finding]:
    findings: list[Finding] = []
    if not probed:
        return findings
    failed: set[str] = set()
    for entry in servers:
        if entry.status == "failed":
            if entry.launch is not None and entry.launch.key() in failed:
                continue
            failed.add(entry.launch.key() if entry.launch is not None else entry.label())
            message = f"{entry.label()}: failed to start for discovery: {entry.failure_detail()}"
            if entry.exit is not None and entry.exit.get("stderr"):
                message += f"; last stderr line: {entry.exit['stderr']}"
            findings.append(Finding("error", "mcp", entry.client, message))
        elif entry.status == "measured" and entry.slim and entry.duplicate_of is None:
            saved = entry.slim["estimated_tokens_saved"]
            clients = sorted({CLIENT_LABELS[s.client] for s in servers
                              if s.status == "measured" and s.identity == entry.identity})
            findings.append(Finding(
                "warning", "mcp", entry.client,
                f"trim '{entry.name}' tool definitions ({' + '.join(clients)}): "
                f"{entry.tokens:,} -> ~{entry.slim['estimated_tokens_after']:,} tokens",
                slim_commands(entry, project), saved))
    return findings
