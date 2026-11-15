# agentwrench gate

`agentwrench gate` turns existing `codex exec --json` or Claude Code
`--output-format stream-json` logs into a normalized trajectory, enforces
deterministic CI policies, compares baseline and candidate runs, and exports
OpenAI-style message lists for AgentEvals. No agents, commands from logs,
network requests, or LLM judges run when you use it.

## Install

Requires Python 3.11 or newer; Linux/POSIX paths.

```bash
pip install agentwrench          # or: pipx install agentwrench
agentwrench gate --version       # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx agentwrench gate check run.jsonl --policy policy.yaml
```

The runtime dependencies used by `gate` (PyYAML and `regex`) are installed
automatically. AgentEvals is optional for normal CLI operation; to score
exported messages with it, install the extra:

```bash
pip install 'agentwrench[agentevals]'
```

All tool operations are offline. Every command also works as
`python -m agentwrench gate ...`.

## Run on the supplied synthetic logs

The example inputs below live in `examples/gate/` in the agentwrench
repository; run the commands from the repository root (or adjust the paths).

```bash
agentwrench gate normalize examples/gate/codex_exec_ok.jsonl -o quickstart.normalized.json
agentwrench gate check examples/gate/codex_exec_ok.jsonl --policy examples/gate/policy.yaml
# 0 failed, 12 passed; exit 0

agentwrench gate check examples/gate/codex_exec_violation.jsonl \
  --policy examples/gate/policy.yaml --junit junit.xml --json
# 4 failed, 8 passed; exit 1. JSON on stdout, XML in junit.xml.

agentwrench gate export-agentevals examples/gate/claude_stream_ok.jsonl -o quickstart.messages.json
# A JSON array of assistant messages/tool_calls and tool messages/tool_call_id.
```

For single-log commands, pass `--from codex-exec` or `--from claude-stream` to
select an adapter; default `auto` scans known event types and refuses
ambiguous/mixed streams. Input is one UTF-8 JSON object per
line; blank lines are allowed. This is **exec stdout**, not Codex internal
rollout/session files or Claude's JSON-array output mode. Keep stderr separate
when capturing your own agent runs. `agentwrench gate` does not run those
agents for you.

`normalize -`, `export-agentevals -` and `check -` read raw JSONL from **binary stdin**
through EOF. After installing the CLI, these pipelines use the supplied
synthetic logs:

```bash
cat examples/gate/codex_exec_ok.jsonl | agentwrench gate normalize - --from codex-exec \
  -o codex.normalized.json --sidecar codex.originals.json
cat examples/gate/claude_stream_ok.jsonl | agentwrench gate normalize - --from claude-stream \
  --sidecar claude.originals.json > claude.normalized.json
agentwrench gate verify-sidecar claude.originals.json claude.normalized.json
agentwrench gate restore claude.originals.json claude.normalized.json --stdout | \
  cmp - examples/gate/claude_stream_ok.jsonl

cat examples/gate/codex_exec_ok.jsonl | agentwrench gate export-agentevals - --from codex-exec \
  -o codex.messages.json --sidecar codex.messages.originals.json
cat examples/gate/claude_stream_ok.jsonl | agentwrench gate export-agentevals - --from claude-stream \
  --sidecar claude.messages.originals.json > claude.messages.json
agentwrench gate verify-sidecar claude.messages.originals.json claude.messages.json
agentwrench gate restore claude.messages.originals.json claude.messages.json --stdout | \
  cmp - examples/gate/claude_stream_ok.jsonl

cat examples/gate/codex_exec_ok.jsonl | agentwrench gate check - --from codex-exec \
  --policy examples/gate/policy.yaml --json --junit codex.policy.xml
# Same 12 passing rules and evidence as file input; exit 0.
cat examples/gate/claude_stream_ok.jsonl | agentwrench gate check - --from claude-stream \
  --policy examples/gate/policy.yaml --json --junit claude.policy.xml
# Exit 1: the supplied policy requires an observed command exit code and
# git-status-before-pytest order, which this Claude example does not provide.
```

Omit `--from` to use the same auto-detection as file input. Bare `-` selects
stdin even if a local file named `-` exists; use `./-` to read that file.
The same inclusive limits apply to files and stdin: **64 MiB total, 1 MiB per
physical line (including line endings), and 100,000 physical lines**, counting
blank lines. Short binary reads are supported, and stdin is consumed from its
current position without seeking or closing it. There is no input timeout.
Invalid, empty, ambiguous, unreadable or oversized input exits 2 before a primary
JSON or policy report is printed or output/sidecar/JUnit files are staged. Invalid
policies also exit 2; `check - --policy POLICY.yaml` validates the local policy
before reading raw-log stdin.
Shell redirection itself can create an empty stdout destination on failure.
With `--sidecar`, original
bytes retain whitespace, Unicode, CRLF and absent final newlines; the serialized
pair caps described below still apply.

