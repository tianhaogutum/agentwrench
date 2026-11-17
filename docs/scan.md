# agentwrench scan

`agentwrench scan` answers two questions in a few seconds: **how many tokens does my
Claude Code / Codex setup spend before I type anything**, and **what in it is broken**.
It reads your MCP server configs, the AGENTS.md / CLAUDE.md files loaded for a project,
and your most recent session files, then names the agentwrench command that fixes each
problem, ordered by tokens saved. If the project holds an `agentwrench handoff` pack,
it also tells you whether that pack has gone stale.

It is read-only and offline. Only `--probe` executes configured code (see
[Privacy and safety](#privacy-and-safety)).

```sh
uvx agentwrench scan                 # this project, your real ~/.claude* and ~/.codex
uvx agentwrench scan --probe         # also start each stdio MCP server to count its tool tokens
agentwrench scan --project ~/src/app --json
agentwrench scan --probe --fail-over 30000   # CI: exit 1 above 30k tokens of fixed overhead
```

## Try the demo

The repository ships a synthetic setup: five MCP servers (two of them shared by
Claude Code and Codex, one remote, one whose binary is missing), an AGENTS.md that is
larger than Codex's budget, a CLAUDE.md with a broken `@import`, and four sessions,
one of which was interrupted mid tool call. Every server is a local Python script
serving fictional tool definitions; nothing touches the network.

```sh
agentwrench scan --home examples/scan/demo-home --project examples/scan/demo-project --probe
```

```text
agentwrench scan  -  context overhead and breakage for Claude Code & Codex (read-only, offline)
  home     examples/scan/demo-home
  project  examples/scan/demo-project

1. MCP tool definitions  (probed 5 servers in 0.06s)
  CLIENT       SCOPE    SERVER         TOOLS  EST. TOKENS  NOTE
  Claude Code  project  issue-tracker     40       22,787  slim: -14,946
  Claude Code  global   github            33       10,425  slim: -4,055
  Codex        global   github            33       10,425  same server as Claude Code global github
  Claude Code  project  playwright        21        2,093
  Codex        global   postgres           3          227
  Claude Code  global   sentry             -            -  failed: cannot start (command not found or not executable)
  Claude Code  global   linear             -            -  remote (http), not measured
  ...
Summary
  Fixed context overhead per turn (estimated tokens, before you type anything):
    Claude Code  44,936  = MCP 35,305 (3/5 servers measured) + CLAUDE.md 9,631
    Codex        18,943  = MCP 10,652 (2/2 servers measured) + AGENTS.md 8,291
  ...
```

The demo paths are relative: servers start with the project directory as their working
directory, exactly as Claude Code and Codex start them.

## Options

| Option | Meaning |
| --- | --- |
| `--project DIR` | Project whose `.mcp.json`, `.codex/config.toml`, AGENTS.md and CLAUDE.md apply. Default: the current directory. |
| `--home DIR` | Use `DIR` instead of `~` for `~/.claude.json`, `~/.claude/` and `~/.codex/` (demos, tests, other users' backups). Without it, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honored. |
| `--probe` | **Executes** every enabled stdio MCP server once to count its tool-definition tokens. |
| `--timeout SECONDS` | Per-server limit for `--probe` (default 10). Servers are probed concurrently. |
| `--sessions N` | Check the N most recently modified session files per client (default 50; `0` skips the section). |
| `--fail-over TOKENS` | Exit 1 when any client's estimated fixed overhead exceeds `TOKENS`. |
| `--json` | Print the [`agentwrench.scan/v1`](#json-output) document instead of text. |
| `--no-color` | No ANSI color. Color is also off when stdout is not a terminal or `NO_COLOR` is set. |

Exit status: **0** the scan completed (also when it found problems), **1** `--fail-over`
was exceeded, **2** usage error or unusable `--project`/`--home`.

## What each section means

All token numbers use the same estimator as `agentwrench slim`: `ceil(UTF-8 bytes / 4)`.
It is not a model tokenizer; treat the numbers as a consistent yardstick for comparing
servers, files and fixes.

### 1. MCP tool definitions

Every enabled MCP server sends the name, description and JSON input schema of each of its
tools to the model on every request. scan lists the servers each client would load:

| Client | Scope | Source |
| --- | --- | --- |
| Claude Code | `global` | `~/.claude.json` → `mcpServers` (Claude's *user* scope) |
| Claude Code | `local` | `~/.claude.json` → `projects["<project>"].mcpServers` (private per project) |
| Claude Code | `project` | `<project>/.mcp.json` |
| Codex | `global` | `$CODEX_HOME/config.toml` → `[mcp_servers.*]` |
| Codex | `project` | `<project>/.codex/config.toml` → `[mcp_servers.*]` |

Server definitions are parsed and validated by the same code as `agentwrench mcp-config`.
When one name is defined in several scopes of a client, the most specific scope wins
(Claude Code: local > project > global) and the others are shown as *overridden*. Servers
disabled with Codex `enabled = false` or Claude Code's per-project `disabledMcpServers` /
`disabledMcpjsonServers` are listed but not counted.

Without `--probe` nothing is started; the table shows each server's transport and its
(redacted) command or URL. With `--probe`, each distinct stdio server is started once
(a server configured identically for both clients is shared; servers with the same
command, cwd and tool definitions are shown once in *Largest tools* and the fixes, even
if their env differs), with its configured
command, args, env (on top of your environment; Claude `${VAR}` / `${VAR:-default}`
references are expanded) and cwd (default: the project directory). scan performs the MCP
`initialize` handshake, pages through `tools/list` with `agentwrench slim`'s bounded
discovery, then closes the server. Codex `enabled_tools` / `disabled_tools` filters are
applied before measuring. A server that cannot start, exits, or exceeds `--timeout` is
reported with a short reason and the scan continues. Server stderr is never shown, except
for a server that exits before listing its tools: then scan reports its exit status and its
last non-empty stderr line (kept in memory only, at most 160 characters, with configured env
values, tokens and `secret=value` pairs redacted), for example
`failed to start for discovery: server exited before listing its tools (exit status 1); last
stderr line: Error: Cannot find module 'server.js'`.

A config file that cannot be read (invalid JSON or TOML, unreadable file) is an error in
this section (`cannot read ~/.claude.json: invalid JSON; its MCP servers were skipped`);
that client's summary says *MCP unknown* instead of *no MCP servers*, and its JSON
`complete` is `false`. Remote
(HTTP/SSE) servers are listed but never contacted.

For each measured server, scan runs `agentwrench slim plan` in memory at a budget of
**200 tokens per tool**: strip schema annotations (`description`, `title`, `examples`,
`$comment`; defaults are kept), then cap tool descriptions, never removing a tool. If that
saves at least 500 tokens, the server gets a *trim* suggestion with the two commands that
reproduce it:

```sh
agentwrench slim discover --output github.tools.json -- <server command>   # capture the catalogue
agentwrench slim plan github.tools.json --budget 6600                      # print the slim config
```

Environment variables from the server config are passed as `--server-env NAME="$NAME"`, so
their values never appear in scan output; export them in your shell before running it.
Apply the printed config with `agentwrench slim proxy --config ... -- <server command>`.

### 2. Instructions loaded every turn

* **Codex**: the AGENTS.md chain for the project, computed by `agentwrench agentsmd`:
  `$CODEX_HOME/AGENTS.md` (or `AGENTS.override.md`), then one file per directory from
  the project root (`.git` or your `project_root_markers`) down to the project directory.
  Project files share `project_doc_max_bytes` (32 KiB unless configured); bytes beyond it
  are **silently dropped**. scan reports the bytes loaded against the limit, where the cut
  falls, and which top-level sections the agent never sees.
* **Claude Code**: `~/.claude/CLAUDE.md`, every `CLAUDE.md` / `.claude/CLAUDE.md` /
  `CLAUDE.local.md` from `/` down to the project, plus their `@imports` (up to four hops).
  Missing or out-of-tree imports are silently skipped by Claude Code; scan lists them.
  More than 40,000 bytes in total is flagged (Claude Code warns about memory files over
  40k characters).

Details: `agentwrench agentsmd explain --cwd <project>` (add `--agent claude` for Claude).

### 3. Sessions

A tool call without its result (or a result without its call) in a saved session makes
the provider reject the conversation, so `--resume` fails. scan checks the N most recent
files per client with `agentwrench doctor`'s streaming checker:

* Claude Code: `~/.claude/projects/*/*.jsonl` (subagent logs in deeper folders are skipped)
* Codex: `$CODEX_HOME/sessions/**/rollout-*.jsonl`

Files are opened read-only under a shared, non-blocking lock; locked or unreadable files
are counted as unreadable. (Windows, which is untested, has no `flock`: files are read
without the lock.) A file modified in the last 10 minutes whose only problem is a
pending call with no new user prompt after it is an agent that is running right now; it is
shown as *in progress* and not counted. For broken files scan prints the repair commands
for one example (stop the agent first):

```sh
agentwrench doctor repair <session.jsonl> --dry-run
agentwrench doctor repair <session.jsonl> --in-place --backup
```

### 4. Handoff packs

Shown only when the project holds a plain [`agentwrench handoff`](handoff.md) pack
(a directory with a `handoff.json` of schema `relayproof-handoff/1`) up to three
directories below the project, for example `.handoff/pack-01/`. Symlinks are not
followed and `.git`, `node_modules` and virtualenv directories are skipped; at most 10
packs are checked. For each one, scan runs the same check as
`agentwrench handoff verify PACK --repo <project>`: read-only `git` commands on the
project's repository (optional locks, fsmonitor, filters and the pager off) and a hash
of every file in scope. It needs no optional extra.

* A pack whose snapshot no longer matches the repository is a warning that names the
  first change; one whose run records no longer match is also a warning.
* A pack that fails its integrity check is an error.
* If the project is not inside a Git worktree, the packs are listed as *not checked*.

Each finding carries the `agentwrench handoff verify ...` command. A pack's own files
count as changes unless the pack sits at a Git-ignored path, which is where
`agentwrench handoff pack` writes inside a repository. In-toto receipt packages need the
`handoff` extra and are not checked. Without a pack, nothing is run and the section is
omitted from the text output.

### Summary

* **Fixed context overhead per turn**, per client: measured MCP tool tokens of the servers
  that client loads + its instruction tokens. Without `--probe` only instructions are
  counted. The model's own system prompt and built-in tools are not included.
* **Problems**: every finding (errors and warnings) from the sections above.
* **Top fixes**: the three findings with a command, ordered by estimated tokens saved per
  turn, then by severity.

## JSON output

`--json` prints one document with `"schema": "agentwrench.scan/v1"`. The full JSON Schema
is [docs/scan/scan.schema.json](scan/scan.schema.json); within v1, fields are only added,
never renamed or removed. Paths are absolute; commands are shell-quoted strings, one per
line.

```jsonc
{
  "schema": "agentwrench.scan/v1",
  "agentwrench_version": "0.1.0",
  "home": "/home/me", "project": "/home/me/src/app",
  "options": {"probe": true, "timeout_s": 10.0, "sessions": 50},
  "estimator": "ceil(UTF-8 bytes / 4); not a model tokenizer",
  "elapsed_ms": 412,
  "summary": {
    "clients": {
      "claude-code": {"fixed_overhead_tokens": 44936, "mcp_tokens": 35305, "instruction_tokens": 9631,
                      "mcp_servers": 5, "mcp_servers_measured": 3, "complete": false},
      "codex": { "...": "same shape" }
    },
    "max_fixed_overhead_tokens": 44936,
    "problems": 6, "errors": 4, "warnings": 2,
    "fail_over": null, "over_limit": false,
    "top_fixes": [ /* up to 3 findings */ ]
  },
  "mcp": {
    "probed": true, "elapsed_ms": 60, "tokens_per_tool_budget": 200,
    "servers": [{
      "client": "claude-code", "scope": "global", "source": "~/.claude.json", "name": "github",
      "transport": "stdio", "enabled": true,
      "status": "measured",            // listed|measured|failed|remote|disabled|shadowed|invalid
      "shadowed_by": null, "command": ["npx", "-y", "@modelcontextprotocol/server-github"], "url": null,
      "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"], "header_keys": [], "error": null,
      "tools": 33, "bytes": 41698, "estimated_tokens": 10425,
      "top_tools": [{"name": "create_pull_request_review", "estimated_tokens": 452}],
      "slim": {"budget": 6600, "estimated_tokens_after": 6370, "estimated_tokens_saved": 4055,
               "meets_budget": true, "config": {"stripSchemaAnnotations": ["description", "title", "examples", "$comment"]}},
      "duplicate_of": null,             // e.g. "Claude Code global github" for the Codex copy
      "exit": null                      // failed probes that exited: {"code": 1, "signal": null, "stderr": "Error: ..."}
    }]
  },
  "instructions": {
    "codex": {"codex_home": "...", "root": "...", "files": [{"path": "...", "scope": "project", "bytes": 37361,
              "loaded_bytes": 32768, "status": "partial"}],
              "project_bytes": 37361, "project_bytes_loaded": 32768, "limit_bytes": 32768, "usage": 1.0,
              "rendered_bytes": 33161, "estimated_tokens": 8291, "truncated": true, "dropped_bytes": 4593,
              "cuts": [{"path": "...", "line": 403, "heading": "### notifications errors",
                        "dropped_headings": ["## Security and secrets"]}], "errors": []},
    "claude-code": {"files": [], "imports": 3, "loaded_bytes": 38523, "estimated_tokens": 9631, "errors": []}
  },
  "sessions": {
    "claude-code": {"root": "...", "checked": 2, "clean": 1, "broken": 1, "live": 0, "errors": 0, "elapsed_ms": 1,
                    "files": [{"path": "...", "status": "broken", "issues": {"RD201": 1}, "first_issue_line": 2}],
                    "example_command": ["agentwrench doctor repair ... --dry-run", "agentwrench doctor repair ... --in-place --backup"]},
    "codex": { "...": "same shape" }
  },
  "handoff": {"repository": "/home/me/src/app",
              "packs": [{"path": "/home/me/src/app/.handoff/pack-01", "status": "stale_or_unbound",
                         "captured_at": "2026-11-17T09:12:44.120Z", "changes": ["File/index changed: src/retry.py"],
                         "evidence": {"records": 1, "matching": 0}, "error": null}]},
  "findings": [{"severity": "warning", "area": "mcp", "client": "claude-code",
                "message": "trim 'github' tool definitions (Claude Code + Codex): 10,425 -> ~6,370 tokens",
                "command": ["agentwrench slim discover ...", "agentwrench slim plan github.tools.json --budget 6600"],
                "tokens_saved": 4055}]
}
```

If an instruction section cannot be inspected at all, its object is `{"error": "..."}`.
Issue codes in `sessions.*.files[].issues` are `agentwrench doctor` codes (see
[doctor.md](doctor.md)).

## Privacy and safety

* **Never modifies files.** Configs are parsed in memory; session files are opened
  read-only with a shared lock. The scan never creates caches, backups or temp files.
  The `git` commands for handoff packs run with optional locks off, so they do not
  rewrite the Git index either.
* **No network.** scan itself opens no sockets. Remote MCP servers are listed, never
  contacted. `--probe` starts only *stdio* servers, and those run your own configured
  code, which may do whatever that code does.
* **No secrets in output.** Environment variables and HTTP headers are reported by
  *name* only. Commands and arguments pass through `agentwrench mcp-config`'s redaction
  plus flag-aware masking (`--token X`, `--api-key=X`, ...); URLs are reduced to
  `scheme://host/path` (no credentials, query or fragment). The suggested `slim discover`
  command passes env vars as `--server-env NAME="$NAME"` so values stay in your shell.
* **No content in output.** Only sizes, counts, headings of dropped AGENTS.md sections,
  tool names, issue codes and line numbers are reported; instruction and session text
  is never printed. Names and paths are stripped of terminal control characters.

## Limitations

* Token counts are estimates (bytes / 4), not tokenizer output.
* Claude Code can defer MCP tool definitions behind tool search when they are large; the
  MCP number is what the definitions cost when they are loaded.
* MCP servers contributed by Claude Code plugins, claude.ai connectors, Cline, Continue
  and VS Code are not listed (convert those configs with `agentwrench mcp-config`).
* Claude Code `.mcp.json` servers awaiting approval are counted as if approved.
* `--probe` accepts MCP protocol revisions 2024-11-05 through 2025-11-25 and needs servers
  that declare the `tools` capability.
