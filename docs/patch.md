# agentwrench patch

`agentwrench patch` parses Codex-style `apply_patch` (V4A) envelopes, applies a
multi-file patch with validation and rollback, saves and re-checks preview
plans, converts text patches to and from Git unified diffs, and explains hunk
matching. The `patch` subcommand and its Python API (`agentwrench.patch`) use
only the Python standard library at runtime and never invoke a shell or
external tool. Git is used only in offline interoperability tests.

## Install

Requires Python 3.11 or newer.

```sh
pip install agentwrench          # or: pipx install agentwrench
agentwrench patch --help

# Or run without installing:
uvx agentwrench patch apply change.patch --root ./project --dry-run
```

Every command also works as `python -m agentwrench patch ...`.

## Run

All patch arguments accept a filename or `-` for stdin. Target-tree commands
require `--root`; relative paths are interpreted against it, never the patch's
own directory. Conversion and explanation are read-only. The `examples/patch/`
inputs below live in the agentwrench repository; run those commands from the
repository root (or adjust the paths). `change.patch`, `change.diff` and
`./project` stand for your own patch and target tree. The commands below are
independent invocation forms, each against the original base tree; they are not
one sequential script.

```sh
agentwrench patch parse examples/patch/simple.patch --json
agentwrench patch explain examples/patch/bom.patch --root examples/patch/bom_tree
agentwrench patch apply change.patch --root ./project --dry-run
agentwrench patch apply change.patch --root ./project --check-only > diagnostics.json
agentwrench patch check-unified change.diff --root ./project > locations.json
agentwrench patch apply change.patch --root ./project --atomic
agentwrench patch preview change.patch --root ./project --output change.plan.json
agentwrench patch check-plan change.plan.json --root ./project --json
agentwrench patch apply-plan change.plan.json --root ./project --check-only
agentwrench patch apply-plan change.plan.json --root ./project --dry-run
agentwrench patch apply-plan change.plan.json --root ./project
agentwrench patch to-unified change.patch --root ./project > change.diff
git -C ./project apply --check ../change.diff
agentwrench patch from-unified change.diff --context 3 > converted.patch
# Pure renames need source context, so pass a base tree:
agentwrench patch from-unified rename.diff --root ./project > rename.patch
```

`--atomic` is accepted for compatibility; application is always all-or-nothing.
`to-unified` and `from-unified` take `--context N` (1..1000, default 3).

Example input (`change.patch`), with `hello.txt` initially containing `old\n`:

```diff
*** Begin Patch
*** Update File: hello.txt
@@
-old
+new
*** Add File: nested/note.txt
+created
*** End Patch
```

`apply` prints `Applied 2 file operations.` and creates `hello.txt` containing
`new\n` and `nested/note.txt` containing `created\n`. `--dry-run` prints
`Validated 2 file operations.` and creates **no files or directories**, not even
a staging directory. To try the supplied multi-file example without changing it,
copy its tree to a scratch directory first:

```sh
mkdir -p scratch
cp -R examples/patch/simple_tree/. scratch/
agentwrench patch apply examples/patch/simple.patch --root scratch --dry-run
agentwrench patch to-unified examples/patch/simple.patch --root scratch | git -C scratch apply --check -
agentwrench patch apply examples/patch/simple.patch --root scratch
```

### Machine-readable check-only diagnostics

`agentwrench patch apply change.patch --root ./project --check-only` validates in
memory and always emits a single JSON document to stdout. It never calls commit, stages
files, creates directories, or applies changes. It accepts stdin (`-`) and is
mutually exclusive with `--dry-run`. No `--json` flag is needed. Format
`v4a-check`, version `1`, has `format`, `version`, `ok` and `diagnostics` fields.
Exit **0** means the whole patch validates, **1** means tree/context/I/O failure,
and **2** means malformed/unsafe/unsupported input. Input and validation failures
are JSON too, with empty stderr; invalid CLI arguments still use argparse's
usage message on stderr.

For example, let `project/old.txt` contain the bytes `head\r\nold` (CRLF followed
by an unterminated last line), and check this patch:

```diff
*** Begin Patch
*** Update File: old.txt
*** Move to: nested/new.txt
@@
 head
-old
+new
*** End Patch
```

The command exits 0 with `ok: true`. Its single diagnostic includes:

```json
{
  "code": "HUNK_MATCHED",
  "path": "old.txt",
  "destination_path": "nested/new.txt",
  "hunk": 1,
  "status": "matched",
  "patch_span": {"start_line": 4, "end_line": 8},
  "source_span": {"path": "old.txt", "start_line": 1, "end_line": 3},
  "result_span": {"path": "nested/new.txt", "start_line": 1, "end_line": 3}
}
```

