# agentwrench slim

`agentwrench slim` shrinks the tool definitions one stdio MCP server exposes to your
agent. It filters tool names, shortens descriptions and removes selected input-schema
annotations while keeping the validation structure. Measure and plan offline first,
optionally discover a server's complete catalogue, then put the proxy in front of the
real server.

```sh
pip install agentwrench            # or: uvx agentwrench slim --help
agentwrench slim measure examples/slim/tools-40.json
agentwrench slim plan examples/slim/tools-40.json --budget 6000 > slim.json
agentwrench slim proxy --config slim.json -- python3 examples/slim/fake_server.py
```

No daemon, API key, model or network service is needed. `measure` and `plan` never
start a server; `discover` and `proxy` start exactly the one server command you give
after `--`. The examples use the synthetic server and 40-tool catalogue in
[`examples/slim/`](../examples/slim); every tool and record in them is fictional.

`agentwrench scan --probe` runs the same measurement against every MCP server in your
Claude Code and Codex configs and prints the two `slim` commands that reproduce its
suggestion (see [scan](scan.md)).

## Usage

```text
agentwrench slim measure <tools.json> [--config slim.json] [--json]
agentwrench slim plan <tools.json> --budget <estimated-tokens>
agentwrench slim discover [--output <catalogue.json>] [--server-cwd <directory>]
    [--server-env <NAME=VALUE>]... [--server-unset-env <NAME>]...
    [--accept-protocol-version <YYYY-MM-DD>]... [--config slim.json]
    [--request-timeout-ms <ms>] [--discovery-timeout-ms <ms>] [--shutdown-timeout-ms <ms>]
    [--max-pages <n>] [--max-tools <n>] [--max-output-bytes <n>] [--max-wire-bytes <n>]
    -- <server> [args...]
agentwrench slim proxy --config slim.json [--request-timeout-ms <ms>] -- <server> [args...]
```

Exit status: **0** success, **1** discovery/runtime failure (with empty stdout for
`discover`), **2** invalid options, input or config (always before a server is started).
The proxy propagates the server's nonzero exit status; signal exits use `128 + signal`.

## Measure and plan offline

Input is a UTF-8 JSON file containing either a **tools/list result**
`{"tools":[{"name":"read_x","description":"Read x.","inputSchema":{"type":"object"}}]}`
or the bare tool array. A full JSON-RPC response envelope is rejected on purpose;
extract its `result` first. `measure` reads one snapshot or page; use `discover`
(below) to collect a paginated catalogue first.

```sh
agentwrench slim measure examples/slim/tools-40.json --json
agentwrench slim measure examples/slim/tools-40.json --config examples/slim/slim.json
```

```text
before  tools: 40  bytes: 91,147  est.tokens: ~22,787
after   tools: 16  bytes: 9,828  est.tokens: ~2,457
saved: 81319 B (89.2%)
```

Each tool's cost is the UTF-8 byte length of compact JSON with **name, description
(if present), inputSchema**, in that order. Totals sum those bytes; tokens are
**ceil(total bytes / 4)**, an estimate, not a tokenizer result, so per-tool estimates
need not add up to the total. Other metadata, transport envelopes, commas between
tools and client-specific prompt wrappers are excluded. JSON output includes per-tool
costs, the ten largest tools, the estimator and the scope; with `--config` it also
includes the original report and the savings.

`plan` writes **only a ready config to stdout**; stages, budget status and deny
suggestions go to stderr. It strips the four ordinary schema annotations, then
searches for the largest description cap that meets the budget. It never strips
`default` and never denies a tool by itself. Deny suggestions are literal tool names,
not pre-escaped globs; review them before adding them to a deny list.

```sh
agentwrench slim plan examples/slim/tools-40.json --budget 6000 > slim.json
agentwrench slim measure examples/slim/tools-40.json --config slim.json
```

```text
Estimator: ceil(UTF-8 bytes / 4); not a model tokenizer
Budget: 6000
original: 91147 B, ~22787 tokens
strip schema annotations (keep default): 37358 B, ~9340 tokens
description cap 155 code points: 23602 B, ~5901 tokens
Budget met. No tools denied.
```

`plan` exits 0 even for an unreachable budget: it still produced a config and says
**Budget NOT met**. When the budget can only be met by cutting descriptions below 40
characters, or cannot be met at all, it also prints `Warning:` lines on stderr that
suggest a strip-only config or a higher budget. For this catalogue, 4,000 tokens is
below the structural floor of ~4,618:

```sh
agentwrench slim plan examples/slim/tools-40.json --budget 4000 > slim.json
```

