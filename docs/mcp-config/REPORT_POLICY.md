# Finding-kind report policy contracts

## Unfiltered selection (version 1)

Use `agentwrench mcp-config report --from FROM --to TO INPUT --policy-json-v1` with zero or
more `--fail-on KIND` options. Each KIND is exactly `loss`, `secret` or
`interpolation` (case-sensitive, one kind per option, no comma lists). Repetition
is allowed and duplicates collapse. Both selection and matches use fixed order
`loss`, `secret`, `interpolation`, independent of argument/finding order.
Omitting `--fail-on` explicitly selects nothing; it does not select all kinds.
There are no scope/path filters: selected kinds apply to server and document
findings alike. A real server named `(document)` retains server scope.

`--policy-json-v1`, `--json-v1` and legacy `--json` are mutually exclusive.
`--fail-on` requires a policy mode (v1 here, or v2 below). These options exist only on `report`.
Missing required arguments, unknown options/kinds, missing kind values, mixed
JSON modes and selection without policy mode are parser errors: exit 2, text
usage on stderr, empty stdout, before reading INPUT. `error` is not selectable;
unsupported projection always fails independently of the policy.

After successful argument parsing, stdout is one object and stderr is empty:

```json
{
  "schemaVersion": 1,
  "operation": "report-policy",
  "status": "accepted",
  "findings": [],
  "policy": {
    "failOn": ["loss", "secret"],
    "matchedKinds": [],
    "decision": "accepted"
  },
  "error": null
}
```

Exactly those six envelope keys and three policy keys are present in every
post-parse outcome. `matchedKinds` contains only selected kinds that occur at
least once, not counts or finding indices. All findings, including nonselected
warnings, remain in their original order. Findings reuse the six-key scoped
source-native shape, granularity and masking in [report v1](JSON_REPORT.md).
Both versions are independent contracts: check `operation` and `schemaVersion`.
Existing report v1, legacy JSON and human bytes and exit codes remain unchanged.

Precedence, from highest to lowest:

| Condition | Exit | `status` | Policy | `error` / findings |
| --- | --- | --- | --- | --- |
| Invalid native input/read/processing failure | 2 | `error` | Normalized `failOn`, `matchedKinds: null`, `decision: "not-evaluated"` | Masked nonempty error, empty findings |
| Valid input, any unsupported server projection | 3 | `unsupported` | Evaluated, decision `accepted` or `rejected` independently | Fixed `unsupported server conversion; no files written`, all source findings |
| Supported projection, any selected kind present | 1 | `rejected` | Evaluated, nonempty matches, decision `rejected` | Null error, all source findings |
| Supported projection, no selected kind present | 0 | `accepted` | Evaluated, empty matches, decision `accepted` | Null error, all source findings |

Invalid server schema anywhere takes precedence over projection/policy evaluation;
processing errors discard even precollected findings. Invalid input never claims
policy acceptance. Unsupported input retains complete diagnostics, never a partial
configuration; its evaluated policy cannot override exit 3. Empty collections,
no-findings reports and empty policies are valid. Nonselected warnings remain
informational. Same-format reports still omit cross-format loss/interpolation
findings but retain secrets. Policy evaluates emitted findings, not raw values.

The mode reads only the explicit bounded UTF-8 native source. No rendering,
writes/backups, environment expansion, execution or network calls occur. It emits
no field values or filenames. Known-token masking is heuristic and can collapse
coordinates; arbitrary names/keys remain visible. Acceptance is neither a complete
credential audit nor runtime/lossless-conversion certification. Output delivery
requires usable streams; no authentication, snapshot locking or saved-plan
binding is added.

## Opt-in scope selection (version 2)

Use `report --from FROM --to TO INPUT --policy-json-v2 [--fail-on KIND ...]
[--fail-scope SCOPE ...]`. Version 2 is a separate explicit opt-in; version 1
remains unfiltered with its exact schema and bytes. All four report JSON modes
(`--json`, `--json-v1`, `--policy-json-v1`, `--policy-json-v2`) are mutually
exclusive. `--fail-on` accepts either policy version; `--fail-scope` requires
version 2, including when the requested scopes are both scopes.

