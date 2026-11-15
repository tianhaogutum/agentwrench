# agentwrench export

`agentwrench export` converts local Codex, Claude Code, and Cline sessions into
portable trajectories (ATIF JSON or normalized `trajport/v1` JSONL) and redacted
Markdown hand-off briefs. It can strictly validate complete raw JSONL without an
export, merge normalized exports into a timestamp-ordered stream with source
provenance, and render merged exports as readable Markdown transcripts. The
export module uses only the Python standard library at runtime, makes no network
access, and does not require any agent to be installed.

## Install

```sh
pip install agentwrench          # or: pipx install agentwrench
agentwrench export --version     # agentwrench export 0.1.0
```

`uvx agentwrench export ...` runs it without a permanent install, and
`python -m agentwrench export ...` is equivalent to the console command.
Requires Python 3.11 or newer.

## Run

The sample inputs below are synthetic sessions shipped in `examples/export/` of
the repository; run the commands from a checkout root.

```sh
# Conversation only: no reasoning, tool calls, results, or raw metadata.
agentwrench export convert examples/export/codex/rollout-simple.jsonl --to md --view conversation

# Inspect recorded tool calls and their outputs.
agentwrench export convert examples/export/codex/rollout-simple.jsonl --to md --view tools

# OUT must not already exist. Output files are created with mode 0600.
agentwrench export convert examples/export/claude/session-simple.jsonl --to atif -o trajectory.json

# Cline input is one task directory, not the account-wide history index.
agentwrench export summary examples/export/cline/task-1

# Portable normalized JSONL; one metadata record followed by step records.
agentwrench export convert examples/export/cline/task-1 --to jsonl --redact strict
```

Conversation Markdown includes this content inside inert code fences (excerpt):

````md
## User · 1

```
2026-01-01T00:00:01Z
```

```
Fix the failing test.
```

## Assistant · 2

```
2026-01-01T00:00:07Z
```

```
The test failed because the return value was wrong.
```
````

`summary` extracts the first/last user requests, last assistant response, files
named in patch/edit/write calls, commands with recorded exit status, and recorded
token usage. For the supplied examples it identifies `src/math.py`,
`python -m pytest -q` (exit 1), and 120 input / 30 output tokens. It does not
execute or verify those actions and does not infer intent with an LLM.

### Options and output

```text
agentwrench export convert SESSION --to atif|md|jsonl
    [--from auto|codex|claude|cline] [--view conversation|tools|full]
    [--redact default|strict|off] [--include-sidechains] [--strict-jsonl]
    [--max-output-chars N] [-o OUT]
agentwrench export merge EXPORT.jsonl EXPORT.jsonl [MORE.jsonl ...] [-o OUT]
    [--check | --max-output-bytes N]
    [--since TIMESTAMP] [--until TIMESTAMP]
    [--sort-temp-dir PATH] [--output-temp-dir PATH]
    [--external-sort [--sort-buffer-bytes N] [--max-sort-temp-bytes N]]
agentwrench export render-merge MERGED.jsonl [--view conversation|tools|full]
    [--source N ...] [--since TIMESTAMP] [--until TIMESTAMP]
    [-o TRANSCRIPT.md] [--output-temp-dir PATH] [--max-output-bytes N]
agentwrench export summary SESSION
    [--from auto|codex|claude|cline] [--redact default|strict|off]
    [--include-sidechains] [--strict-jsonl] [--max-output-chars N] [-o OUT]
agentwrench export validate SESSION
    [--from auto|codex|claude] [--include-sidechains] [--fail-on-warning]
agentwrench export validate-merge MERGED.jsonl
```

`-o` is also spelled `--output`. `agentwrench export <subcommand> --help` lists
each subcommand's options.

`--max-output-chars` limits **each tool result**, including copies in raw record
metadata, to N characters plus a truncation notice (default 20000; 0 unlimited).
It is not a limit on the whole document. Redaction happens before truncation.
`full` retains conversation, tool activity, reasoning summaries and metadata;
`tools` retains calls/results; `conversation` retains only user/assistant text
and media placeholders. Step IDs are renumbered after filtering. A view with
no supported steps is an error. Markdown presents the selected activity and
export diagnostics; detailed raw records are available in ATIF/JSONL.

Successful conversion/summary output goes to stdout or the requested new file.
Machine-readable warning/redaction counts go to stderr (prefixed
`agentwrench export: `) and into export metadata. Exit 0 means conversion
succeeded (possibly with counted warnings); exit 2 means invalid input, an
unsupported/empty format or view, or an I/O error. Existing outputs and symlinks
are refused. Parent directories are not created. Failure messages do not echo
input contents or private filesystem paths.

## Render a merged transcript

`render-merge` reads **one `trajport/merge-v1` JSONL export**, including an interval
export produced by `merge`. It validates the merged envelope directly; it never
passes the file to a raw Codex/Claude/Cline adapter.

```sh
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl -o merged.jsonl
agentwrench export render-merge merged.jsonl -o transcript.md
# Inspect only declared input 2 from the merged file, without the original inputs:
agentwrench export render-merge merged.jsonl --source 2 -o source-2.md
# Focus on source 2 in [10:01, 10:03) UTC, still using only the merged file:
agentwrench export render-merge merged.jsonl --source 2 \
  --since 2026-01-01T10:01:00Z --until 2026-01-01T10:03:00Z -o window.md
# Keep only conversation messages, or only tool activity, from the same export:
agentwrench export render-merge merged.jsonl --view conversation -o conversation.md
agentwrench export render-merge merged.jsonl --view tools -o tools.md
# Or publish to stdout only after all validation and output-byte checks succeed:
agentwrench export render-merge merged.jsonl --output-temp-dir /existing/writable/scratch
```

