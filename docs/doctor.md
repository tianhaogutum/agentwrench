# agentwrench doctor

`agentwrench doctor` finds and repairs broken tool-call pairing in persisted **Codex rollout JSONL** and **Claude Code session JSONL** files. It runs entirely offline on Linux, using only the Python standard library at runtime; no agent, shell command, model API, or session tool is executed.

A missing output or an output with no identifier can poison a resumed conversation. This tool checks the persisted file, shows a repair plan, and produces a locally validated copy. **Stop the agent before repairing its session.** Keep the original until you have successfully resumed it. A clean report describes the supported pairing invariants; provider acceptance is not guaranteed.

## Install

Requires Python 3.11 or newer.

```sh
pip install agentwrench          # or: pipx install agentwrench
agentwrench doctor --version     # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx agentwrench doctor check ./session.jsonl
```

## Use

The example inputs below live in `examples/doctor/` in the agentwrench repository; run the commands from the repository root (or adjust the paths).

```sh
# Inspect the synthetic examples shipped in the repository.
agentwrench doctor check examples/doctor/codex/clean.jsonl
agentwrench doctor check examples/doctor/codex/orphan.jsonl --json
agentwrench doctor check examples/doctor/claude/orphan_tool_use.jsonl

# Review a diff without writing any file.
agentwrench doctor repair examples/doctor/codex/orphan.jsonl --dry-run

# Write a NEW file; existing output paths are refused.
agentwrench doctor repair examples/doctor/codex/orphan.jsonl --out repaired.jsonl
agentwrench doctor check repaired.jsonl

# Opt in to in-place replacement and a timestamped backup.
# Replace this example path with a stopped agent's session file.
agentwrench doctor repair ./session.jsonl --in-place --backup

# Remove unfinished calls rather than synthesize interrupted outputs.
agentwrench doctor repair ./session.jsonl --strategy drop --out dropped.jsonl

# Explicitly opt in to discarding invalid middle lines/invalid records.
agentwrench doctor repair ./session.jsonl --drop-invalid --out recovered.jsonl

# Recursively check *.jsonl, or specify a known format for damaged headers.
agentwrench doctor check ./session-directory --json
agentwrench doctor check ./damaged.jsonl --format codex
```

`python -m agentwrench doctor` is equivalent to `agentwrench doctor`. `--format auto|codex|claude` and `--json` work with `check`, `repair`, `preview`, `audit`, `migrate-audit`, and `verify-audit`; `apply` supports `--json` and uses the saved format. Directory scans never repair anything. They skip symlink directories, and report symlink files as errors. Mixed Codex/Claude files are refused. An empty file is clean; a nonempty file without recognizable markers needs an explicit format.

For the Codex orphan fixture, `check` exits 1 and reports `RD103` at line 1 and `RD101` at line 2. Default repair drops the unusable output and inserts a `function_call_output` after the call:

```json
{"type":"response_item","payload":{"type":"function_call_output","call_id":"c1","output":"[rollout-doctor] tool output missing: call was interrupted"},"timestamp":"2026-01-01T00:00:00Z"}
```

A single-file `--json` report has this shape (line and block indexes are respectively 1-based and 0-based):

```json
{
  "file": "examples/doctor/codex/orphan.jsonl",
  "format": "codex",
  "issues": [
    {"code":"RD103","line":1,"detail":"tool output missing usable call_id"},
    {"code":"RD101","line":2,"detail":"tool call has no later output in its chain","call_id":"c1"}
  ],
  "changes": []
}
```

Repair reports retain the original issues and add `changes`, `clean_after`, `dry_run`, `output`, and `backup`. Directory reports use `{"files":[...],"errors":[...]}`. Operational errors use `{"file":...,"error":...}` when arguments were successfully parsed. JSON output contains identifiers and edit descriptions, never arguments or tool-output contents. Human dry-run diffs include changed input contents; review them locally if sessions contain secrets. Warnings go to stderr.

Exit codes: `check`: **0** clean, **1** issues, **2** usage/unreadable/unsupported format. `repair`: **0** validated clean result or dry-run, **2** refusal/failure. Repair is idempotent: a second run reports zero changes; an in-place no-op does not write or create another backup.

## Saved repair preview and apply

Save a reviewable repair manifest, then apply it in a later invocation. Choose a
**new output file** to leave the original session intact:

```sh
# All inputs here are local synthetic fixtures. Output parents must already exist.
agentwrench doctor preview examples/doctor/codex/orphan.jsonl \
  --out repaired.jsonl --manifest repair-plan.json --json
# Creates only repair-plan.json (mode 0600), with two planned changes.
# Review its source/output paths, SHA-256 values, options, and report.changes.

agentwrench doctor apply repair-plan.json --json
# status="applied": creates repaired.jsonl (mode 0600), validated clean.
agentwrench doctor check repaired.jsonl
agentwrench doctor apply repair-plan.json --json
# status="already_applied": verifies the same bytes without rewriting any file.
```

Or explicitly bind **in-place replacement with a backup**:

```sh
cp examples/doctor/codex/orphan.jsonl session.jsonl
agentwrench doctor preview session.jsonl --in-place --backup --manifest in-place-plan.json --json
# Creates only in-place-plan.json; session.jsonl is unchanged, no backup yet.
agentwrench doctor apply in-place-plan.json --json
# status="applied", backup=".../session.jsonl.bak-<UTC timestamp>".
agentwrench doctor apply in-place-plan.json --json
# status="already_applied", backup=null; no rewrite or duplicate backup.
```

`preview` accepts the same `--format`, `--strategy stub|drop`, and `--drop-invalid`
policy as `repair`. Choose exactly one of `--out DEST` or `--in-place --backup`;
`--backup` is required for in-place mode and forbidden with `--out`. It validates
the repair before saving anything, requires a new manifest path separate from the
session/output, and refuses existing `--out` paths (including symlinks). All parents
must exist. Preview does not create a repaired output, backup, or audit checkpoint.
Use `repair --dry-run` for a payload-containing diff.

The version 1 JSON manifest contains `source: {path, sha256}`, `output: {path, sha256}`,
`options: {format, strategy, drop_invalid}`, and `report: {file, format, issues, changes}`.
New-file previews still emit version 1, and existing v1 manifests remain supported.
In-place previews emit version 2: source/output paths must be identical and options
add `in_place: true, backup: true`. Missing/false backup consent or a different
destination is refused. The timestamped backup path is chosen only when applying.
Paths are absolute and parent aliases are resolved at preview time, so applying from
another working directory keeps the same destination. The manifest and JSON console
reports contain identifiers and repair descriptions, never argument/tool-output text.
Console reports add `manifest` and `status` (`previewed`, `applied`, or `already_applied`).
Apply also reports `backup`: the path created by that invocation, or null.