Raw JSONL stdin applies to `normalize`, `export-agentevals` and `check`. Export keeps
the same loss warnings as file input and refuses incomplete trajectories or
unsupported message orderings before printing primary JSON or staging files.
Check keeps the same policy JSON/console report, evidence indexes, completeness
and `--allow-incomplete` semantics, optional private JUnit, and exit 0/1 as file
input.

For a **local log**, `check --policy -` reads strict YAML from binary stdin:

```bash
cat examples/gate/policy.yaml | agentwrench gate check examples/gate/codex_exec_ok.jsonl \
  --policy - --from codex-exec --json --junit codex.stdin-policy.xml
# Same 12 passing rules and evidence as file policy; exit 0.
cat examples/gate/policy.yaml | agentwrench gate check examples/gate/claude_stream_ok.jsonl \
  --policy - --from claude-stream --json --junit claude.stdin-policy.xml
# Same policy failure as file policy; exit 1.
```

Policy stdin is read from its current position through EOF across short binary
reads without seeking or closing it, with an inclusive **64 KiB** byte cap and
the same strict YAML parser and rule limits as file policy. There is no input
timeout. Invalid/empty YAML, invalid UTF-8, duplicate keys, anchors, aliases,
unsafe tags, overflow and read failures exit 2 before reading the log, printing
a report or staging JUnit. Bare `--policy -` chooses stdin even if a local file
named `-` exists; `--policy ./-` reads that literal file.
`check - --policy -` is rejected before any stdin access or file staging: only
one input may use stdin. `compare --policy -` also accepts policy stdin, as
described below. Comparison manifests and their stream paths remain file-based.
`inspect-sidecar` accepts sidecar JSON on binary stdin; `verify-sidecar` and
`restore` accept binary stdin in their documented sidecar
or primary positions. Python callers can pass a borrowed binary `read(size)`
stream to `agentwrench.gate.streams.normalize(..., preserve_original=True)`,
`agentwrench.gate.policy.load_policy(...)` or the `policy` argument of
`agentwrench.gate.compare.compare(...)`; Python path arguments,
including `"-"`, always name files.

JSON/XML output files are **created exclusively** with private permissions.
Existing files, symlinks, and missing parent directories cause exit 2. Choose a
fresh output path on each run. No path contained in an event becomes a file to
read or write. Input errors produce a diagnostic on stderr and no new report.
Exit codes are **0** = operation/gate passed, **1** = policy or regression gate failed,
**2** = bad input, unsupported export, invalid policy, or I/O error. Normalizing
an incomplete log succeeds but records `complete: false`; checking fails unless
explicitly overridden, and exporting refuses it.

To save a check's JSON report directly, use `--json -o FRESH.json` (or
`--json --output FRESH.json`), optionally with paired JUnit:

```bash
agentwrench gate check examples/gate/codex_exec_ok.jsonl --policy examples/gate/policy.yaml \
  --json -o codex.check.json --junit codex.check.xml
# Exit 0; 12 passing rules. Primary stdout is empty.
agentwrench gate check examples/gate/claude_stream_ok.jsonl --policy examples/gate/policy.yaml \
  --json -o claude.check.json --junit claude.check.xml
# Exit 1; the supplied policy fails as described above. Both reports are saved.
```

Saved JSON is byte-identical to `check --json` stdout, including its final
newline. `-o` **requires `--json`**; there is no console report-file mode.
Without `-o`, JSON/console stdout and optional JUnit behave as before. Raw-log
stdin with a local policy, local-log policy stdin, and `--allow-incomplete`
work with saved reports under the same input limits.

JSON and optional XML are serialized before either is staged, then created as
one private (`0600`), exclusive report group. Invalid input, policy or report
serialization exits 2 without a report prefix or files. Existing destinations
(including symlinks), missing parents, and write/fsync/link errors exit 2 with
empty primary stdout; ordinary-error rollback removes this invocation's staging
and created files while preserving concurrent winners. The group is not
crash-atomic across directories. A valid policy outcome exits 0 or 1 only after
all requested reports are saved.

## Compare baseline and candidate evaluations

`compare` evaluates paired tasks using the **same policy snapshot** and the
existing normalized trajectory model. Manifests identify local JSONL logs;
the gate normalizes them internally, without running an agent or evaluator.

```bash
agentwrench gate compare examples/gate/compare_baseline.json \
  examples/gate/compare_candidate.json --policy examples/gate/policy.yaml
# JSON report: lint=unchanged_pass, tests=regressed; exit 1

agentwrench gate compare examples/gate/compare_baseline.json \
  examples/gate/compare_candidate.json --policy examples/gate/policy.yaml \
  --max-regressions 1 -o comparison.json
# Same task outcomes, threshold passed; exit 0. Use a fresh output path.
```

