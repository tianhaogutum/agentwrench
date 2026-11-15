# Cassette format v1

This is the file format read and written by
[`agentwrench cassette`](../cassette.md). A cassette is one UTF-8 JSON object, not an executable configuration. Replay
never interprets shell, Python, templates or expressions from a cassette.
Unknown object fields are retained/ignored; the version must be exactly integer
`1`. Duplicate JSON keys, NaN/Infinity, non-UTF-8 strings and lone surrogates are
rejected. JSON-RPC batches are not supported.

```json
{
  "version": 1,
  "incomplete": false,
  "redact_paths": [],
  "metadata": {
    "generator": "agentwrench-cassette/0.1.0",
    "server_exit_code": 0,
    "reasons": []
  },
  "events": [
    {"seq": 0, "ms": 0.0, "direction": "client",
     "message": {"jsonrpc": "2.0", "id": 7, "method": "ping"}},
    {"seq": 1, "ms": 1.25, "direction": "server",
     "message": {"jsonrpc": "2.0", "id": 7, "result": {}}}
  ]
}
```

`seq` is contiguous from zero. `ms` is nonnegative, finite, monotonic elapsed
milliseconds, measured at receipt by the proxy (not server execution time).
`direction` is `client` for messages sent to the child, `server` for messages
received from the child. Messages include requests, responses and notifications.
`metadata` is informational; exit code and reason strings indicate why a
recording is incomplete. Readers never interpret `generator`: new recordings say
`"agentwrench-cassette/<version>"`, older ones `"mcp-cassette/0.1.0"`, and both
load the same. No argv, environment, cwd, PID or stderr is stored.

The parser validates JSON-RPC 2.0 envelope structure; MCP params and `_meta`
must be objects. Request IDs are integers or strings, with types kept distinct.
Responses are paired with requests by `(direction, typed id)` rather than order;
concurrent requests and subsequent reuse of a completed ID are supported. IDs
must be unique while in flight in each direction. Complete cassettes cannot
contain orphan responses or unanswered requests. Incomplete cassettes can be
inspected but are refused by replay, including non-strict replay.

Message container depth is at most **64**, including the root message object
as level 1. Each nested object/array, including an empty one, adds one level;
scalars add none. Cassette/event wrappers do not count. All message fields,
including unknown fields, are checked before recursive redaction/copy. No
pointers, missing pointers and whole-value redactions do not bypass admission.
Exactly 64 levels is accepted. Excessive depth raises `CassetteError` with
`message container nesting exceeds 64 levels` in wire/cassette and direct Python
paths, without changing Python's global recursion limit. Parser/serializer
recursion failures on extremely deep JSON use the same text; input is not
partially recovered.

Integer magnitude is limited to **4,096 decimal digits**; the minus sign is not
counted, and `0`/`-0` count as one digit. JSON grammar still forbids leading zeros
and plus signs. Integer tokens are length-checked before conversion throughout
the JSON document, including unknown fields/metadata. Direct Python message
values are checked by signed magnitude before recursive redaction/copy or
serialization, including IDs and progress tokens. Missing/whole-value redaction
pointers cannot bypass admission. The fixed diagnostic is
`integer exceeds supported 4096 decimal digits` (`CassetteError`, code
`integer_too_large`), without echoing the value. Exactly the bound is supported
under Python's default integer-string limit (or a configured limit >=4,096 or
disabled); the tool does not change global interpreter limits. Strings and
float notation retain existing rules. Supported integers are exact and typed.
Stored token rejection occurs at JSON loading: validate/frame inspection exits
2 with a root `integer_too_large` diagnostic and null frame/peer. A direct Python
stored-message rejection is `invalid_schema` at the message path.

All floating-point message values must be finite, including in direct Python
dictionaries and nested lists. `NaN` and positive/negative infinity raise
`CassetteError` with `non-finite JSON numbers are not supported` and code
`nonfinite_number` before copying, redaction or serialization in direct message
admission. Unknown fields and whole-value redaction pointers are checked too.
Direct stored-message validation retains this text with `invalid_schema` at
`/events/N/message`; wire JSON (including overflowing exponents) retains its
existing `invalid_json` rejection. Finite floats, including `1e308`, subnormals
and signed `0.0`/`-0.0`, remain whole. Rejection preserves recorder prefix,
pending IDs and budgets, and consumes no live replay interaction.

