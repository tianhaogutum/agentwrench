# agentwrench mcp-config

`agentwrench mcp-config` converts MCP server definitions between **Claude Code,
Codex, Cline, Continue and VS Code editor-managed configs**, without launching a
server. Review field loss before migration, overlay selected servers without
deleting unrelated settings, merge updates against a previous source snapshot,
detect configuration drift in CI, and relocate explicitly selected project paths
with JSON provenance. It has four subcommands: `convert`, `report`, `check` and
`relocate`.

## Install

```sh
pip install agentwrench            # or: pipx install agentwrench
uvx agentwrench mcp-config --help  # run without installing
```

Requires Python >= 3.11. The runtime dependencies used by this subcommand
(`tomlkit`, `ruamel.yaml`) are installed automatically. `agentwrench mcp-config ...`
and `python -m agentwrench mcp-config ...` provide the same interface.

## Convert and review

Use explicit paths; the tool never discovers or opens real client settings.
The example configs in `examples/mcp-config/` are synthetic and safe to try:

```sh
agentwrench mcp-config report --from cline --to codex examples/mcp-config/cline/cline_mcp_settings.json
agentwrench mcp-config report --from vscode --to claude examples/mcp-config/vscode/mcp.json --json
agentwrench mcp-config report --from cline --to codex examples/mcp-config/cline/cline_mcp_settings.json --json-v1
agentwrench mcp-config convert --from claude --to codex examples/mcp-config/claude/.mcp.json
agentwrench mcp-config convert --from claude --to codex examples/mcp-config/claude/.mcp.json -o generated.toml
```

The first server in the conversion is:

```toml
[mcp_servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]

[mcp_servers.github.env]
GITHUB_TOKEN = "${GITHUB_TOKEN}"
```

Conversion writes the native config to stdout (or `-o`) and the fidelity report to
stderr. The report prints **server, field path, kind, explanation**, with no field
values. JSON reports have a stable `{"findings": [...]}` envelope and the same four
string fields. `loss` means a setting has no verified equivalent; `interpolation`
means the expression remains literal and needs client-specific review; `secret`
means a suspected credential; `error` means that server cannot be converted.
Unmappable fields are never hidden in fabricated client metadata.

For scoped source-native coordinates, opt into `report --json-v1` instead of
`--json`. It emits one value-free version-1 envelope with `operation: "report"`,
`status`, `findings` and `error` on stdout, with empty stderr after parsing.
Findings carry `scope`, `server`, literal `path` segments, and the existing
`field`, `kind`, `message` strings. Dotted keys stay literal, array indices stay
integers, and document scope has `server: null`, distinct from a real server
named `(document)`. Labels are never split to infer coordinates.

`reported` exits 0 even with losses or masked secret/interpolation warnings;
`unsupported` exits 3 with source findings and an explanation. Invalid input or
processing errors exit 2 with empty findings and a masked error. This mode reads
only the explicit source and writes nothing. The two JSON options are mutually
exclusive; parser failures retain text usage on stderr. Legacy `--json` and human
reports keep their exact envelopes, labels and exits. See the full
[version-1 contract and negative cases](mcp-config/JSON_REPORT.md).

For a CI gate, explicitly select `--policy-json-v1` and repeat `--fail-on` for
each kind that should fail:

```sh
agentwrench mcp-config report --from claude --to codex examples/mcp-config/claude/.mcp.json \
  --policy-json-v1 --fail-on loss --fail-on secret --fail-on interpolation
```

This emits a separate version-1 `operation: "report-policy"` envelope containing
all scoped findings and `policy: {failOn, matchedKinds, decision}`. Supported
projections exit 1 with `status: "rejected"` when a selected kind occurs, otherwise
0 with `status: "accepted"`. Nonselected warnings remain informational. Without
`--fail-on`, the selection is empty; duplicate kinds collapse in fixed order
`loss`, `secret`, `interpolation`. Selection covers both server and document
findings. Unsupported projection takes precedence (exit 3), retaining findings
and the evaluated policy. Invalid input takes precedence over both (exit 2),
with empty findings and policy `decision: "not-evaluated"`, `matchedKinds: null`.
All six JSON modes are mutually exclusive; `--fail-on` requires policy v1/v2.
Other report modes keep their bytes and exits.

