# Budget byte-component evidence

Input: `agentwrench checkpoints budget BASELINE.json CURRENT.json --show-byte-components`
with a valid `--policy POLICY.json` or one/both uniform growth limit flags.
Optional `--max-results N`, `--json`, presence rows/summary and policy coverage
(including its independent selector cap) compose with this switch.
Python: `budget_report(..., show_byte_components=True)`.

The valueless, budget-only switch adds `byte_components` to every displayed
comparison and violation. It adds no top-level marker. Exact keys, in order:

| Row group | Component keys |
| --- | --- |
| `thread` | `checkpoint_bytes`, `write_bytes` |
| `channel` | `checkpoint_value_bytes`, `write_bytes` |

Each component is an object with nonnegative integer `baseline_bytes` and
`current_bytes` and signed integer `growth_bytes = current_bytes - baseline_bytes`.
Values come independently from the same kind's validated snapshot row, never
from channel sums, totals minus writes, or another kind's checkpoint bytes.
An absent group contributes zero to both components on that side. A present
zero-byte group also contributes zero; use `--show-group-presence` to distinguish
these cases. New and removed groups use the same rule.

For a thread with checkpoint bytes 10 -> 2 and write bytes 1 -> 9:

```json
"byte_components": {
  "checkpoint_bytes": {"baseline_bytes": 10, "current_bytes": 2, "growth_bytes": -8},
  "write_bytes": {"baseline_bytes": 1, "current_bytes": 9, "growth_bytes": 8}
}
```

The total stays 11 -> 11, growth 0: it passes a zero allowance. A channel can
independently have different checkpoint-value bytes and total growth. Components
are diagnostics, not separate limits. Their growth sums to the row's existing
total growth; offsets may hide one component's increase in the total decision.

Human rows append (after optional presence):

```text
; byte_components: checkpoint_bytes 10 -> 2 bytes (growth -8), write_bytes 1 -> 9 bytes (growth +8)
```

Channel rows substitute `checkpoint_value_bytes`. Identifiers retain ASCII JSON
escaping. The optional row cap still independently bounds comparisons and
violations in their established sorted order. At cap zero there are no row
components and output is identical with/without this switch. No component
summary is emitted. Omission preserves the existing JSON and human output.

Negative cases: a value such as `--show-byte-components=false` or use on another
command fails parsing (exit 2, empty stdout). Missing/mixed/negative growth limits,
invalid policy, duplicate keys/selectors/groups, malformed or incomplete component
accounting, inconsistent writes, mismatched scope, oversized artifacts/group counts,
and strict unused-policy-selector refusals retain exit 2 with empty stdout even
at cap zero. Complete validation happens before any report. Hidden violations
still cause exit 3; equality, offsets, removed groups and empty inputs pass when
all total growth is within the effective limits (exit 0). Overrides, coverage,
presence, exact totals and input hashes remain unchanged. Only local snapshot and
policy JSON is read; no DB or graph access occurs. These local declarations do not
establish authenticated provenance, file size, or latest-state size.