```text
description cap 0 code points: 18469 B, ~4618 tokens
Budget NOT met without removing tools. Config denies nothing.
Deny suggestion (review manually): "create_attachment" (465 B); remaining ~4501
...
Warning: description cap of 0 code points is below 40; tool descriptions lose most of their usage guidance.
Warning: budget 4000 is not met even by this config (~4618 tokens).
Warning: consider strip-only (remove maxDescriptionChars from the config: ~9340 tokens) or a higher budget (--budget 4885 keeps up to 40 characters).
```

Use `measure --json` on the result to check a budget in automation; the proxy does not
enforce token budgets.

| Configuration | Tools | Bytes | Estimated tokens |
| --- | ---: | ---: | ---: |
| Original `tools-40.json` | 40 | 91,147 | 22,787 |
| `plan --budget 6000` | 40 | 23,602 | 5,901 |
| `examples/slim/slim.json` (read/search allow list, 160-character cap) | 16 | 9,828 | 2,457 |

These are transformed fixture bytes, not a measure of model behavior. Shorter
descriptions can remove instructions the model relies on: review the plan and test your
real workload.

## Configuration

```json
{
  "allow": ["read_*", "search_*"],
  "deny": ["*_admin"],
  "maxDescriptionChars": 160,
  "stripSchemaAnnotations": ["description", "title", "examples", "$comment"]
}
```

All fields are optional; `{}` keeps tool content unchanged. Unknown keys and invalid
values exit 2 and name the offending key, before any server is started.

- `allow`: case-sensitive, whole-name globs. Omitted means all tools; `[]` means none.
  Only `*` (zero or more code points) and `?` (one code point) are special; brackets,
  slashes and regex characters are literal. There is no escape syntax.
- `deny`: matching names are hidden even if allowed (deny wins). Defaults to `[]`.
- `maxDescriptionChars`: integer 0–1,000,000, counted in Unicode **code points**. The
  cut prefers the last complete sentence, then a word boundary; a single long word is
  cut at a code point. No ellipsis is added, and combining-character graphemes may
  split. Omitted keeps descriptions; 0 makes them empty strings.
- `stripSchemaAnnotations`: any of `description`, `title`, `examples`, `$comment`,
  `default`. Omitted or `[]` strips nothing. `default` is opt-in because it can guide
  client behavior. Root and nested **inputSchema** annotations are eligible; tool
  titles, behavior annotations and output schemas are kept.

Schema traversal recognizes `properties`, `patternProperties`, `$defs`, `definitions`,
`dependentSchemas`, schema-valued `dependencies`, `items` (including legacy tuples),
`prefixItems`, `allOf`/`anyOf`/`oneOf`, `not`, `if`/`then`/`else`, `contains`,
`propertyNames`, `additionalProperties`, `additionalItems`, `unevaluatedProperties`,
`unevaluatedItems` and `contentSchema`. Property and definition names (even
`description`), `required`, `enum`, `const`, `type`, constraints, formats, references
and all other keywords are preserved. Literal data under `const`, `enum`, `default` and
`examples` is not rewritten, and the contents of unknown extension keywords are kept.
External references are never fetched. If a JSON Pointer `$ref` targets an annotation
value, that annotation keyword is kept throughout the schema so the target stays
valid; this can reduce savings. This is not a JSON Schema validator.

## Use the proxy with an MCP client

Replace the server command in your client config with `agentwrench slim proxy` and put
the real command after `--`. For example, in Claude Code's `.mcp.json`:

```json
{
  "mcpServers": {
    "github": {
      "command": "agentwrench",
      "args": ["slim", "proxy", "--config", "/path/to/github.slim.json", "--",
               "npx", "-y", "@modelcontextprotocol/server-github"]
    }
  }
}
```

Use an absolute path for `--config`: clients start servers in their own working
directory. Try it by hand with the synthetic server:

```sh
agentwrench slim proxy --config examples/slim/slim.json -- python3 examples/slim/fake_server.py <<'JSONL'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{"cursor":"page-2"}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"read_issue","arguments":{"workspace":"demo","id":1}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"delete_issue","arguments":{"workspace":"demo","id":1}}}
JSONL
```

The two list pages contain 2 and 14 visible tools, and `nextCursor: "page-2"` is kept
even when a filter empties a page. The `read_issue` call reaches the server; the
`delete_issue` call is answered locally with
`{"code":-32602,"message":"tool hidden by agentwrench slim"}`. Responses may arrive out
of order. Stdout carries only JSON-RPC; warnings and the server's own stderr go to
stderr.

### Proxy behavior

Wire references checked: MCP **2026-07-28** and **2025-11-25**. The proxy leaves
protocol negotiation to the endpoints; it supports the `initialize` exchange and
transparently forwards `server/discover`, request `_meta`, cache fields and
non-complete list results.

- It starts exactly one executable without a shell, with the inherited environment and
  working directory; arguments are passed literally.
