# agentwrench cassette

`agentwrench cassette` records an MCP stdio session once so client and agent
tests can run offline against its recorded responses. It is a real full-duplex
recording proxy and a standalone JSON-RPC replay server, plus a pytest fixture
that checks for unmatched and unused calls. The runtime `record`, `replay`,
`inspect` and `validate` commands use only the Python standard library; Linux.
The cassette file format is documented in [cassette/CASSETTE.md](cassette/CASSETTE.md).

## Install

```sh
pip install agentwrench          # or: pipx install agentwrench
agentwrench cassette --help
```

`uvx agentwrench cassette ...` runs it without a permanent install, and
`python -m agentwrench cassette ...` is equivalent to the console command.
Requires Python 3.11 or newer.

The pytest plugin needs pytest in the same environment as agentwrench. The
official SDK example below needs the official MCP Python SDK:

```sh
pip install 'mcp>=1.26,<2'       # or: pip install 'agentwrench[test]'
```

Runtime `record`, `replay`, `inspect` and `validate` do not import pytest or the
SDK. There is no network access during the test suite, no credentials, and no
persistent service.

## Try a complete session

From a checkout of the repository, with the MCP SDK installed:

```sh
python examples/cassette/echo_client.py record demo-echo.json
python examples/cassette/echo_client.py replay demo-echo.json
python examples/cassette/echo_client.py replay examples/cassette/cassettes/echo.json
```

All three print:

```json
{"tools": ["echo"], "content": [{"type": "text", "text": "你好, offline MCP!", "annotations": null, "meta": null}]}
```

(The SDK may include additional null-valued content fields.) The example uses
`ClientSession` and `stdio_client` from the official MCP SDK and launches
`python -m agentwrench cassette record|replay` as its server process. It performs
initialize, initialized notification, tool listing, an echo call and ping.
`record` runs the synthetic `examples/cassette/echo_server.py`; replay does not
run any server script and never modifies the cassette. The SDK end-to-end test
goes further: it deletes the server copy before replay and compares the full SDK
results.

To connect any stdio MCP client, configure its executable and arguments:

```sh
# The client launches this proxy in place of the real server:
agentwrench cassette record --cassette cassettes/my-server.json \
  --redact-path /params/arguments/token \
  --redact-path /result/content/0/text \
  -- python path/to/your_server.py --your-server-option

# Later the client launches this offline process instead:
agentwrench cassette replay --cassette cassettes/my-server.json --strict

agentwrench cassette inspect cassettes/my-server.json
agentwrench cassette validate cassettes/my-server.json
```

In an MCP client configuration that takes a command plus an argument array:

```json
{
  "command": "agentwrench",
  "args": ["cassette", "replay", "--cassette", "cassettes/my-server.json", "--strict"]
}
```

(Use `"command": "python", "args": ["-m", "agentwrench", "cassette", ...]` to pin
a specific interpreter.)

`record`/`replay` speak newline-delimited MCP JSON-RPC on stdin/stdout; they wait
for client input, so a bare invocation is not a human-facing interactive prompt.
Diagnostics go to stderr, prefixed `agentwrench cassette: `. The real command is
an explicit argv list after `--`, never a shell string. `record` runs that
command with the caller's environment; argv and environment values are not
captured as metadata. Redaction changes only the stored copy and the matching
key, not the live session. Redacted outputs remain redacted during replay.
Choose pointers for every sensitive field before recording; there is no
automatic secret detector. Upstream stderr is forwarded live.

| Subcommand | Options |
| --- | --- |
| `record -- COMMAND...` | `--cassette PATH` (required), `--redact-path POINTER` (repeatable), `--shutdown-timeout SECONDS` (default 2), `--skip-non-json`, `--report FILE` |
| `replay` | `--cassette PATH` (required), `--strict`, `--match params\|sequence` (default `params`), `--fault-scenario FILE`, `--report FILE` |
| `inspect PATH` | `--frame N` |
| `validate PATH` | none |