Recording rejects the whole frame before forwarding, appending or changing
pending IDs, atomically saves the accepted prefix as incomplete and exits/reports
1. Replay startup/live rejection exits/reports 2, without consuming a live
interaction; prior output may already exist. A parseable stored-message depth
failure is `invalid_schema` at `/events/N/message` in validate/frame inspection
(exit 1, null frame/peer); JSON beyond parser capacity is `invalid_json` at the
root (exit 2). Supported whole values, pointers and matching remain unchanged.

Limits: 1 MiB per newline-terminated wire message, 100,000 events, 64 MiB per
cassette. Stored messages are bounded after explicit redaction: their canonical
compact JSON (`ensure_ascii=False`, sorted keys, compact separators), encoded as
UTF-8 plus one LF, must be at most 1,048,576 bytes. This bound excludes pretty
indentation and event metadata, which count toward the file budget instead.
The recorder checks the transformed bytes even when no redaction paths match;
compact numeric reserialization can also expand a live line. Exactly the limit
is accepted. Larger messages are rejected whole before event append, budget or
pending-ID changes and live forwarding. Recording finalizes the valid prefix
atomically as incomplete, with exit/report 1 and the fixed diagnostic
`recorded message exceeds 1 MiB after redaction (including newline)`.
Rejected responses leave their accepted request pending; rejected requests
create no pending lifetime. Successful forwarding preserves the original live
bytes. JSON, routing fields and redaction placeholders are never truncated.

Replay checks each actual compact UTF-8 output frame after live ID
and owned progress-token rewriting, including its final newline: at most
1,048,576 bytes. An oversized frame is not written or truncated; replay stops
with stderr diagnostic and exit/report 2. Any valid prefix already emitted
remains; the matched interaction stays consumed and remaining interactions
stay unused. Generated replay errors share this output bound. Fault-suppressed
outputs are not serialized or emitted.

Recording budgets the exact pretty-printed UTF-8 representation plus final LF,
including all configured `redact_paths`. It preflights metadata before launching
the server or touching the destination; paths plus finalization reserve that
cannot fit cause exit/report 2 with
`recording metadata exceeds 64 MiB finalization budget`. No pointer is removed
or truncated. Event admission includes this metadata and the largest bounded
failure envelope, with equality accepted. Overflow rejects the whole event
before append, pending-ID changes or live forwarding, and atomically saves the
accepted prefix as a schema-valid incomplete cassette with exit/report 1 and
`recording limit reached (64 MiB / 100000 events)`.

Final metadata holds at most eight reasons, each a JSON string of at most 256
UTF-8 bytes including quotes/escaping. Oversized diagnostics become
`recording failure (diagnostic omitted)`; excess reasons replace the last entry
with `additional recording failures omitted`. The same bounded list feeds the
stderr summary. The reserve covers null or signed 32-bit subprocess statuses,
complete/incomplete flags and empty/nonempty event-array formatting. Paths and
messages are preserved whole; the reserve can leave a small amount of unused
space even for a successful recording. Cassettes are held in memory;
the 64 MiB file bound is not a 64 MiB process-RSS guarantee. The message-depth
bound is admission before recursive copying, not streaming JSON parsing. Millisecond
values are informational; replay does not sleep or emulate wall-clock timing.

## Validation report v1

`agentwrench cassette validate PATH` checks cassette schema, JSON-RPC envelopes and
request/response integrity without replaying or executing anything. Its stdout
is one JSON object. The report version is independent of the cassette version:

- `report_version`: integer `1`.
- `cassette_version`: the parsed integer version, or `null` if absent, not an
  integer, or the file could not be parsed.
- `valid`: true only for a complete cassette passing all integrity checks.
- `event_count`: length of the parsed events array, otherwise `null`.
- `diagnostics`: array of objects with `code`, `message`, and `location`.

Locations contain `path`, an RFC 6901 JSON Pointer (empty string means the file
root). Frame locations additionally contain `frame_index`, the zero-based events
array index; they never trust a corrupt stored `seq`. A `duplicate_request_id`
diagnostic includes `related`, an array containing the original request's
location. No request/response bodies or IDs are copied into diagnostics.

