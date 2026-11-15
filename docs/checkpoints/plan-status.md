# Read-only saved-plan diagnostics contract

This is the base v1 contract. New exports use v2; see
[versioned raw-row localization](prune-plan-v2.md) for the exact v2 artifact and
output contract and additive explicit localization-unavailable fields on v1
reports. The rules below remain applicable to v1 plans.

Input: `agentwrench checkpoints plan-status DB --plan PLAN.json [--json]`. DB must be an existing
SQLite file; PLAN is an existing bounded `lgckpt-scope.prune/v1` artifact. This
command accepts no retention, scope, backup, deletion, export, or VACUUM options.
It uses a single read-only SQLite transaction, includes committed WAL rows, and
never imports a graph or decodes checkpoint/write payloads for stale diagnostics.
The original plan is never rewritten. Apply still revalidates under its writer lock.

JSON output uses `lgckpt-scope.plan-status/v1`:

```json
{
  "format": "lgckpt-scope.plan-status/v1",
  "binding_matches": false,
  "path_changed": false,
  "schema_changed": false,
  "rows_changed": true,
  "scope": {"thread": "t-1"},
  "added_checkpoints": [
    {"thread_id": "t-1", "checkpoint_ns": "", "checkpoint_id": "c3"}
  ],
  "removed_checkpoints": [
    {"thread_id": "t-1", "checkpoint_ns": "", "checkpoint_id": "c1"}
  ]
}
```

Concrete example: export a plan for selected IDs c1/c2 in t-1, then remove c1
and add c3. The output above identifies exact added/removed tuples, ordered by
(thread_id, checkpoint_ns, checkpoint_id). Namespace/thread are part of identity,
so equal checkpoint IDs in different groups never cancel. Scope comes only from
the file. An empty or deleted selected thread yields removed IDs, not a missing
thread error. Lists concern the **plan scope**: v1 plans do not retain IDs for
unselected threads. `rows_changed` still compares **all raw checkpoint, metadata,
and write rows in the database**, so an unselected change can produce true with
empty added/removed lists. Stable IDs with changed metadata, payloads, parent links,
or pending writes likewise produce true with empty lists.

`schema_changed` independently compares schema SQL, schema version, user version
and application ID. A compatible extra index/table/view/trigger can therefore
produce `schema_changed=true, rows_changed=false`. Such a schema still refuses
prune. A database at a different resolved path can produce `path_changed=true`
with the other comparisons false. No payload values, SQL, hashes, state summaries,
write channels, or metadata values are printed. Human output quotes identifiers
using ASCII JSON escaping, including terminal controls and Unicode.

If the two pinned saver tables no longer have the expected columns or are no
longer real tables, schema/path comparisons still work, but `rows_changed`,
`added_checkpoints`, and `removed_checkpoints` are null. The report includes
`rows_unavailable_reason="unsupported checkpoint/write table layout"`. Null means
unknown, never unchanged. The command does not execute queries against views in
place of the saver tables or attempt a migration. The same whole-database row,
payload, and per-row limits as exported plans apply whenever rows are comparable.
Opaque payloads, orphan writes, and nonempty namespaces can still be diagnosed
because stale diagnostics use raw rows only.

Exit **0**: the whole binding matches. For this case, recompute actions/summary
using normal prune validation and reject an edited/inconsistent artifact rather
than declaring it current. Exit **3**: at least one binding component differs;
return the diagnostic report (including unavailable row comparisons after a table
layout change). Exit **2** with stderr and no report: invalid/missing/non-SQLite DB,
malformed/duplicate-key/unsupported/oversized plan, resource limit, invalid raw row
keys, unreadable inputs, or inconsistent plan when the binding matches. No
execution flags are accepted. Inputs remain unchanged on success and failure
(apart from normal SQLite WAL shared-memory bookkeeping).

The command explains fingerprint drift; it does not authenticate a plan, prove
pruning safe on changed data, or rebase a decision. A stale plan's actions/summary
cannot be revalidated against its original database. Export and review a new plan
after changes. The original `prune --apply-plan` behavior remains conservative.

## Opt-in bounded identity output (v1 and v2)

`plan-status DB --plan PLAN.json --max-differences N [--json]` caps each of the
eight identity arrays independently, after complete snapshot diagnosis and all
validation. The arrays are `added_checkpoints`, `removed_checkpoints`,
`added_database_checkpoints`, `removed_database_checkpoints`, `added_writes`,
`removed_writes`, `changed_checkpoints`, and `changed_writes`. Their scoped/global
meanings do not change. Each available array retains its first N full identities
in literal tuple order, including numeric write index ordering. Same-key changes
remain disjoint from membership differences for that table.

N is a nonnegative ASCII decimal integer (leading zeros allowed); zero is
counts-only. Missing values, signs, whitespace, fractions, exponents, non-ASCII
digits and values exceeding Python's integer-string conversion limit exit **2**
with an argparse diagnostic, empty stdout and no DB/plan reads. The Python API
accepts only `None` or an exact nonnegative `int` (not bool), raising `ScopeError`
for invalid limits before reads.

Default full JSON fields, status format versions and human output are unchanged.
With the option, JSON adds `max_differences: N` and `difference_summary`, with
one entry per identity array. For an available list each entry is
`{"total": T, "shown": S, "truncated": B}`: T is the exact full count,
S = min(T, N), and B = (S < T). For an unavailable list the array **and its summary
entry** are null; no zero count is invented for missing evidence.

For example, with N=0 and two new scoped checkpoints, `added_checkpoints` is `[]`
and its summary is `{"total":2,"shown":0,"truncated":true}`. An available
unchanged list is `[]` with total/shown 0 and truncated false. A v1 global
checkpoint/write/changed list is null with a null summary, even on an unchanged
binding. Incompatible table layouts make all eight arrays and summaries null.
Scoped v1 checkpoint lists remain available with a compatible layout.

Human headers for available lists become, for example,
`added checkpoints: 2 total, 0 shown, truncated: true`; unavailable headers keep
`unavailable`. Only shown identities follow, ASCII-JSON-escaped as before.
UNCHANGED/STALE, binding flags, localization labels and exit **0/3** derive from
the complete diagnosis, including with all shown arrays empty. Schema/path-only
changes still exit 3. Exit **2** errors never emit a partial report.

This is a per-list display-entry cap, not a byte, memory or input-work bound.
Complete bounded raw comparisons and matching-binding action/summary validation
precede caps. Malformed/duplicate/oversized artifacts, invalid raw keys and DB
row/payload limits still refuse at every cap, including zero. No row values,
digests or SQL are printed. One committed-WAL-aware read-only snapshot is used;
DB/WAL/plan bytes stay unchanged apart from normal SHM bookkeeping. Application
still refuses stale plans before backup/deletion and never consumes capped output.