Redaction pointers use JSON Pointer escaping (`~1` for `/`, `~0` for `~`).
Object keys match exactly, including numeric-looking keys. Array indices must
be canonical ASCII decimal (`0`, `1`, …), without signs or leading zeros.
Missing paths, out-of-range indices and invalid array-index tokens are no-ops;
this includes arbitrarily long decimal tokens within the metadata budget and
Unicode digit lookalikes. Array lookup checks token length and bounds before
integer conversion, in both traversal and replacement. Valid pointers replace
the whole selected value with `"<REDACTED>"`; configured paths are saved intact.
See [Redaction](cassette/CASSETTE.md#redaction) for which fields may be redacted.

`agentwrench cassette inspect examples/cassette/cassettes/echo.json` reports
`incomplete: false`, 10 events, one each of
initialize/initialized/tools-list/tools-call/ping, and `echo: 1`:

```json
{
  "version": 1,
  "incomplete": false,
  "events": 10,
  "methods": {
    "initialize": 1,
    "notifications/initialized": 1,
    "ping": 1,
    "tools/call": 1,
    "tools/list": 1
  },
  "tools": {
    "echo": 1
  },
  "server_requests": 0
}
```

## Inspect one frame and its peer

```sh
agentwrench cassette inspect examples/cassette/cassettes/echo.json --frame 8
```

This prints a JSON report with `report_version: 1`, `valid: true`,
`event_count: 10`, `diagnostics: []`, `frame` for the stored ping at index 8,
and `peer` for its response at index 9. Each selection contains `frame_index`,
`path` (for example `/events/8`), and the exact stored `event`, including its
original message and ID. It never reads client input, replays messages, launches
a server or changes the cassette. Stored redactions remain as stored; this
command prints payloads, so choose the cassette's redactions before recording.

Indices are zero-based nonnegative decimal integers. Notifications and requests
without a response have `peer: null`. Pairing follows in-flight typed IDs in
each direction: integer `7` differs from string `"7"`, and reuse after a completed
exchange selects that exchange's own peer. Duplicate in-flight IDs keep the
original pending request; the duplicate gets no peer.

Exit **0** means a complete valid recording. Incomplete recordings and ID errors
still show the selected frame and any peer, with validation diagnostics and exit
**1**. A schema/version error exits **1** with both selections `null`; unreadable,
oversized or malformed JSON exits **2**. Negative, non-integer or out-of-range
indices (including index 0 in an empty recording) exit **2** with an
`invalid_frame_index` diagnostic and both selections `null`. Reports go to
stdout, with no stderr for these failures. See the
[full report contract](cassette/CASSETTE.md#frame-inspection-report-v1).
Without `--frame`, the counts-only output and exit behavior are unchanged.

## Validate a recording

`agentwrench cassette validate session.json` prints a JSON integrity report with
`report_version: 1`, the cassette version, event count, `valid`, and diagnostics.
It never launches a server. Unlike `inspect` without `--frame`, it exits **1** for
incomplete recordings, schema/version errors, duplicate in-flight IDs, unmatched
responses or unanswered requests. A complete valid recording exits **0** with
`diagnostics: []`. Unreadable, oversized or malformed JSON files exit **2** with
the same report envelope and unknown version/counts. Reports go to stdout;
command-line usage errors go to stderr.

For example, save this synthetic mismatched-ID recording as `session.json`:

```json
{"version":1,"incomplete":false,"redact_paths":[],"events":[
  {"seq":0,"ms":0,"direction":"client","message":{"jsonrpc":"2.0","id":1,"method":"ping"}},
  {"seq":1,"ms":1,"direction":"server","message":{"jsonrpc":"2.0","id":"1","result":{}}}
]}
```

`agentwrench cassette validate session.json` exits 1 and reports (shown
compacted; the CLI pretty-prints with two-space indentation):

```json
{
  "report_version": 1,
  "cassette_version": 1,
  "valid": false,
  "event_count": 2,
  "diagnostics": [
    {"code":"unmatched_response","message":"response has no matching in-flight request","location":{"path":"/events/1/message/id","frame_index":1}},
    {"code":"unanswered_request","message":"unanswered request at end of recording","location":{"path":"/events/0/message/id","frame_index":0}}
  ]
}
```

Integer `1` and string `"1"` are different IDs. Locations use zero-based event
array indices and JSON Pointers, even if a stored `seq` is corrupt. Duplicate
IDs also point to the original request in `related`. An incomplete cassette
reports `/incomplete` plus each unanswered request's frame. Physically truncated
JSON reports the exact parser line/column (one-based) and character offset
(zero-based); unparseable frames are not recovered. See the
[report contract](cassette/CASSETTE.md#validation-report-v1) for limits and codes.

## pytest integration

Installing agentwrench registers the plugin through the `pytest11` entry point
`agentwrench_cassette = agentwrench.cassette.pytest_plugin`, so pytest loads it
automatically whenever agentwrench is installed in the same environment. Disable
it with `pytest -p no:agentwrench_cassette`. The plugin adds the `mcp_cassette`
marker and fixture and the `--mcp-record` option.

Put cassettes in a `cassettes/` directory next to the test file. This complete
example (it needs the MCP SDK) consumes the echo cassette when a copy of
`examples/cassette/cassettes/echo.json` is placed in `cassettes/echo.json` next
to it:

```python
import asyncio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

@pytest.mark.mcp_cassette("echo")
def test_agent(mcp_cassette):
    async def exercise():
        argv = mcp_cassette.argv
        params = StdioServerParameters(command=argv[0], args=argv[1:])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                result = await session.call_tool("echo", {"text": "你好, offline MCP!"})
                await session.send_ping()
                assert result.content[0].text == "你好, offline MCP!"
    asyncio.run(exercise())
```

`mcp_cassette.argv` launches `python -m agentwrench cassette replay ...` (or
`record ...`) with the current interpreter. For tests you want to record, add an
explicit real-server command to the marker:

```python
import sys

@pytest.mark.mcp_cassette(
    "my-session",
    command=[sys.executable, "path/to/server.py"],
    redact_paths=["/params/arguments/token"],
    strict=True,
    match="params",
)
```

| Option | Behavior |
| --- | --- |
| `pytest --mcp-record=none` (default) | Replay only; missing/incomplete cassette is an error. Never executes the marker command. |
| `pytest --mcp-record=once` | Record only a missing cassette, otherwise replay it. |
| `pytest --mcp-record=all` | Run the real command and replace the cassette. Explicitly opts into server side effects. |

The fixture returns `.argv`, `.path`, `.recording`. Without a marker it uses a
safe node name plus a node-ID hash to avoid collisions. An explicit name must be
a plain filename, never a path. The client **must close and wait for the process
within the test**. Fixture teardown checks an exit report, so strict errors fail
pytest even if the SDK discards the server exit code. Accessing the fixture
without running/closing its process also fails teardown. Launch it once per test.

For replay and recording, the `--report` sidecar must be a JSON object with an
integer `exit_code` and nonnegative integer `unmatched` and `unused` counts.
The pytest fixture accepts at most **4096 bytes**, including encoding and all
leading/trailing whitespace, in ordinary replay, fault replay and recording.
It reads at most 4097 bytes without buffered read-ahead and rejects oversize
before JSON parsing: `invalid MCP subprocess result: exit report exceeds 4096 bytes`.
An exactly 4096-byte valid report is accepted; malformed bounded JSON and report
read errors fail with `invalid MCP subprocess result`. Current CLI reports fit
comfortably within this bound.
Missing fields, booleans (including `false` in place of zero), floats, strings,
nulls and non-object reports fail teardown with `invalid MCP subprocess result`.
Additional top-level fields are allowed. Success requires `exit_code: 0`;
nonzero integer codes fail with `MCP cassette process failed`. Strict replay
also requires both counts to be zero, even if a report claims exit zero or a
fault triggered. Non-strict replay permits valid positive counts. Recording
still requires a complete cassette. The report is a local subprocess result,
not proof of launch or authentication; the client must launch and wait as above.

To arm a transport fault, pass a **v1 dictionary** as `fault_scenario`. This
complete example uses the same `cassettes/echo.json` copy:

```python
import subprocess
import pytest

@pytest.mark.mcp_cassette(
    "echo", strict=False,
    fault_scenario={"version": 1, "request_seq": 8, "kind": "eof"},
)
def test_ping_eof(mcp_cassette):
    result = subprocess.run(
        mcp_cassette.argv,
        input=b'{"jsonrpc":"2.0","id":99,"method":"ping"}\n',
        capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""
```

This deliberately exercises only ping, so `strict=False` permits the unused
earlier interactions. With the default `strict=True`, consume the earlier
interactions too; a triggered fault does not waive unmatched/unused checks.
For a timeout, use `{"version":1,"request_seq":8,"kind":"timeout","duration_ms":1000}`
and a client request timeout shorter than 1000 ms; see the
[transport behavior](#opt-in-timeout-and-eof-tests).

The fixture validates the dictionary and its recorded client-request target
before yielding, then writes a private, separate scenario file under pytest's
temporary test directory and adds `--fault-scenario FILE` to replay `.argv`.
It leaves the marker dictionary and cassette unchanged; later dictionary
mutations do not alter the armed snapshot. The same v1 field and duration bounds
apply as in the CLI. Explicit `None`, file paths, missing/unknown fields,
boolean/noninteger indices or durations, and notification/response/server-request
targets are setup errors. Omit the option for ordinary replay.

Faults require an existing complete cassette with `--mcp-record=none` or `once`.
`all`, or `once` with a missing cassette, fails setup before the test body or
server command can run. Teardown requires both a successful exit report and
`fault` fields matching the armed scenario with boolean `triggered: true`.
Missing, malformed, mismatched or untriggered fault reports fail teardown even
when a client ignores the subprocess status. Cassette metadata never arms a fault.

## Determinism and failure behavior

- `--match params` (default): method plus recursively canonicalized params,
  one-use FIFO for identical repeats. Request and notification keys are distinct.
  `--match sequence`: require the next recorded client event to match.
- Initialize reuses recorded server identity/capabilities, ignoring client identity
  and capability differences. A requested/recorded/negotiated version mismatch
  warns; `--strict` sends an error and fails. Responses use the new live IDs.
- Recorded server notifications are emitted once with their owning interaction;
  ordinary request progress tokens bind to the live token in either matching
  mode. Progress-token ownership works for overlapping recorded calls. Output order is
  preserved within an interaction. See the [format](cassette/CASSETTE.md#matching)
  for precise ownership rules. Recorded ping calls are consumed; extra pings return `{}`.
- Unrecorded requests get JSON-RPC `-32001` diagnostics. Unknown notifications
  warn without a response. Strict replay exits 1 for unmatched **or unused**
  interactions (including recorded client notifications) at EOF or SIGTERM.
- Replay retains the first **16 unmatched diagnostic details**, with a separate
  exact count. Final unmatched/unused summaries show the exact total and at most
  16 whole details, adding `; ... N omitted` when details are omitted. Each
  summary is at most **4096 UTF-8 bytes** (4119 including the CLI's 22-byte
  `agentwrench cassette: ` prefix and newline), including escaped Unicode/control
  characters and omission counts. Params are never included. Small summaries
  keep their full text. Per-event warnings and request error responses
  are not shortened; cumulative stderr output is not rate limited.
  For Python callers, `agentwrench.cassette.replay.Replayer.unmatched` holds
  only that first-16 sample; use `unmatched_count` for the total. `unused` still
  returns all unused details; internal finalization and CLI reports use
  `unused_count` and a bounded sample without formatting the full list. Report
  counters and strict/fault behavior remain exact, including omitted interactions.
- Record finalizes atomically on EOF or SIGTERM/SIGINT. Crashes, unanswered
  requests, framing errors and shutdown timeouts produce `incomplete: true` and
  exit 1. Replay refuses incomplete or unsupported cassettes with exit 2.
- For record/replay/inspect, invalid JSON, unsupported format versions and I/O errors exit 2. No non-MCP
  text goes to stdout in record/replay. `--skip-non-json` explicitly opts into
  discarding noisy server stdout lines with warnings; invalid JSON-RPC still fails.

For example, a strict replay of the echo cassette that sends an unrecorded
request answers it with `-32001` and lists the problems on stderr before exiting 1:

```text
agentwrench cassette: unmatched interactions (1): unmatched request/notification; params and progressToken presence must match: method="nope" (params omitted)
agentwrench cassette: unused interactions (5): seq=0 method="initialize"; seq=2 method="notifications/initialized"; seq=3 method="tools/list"; seq=5 method="tools/call"; seq=8 method="ping"
```

## Progress tokens across runs

Ordinary requests can use a fresh `params._meta.progressToken` at replay time,
including changing a string token to an integer or vice versa. Only that value
is bound to the matched interaction; arguments and every other `_meta` field
still compare strictly (after configured redaction). Token presence must agree:
adding or omitting a token fails matching with `-32001` and fails strict replay.
The entire `_meta` object is never discarded. Initialize retains its special
matching.

For example, from a checkout, record the dependency-free synthetic server:

```sh
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"work","arguments":{"text":"hello"},"_meta":{"progressToken":"recorded"}}}' | \
  agentwrench cassette record --cassette progress.json -- python examples/cassette/progress_server.py
printf '%s\n' '{"jsonrpc":"2.0","id":"live-id","method":"tools/call","params":{"name":"work","arguments":{"text":"hello"},"_meta":{"progressToken":42}}}' | \
  agentwrench cassette replay --cassette progress.json --strict
```

Replay emits two `notifications/progress` messages with token `42`, progress
`0.5` then `2.5`, total `2.5`, and messages `starting` then `done`, followed by
the unchanged tool result with ID `"live-id"`:

```text
{"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":42,"progress":0.5,"total":2.5,"message":"starting"}}
{"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":42,"progress":2.5,"total":2.5,"message":"done"}}
{"jsonrpc":"2.0","id":"live-id","result":{"content":[{"type":"text","text":"hello"}],"isError":false}}
```

Only progress notification tokens and response IDs change; unrelated
notification/result fields remain as stored. No server is launched during
replay, and the v1 cassette is not rewritten.

Tokens must be strings or integers, excluding booleans. Replay rejects invalid
tokens, duplicate active recorded tokens, and progress without a matching active
recorded request (including startup or after its response), with exit 2 and a
stderr diagnostic. Typed tokens `7` and `"7"` differ; completed tokens may be
reused. Existing valid v1 cassettes need no conversion; orphan or ambiguous
progress is refused rather than guessed. Redaction must preserve unambiguous
stored request/notification linkage.

This uses the per-interaction output order in both `params` and `sequence`
modes; it adds no concurrency scheduling, timing or cancellation.
Task-augmented requests (`params.task`) and `tasks/*` keep exact token matching;
Tasks lifecycle and progress after a task-creation response remain unsupported.
See the [compatibility contract](cassette/CASSETTE.md#progress-token-binding) and
the [official MCP progress specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress).

## Opt-in timeout and EOF tests

Use a **separate** JSON scenario with `replay --fault-scenario FILE`. Nothing in
a cassette automatically enables faults; replay without the flag keeps the same
successful stdout bytes. A scenario targets one recorded **client request's
event `seq`**, after normal matching, independently of its new live ID.

For example, the echo cassette's ping is at `seq: 8`:

```sh
printf '%s\n' '{"version":1,"request_seq":8,"kind":"eof"}' > eof.json
printf '%s\n' '{"jsonrpc":"2.0","id":99,"method":"ping"}' | \
  agentwrench cassette replay --cassette examples/cassette/cassettes/echo.json \
  --fault-scenario eof.json --report fault-report.json
```

This prints **no stdout**, closes the transport and exits 0. Stderr identifies
the triggered fault and warns about unused earlier interactions:

```text
agentwrench cassette: transport fault triggered: kind=eof request_seq=8
agentwrench cassette: unused interactions (4): seq=0 method="initialize"; seq=2 method="notifications/initialized"; seq=3 method="tools/list"; seq=5 method="tools/call"
```

The report includes `"fault":{"version":1,"request_seq":8,"kind":"eof","triggered":true}`.
Without the scenario, the same ping produces
`{"jsonrpc":"2.0","id":99,"result":{}}` followed by a newline.

To test a client timeout instead, use:

```json
{"version":1,"request_seq":8,"kind":"timeout","duration_ms":1000}
```

Set the client's request timeout shorter than 1000 ms. Replay consumes the
matched request, suppresses all its owned outputs (including notifications),
stops reading further requests, stays silent for the configured duration, then
closes stdout and exits even if client stdin remains open. EOF uses the same
terminal behavior immediately. These are transport failures, not synthetic
JSON-RPC errors. A client with a longer timeout sees EOF instead. SIGTERM/SIGINT
can interrupt the stall early; deadlines use the event loop and are subject to
OS scheduling and the bounded stdout shutdown.

`duration_ms` must be an integer from 1 to 60,000; EOF accepts no duration.
Scenarios are limited to 4096 bytes and reject missing/unknown fields, invalid
versions, and targets that are not recorded client requests, before any output
(exit 2). A scenario that never triggers exits 1 even without `--strict`.
Intentional faults alone exit 0, but strict unmatched/unused checks still apply:
use a cassette ending at the target interaction for a fully consumed strict
fault test. Earlier successful interactions retain their bytes; later pipelined
requests are not processed after the terminal fault. Each launch arms the
scenario once; cassette and scenario files are never modified.

The official SDK integration tests verify `McpError` connection-closed
and request-timeout behavior. See the [scenario contract](cassette/CASSETTE.md#transport-fault-scenario-v1).

## Scope and limits

The tested protocol compatibility target is the initialize-based MCP revision
**2025-11-25**, using the official Python SDK (the `test` extra requires
`mcp>=1.26,<2`). The current 2026-07-28 protocol uses discovery/per-request
negotiation; this release does not synthesize those behaviors or claim
modern-protocol conformance.

Only stdio, not HTTP/SSE, auth flows, fuzzy matching or LLM matching. Recorded
server-initiated requests are captured, but replay warns and refuses the entire
cassette rather than dropping sampling/elicitation/roots callbacks. Recorded timing,
concurrent completion order, Tasks lifecycle and cancellation semantics are not
simulated. Progress-token binding is limited to ordinary requests as above.
Use deterministic client calls. Raw JSON-RPC
envelopes are validated, not all method-specific MCP schemas.

The tool rejects batches, non-integer/non-string IDs, duplicate in-flight IDs,
oversized lines (>1 MiB), oversized cassettes (>64 MiB), and >100,000 events.
Messages support at most **64 container levels**, counting the message object as
level 1 and every nested object or array (including empty containers) as one
more. Scalars add no level; cassette/event wrappers do not count. The bound
covers every message field, including unknown fields, and applies **before
redaction**, even with no pointers, absent pointers or whole-value replacements.
Exactly 64 levels is supported. Deeper messages raise `CassetteError` with
`message container nesting exceeds 64 levels`. The shared iterative check runs
before recursive redaction/copy for wire and stored messages and the direct Python
paths (`validate_message` and `redact` in `agentwrench.cassette.format`,
`Recorder.append` in `agentwrench.cassette.record`, and
`agentwrench.cassette.replay.Replayer`). It does not
change Python's global recursion limit. JSON deeper than the decoder/serializer
can materialize also uses this diagnostic, without partial recovery.

Integers support at most **4,096 decimal digits of magnitude**, excluding the
minus sign. Zero (including JSON `-0`) counts as one digit. Exactly 4,096 is
accepted; larger integers raise `CassetteError` with
`integer exceeds supported 4096 decimal digits` and code `integer_too_large`.
The JSON parser bounds integer tokens before decimal conversion, including in
cassettes and unknown fields. Direct Python message paths compare integer
magnitude before recursive copying or decimal serialization. The bound applies
before redaction, including absent and whole-value pointers, and to integer IDs,
progress tokens, params, results and error data. Supported integers stay exact;
integer IDs remain distinct from string IDs. Strings of digits and floating-point
notation are unaffected. Python's global integer-string and
recursion limits are not changed; use the default integer-string limit (4,300
on CPython 3.11) or a limit of at least 4,096, or disable that interpreter limit.
`--skip-non-json` never skips an unsupported integer frame. Stored integer-token
rejection makes validate/frame inspection exit 2 with a root `integer_too_large`
diagnostic and null frame/peer, before cassette schema checks.

Floating-point message values must be finite, including direct Python inputs.
`NaN`, positive/negative infinity and overflowing JSON float notation are
unsupported. Direct `validate_message`, `redact`, `Recorder.append` and live
`Replayer.handle` calls raise `CassetteError` with
`non-finite JSON numbers are not supported` and code `nonfinite_number` before
copying, redaction or serialization. This covers every dict/list message value,
including params, results, error data and unknown fields; absent or whole-value
pointers cannot hide it. Direct stored-cassette validation uses the same text
with `invalid_schema` at `/events/N/message`. Wire JSON uses the
`invalid_json` rejection. Finite values such as `1e308` and signed `0.0`/`-0.0`
remain supported and preserve their whole values. Rejection leaves recorder
prefix, pending IDs and byte budgets, and live replay consumption unchanged.

Recording rejects the whole frame before forwarding, storing or changing
pending IDs, saves the accepted prefix as incomplete, and exits/reports **1**.
Rejected responses leave accepted requests pending. Replay rejects unsupported
stored or live messages with exit/report **2**; live rejection consumes no
interaction. Supported messages retain their whole values, matching behavior
and original recording wire bytes. This admission bound does not guarantee
streaming parsing or a process-memory ceiling.

For recording, each **stored** message has the same 1,048,576-byte limit,
measured as compact JSON encoded as UTF-8 (`ensure_ascii=False`) plus one LF,
**after redaction**. This differs from the pretty-printed event/file size.
Exactly the limit is accepted. Redaction of small values or numeric
reserialization can expand an otherwise valid live message beyond it. Recording
then rejects that entire message before storing, changing pending IDs or
forwarding it, and atomically saves the valid prefix as `incomplete: true` with
exit/report **1** and `recorded message exceeds 1 MiB after redaction (including newline)`.
Accepted requests remain pending if their response is rejected; rejected new
requests never enter pending state. No payload, routing field or redaction
placeholder is truncated or silently omitted. Successful recording still
forwards the original live bytes, including whitespace and newline style.

The aggregate **64 MiB** recording budget includes actual serialized
`redact_paths`, metadata, pretty event indentation and the file's final LF.
Before launching the server, the recorder checks that the configured paths and
bounded finalization metadata fit; otherwise it exits/reports **2** with
`recording metadata exceeds 64 MiB finalization budget`, preserving the destination.
Before accepting each event, it reserves space for final failure reasons. If the
next event cannot fit, it is rejected before pending-ID changes or forwarding;
the accepted prefix atomically replaces the destination as a valid incomplete
cassette with exit/report **1**. All configured paths and accepted message fields
are preserved. Reasons are limited to eight JSON strings of at most 256 UTF-8
bytes each including quotes/escaping; oversized diagnostics use fixed omission
text and excess reasons share a final omission marker. The stderr summary uses
the same bounded reasons. This is a file-size bound, not a process-memory bound.

Each replay output frame is compact JSON encoded as UTF-8 (`ensure_ascii=False`),
plus one newline, with a maximum of **1,048,576 bytes including that newline**.
The actual serialized bytes are checked after live response-ID/progress-token
rewriting, also for generated error responses. Exactly that limit is valid;
larger output stops replay before writing any of that frame, with
`replay output line exceeds 1 MiB (including newline)` on stderr and exit/report
status **2**, even in non-strict mode. JSON, IDs and tokens are never truncated.
Earlier valid frames may already have been emitted; subsequent frames are not
emitted. A matched interaction remains consumed even if one of its outputs is
rejected; report counters preserve prior unmatched and remaining unused calls.
An armed transport fault still suppresses its target's outputs before this check.

Cassettes are memory-resident. No cross-process locks or pytest-xdist coordination;
use one writer per cassette and run these tests sequentially. SIGKILL cannot
finalize a recording. CLI parent directories are caller-trusted; this is not a
sandbox for untrusted server commands. No dry-run is provided: replay/inspect
never execute workflows, and recording explicitly executes the supplied server.

This is independent of MCP Inspector and its native export formats; it does not
import Inspector sessions. The JSON format is specific to this project and
[versioned/documented](cassette/CASSETTE.md); cassettes record
`"generator": "mcp-cassette/0.1.0"` in their metadata.

## Development

From the repository root:

```sh
uv run --extra test pytest tests/cassette
```

The tests use synthetic, dependency-free servers in `tests/cassette/fixtures/`
and the checked-in `tests/cassette/cassettes/echo.json`; the official SDK tests
record and replay through the real `ClientSession`.

## License

Apache-2.0 as part of agentwrench; see [LICENSE](../LICENSE),
[NOTICE](../NOTICE), and [THIRD_PARTY.md](../THIRD_PARTY.md).