SCOPE is exactly `server` or `document`, case-sensitive, one per option. No
comma lists, server names, JSON arrays, paths, whitespace trimming or wildcards.
Omitting `--fail-scope` selects both scopes. Repetition is allowed; duplicates
collapse and `scopes` always orders `server`, `document`. An explicit empty value
is invalid, not an empty selection; version 2 has no empty-scope selector. To
select no findings, omit `--fail-on` (the kind selection remains empty). These
choices avoid silently accepting a misspelled or empty scope. Unknown/empty or
missing selector values, scopes in other modes, missing input/formats and mixed
JSON modes exit 2 with text usage stderr and empty stdout before reading INPUT.
`--fail-scope` and version 2 exist only on `report`.

Version 2 retains exactly the six version-1 envelope keys and the same
`operation: "report-policy"`, status, findings and error rules, but sets
`schemaVersion: 2` and uses exactly these four policy keys:

```json
{
  "failOn": ["loss", "secret"],
  "scopes": ["server"],
  "matchedKinds": ["loss"],
  "decision": "rejected"
}
```

A kind matches only if **one finding has both that selected kind and a selected
scope**. The scope comes from structured native metadata, never the displayed
server name or field. Document metadata uses `scope: "document", server: null`;
a real server named `(document)` uses `scope: "server", server: "(document)"`.
There are no per-server, path, pair-specific or threshold selectors.
`matchedKinds` remains deduplicated in kind order, without counts/coordinates.
The full findings list, including nonselected scopes/kinds and unsupported
errors, is identical in order and content to the unfiltered report v1 list.
Scopes affect policy matching only, never diagnostic collection.

Precedence is unchanged: invalid input/processing exits 2 with empty findings,
normalized `failOn`/`scopes`, `matchedKinds: null` and `decision: "not-evaluated"`.
Valid unsupported projections always exit 3 with all findings and independently
evaluated policy, even if all scopes/kinds of their warnings are excluded.
Supported selected matches exit 1; otherwise exit 0, including empty kind
selection, no findings and empty server collections. Same-format reports keep
the existing emission rules (secrets remain; cross-format losses/interpolation
are absent). With omitted scopes, version-2 policy decisions equal version 1;
only the envelope version and added `scopes` differ. All value masking, source
bounds and read-only/no-render/no-expansion/no-execution/no-network guarantees
above apply unchanged. Acceptance does not certify runtime or lossless behavior.

## Opt-in paired kind/scope rules (version 3)

Use `report --from FROM --to TO INPUT --policy-json-v3 [--fail-rule KIND:SCOPE ...]`.
Each rule is exactly one of `loss:server`, `loss:document`, `secret:server`,
`secret:document`, `interpolation:server`, `interpolation:document`. Kind and scope
are case-sensitive; no whitespace trimming, comma lists, JSON, wildcards, names
or paths. Repeat `--fail-rule` to select multiple independent pairs. Omission
selects no rules. Duplicate pairs collapse; normalization orders by kind
(`loss`, `secret`, `interpolation`), then scope (`server`, `document`). An empty,
unknown or missing rule value is a parser error, never an empty/all selection.

All five JSON modes are mutually exclusive. `--fail-rule` requires version 3;
`--fail-on` and `--fail-scope` cannot be used with version 3. All these options
remain report-only. Malformed selectors, incompatible flags, missing required
input/formats and unknown options fail before reading INPUT: exit 2, empty
stdout, text usage on stderr. V1/v2 retain exact schemas, defaults, bytes and
exit behavior; v2 deliberately continues to apply a kind/scope cross product.

V3 retains the same six envelope keys, operation, findings, status and error
contracts but sets `schemaVersion: 3`. Its policy has exactly three keys:

```json
{
  "rules": [{"kind": "loss", "scope": "server"}, {"kind": "secret", "scope": "document"}],
  "matchedRules": [{"kind": "secret", "scope": "document"}],
  "decision": "rejected"
}
```

A rule matches only if **one finding has both that rule's kind and scope** in
structured metadata. Rules are ORed, never crossed. The example rejects server
losses or document secrets; server secrets and document losses alone are accepted.
Document findings use `server: null`; a real server named `(document)` still has
server scope. `matchedRules` is the deduplicated matching subset of `rules` in
normalized order, without counts or finding indices. All findings, even excluded
kinds/scopes and unsupported errors, remain identical to `--json-v1`, in order.
Matching precedes coordinate masking; display labels are never parsed.

