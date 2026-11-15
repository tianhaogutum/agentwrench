# Three-way JSON preview contract (schema 2)

Use `agentwrench mcp-config convert --from FROM --to TO INPUT --base BASE --merge -o TARGET
--dry-run --json`. BASE and INPUT are complete source snapshots; TARGET is an
existing native destination. `--json` requires `--base` and `--dry-run` and cannot
write configs. Existing three-way flag restrictions still apply.

After argument parsing, stdout contains exactly one JSON object, including on
runtime errors, and stderr is empty. Argument-parser failures (unknown options,
missing positional arguments, invalid format choices) retain text usage on stderr
and empty stdout, with exit 2. `report --json` keeps its existing contract.

All objects have exactly these fields:

```json
{
  "schemaVersion": 2,
  "operation": "three-way-merge",
  "status": "preview",
  "changes": [{"path": ["worker", "command"], "kind": "change"}],
  "conflicts": [],
  "servers": {"added": 0, "removed": 0, "total": 2},
  "findings": {"source": [], "base": []},
  "error": null
}
```

- `status: "preview"`, exit 0: the merged config passed native validation.
  `changes` compares TARGET to the resolved result, not BASE to INPUT, so equal
  edits, unchanged source fields and target-only edits produce no changes.
  `servers` counts target-to-result additions/removals and total result servers.
  A no-op has empty changes and zero added/removed servers.
- Each change has `kind: "add"`, `"change"` or `"delete"`. Paths start at a
  literal server name and continue with native destination keys. Dots, slashes,
  empty keys, Unicode and controls in field keys remain individual JSON strings;
  they are never split or rendered as dotted paths. Arrays are atomic, so paths
  have no array indexes. Server names retain the existing 1–256 printable-character
  restriction. Added/deleted maps (including whole servers) have one
  entry at that map's path; maps present on both sides recurse. Null is a value,
  distinct from absence; booleans are distinct from numbers.
- `status: "conflict"`, exit 2: all conflicts are reported as, for example,
  `{"path":["worker","command"],"incoming":"change","target":"change"}`.
  Both classifications compare that side to BASE at the conflict boundary.
  Delete/edit conflicts use `delete`/`change` (or the reverse); concurrent
  different additions use `add`/`add`. No partial plan is offered: `changes` is
  empty and `servers` is null. `error` is a masked explanation.
- `status: "error"`: invalid flag combinations, I/O, credential refusal, malformed
  snapshots or invalid merged output exit 2. Unsupported conversion in either
  snapshot exits 3. `changes` and `conflicts` are empty and `servers` is null;
  `error` is a masked explanation. Projection errors retain available findings;
  other failures have empty findings. No omitted server is interpreted as a
  deletion on projection failure.
- `findings.source` and `findings.base` contain scoped source-native findings as
  specified below, including fidelity losses and warnings. Lossy projection can
  still return a preview; it does not promise lossless sync.
- Changes and conflicts are sorted lexicographically by original path segments,
  independent of snapshot map order. No field values or merged config are emitted.
  Known credential signatures in names/keys/messages are replaced by `[REDACTED]`,
  so such diagnostic paths may no longer uniquely identify original keys.

All outcomes leave inputs, target, backups and directories untouched. A preview
validates merge semantics, not write feasibility (for example, an existing backup
may still block a later changing write). There is no saved-plan binding or base
advancement; a subsequent ordinary conversion recomputes the merge. Human text
preview/write behavior is unchanged. JSON output is deliberately preview-only.


## Fidelity coordinates and version compatibility

Every post-parse outcome now emits **schemaVersion 2**, including errors and empty
results. Schema-1 consumers must update; no version selector is provided. The
other top-level keys, changes/conflicts, counts, ordering, status and exit codes
are unchanged. `report --json` retains its four-string findings; human conversion,
preview and base-fidelity labels/streams are unchanged. `check --json` already uses
schema 2 with the same six-key finding shape.

Both `findings.source` and `findings.base` describe their respective complete
**native source snapshot**, before projection to the destination. Every finding
has exactly these keys:

```json
{"scope":"server","server":"worker.tools","path":["vendor.dot","工具",0,""],"field":"vendor.dot.工具[0].","kind":"loss","message":"native passthrough has no verified equivalent; omitted"}
```

- `scope` is `"server"` or `"document"`. For server scope, `server` is the literal
  native server name and `path` is relative to its native server object, excluding
  container keys and Continue's list index/name wrapper. For document scope,
  `server` is null and `path` is relative to the document root. A real server named
  `(document)` has server scope; scope is never inferred from a display label.
- `path` is a nonempty array of literal string object keys and nonnegative integer
  array indices. Dots, empty strings, bracket-like text, slashes, Unicode and
  control characters remain literal. Integer `0` differs from string `"0"`.
  Coordinates come from structured native traversal or explicit native mappings,
  never from splitting `field`. Continue header warnings use, for example,
  `["requestOptions","headers","X.Key"]`, while a corresponding **change** path
  in Codex is `["s","http_headers","X.Key"]`. Finding paths locate diagnostics,
  not executable edits. An inferred transport error can refer to `type` even if
  the optional key was absent.
- `field` is the legacy display-only dotted/bracket string; it remains ambiguous.
  `kind` is `loss`, `error`, `interpolation` or `secret`. `message` is a value-free
  explanation. No field values, native config or filenames are added.

For example lost `vendor: {"a.b": 1, "a": {"b": 2}}` yields two findings with
`field: "vendor.a.b"` but distinct paths `["vendor","a.b"]` and
`["vendor","a","b"]`. A document setting named `"a.b"` has document scope,
null server and path `["a.b"]`, distinct from a field on a server `(document)`.

Each list retains projection finding order, followed by cross-format document
losses and document-secret annotations in native traversal order. Base document
annotations are newly included in JSON mode; document settings are still never
merged. Existing granularity is retained: passthrough maps traverse leaves
(including nested arrays), top-level passthrough arrays/empty containers and
document losses use the field boundary; interpolation/secret warnings traverse
leaves. Same-format document settings have no loss finding, but base document
secret annotations remain available.

## Negative cases and disclosure boundaries

Conflicts retain both scoped finding lists, but never safe partial changes or
counts. Unsupported projection in **either** snapshot exits 3, retaining available
findings with native error coordinates and empty changes/conflicts, null servers
and a masked error. Unsupported servers are never treated as deletions. Existing
validation order is retained: unsupported projections abort before target parsing.

Reached invalid flag/read/parse/native-schema failures in source, base or target,
source credential refusal and invalid merged output exit 2 with status `error`,
empty source/base findings, empty changes/conflicts and null servers. Precollected
findings are discarded in these cases. Argument-parser errors remain text usage.
Source credential refusal is unchanged. Base credentials remain warnings rather
than introducing a new refusal policy; only masked locations/explanations appear.
This is not a target credential scan or an audit of raw base comments.

Known credential signatures are masked independently in every string path
segment, server, legacy label and message. Integer indices and document null stay
typed; JSON escapes controls. Masking is heuristic and may collapse coordinates;
arbitrary names/keys remain visible. All JSON outcomes write nothing, including
when a backup already exists. Preview validation still does not guarantee later
write feasibility or bind a future write to these snapshots.