To gate selected diagnostic scopes, opt into policy version 2:

```sh
agentwrench mcp-config report --from claude --to codex examples/mcp-config/claude/.mcp.json \
  --policy-json-v2 --fail-on loss --fail-on secret --fail-scope server
```

V2 adds `policy.scopes`; its `schemaVersion` is 2 with the same operation and
exit precedence. A kind matches only within a selected scope, while **all findings
remain visible**. `--fail-scope` accepts exactly `server` or `document`; repeat it
to include both. Omission selects both, duplicates collapse in `server`, `document`
order, and empty/unknown selectors are usage errors. A real server named
`(document)` has server scope, distinct from document metadata. `--fail-scope`
requires v2; v1 stays unfiltered with its exact schema. Empty `--fail-on`
selection still selects nothing. Invalid input retains normalized selections but
has no findings or evaluated-policy claims. See the complete
[versioned policy contracts and negative cases](mcp-config/REPORT_POLICY.md).

To select independent kind/scope pairs, opt into policy version 3:

```sh
agentwrench mcp-config report --from claude --to codex examples/mcp-config/claude/.mcp.json \
  --policy-json-v3 --fail-rule loss:server --fail-rule secret:document
```

This rejects server losses or document secrets. Server secrets and document
losses alone are accepted; rules never form a cross product. V3 emits
`policy: {rules, matchedRules, decision}`, where each rule is `{kind, scope}`.
Kinds are `loss`, `secret`, `interpolation`; scopes are `server`, `document`.
Duplicate pairs collapse in kind-then-scope order; omitted rules select nothing.
Empty, unknown or missing rules are usage errors. `--fail-rule` requires v3;
`--fail-on`/`--fail-scope` cannot be combined with v3. Every diagnostic remains
visible, including nonselected kinds/scopes and real `(document)` servers.
Invalid inputs retain normalized rules but use `matchedRules: null` and
`decision: "not-evaluated"` with empty findings. Exit 2/3/1/0 precedence,
heuristic masking and read-only behavior stay the same. V1/v2 and default report
contracts remain unchanged; see [the full contract](mcp-config/REPORT_POLICY.md).

For a reusable CI policy, save this strict JSON as `ci-policy.json`:

```json
{"schemaVersion":1,"rules":[{"kind":"loss","scope":"server"},{"kind":"secret","scope":"document"}]}
```

```sh
agentwrench mcp-config report --from claude --to codex examples/mcp-config/claude/.mcp.json \
  --policy-json-v3 --policy-file ci-policy.json
```

