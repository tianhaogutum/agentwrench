# Exact budget group presence (opt-in)

`agentwrench checkpoints budget BASELINE.json CURRENT.json --show-group-presence` accepts the same
policy or uniform limits as ordinary budget evaluation; it does not require a
policy. The Python API is `budget_report(..., show_group_presence=True)`.

Every displayed comparison and violation gains two JSON booleans:
`baseline_present` and `current_present`. They mean exact identity membership
in the fully validated baseline/current snapshot's corresponding group array.
No new top-level field or snapshot/policy format is introduced. For example,
a newly added zero-byte channel comparison includes:

```json
{"group":"channel","thread_id":"t","checkpoint_ns":"","channel":"data",
 "baseline_bytes":0,"current_bytes":0,"growth_bytes":0,"limit_bytes":0,
 "exceeded_bytes":0,"baseline_present":false,"current_present":true}
```

Removed zero-byte groups have true/false; common zero-byte groups have true/true.
Union comparisons cannot have false/false. Thread membership is independent of
channel membership: a thread with no channel entries does not imply an empty or
other channel. Matching is literal, case-sensitive and namespace-sensitive,
including empty strings, asterisks, control characters and Unicode (no
normalization). Only group kinds selected by the limits are compared.

Human rows append `; baseline_present: false, current_present: true` (lowercase
booleans, labels unchanged across group kinds). Identity rendering remains
ASCII-escaped JSON strings. With a display cap, both comparison and violation
rows carry presence; with no cap human output still shows comparisons only.

Without the flag, JSON fields and human output are unchanged. Byte-zero fallback,
limit selection/override precedence, ordering, coverage, passed and exit 0/3 are
unchanged. Presence is evidence only, not a membership budget. `--max-results N`
remains independent per array with complete exact totals; N=0 produces unchanged
counts-only output with no presence rows or new top-level marker. Coverage evidence
remains complete and uncapped. Strict coverage refuses unused selectors before
any report, with exit 2 and empty stdout.

Negative contracts: the flag does not replace required limits or relax policy vs
uniform exclusivity, scope equality, JSON/identity uniqueness, component accounting,
opaque-capture refusals or file/group/override resource limits. Invalid artifacts,
missing limits, incompatible scope and strict coverage errors retain exit 2,
empty stdout and escaped diagnostics, even at cap zero. No DB/graph is opened or
input file rewritten. The switch accepts no value; an explicit value or use on a
different command is a parser error (exit 2, empty stdout).

Presence establishes membership only in local accounting artifacts, not live DB
state, authenticated provenance or intended workload scope. Existing bounded
input validation and entry caps still apply; this does not bound output bytes.