For local manifests, pipe the policy on binary stdin instead:

```bash
cat examples/gate/policy.yaml | agentwrench gate compare examples/gate/compare_baseline.json \
  examples/gate/compare_candidate.json --policy -
# 1 task regression; exit 1.
cat examples/gate/policy.yaml | agentwrench gate compare examples/gate/compare_baseline.json \
  examples/gate/compare_candidate.json --policy - --max-regressions 1 -o stdin.comparison.json
# Inclusive task allowance passes; exit 0, empty stdout, fresh private report.
cat examples/gate/policy.yaml | agentwrench gate compare examples/gate/compare_baseline.json \
  examples/gate/compare_candidate.json --policy - --regression-unit rule --max-regressions 3
# 4 rule regressions exceed the allowance; exit 1.
cat examples/gate/policy.yaml | agentwrench gate compare examples/gate/compare_baseline.json \
  examples/gate/compare_candidate.json --policy - --regression-unit rule --max-regressions 4
# Inclusive rule allowance passes; exit 0.
```

File and stdin policies produce byte-identical reports, including the SHA-256
of the exact policy bytes. Comparison reads one policy snapshot from the current
stdin position through EOF over short binary reads, without seeking or closing
stdin. The inclusive **64 KiB** cap and strict YAML/rule validation apply; there
is no input timeout. Empty/malformed/invalid UTF-8/duplicate/anchor/alias/unsafe-tag,
over-limit and read-failing policies exit 2 with no JSON prefix, output file or
staging residue. Manifest validation precedes policy reading; policy validation
precedes trajectory evaluation. Bare `--policy -` chooses stdin even with a local
dash file; `--policy ./-` reads that file. Both manifests and their stream paths
always name files, including literal `-`; policy stdin does not change their
resolution. Optional `-o` keeps exclusive private creation and ordinary-error
rollback. Python callers may pass a borrowed binary stream as `policy`; Python
path arguments remain literal.

Each manifest has this format (the supplied examples use synthetic logs):

```json
{
  "schema_version": 1,
  "tasks": [
    {"id": "tests", "stream": "codex_exec_ok.jsonl"},
    {"id": "lint", "stream": "claude_stream_ok.jsonl",
     "source": "claude-stream", "root": "/workspace/demo"}
  ]
}
```

`stream` paths resolve relative to their manifest, independently of the current
directory; absolute paths are also accepted. Optional `source` defaults to
`auto`; optional `root` is an absolute POSIX path with the same lexical mapping
as `--root`. These explicit manifest paths authorize input reads; no path from
an event is opened. Saved `normalize` JSON objects are not JSONL stream inputs.
Manifests require integer `schema_version: 1`, 1–200 tasks, unique nonempty
string IDs, and identical task ID sets on both sides. Missing/extra tasks,
unknown keys, duplicate JSON keys, malformed logs, or invalid policies cause
exit 2 with no new report. Each manifest is limited to 64 KiB; the existing
per-log and policy limits apply. Trajectories are evaluated one at a time.

The JSON report always includes sorted per-task outcomes and each side's full
policy report (pass/fail, rule evidence indexes, completeness, pending calls,
ignored counters). Outcomes are:

| Baseline policy | Candidate policy | Outcome |
| --- | --- | --- |
| Pass | Fail | `regressed` |
| Fail | Pass | `improved` |
| Pass | Pass | `unchanged_pass` |
| Fail | Fail | `unchanged_fail` |

By default, `--max-regressions N` permits at most **N pass-to-fail tasks**,
inclusively; default 0. `--regression-unit task` explicitly selects this behavior.
Two failing evaluations remain `unchanged_fail` at the task level even if
different rules failed.

Use `--regression-unit rule` to count **pass-to-fail (task ID, rule ID) pairs**:

```bash
agentwrench gate compare examples/gate/compare_baseline.json \
  examples/gate/compare_candidate.json --policy examples/gate/policy.yaml \
  --regression-unit rule
# 4 rule regressions on task "tests"; exit 1, although only 1 task regressed.

agentwrench gate compare examples/gate/compare_baseline.json \
  examples/gate/compare_candidate.json --policy examples/gate/policy.yaml \
  --regression-unit rule --max-regressions 4
# Inclusive allowance for 4 rule regressions; exit 0.
```

