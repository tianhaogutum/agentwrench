# Versioned raw-row localization contract

Export: `agentwrench checkpoints prune DB --keep-latest N [--thread T] --dry-run --export-plan PLAN`.
New exports use `lgckpt-scope.prune/v2`. Application and diagnosis also accept the
unchanged v1 artifact shape. There is no implicit upgrade or v1 export switch.

V2 has exactly the v1 fields plus `raw_rows`, an object with exactly `checkpoints`
and `writes` arrays. Every row in the **whole database**, including unselected
threads and committed WAL rows, has one entry. Checkpoint entries have exactly
`thread_id`, `checkpoint_ns`, `checkpoint_id`, `sha256`; write entries additionally
have `task_id`, `idx`. Identities are literal strings; idx is a signed SQLite
64-bit integer (negative indexes are valid, booleans are not). `sha256` is 64
lowercase hex characters. It hashes the data-only typed encoding of
`(table_name, *raw_SQLite_row_in_pinned_column_order)` with SHA256. This preserves
raw types and bytes; it does not decode checkpoint state or parse metadata.
Arrays are exported in exact identity order. `checkpoints` actions still concern
only the selected scope; their identities must equal scoped raw checkpoint IDs.

Inputs retain the 64 MiB plan, 200,000 total database rows, 256 MiB payload and
16 MiB row limits. V2 raw arrays have at most 200,000 **combined** entries. Refuse
unknown/missing fields or versions, malformed/duplicate JSON keys, invalid hashes,
invalid key types/indexes, duplicate exact row identities, and scoped-ID mismatch
with exit 2 and no diagnostic report or backup/deletion. Validate fingerprints
and all actions/estimates by recomputation when the global binding matches;
edited fingerprints then refuse rather than declaring unchanged. A stale binding
refuses application before backup or deletion, regardless of version. Local
artifacts are not authenticated: plausible edits on an already stale artifact
cannot be verified against the original database.

Diagnosis: `agentwrench checkpoints plan-status DB --plan PLAN [--json]`. V2 plans produce
`lgckpt-scope.plan-status/v2` with all existing fields plus `row_localization`,
`changed_checkpoints`, `changed_writes`, `added_writes`, `removed_writes`,
`added_database_checkpoints`, `removed_database_checkpoints`. For a comparable schema,
`row_localization="database"`; changed arrays list **same-identity raw content
changes across the whole database**, sorted by the full identity, with no hashes.
The checkpoint tuple includes thread/namespace/checkpoint; writes add task ID and
index. Added/removed checkpoint lists retain their existing **selected-scope**
meaning. Added/removed write lists cover the whole database as specified below.
A removed/replaced key is not a changed key.

Concrete input: export with c1 and c2; change c1 metadata and the value or task_path
of write `(t, "", c2, task, 0)` without changing IDs. Output includes:

```json
{
  "format": "lgckpt-scope.plan-status/v2",
  "binding_matches": false,
  "path_changed": false,
  "schema_changed": false,
  "rows_changed": true,
  "scope": {"thread": null},
  "added_checkpoints": [],
  "removed_checkpoints": [],
  "added_database_checkpoints": [],
  "removed_database_checkpoints": [],
  "row_localization": "database",
  "added_writes": [],
  "removed_writes": [],
  "changed_checkpoints": [{"thread_id":"t","checkpoint_ns":"","checkpoint_id":"c1"}],
  "changed_writes": [{"thread_id":"t","checkpoint_ns":"","checkpoint_id":"c2","task_id":"task","idx":0}]
}
```

The same changes against a legacy v1 plan retain status format v1 and all existing
fields/exit semantics, adding `row_localization="unavailable-v1"` and both changed
arrays null. Write membership arrays are also null, as specified below. Human
output explicitly says localization is unavailable.
For incompatible table layouts both versions use `row_localization=
"unavailable-layout"`, changed arrays null, and the existing unknown row/identity
fields and fixed reason. Empty comparable arrays mean no same-key content change;
null means localization is unavailable. Schema/rows evidence remains independent.

Exit 0 unchanged, 3 stale, 2 invalid; diagnosis is read-only and never rewrites
plans. No payload, metadata, SQL, channel, task_path, absolute DB path or digest
values are printed by plan-status. Human output ASCII-JSON-escapes every identity
component (including task IDs); idx is an integer. Fingerprints exist only in the
local saved artifact, so protect it as potentially sensitive (hashes can permit
guessing low-entropy values). These contracts extend [plan-status.md](plan-status.md).

## Pending-write membership

The additive status contract retains formats v2 and v1 and all existing fields.
Every status object additionally has `added_writes` and `removed_writes`.
For a v2 plan with comparable tables these are arrays of objects with **exactly**
`thread_id`, `checkpoint_ns`, `checkpoint_id`, `task_id`, `idx`; no other fields.
They compare the existing bounded whole-database `raw_rows.writes` keys against
current raw write keys: added = current minus saved, removed = saved minus current.
Both arrays sort lexicographically by those five components, with **numeric** idx
ordering. Thread, namespace, checkpoint, task ID and index all distinguish keys;
empty/control/Unicode strings are literal, and negative signed-64-bit indexes
remain valid. No string concatenation, decoding or state reconstruction is used.
Coverage includes unselected threads, nonempty namespaces, orphan writes and
committed WAL rows, regardless of saved checkpoint scope.

