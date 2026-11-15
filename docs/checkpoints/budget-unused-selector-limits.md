# Bounded unused policy selectors (opt-in)

`budget BASELINE.json CURRENT.json --policy POLICY.json --show-policy-coverage
--max-unused-selectors N [--max-results M] [--json]` caps each coverage kind's
`unused_selectors` independently. The API is
`budget_report(..., policy=policy, show_policy_coverage=True, max_unused_selectors=N)`.

JSON adds top-level `max_unused_selectors: N`. Each of `policy_coverage.threads`
and `.channels` retains exact `total_overrides`, `covered_overrides` and
`unused_overrides` from full matching, keeps the first N sorted unused identities,
and adds `unused_selectors_summary` with exact `total`, `shown` and boolean
`truncated` (`shown < total`). `total` equals `unused_overrides`. For example:

```json
{"total_overrides":5,"covered_overrides":2,"unused_overrides":3,"unused_selectors":[{"thread_id":"a","checkpoint_ns":""}],"unused_selectors_summary":{"total":3,"shown":1,"truncated":true}}
```

Human output retains each existing coverage count line and inserts a line before
that kind's bounded, ASCII-escaped `unused:` identities:

```text
  threads: 5 total overrides, 2 covered, 3 unused
    unused selectors: 3 total, 1 shown, truncated: true
    unused: thread_id="a", checkpoint_ns=""
```

N=0 hides both identity lists, without hiding counts or budget failures. Complete
empty lists have total/shown 0 and truncated false. N at or above the total has
truncated false. Omission (`None` in the API) preserves all existing output,
including complete unused lists, and adds no cap/summary fields or human lines.

Both lists use exact literal tuple order `(thread_id, checkpoint_ns[, channel])`,
without case folding, wildcard matching or Unicode normalization. Matching uses
each kind's complete validated baseline/current union independently, including
removed/zero-byte groups. Thread-only groups cannot cover channel selectors.
Neither this cap nor `--max-results` implies or limits the other. Group presence
rows/summaries, effective limits, override precedence, full comparisons, result
totals, decisions and exit 0/3 remain unchanged.

Negative contracts: the option is budget-only and requires both
`--show-policy-coverage` and `--policy`. Missing dependencies cause `ScopeError`
(CLI exit 2, empty stdout); a missing policy retains the existing coverage-policy
diagnostic when coverage is requested. N accepts only nonnegative ASCII decimal
digits (leading zeros allowed, subject to Python's integer-string conversion
bound). Missing/empty/signed/negative/fractional/whitespace/non-ASCII/nonnumeric
values fail parsing with exit 2 and empty stdout before input reads. The API
accepts only an exact nonnegative int or None, rejecting bool and other types.
Strict coverage still refuses the first unused selector before any report,
including at zero, with the same escaped diagnostic. Full malformed, duplicate,
scope, component, policy/uniform-mixing and resource validation is retained;
hidden invalid entries cannot pass. Snapshot/policy artifact formats are unchanged.

This bounds displayed entries, not bytes, validation work or memory. Coverage
proves local unauthenticated artifact membership only. Inputs remain unchanged;
budget needs no DB connection, graph import/execution or network access.