For example, suppose a policy forbids commands `^a$` and `^b$`. If a baseline
task runs `a` and its candidate runs `b`, both tasks fail, so task mode passes
with zero regressions. Rule mode reports `forbid_command[0:^a$]` as `improved`
and `forbid_command[1:^b$]` as `regressed`: one rule regression, exit 1 at the
default threshold. Even two improvements cannot offset that regression.
Repeated evidence for one rule counts once per task; the same rule regressing
on two tasks counts twice. Expanded list rules have separate indexed IDs even
when their patterns are identical. Disabled rules have no result; the automatic
`incomplete_stream` rule is included, so incomplete logs can cause regressions.

JSON always names the selected `regression_unit`. `summary` and task `outcome`
always describe whole tasks. In rule mode, `rule_summary` contains `total`,
`regressed`, `improved`, `unchanged_pass`, and `unchanged_fail` counts across all
task/rule pairs. Each task also has a `rules` list sorted by rule ID, with
`id`, `outcome`, `baseline_passed`, and `candidate_passed`; evidence remains in
that task's full `baseline.results` and `candidate.results`. IDs come from the
same policy snapshot and the automatic completeness check, rather than from
array positions. `threshold.observed_regressions` uses the selected unit, and
`passed` is true exactly when it is at most `threshold.max_regressions`; CLI
exits 0 or 1 accordingly. Invalid units/inputs cause exit 2 without a new report.
The Python API accepts
`agentwrench.gate.compare.compare(..., regression_unit="rule")` as well.

Improvements never offset regressions in either mode. The gate does not infer
task correctness from natural-language answers, compare token/cost deltas, or
require every candidate task to pass.

Reports contain the agentwrench version (in the `trajgate_version` key), the
SHA-256 of the exact policy bytes, and per-side SHA-256 of normalized trajectory
data serialized with sorted keys, ASCII escapes and compact separators. No
timestamps or manifest locations are included: identical normalized inputs,
policy bytes, version, regression unit and threshold produce byte-identical
output regardless of manifest task order. Evidence indexes refer to the corresponding side's normalized events, not a combined
stream. Keep the original logs and policy alongside the report to reproduce it.

## Policy language

The YAML root is a mapping of the following keys. Unknown keys (including
nested keys), invalid types, duplicate keys, invalid regex/globs, YAML anchors,
aliases, and unsafe tags are rejected. Rules do not evaluate Python, shell,
SQL, templates, or arbitrary expressions.

```yaml
require_command:                          # a mapping or list of mappings
  - pattern: '^python -m pytest(?:\s|$)'
    exit_code: 0                          # optional; must be explicitly observed
forbid_command:
  - pattern: '\bcurl\b'
forbid_file_change:
  - glob: '.github/**'
allow_file_change_only: ['src/**', 'tests/**']
max_tool_calls: 8
max_commands: 3
max_total_tokens: 10000
require_final_message:
  pattern: '^Done:'
forbid_error_events: true
order:
  - ['git status', 'pytest']               # command-regex shorthand
  - [{kind: file_change, glob: 'src/**'}, {kind: command, pattern: pytest}]
require_success: true
```

| Rule | Meaning and evidence |
| --- | --- |
| `require_command` | At least one **completed** command matching the regex, and the specified exit code if present. Failure cites matching commands with wrong/missing outcomes; no match has empty evidence. |
| `forbid_command` | No observed command string matches; includes commands still running or declined. Evidence cites violations. |
| `forbid_file_change` | No observed/attempted changed path matches `glob`. A mapping or list of `{glob: ...}` mappings. |
| `allow_file_change_only` | Every changed path matches at least one listed glob. Empty list forbids every change. Absolute/unrooted paths and `..` escapes fail even with `**`. |
| `max_tool_calls` | Count unique invocations across command, file-change batch, MCP, web search, and generic tools. Derived Claude command/file events do not double-count their tool_use. |
| `max_commands` | Count command invocations, merging started/updated/completed snapshots. |
| `max_total_tokens` | Sum terminal usage across turns. Missing usage, unresolved work, or an incomplete stream **fails**, rather than counting missing usage as zero. |
| `require_final_message` | Last conversational action must be a completed, non-reasoning assistant message matching the regex. A message before unresolved tool activity is insufficient. |
| `forbid_error_events` | Reject normalized errors, including failed commands, failed file patches, failed MCP/Claude tools, and terminal failures. |
| `order` | Each pair requires at least one A and B, and **all A event indexes must precede all B indexes**. This is invocation/observation order, not process completion order. |
| `require_success` | Stream is complete, has terminal events, and every terminal event reports success. Recovered command failures can coexist with a successful turn; use `forbid_error_events` too if desired. |

`false` disables boolean rules. Zero budgets are valid. `order` selectors accept
`kind` and optional `name` (exact normalized name), `pattern` (command/message
only), or `glob` (file_change only). Kinds are `message`, `command`, `file_change`,
`tool_call`, `tool_result`, `web_search`, `usage`, `error`, `end`.