The artifact version is **1**, and its result uses the existing **v3** envelope.
`--policy-file` with artifact v1 requires v3 and refuses `--fail-rule`, `--fail-on`, and
`--fail-scope` overrides. Only the explicitly supplied regular UTF-8 file is read:
maximum 64 KiB and 128 rule entries before deduplication, exact fields/types,
no duplicate JSON keys, comments, non-finite constants, unknown fields or versions.
Empty rules select nothing; duplicate pairs collapse in the same fixed order.
The entire artifact validates **before native INPUT is read**. Invalid artifacts
exit 2 with empty rules/findings and no evaluated-policy claims. A valid policy
retains normalized effective rules on native input failure. All diagnostic,
masking, no-write and exit precedence contracts match inline v3; neither file
paths nor raw policy content are added to results. No discovery or includes.
See [the artifact contract and failure cases](mcp-config/REPORT_POLICY.md#reusable-paired-rule-artifacts-version-1).

To gate one exact native location, save a **version-2 artifact**:

```json
{"schemaVersion":2,"rules":[{"kind":"loss","scope":"server","server":"worker","path":["vendor","a.b"]}]}
```

```sh
agentwrench mcp-config report --from claude --to codex path/to/synthetic-input.json \
  --policy-json-v4 --policy-file ci-policy-v2.json
```

V4 requires the file, refuses inline selectors, and emits result `schemaVersion:
4` with normalized `rules`, `matchedRules`, and `decision`. Every explicit
selector must match the same original finding. Server/path strings are literal;
`["vendor","a.b"]` differs from `["vendor","a","b"]`, and indices are integers.
Omitted path selects any coordinate; `[]` selects only the root coordinate.
Document rules forbid a server selector. The full findings list remains visible.
Limits: 64 KiB, 128 raw rules, 64 path segments, indices 0..2147483647. Duplicates
normalize before masking; token-shaped names/keys are masked in results and may
look identical despite distinct original matches. No values, writes, expansion,
execution or network. V3/artifact v1 and all older report contracts stay intact.
See [exact selector semantics and negative cases](mcp-config/REPORT_POLICY.md#exact-native-selectors-result-version-4-artifact-version-2).

For example, Cline `disabled: true` becomes Codex `enabled = false`, HTTP transport
spellings and header nesting are mapped, while Cline `autoApprove`, `metadata`,
`oauth` and request `timeout` produce explicit loss entries. Codex tool allow/deny
lists do not grant approval. Cline's request timeout is seconds but covers more
than Codex's tool-call timeout, so these limits are not falsely equated.

`${GITHUB_TOKEN}` is intentionally **not** expanded or rewritten. Its appearance
in a Codex output does not mean Codex supports Claude's interpolation semantics.
Use the report and [field contract](mcp-config/FORMATS.md) to review differences.

## Merge safely

```sh
# generated.toml from the previous section already defines both servers, so
# --merge alone refuses with "server name collision" (exit 2).
agentwrench mcp-config convert --from claude --to codex examples/mcp-config/claude/.mcp.json \
  -o generated.toml --merge --overwrite --dry-run
agentwrench mcp-config convert --from claude --to codex examples/mcp-config/claude/.mcp.json \
  -o generated.toml --merge --overwrite
```

`--merge` without `--base` adds new servers. Existing names require `--overwrite`; it overlays
**only supplied fields**, recursively for maps. Existing native unknown keys,
unspecified settings, other servers and document-level settings stay in place.
Arrays are replaced as a whole. This two-way overlay makes no deletions or client scope changes.
Changing an existing server between command and URL transports can leave
conflicting fields; output validation refuses the update instead of guessing
which settings to remove. Remove the old definition deliberately first.

### Three-way updates

Supply the previous **complete source snapshot** with `--base` to distinguish
incoming edits from local target edits. The base and input both use `--from`;
both are projected to `--to` before comparison. The existing output is the local
target. A missing server/field in the input means deletion in this mode.

Try the synthetic example in a temporary directory:

```sh
example_dir=$(mktemp -d)
cp examples/mcp-config/three_way/target.toml "$example_dir/config.toml"
agentwrench mcp-config convert --from claude --to codex examples/mcp-config/three_way/incoming.json \
  --base examples/mcp-config/three_way/base.json --merge -o "$example_dir/config.toml" --dry-run
# CHANGE worker.command: target would change (values masked)
# PLAN: 0 servers added; 0 removed; 2 total after merge.
agentwrench mcp-config convert --from claude --to codex examples/mcp-config/three_way/incoming.json \
  --base examples/mcp-config/three_way/base.json --merge -o "$example_dir/config.toml"
diff -u examples/mcp-config/three_way/expected.toml "$example_dir/config.toml"
```

Here the base command is `old`, the incoming command is `new`, and the target
still has `old`. The result uses `new`, retains the target's `args = ["local"]`,
`env.LOCAL`, unknown `extension` map, `local` server and document-level model.
`${REGION}` stays literal. `expected.toml` contains the complete output.

Maps merge field by field; arrays are indivisible values. Unchanged incoming
fields retain local target edits. Equal edits on both sides succeed. A source
deletion applies only if the target is unchanged there; deletion versus a local
edit conflicts. Different concurrent additions with the same server/field name
also conflict. Target-only servers and unknown fields are preserved. Client
scopes and document-level source settings are never synchronized.

For a conflict example, copy `examples/mcp-config/three_way/conflicting.toml` as the
output instead. Both commands then exit 2 with `CONFLICT worker.command`, print
no values, and create neither a changed output nor a backup. All conflicts are
reported before writing. Resolve the snapshots deliberately and preview again;
`--base` rejects `--overwrite` and `--redact-secrets`. It requires an existing
output. If either snapshot has an unsupported server conversion, the entire
three-way merge exits 3 without writing; it never treats that omission as deletion.
Cross-format losses remain reported and cannot be synchronized.

The preview lists changed target paths and server addition/removal counts without
values. The write recomputes and validates the result, then uses the same atomic
replacement and exclusive backup described below. It does not bind a later write
to a saved preview. Base and input files are never updated automatically; retain
an accurate base snapshot for the next round. An unchanged repeat writes nothing.
Three-way JSON/JSONC rewrites changed server objects, so comments *inside those
objects* can be lost; untouched servers, surrounding comments and root settings
stay intact. TOML and block YAML retain native trivia on surviving fields.

### JSON previews for CI

Add `--json` to a three-way **dry-run** to emit one schema-version-2 object on stdout,
with no text diagnostics on stderr:

```sh
agentwrench mcp-config convert --from claude --to codex examples/mcp-config/three_way/incoming.json \
  --base examples/mcp-config/three_way/base.json --merge -o "$example_dir/config.toml" \
  --dry-run --json
```

Against the original `target.toml`, `status` is `"preview"` and `changes` is
`[{"path":["worker","command"],"kind":"change"}]`. Each path is an array of
literal native destination keys, starting with the server name; dots are never
split. Changes classify target-to-result edits as `add`, `change` or `delete`.
Arrays and newly added/deleted maps are atomic. The record includes server counts
and separate source/base fidelity findings, without field values. Each finding has
`scope`, `server`, a literal source-native `path`, and legacy `field`, `kind` and
`message` strings. Document findings have `server: null`; a real server named
`(document)` has server scope. Both snapshots include document annotations.
Finding paths preserve native string keys and integer array indices; Continue
header warnings use `["requestOptions","headers","X.Key"]`, even when destination
change paths use Codex `http_headers`. Display labels are never parsed as paths.
Schema-1 preview consumers must update; no version selector is provided.

Conflicts exit 2 with `status: "conflict"` and entries such as
`{"path":["worker","command"],"incoming":"change","target":"change"}`.
The two edit kinds are relative to the base. No partial plan is returned.
Runtime failures use `status: "error"`, with exit 2 (invalid input) or 3
(unsupported conversion). Argument-parser failures retain text usage on stderr.
`--json` requires `--base --dry-run`; writes and two-way conversions cannot use it.
Nothing is written in any JSON outcome. See the complete
[schema and negative-case contract](mcp-config/JSON_PREVIEW.md), including sorting,
masked names, no-ops and the limits of write preflight. Conflicts retain scoped
findings without partial plans; unsupported snapshots exit 3 with findings and no
plan; invalid inputs exit 2 with empty findings. Base credential annotations remain
masked warnings; source credential refusal is unchanged. `report --json` and human
output retain their legacy labels.

JSON/JSONC uses value-span edits; TOML uses tomlkit; YAML edits only touched block
sequence items using ruamel.yaml, leaving other servers and root content intact.
Touched YAML entries can be reformatted; native comments are retained. Same-format
conversion without edits returns original bytes. Cross-format conversion creates
new syntax and cannot retain comments or unrelated source document settings.
Unknown server fields survive A → IR → A; A → B → A retains representable fields
and reports the rest. Keep your original file as the migration source.

Existing output files require `--merge`; `--overwrite` alone is invalid. Every
actual replacement creates an exclusive `OUTPUT.bak` containing the original
bytes, then atomically replaces the file. An existing backup blocks another
change; archive it deliberately. An unchanged merge creates no backup. New output
and backups have mode `0600`; replacement files also use `0600`. Writes use a
same-directory temporary file, fsync and a stale-content check. Coordinate writers:
this is not a lock against an editor racing between the final check and rename.
Parent directories must already exist. Output paths and parents cannot be symlinks;
inputs must be regular UTF-8 files, not links, devices or pipes. Server names are
never interpreted as paths; dotted/spaced/Unicode TOML keys are quoted correctly.

`--dry-run` validates parsing, mapping, merge collisions and resulting config,
prints planned counts, and writes nothing: no temp file, backup, network operation
or external workflow step.

## Relocate project paths

Choose a project's native config explicitly, supply its old and new roots, and
select each path-valued field. This synthetic example changes only `worker.cwd`
and argument index 1:

```sh
agentwrench mcp-config relocate --format codex examples/mcp-config/relocation/source.toml \
  --from-root /old/project --to-root /new/project \
  --path '["worker","cwd"]' --path '["worker","args",1]' --dry-run

relocation_dir=$(mktemp -d)
agentwrench mcp-config relocate --format codex examples/mcp-config/relocation/source.toml \
  --from-root /old/project --to-root /new/project \
  --path '["worker","cwd"]' --path '["worker","args",1]' \
  -o "$relocation_dir/config.toml" 2> "$relocation_dir/provenance.json"
diff -u examples/mcp-config/relocation/expected.toml "$relocation_dir/config.toml"
```

`/old/project/work` becomes `/new/project/work`, and `/old/project/data` becomes
`/new/project/data`. The complete `relocation/expected.toml` retains the absolute command,
an unselected absolute argument, URL, env values, `${ROOT}` expression, native
extension, comments and document settings unchanged. Each `--path` is a JSON
array of a literal server name and field segments; dots in names are not split.
Argument indexes are zero-based. Selections are an explicit assertion that the
whole argument is a path: the tool never infers argument semantics from a prefix.

`args` selections work in all five formats. `cwd` is available only in Codex,
Continue and VS Code, where the existing native field contract recognizes it.
Commands, URLs, env values and unknown fields cannot be selected. Nothing is
expanded, executed, fetched or moved. Only selected fields must be project paths;
unselected literals can point anywhere.

Both roots and selected values must be normalized absolute POSIX paths. The
source root must contain each selected path by **path components**, so
`/old/project-other/data` does not match `/old/project`. The root itself can be
selected and maps exactly to the target root. Spaces and Unicode are supported.
Relative paths, `/` as a root, `.`/`..` components, repeated/trailing slashes,
Windows/backslash paths, substitutions, glob syntax, colon-separated path lists,
flag assignments and duplicate/missing selectors are refused with exit 2. The
same roots are also refused. For example, selecting `/old/project/../outside`
aborts the **entire** operation, even if another selection is valid. Errors print
no values, and no output or backup is created. All selections and the complete
resulting config are validated before writing.

Dry-run prints JSON provenance to stdout and writes nothing. Without dry-run,
the native config goes to stdout or `-o`, and provenance goes to stderr. The
versioned JSON record contains `scope: "project"`, native format, both supplied
roots, and each selection's field segments, `basis: "explicit-selection"`,
relative suffix, original value and relocated value. **Provenance deliberately
contains path values**; store it accordingly. Detected literal credentials in
the input or credential signatures in supplied roots block relocation. There is
no relocation redaction mode.

`-o` creates a new file and refuses any existing output. To replace the input,
use `--in-place` instead: it creates an exclusive `.bak` and uses the mode-0600
atomic-write safeguards above. Dry-run also checks the intended output path and
backup guard. Output parents must exist. Native same-format editing retains
unrelated content; Continue edits require the existing block-style restriction.
Cross-format conversion is a separate subsequent `convert` operation.

Containment is lexical: roots and referenced files need not exist, and symlinks
inside those roots are not resolved. The operation does not prove runtime file
access or discover a client's active project/global scope. `scope: "project"`
records the user-supplied project boundary; no client scope metadata is changed.
Explicit `--path` writes recompute selections without binding to an earlier preview.

### Reuse reviewed selections

Generate a source-bound selection manifest for review, then reuse it without
retyping the roots or argument indexes:

```sh
agentwrench mcp-config relocate --format codex examples/mcp-config/relocation/source.toml \
  --from-root /old/project --to-root /new/project \
  --path '["worker","cwd"]' --path '["worker","args",1]' --plan > plan.json
agentwrench mcp-config relocate examples/mcp-config/relocation/source.toml --manifest plan.json --dry-run
agentwrench mcp-config relocate examples/mcp-config/relocation/source.toml --manifest plan.json \
  -o "$relocation_dir/reviewed.toml"
```

`--plan` validates the entire relocation and prints a versioned JSON manifest
containing the native format, roots, ordered selectors and SHA-256 of the exact
source bytes. It writes no files; the example's shell redirection saves it.
It rejects `--dry-run`, `-o` and `--in-place`. Manifest application accepts those
output modes, but rejects CLI format/root/path overrides. Existing explicit
relocation and provenance output keep their schemas.

If any source byte changes, applying the manifest exits 2 with
`source digest mismatch` before config/provenance output, file creation or backup.
An inserted argument cannot silently redirect a reviewed index. Comment-only and
line-ending changes also require a new review; identical bytes at another input
path are accepted. Generate and review a new manifest after intentional edits.
Manifest JSON uses strict version/type/key validation and the input size/depth
limits. Matching digests still require all existing path, credential, native
rendering and output checks. The manifest is editable and **not authenticated**;
review and protect it too. It binds a loaded snapshot and does not lock files
against concurrent editors. See the complete
[schema and negative-case contract](mcp-config/RELOCATION_MANIFEST.md).

### Archive an application receipt

Add `--receipt` when applying a reviewed manifest to tie the provenance to the
exact artifact, input and rendered output bytes:

```sh
agentwrench mcp-config relocate examples/mcp-config/relocation/source.toml --manifest plan.json \
  --receipt --dry-run > planned-receipt.json
agentwrench mcp-config relocate examples/mcp-config/relocation/source.toml --manifest plan.json \
  --receipt -o "$relocation_dir/receipted.toml" 2> applied-receipt.json
```

The version-1 `relocation-application` record includes `manifestSha256`,
`sourceSha256`, `outputSha256` and the complete existing `provenance`. All hashes
cover **exact UTF-8 bytes**: manifest whitespace, key order and CRLF change its
identity, even when the resulting relocation is identical. No output newline is
added. Default provenance remains unchanged when `--receipt` is absent.

Dry-run emits `status: "planned"` on stdout and writes nothing. Application emits
`status: "applied"` on stderr only after writing the new file, in-place file or
native stdout (and flushing it) succeeds. Post-parse failures exit 2 with
`status: "error"`, null hashes/provenance and a value-masked message, on stdout for
dry-run or stderr for apply. Parser usage errors remain text stderr.
`--receipt` requires `--manifest` and retains all stale-source and exclusive
output/backup guards. Receipts deliberately contain path values on success and
are not signed. A failed stdout stream can contain a prefix; an error after
atomic publication can leave a complete file. See the exact
[schema, streams and failure boundaries](mcp-config/RELOCATION_RECEIPT.md).

## Drift checks

```sh
agentwrench mcp-config check --source examples/mcp-config/claude/.mcp.json --from claude \
  --target examples/mcp-config/codex/drifted.toml --to codex
# DRIFT github.args: source and target differ (values masked)
# exit 1
```

Checks compare the servers named by the source. Extra target servers and
non-server document settings are ignored. Within those servers, canonical fields
(including extra env/header keys, argument order and enablement) are compared.
Same-format unknown server settings are also compared. Missing args/env/headers
and enabled defaults are normalized. Unrepresentable source server fields count
as drift, so a lossy projection cannot be certified as fully in sync.
Interpolation warnings remain warnings: checks do not prove variable expansion,
server availability, authentication or client execution behavior.

### JSON drift records for CI

```sh
agentwrench mcp-config check --source examples/mcp-config/claude/.mcp.json --from claude \
  --target examples/mcp-config/codex/drifted.toml --to codex --json
```

`--json` emits one schema-version-2 object with `operation: "check"`, status
`"in-sync"` (exit 0), `"drift"` (exit 1) or `"error"` (exit 2), plus `drift`,
`findings` and a nullable masked `error`. Runtime errors use JSON stdout and empty
stderr; argument-parser failures retain text usage on stderr. No files are written.

Drift records have literal path segments, a target-to-source `add`, `change` or
`delete` kind, and a `basis`: canonical `normalized` fields or same-format unknown
`native` fields. For example:
`{"path":["github","args"],"kind":"change","basis":"normalized"}`.
Records sort by path and basis. Arrays are atomic and order-sensitive; null differs
from absence. Unrepresentable source settings appear in fidelity findings and cause
exit 1 even when comparable drift is empty; they are not speculative target edits.
Each fidelity finding has `scope`, `server`, `path`, `field`, `kind` and `message`.
Server paths are relative to the source native server object; document paths are
relative to the document root with `server: null`. Literal keys remain strings and
array indices are integers. For example, a loss at `["vendor", "a.b"]` differs from
`["vendor", "a", "b"]` even if both have the display label `vendor.a.b`. A real
server named `(document)` has server scope. Never parse the legacy `field` label.

This versions the check envelope from 1 to 2, including errors and empty results;
schema-1 consumers must update. Human output and `report --json` keep legacy
finding labels/schemas. Three-way JSON previews also use schema 2 with scoped
source/base coordinates. Extra target servers and document settings
remain ignored for drift. Values are never emitted, and known credential
signatures in names/keys are masked. See the
[exact schema and negative-case contract](mcp-config/JSON_CHECK.md) for path granularity,
inferred transport coordinates, normalization and version compatibility.

| Exit | Meaning |
|---|---|
| 0 | Successful conversion/report; or source servers in sync |
| 1 | `check`: drift or an unrepresentable source server setting; `report --policy-json-v1`/`--policy-json-v2`/`--policy-json-v3`/`--policy-json-v4`: selected finding rule present in a supported projection |
| 2 | Invalid/unsupported document structure, credential refusal, I/O error, merge collision, unsafe path or write failure |
| 3 | `convert`/`report`: at least one server unsupported; other valid servers still converted, except `--base` aborts the entire merge |

If all servers are unsupported, conversion emits no config and creates no output
file. Disabled servers targeting clients without mapped disablement are errors,
not silently enabled. SSE → Codex is unsupported. Non-HTTP endpoint schemes are
native-only. `check` treats these cases as drift (exit 1).

## Credentials and input boundaries

No environment variables are evaluated, commands executed, URLs fetched, OAuth
flows started, or real settings inferred. Parsing is bounded to 1 MiB, 64 nested
containers and finite JSON-compatible values. Duplicate keys error. YAML custom
tags, anchors, aliases, directives, multi-document streams and hub references are
outside this version. JSON comments/trailing commas are accepted only for VS Code.

Error diagnostics and drift output never print values. Relocation provenance
deliberately records selected paths and supplied roots, as described above.
Conversion refuses suspected literal credentials by default, including sensitive env/header/OAuth leaves,
known token prefixes, URL credentials and secret defaults in references. Use
client-supported environment references. To make an explicitly unusable preview:

```sh
agentwrench mcp-config convert --from cline --to codex path/to/synthetic-input.json --redact-secrets
```

`--redact-secrets` replaces detected credential leaves with `[REDACTED]`, emits
server-only source content, and warns that source document metadata/comments are
not copied. It never fabricates working credentials. Names/keys containing known
token signatures cannot be safely rewritten and error. Detection is heuristic,
not a comprehensive secret scanner: do not feed production credentials. Existing
merge-target settings and backup bytes are preserved, including any credentials
already in that file; these are never dumped into diagnostic output. The Python
library IR retains original values and must not be logged as a diagnostic.

## Limits and upstream relationship

This is independent file-format interoperability, not an upstream import command,
MCP runtime, scope synchronizer, OAuth manager or rules/skills converter. It does
not discover settings or promise that a client honors a given file location.

- Only explicit native Continue list entries are supported; nonempty flow-style
  lists/entries cannot be merged. Block lists and empty `[]` collections can.
  Remote Continue entries need an explicit type for cross-format conversion.
- VS Code means the requested editor-managed `servers`/`inputs` schema. The
  current documentation calls this Local compatibility, separate from newer
  Copilot `mcpServers` configs. See [FORMATS.md](mcp-config/FORMATS.md).
- Cline request timeout, Continue connection timeout, client tool/approval
  policies, OAuth state, helpers, env files and other native extras stay native;
  cross-format omissions are reported. No unsupported interpolation is rewritten.
- Native same-format identity covers accepted values. Non-JSON TOML dates and
  unusual YAML object types are rejected rather than silently normalized.

Field provenance, URLs and retrieval date are in [FORMATS.md](mcp-config/FORMATS.md).
No upstream client code is bundled. See [LICENSE](../LICENSE),
[NOTICE](../NOTICE) and [THIRD_PARTY.md](../THIRD_PARTY.md).

## Development

From the repository root:

```sh
uv run --extra test pytest tests/mcp_config
```

The suite covers all 25 format directions, realistic native fixtures and round-trip
fidelity, untouched content/comments, atomic backups, exit codes, unsafe input,
secret handling, classified JSON drift checks, three-way conflicts/deletions and
classified JSON previews, selected project relocation with provenance,
source-bound reusable manifests and exact-byte application receipts,
stale-index/byte-change and ambiguous-path refusal, and dry-run non-execution. It
uses temporary files only and never opens the user's client settings. Test
fixtures live in `tests/mcp_config/fixtures/`; the same files are copied to
`examples/mcp-config/` for the examples above.