The original file remains byte-for-byte unchanged and `nested` is never created.
The hypothetical result is `head\r\nnew\r\n`, following the existing update
newline policy. `diagnosis.crlf` and `diagnosis.missing_final_newline` are true.
Replace `-old` with `-missing` to get exit 1, `ok: false`, `CONTEXT_NOT_FOUND`,
the same patch span, and null source/result spans. A separate `candidate` offers
an approximate window; it is never presented as an exact match.

All new `*_span` ranges are **one-based, half-open**: start inclusive, end
exclusive. Patch spans refer to the original physical input, counting leading
blank lines/heredoc wrappers, and include `@@` and `*** End of File` when present.
An implicit hunk starts at its first content line. Source/result spans include
the hunk's context as well as changed lines, exclude the `@@` anchor, and use
logical text lines: CRLF is one boundary, a BOM adds no line, and an unterminated
last line still counts. Result coordinates account for earlier hunks' line-count
changes and use the move destination. An insertion has an empty source range;
a deletion has an empty result range. EOF insertion into an N-line file is
`[N+1,N+1)` in the source; an empty file uses `[1,1)`. Tolerated final empty
sentinels are excluded from file spans, while remaining inside the patch span.

| Stable code | Meaning |
| --- | --- |
| `HUNK_MATCHED` | Matched at any supported tier; `tier` identifies which |
| `CONTEXT_NOT_FOUND` | Hunk old context cannot be located |
| `ANCHOR_NOT_FOUND` | The `@@ text` anchor cannot be located |
| `SOURCE_MISSING` | An update/delete source is absent |
| `ADD_TARGET_EXISTS` | An add would overwrite a file |
| `MOVE_TARGET_EXISTS` | A move would overwrite its destination |
| `INVALID_PATCH` | Syntax, encoding, unsafe path, unsupported representation, or resource limit |
| `VALIDATION_FAILED` | Other tree validation failure, such as a non-directory parent |
| `IO_ERROR` | The patch or target tree could not be read |

Hunks are reported in patch order, including later hunks after a context failure.
If any hunk in a file fails, that file has no planned result: all its
`result_span` values are null, including earlier matched hunks. Other valid
files can still have hypothetical result spans; `ok: false` always prevents
application of the entire patch. A file-level conflict has `hunk: null`, the
whole operation's patch span, null file spans, and replaces its hunk reports.
Successful adds/deletes have no hunks and produce no diagnostics. Fatal input,
path-safety, resource or I/O errors stop validation and emit one error; unknown
locations are null. Codes and v1 span semantics are stable; human-readable
`reason` and `suggested_fix` text may evolve. This is a check of the current tree,
not a saved precondition for a later apply; use a preview plan for that.

`plan(...).diagnostics` and `explain --json` expose the same additive code/span
fields. Parsed AST nodes now also include exclusive `end_line`; a hand-built or
unlocated imported AST has null patch spans until serialized and reparsed as V4A.
Existing `candidate.start_line`/`end_line` remain inclusive, with an approximate
range on failures; consumers needing exact locations should use the new spans.

### Check unified source locations exactly

```sh
agentwrench patch check-unified change.diff --root ./project
agentwrench patch check-unified - --root ./project < change.diff
```

This read-only command accepts a unified diff and explicit base tree. It checks
all context/deletion lines at each declared **old-file coordinate**, against the
original source for every hunk. It never searches another occurrence, trims
context, uses V4A's fuzzy matching, applies changes, stages files, or writes a
converted patch. LF-delimited source lines retain their endings: CRLF, BOM bytes,
whitespace and a missing final terminator are significant. A diff body line
without a final physical LF is interpreted as LF-terminated, as by the importer;
explicit `\ No newline at end of file` markers remain unsupported.

For `@@ -N,C +M,D @@`, a nonempty old range selects source lines `[N,N+C)`
(one-based, half-open). `-N,0` selects the gap **after N lines**: `-0,0` is BOF,
`-L,0` is EOF for an L-line file, and N > L fails. Empty-source insertion uses
`-0,0`. Unlike V4A conversion, checking permits zero-context insertions without
inventing old context. It checks only that their gap exists. New ranges receive
syntax/consistency validation; no result tree is constructed.

For example, a source containing `old\nkeep\ngap\nold\nkeep\n` matches
`@@ -4,2 +4,2 @@` with body `-old`, `+new`, ` keep`. Declaring `-2,2 +2,2`
returns exit 1 with `LOCATION_MISMATCH`, even though that old text occurs twice
elsewhere. No source content appears in the report:

```json
{
  "hunk": 1,
  "code": "LOCATION_MISMATCH",
  "status": "failed",
  "old_range": {"start": 2, "count": 2},
  "new_range": {"start": 2, "count": 2},
  "patch_span": {"start_line": 3, "end_line": 7},
  "source_span": {"start_line": 2, "end_line": 4},
  "mismatch_line": 2
}
```