The transcript starts with a `# agentwrench export merged trajectory` heading. Each
recorded event has a User/Assistant/System heading with its original step ID
and an explicit **Source / session** fenced label containing the input ordinal,
session ID, original source line and step ID. The source catalogue lists the
recorded agent names/versions. Message text, reasoning summaries, tool calls and
grouped tool results use the existing normalized Markdown renderer and inert
escaping (HTML/control escaping and fences longer than embedded backticks).
Unicode text is retained. JSON argument formatting is a presentation of parsed
values; this is not original-byte reconstruction. Detailed unrendered metadata
remains in the input export.

The command preserves the **recorded event order**, including equal timestamp
instants, and displays original timestamp strings without timezone rewriting.
It rejects decreasing timestamps rather than sorting. Sessions are never joined,
step IDs are never renumbered, and matching IDs or tool-call names in independent
sessions remain independent. No new redaction, truncation, missing-message
inference or source-metric recomputation is performed; review supplied content
before sharing. A header-only interval export is valid: it lists its sources and
states that no events were recorded in this merged export, without inventing
conversation or changing whole-source metrics.

`--source N` selects events by their **declared input ordinal** in the merged
header. Repeat it for multiple sources (`--source 2 --source 1`); repetitions
are ignored. The transcript keeps the full source catalogue and adds an explicit
selection section listing unique selected ordinals in numeric order. Event order,
original step IDs, timestamps and per-event labels stay as recorded. With no
`--source`, all declared sources are selected.

`--since TIMESTAMP` is an **inclusive** lower bound; `--until TIMESTAMP` is an
**exclusive** upper bound. Bounds use the existing timezone-bearing RFC3339
syntax with at most six fractional digits and compare UTC instants, including
offset-equivalent times. An omitted endpoint is unbounded; equal instants select
no events. Invalid or reversed bounds fail with exit 2 and a fixed diagnostic
before opening input, output or staging storage, without echoing the value.
Time and source selection are combined: an event must satisfy both. Equal-time
events retain file order, original timestamp strings and IDs.

With either bound, the transcript adds a timestamp-selection section containing
both original endpoint strings (`null` for an omitted endpoint), explains the
inclusive/exclusive semantics, and states whether all sources or the explicit
source selection applies. The complete source catalogue remains visible. An
empty result explicitly states that no events match the active selection in this
merged export. No claim is made about events outside this export or whole-source
metrics. Omitting both time bounds retains the existing selected/unselected
Markdown and diagnostics bytes.

`--view conversation` keeps nonempty user and assistant messages, including
media placeholders, and hides reasoning, tool activity and step metadata.
`--view tools` keeps steps with tool calls or a nonempty observation from any
role, while hiding messages, reasoning and step metadata; recorded calls and
results retain the existing normalized presentation. Neither view joins
sessions, renumbers step IDs, or infers missing events. Fields hidden by the
view and events excluded by source/time selection are still fully validated.
`--view full` is the default and preserves the existing transcript bytes when
no other new context is active. A non-full view adds an explicit view section;
source/time/view selection is combined, and empty views are valid with an
explicit no-matching-events statement. When a view is active,
`excluded_events` counts source, time and view exclusions once per event.
Diagnostics count events in this export only and always report all declared
sources. No new redaction is applied.

Every event is validated, including excluded events and the entire tail, before
stdout publication. A late error still removes a new output file. Nonpositive
ordinals and noninteger values fail; unknown ordinals fail after complete header
validation. A valid selected source may have no events in this export (including
an interval export): the transcript states that no events from the selected
sources were recorded. This says nothing about the source's whole session.

With explicit source selection, JSON diagnostics on stderr and in the transcript
add `selected_sources` (unique sorted ordinals). With either time bound, they add
`since` and `until` (original strings or `null`). Either selection adds
`excluded_events`: the number of validated events omitted from this export by
source or time, counted once even if both exclude the event. `rendered_events`
counts displayed events; `sources`
still counts **all declared sources**. These are export/render counts, not
recomputed whole-source metrics. `--max-output-bytes` counts only the resulting
Markdown, including its catalogue, source/endpoint context and diagnostics;
input limits still cover the entire merged export, including the excluded tail
beyond `--until`. A window does not bypass malformed records, provenance,
duplicate-identity/location or timestamp validation.

For example, the `--source 2` command above reports
`agentwrench export: {"excluded_events": 2, "rendered_events": 2, "selected_sources": [2], "sources": 2}`
on stderr.

Validation requires:

- One merge-v1 header with 2–64 normalized trajectory headers, unique input
  ordinals covering 1 through the source count, known nonempty session IDs,
  text agent names/versions and object-valued source extras when present.
- Every event references a declared source, matches its session ID, and includes
  an original physical source line greater than 1. Step and provenance IDs must
  be equal **positive JSON integers**: booleans, floats and strings are rejected
  rather than coerced. Duplicate `(session_id, step_id)` identities or duplicate
  `(input, line)` locations fail; repeated IDs across different sessions are valid.