- Valid JSON-RPC lines are forwarded byte for byte in both directions, except
  successful `tools/list` responses (re-serialized after slimming) and requests the
  proxy answers itself. List metadata, cursors, errors and notifications (including
  `list_changed`) are preserved.
- Hidden tools are blocked by name, even before any listing. Allowed unknown tools are
  passed through. Filtering is a convenience, **not an authorization sandbox**: other
  server methods may still have side effects.
- Invalid or non-JSON server stdout becomes a stderr warning without its content.
  Invalid client lines get `-32700`; invalid list requests `-32602`; invalid list
  results `-32603` (never the unfiltered tools).
- Only an absent `nextCursor` ends a listing; the empty string is a valid cursor.
  Pagination self-loops and cycles such as `A -> B -> A` are rejected with `-32603`
  (`agentwrench slim rejected tools/list pagination loop`); retrying the same page is
  allowed. Cursor histories are kept per request context (non-cursor params and
  `_meta`, excluding `progressToken`), stored as SHA-256 digests, capped at 10,000
  contexts and 10,000 transitions, and reset by `notifications/tools/list_changed`.
- Pending requests receive `-32000` when the server exits. Cancellation is forwarded;
  late `tools/list` replies are still filtered. Reusing a live or unresolved
  cancelled/timed-out request ID closes the transport. Numeric `1` and string `"1"`
  are distinct IDs.
- On stdin EOF the proxy closes server stdin, waits 1 s, sends SIGTERM, waits another
  second, then SIGKILL. SIGTERM/SIGINT are forwarded (exit 143/130). Only the direct
  child is managed, not grandchildren.
- Limits: JSON files ≤16 MiB, wire lines ≤8 MiB, nesting ≤128, ≤10,000 tools per
  page, ≤10,000 in-flight requests plus ≤10,000 unresolved cancelled/timed-out IDs,
  names ≤1,024 UTF-16 code units, descriptions ≤1,000,000 code points, ≤1,024 globs
  per list of ≤256 characters each. Exceeding a limit is an explicit error.
- Newline-framed UTF-8 only: no batch arrays and no Content-Length framing. Numeric
  IDs must be safe integers. Rewritten lists reject non-finite or unsafe numbers
  instead of rounding them.

Not provided: HTTP/SSE transports, auth, tool search or deferred loading, multi-server
aggregation, LLM rewriting, automatic pagination, config hot reload or task-quality
evaluation.

### Request timeout

```sh
printf '%s\n' '{"jsonrpc":"2.0","id":"slow","method":"tools/list"}' | (cat; sleep 1) |
  agentwrench slim proxy --config examples/slim/slim.json --request-timeout-ms 200 \
    -- python3 examples/slim/fake_server.py hold
```

```json
{"jsonrpc":"2.0","id":"slow","error":{"code":-32001,"message":"agentwrench slim request timed out"}}
```

`--request-timeout-ms` (1–2,147,483,647; omitted means no deadline) gives every
forwarded client request the same total deadline, starting just before its upstream
write; progress notifications do not extend it. On expiry the client gets the `-32001`
error above and the server gets `notifications/cancelled` (not for `initialize`,
task-augmented requests or null IDs). A late reply to a timed-out request is
discarded, and its ID stays reserved until that reply or transport close. A timeout
alone leaves the connection usable. It is a transport option, not a config key.

## Discover a complete stdio catalogue

```sh
agentwrench slim discover --output catalogue.json -- python3 examples/slim/fake_server.py static
agentwrench slim measure catalogue.json
agentwrench slim plan catalogue.json --budget 6000 > slim.json
```

`discover` starts the server once, performs the MCP `initialize` handshake with empty
client capabilities, sends `notifications/initialized`, then requests `tools/list`
sequentially, following opaque `nextCursor` values until the field is absent. It reads
no stdin and never sends `tools/call`. The output is one compact `{"tools":[...]}`
object plus a newline, ready for `measure` and `plan`; tools keep upstream order and
metadata, with `--config` (optional, `{}` by default) applied. Page metadata and cursors
are dropped. Duplicate names across pages are rejected, and a
`notifications/tools/list_changed` seen during collection fails the run instead of
mixing two catalogues (try `fake_server.py normal`, which sends one after every page).

Every failure (upstream error, invalid cursor/tool/JSON, unexpected response ID,
server request, early or nonzero exit, timeout, limit) exits **1 with empty stdout**.
Remote error contents are left out of diagnostics; the server's stderr is inherited
verbatim. Shell redirection still creates the destination on failure, so prefer
`--output`: it writes a temporary file beside the destination and renames it into
place only after a clean shutdown, so a failed run leaves an existing file untouched.
The new file has mode 0600; the parent directory must exist; `-` is a literal file
name. This is atomic visibility, not crash durability (no fsync).

### Protocol versions