Output is always one JSON document: `format: "v4a-unified-check"`, `version: 1`,
`ok`, `files`, and `diagnostics`. Each file has one-based `file`, `kind`, `path`,
nullable `old_path`/`new_path`, `ok`, `code`, `patch_span`, and ordered `hunks`.
Hunks have one-based `hunk` and the fields above. All spans are one-based,
half-open. `source_span` is the **declared** range, including on failure; it is
not a discovered match. `mismatch_line` identifies the first unequal old line,
or is null for matches, out-of-range locations and unreadable/missing sources.

| Code | Meaning |
| --- | --- |
| `HUNK_MATCHED` | Every old line, or the empty insertion gap, matches |
| `LOCATION_MISMATCH` | At least one old line differs at the declared location |
| `LOCATION_OUT_OF_RANGE` | Old range or empty gap exceeds the source |
| `SOURCE_MISSING` | Update/delete/rename source is absent |
| `ADD_TARGET_EXISTS` | Added file's target is already present |
| `LOCATIONS_MATCHED` | File's hunks all match |
| `SOURCE_PRESENT`, `SOURCE_ABSENT` | Metadata-only operation's required presence/absence matches |
| `INVALID_DIFF` | Malformed/unsafe/unsupported input, source representation or resource limit |
| `ROOT_UNAVAILABLE`, `IO_ERROR` | Root unavailable, or input/source read failed |

Exit **0** requires all files/hunks to match. Exit **1** reports valid input with
source/location/I/O/root failures; checking continues through later files/hunks
where possible. Source conflicts retain each hunk's declared coordinates.
Exit **2** rejects invalid input, unsafe filesystem paths, unsupported encodings
or representations, and resource excesses. Fatal errors return empty `files`
and one `diagnostics` entry with `code` and nullable `patch_line`. These bounded
errors never echo patch/source contents. Stderr is empty for these outcomes;
CLI argument misuse still uses argparse's usage/exit 2.

The shared strict importer parser validates paths, path sets, metadata, counts,
10-digit numeric fields and representation rules before source access. Existing
refusals (including missing-newline markers, mixed/newline-changing hunks,
BOM-changing updates, binary patches and unsupported modes) still apply.
Input/source limits are 8 MiB, 100000 input LF/source logical line endings,
1000 operations and 64 MiB total source reads. Limits apply to all sources,
including metadata-only operations. No canonical V4A output budget applies
because no conversion is performed.

This is a source-location snapshot, not a Git applicability test or saved plan.
Metadata-only operations check presence/absence, with no content/mode claim.
Rename destinations are path-safety checked but their availability is not tested.
Permission bits are not preconditions. Exclude concurrent writers; a later
apply or conversion keeps its own semantics and does not consume this report.

### Save and apply a preview plan

`preview` validates the entire patch without staging or changing target files,
then emits a self-contained JSON plan. `--output` creates a new file, refusing
to overwrite any existing file or overlap a patch path. Without `--output`, JSON
goes to stdout. Output parent directories must already exist. Keep plan files
outside the target tree when you want the whole tree to remain unchanged.

For the `hello.txt` / `nested/note.txt` example above:

```sh
agentwrench patch preview change.patch --root ./project --output change.plan.json
python -c 'import json; print(json.load(open("change.plan.json"))["diff"], end="")'
agentwrench patch apply-plan change.plan.json --root ./project --dry-run
agentwrench patch apply-plan change.plan.json --root ./project
```

The final two commands print `Validated 2 file operations.` and
`Applied 2 file operations.` respectively. The original patch file is no longer
needed. `apply-plan` also accepts `-` for a plan on stdin.

Format `v4a-plan`, version `1`, has exactly these fields:

| Field | Meaning |
| --- | --- |
| `format`, `version` | Format identifier and integer version |
| `root` | Absolute resolved target root; `apply-plan --root` must resolve to it |
| `patch` | Complete canonical V4A envelope |
| `preconditions` | Every source and add/move destination mapped to `sha256` and `mode` |
| `diff` | Git unified preview, regenerated and compared before application |

For an existing file, `sha256` is the lowercase SHA-256 of its original bytes
and `mode` is four octal permission digits such as `0644` or `0751`. An expected
absent path has both fields set to JSON `null`; an empty file has a real hash.
Any touched file's byte/mode change, removal, new add/move destination, or unsafe
filesystem substitution causes exit **1** with `tree changed since planning`.
The **entire target remains unchanged**, including unrelated files, permissions,
and directories; even a dry run checks these preconditions before staging.
For example, append a line to `project/hello.txt` after preview: application fails
even though the original `old` context still matches, and `nested/note.txt` is
never created. Generate a new preview to accept the changed base.