Exit 0 means no diagnostics. Exit 1 means a parseable file failed validation:
`unsupported_version`, `invalid_schema`, `duplicate_request_id`,
`unmatched_response`, `unanswered_request`, or `incomplete_recording`. The scan
continues after ID errors, reports outstanding requests in frame order at EOF,
then reports the incomplete flag if set. A duplicate leaves the original
request pending; the next matching response consumes that original. There is no
guess about which pending request an orphan response was intended to answer.
Both directions and typed IDs are independent; completed IDs can be reused.

Schema validation stops at the first schema error because later frames cannot
be trusted; any earlier ID errors remain in the report. Unanswered-request and
incomplete-flag checks run only if the scan reaches EOF. Inspect/replay use the
same checks; counts-only inspect and replay preserve their existing fail-fast
behavior and exit codes. Counts-only inspect continues to allow structurally
sound incomplete recordings.

Exit 2 means `read_error`, `cassette_too_large`, or `invalid_json`. Version and
event count are `null`. JSON syntax errors, including physical truncation, have
one-based `line`/`column` and zero-based `char_offset` from the JSON parser.
Invalid byte encodings have a zero-based `byte_offset` when available. These
locations identify the parse failure, not a recovered frame; no partial JSON
salvage or heuristic claim that every syntax error is truncation is made.
Duplicate keys, non-finite numbers, invalid Unicode and JSON beyond parser
recursion capacity produce `invalid_json`, but may have only a root location.
Parseable messages beyond 64 container levels produce `invalid_schema` at the
message path instead.

The existing file/event/message limits apply. This command checks recording
integrity, not method-specific MCP schemas, authentic provenance or replay
support: a complete server-initiated exchange is valid even though replay of
server-initiated requests is unsupported.

## Frame inspection report v1

`agentwrench cassette inspect PATH --frame N` reads and validates one bounded snapshot,
then prints the validation report v1 envelope above with two additional fields:

- `frame`: `{"frame_index": N, "path": "/events/N", "event": <stored event>}`.
- `peer`: the same structure for the matching request/response, or `null`.

For the two-event ping cassette at the top of this document, `--frame 1`
returns `frame.frame_index: 1` and `peer.frame_index: 0`, preserving both complete
events, including `seq`, `ms`, `direction`, message ID `7`, payloads and unknown
event fields. No ID rewriting, additional redaction, notification ownership or
timing simulation is applied. Notifications always have a null peer, even if
their params mention a request ID or progress token. Requests and responses can
be selected in either direction, including server-initiated exchanges that
replay does not support.

Pairing uses exactly the validation scan: a response consumes the pending
request with the same typed ID in the opposite direction. Completed IDs can be
reused; each occurrence retains its own frame identity. A duplicate in-flight
request never overwrites the original, and gets no peer. Orphan responses and
unanswered requests also have no peer. Diagnostics describe the whole cassette,
not only the selection. Exit 0 means complete and valid; incomplete recordings
and ID-integrity failures still return selections with diagnostics and exit 1.

Schema/version failures exit 1 with both selections null, even when the selected
frame precedes the error; the command does not infer pairs from an untrusted
schema. Read/parse/file-size failures exit 2 with both selections null. These input
checks take precedence over selector checks. Otherwise N must contain only ASCII
decimal digits (leading zeros allowed) and be less than `event_count`. Invalid
indices exit 2, set `valid: false`, leave both selections null, and append:

```json
{"code":"invalid_frame_index","message":"frame index must be a nonnegative decimal integer less than event_count","location":{"path":"/events"}}
```

Existing integrity diagnostics are retained. Reports are a single JSON object
on stdout with no stderr. Missing option values and other CLI usage errors still
use argparse stderr. The operation never reads stdin, starts a replay/server,
executes stored messages or metadata, or writes files. Existing file, message
and event bounds apply. Without `--frame`, inspect keeps its counts-only contract.

## Matching

Default `params` matching uses `(request vs notification, method, canonical
params JSON)`, with ordinary request progress-token values normalized as below.
Object keys are sorted recursively, array order and numeric
representations remain significant elsewhere. Missing params equals `{}`; repeated keys
consume recorded interactions once each, in original FIFO order. Calls with
different params may be replayed in a different order. `sequence` requires the
next unused client event to match both method and canonical params. Client
notifications, including `notifications/initialized`, are interactions and count
as unused under strict mode. Unknown notifications generate diagnostics but no
response, as required by JSON-RPC.