`discover` speaks MCP **2025-11-25** and by default requires the server to answer
`initialize` with that version and a `tools` capability. Many servers still negotiate
an older revision; the error names it:

```sh
agentwrench slim discover -- python3 tests/scan/fixtures/old_protocol_server.py
agentwrench slim discover --accept-protocol-version 2025-06-18 -- python3 tests/scan/fixtures/old_protocol_server.py
```

```text
agentwrench slim: discovery requires tools capability and protocol 2025-11-25 (server negotiated 2025-06-18; to accept it, add --accept-protocol-version 2025-06-18)
{"tools":[{"name":"ping_db","description":"Check the database connection.","inputSchema":{"type":"object","properties":{}}}]}
```

(`tests/scan/fixtures/old_protocol_server.py` ships in the source distribution; it also
prints a marker line on stderr.)

`--accept-protocol-version YYYY-MM-DD` is repeatable and adds revisions to accept
besides 2025-11-25 (`initialize`, `tools/list` and pagination are the same in
2024-11-05, 2025-03-26 and 2025-06-18). `agentwrench scan --probe` accepts those
revisions automatically and adds the flag to the `slim discover` command it suggests
when a server negotiated one of them. Servers that need `server/discover`, client
capabilities, authentication or interactive server-initiated requests are not
supported by `discover`; the proxy still works for them.

### Server environment and working directory

- `--server-cwd DIR` starts the server in `DIR` (relative to the current directory).
  Relative server paths then resolve from `DIR`; `--config` and `--output` still
  resolve from the current directory. Missing paths and non-directories exit 2 before
  the server starts. It provides no filesystem isolation.
- `--server-env NAME=VALUE` (repeatable) overlays a variable in the server's inherited
  environment. The value is taken literally (split at the first `=`; `NAME=` sets an
  empty string). Names must match `[A-Za-z_][A-Za-z0-9_]*` and be distinct. Use a
  separate argument, not `--server-env=NAME=VALUE`. Diagnostics never echo values, but
  command-line values may be visible in process listings and shell history; prefer
  `--server-env TOKEN="$TOKEN"` from an exported shell variable.
- `--server-unset-env NAME` (repeatable) removes a variable from the server's
  environment; a name may not appear in both options. Removing `PATH` makes a bare
  executable fall back to the platform default lookup.

```sh
agentwrench slim discover --server-cwd examples/slim --server-env DEMO_MODE=local \
  --output catalogue.json -- python3 fake_server.py static
```

### Discovery limits

| Option | Range (default) | Bounds |
| --- | --- | --- |
| `--request-timeout-ms` | 1–2,147,483,647 (30,000) | each request, including `initialize`, until its reply |
| `--discovery-timeout-ms` | 1–2,147,483,647 (none) | the whole run: start, every page, clean shutdown |
| `--shutdown-timeout-ms` | 1–30,000 (1,000) | grace after the last page for the server to exit cleanly once stdin closes |
| `--max-pages` | 1–10,000 (10,000) | `tools/list` pages, including empty or fully filtered ones |
| `--max-tools` | 1–10,000 (10,000) | source tools across pages, counted before filtering |
| `--max-output-bytes` | 13–16,777,216 (16 MiB) | the compact output: 13 bytes for `{"tools":[]}` plus newline, each kept tool's JSON and one comma between tools |
| `--max-wire-bytes` | 1–2,147,483,647 (none) | raw bytes read from server stdout, through clean shutdown |

A run that ends exactly at a limit succeeds with identical output; the first page or
chunk that crosses one fails before another request is sent. Missing, invalid,
duplicate or out-of-range values exit 2 before the server starts. Independently, wire
lines are limited to 8 MiB. On failure the server gets SIGTERM, then SIGKILL after one
second; cleanup can outlast a deadline. Only the direct child is managed.

```sh
agentwrench slim discover --discovery-timeout-ms 5000 --request-timeout-ms 1000 \
  --max-pages 3 --max-tools 40 --output catalogue.json -- python3 examples/slim/fake_server.py static
```

## Python API

```python
import asyncio, json
from agentwrench.slim import apply_config, extract_tools, measure, plan, run_discover

tools = extract_tools(json.load(open("examples/slim/tools-40.json")))
print(measure(tools)["estimatedTokens"])                   # 22787
result = plan(tools, 6000)
print(result["config"], result["meetsBudget"])
slimmed = apply_config(tools, result["config"])

catalogue = asyncio.run(run_discover(["python3", "examples/slim/fake_server.py", "static"], {},
                                     protocol_versions=("2025-06-18", "2025-11-25")))
```

`run_discover` takes the same limits as keyword arguments (`request_timeout_ms`,
`max_pages`, `server_env=["NAME=VALUE"]`, ...), plus `server_stderr` (for example
`subprocess.DEVNULL`) and `on_protocol_version`, a callback that receives the
negotiated version.