Invalid native input/read/processing failure takes precedence: exit 2, status
`error`, empty findings, normalized `rules`, `matchedRules: null`, decision
`not-evaluated`, masked nonempty error. No partial findings or evaluated-policy
claims. Valid unsupported projections exit 3 with status `unsupported`, complete
findings, the fixed unsupported error and independently evaluated matched rules
and decision. Otherwise nonempty matches exit 1 (`rejected`); empty matches exit
0 (`accepted`). Empty rules, no findings and empty server collections are valid.
Same-format emission rules and existing heuristic masking remain unchanged.
The bounded explicit-input, value-free, no-render/write/expansion/execution/network
contracts and limitations above apply. No per-server/path rules, thresholds or
baseline suppression are added.

## Reusable paired-rule artifacts (version 1)

`agentwrench mcp-config report --from FROM --to TO INPUT --policy-json-v3 --policy-file FILE`
loads only the explicitly named policy file. INPUT, FROM and TO remain required;
the artifact does not select a native file or conversion direction. Paths may
be relative to the current directory. There is no discovery, include, expansion,
remote fetch or policy output write. Only a regular UTF-8 file is accepted;
symlinks at the file itself, directories, devices and pipes are refused without
blocking. Parent directory symlinks have the same behavior as native input paths.

The file has exactly this shape (the empty rules array is also valid):

```json
{
  "schemaVersion": 1,
  "rules": [
    {"kind": "loss", "scope": "server"},
    {"kind": "secret", "scope": "document"}
  ]
}
```

Artifact `schemaVersion` must be the integer 1, excluding boolean/float/string
lookalikes. It is independent of the **result's schemaVersion 3**. The root has
exactly `schemaVersion` and `rules`. Each rule is an object with exactly `kind`
and `scope`, each a case-sensitive string from the v3 sets above. Unknown or
missing fields, nested member values, invalid pairs and unknown versions are
errors. JSON is strict: no comments, trailing commas, BOM, duplicate object keys
(including escaped-equivalent keys, even when values agree), non-finite constants
or trailing content. Files above **65,536 bytes** and arrays above **128 entries
before deduplication** are errors; exact limits are accepted. These bounds also
apply to JSON whitespace and duplicate pairs. Excessive decoder nesting is safely
refused, and the schema itself permits no containers beyond root/rules/rule.

Repeated pairs are allowed and collapse into the existing fixed kind-then-scope
order. Empty rules select nothing, preserving all diagnostics. Pairs match the
same individual finding metadata as inline v3; no kind/scope cross product and
no inference from the display server name. Complete native scoped findings,
including real `(document)` servers and excluded kinds/scopes, remain visible.

`--policy-file` requires `--policy-json-v3` and refuses **any** inline
`--fail-rule`, `--fail-on` or `--fail-scope`, even an equivalent selector. Mode
exclusivity and required operands are unchanged. These argument errors exit 2
with text usage on stderr, empty stdout and no reads. The file is not used by
convert/check or other report modes. Repeated `--policy-file` uses argparse's
last supplied path, without reading earlier paths.

After parsing arguments, the entire policy is read and validated before reading
or evaluating native INPUT. The result uses exactly the existing v3 envelope;
there is no added filename, raw content, file hash, value or source discriminator:

* Invalid/unreadable policy: exit 2, status `error`, empty `findings` and `rules`,
  `matchedRules: null`, `decision: "not-evaluated"`, safe nonempty `error`. No
  partially validated rules or findings are exposed.
* Valid policy and invalid native input or processing failure: same exit/error
  contract, retaining the full normalized effective `rules`.
* Valid policy and unsupported native conversion: exit 3, status `unsupported`,
  complete findings and independently evaluated rules/matches/decision. This
  takes precedence over policy acceptance or rejection.
* Supported native projection: exit 1/status `rejected` for nonempty matches,
  otherwise exit 0/status `accepted`. Empty policies and inputs are valid.

The file is an unauthenticated local selection artifact, not an audit of policy
intent or runtime compatibility. Existing heuristic detection/masking, native
finding granularity and stream limitations apply. There are no server/path
filters, thresholds or baselines. Existing inline v3, v1/v2, scoped report,
legacy JSON and human report outputs retain their contracts.

## Exact native selectors (result version 4, artifact version 2)

Use `report --from FROM --to TO INPUT --policy-json-v4 --policy-file FILE`.
All operands remain explicit. V4 requires a file and accepts **only artifact
version 2**; v3 continues accepting **only artifact version 1**. There is no
implicit upgrade, discovery or inline selector syntax. All six report JSON
modes are mutually exclusive. V4 refuses `--fail-on`, `--fail-scope` and
`--fail-rule`, including equivalent selections, before any reads. Missing
operands, file flag or incompatible flags produce exit 2, empty stdout and text
usage stderr. `--policy-file` otherwise keeps its regular-file/UTF-8/last-flag
behavior above.

