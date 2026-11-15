# JSON drift check contract (schema 2)

Use `agentwrench mcp-config check --source SOURCE --from FROM --target TARGET --to TO --json`.
Both paths and formats are explicit; inputs are native files, never discovered
client settings. This read-only operation writes no config, backup or other file.

After argument parsing, stdout is exactly one JSON object and stderr is empty.
Argument-parser errors (missing required options, unknown options, invalid format
choices) retain the existing text usage on stderr, empty stdout and exit 2.
`--help` retains text help. Human check formatting and other commands are retained.
A server literally named `(document)` now correctly reports its unrepresentable
settings as drift in both text and JSON, rather than confusing them with document
metadata.

All JSON outcomes have exactly these fields:

```json
{
  "schemaVersion": 2,
  "operation": "check",
  "status": "drift",
  "drift": [{"path": ["worker.tools", "args"], "kind": "change", "basis": "normalized"}],
  "findings": [],
  "error": null
}
```

- `status: "in-sync"`, exit 0: normalized projected source servers match target
  servers, with no unrepresentable source server settings. `drift` is empty;
  `error` is null. Source document losses and interpolation/secret warnings do not
  themselves imply drift.
- `status: "drift"`, exit 1: comparable differences and/or unrepresentable source
  server settings exist. `error` is null. `drift` contains comparable differences;
  `findings` identifies losses/errors, including omitted unsupported servers.
  Thus `drift` can be empty even with exit 1. Unrepresentable fields have no target
  equivalent and are not assigned a speculative target edit or deletion.
- `status: "error"`, exit 2: unreadable/non-regular/non-UTF-8 files, invalid native
  syntax, duplicate keys, bounds violations or invalid server structures in
  either input. `drift` and `findings` are empty (no partial comparison), and
  `error` is a nonempty masked explanation. This check never exits 3: a valid
  source unsupported by the destination counts as drift, not invalid input.

Each drift record has exactly `path`, `kind` and `basis`. Kinds describe the
**target-to-source** difference: `add` for a missing source server/field in the
target, `delete` for an extra target field within a compared server, and `change`
for differing values. Extra target servers and all document settings are ignored.
Added/deleted maps have a single record at that map's boundary; maps on both sides
recurse. Arrays are atomic and order-sensitive. Null differs from absence;
booleans differ from numbers. No field values are emitted.

Paths start with the literal server name, followed by literal key segments.
Dots, empty keys, slashes, Unicode and controls in keys are never split. Server
names retain the existing printable 1–256-character restriction. Records sort by
original path segments, then basis, independent of input map order.

- `basis: "normalized"` uses canonical comparison keys (`headers`, `enabled`,
  `transport`, etc.), not necessarily native destination keys. Missing args/env
  for stdio and headers for remote servers normalize to empty; missing enabled
  normalizes to true. Transport aliases and native header/disablement spellings
  use the existing canonical mapping. A missing whole server has path `[name]`.
- `basis: "native"` compares unknown server fields only for same-format inputs.
  Its paths use native keys, with no format wrapper. The basis distinguishes a
  native unknown key such as `transport` from the synthetic normalized transport.
  Cross-format target unknown fields remain ignored.

`findings` describes source fidelity, not a scan of target credentials. It uses
explicit scope and source-native coordinates as defined below. Known credential
signatures are masked; arbitrary names/keys remain visible. Values, parser
exception content and input filenames are not emitted. No environment expansion,
network or server execution occurs. No snapshot locking or runtime-equivalence
guarantee is provided.

## Fidelity paths and version compatibility

`check --json` now emits **schemaVersion 2** for every post-parse outcome,
including errors and empty results. The top-level keys, drift records, status and
exit 0/1/2 semantics above are unchanged. This is an explicit version change:
consumers requiring schema 1 must update; no schema-1 selector is provided.
`report --json` and human output keep their existing four-string findings and
legacy labels. Three-way JSON previews now use schema 2 with the same scoped
finding shape; see [the preview contract](JSON_PREVIEW.md).

In schema 2 each check finding has exactly these six keys:

```json
{"scope":"server","server":"worker.tools","path":["vendor.dot","工具",0,""],"field":"vendor.dot.工具[0].","kind":"loss","message":"native passthrough has no verified equivalent; omitted"}
```

- `scope` is `"server"` or `"document"`. For server scope, `server` is the literal
  source server name and `path` is relative to its native server object. For
  document scope, `server` is null and `path` is relative to the native document
  root. Native server-container keys and Continue's list index/name wrapper are
  excluded from server paths. A real server named `(document)` has server scope;
  the legacy document display label is never used to infer scope.
- `path` is a nonempty array of literal string object keys and nonnegative integer
  array indices. Empty string keys, dots, bracket-like text, slashes, Unicode and
  control characters remain literal; integer `0` differs from string `"0"`.
  Segments originate in structured source traversal or explicit native field
  mappings, never by parsing `field`. Missing/inferred transport diagnostics use
  the source `type` coordinate even if that optional key was absent. Paths locate
  diagnostics, not executable edits or a promise that every coordinate exists.
- `field` retains the legacy human dotted/bracket label, including its historical
  ambiguities. It is display-only: two different paths may have the same label.
- `kind` is `loss`, `error`, `secret` or `interpolation`; `message` is a value-free
  explanation. Existing finding emission order and granularity are retained:
  passthrough maps traverse their leaves (including nested arrays), but top-level
  passthrough arrays/empty containers and document losses use their field boundary.
  Secret/interpolation warnings traverse leaves, including array indices.

For example a literal `{"a.b":1}` and nested `{"a":{"b":2}}` inside a lost
native map yield distinct paths `["vendor","a.b"]` and `["vendor","a","b"]`,
although both have `field: "vendor.a.b"`. A document setting named `"a.b"` has
`scope: "document", server: null, path: ["a.b"]`. A lost field on the server
`(document)` has `scope: "server", server: "(document)"`.

Negative cases retain the existing contract: malformed, invalid, duplicate,
unreadable, non-UTF-8, oversized or too-deep input on either side produces exit 2
with empty `drift` and `findings`, even after source findings were collected.
Valid unsupported transports, disabled/unrepresentable servers and native-only
fields still yield exit 1 through findings, possibly with empty comparable drift.
Document losses and credential/interpolation warnings alone still allow exit 0.
Known tokens are masked separately in every string segment, server, legacy label
and message; indices and document null stay typed. Masking can collapse paths and
is heuristic; arbitrary names/keys remain visible. No values or file paths are
added, and checks write nothing. Comparable drift ordering/bases, normalization,
extra-target-server ignoring and invalid-target validation are unchanged.