Regexes use the third-party Python `regex` library with search semantics, a 2,048
character pattern limit, and a 50 ms timeout **per match** (timeout = exit 2).
Quote YAML regexes with single quotes to preserve backslashes. Policies are
limited to 64 KiB and 200 expanded rules. The number of rules and log size also
bound work; a policy with many expensive regexes can still take significant time.

Globs are case-sensitive full POSIX paths: `*` and `?` never cross `/`, `**` is
a whole path segment, `**/` also matches zero directories, and `[abc]`, `[a-z]`,
`[!a]` classes are supported. Braces, escaping, malformed classes and partial
`**` segments are errors. Hidden files are matched normally. Paths are normalized
lexically (`src/../x` becomes `x`), without consulting the filesystem or resolving
symlinks. Use `--root /absolute/repo` for absolute Codex paths. Claude's
`system.init.cwd` supplies the default root; `--root` overrides it. Paths outside
the root retain `../` and cannot pass an allowlist.

## Normalized data and evidence

`normalize` returns an object with `schema_version: 1`, `source`, `events`,
`complete`, `pending_calls`, `ignored`, `ignored_count`, and `losses`. Example event:

```json
{"index": 6, "source_line": 7, "kind": "command", "call_id": "codex:c1",
 "command": "python -m pytest -q", "exit_code": 0, "completed": true,
 "completion_line": 9, "output": "3 passed in 0.02s\n"}
```

Indexes are **one-based normalized event indexes**. `source_line` points to
the original JSONL line; `completion_line` records completion when applicable.
Multiple normalized events may come from one line. Lifecycle snapshots update
one event at its first-observed index; command/MCP identity changes are rejected,
and file snapshots retain all observed paths. Events also carry source-specific
fields such as `item_id`, `status`, `arguments`, `changes`, `query`, or `content`.
Codex reasoning summaries remain messages with `reasoning: true`; export omits
them. Todo-list lifecycle metadata is recognized and omitted.

`check --json` returns one `{id, passed, message, evidence}` result per rule,
a summary of pass/fail counts, completeness, and ignored-event counters. A
missing terminal event or unresolved item adds `incomplete_stream` automatically.
`--allow-incomplete` suppresses that one rule; it does not waive command outcomes,
known usage, or `require_success`. JUnit has one testcase per evaluated rule,
evidence in failure/system-out text, and matching counts. Unknown event/item/
content types are counted by name. Partial Claude `stream_event` records are
counted and ignored, because complete assistant blocks carry the actual content.
Inspect these counters when upgrading agent versions. Export warns on stderr
about ignored records; it cannot encode report counters into the message array.

## Preserve originals and account for non-text data

Both adapters report source data outside their normalized projection in
`normalize`'s `losses` array. AgentEvals export warns on stderr about its own
projection losses, including unsupported fields/blocks, omitted reasoning and
usage, and structured tool results serialized as JSON strings. These are
nonfatal warnings; they do not change policy results or prove that all source
content was understood. Existing `ignored` counters keep their previous meaning.

Use `--sidecar PATH` with either command to retain the **exact original UTF-8
JSONL**, including whitespace, blank lines, CRLF and absent final newline:

```bash
agentwrench gate normalize examples/gate/claude_attachments.jsonl \
  -o normalized.json --sidecar normalized.originals.json
agentwrench gate export-agentevals examples/gate/claude_attachments.jsonl \
  -o messages.json --sidecar messages.originals.json
```

This local synthetic example contains an image tool result, thinking/image
assistant blocks and extra fields. The image result remains in normalized
`content`; the message export encodes that list as a JSON string. It does not
render a multimodal OpenAI message. The assistant image/thinking blocks and
unsupported fields are retained in the original-log sidecar, with locations
in its loss report rather than invented text messages.

The sidecar is a version-1 JSON object with `source`, `target` (`normalize` or
`agentevals`), `original: {encoding: "utf-8", jsonl: "...", sha256: "..."}`,
`output_sha256` (the exact primary JSON output bytes), and `losses`. Each loss
has `source_line` (physical line), `pointer` (RFC 6901 path within that line's
object; empty means the whole record), `reason`, and `disposition` (`omitted`
or `json_string`). For example, line 2's extra field is reported at
`/message/content/0/future~1field~0`, and the exported attachment list at
`/message/content/0/content`. Unsupported objects are reported at their root;
we do not enumerate every descendant. Tool arguments and result payloads are
opaque values, preserved without validating their private schemas. Known
metadata/partial records also receive explicit omission entries. This is a
schema-level accounting report, not a field-by-field semantic round-trip
mapping of lifecycle snapshots, path normalization or tool-name transformations.

