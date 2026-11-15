# Saved prune action comparison

`agentwrench checkpoints compare-plans BASELINE.json CURRENT.json [--json]` reads only two saved
plan artifacts. The database may be absent. Both inputs must pass the existing
strict, bounded `lgckpt-scope.prune/v1` or `/v2` loader: exact fields and types,
positive integer retention, nonnegative summary counts, valid digests, unique JSON
keys and full checkpoint/raw-row identities, and v2 scoped/raw membership agreement.
The limits remain 64 MiB per file, 200,000 selected checkpoints per plan and
200,000 combined v2 raw entries per plan. Both files are validated before output.

The complete `database` objects must be equal: literal saved path, schema SHA256
and whole-database rows SHA256. Scope objects must also be equal, including the
difference between `thread: null` and a literal thread string. Paths are compared
as saved strings; no filesystem resolution or database access occurs. Each saved
checkpoint must belong to its declared thread scope, including for v1. If both
inputs are v2, their full raw-row identity/digest maps must agree, independent of
array order; contradictory snapshot evidence is incomparable. A mixed v1/v2 pair
uses the common database binding and scope; missing v1 raw evidence is never
invented. A path, schema, content, scope or v2 raw-evidence mismatch exits **2**.

JSON has exactly these fields (example: change retention from keep-two to keep-one
for a snapshot containing c1/c2 in `(t, "")`):

```json
{
  "format": "lgckpt-scope.plan-comparison/v1",
  "actions_identical": false,
  "scope": {"thread": null},
  "newly_selected": [],
  "no_longer_selected": [],
  "keep_to_delete": [{"thread_id":"t","checkpoint_ns":"","checkpoint_id":"c1"}],
  "delete_to_keep": []
}
```

Selection means presence in the plan's `checkpoints` action array, **not** just a
delete action. `newly_selected` = current-only identities, `no_longer_selected` =
baseline-only identities; their objects contain the three identity fields plus
the saved `action` from the side where present. `keep_to_delete` and
`delete_to_keep` contain only the three identity fields for common identities
whose action changed. All four arrays are disjoint and sorted by the literal
`(thread_id, checkpoint_ns, checkpoint_id)` tuple. Empty strings, case, Unicode
and namespace distinctions are preserved; IDs alone never establish equivalence.

Exit **0** means all four lists are empty (identical actions); **3** means at least
one difference. Retention settings, estimates, row ordering and input version
alone do not constitute action changes. Genuine exports from the same snapshot
and scope have equal checkpoint membership; newly/no-longer-selected evidence
can arise from legacy action arrays whose completeness cannot be established
offline. V2 already validates completeness against its saved raw inventory.

Malformed, missing, oversized, duplicate, unsupported or incomparable inputs
exit **2** with a fixed diagnostic on stderr and **no report** on stdout. Human
output ASCII-JSON-escapes identifiers. Neither output mode prints database paths,
digests, row bodies or input file contents. No SQLite connection, decoding,
backup, prune, graph execution or plan rewrite occurs.

This compares saved declarations, not authenticated provenance or prune safety.
The offline comparison cannot recompute digests, verify summaries against row
bodies, or certify that saved actions implement retention correctly. Application
must still revalidate the complete artifact against the database under its lock.
This command is distinct from `plan-status`, which opens a database to diagnose
snapshot drift and reports scoped and whole-database membership changes.

## Opt-in bounded difference output

`compare-plans BASELINE.json CURRENT.json --max-differences N [--json]` caps
**each** of the four sorted difference arrays at its first N entries. N must be
an ASCII decimal nonnegative integer (`0`, `1`, `002`, etc.); zero is counts-only.
Signs, whitespace, fractions, exponents, non-ASCII digits and values exceeding
Python's integer-string conversion limit are invalid. A missing or invalid value
exits **2** with an argparse diagnostic on stderr, no stdout and no artifact reads.
The Python comparison function accepts only `None` or an exact nonnegative `int`
(not a boolean); invalid values raise `ScopeError` before reading artifacts.

Without the option, JSON fields and human output remain exactly as above. With
the option, the same `lgckpt-scope.plan-comparison/v1` report adds only
`max_differences: N` and `difference_summary`, containing an entry for every array:

```json
{
  "max_differences": 0,
  "difference_summary": {
    "newly_selected": {"total": 0, "shown": 0, "truncated": false},
    "no_longer_selected": {"total": 0, "shown": 0, "truncated": false},
    "keep_to_delete": {"total": 1, "shown": 0, "truncated": true},
    "delete_to_keep": {"total": 0, "shown": 0, "truncated": false}
  }
}
```

The example's four arrays are all empty, but `actions_identical` is false and
exit **3** still applies. `total` is the exact full difference count; `shown` is
`min(total, N)`; `truncated` is exactly `total > shown`. An empty array with
`total: 0, truncated: false` is complete. N equal to or greater than a total
never marks that list truncated. The cap is independent per list, not a combined
row or byte budget. All complete validated action differences and their full
identity order are computed before any cap; no input validation or raw-evidence
compatibility check may stop early. The input/resource bounds remain unchanged.

Human list headers become `keep_to_delete: 1 total, 0 shown, truncated: true`
(and analogously for every list), followed by only the shown escaped identities.
IDENTICAL/DIFFERENT and exit **0/3** reflect the complete comparison, even when no
identities are shown. Invalid/incomparable inputs still exit **2** without a
partial report. The option never reads database/checkpoint bodies or rewrites
input artifacts, including for legacy membership comparisons.