Malformed/version-unsupported plans, duplicate JSON keys, incomplete path
coverage, inconsistent operation preconditions, and a patch/diff disagreement
cause exit **2** without target writes. Update/delete sources (including move
sources) require a present-file fingerprint, even for an empty file; add/move
destinations require two nulls for absence. These role requirements are checked
before target-tree access, even if other paths are stale or the root differs.
A different root causes exit **1**. Unrelated file changes do not invalidate a
plan. Plans are limited to 64 MiB including JSON and the diff; the patch and
source/result limits below still apply. Successful application normally makes
the old plan stale. Preconditions track content, permissions and absence, not
inode identity or edit history: a restored base, or a no-op patch, can match again.

Plan files contain source text in the diff and carry no signature or approval
authentication. Review and retain the whole file; editing both the patch and
its matching diff can create a different valid plan. Loading a plan uses the
same rollback transaction and concurrent-writer limits as ordinary `apply`.

### Check every saved-plan precondition

`agentwrench patch check-plan change.plan.json --root ./project --json` checks a
saved preview without applying, staging, or creating files/directories. It
accepts `-` for stdin.
`agentwrench patch apply-plan change.plan.json --root ./project --check-only`
performs the same check and always emits the same `v4a-plan-check` v1 JSON, including on input/I/O
failures; no `--json` flag is needed. `--check-only` and `--dry-run` are mutually
exclusive. Both JSON commands report **every failing path**, in patch order
(source then move destination), with all applicable mismatch codes. Unrelated
edits are ignored. Without `--json`, `check-plan` prints `Plan is valid.` or a
line for each failing path. Ordinary `apply-plan` and `--dry-run` retain their
text output and first-failure behavior.

For the saved `hello.txt` / `nested/note.txt` example, append `extra\n` to
`hello.txt`, change its mode from `0644` to `0600`, and create an empty
`nested/note.txt`. The command exits **1**, leaves the entire current tree
unchanged, and returns the following for either JSON check command:

```json
{
  "format": "v4a-plan-check",
  "version": 1,
  "ok": false,
  "diff_verified": false,
  "diagnostics": [
    {
      "path": "hello.txt",
      "codes": ["BYTES_MISMATCH", "MODE_MISMATCH"],
      "expected": {"sha256": "01d09d19c2139a46aebfb577780d123d7396e97201bc7ead210a2ebff8239dee", "mode": "0644"},
      "actual": {"sha256": "f6871d6832d3477caa7ca1963fa9e7dbed88fef7340124d5ac41f50a77f2819a", "mode": "0600"},
      "reason": "tree changed since planning"
    },
    {
      "path": "nested/note.txt",
      "codes": ["ABSENCE_MISMATCH"],
      "expected": {"sha256": null, "mode": null},
      "actual": {"sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "mode": "0644"},
      "reason": "tree changed since planning"
    }
  ]
}
```

Hashes are lowercase SHA-256; the observed mode of a newly created file depends
on how it was created.
Fingerprints use raw bytes, including BOMs, newline endings and non-UTF-8 drift.
An absent path has two null fields, distinct from an empty file. Removing a source
also produces `ABSENCE_MISMATCH`, with its saved fingerprint in `expected` and
two nulls in `actual`. Presence changes use only this code; byte/mode comparisons
apply when both versions are present. Byte codes precede mode codes.

| Code | Meaning |
| --- | --- |
| `BYTES_MISMATCH` | Existing file's SHA-256 differs |
| `MODE_MISMATCH` | Existing file's permission bits differ |
| `ABSENCE_MISMATCH` | Expected file removed, or expected absent path occupied |
| `UNSAFE_PATH` | Symlink, non-regular file, or non-directory parent; no read through it |
| `IO_ERROR` | Input or tree path cannot be read |
| `RESOURCE_LIMIT` | Path exceeds 8 MiB or the remaining 64 MiB source-read budget |
| `ROOT_MISMATCH` | Supplied root resolves differently from the saved root |
| `ROOT_UNAVAILABLE` | Saved root is no longer a directory |
| `INVALID_PLAN` | Invalid encoding/JSON/schema/patch, size limit, or saved diff disagreement |
| `VALIDATION_FAILED` | Other validation failure, including a concurrent change during rechecking |

Exit **0** means all preconditions and the regenerated diff validate: `ok` and
`diff_verified` are true and `diagnostics` is empty. Exit **1** means tree, root,
resource or I/O failure; exit **2** means invalid plan input. All these outcomes
use the same JSON envelope and empty stderr with `--json` or `--check-only`;
CLI argument misuse still uses argparse's stderr/exit 2. Unknown fingerprints are JSON null (not a
two-null absence object); input/root/global errors have `path: null` and null
`expected`/`actual`. Codes and v1 fields are stable; reason text may change.