- A normalized step with type `step`, user/agent/system source, text message and
  the existing timezone-bearing RFC3339 timestamp syntax (at most six fractional
  digits). Rendered optional fields are checked: text reasoning, list-valued
  tool calls with unique text IDs/names and object arguments, and object
  observations with list-valued results, text content, nullable text call IDs
  and boolean orphan flags. Step/call/result extras and metrics must be objects
  when present. Call references are displayed as recorded; no cross-event or
  cross-session result matching is attempted.

Strict UTF-8/LF-framed parsing rejects malformed tails, duplicate JSON keys and
nonfinite numbers. CRLF, a first-line BOM, JSON whitespace blank lines, and a
valid final record without LF are accepted. The existing bounds apply to this
single input file: **64 MiB/file, 8 MiB/physical line, 200000 nonblank records**,
including the header. Identity/location sets are held in memory within those
bounds. This validates the documented envelope and rendered field subset, not
all retained vendor metadata or an exhaustive external schema.

`-o` creates a new 0600 Markdown file and refuses existing files and symlinks;
handled failures remove the new file. Stdout uses the same private staging path
as merge: all successful validation precedes publication. `--output-temp-dir`
selects an existing writable staging root, defaults to platform temporary
storage, and conflicts with `-o` before I/O. Handled success/failures clean staging
and preserve the root and its contents. `--max-output-bytes N` optionally bounds
all rendered UTF-8 bytes, including headings, escaping, diagnostics and LF;
positive integer, unlimited by default. Exact limits succeed; overflow fails
before the crossing write. These options do not change `merge` or `convert`.

Success exits 0 with `{rendered_events, sources}` JSON on stderr. Invalid input,
output limits and I/O failures exit 2 with safe diagnostics and no success counts.
Input/limit failures publish no stdout. An OS failure during the final stdout
copy can leave a validated prefix; abrupt termination or cleanup refusal can
leave artifacts. No combined temporary-disk or fixed-memory guarantee is claimed.

## Validate an existing merged export

`validate-merge` checks **one `trajport/merge-v1` JSONL file** without rendering
Markdown, staging stdout, creating an output file or temporary index, or opening
any original normalized source. A shared export alone is sufficient:

```sh
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl -o merged.jsonl
# Later, with only merged.jsonl available:
agentwrench export validate-merge merged.jsonl
# stderr: agentwrench export: {"sources": 2, "validated_events": 4}
# stdout is empty; exit 0
```

The example inputs contain two events each. Success reports exactly
`{validated_events, sources}` JSON on stderr: events present in this export and
all declared sources, including sources with no retained events. Header-only
interval exports succeed with zero validated events. Counts do not describe
whole original sessions or recompute retained source metrics.

Validation reuses the strict merged-envelope reader described above: header,
field shapes, provenance consistency, unique identity/location, nondecreasing
timestamps, strict JSON and **every record through EOF**, including malformed
unterminated tails. Invalid input exits 2 with a safe physical-line diagnostic
(I/O failures use a safe generic diagnostic), empty stdout and no success
counts. No presentation or output options are supported: `--source`, `--since`,
`--until`, `--view`, `-o`/`--output`, `--output-temp-dir` and
`--max-output-bytes` are rejected as usage errors (exit 2) before input I/O. Raw
sessions use `validate`; multiple normalized exports use `merge --check`.

The same **64 MiB/file, 8 MiB/physical line, 200000 nonblank records including
the header, and 2–64 declared sources** bounds apply. Input must be a regular
file; LF/CRLF, first-line BOM, JSON whitespace blank lines and valid final
records without LF are supported. Identity/location sets remain in memory;
validation-only avoids transcript allocation but does not promise fixed memory.
This checks the documented envelope and renderer-consumed field subset, not
all retained metadata, exhaustive external schemas, authentic provenance,
original file existence or completeness. It does not redact or modify input.

## Validate without an export

`validate` checks a complete raw Codex or Claude JSONL session using strict JSON
parsing and the same supported adapter rules as conversion. It always rejects
malformed records, including an unterminated corrupt tail, duplicate JSON fields
and nonfinite numbers. It does not offer permissive tail recovery.

```sh
printf '%s\r\n%s' '{"type":"user","message":{"role":"user","content":"hello"}}' '{"type":"user","message":{"role":"user","content":"done"}}' > complete.jsonl
agentwrench export validate complete.jsonl
# Exit 0; empty stdout; stderr (one line):
# agentwrench export: {"format": "claude", "records": 2, "steps": 2, "warning_count": 0, "warnings": {}}

printf '\r\n%s' '{"type":' >> complete.jsonl
agentwrench export validate complete.jsonl
# Exit 2; empty stdout; stderr: agentwrench export: error: invalid JSON at line 3
```

Validation creates no export or temporary output files and does not modify the
input. Success diagnostics use the `agentwrench export: ` prefix followed by a
JSON object on stderr. `records` counts all nonblank input objects, including
metadata, ignored records and excluded sidechains. `steps` counts normalized
adapter steps before view filtering. `warnings` maps warning codes to occurrence
counts; `warning_count` is their sum, not the number of distinct codes. By default,
warnings do not fail validation. With `--fail-on-warning`, a nonzero
`warning_count` exits 2 with exactly the same JSON counts on stderr, empty stdout
and no output files. Warning-free sessions exit 0 in either mode. This policy
uses warnings after the selected sidechain inclusion; it does not change parsing
or malformed-input error diagnostics. Counts do not include redaction or export
transformations.