Originals are captured during the same bounded read used for normalization.
No attachment URL or event path is opened, no media is decoded, and no separate
attachment file is created. Use `restore` below to verify the sidecar and
reconstruct a fresh JSONL file; it can be normalized again.
Without `--sidecar`, dropped originals cannot be recovered from the output.
The Python API exposes `normalize(..., preserve_original=True).original_jsonl`,
plus `Trajectory.losses` and `Trajectory.export_losses` for the two projections;
originals are excluded from `as_dict()` and comparison fingerprints.

Sidecars contain the entire supplied log, including any supplied secrets, and
use the same exclusive creation and private `0600` permissions as reports.
Invalid input or an unsupported conversation creates neither output. If either
output path already exists (including a symlink), or writing/linking either
file fails, newly created files are rolled back and exit 2 is returned. Pair
creation is not crash-atomic across directories. With stdout output, the
sidecar is created before any primary JSON is printed. Original capture and
loss reports add memory/output proportional to the bounded input; no streaming
or performance claim is made.

### Inspect a sidecar without its primary

If the raw log or paired output is unavailable, inspect the saved sidecar alone:

```bash
agentwrench gate inspect-sidecar normalized.originals.json
cat messages.originals.json | agentwrench gate inspect-sidecar -
# ./- names a literal file called -; bare - always selects binary stdin.
```

Exit 0 prints deterministic, sorted metadata-only JSON and empty stderr. The
report schema is version 1; this example uses an empty original and loss list:

```json
{
  "schema_version": 1,
  "sidecar_schema_version": 1,
  "source": "claude-stream",
  "target": "normalize",
  "original": {
    "encoding": "utf-8",
    "bytes": 0,
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "verified": true
  },
  "primary": {
    "encoding": "utf-8",
    "expected_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  },
  "primary_verified": false,
  "losses": {
    "total": 0,
    "by_disposition": {"omitted": 0, "json_string": 0}
  }
}
```

`source` and `target` name the supported formats in the version-1 envelope;
`original.encoding` describes the embedded UTF-8 JSONL string. Version 1 has
no separate source/target version fields or base64 envelope encoding.
`original.bytes` counts encoded UTF-8 bytes, and `original.verified` confirms
only their SHA-256 against the supplied hash. `primary.encoding` is the expected
UTF-8 encoding of generated JSON; `primary.expected_sha256` remains an assertion.
**`primary_verified` is always false.** Use `verify-sidecar SIDECAR PRIMARY` to
verify a pair. Inspection does not check primary bytes, authenticity, semantic
correspondence, or whether the supplied loss accounting is exhaustive.

Loss counts group entries by the two supported dispositions, `omitted` and
`json_string`, including zero counts. Reasons remain nonempty strings as in
the existing sidecar schema; their values and all pointers are omitted from the
report. No original content, attachment data, paths or timestamps are printed.
No primary argument or output-file option is accepted. Inspection never restores
bytes, runs adapters, decodes media, opens embedded paths, or creates files.

Inspection shares strict bounded sidecar decoding and original-hash validation
with verification/restore. Invalid JSON/UTF-8, duplicate keys, nonfinite numbers,
unknown/missing fields, unsupported versions/formats/encodings, malformed losses,
hash failures, read errors and oversize inputs exit 2 with empty stdout and a
fixed diagnostic containing no payload excerpts. Both serialized sidecar and
embedded original have an inclusive **64 MiB** cap. Binary stdin supports short
reads from its current position through EOF without seeking or closing it.
Sidecars and originals remain in bounded memory during inspection. The Python
`agentwrench.gate.sidecar.inspect_sidecar(path_or_binary_stream)` returns the same
metadata dictionary and raises `agentwrench.gate.model.InputError` on rejection.

### Verify and restore a sidecar

To check a pair without restoring a file:

```bash
agentwrench gate normalize examples/gate/codex_exec_ok.jsonl \
  -o verified.json --sidecar verified.originals.json
agentwrench gate verify-sidecar verified.originals.json verified.json
# Verify the exact primary bytes from stdin:
cat verified.json | agentwrench gate verify-sidecar verified.originals.json -
# Or verify a sidecar from stdin against the primary file:
cat verified.originals.json | agentwrench gate verify-sidecar - verified.json
```

`verify-sidecar` exits 0 with a JSON integrity report on stdout and empty stderr.
The report contains `schema_version: 1`, `verified: true`, `source`, `target`,
and `original` / `output` objects, each with `sha256` and `bytes`. Counts measure
exact file bytes (UTF-8 for the original), including whitespace and newlines.
Keys are sorted; there are no timestamps, input paths, payloads or loss entries,
so repeated checks of an unchanged pair produce identical JSON. This command
creates no files and accepts no `-o` / `--output` option. A malformed, oversized,
missing or hash-mismatched input exits 2 with stderr and no success JSON.