Schema, patch safety, complete path coverage and operation precondition roles
are checked before reading target files. Unsafe, unreadable and oversized paths
get diagnostics while checking continues with later paths; paths beyond the
source budget may have unknown bytes and report `RESOURCE_LIMIT`. A stale base
cannot verify the diff: `diff_verified` stays false, and a tampered diff may remain undetected until the
preconditions match again. On a matching base, the full `load_plan` validation
regenerates and compares the saved diff. The check is a snapshot, not a lock or
permission to skip `apply-plan`'s own rechecks; exclude concurrent writers.
Edited preconditions with inconsistent operation roles (such as an absent update
source or a present add destination) instead return exit 2 with `INVALID_PLAN`,
a reason naming the inconsistent path, and null path/fingerprints in the global
diagnostic. This input error takes priority over stale paths.
Likewise, a plan touching both a file and its descendant (for example `node`
and `node/child`) returns `INVALID_PLAN`/exit 2 before root checks or file reads,
with both conflicting paths in the reason. All source and destination paths
participate, in either operation order.

The `examples/patch/bom.patch` explanation (see the commands above) includes:

```text
hunk 1 of app.js: context matched (exact)
  best candidate: lines 1-1 (similarity 1.0000)
  diagnosis: bom, crlf
  suggested fix: Match succeeds; original context bytes, BOM and line endings are preserved.
```

A context failure produces exit 1, a candidate line range, a similarity score,
diagnosis flags and a concrete suggestion. `explain --json` returns an `ok`
boolean and `diagnostics` list. Each hunk report has `path`, `hunk`, `status`,
`tier`, `candidate`, `diagnosis`, `matches`, `eof_fallback`, and `suggested_fix`.
File-level failures (missing source/existing add or move destination) have a
reason and suggestion without a candidate. `parse --json` emits the AST with
file operations, hunks, operation/text pairs and original source line numbers.

Exit codes: **0** success (including explainable fuzzy matches), **1** application,
context or I/O failure, **2** malformed/unsafe/unsupported input. No diff output
is emitted when conversion fails. Ordinary `apply` failures include diagnostics
on stderr. Invalid grammar reports a patch line number.

## Python API

```python
from agentwrench.patch import (parse, plan, commit, apply, to_unified, from_unified,
                               preview, load_plan, check_plan, check_unified)

patch = parse(open("change.patch", encoding="utf-8").read())
prepared = plan(patch, "./project")       # reads only; prepared.ok and diagnostics
print(to_unified(prepared))              # git-compatible diff; no subprocess
commit(prepared, dry_run=True)           # validates; no writes
apply(patch, "./project")                # fresh plan + transactional application
# For an unchanged base, save preview(patch, "./project") to a UTF-8 file;
# later load it and commit(load_plan(saved_json, "./project")).
```

`PatchError` (also importable from `agentwrench.patch`) denotes
invalid/unsupported input; `ApplyError` includes structured
`diagnostics`. `commit` rechecks bytes and permissions to reject a stale plan.
This check runs before staging, including dry runs, and again before file writes.
`preview(patch, root)` returns JSON text; `load_plan(text, root)` verifies it
without writes and returns a normal `Plan` for `commit`. Plans are snapshots,
not reusable live transactions.
`check_plan(text, root)` returns the `v4a-plan-check` report dictionary and raises
`PatchError` for invalid plan input; I/O or final revalidation errors can raise
`OSError`/`ApplyError`. The CLI maps these exceptions to the JSON contract above.

`check_unified(text, root)` returns the `v4a-unified-check` dictionary. Invalid
syntax, unsafe paths, unsupported representation and resource excess raise
`PatchError`; root/global I/O errors may raise `OSError`. The CLI maps them to
bounded JSON errors. Per-source I/O failures are included in file/hunk reports.

## Matching and format mapping

Matching checks the original file in hunk order: exact, trailing-whitespace
insensitive, trimmed, then trimmed with common Unicode punctuation/spaces
normalized. A later exact match wins over an earlier fuzzy match. `@@ text`
finds an anchor and starts the hunk search after that line. An insertion without
old lines appends to EOF, as upstream does. Duplicate context selects the first
eligible match and is flagged `ambiguous_match`; add unique context to disambiguate.
Reports list at most 100 matching positions. Unicode punctuation is normalized
only for finding context; unchanged context keeps its original bytes.