```sh
printf '%s\n' '{"type":"user","message":{"role":"user","content":"hello"}}' '{"type":"future_record"}' > warning.jsonl
agentwrench export validate warning.jsonl
# Exit 0; empty stdout; stderr:
# agentwrench export: {"format": "claude", "records": 2, "steps": 1, "warning_count": 1, "warnings": {"unknown_record": 1}}
agentwrench export validate warning.jsonl --fail-on-warning
# Exit 2; identical stderr, empty stdout, no output files.
```

LF/CRLF, an initial UTF-8 BOM, JSON whitespace blank lines, Unicode separators
inside strings and a valid final record without LF are accepted. Syntax errors
report physical LF-based line numbers, including blank lines. Empty input,
undetectable/ambiguous format, no supported steps, adapter errors (such as duplicate
tool call IDs), and existing input size/record bounds fail with exit 2.

This is validation of the supported adapter subset, not an exhaustive vendor
schema or proof of a clean agent shutdown. Unknown records remain warnings.
Claude sidechains are excluded from adapter steps/checks unless
`--include-sidechains` is given; **every record is still parsed strictly**.
Auto-detection uses the first 100 records, as in conversion; use `--from` to
select the adapter explicitly. Cline directories, normalized/merged exports and
ATIF are outside this command's scope. There are no output, view or redaction
options. `convert` and `summary` keep their permissive defaults.

The Python API returns the same diagnostic dictionary (including counted warnings)
or raises `InputError`; the exit-code policy is CLI-only:

```python
from agentwrench.export import validate_session

counts = validate_session("complete.jsonl", "auto", include_sidechains=False)
```

## Streaming multi-file merge

`merge` accepts **2–64 normalized `trajport/v1` JSONL exports**, produced by
`convert --to jsonl`. Each input needs a known, nonempty session ID (`unknown`
is rejected) and at least one step. By default, step timestamps must be
nondecreasing within each input. It reads files incrementally; add
`--external-sort` to sort unsorted inputs using temporary disk runs. Neither
mode infers missing times.
Timestamps must use `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` or an explicit `±HH:MM`
offset, with at most six fractional digits. UTC instants determine ordering;
equal instants use command argument order, then physical record order within
that input. Original timestamp strings remain intact.

The supplied synthetic example has A1 at 10:00, A2 at 10:02, B1 at 10:01, and
B2 at 10:02:

```sh
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl -o merged.jsonl
# stderr: agentwrench export: {"merged_events": 4, "sources": 2}
# Events: A1, B1, A2, B2. Swapping the inputs swaps the final timestamp tie.
```

Output uses **`trajport/merge-v1`**, a separate envelope format. The first record
is `{"type":"merge","schema_version":"trajport/merge-v1","sources":[...]}`;
each source is `{"input":1,"trajectory":<original trajectory header>}`. Subsequent
records contain an unchanged original step plus explicit provenance (keys are
written in sorted order), for example:

```json
{"provenance": {"input": 1, "line": 3, "session_id": "A", "step_id": 2}, "step": {"extra": {}, "message": "A2", "source": "user", "step_id": 2, "timestamp": "2026-01-01T10:02:00Z", "type": "step"}, "type": "event"}
```

`input` is the 1-based command argument number and `line` is the physical LF-based
line number, including blank lines. No input filesystem paths are added. Step
IDs and tool call/result IDs remain scoped to the original session; records are
not flattened into a single agent's trajectory or renumbered. Grouped tool
results stay attached to the original call step. Headers, metrics, raw metadata,
and extensions are retained as supplied; aggregate metrics are not recomputed.
Merge checks the envelope and required step fields, not the full nested tool
payload schema. It does **not** redact or truncate content again: select views,
redaction and result limits during `convert`, before merging.

Event identity is `(session_id, step_id)` in the supplied exports. Any repeated
identity, even with identical content, fails instead of dropping an event.
Disjoint fragments of one session are allowed. Avoid combining different views
of the same session, since `convert` renumbers IDs after filtering. For example:

```sh
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/conflict.jsonl
# Exit 2; stderr: agentwrench export: error: duplicate event identity: input 1 line 3 conflicts with input 2 line 2
```

Malformed records (including an unterminated malformed tail), missing/invalid
timestamps, and (without `--external-sort`) decreasing timestamps fail with
exit 2 and input/line locations. No record is skipped. On input failure stdout is empty and a newly
created output file is removed. Existing output files and symlinks are refused;
successful output files have mode 0600. Stderr reports source/event counts.

By default, the merge iterator keeps one pending event per input and indexes identities in
a private temporary SQLite database. It does not collect all steps or identities
in Python memory. Stdout is staged in a private temporary file and copied only
after validation, so it becomes available after all inputs have been read.
Temporary disk space is required for the identity index and staged stdout;
`-o` writes incrementally to the requested new file and removes it on failure.
The 64 MiB/file, 8 MiB/line and 200000 records/file (including the header) limits
apply separately to every input. LF, CRLF, an optional initial UTF-8 BOM and
valid final records without LF are supported. Raw agent logs, ATIF, and merged
envelopes are not merge inputs; use `convert` for raw agent logs first. This
command does not add an import/resume or archive round-trip capability.

