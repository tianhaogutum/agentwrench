# Scoped JSON fidelity report contract (version 1)

Use `agentwrench mcp-config report --from FROM --to TO INPUT --json-v1`. INPUT is an
explicit native UTF-8 regular file; formats are required. `--json-v1` is mutually
exclusive with legacy `--json`. No target file is read. No file, backup, native
config output, environment expansion, execution or network operation is produced.

After argument parsing, stdout is exactly one JSON object and stderr is empty:

```json
{
  "schemaVersion": 1,
  "operation": "report",
  "status": "reported",
  "findings": [],
  "error": null
}
```

All outcomes have exactly those five keys:

- `reported`, exit 0: the source is valid and every server can be projected.
  Findings can still include losses, interpolation and secret warnings; this
  status does not promise lossless conversion or runtime equivalence. Empty
  collections and same-format reports are valid. `error` is null.
- `unsupported`, exit 3: valid source contains at least one server that cannot
  be projected (e.g. transport, disablement or non-string VS Code env values).
  Findings retain source diagnostics for all servers and document metadata,
  including scoped `kind: "error"` entries. `error` is the fixed string
  `unsupported server conversion; no files written`. No partial config is emitted.
- `error`, exit 2: unreadable/non-regular/non-UTF-8 input, invalid native syntax,
  duplicate keys, size/depth bounds, invalid server schema or processing failure.
  `findings` is empty, even if other source servers are valid or unsupported;
  `error` is a nonempty value-masked explanation. All source servers are validated
  before projection, so invalid input takes precedence over unsupported projection.

Argument-parser failures (missing paths/formats, unknown options, invalid choices,
or `--json --json-v1`) retain text usage on stderr, empty stdout and exit 2.
`--help` retains text help. `--json-v1` is available only on `report`.

Each finding has exactly these six keys, using the shared native-coordinate
shape from schema-2 checks and previews:

```json
{"scope":"server","server":"(document)","path":["vendor.dot","rows",0,""],"field":"vendor.dot.rows[0].","kind":"loss","message":"native passthrough has no verified equivalent; omitted"}
```

`scope` is `server` or `document`. Server paths are relative to the native server
object; `server` is its literal name, including a real name `(document)`. Document
paths are relative to the document root and `server` is null. Server-container
keys and Continue's list index/name wrapper are excluded from server coordinates.
`path` is a nonempty array of literal string keys and nonnegative integer indices.
Dots, empty/Unicode/control/bracket keys and string `"0"` remain distinct from
nested keys and integer `0`. Paths come from structured native traversal and
explicit mappings, never from splitting the display-only `field` label. Different
paths may have the same label. Continue header warnings retain
`["requestOptions","headers","X.Key"]` regardless of destination spelling.

`kind` is `loss`, `interpolation`, `secret` or `error`; `message` is a value-free
explanation. Existing finding order/granularity is retained: secret then
interpolation then projection findings per source server, followed by document
losses then document secrets. Passthrough maps traverse leaves including nested
arrays; top-level passthrough arrays/empty containers and document losses use their
field boundary. Same-format reports omit cross-format losses/interpolation but
retain secret warnings. An inferred transport warning can name an absent `type`
key. Coordinates locate diagnostics; they are not executable edits.

Every server name, string path segment, label, message and runtime error is masked
for known credential signatures, independently; integer indices and document null
stay typed. JSON escapes controls. No field values, parser exception content or
input filenames are included. This is source-only fidelity, not a credential
audit of raw comments or a target config. Credential detection/masking is heuristic;
arbitrary names/keys stay visible and masking may collapse distinct coordinates.
Secret warnings do not refuse read-only reports. No locking or live client checks
are provided.

Compatibility is opt-in: `report --json` keeps exactly `{"findings":[...]}` with
the established four strings (`server`, `field`, `kind`, `message`), legacy labels,
formatting, order, exits and text runtime errors. Human report output also stays
unchanged. Report version 1 is a new envelope, independent of check/preview schema
versions; consumers must check both `operation` and `schemaVersion`.

For an opt-in CI gate, use the separate
[versioned finding-kind, scope, paired-rule and exact-selector policies](REPORT_POLICY.md). It does not change
this report-v1 envelope or its warning-only exit semantics.
