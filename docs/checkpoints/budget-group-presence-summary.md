# Complete budget group presence counts (opt-in)

`budget BASELINE.json CURRENT.json --show-group-presence-summary` accepts the
usual policy or uniform growth limits. The API is
`budget_report(..., show_group_presence_summary=True)`.

The option adds exactly one top-level JSON object, `group_presence_summary`.
Its keys are only the evaluated kinds (`threads`, `channels`, in that order).
Each value has five exact nonnegative integer counts:

```json
{"group_presence_summary":{"channels":{"baseline":2,"current":3,"added":2,"removed":1,"common":1}}}
```

For each kind independently, baseline/current count its full validated snapshot
index; common counts the intersection, added counts current minus baseline, and
removed counts baseline minus current. Thus baseline=common+removed,
current=common+added and union=common+added+removed. Zero-byte groups count,
including new and removed ones. Threads with no channels count only as threads.
Empty evaluated kinds have five zeros. Identity is literal, case-sensitive and
namespace-sensitive, with no Unicode normalization or wildcard interpretation.

Human output appends this block after evidence rows and before policy coverage:

```text
group presence summary:
  channels: 2 baseline, 3 current, 2 added, 1 removed, 1 common
```

The counts do not depend on `--max-results` (even 0) or `--show-group-presence`.
Neither option implies the other. With no summary flag there is no summary field
or human block; all existing defaults remain unchanged. Existing comparison and
violation summaries remain exact and independent. Counts do not change bytes,
limits, override precedence, ordering, passed, exits 0/3, uncapped coverage or
snapshot/policy formats. Membership changes alone do not fail a byte budget.

Negative contracts: the switch accepts no value and is budget-only (parser exit
2, empty stdout otherwise). It does not substitute for growth limits, permit
policy/uniform mixing or relax complete artifact validation, duplicate JSON keys
or identities, component totals, scope equality, malformed input or resource
limits. Strict coverage refuses before any summary/report (exit 2, empty stdout).
All input artifacts are preserved; no DB connection or graph execution is needed.
Existing bounded validation still applies and display caps bound entries, not
bytes or evaluation memory. Counts attest only to local unauthenticated artifact
membership, not live DB state, workload intent or provenance.