`--output-temp-dir PATH` selects an **existing writable directory** for the
private mode 0600 stdout staging file, in ordered or external-sort mode,
including interval exports and `--max-output-bytes`. The file is created directly
under that root (it may be anonymous/unlinked on the platform). Omission retains
platform-default temporary storage (`TMPDIR` where supported). This is independent
of `--sort-temp-dir`, which selects only identity-index/run storage; neither
option changes the run-only quota or the serialized output-byte limit.

```sh
mkdir output-scratch index-scratch
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl \
    --output-temp-dir output-scratch --sort-temp-dir index-scratch \
    --since 2026-01-01T10:01:00Z --until 2026-01-01T10:02:00Z
# Only B1; stderr: agentwrench export: {"excluded_events": 3, "merged_events": 1, "sources": 2}
```

Relative paths and symlinks to existing directories are accepted. The selected
root is never created, removed or emptied; existing contents and permissions
are preserved. Invalid/unusable roots exit 2 with
`agentwrench export: error: output temporary directory must be an existing writable directory`,
empty stdout and no fallback to another root. `--output-temp-dir` conflicts with
`--check` and `--output`/`-o` before input, root or output I/O, since neither uses
stdout staging. It is a CLI serialization option; `merge_jsonl` remains unchanged.

All input validation and output-byte checks finish before stdout copying starts.
Staging is closed/removed on success and handled failures, including write, seek
and stdout-copy errors. A copy error can leave bytes already published to stdout;
publication is not transactional once copying begins. Abrupt termination or a
filesystem refusing cleanup may leave temporary artifacts. There is no combined
temporary-disk quota or free-space guarantee.

Use `--since TIMESTAMP` (inclusive) and/or `--until TIMESTAMP` (exclusive) to
select a timestamp interval for the merged export:

```sh
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl \
    --since 2026-01-01T10:01:00Z --until 2026-01-01T10:02:00Z
# Only B1; stderr: agentwrench export: {"excluded_events": 3, "merged_events": 1, "sources": 2}
```

Endpoints use the same timestamp syntax as events, including offsets and up to
six fractional digits, and compare UTC instants. Omitted endpoints are unbounded.
Invalid endpoints or a lower bound later than the upper bound exit 2 before
input, output or temporary-storage I/O; diagnostics do not echo endpoint values.
Equal bounds are allowed and select no events. A zero-event result is a valid
metadata-only `trajport/merge-v1` export: the original merge header retains **all**
source headers, even sources with no retained events. Input exports still require
at least one step each. Retained steps, provenance and deterministic order are
unchanged; source metrics/metadata are not recomputed for the interval.

**Every input event is still validated**, including events outside the interval:
schema/required fields, timestamps, default nondecreasing order and duplicate
identities across all events. Malformed late records and duplicates among excluded
events fail with the same no-published-output behavior. Both ordered and
`--external-sort` modes support selection; external sorting still sorts all
events and charges all runs to the run quota. Selection does not reduce input
limits or scratch-storage requirements. `--max-output-bytes` counts only the
header and retained events, including LF; the header must fit even for an empty
result. Success stderr adds `excluded_events` when either endpoint is supplied;
`merged_events` counts retained events. With no interval options, output and
diagnostics remain byte-identical. This selection policy is CLI-only; the
`merge_jsonl` dictionary iterator still yields all validated events.

Add `--check` to validate normalized exports without producing a merged export:

```sh
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl --check
# Exit 0; empty stdout; stderr: agentwrench export: {"merged_events": 4, "sources": 2}
```

This consumes **all inputs and events** through the same merge iterator, checking
envelopes, required fields, timestamps and duplicate identities. Success reports
the same source/event-count JSON on stderr. Invalid inputs exit 2 with the same
safe input/physical-line diagnostics, including late malformed records and
duplicate identities; no success counts are emitted on failure. Stdout stays
empty, with no output staging or output file. `--check` conflicts with `-o` /
`--output`, `--output-temp-dir`, `--max-output-bytes`, `--since` and `--until`;
these combinations are rejected before I/O. Validation-only checks do not select
an interval.

Validation still requires temporary disk for the private SQLite identity index.
Ordered inputs use the default streaming mode; unsorted inputs need
`--external-sort`, which also uses temporary runs.
`--sort-temp-dir` works with ordered or externally sorted checks. The
`--sort-buffer-bytes` and `--max-sort-temp-bytes` options apply
with external sorting, including their bounds and run-only quota. The private
index/run child is cleaned on success and handled failure; inputs and user
scratch-root contents are preserved. This checks the same supported envelope
and required step fields as merge, not the full nested payload schema.

`--max-output-bytes N` optionally limits the **total serialized UTF-8 output**
in either merge mode, to stdout or `-o`. N must be a positive integer; omission
is unlimited. The count includes the merge header, every event envelope and
each LF. A row that would cross the limit is rejected before writing it to the
stdout staging file or requested output. An exact byte boundary succeeds.
Exceeding the limit exits 2 with `agentwrench export: error: merge output byte limit exceeded`,
empty stdout, removal of the new output and cleanup of the private index/run
directory. Existing outputs, symlinks and scratch-root contents are preserved.

```sh
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl \
    --max-output-bytes 1048576 -o bounded.jsonl
```