Initialize is the deliberate exception: client identity and capabilities are
ignored; it returns the next recorded initialize response. The live requested
protocol version is compared to the recorded request's version (not to the
version the server negotiated in its recorded result, which is replayed as is).
A mismatch warns in default mode; strict mode returns error `-32002`, leaves
the interaction unused and fails the process. No capabilities are synthesized.
A recorded ping is consumed like other requests; an extra ping is answered with
`{}` without consuming another interaction or breaking sequence matching.

Recorded response IDs become the live request IDs. Result/error payloads are
otherwise unchanged. Every server notification is assigned **once**:

1. A progress notification must have a valid token matching exactly one pending
   request and belongs to it; otherwise replay refuses the cassette.
2. Other notifications belong to the most recent still-pending request.
3. With none pending, they belong to the latest client event (including a client
   notification); those before any client event are emitted at startup.

Outputs within an interaction preserve recorded order, including non-progress
notifications before or after its response. Concurrent response completion order,
cross-call notification timing and cancellation request IDs are not
remapped/scheduled. Unrecorded cancellation is an unmatched notification. This is a
deterministic transcript server, not a timing/concurrency simulator.

### Progress-token binding

For ordinary client requests, both matching modes normalize **only** the value
at `params._meta.progressToken`, after configured redaction. Its presence and
all other params/metadata remain part of the canonical key; `_meta` itself is
preserved. Missing and empty `_meta` remain distinct. Requests without tokens
keep the previous matching behavior. Initialize keeps its existing identity/
capability/version exception; `params.task` and `tasks/*` are excluded from
binding and keep exact tokens. This does not implement Tasks or extend token
lifetimes after a task-creation response.

A matched ordinary interaction binds its stored token to the live string or
integer token, even across types. Its `notifications/progress.params.progressToken`
is replaced in a deep copy; progress/total/message, other notification fields,
result/error payloads and within-interaction order stay unchanged. Response IDs
still become live IDs. No global mapping survives an interaction, so FIFO
repeats and completed token reuse cannot inherit an earlier binding. This also
retains the existing owner selection for overlapping recorded calls without
adding concurrent output scheduling.

Replay checks tokens with exact types (string or integer, never bool/float/null).
Integer `7` differs from string `"7"`. During cassette indexing, active tokens
must be unique and every server progress notification must reference an active
recorded request. Missing/invalid tokens, duplicate active tokens, orphan
notifications and progress after the response are replay errors: CLI exit 2
before any output. Invalid live request tokens also exit 2. Diagnostics do not
print token values. Live/recorded token presence mismatches are unmatched
requests (`-32001`, interaction not consumed; strict exit 1), including pings
that would otherwise use the extra-ping fallback. Other parameter differences
retain ordinary unmatched behavior. Existing explicit fault scenarios still
match/consume first and suppress all target-owned output.

The v1 storage schema and recorder are unchanged: valid old cassettes need no
rewrite. Record/validate/inspect still check envelopes and ID integrity, not all
method-specific schemas. A cassette can validate yet be refused for unsupported
progress ownership at replay, just as for server-initiated requests. Previously
guessed orphan/ambiguous progress is intentionally rejected. If progress tokens
are redacted, the stored request and notifications must still have an unambiguous
matching token; replay cannot recover a removed or collapsed link.

A cassette containing server-initiated requests (sampling, elicitation, roots,
etc.) produces a warning and is refused with exit 2 at replay startup. Recording
remains full duplex and can capture their request/response exchanges. Refusing
the whole cassette prevents accidentally dropping a required callback.

## Transport fault scenario v1

`replay --fault-scenario FILE` opts into a separate, bounded JSON document:

```json
{"version":1,"request_seq":8,"kind":"timeout","duration_ms":1000}
```

Only these fields are accepted. `version` is integer 1; `request_seq` is an
integer event index identifying a client request (both `method` and `id`), never
a notification or response. `kind` is `timeout` or `eof`. Timeout requires integer
`duration_ms` from 1 through 60000; EOF forbids `duration_ms`. Booleans are not
integers. The file is limited to 4096 bytes and uses the same strict JSON parser
as cassettes. Invalid schema, unavailable files and invalid targets fail with
exit 2 before startup output. Existing complete-cassette and replay-support
requirements still apply. The fault file is not part of cassette format v1;
unknown cassette metadata cannot activate transport faults.