Concrete input: export c1/c2 with writes `(t,"",c2,task,0)` and
`(t,"",c2,task,-1)`. Keep checkpoint IDs fixed, change index 0's payload, delete
index -1 and insert `(t,"",c2,new-task,2)`. V2 output adds:

```json
{
  "added_writes": [{"thread_id":"t","checkpoint_ns":"","checkpoint_id":"c2","task_id":"new-task","idx":2}],
  "removed_writes": [{"thread_id":"t","checkpoint_ns":"","checkpoint_id":"c2","task_id":"task","idx":-1}]
}
```

`changed_writes` contains only the index-0 key; the three write arrays are
pairwise disjoint. Both scoped checkpoint membership arrays stay empty;
`rows_changed=true`, `schema_changed=false`, `row_localization="database"`, exit 3.
Membership-only drift has `changed_writes=[]`; unchanged/schema-only/copy-only
v2 comparisons have both write membership arrays empty. Empty means comparable
and no membership difference, never unavailable.

Legacy v1 has no saved write identities: both membership arrays are **null**,
even for an unchanged binding, with `row_localization="unavailable-v1"` and the
existing null changed arrays. Human output explicitly says
`write membership localization: unavailable (v1 plan)` and prints unavailable
counts. V1 aggregate/scoped checkpoint diagnostics and application remain usable.
Unsupported table layouts in either version yield null write arrays with
`row_localization="unavailable-layout"`, the fixed existing reason and exit 3.

JSON and human diagnostics print identity objects/components only, never row
values, digests, channel/type/task_path, metadata, schema SQL or database paths.
Human output uses ASCII JSON escaping for added/removed write IDs too. The
existing 0/2/3 semantics, schema/row separation, strict artifact validation and
resource limits above remain unchanged. Invalid/duplicate write keys or malformed
artifacts refuse with exit 2 and **no report**, rather than partial membership
lists. Diagnosis changes no main DB, committed WAL or plan bytes (SHM bookkeeping
is allowed). Stale application refuses before backup/deletion for both versions.

## Whole-database checkpoint membership

Status formats remain v2/v1; every report adds `added_database_checkpoints` and
`removed_database_checkpoints`. For comparable v2 plans these arrays compare the
existing bounded `raw_rows.checkpoints` identities across the **whole database**.
Each object has exactly `thread_id`, `checkpoint_ns`, `checkpoint_id`, sorted
lexicographically by that literal three-string tuple. No concatenation or payload
decoding is used. Equal checkpoint IDs in separate threads/namespaces remain
separate identities, including empty, control and Unicode strings.

Concrete input: export `--thread t-1` with `(t-2,"",c1)` and `(t-2,"ns",c1)`
also present, then replace only `(t-2,"",c1)` with `(t-2,"",c3)`. V2 evidence:

```json
{
  "added_checkpoints": [],
  "removed_checkpoints": [],
  "added_database_checkpoints": [{"thread_id":"t-2","checkpoint_ns":"","checkpoint_id":"c3"}],
  "removed_database_checkpoints": [{"thread_id":"t-2","checkpoint_ns":"","checkpoint_id":"c1"}],
  "changed_checkpoints": []
}
```

Exit 3, `rows_changed=true`, `schema_changed=false`, `row_localization="database"`.
The existing `added_checkpoints` / `removed_checkpoints` retain their selected
scope for both versions; for an unfiltered v2 plan they equal the new lists.
`changed_checkpoints` still lists only same-key raw content changes; its keys and
the two new membership lists are pairwise disjoint. A key replacement with changed
content appears only in membership lists. Unchanged, schema-only and copy-only
v2 comparisons yield empty new arrays, including empty databases; a deleted
selected thread still yields scoped and global removed keys.

V1 cannot reconstruct saved unselected identities: **both new fields are null**
even if its binding matches or its scope is the whole database. Its existing
scoped lists remain usable. Human output says `database checkpoint membership
localization: unavailable (v1 plan)` and prints unavailable counts. Incompatible
checkpoint **or write** table layouts in either version give null new arrays,
`row_localization="unavailable-layout"`, the existing fixed reason and exit 3.
For comparable v2 layouts human output identifies whole-database coverage and
prints `added database checkpoints` / `removed database checkpoints` with escaped
identities. Null is unavailable; empty is a complete comparison with no difference.

No artifact change, additional row reads, resource allowances or authentication
claims are introduced. Strict malformed/duplicate/oversized artifact and invalid
raw key refusals still exit 2 with no partial report. Whole-database limits and
committed WAL snapshot coverage remain in force. Diagnosis changes no main DB,
committed WAL or plan bytes; SHM/empty sidecar bookkeeping is allowed. No row
values, metadata, SQL, paths or digest values appear in diagnostics. Application
still refuses stale plans before backup/deletion for v1 and v2.