This limit is independent of the external-sort run quota: it includes headers,
but excludes runs, the SQLite index and filesystem overhead. It bounds logical
output bytes, not total temporary disk, process memory or per-row serialization
allocations. `--output-temp-dir` selects stdout staging storage independently.
The `merge_jsonl` API yields dictionaries and has no serialization byte limit;
API callers choose their own output policy.

### Opt-in external sorting

The `a-unsorted.jsonl` example contains A2 at 10:02, A1 at 10:00 and A3 at
10:02, in that physical order:

```sh
mkdir sort-scratch
agentwrench export merge examples/export/merge/a-unsorted.jsonl examples/export/merge/b.jsonl \
    --external-sort --sort-temp-dir sort-scratch \
    --sort-buffer-bytes 8388608 --max-sort-temp-bytes 1048576 -o sorted.jsonl
# Events: A1, B1, A2, A3, B2.
# Original (input,line): (1,3), (2,2), (1,2), (1,4), (2,3).
```

Without the flag, this input fails with exit 2 at input 1 line 3
(`agentwrench export: error: merge input 1 line 3: timestamps are not nondecreasing`).
Sorting keeps the same `trajport/merge-v1` envelope and orders by UTC instant,
input argument number, then original physical line. Step IDs do not break
timestamp ties. Headers, step payloads and timestamp strings remain intact.
Changing the buffer size does not change the output. Duplicate identities still
fail, even with identical payloads or when split across disk runs; both conflict
locations refer to original inputs, never temporary run lines. All existing
validation, per-input bounds and output failure rules apply.

`--sort-buffer-bytes N` requires `--external-sort` and accepts 1–67108864 bytes
(default 8388608, or 8 MiB). The buffer counts **serialized UTF-8 event envelope
bytes**, including provenance and LF, rather than Python process memory. One
event larger than N gets its own run; a current decoded/encoded event may exist
alongside the buffer. Merge passes keep at most 32 pending run events and use
repeated passes when there are more runs. The run-path list is bounded too.
Source headers and the existing SQLite identity cache also consume memory.

Runs use mode 0600 inside a private mode 0700 temporary directory and are
removed on completion, handled failure, or closing the Python iterator early.
`--sort-temp-dir PATH` selects an **existing writable directory** as the root
for that private child directory, which holds both runs and a mode 0600 SQLite
identity index. It works with ordered exports, interval exports and `--check`
without `--external-sort`; ordered mode creates **only the index, no runs**.
The root is never created, removed or emptied; existing contents and permissions are preserved. Relative
paths and symlinks to existing directories are accepted. Invalid/unusable roots
return exit 2 with a fixed diagnostic that does not echo the root, empty stdout
and no new output file; there is no fallback to another root. Omission keeps
platform-default temporary storage (`TMPDIR` where supported).
For ordered inputs, both commands below put only the private index under the
chosen root (create the root first):

```sh
mkdir ordered-scratch
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl \
    --sort-temp-dir ordered-scratch \
    --since 2026-01-01T10:01:00Z --until 2026-01-01T10:02:00Z
agentwrench export merge examples/export/merge/a.jsonl examples/export/merge/b.jsonl \
    --check --sort-temp-dir ordered-scratch
```

The interval command emits B1 and reports one retained / three excluded events;
the check emits no stdout and reports four events from two sources. Both still
validate every event. Only sort-buffer and run-quota options require
`--external-sort`.

Staged stdout uses `--output-temp-dir` when supplied, otherwise platform-default
temporary storage, independently of `--sort-temp-dir`; `-o` writes to the
requested new output path.

`--max-sort-temp-bytes N` optionally caps the **total serialized bytes in live
run files**, including LF and bytes buffered for writing. N must be a positive
integer and requires `--external-sort`; omitting it keeps unlimited run storage.
This quota is separate from the run buffer and process memory. It excludes
the SQLite identity index, staged stdout/requested output, filesystem metadata
and block-allocation overhead; it is not a quota for all temporary disk usage.
Every run write is checked before writing, with no oversized-event exception
and no free-disk estimate. Old runs count until deleted: intermediate passes
need room for the replacement run **alongside its input runs**. A quota that
fits all initial runs can therefore still fail during a merge pass.

Exceeding the quota returns exit 2 with
`agentwrench export: error: sort temporary-run byte quota exceeded`, empty stdout, no
new output file and no leftover run directory. Existing files and symlinks
remain intact. Disk I/O failures retain the same cleanup. Abrupt process
termination or a filesystem failure preventing cleanup may leave artifacts. Even with
`-o`, sorting reads all inputs into runs before emitting events. No throughput
or fixed process-memory claim is made.

The Python iterator supports ordered selection with
`merge_jsonl(paths, sort_temp_dir="ordered-scratch")`, or external sorting with
`merge_jsonl(paths, external_sort=True,
sort_buffer_bytes=8388608, max_sort_temp_bytes=1048576,
sort_temp_dir="sort-scratch")` from `agentwrench.export.merge`.
The quota defaults to `None` (unlimited); invalid types (including booleans),
nonpositive values and use without external sorting raise `InputError`.
`sort_temp_dir` defaults to `None`; otherwise it accepts a nonempty string or
`os.PathLike[str]` naming an existing writable directory in either merge
mode. Invalid types/paths and directory creation failures raise `InputError`
with a safe diagnostic. As in default mode, it may
yield records before a later error; callers needing atomic publication must
stage their output and close the iterator on early exit. The CLI does both.

## Formats and mapping