For both commands, primary argument `-` reads binary stdin incrementally,
with the same inclusive 64 MiB limit. Bytes are hashed exactly, without text
decoding or newline conversion. With a primary on stdin, the sidecar is a file
and is validated before stdin is consumed. An invalid sidecar, oversized or mismatched primary,
or stdin read error produces exit 2 with no success JSON or files. A primary
file named `-` can be addressed as `./-`. The Python verifier
(`agentwrench.gate.sidecar.verify_sidecar`) also accepts a
binary primary stream, reading from its current position without seeking or
closing it. `restore` accepts primary files or stdin and requires either `-o`
or `--stdout`.

For `verify-sidecar` and `restore`, sidecar argument `-` instead reads binary
stdin against a primary file. Short reads are handled until EOF, with the same inclusive
64 MiB serialized-sidecar cap and a one-byte overflow probe. The complete
sidecar schema and original hash are validated before the primary file is read.
Read, schema, size or hash errors exit 2 with stderr, no success output and no
file writes. Both `verify-sidecar - -` and `restore - - -o FRESH.jsonl`
exit 2 before accessing stdin: only one input may use it. The Python verifier also
accepts a borrowed binary sidecar stream without seeking or closing it.
Bare `-` selects stdin even if a local file named `-` exists; use `./-` to
select that literal sidecar or primary file in either command.

Supply the sidecar, its exact primary JSON output, and a fresh destination:

```bash
agentwrench gate restore normalized.originals.json normalized.json -o restored.jsonl
# Restored verified original JSONL. (exit 0)
# Or supply exact primary bytes through a pipe (fresh output path):
cat normalized.json | agentwrench gate restore normalized.originals.json - -o piped.jsonl
# Or supply the sidecar through a pipe against its primary file:
cat normalized.originals.json | agentwrench gate restore - normalized.json -o sidecar-piped.jsonl
# AgentEvals sidecars use the same restore command (fresh output path):
cat messages.originals.json | agentwrench gate restore - messages.json -o messages-restored.jsonl
```

This works for both `normalize` and `export-agentevals` sidecars and both source
formats. Restore validates the version-1 envelope, the SHA-256 of the UTF-8
encoded `original.jsonl`, and the SHA-256 of the exact primary file or stdin bytes.
Reformatting the primary JSON, including changing its final newline, fails
verification. The restored bytes preserve Unicode, blank lines, CRLF and a
missing final newline. A destination (`-o` or `--stdout`) is required before stdin
is accessed. With `-o`, the destination is created exclusively at mode `0600`,
after verification; the existing success diagnostic is printed on stdout.

To pipe the fully verified original without creating an output or staging file:

```bash
agentwrench gate restore normalized.originals.json normalized.json --stdout | sha256sum
# Either input may also use stdin (only one):
cat normalized.json | agentwrench gate restore normalized.originals.json - --stdout | sha256sum
cat messages.originals.json | agentwrench gate restore - messages.json --stdout | sha256sum
```

`--stdout` writes only the exact original UTF-8 bytes to binary stdout, without
adding a newline. The success diagnostic goes to stderr. It is mutually exclusive
with `-o` / `--output`; combining them exits 2 before any input or output access.
Two stdin inputs are also rejected before access. Neither borrowed stdin nor stdout
is sought or closed. All bounded sidecar/schema/original/primary checks finish
before the first stdout write, so a verification or input-read failure emits no
stdout prefix. The verifier accepts an empty original string with its matching
SHA-256; stdout then emits zero bytes (file restore creates an empty private file).
This does not change normalization's rejection of an empty input stream.
Stdout short writes are completed; write/flush failures, including broken pipes,
exit 2 with a diagnostic and no success text. A downstream failure can leave
already-written verified bytes; a pipe cannot roll them back.

Malformed UTF-8/JSON, duplicate or unknown envelope keys, missing fields,
unsupported version/source/target/encoding, invalid loss entries or hashes,
hash mismatches, missing inputs, and I/O failures produce a diagnostic on stderr
and exit 2 without a new destination. Existing destinations (including inputs,
directories and dangling symlinks) are refused without overwriting them.
Temporary files are removed on ordinary write/link failures.

Both commands use the same strict validator and accept at most **64 MiB each**
of serialized sidecar, primary output,
and reconstructed original. The primary is hashed incrementally. With `--sidecar`, generation enforces
the same inclusive caps on the exact serialized primary and sidecar bytes,
including their final newlines. JSON escaping and loss entries can exceed a
cap even when the source log fits the input limits. Either oversized artifact
causes exit 2 with a size diagnostic on stderr, before staging any files or
printing primary JSON; existing files and symlinks remain unchanged. These
output caps apply when `--sidecar` is requested. Memory use grows with the
bounded input and serialized output; generation and restore hold data in memory.