Normal params/sequence matching, redaction, typed/live IDs, FIFO repeated calls
and initialize version checks run first. An unmatched call, extra synthesized
ping, or strict initialize mismatch does not trigger a fault. When the matched
interaction has `seq == request_seq`, it is consumed once, all its owned output
is suppressed (response and notifications), and replay stops processing input.
Earlier output is unchanged. EOF closes stdout through normal process shutdown.
Timeout keeps stdout open but silent for `duration_ms`, then does the same;
the watchdog runs even with open or closed client stdin. SIGTERM/SIGINT can end
the stall early. Scheduling and the existing one-second stdout-drain bound
apply; this does not promise exact wall-clock delivery or simulate recorded
timing. Clients must use a shorter request timeout to observe a timeout before
the watchdog's EOF. No JSON-RPC error frame is fabricated for either fault.

An armed scenario that never triggers exits 1 even in non-strict mode. A
triggered scenario alone exits 0. Strict unmatched/unused checks remain in force,
including interactions beyond the terminal target; use a cassette ending at
the target interaction or explicitly use non-strict replay when that suffix is
intentional. In params mode it is still possible to consume other calls before
the target, regardless of recorded event order.

When a valid scenario is configured, `--report` adds `fault`, containing its
fields plus boolean `triggered`. This distinguishes a deliberately withheld
response from an unexercised test. A report may not overwrite either input file
(including symlink aliases). Without a scenario, the exit report and successful
stdout bytes retain their existing format. Each process arms one terminal fault;
there are no retries, multiple-fault schedules, recovery, or incomplete-cassette
fault inference. The Python `agentwrench.cassette.replay.Replayer` accepts the same `fault_scenario` object
and exposes `fault_triggered`; `handle` returns no messages for the target and
refuses later calls. The CLI owns the actual stall and transport closure.

## Redaction

`--redact-path /params/arguments/token` applies an RFC 6901 JSON Pointer relative
to **each JSON-RPC message**, on a deep copy before storing it. Escapes `~0`
and `~1` address `~` and `/`; array indices are supported. Nonexistent paths are
no-ops. There are no wildcards, regexes, environment expansions or code hooks.
Matched values become the string `<REDACTED>`; the live wire is untouched.

Only fields below `/params`, `/result` (including the whole result) and
`/error/data` or `/error/message` may be redacted. Routing fields and error codes
cannot be redacted. The entire params object and its entire `_meta` object
cannot be redacted, since they must remain objects. Paths are stored in the
cassette and applied to incoming live messages before matching; new secrets at
those paths therefore match the redacted request. Responses remain redacted.
Removing required output schema fields or protocol versions can make a cassette
unusable to a real SDK. Choose data fields, not schema/lifecycle fields.

A JSON pointer is not automatic secret detection. A secret repeated in another
field stays there unless that field is separately redacted. Redact outbound
content too, where appropriate. Real server stderr is inherited and may contain
anything that server logs; it is never copied into the cassette.

## Files and process lifecycle

The output path is explicitly supplied by the caller. Saves use a same-directory
private (0600) temporary file, fsync and atomic replace; a final symlink is
replaced rather than followed. Parent directories are trusted caller-selected
locations (the CLI is not a filesystem sandbox). The pytest marker accepts only
plain filenames, confines them to `cassettes/` next to the test file and rejects
symlinked cassette files/directories. Do not share output paths between workers
or writers; no cross-process locking is provided.

EOF closes child stdin. SIGTERM/SIGINT request the same graceful finalization.
The proxy waits `--shutdown-timeout` (default 2 seconds), then terminates/kills
its own child process group if necessary. Nonzero child exits, timeouts,
framing/pairing errors, undrained output or pending requests mark the recording
incomplete and return 1. A clean EOF or signal with all exchanges paired and
child exit 0 produces a complete cassette. SIGKILL/power loss cannot finalize;
atomic replacement leaves a previous cassette unchanged or no new file.

Replay strict checking runs at EOF and SIGTERM/SIGINT. It lists unmatched and
unused interactions on stderr, omitting params. `--report FILE` writes an atomic
JSON object with `exit_code`, `unmatched`, `unused`; the pytest fixture checks it
after the client has shut down the subprocess. Stdout remains MCP messages only.
No subprocess is launched by `replay` or `inspect`.

Exit codes: `0` successful recording/replay/inspection, `1` incomplete recording
or strict replay failure, `2` invalid/unsupported input or I/O failure. JSON-RPC
unmatched requests receive `-32001`; this diagnostic response does not by itself
make non-strict replay exit nonzero.