`*** End of File` first tries the tail with all four tiers, then falls back to
searching from the current cursor with all four tiers. This intentionally fixes
the divergence reported in [Codex #34041](https://github.com/openai/codex/issues/34041):
the upstream revision referenced below implements only the tail stage despite
documenting fallback.
A final empty old-line sentinel is retried without that line if needed.

UTF-8 BOM is excluded from matching and preserved on update/move. Original
context line endings (even mixed LF/CRLF/CR) survive; inserted/replaced lines
use the first existing line ending, defaulting to LF. Like upstream, an update
adds a terminator to an unterminated last line. Add File creates LF-terminated
lines (or a zero-byte file if no content lines).

| Unified diff operation | V4A representation / boundary |
| --- | --- |
| Add/delete | Add/Delete File, including Git's metadata-only empty files |
| Text update | Context and change lines; hunk counts and coordinate consistency checked |
| Rename with edits | Update File + Move to; explicit Git rename headers required |
| Pure rename | `from-unified --root` reads source context; empty regular files become delete+add |
| Executable update/move/delete | Existing permission bits preserved by application; Git export uses canonical regular/executable modes |
| C-quoted/space/UTF-8 paths | Parsed and escaped for Git; unsafe/control-character paths rejected |

`from-unified --context N` retains up to N leading/trailing context lines per
hunk and preserves internal context. It cannot invent context absent in the
diff. Line numbers have no V4A equivalent: textual context chooses placement,
so repeated context may select a different occurrence than Git's numeric hunk
hint. Use `check-unified` to check strict source locations independently;
inspect `explain` and increase context when needed. Delete File cannot
encode the old-content precondition of a Git deletion. Apply converted patches
only to the intended base tree; a V4A deletion removes the current file.

Each authoritative Git metadata key (`rename from`, `rename to`, `new file mode`,
`deleted file mode`) may appear **once per file record**. A second declaration
raises `PatchError` at its physical input line with `duplicate git metadata: KEY`,
even when values are equal or C-quoted. Duplicate detection precedes decoding
the replacement value, so an earlier unsafe path cannot be overwritten into an
accepted rename. Existing errors in the first declaration still take precedence.
For example, `rename from ../outside` followed by `rename from old` rejects at
the second declaration. The whole diff is parsed before root/source access,
including when a duplicate follows an earlier pure rename. `from-unified` exits
**2** with empty stdout; `check-unified` exits **2** with `INVALID_DIFF` and the
duplicate's `patch_line`, empty `files` and empty stderr. Neither reads sources
or changes the tree. Single declarations, supported key ordering and repeated
informational `index`/`similarity index` hints retain their behavior.

Each unified hunk start/count field supports at most **10 decimal digits**,
including leading zeroes; omitted counts still mean one. Longer fields raise
`PatchError` at the hunk header; the CLI exits **2** with empty stdout and no
traceback. This bound is checked before integer conversion and is independent
of Python's configurable integer digit limit. Ten-digit starts through
`9999999999` remain supported; counts must still match the supplied hunk lines
and coordinates must remain consistent. Numeric offsets do not control V4A
matching or placement.

Unified import validates the final file path set before returning a patch or
emitting V4A. A diff adding `node` and deleting `node/child`, in either order,
exits **2** with both paths in the reason and empty stdout. Duplicate paths,
overlapping rename sources/destinations, and more than 1,000 resulting file
operations are also rejected. An empty-file pure rename counts as two operations
(delete + add). Shared-directory siblings and prefix-only names such as `node`
and `node2/child` remain valid. Import never writes or stages target files;
the original diff path set is checked before source access. Pure renames may
read source context with `--root`; expansion to empty-file delete/add operations
receives another final path-set check.

Pure-rename source reads have independent **8 MiB per-file** and **64 MiB total**
raw-byte budgets per import. BOM bytes and CRLF endings count; empty files consume
zero bytes. Stat-known excesses reject before opening the source. Each subsequent
read is capped at the smaller of the per-file and remaining aggregate allowance,
plus **one detection byte** for growth after stat. Overflow rejects before decoding
or constructing that source's context, and later sources are not read. Python
raises `PatchError`; `from-unified` exits **2** with empty stdout and a source-budget
reason (aggregate excess names `64 MiB`). Ordinary hunks use the diff's context and
do not read tree sources during import. Source-budget equality is allowed, but the
separate canonical output budget below can still reject the import.

The canonical V4A output must also fit the parser's **8 MiB UTF-8 byte** and
**100,000-line** budgets. Counts include the envelope, file/move headers, hunk
markers, context prefixes and final newline, across all operations. Equality is
accepted. A small pure-rename diff can expand beyond these limits: a source with
100,000 LF-terminated `x` lines fits the source limit, but its imported patch
has 100,005 lines and is rejected. Python `from_unified` raises `PatchError`;
the CLI exits **2** with `converted V4A patch exceeds 8 MiB or 100,000-line limit`
and empty stdout. Source bytes, permissions and directories remain unchanged.

Unified export uses the same **8 MiB UTF-8 byte** and **100,000 LF** budgets
as unified input. The complete diff counts, including quoted paths, mode/rename
metadata, hunk headers, body prefixes and missing-final-newline markers, across
all file changes. Equality is accepted. A three-line V4A deletion of a source
with 100,000 LF-terminated `x` lines would produce 100,005 unified lines and is
rejected. Python `to_unified` raises `PatchError`; `to-unified` exits **2** with
`exported unified diff exceeds 8 MiB or 100,000-line limit` and empty stdout.
Preview creation and saved-plan diff regeneration share this check, before any
output file creation or staging. Target bytes, modes and directories are unchanged.
Within-budget exports retain existing Git compatibility and import representation
limits (for example, missing-final-newline exports still cannot import as V4A).

## Safety and transaction limits

All paths must be relative, without empty components, `.`/`..`, backslashes,
drive prefixes, control characters, leading/trailing whitespace, or `.git`
components. Symbolic links and non-regular files are rejected, including links
in parent directories. A path may occur only once in a patch, including move
destinations: rename cycles and delete-then-add of the same path are rejected.
Planning also rejects file/descendant path overlaps before target-tree access,
including move sources/destinations and file-to-directory transitions such as
deleting `node` and adding `node/child`. Ordinary preview/apply/dry-run exit 2;
`apply --check-only` returns `INVALID_PATCH`, and saved-plan JSON checks return
`INVALID_PLAN`. Siblings sharing a directory and prefix-only names such as
`node` and `node2/child` remain valid. `parse` checks grammar only.
The explicitly supplied root is resolved once; it may itself be a symlink.
New files use mode 0644; updates/moves retain permissions. ACLs, xattrs, ownership,
hard-link identity and timestamps are not preserved.

Validation computes **all** results before writing. Application stages new bytes
and backups in a private `.v4a-*` directory within root, then replaces files. An
ordinary commit I/O error restores touched files/permissions and removes newly
created directories. Tests inject a failure after earlier file writes. If a
second I/O failure prevents rollback, the tool explicitly reports
`ROLLBACK INCOMPLETE` and retains backups with a `recovery.json` path mapping.
A cleanup failure after a successful commit is a warning, not a false application
failure. Dry-run does not create this directory or invoke any workflow.

Update/move results must fit the same **100,000 logical newline endings** as
sources: LF and bare CR each count once, and CRLF pairs count once. Equality is
accepted. The check uses the fully assembled result, including all hunks,
embedded CR payloads and the existing final-newline restoration. BOM and Unicode
line separators do not add to this count. An append to 100,000 LF-terminated
lines rejects with `source: result exceeds 100,000-line limit`; moves name the
destination. Planning, apply/check-only/dry-run, preview and saved-plan
regeneration reject before commit, staging or preview output creation. The CLI
exits **2**; `apply --check-only` reports `INVALID_PATCH`, while saved-plan JSON
checks report `INVALID_PLAN`. Matching and newline reconstruction are unchanged.

Ordinary V4A planning snapshots have independent **8 MiB per-file** and
**64 MiB total source-byte** allowances. Every existing touched file counts,
including existing add targets and move destinations; absent paths and empty
files consume zero bytes. Stat-known excess rejects before opening that file.
Each actual read requests at most the smaller of the per-file and remaining
aggregate allowance **plus one detection byte**, so post-stat growth cannot
cause an unbounded read. Actual raw bytes (including BOM, UTF-8 and CRLF) count;
equality is allowed, and an empty file at zero remaining allowance reads at most
one byte. Overflow rejects before content planning or any later snapshot read.
Path order, snapshot bytes/modes and per-file-before-aggregate error precedence
are retained. Python raises `PatchError` with `PATH: file exceeds 8 MiB limit` or
`total source data exceeds 64 MiB limit`. The CLI exits **2** without output or
staging; `apply --check-only` instead emits `INVALID_PATCH` JSON. These source
allowances are separate from result and serialized-output limits: eight 8 MiB
deletions can pass check-only/dry-run but exceed the unified preview budget.
Saved-plan regeneration uses the same planner; its JSON resource errors remain
`INVALID_PLAN`/exit **2**. The earlier saved hash-preflight and aggregate drift
checks retain their existing failure categories and exit **1**.

Saved `load_plan` hash-preflight independently enforces the same per-file and
remaining aggregate read bounds, including stat-known refusal before open and
one detection byte for growth after stat. It accounts actual raw bytes, accepts
empty files at zero remaining allowance, and stops at the first failed
precondition in **JSON object order**. A resource refusal raises `ApplyError`:
`tree changed since planning: PATH: file exceeds 8 MiB limit` or
`tree changed since planning: PATH: total source data exceeds 64 MiB limit`.
Ordinary `apply-plan` and `--dry-run` exit **1** before regeneration or staging.
The aggregate check routes keep patch order, `RESOURCE_LIMIT` diagnostics with
unknown actual fingerprints, and continue to later paths. Matching preconditions
still require planner regeneration, second fingerprint checks and saved-diff
verification; source equality does not bypass their independent limits.

Commit checks each touched path against its in-memory snapshot **before staging**;
dry-run performs this same check and returns without staging. A normal commit
checks again after staging backups/new bytes and before its first target write.
Known presence, byte-length or permission drift rejects before opening source
bytes. Otherwise each read requests only the expected snapshot length **plus one
detection byte**, including a one-byte request for an expected empty file.
Exact bytes and permissions must still match; absent snapshots require absence
and are never read. This bounds growth after stat without changing accepted
BOM/UTF-8/CRLF bytes, permissions or snapshot order.

Initial drift raises `ApplyError` with
`tree changed since planning: PATH: original bytes, permissions or absence no longer match`;
ordinary/saved application and dry-run exit **1**. Drift found by the final
check adds `commit failed; changes rolled back:`, removes staging and preserves
the concurrent edit without applying planned target changes. Existing rollback,
recovery-data and cleanup-warning behavior is retained. These rechecks are
snapshots, not locks, and do not eliminate races or bound total process memory.

This is rollback atomicity, **not** simultaneous multi-file visibility, crash
recovery, or a sandbox against concurrent hostile filesystem changes. Exclude
other writers for the duration of plan/commit; the stale-plan check cannot
eliminate races. Process termination, disk failure, and failed rollback require
manual recovery. Empty directories left by successful deletions are retained.

Explicit first-version limits:

- Import/export output budgets are checked after source reads and complete
  serialization; this is not an incremental memory bound. Pure-rename import
  source reads have their own allowance-plus-one bound described above.
- UTF-8 text only; no binary/NUL data, three-way merge, combined diffs, copies,
  submodules, file-type/mode changes, or new executable files.
- Unified input uses Git `a/` / `b/` prefixes (or `/dev/null`), without timestamps
  or mail preambles. A root insertion with no old context must be regenerated
  with `git diff -U3`; zero-context replacements/deletions with old lines work.
- Unified input with missing-final-newline markers, BOM changes, CRLF added
  files, or mixed/newline-changing hunks is rejected rather than losing bytes.
  Export handles missing final newlines and mixed line endings with real Git.
- Pure rename import needs `--root`; unterminated sources and empty BOM-only or
  executable sources cannot be represented as a valid nonempty V4A update.
- No environment routing, Markdown fences or shell-command wrappers. Outer
  blank lines and the three upstream heredoc openers `<<EOF`, `<<'EOF'`,
  `<<"EOF"` are accepted as inert text; the closer ends with `EOF`, matching
  upstream leniency. No shell interpolation or execution occurs.
- At most 8 MiB / 100,000 lines per input/source/result, 1,000 file operations and
  64 MiB total source/result bytes. A search exceeding 10 million potential
  line comparisons per tier is rejected. Failure similarity is an approximate,
  bounded search of at most 2,000 windows, 80 lines / 8 KiB each.
- Planner snapshots and saved hash-preflight each bound accepted source bytes
  plus one overflow-detection byte in separate passes. This does not bound total
  process memory or isolate concurrent writers; checks remain snapshots, not locks.

## Development

From the repository root:

```sh
uv run --extra test pytest tests/patch
```

Tests use synthetic text fixtures (in `tests/patch/fixtures/`), temporary
directories and local Git repositories; no network or credentials are needed.
Git is required for the round-trip tests. They cover parser goldens, all
matching tiers, EOF preference/fallback, BOM and line endings,
ambiguous/context-drift diagnostics, paths/symlinks, validation and injected
I/O atomicity failures, CLI exit codes, bidirectional byte-for-byte comparisons
with real `git apply`, saved-plan creation/checking/application, strict
unified-location checks, path-set and Git-metadata validation, and the source,
result and serialized-output budgets described above (one below, equal to and
one above each boundary).

## Provenance and license

This is an independent implementation, not an official OpenAI package or a
wrapper around Codex or Git. Grammar and matching semantics were checked
against the upstream Codex apply-patch implementation
(`codex-rs/apply-patch/src/` in <https://github.com/openai/codex> at revision
`54b90f98a5724b688bf78793de5c21aca29ecec2`); intentional divergences, such as
the `*** End of File` fallback above, are documented in this file.

agentwrench is licensed under Apache-2.0; see [LICENSE](../LICENSE),
[NOTICE](../NOTICE) and [THIRD_PARTY.md](../THIRD_PARTY.md) for licenses and
attribution.