Verification checks integrity against the supplied hashes, not authenticity:
someone can change both content and hashes. It validates the sidecar envelope
and loss-entry shapes, without reparsing the embedded log or primary JSON,
rerunning adapters/policies, or proving their semantic correspondence. Loss
pointers remain metadata; no embedded path or attachment is opened. Sidecars
and restored logs contain the supplied data without secret redaction.

## AgentEvals interoperability

```python
import json
from agentevals.trajectory.match import create_trajectory_match_evaluator

with open('messages.json') as f:
    outputs = json.load(f)
with open('expected-messages.json') as f:
    expected = json.load(f)
evaluate = create_trajectory_match_evaluator(trajectory_match_mode='strict')
print(evaluate(outputs=outputs, reference_outputs=expected))
```

Claude generic tool names and input objects are preserved. Bash, Write/Edit/
MultiEdit/NotebookEdit, and WebSearch also produce policy events. Distinct
complete blocks may share an API `message.id`; they are all retained. Duplicate
outer record UUIDs, replayed messages, duplicate tool IDs and orphan results
are rejected. Adjacent parallel calls are grouped into one assistant tool_calls
batch and tool results retain their original IDs.

Codex exports `command_execution`, `file_change`, `web_search`, and MCP function
calls. Command arguments are `{command: ...}`; results are `{exit_code, output}`.
File arguments/results carry the normalized changes and status. Web search
results carry available `action`/`results` metadata, not invented page content.
MCP names are `server__tool`, with component underscores escaped to `_u` to
avoid collisions. Unsafe/long MCP names become `mcp_` plus a 56-hex SHA-256 prefix
of the JSON pair; other unsafe/long tool names become `tool_` plus a 56-hex hash.
Normalized events preserve the original MCP server/tool. Missing/non-object tool
arguments, missing outcomes, incomplete trajectories and conversational
interleaving that cannot be serialized faithfully are explicit export errors.

AgentEvals evaluates the exported messages; `agentwrench gate` adds format adapters and
deterministic gates. It does not replace AgentEvals. The integration test calls
`create_trajectory_match_evaluator` against an independent hand-written reference
and verifies that altered tool arguments fail. No judge/model endpoint is used.

## Limits and trust boundary

- Logs are observations, not a security attestation. Users can edit logs. Command
  regexes match text: `echo pytest` can satisfy a broad pattern. File policy sees
  reported patches and known Claude write tools, **not arbitrary writes inside
  shell commands or opaque MCP/custom tools**. This is not a sandbox.
- Claude's documented Bash output does not guarantee a numeric exit code.
  `exit_code: 0` cannot pass when it is absent. If a compatible emitter provides
  `tool_use_result.exit_code`, it is retained; the gate never infers zero from a
  successful tool-result block. Requiring the command without a code proves only
  an observed completed invocation.
- Claude subagent/nested events are rejected; detached/background tools remain
  unresolved and fail completeness. Internal session logs, multimodal rendering,
  replay/compaction reconstruction, collab-agent expansion, live tailing and
  arbitrary tool-to-file mappings are out of scope. Unknown types are reported
  in counters rather than treated as evidence. Explicit `--from` can parse a
  fragment; it cannot recover omitted events.
- Terminal markers are Codex `turn.completed`/`turn.failed` and Claude `result`.
  A lone Codex fatal `error` still lacks a turn marker and is incomplete. A
  subsequent new turn/action resets completeness. A failed terminal event makes
  the stream complete, but fails `require_success`.
- Codex token totals are input + output (cached and reasoning tokens are
  subsets); Claude totals additionally include separately reported cache-read
  and cache-creation input tokens. Only terminal usage is counted. No costs or
  estimates are inferred. Missing Claude cache fields are treated as zero.
- Input limits: 64 MiB total, 1 MiB per line, 100,000 physical lines. The tool
  holds the bounded trajectory in memory and uses no background services.
- Outputs contain supplied command text, arguments and tool results. There is
  no automatic secret redaction. Supply synthetic/sanitized logs and keep your
  reports private. The gate does not read credentials, run agent binaries, or
  access production services.

## Development

From the repository root:

```bash
uv run --extra test pytest tests/gate
```

The `test` extra includes AgentEvals; the AgentEvals integration test fails if
it is missing. Tests block Python socket connections and disable LangSmith
tracing. Fixtures (in `tests/gate/fixtures/`) cover both documented formats,
success/failure, lifecycle merging, every policy rule, YAML/regex/path
boundaries, CLI exit codes and JUnit counts.

## License

agentwrench is licensed under Apache-2.0; see [LICENSE](../LICENSE),
[NOTICE](../NOTICE) and [THIRD_PARTY.md](../THIRD_PARTY.md).