| Input | Supported behavior |
| --- | --- |
| Codex rollout JSONL | `session_meta`; `response_item` message, function/custom calls and outputs, reasoning summaries; cumulative and last-call token usage; retains event metadata without duplicating response text |
| Claude Code JSONL | User/assistant message blocks: text, thinking, tool_use, tool_result; excludes `isSidechain` by default; ignores known nonconversation record types |
| Cline task directory | `api_conversation_history.json` Anthropic-style message array; native tool blocks and common legacy XML tool calls; optional `ui_messages.json` for exact text-matched timestamps and recorded API usage |

Unknown record/content types are retained in `extra` with warning counts.
Orphan results are kept and flagged. Duplicate call IDs fail because matching
would be ambiguous. Missing IDs get deterministic generated IDs and a warning.
Tool results attach to their originating call's step; original result timestamps
remain in result metadata. This groups observations with actions rather than
preserving the original stream's strict display order.

Cline UI timestamps are used only for **unique exact text matches**, never by
array index. Native tool IDs take precedence. For legacy XML, known invocation
tags are parsed as data; a `[tool for '…'] Result:` record attaches to the latest
unresolved invocation with that tool name. Unmatched results are flagged. This
is a heuristic for legacy histories, not proof of causality. Supported legacy
tags are listed in [the adapter](../src/agentwrench/export/adapters.py)
(`XML_TOOLS`); other XML remains text.
Files touched are extracted from known tool argument names and patch headers,
not from shell script interpretation or filesystem inspection.

Codex cumulative token snapshots replace earlier totals. Claude usage snapshots
sharing a response ID count once (the latest snapshot); Anthropic prompt totals
include noncached, cache-read, and cache-creation input tokens. Cline UI
`tokensIn`, `cacheReads`, and `cacheWrites` are summed for prompt totals.
Missing usage stays absent. Costs are not calculated.

### Schemas

