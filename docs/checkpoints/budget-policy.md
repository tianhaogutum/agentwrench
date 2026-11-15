# Budget policy v1 contract

`agentwrench checkpoints budget BASELINE.json CURRENT.json --policy POLICY.json [--json]`
uses `lgckpt-scope.budget-policy/v1`. The complete schema is:

```json
{
  "format": "lgckpt-scope.budget-policy/v1",
  "default": {
    "max_thread_growth_bytes": 4096,
    "max_channel_growth_bytes": 0
  },
  "threads": [
    {"thread_id": "t", "checkpoint_ns": "", "max_growth_bytes": 1024}
  ],
  "channels": [
    {"thread_id": "t", "checkpoint_ns": "", "channel": "data", "max_growth_bytes": 17},
    {"thread_id": "t", "checkpoint_ns": "", "channel": "__internal__", "max_growth_bytes": 4}
  ]
}
```

All four top-level fields and both defaults are required. Empty override arrays
are valid. Limits are nonnegative JSON integers; zero forbids positive growth.
Both kinds are checked independently. Each exact `(thread_id, checkpoint_ns)` or
`(thread_id, checkpoint_ns, channel)` override replaces its kind's default for
that group. A thread override does not affect its channels. Identifiers are
literal, case-sensitive strings, including empty strings; `*` has no special
meaning. No prefix, wildcard, regex, namespace-only, or channel-only selectors.
Unused overrides are allowed so a policy can cover future or removed groups.

## Opt-in override coverage

`agentwrench checkpoints budget BASELINE.json CURRENT.json --policy POLICY.json
--require-policy-match [--json]` requires every override to match an exact group
in **either** snapshot. Thread `(thread_id, checkpoint_ns)` and channel
`(thread_id, checkpoint_ns, channel)` selectors are checked independently;
matching a thread never covers a missing channel. New, removed and zero-byte
groups count as matches. Defaults need not match any group, so a defaults-only
policy also works with empty snapshots. Without the flag, unused overrides remain
valid. The flag requires `--policy`; uniform limits cannot be combined with it.

Concrete typo regression: baseline has `(t, "", data)` at 9 bytes, current at 28.
A policy with both defaults at 4096 and a channel override for `dtaa` at 0
silently passes without strict coverage: `data` grows 19 bytes under its default.
With `--require-policy-match`, exit **2**, empty stdout (also with `--json`), and
this diagnostic on stderr:

```text
error: unused budget policy channel selector: thread_id="t", checkpoint_ns="", channel="dtaa"
```

Identifiers are JSON-quoted with ASCII escaping, including controls and Unicode.
If several overrides are unused, diagnose only the first in thread-then-channel,
sorted tuple order. Strict coverage runs after existing policy/snapshot validation
and same-scope checks, before any budget report; it does not hide malformed input.
Covered policies retain the exact report shape and exit **0**/**3** semantics,
including removed groups' negative growth. No database or graph access occurs;
policy and snapshot files remain unchanged. Existing resource limits still apply.

## Opt-in coverage evidence without refusal

`agentwrench checkpoints budget BASELINE.json CURRENT.json --policy POLICY.json
--show-policy-coverage --json` adds exactly one top-level `policy_coverage` object.
It has `threads` and `channels` entries, each with integer `total_overrides`,
`covered_overrides`, `unused_overrides` counts and an `unused_selectors` array.
Total equals covered plus unused; unused equals the array length. Counts refer
to exact policy overrides, not snapshot groups or defaults. Each override counts
once even if present in both snapshots. Matching is the same independent, literal,
case-sensitive union matching as strict coverage, including new, removed and
zero-byte groups. Empty strings and literal `*` retain their usual exact meaning.

For the typo example above (no thread overrides, one unused `dtaa` override),
the added object is exactly:

```json
{
  "threads": {
    "total_overrides": 0, "covered_overrides": 0, "unused_overrides": 0,
    "unused_selectors": []
  },
  "channels": {
    "total_overrides": 1, "covered_overrides": 0, "unused_overrides": 1,
    "unused_selectors": [{"thread_id": "t", "checkpoint_ns": "", "channel": "dtaa"}]
  }
}
```

Unused thread objects contain only `thread_id` and `checkpoint_ns`; channel
objects also contain `channel`. They contain no limit or payload values. Arrays
are sorted lexicographically by their exact identity tuples, independent of file
order. Identifiers remain JSON string data, including controls and Unicode.
Covered overrides are identifiable by existing comparisons' `limit_source`.
Defaults-only policies report zero counts and empty arrays, even for empty
snapshots. With no matching groups, all overrides are unused.

The flag also works without `--json`: append a `policy coverage:` block after
comparisons, with thread then channel count lines and each unused selector using
ASCII JSON-quoted identifiers. Without `--show-policy-coverage`, both JSON shape
and human output remain unchanged. Coverage does not affect `passed`, comparisons,
violations, or exits **0**/**3**. For example, the typo above still passes under
the permissive default; changing `dtaa` to `data` with allowance 17 produces exit
3 with growth 19 and exceeded 2 while reporting that override covered.

Negative cases: `--show-policy-coverage` requires `--policy` (exit **2**, empty
stdout and `error: --show-policy-coverage requires --policy` for valid snapshot
inputs), including when uniform limits are supplied. Mixed policy/uniform limits,
malformed/duplicate/oversized policy or snapshot inputs, invalid components and
incompatible scopes still refuse with exit **2** and no report. Evidence never
bypasses existing validation or resource limits. Combining both coverage flags
still refuses the first unused selector with the existing strict diagnostic and
no report; fully covered policies return coverage evidence with exit 0 or 3.
No database or graph access occurs, and input files remain unchanged. Coverage
proves presence in either snapshot, not provenance or intended policy scope.

## Comparison and validation

The existing snapshot accounting and scope validation are unchanged. Every group
in either snapshot is compared in deterministic thread-then-channel tuple order.
New groups start at zero, removed groups end at zero, and equality passes.
Policy comparisons add `limit_source` (`default` or `override`) beside the
effective `limit_bytes`; the report adds `policy_format`. Human output also
shows the source. Exit 0 means all groups pass; exit 3 includes exact violations.
The original uniform-limit flags remain available without a policy and retain
their output shape.

Concrete regression: retain a 19-byte `data` value alongside a baseline 9-byte
value, and add 4 bytes of `__internal__` writes. With the example policy, `data`
reports baseline 9, current 28, growth 19, limit 17, exceeded 2, source `override`;
the internal channel passes at equality. Raising the data override to 19 passes
when the thread allowance covers its checkpoint/write bytes. A same-named channel
in another thread or namespace uses the default, not this override.

Exit 2 emits a diagnostic on stderr and no report for mixed `--policy` and either
uniform-limit flag, unsupported versions, missing/extra fields, malformed or
non-UTF-8 JSON, duplicate JSON keys, duplicate exact selectors, non-string
identifiers, null/bool/fractional/negative limits, or invalid/incompatible
snapshots. A policy is limited to 1 MiB and 10,000 combined overrides. The CLI
reads only the policy and two snapshot files; it never opens the source database
or changes any input. These artifacts are local configuration, not authenticated
provenance. Percentage limits and latest-state budgets are outside this version.