`apply` rechecks the **entire source SHA-256**, regenerates and validates the plan,
compares its report and rendered output hash to the preview, then publishes only those
bytes at the saved destination. There are no policy or destination override flags.
In new-file mode, changed source bytes are refused even if the destination already
matches. A byte-identical regular output is accepted as already applied. In-place
mode accepts either the original source hash (regenerates the reviewed plan before
backing up and replacing) or the expected repaired hash (verifies clean using the
preview's detected format, without writing). A clean in-place preview is already
applied on its first apply, with no backup or rewrite. Any other source bytes,
differing new-file output, symlink, special file, malformed/unsupported manifest,
or regenerated plan drift is refused without replacement.
Preview/apply exit **0** on success and **2** on refusal/failure. Re-preview changed
inputs using a new manifest (and a new output path for new-file mode).

The manifest is **not signed or authenticated**: review and protect it like any file
that directs writes, and apply only trusted manifests. Hashes bind input/output bytes;
they do not prove who approved the paths or policy. Source inode changes between
invocations are allowed if the content still matches. Both commands use cooperative
locks and detect source changes during processing, with the same non-cooperating-writer
and parent-directory race limits as repair. Stop session writers first. Preview saves
manifests atomically without overwriting. New-file apply uses atomic no-clobber
publication; in-place apply uses repair's fsynced exclusive mode-0600 backup followed
by atomic replacement, preserving ordinary source permission bits.
A failure after publication (for example, directory fsync or stdout failure) may leave
the completed artifact present; repeating apply verifies it. If a backup succeeds
but replacement fails, the original and backup remain; retrying may create another
backup before replacement. Already-applied verification checks content, not execution
history or backup existence, and does not recreate a deleted backup. It retains the
preview's original report; without original bytes it cannot regenerate that old plan.
Manifests do not support batch inputs or migration across changed repair algorithms;
regenerate a preview if an unapplied plan reports drift.

### Pin exact manifest bytes before apply

`apply MANIFEST [--expect-manifest-sha256 HASH] [--json]` optionally binds the
manifest to an independently retained full SHA-256. Retain that digest when
reviewing the trusted manifest, then supply it on first apply and reapply:

```sh
agentwrench doctor apply repair-plan.json --expect-manifest-sha256 "$REVIEWED_MANIFEST_SHA256" --json
```

HASH must be exactly **64 lowercase hex characters**, without normalization.
Invalid pins refuse before any input opens with error
`--expect-manifest-sha256 requires exactly 64 lowercase hex characters`.
A full manifest-byte mismatch refuses before manifest parsing, bound source or
output access, backup creation or staging with error
`manifest SHA-256 differs from --expect-manifest-sha256; no source opened`.
Both exit **2**: parsed JSON is exactly `{file: MANIFEST, error: ERROR}`;
stderr is `error: ERROR` plus a newline, and human stdout is empty. A missing
HASH is an argument error (stderr only, exit 2). These early refusals create
no output, backup, temporary file or lock file, including on already-applied
and clean no-op paths.

Pinned apply hashes and parses the **same locked regular-file descriptor** and
holds its shared lock through replay, publication and already-applied verification.
It rechecks the complete manifest digest, initial size/mtime/ctime and path
identity before source open, staging, backup creation, atomic publication and
successful return. Detected manifest changes refuse further writes; staging is
cleaned up. A completed backup can remain if a later recheck refuses replacement;
a failure after publication can leave the completed artifact. There is no rollback.
Stop writers: advisory locks and final rechecks cannot prevent non-cooperating
writes after a check or hostile parent-directory races.

Exact agreement continues existing strict v1/v2 schemas, reviewed policy and
destinations, replay/report/output drift checks, source locks/hash/identity checks,
private backups and no-clobber publication. Default apply behavior and JSON/human
success receipts are unchanged, with no added pin field. Already-applied and
clean in-place no-op checks create no duplicate backup or replacement.

The pin binds **exact bytes**, including whitespace, key order and trailing
newlines. Equivalent JSON with different bytes requires a different digest.
Retain it independently: computing a digest from a replacement manifest at apply
time cannot detect that replacement. Neither a matching digest nor a successful
apply proves author identity, approval, signatures, provenance or provider
resumability. Continue to review and protect the manifest's paths and policy.


## Recover original bytes into a new file

Use a trusted **v2 in-place manifest**, the explicit backup path returned by apply,
and a **new recovery destination** in an existing directory:

```sh
agentwrench doctor recover in-place-plan.json \
  --backup './session.jsonl.bak-<UTC-timestamp>' --out ./recovered-original.jsonl --json
# status="recovered"; restored_bytes is the exact copied byte count.
# output.sha256 equals the manifest's source.sha256.
```

Replace the example backup name with the actual path (quote it if needed).
`--backup FILE` and `--out NEW` are required; there is no backup auto-discovery.
The current session may be missing or changed. Recovery never opens or overwrites
it and cannot restore in place. Review the recovered copy separately before any
manual session replacement. Original bytes may still contain the pairing errors
that prompted repair; recovery copies bytes without parsing or repairing them.

Exit **0** publishes exactly the explicit backup bytes, after verifying their entire
SHA-256 against `manifest.source.sha256`. JSON contains only `status="recovered"`,
`version=2`, absolute `manifest`/`backup` paths, `source: {path, sha256}`,
`output: {path, sha256}` and `restored_bytes` (a nonnegative integer counted from
the copied bytes, including zero for an empty backup). The receipt is command
output, not a separate file. It does not echo the manifest's issue/change report
or session payloads. Human output is
`recovered: /absolute/destination (123 bytes) SHA-256 <hash>`. Neither receipt
certifies that the original session is clean or resumable.

Exit **2** refuses malformed/unsupported manifests (including v1), invalid v2
in-place/backup consent, paths or hashes, missing/unreadable/locked inputs,
symlink/special manifest or backup endpoints, changed/mismatching backup bytes,
missing output parents, and **any** existing output (even byte-identical). Path
aliases and hard-link collisions among manifest, backup, current session and
output are refused, including a missing current session used as the destination.
Parsed `--json` failures use the ordinary `{file, error}` envelope; argument parsing
errors go to stderr. Validation/publication refusals emit no success receipt.
Manifest, backup and current session bytes, inode, mode, mtime and ctime remain
unchanged; input reads may update atime.

Recovery uses shared advisory input locks, a private mode-0600 temporary sibling,
a verified copy, full input hash/identity/stat rechecks, fsync and atomic no-clobber
publication. A concurrent destination wins without being overwritten. Temporary
files are removed on failure. Failure after publication (such as directory fsync
or stdout failure) may leave a complete output; retry refuses it, so inspect that
file or choose another new destination. Stop input writers: advisory locks and
rechecks cannot eliminate non-cooperating writes after the final check or hostile
parent-directory races.

The manifest remains **unsigned trusted local metadata**. Protect and review its
policy, paths and hashes. Backup verification proves agreement with its recorded
original hash; it does not authenticate the manifest, prove a prior apply occurred,
or prove that a particular process created the backup. An explicitly supplied
copy of the original bytes is sufficient, even if apply never ran. Recovery does
not regenerate or authenticate the historical repair report or execution history.

## Verify an explicit backup without creating a recovery copy

```sh
agentwrench doctor verify-backup in-place-plan.json \
  --backup './session.jsonl.bak-<UTC-timestamp>' --json
```

`verify-backup MANIFEST --backup FILE [--expect-manifest-sha256 HASH] [--json]` accepts the same trusted v2
in-place manifest and explicit immutable regular backup as `recover`. It verifies
all backup bytes without parsing or repairing them; the current session may be
missing or modified and is never opened. No output, checkpoint, lock file or
temporary file is created. Use `recover ... --out NEW` when you want a copy.

Exit **0** returns metadata only: `{"status":"verified","version":2,
"manifest":"/absolute/plan.json","backup":"/absolute/backup",
"expected_sha256":"<manifest source hash>","actual_sha256":"<backup hash>",
"verified_bytes":123}`. Human output gives the backup path, byte count and hash.
There is no issue/change report or session payload in either output form.

Exit **2** refuses malformed/v1 manifests, invalid v2 policy/path/hash fields,
missing/unreadable/locked/symlink/special inputs, aliases among manifest, backup
and current session, mismatching backup bytes, and input byte/identity/stat changes
during reading. Parsed `--json` failures use `{file,error}`; argument errors go
to stderr. `--backup` is required: no discovery, implicit destination or `--out`
is supported. Both input locks remain held through full hash/identity/stat rechecks.
Manifest, backup and current-session bytes, inode, mode, mtime and ctime remain
unchanged; reads may update atime. Stop input writers; non-cooperating writes after
the final check and hostile parent-directory races remain outside guarantees.

The manifest is **unsigned trusted local metadata**. Hash agreement proves bytes
against its recorded original hash, not authenticity, prior apply execution or
backup creation provenance. An explicit original copy works even without apply.
Verification does not replay/authenticate the historical repair report or assert
that original bytes are clean or resumable.

### Pin manifest bytes before backup verification or recovery

`verify-backup MANIFEST --backup FILE [--expect-manifest-sha256 HASH] [--json]`
and `recover MANIFEST --backup FILE --out NEW [--expect-manifest-sha256 HASH] [--json]`
optionally bind the complete manifest bytes to an independently retained SHA-256.
HASH must contain exactly **64 lowercase hex characters**, without normalization.
Invalid pins refuse before either input opens with error
`--expect-manifest-sha256 requires exactly 64 lowercase hex characters`.
A mismatch with the existing full shared-locked manifest fingerprint refuses
before manifest parsing, backup open or recovery staging with error
`manifest SHA-256 differs from --expect-manifest-sha256; no backup opened`.
Both return exit **2**: parsed JSON is exactly `{file: MANIFEST, error: ERROR}`,
stderr is `error: ERROR` plus a newline, and human stdout is empty. A missing HASH
is an argument error (stderr only, exit 2). Refusals create no destination,
temporary file or lock file. Existing endpoint/alias and new-output restrictions
still apply; pins do not relax strict v2 in-place/backup policy.

Exact agreement continues all existing byte/hash/stat/identity/alias checks under
the same locks, including final manifest mutation checks, without an extra read
pass or current-session open. Default behavior and JSON/human success receipts
are unchanged, including arbitrary empty/binary/multichunk original bytes and
missing or modified current sessions. Recovery still publishes privately and
atomically to an explicit new destination, with no clobbering.

Retain the digest independently when reviewing the manifest. It binds **exact
bytes**: equivalent JSON with different whitespace, key order or trailing newline
requires a different pin. This is not a signature or proof of author identity,
approval, apply execution, backup creation provenance or provider resumability.

```sh
agentwrench doctor verify-backup in-place-plan.json --backup ./original-copy \
  --expect-manifest-sha256 "$RETAINED_MANIFEST_SHA256" --json
agentwrench doctor recover in-place-plan.json --backup ./original-copy --out ./restored-copy \
  --expect-manifest-sha256 "$RETAINED_MANIFEST_SHA256" --json
```

## Verify an explicit repaired output without the original source

```sh
agentwrench doctor verify-output new-file-plan.json --candidate ./repaired-copy.jsonl --json
# Explicitly read the current source bound by a v2 in-place manifest:
agentwrench doctor verify-output in-place-plan.json --candidate ./session.jsonl --json
```

`verify-output MANIFEST --candidate FILE [--expect-manifest-sha256 HASH] [--check-pairing] [--diagnostic-summary] [--json]` accepts a trusted **v1 new-file**
or **v2 in-place** repair manifest and one explicit immutable regular candidate.
It compares every candidate byte to `manifest.output.sha256`; original pre-repair
bytes and backups are not required. V2 requires the same strict `in_place: true`
and `backup: true` boolean policy as apply/recovery.

For v1 the candidate can be the intended output or a separate copy; the protected
source is never opened or accepted as a candidate. For v2 explicitly supply either
the bound current source path or a separate copy. Parent-directory aliases of the
bound path are accepted, but a differently named hardlink to the source is refused.
Final-component symlinks are refused. A copy must be separate from the source by
resolved path and device/inode. The manifest must always be separate from both.
These alias checks run before candidate open and again before success.

There is no discovery or implicit opening of the saved source/output path.
When verifying a copy, the current source may be missing, modified, locked or
nonregular; only its path/alias metadata is inspected. When explicitly verifying
the current source, it gets the same read-only regular-file open, shared lock,
full hash and identity/stat rechecks as any candidate. No output, checkpoint,
lock file or temporary file is created.

Exit **0** returns exactly `{"status":"verified","version":2,
"manifest":"/absolute/plan.json","candidate":"/absolute/candidate",
"expected_sha256":"<manifest output hash>","actual_sha256":"<candidate hash>",
"verified_bytes":123}` (version is 1 for v1). Human output gives the candidate
path, byte count and hash. Neither form echoes payloads or historical issues/changes.

Exit **2** refuses malformed/unsupported manifests, invalid version policy/path/hash
fields, missing/unreadable/locked/symlink/special explicit inputs, forbidden aliases,
mismatching candidate bytes, and input byte/identity/stat changes. Parsed `--json`
failures return `{file,error}`; argument errors are stderr-only. `--candidate` is
required; write options, format selection and repair replay are unsupported. Both
shared input locks stay held through full input hash/identity/stat and alias
rechecks. Input bytes, device/inode, mode, mtime and ctime stay unchanged; reads
may update atime. Stop input writers; non-cooperating writes after final checks
and hostile parent-directory races remain outside guarantees.

Hash agreement with an **unsigned trusted local manifest** proves content only.
It does not authenticate the manifest, replay its historical repair report, prove
apply execution or output provenance, or establish cleanliness/provider resumability.
The default remains hash-only and accepts even empty or arbitrary binary bytes
when their full hash matches.

Opt in with `--check-pairing` to also scan only the explicit matching candidate
under the same two shared locks. It uses the manifest's saved detected
`report.format`, so drop outputs that lost their last format marker still work.
Empty results are valid; a saved `empty` format requires an empty candidate.
Unknown records within a saved Codex/Claude format pass through as in `check`;
this is supported pairing validation, not full provider-schema validation.

Opt-in success retains the exact fields above and adds
`"pairing":{"format":"codex","clean":true}` (format may be `claude` or `empty`).
Human output is exactly `verified: PATH (N bytes) SHA-256 HASH; pairing clean [FORMAT]`
followed by a newline. Exit remains **0**. Non-clean, malformed, unsupported or
mixed/mismatched-format candidates refuse with exit **2**, JSON
`{"file":"MANIFEST","error":"candidate pairing check failed; no output published"}`,
and stderr `error: candidate pairing check failed; no output published` plus a
newline. Human refusal has only that stderr line. No issue identifiers, payloads,
or historical report details are emitted. Hash mismatches refuse before parsing;
input changes still refuse on the full hash/identity/stat checks after parsing.
No repair is planned/applied and no historical report is regenerated. Pairing
cleanliness does not prove authenticity, approval/apply execution, creator
provenance or provider resumability.

```sh
agentwrench doctor verify-output in-place-plan.json --candidate ./session.jsonl --check-pairing --json
```

### Optional manifest-byte pin

Supply `--expect-manifest-sha256 HASH` with an independently retained SHA-256
of the **complete manifest file bytes**, including whitespace and final newline.
HASH must be exactly 64 lowercase hexadecimal characters. Invalid values refuse
before opening either input, with error `--expect-manifest-sha256 requires exactly
64 lowercase hex characters`. A well-formed pin is compared to the full digest
already computed under the manifest's shared lock, before parsing the manifest
or opening/scanning the candidate. A mismatch refuses with error `manifest SHA-256
differs from --expect-manifest-sha256; no candidate opened`. Both errors exit **2**:
parsed `--json` returns exactly `{file,error}` with the manifest argument as `file`
and stderr `error: ERROR` plus newline; human mode has empty stdout and the same
stderr. A missing HASH is an argument error (stderr only, exit 2).

Exact agreement continues through the existing strict v1/v2 schema, source/copy
and alias policy, candidate verification and final full input hash/stat/identity
rechecks under the same locks. There is no separate check then reopen. Pinning
combines with `--check-pairing` and `--diagnostic-summary` without changing any
success or pairing-refusal receipt. Pin refusals have no diagnostic summary.
Semantically equivalent JSON with different bytes needs a different pin. Omitting
the flag retains all existing defaults, including hash-only arbitrary-byte checks.

Pinning binds caller-supplied bytes; it does not establish author identity,
approval, signature, provenance or historical apply execution. Retain the digest
independently when reviewing the trusted manifest; deriving it from a replacement
manifest at verification time cannot detect that replacement. No files are created.

```sh
agentwrench doctor verify-output plan.json --candidate ./repaired-copy.jsonl --expect-manifest-sha256 "$RETAINED_MANIFEST_SHA256" --check-pairing --json
```

### Optional pairing-refusal summary

`verify-output ... --check-pairing --diagnostic-summary [--json]` opts into
metadata-only explanations **only for pairing refusals after hash agreement**.
`--diagnostic-summary` requires `--check-pairing`; using it alone refuses with
exit **2** and the usual `{file,error}` JSON envelope (no summary).
Hash/stat/identity/alias/manifest/lock failures also retain that envelope.
Success receipts are unchanged, including when both flags are present.

A pairing refusal still exits **2**. JSON adds exactly
`"diagnostic_summary":{"reason":"pairing_findings","issue_counts":{"RD101":2}}`
to the existing `{file,error}` envelope. `error` and stderr remain the generic
`candidate pairing check failed; no output published` (stderr prefixed by
`error: ` and followed by a newline). Without `--json`, stdout is empty and
stderr adds exactly `pairing summary: REASON; issue counts: CODE=N, CODE=N`
plus a newline, with codes sorted lexicographically, or `none` for empty counts.
Without the summary flag, the prior generic refusal is unchanged.

Reasons, in precedence order:

- `format_refusal`: unsupported/undetectable or mixed/mismatched saved format,
  including nonempty content with saved `empty`. Counts are `{}` because a format
  refusal may stop scanning early; these are **not** a complete finding count.
- `malformed_records`: a completed scan contains RD001, RD002 or RD003. Counts
  include all scan findings, including any supported pairing findings.
- `pairing_findings`: a completed scan contains other supported pairing findings.

Counts describe diagnostics, not unique calls, records or repair actions. Only
codes and positive integer counts are exposed; no line numbers, call IDs, UUIDs,
parser messages, payloads or historical report entries. The single candidate scan
and all rechecks stay under the two existing locks; no source is opened implicitly,
repair planned/applied or artifact created. This does not expand supported checks
or prove provider resumability, complete schemas, provenance or authenticity.


## Resumable JSONL audit

`audit` is a read-only, single-file pairing audit with a persistent JSON checkpoint.
It reports **only newly observed diagnostics** and keeps their original 1-based
line numbers and **0-based byte ranges** (`byte_offset` inclusive,
`end_byte_offset` exclusive, including any CRLF/newline). The session is never
written. Choose a separate checkpoint path in an existing directory:

```sh
# Local synthetic input: one unusable output and one unfinished JSON object.
printf '%s\n' '{"type":"response_item","payload":{"type":"function_call_output"}}' > live.jsonl
printf '%s' '{"type":' >> live.jsonl
agentwrench doctor audit live.jsonl --checkpoint audit-cursor.json --allow-incomplete-final --json
# Exit 1: RD103 at line 1, bytes [0,67); offset=67.
# incomplete_final describes line 2, bytes [67,75).

# Finish that object, then resume with the SAME checkpoint.
printf '%s\n' '"event_msg","payload":{}}' >> live.jsonl
agentwrench doctor audit live.jsonl --checkpoint audit-cursor.json --json
# Exit 1: issues=[], issue_count=1, resumed_from=67, offset=101.
# RD103 still exists in the file, but is not emitted twice.
```

Without `--allow-incomplete-final`, any invalid JSONL record exits **2**. The flag
only defers a syntactically incomplete final JSON object **without a newline**;
the cursor stays at that record's first byte. Invalid interior records, duplicate
keys, non-finite constants, invalid UTF-8, malformed complete final records, and
malformed supported session structures still fail. Split final UTF-8 characters
inside a string may be deferred. Valid final objects without newlines are audited
normally. No invalid records are dropped and no repair is attempted.

On success, `offset` marks the accepted byte prefix; `incomplete_final` is null
or gives the deferred line's byte range. `issue_count` counts all current findings,
while `issues` contains only findings unseen by this checkpoint. Exit **0** means
no current pairing issues, **1** means current pairing issues even if all were
reported earlier, and **2** means refusal/failure. Missing-output findings describe
the current snapshot and can resolve after an output is appended; this mode does
not emit retractions. A diagnostic with the same code, location and details is
emitted once for the lifetime of the checkpoint.

Resume verifies the source path, device/inode, requested `--format`, and SHA-256
of the accepted prefix. Rewriting/truncating that prefix, rotating the file or
changing the format choice requires a new checkpoint; failures leave the old
checkpoint intact. The deferred tail may be completed or replaced. Source changes
detected during an audit are refused; pause writers for a stable snapshot. The
same cooperative-lock/non-cooperating-writer limits as repair apply.

Checkpoints are atomically replaced with mode 0600, contain only source identity,
byte cursor, prefix hash, diagnostic hashes and compact pairing/graph metadata,
and use a persistent sibling
`<checkpoint>.lock` to serialize audits. Payloads are never cached. The checkpoint
is committed before output is printed: a crash or broken output pipe in between
can lose that invocation's displayed diagnostics; start a new checkpoint to replay
them. This is cross-run deduplication, not transactional delivery to another system.

Version **2** checkpoints cache only identifiers, call/output kinds, Claude UUIDs,
parent/scope links, structural diagnostics, and byte ranges for records referenced
by that metadata. Resume **decodes only appended records**, including a previously
deferred tail once complete. It re-reads the accepted prefix as bytes to verify
SHA-256, then recomputes pairing over all cached metadata. Early outputs, missing
outputs, dangling parents and graph ambiguity therefore match a full replay of
the accepted snapshot. This avoids parsing old payloads; it does not promise
sublinear I/O, incremental graph work, or a measured speedup.

Normal `audit` refuses version 1, unknown-version and malformed checkpoints unchanged.
Choose **a new checkpoint path** to replay the entire source and **reset** diagnostic
deduplication. To preserve v1 diagnostic history instead, explicitly migrate into
another new path (stop session writers first):

```sh
agentwrench doctor migrate-audit ./session.jsonl --checkpoint v1-cursor.json \
  --out v2-cursor.json --json
agentwrench doctor audit ./session.jsonl --checkpoint v2-cursor.json --json
```

Migration requires an existing **version 1** audit checkpoint and verifies the source's
absolute path, device/inode, original requested `--format` choice and accepted-prefix
SHA-256. If the v1 audit used `--format codex` or `--format claude`, pass that same
choice to migration and subsequent audits; explicit and `auto` are distinct choices.
It decodes each record of **only the accepted prefix once** to build v2 pairing/graph
metadata. The byte cursor, source identity, prefix digest and entire emitted-hash
list are preserved. Unaccepted bytes are not audited or consumed, even if now
complete or invalid; the next audit handles them. Migration emits no diagnostics
and adds no diagnostic hashes. It leaves the session and old checkpoint unchanged.
A valid unterminated accepted record is supported; any following bytes must begin
with a JSON whitespace/newline separator. A cursor splitting JSON or followed by
concatenated nonseparator bytes is refused.

`--out` must be a **new path in an existing directory**. Migration publishes an
fsynced mode-0600 checkpoint atomically without overwriting an existing/concurrently
created destination. Inputs must be regular files; symlinks, special endpoints,
path/hard-link aliases among the session, checkpoints and their locks, stale or
truncated/rotated sources, format mismatches and malformed/unsupported checkpoints
are refused. Both checkpoint sibling locks are held; lock files may remain after
success or refusal. Detected changes to either input before publication also fail.
Exit **0** means migrated, even if the prefix has pairing findings; **2** means
refusal/failure. JSON success contains `file`, detected `format`, `status: "migrated"`,
`previous_checkpoint`, absolute destination `checkpoint`, `version: 2`, `offset`, and
`emitted_count`. Repeating migration to the same destination is refused; use that
checkpoint with `audit`. A failure after publication (directory fsync or stdout)
may leave the complete v2 checkpoint present; inspect it before resuming.

There is no automatic migration, v2-to-v2 copy, or recovery of invalid v1 state.
Checkpoints are unsigned trusted local files. Ordinary `audit` verifies source
hashes and the metadata schema, then **trusts cached parsing/graph metadata**;
it does not replay old records to detect well-formed cached-state edits.
Protect checkpoints like session metadata. They contain IDs, but no arguments,
message text or tool-output contents.

### Optional read-only v2 replay verification

To compare an existing v2 checkpoint's parsing metadata with a full replay of
its accepted prefix, stop source writers and use the original requested format:

```sh
agentwrench doctor verify-audit ./session.jsonl --checkpoint v2-cursor.json --json
# If the original audit used --format claude, pass --format claude here too.
# {"file":"session.jsonl","checkpoint":"v2-cursor.json","version":2,
#  "format":"codex","offset":123,"status":"verified","mismatches":[],
#  "history_verified":false}  (paths/offset shown only as an example)
```

Exit **0** / `status="verified"` means metadata matches, even if the accepted
prefix has pairing findings. Exit **1** / `status="mismatch"` means a strictly
well-formed cache differs from replay. `mismatches` lists only differing cached
field names, sorted: `events`, `format`, `issues` (structural findings), `lines`,
`locations` (byte ranges), `nodes` (Claude graph/scope), `terminated` (separator
state). Reports expose no cached/replayed values or diagnostic hashes. Human
output also names differing fields. Exit **2** is refusal/failure with the usual
JSON `{file,error}` envelope: invalid/v1/unknown schema, changed source identity,
requested format or prefix hash, truncated/rotated sources, ambiguous/split cursor,
invalid accepted records, unsafe/aliased/missing/locked inputs, or detected input
changes during verification. Explicit formats and `auto` are distinct choices.

Verification hashes **exactly the accepted prefix first**, decodes each accepted
record once, checks the replayed bytes' hash again, and compares all cached parsing
metadata. It supports zero cursors, UTF-8/CRLF ranges, valid unterminated objects,
and partial tails. Unaccepted records are never decoded, accepted or added to the
hash. For an unterminated accepted object, only the following whitespace separator
is inspected through LF/EOF to reject concatenation; the cursor remains unchanged.
A zero cursor ignores the entire tail.

The source, checkpoint and diagnostic history remain unchanged (reads may update
atime). There is no output file, temporary file, replacement checkpoint, cursor
advance, diagnostic emission or added diagnostic hash. The stable sibling
`<checkpoint>.lock` may be created/retained, as with `audit`; source/checkpoint
shared locks and that exclusive sibling lock are held throughout. Stop writers:
stat/identity/hash checks detect ordinary changes but cannot prevent a
non-cooperating write after the final check or hostile parent-directory races.

`history_verified` is always **false**. Verification validates historical hash
shapes but cannot prove those diagnostics were ever emitted, complete, or correct;
resolved/unknown historical hashes are preserved without regeneration. The
checkpoint is unsigned: replay agreement proves current metadata matches the
prefix identified by that checkpoint, not authenticity of its identity/hash,
author or diagnostic history. A mismatch does not repair or replace anything;
inspect the files before choosing a new checkpoint or other manual action.

A valid final object without a newline is still accepted. Before appending another
record, append a newline (LF or CRLF); JSON whitespace before that newline is also
accepted. Concatenating objects without a separator is refused without advancing
the checkpoint. Extending a record's byte range with whitespace/newline changes
its diagnostic identity and may emit that finding again, matching full replay.

Memory and checkpoint size grow with call/output count, Claude graph nodes,
structural diagnostics and historically emitted diagnostic hashes, rather than
unrelated payload-only records. Directory audits and checkpoint compaction are
not supported.

## Checks and repair policy

| Code | Meaning | Repair |
| --- | --- | --- |
| RD001 | Invalid UTF-8/JSON line (including duplicate keys/non-finite numbers) | Refuse unless `--drop-invalid`; then drop line |
| RD002 | Invalid final line without newline, possibly truncated | Drop final line |
| RD003 | Non-object JSON or malformed supported record structure | Refuse unless `--drop-invalid`; then drop record |
| RD101 | Codex call without later output | Insert interrupted output, or drop call |
| RD102 | Codex output without call | Drop output |
| RD103 | Codex output missing a usable `call_id` | Drop output |
| RD104 | Duplicate Codex call/output identifier | Keep first call and first later valid output |
| RD105 | Codex output before call | Drop early output; retain later valid output or repair call |
| RD106 | Codex output belongs to wrong tool family | Drop output and repair unsatisfied call |
| RD107 | Codex call missing usable identifier | Drop call; never invent an identifier |
| RD201 | Claude `tool_use` without later result in same chain | Insert error `tool_result`, or drop block |
| RD202 | Claude result has no eligible ancestor `tool_use` | Drop block |
| RD203 | Duplicate Claude result | Keep first valid result |
| RD204 | Claude dangling `parentUuid` | Set parent to `null` |
| RD205 | Duplicate Claude tool-use ID in one chain | Keep first use |
| RD206 | Claude tool block missing ID or in wrong message role | Drop block |
| RD207 | Duplicate UUID, cycle, or forward parent reference | Refuse repair; requires manual graph decision |

Codex pairing supports `function_call` / `function_call_output`, `custom_tool_call` / `custom_tool_call_output`, legacy `local_shell_call` / `function_call_output` (using `call_id`, or legacy `id`), and client `tool_search_call` / `tool_search_output`. Client search with a missing result requires **`--strategy drop`**: its output schema cannot carry an interrupted-text stub. Server-executed tool search, web search, image generation and unknown item types pass through. This is a pairing validator, not a complete upstream schema validator.

Claude results must be in later **user** lines descended from the assistant record through `parentUuid`, with matching `sessionId` and `isSidechain` along the chain. Sibling branches do not satisfy each other. Independent branches may reuse IDs. Inserting a result creates a deterministic UUID and links direct children in that scope through it. Removing all tool blocks from a message leaves a short explanatory text block so its UUID and descendants remain usable. Unknown blocks, unrelated fields, and metadata pass through.

Untouched lines remain byte-identical and in order, including CRLF and valid final lines without a newline. Changed Claude records are serialized again, preserving unrelated JSON fields. Inserting after an unterminated valid final record adds a necessary separator newline. Existing Codex `ordinal` values are preserved; synthetic records omit `ordinal` rather than renumbering history.

## File safety and limits

- `repair` requires `--out`, `--in-place --backup`, or `--dry-run`. There is no implicit in-place mode. Dry-run validates the same plan without creating temporary files, backups, or outputs. With `--json`, it emits a metadata-only plan instead of a diff.
- Writes use a private temporary sibling and atomic publication. A real in-place edit first creates and fsyncs `<file>.bak-<UTC timestamp>` using exclusive creation. Output/backup files are mode 0600; in-place replacement preserves ordinary source permission bits. Owner, ACLs, xattrs, and hard-link relationships are not preserved.
- The source's identity and SHA-256 are rechecked before publication. Cooperative advisory file locks are used. Agents may ignore these locks: **stop live writers**. `check`/`repair` warn on modification within 5 seconds. There is no transactional guarantee against a non-cooperating writer racing the final replacement, or an attacker controlling parent directories.
- Only regular files are accepted. Symlink inputs, FIFOs, devices, existing output paths (except verified manifest reapply), and mixed formats are refused. Output parents must exist. Repair destinations come from an explicit caller-supplied `--out` or its saved manifest; session payloads never supply paths.
- Scanning parses one line at a time and retains compact IDs/graph metadata, not message payloads. Memory is proportional to the longest line plus call/result and Claude graph counts. One exceptionally long line must fit in memory. Repeated identical IDs across very many independent Claude branches can increase pairing work.
- No repair of Codex SQLite databases, compaction semantics, reasoning incompatibilities, provider size limits, or complete API message-order rules. Unknown/new tool types are not validated. Partial graph extracts may produce dangling-parent findings. Final invalid lines without newlines are a heuristic, not proof of truncation. Backups are not automatically restored or deleted.

## Development

Tests run offline using synthetic fixtures for every code, CLI end-to-end tests, seeded pairing mutations, sidechain isolation, byte preservation, idempotency, a 4,001-node chain, 2 MB lines, and failure injection for backups/source changes and saved-manifest publication.
Incremental audits verify append-only decoding against full replay for both formats,
including early/duplicate outputs, Claude branch/scope isolation, late parents,
malformed state, incomplete-tail completion and newline boundaries. Manifest cases cover exact output hashes, saved policies, stale inputs, conflicting destinations, cross-directory apply, in-place backup consent/failures, already-applied no-ops without duplicate backups, and explicit backup recovery with hash/alias/race refusals. Fixtures live in `tests/doctor/fixtures/`; their generator is `tests/doctor/make_fixtures.py`.

From the repository root:

```sh
uv run --extra test pytest tests/doctor
```

## Provenance

This independent implementation interoperates with observable file formats; it does not include upstream source code. Codex is Apache-2.0 licensed; Claude Code is proprietary and is used only as an interoperability reference. There is no affiliation or endorsement. Sources and license distinctions are in [THIRD_PARTY.md](../THIRD_PARTY.md); agentwrench's license is in [LICENSE](../LICENSE).