`--to atif` emits **ATIF-v1.8**, pinned to Harbor commit
[`500af91374dcc43b59fac54cf90295646617b14d`](https://github.com/harbor-framework/harbor/tree/500af91374dcc43b59fac54cf90295646617b14d).
See its [RFC](https://github.com/harbor-framework/harbor/blob/500af91374dcc43b59fac54cf90295646617b14d/rfcs/0001-trajectory-format.md).
Each export runs a stdlib validator for the emitted text-only subset. The test
suite additionally validates offline against the pinned upstream Pydantic models
(vendored, Apache-2.0, in `tests/export/vendor/harbor`) and the
[generated JSON Schema](../tests/export/atif-v1.8.schema.json), which
`tests/export/harbor_schema.py` regenerates from those models without network
access.

Normalized calls `{id,name,arguments}` map to ATIF
`{tool_call_id,function_name,arguments}`. Raw tool input is wrapped in
`arguments.raw_input` when it is not a JSON object. Reasoning maps to
`reasoning_content`, token counts to `metrics`/`final_metrics`, and custom data
to `extra`. ATIF orphan references become null with the original unmatched ID
in `extra.unmatched_source_call_id`, so the export remains valid.

JSONL is **trajport/v1**, not ATIF JSONL. Its first line is
`{"type":"trajectory","schema_version":"trajport/v1","session_id":...,"agent":...,"extra":...}`.
Remaining lines are `{"type":"step", ...}` with:

```json
{
  "step_id": 1,
  "timestamp": "2026-01-01T00:00:00Z",
  "source": "agent",
  "message": "",
  "tool_calls": [{"id": "call-1", "name": "write_file", "arguments": {"path": "hello.txt"}}],
  "observation": {"results": [{"source_call_id": "call-1", "content": "Saved", "extra": {}}]},
  "extra": {}
}
```

`source` is user/agent/system; timestamps, calls, observations and metrics are
optional. Results can have null/unmatched call IDs in normalized JSONL.
Recognized raw records and unknown records live in `extra` in full exports,
subject to redaction, media omission, and output limits. Markdown output from
`convert --to md` starts with a `# agentwrench export trajectory` heading; `summary`
output starts with `# Hand-off brief`.

JSONL input records are delimited by LF (`\n`); CRLF (`\r\n`) is also accepted.
Literal U+0085 (NEXT LINE), U+2028 (LINE SEPARATOR), and U+2029 (PARAGRAPH
SEPARATOR) inside JSON strings remain data. Literal and `\u`-escaped spellings
produce identical decoded messages, tool arguments/results, and raw metadata
in ATIF and normalized JSONL exports, subject to the selected export options.
When consuming normalized JSONL, split on LF rather than Unicode-aware
`splitlines()`. A bare CR does not delimit records.

### Strict JSONL validation

By default, `convert` and `summary` recover from one malformed final JSONL
record without LF: they export the preceding good records and count one
`truncated_last_line` warning. Add **`--strict-jsonl`** to reject that tail
instead. This is useful when validating a completed export rather than reading
a session log that may still be receiving writes.
Use `validate` for a strict check without creating an export.

```sh
# Input bytes: a valid Claude record followed by an incomplete final record.
printf '%s\n%s' '{"type":"user","message":{"role":"user","content":"hello"}}' '{"type":' > partial.jsonl
agentwrench export convert partial.jsonl --to atif --strict-jsonl -o trajectory.json
# Exit 2; stderr: agentwrench export: error: invalid JSON at line 2
# Empty stdout; trajectory.json is never created. Existing files remain intact.

agentwrench export convert partial.jsonl --to atif
# Exit 0; one "hello" step; stderr/metadata: truncated_last_line: 1.
```

The Python API exposes the same opt-in contract:

```python
from agentwrench.export import load_session
from agentwrench.export.model import InputError

try:
    doc = load_session("partial.jsonl", strict_jsonl=True)
except InputError as error:
    assert str(error) == "invalid JSON at line 2"
```

Validation finishes before any conversion/summary output is written. Strict
mode accepts valid final records **with or without LF**, LF/CRLF delimiters,
blank JSON whitespace lines, and Unicode separators inside strings. It rejects
malformed records (including duplicate fields and nonfinite numbers) even at
unterminated EOF. Error locations count physical LF-based lines, including blank
lines; input contents are not echoed. Empty/whitespace-only input still fails
as an empty or undetectable session with exit 2 and no output.

`--strict-jsonl` controls parsing, independently of `--redact strict`, which
controls credential/path redaction. It applies to Codex and Claude JSONL;
Cline task JSON documents are always parsed in full, so the option does not
change their validation. `merge` already rejects every malformed tail and needs
no additional flag. Size/record bounds and all export options still apply.

## Redaction and input boundaries

Default redaction covers recognizable `sk-`, `ghp_`, `github_pat_`, `AKIA` keys,
Bearer credentials, values in sensitive JSON fields and single-line
`*_KEY`, `*_TOKEN`, `*_SECRET`, `PASSWORD`/`*_PASSWORD` assignments, email addresses,
and `/home/<user>` or `/Users/<user>` prefixes (replaced by `~`). It traverses
messages, arguments, results, metadata, and dictionary keys.

Other replacements are deterministic `[REDACTED:category:sha256-prefix]` markers;
repeated values in the same category use the same marker. Counts measure matched
**occurrences**, including copies in raw metadata, not unique secrets. No secret
values are put in diagnostics. `strict` additionally removes PEM private keys,
URL credentials, common credential query parameters, JWTs, and more absolute
paths. `off` deliberately retains secrets. Short key-like words (`sk-short`),
unknown encodings, arbitrary prose, custom credentials and every possible path
are not exhaustively detected. Review exports before sharing. Stable hashes
expose equality and can permit guessing low-entropy values; this is not an
anonymization guarantee.

All inputs are inert data. There is no `eval`, template execution, command
execution, archive extraction, URL/media fetching, import/resume, agent config
write, or workflow invocation. Cline fixed-name child files resolving outside
the task directory are rejected. Output uses only the explicit `-o` path.
Input must be a regular UTF-8 file, up to 64 MiB; JSONL lines up to 8 MiB and
histories up to 200000 records. `convert`, `summary` and `validate` hold inputs in memory;
avoid large histories on memory-constrained hosts. `merge` uses the incremental
reader and temporary disk described above. Duplicate JSON fields and nonfinite
JSON numbers are rejected. Recursive/malformed data fails with exit 2.
Markdown escapes HTML and terminal control characters and uses adaptive fences.

## Limits

- Export/handoff only: no agent import/resume or lossless archive round trip.
- Images, audio, documents and encrypted reasoning become placeholders; media
  type and encoded character count are reported when available, otherwise size
  is unknown. No external attachments are copied or accessed.
- For `convert`/`summary` by default, one malformed final JSONL record without a terminating LF is
  treated as a truncated write: warning and continue. Use `--strict-jsonl` to
  reject it. Malformed terminated/middle lines always fail.
- Formats evolve. Auto-detection inspects the first 100 records; use `--from`
  when needed. Event-only legacy Codex conversations are not reconstructed.
  Unknown data is preserved in metadata rather than invented as dialogue.
- Sidechains are flat included records with `--include-sidechains`, not a
  reconstructed subagent tree. Parent/child ordering is not repaired. Known
  Claude progress/system/snapshot/summary records do not become conversation.
- File/command summaries cover known direct calls only, not arbitrary nested
  JavaScript, shell scripts, MCP wrappers, or filesystem diffs. Exit status is
  unknown if the source does not explicitly record it.
- ATIF runtime validation covers only the emitted subset, not arbitrary ATIF
  inputs. No cost estimation, HTML viewer, or LLM summarization.

## Development

From the repository root:

```sh
uv run --extra test pytest tests/export
```

Tests use hand-written, redistributable synthetic fixtures only
(`tests/export/fixtures/`, `tests/export/golden/`). They cover three adapter
goldens, cross-agent equivalence, all views/redaction modes against upstream
ATIF validators, redaction positives/negative guards, determinism, legacy Cline
XML, CLI success/failure, truncation, media, sidechains, malformed inputs,
filesystem/command boundaries, Unicode/LF/CRLF framing, strict-mode and
validation behavior, merge ordering/provenance/conflicts, external sorting and
quotas, scratch-root placement and cleanup, interval selection, output limits,
and `render-merge` source/time/view selection.

## License

Apache-2.0; see [LICENSE](../LICENSE), [NOTICE](../NOTICE), and
[THIRD_PARTY.md](../THIRD_PARTY.md). This is an independent format
interoperability tool, not an official exporter or a wrapper around Harbor
converters. Claude Code interoperability uses observable file formats only; no
Anthropic code is copied. No converter code is copied from competitors or
upstream agents.