Artifact v2 has exactly `schemaVersion` (integer 2, excluding boolean/float/string
lookalikes) and `rules`. Each rule requires `kind` and `scope` from the existing
sets, with only these optional members:

* `server`: an exact string identity, allowed only with `scope: "server"`.
  Omission selects all real servers. Empty string is a literal empty identity,
  never a wildcard. A document rule forbids this member, **including null**.
  A real server named `(document)` retains server scope; document findings have
  null server identity in results.
* `path`: an array of at most **64 segments**, each a literal string or integer
  index **0 through 2147483647**. Strings may be empty or contain Unicode,
  controls, periods, slashes and brackets. Booleans, floats, nulls, negative or
  larger indices and nested containers are invalid. Omission selects any path;
  **`[]` selects only the empty root coordinate**, not every path. Native readers
  currently emit nonempty paths; the root selector is valid and normally matches
  nothing.

```json
{
  "schemaVersion": 2,
  "rules": [
    {"kind": "loss", "scope": "server", "server": "worker", "path": ["vendor", "a.b"]},
    {"kind": "secret", "scope": "document", "path": ["metadata", 0, "API_TOKEN"]}
  ]
}
```

A rule matches only when **all its explicit selectors match one finding's
original structured native metadata**. Rules are ORed. Exact equality is
case-sensitive with no trimming, globbing, prefix matching, path splitting,
normalization, interpolation or environment expansion. `['vendor', 'a.b']` and
`['vendor', 'a', 'b']` differ; integer `0` and string `'0'` differ. Server paths
are relative to server objects; document paths are relative to the document
root. Display `field`/server labels are never parsed. These are diagnostic
locations, not executable edit coordinates. Existing granularity applies: a
loss on an array as a whole does not create findings at its elements.

The 65,536-byte UTF-8 and 128-entry limits apply before deduplication, including
whitespace and duplicates; exact limits are accepted. Strict JSON/duplicate-key,
root/rule unknown-field, version and type checks from artifact v1 remain.
Validate the **whole artifact before reading INPUT**. Empty rules select nothing.
Duplicate complete original rules collapse. Distinct overlapping rules remain;
omitted selectors and empty selectors remain distinct. Normalization orders by
kind, then scope as in v3, then omitted server before present server (Unicode
string order), then omitted path before present path. Paths use lexicographic
segment order: strings before integers, Unicode string order and numeric integer
order, with shorter prefixes first. Object members order `kind`, `scope`,
`server`, `path`, omitting absent selectors.

V4 retains exactly the six envelope keys, `operation: "report-policy"`, full
findings, and status/error/exit precedence, with `schemaVersion: 4`. Policy has
exactly `rules`, `matchedRules`, `decision`. Both arrays expose normalized rule
objects, including only explicit selectors; matches are the ordered subset.
Every finding, including nonselected warnings and unsupported errors, is
identical to `--json-v1` in order and content.

Matching, ordering and deduplication use **original unmasked** server/path
metadata. Only serialization masks known token signatures in selectors, just
as in findings. Different raw identities may therefore produce identical
looking rule/finding objects. Those masked records are **not deduplicated**;
a literal `[REDACTED]` selector is never a wildcard. Masked output cannot always
reconstruct the effective raw artifact. Ordinary names/keys remain visible;
no configuration values, raw artifact content or filenames are emitted.

Invalid/unreadable artifact: exit 2, empty rules/findings, null matchedRules,
not-evaluated decision and safe nonempty error. Valid artifact plus invalid
native input/processing: same, retaining all normalized masked rules. Neither
case exposes partial findings or evaluation. Valid unsupported conversion:
exit 3 with complete findings and independently evaluated decision, even if no
rule matches. Otherwise matches reject with exit 1; no matches accept with exit
0. Empty policies, empty native server collections and no-findings reports are
valid. Same-format reports retain existing secret-only diagnostic behavior.

V1/v2/v3 policy results, artifact v1, scoped report v1, legacy JSON and human
report contracts stay unchanged. No native writes/backups, rendering, execution,
network, discovery or expansion are added. Selection is still source-only and
uses heuristic diagnostics/masking; acceptance is no runtime or credential-audit
certification. No thresholds, baselines, authenticity or snapshot binding.
