# agentwrench agentsmd

`agentwrench agentsmd` explains which instruction files a **trusted, local Linux
Codex workspace** loads, where its cumulative project byte budget cuts off, and
gates instruction loss in CI. It also audits the `CLAUDE.md` import graph described
by Claude Code's public documentation. This subcommand uses only the Python standard
library at runtime. It needs no model, credentials, agent session or network
access, and it never modifies instruction or configuration files.

## Install

```sh
pip install agentwrench          # or: pipx install agentwrench
uvx agentwrench agentsmd --help  # run without installing
```

Requires Python >= 3.11. `agentwrench agentsmd ...` and
`python -m agentwrench agentsmd ...` provide the same interface.

## Quickstart

The repository ships a small example workspace in `examples/agentsmd/`: `big/` is a
project tree (its root is marked by `.scope-root`) and `codex_home/` is a Codex home
whose `config.toml` sets `project_root_markers = [".scope-root"]`.

```sh
agentwrench agentsmd explain --cwd examples/agentsmd/big/pkg/api \
  --codex-home examples/agentsmd/codex_home
```

The example produces these rows (paths shortened):

| File | Source bytes | Loaded source bytes | Status |
| --- | ---: | ---: | --- |
| codex_home/AGENTS.md | 1,204 | 1,204 | loaded |
| big/AGENTS.md | 23,040 | 23,040 | loaded |
| big/pkg/AGENTS.md | 12,000 | 9,728 | partial |
| big/pkg/api/AGENTS.md | 900 | 0 | dropped-over-budget |

The package file is cut at **zero-based byte offset 9,728, line 233, under
`## Testing`**. The report also identifies fully dropped ATX headings.
Global instructions do **not** consume the pinned Codex implementation's project
budget, so the 1,204-byte global file does not move the cut. Output separators and lossy UTF-8 decoding can increase rendered
bytes beyond the project budget.

## Commands: explain, render, check

```sh
# Machine-readable report; does not dump instruction bodies.
agentwrench agentsmd explain --cwd ./my-repo/pkg --json

# Exact assembled instruction body, with no added final newline.
agentwrench agentsmd render --cwd ./my-repo/pkg > instructions.txt

# Fail on any loss or project usage above 80% (default).
agentwrench agentsmd check --cwd ./my-repo/pkg --max-usage 0.8

# Example workspace: exit 1, then exit 0.
agentwrench agentsmd check --cwd examples/agentsmd/big/pkg/api \
  --codex-home examples/agentsmd/codex_home
agentwrench agentsmd check --cwd examples/agentsmd/big/pkg/api \
  --codex-home examples/agentsmd/codex_home --max-bytes 65536 --max-usage 1.0
```

The first `check` prints `FAIL: project instructions truncated/dropped (32768/32768 B)`;
the second prints `PASS: 4 files; project usage 54.84%`.

`check` emits one line. Exit 0 means the policy passes; exit 1 means truncation,
dropped files, excess usage, or file/import errors. Invalid arguments/configuration
exit 2. `explain` exits 2 on file/import errors but still supplies the report;
`render` refuses to produce a misleading exact result after any file errors.
A truncated `render` succeeds, because that truncated body is the intended output.
Run `check` separately to enforce a policy. Reports contain paths and headings;
`render` deliberately reveals the selected instruction text.

## Discovery and configuration

- Global home is `--codex-home`, then `$CODEX_HOME`, then `~/.codex`. Read only
  its `config.toml` and instruction candidates. The project root is the nearest
  ancestor with a marker (`.git` by default); a Git worktree's `.git` file counts.
  Without a marker, inspect cwd only. Do not scan descendants.
- Read top-level `project_doc_max_bytes`, `project_doc_fallback_filenames`, and
  `project_root_markers` from that config. Flags replace those values.
  `--fallback RULES.md TEAM.md` sets ordered alternatives; `--fallback` clears
  them. `--root-marker pyproject.toml --root-marker .git` replaces markers;
  `--no-root-markers` disables upward discovery. `--max-bytes 0` disables
  project instruction loading while retaining global instructions.
- Per project directory, choose `AGENTS.override.md`, else `AGENTS.md`, else the
  first existing fallback. An empty chosen override **still shadows** regular
  instructions. Skip whitespace-only selected content without charging budget.
- Globally, choose the first **nonempty** override/regular file, trim its outside
  whitespace, and do not apply the project budget. No global fallback filenames.
- Project content is truncated as raw bytes, decoded with UTF-8 replacement,
  preserved without trimming, and joined with `\n\n`. Global-to-project joining
  uses `\n\n--- project-doc ---\n\n`. Separators do not consume budget.

## Report JSON

`explain --json` has `schema_version: 1`, ordered `entries`, `truncations`, `diagnostics`,
`usage`, and `rendered_bytes`. `size` is raw file length; `bytes_loaded` is accepted
source bytes before decoding/global trimming; `text_bytes` is UTF-8 output size.
`cumulative_bytes` includes global source bytes; `project_cumulative_bytes` and
`project_bytes_used` count only budget-charged project bytes. Shadowed entries
have zero loaded bytes. Additional audit statuses are `shadowed-by-precedence`
and `unreadable`. Every truncation gives its file, raw byte offset, LF-based
one-indexed line, nearest preceding ATX heading, fully dropped ATX headings, and
whether it splits a valid UTF-8 character. Headings in fenced code are excluded.

## Compare configuration impact across working directories

`impact` compares Codex instruction discovery and byte-budget cuts for each
explicit working directory under two configurations. Each side reads the same
three supported top-level keys from its home directory's `config.toml`, plus its
global instruction candidates. Use the same home on both sides to compare budget
overrides without editing any settings:

```sh
agentwrench agentsmd impact \
  --before-home examples/agentsmd/codex_home \
  --after-home examples/agentsmd/codex_home --after-max-bytes 65536 \
  --cwd examples/agentsmd/big --cwd examples/agentsmd/big/pkg \
  --cwd examples/agentsmd/big/pkg/api --json

# Compare fallback names, root markers, budgets and global instructions from
# two real Codex-home directory layouts. Relative paths use the process cwd.
agentwrench agentsmd impact --before-home ./config-before --after-home ./config-after \
  --cwd ./my-repo --cwd ./my-repo/pkg
```

The example workspace reports 2 of 3 directories changed: the root's files are
unchanged, the package cut at byte 9,728 disappears, and the API file changes from
`dropped-over-budget` (cut byte 0) to `loaded` (900 bytes). Both sides inspect the
current local files; this is a configuration comparison, not a historical file
diff. `--before-max-bytes` and `--after-max-bytes` replace each side's configured
budget. Missing `config.toml` uses the usual defaults; homes are explicit and do
not default to the user's environment. There is no Claude impact mode.

Impact JSON has its own `schema_version: 1`, the pinned `semantics`, effective
`before`/`after` home/settings, a `summary`, and ordered `directories`. Each
directory has metadata snapshots and `changes` keyed by lexical `(scope, path)`:
`added`, `removed`, or `changed`, with before/after states (`null` for absence).
States contain size, status, loaded source/output bytes, zero-based discovery
`order`, and a `cut` with byte offset, line, and UTF-8-split flag, or `null` for
no cut. **Neither JSON nor human impact output contains instruction bodies or
heading text.** It does contain paths, config filenames and diagnostic metadata.
Snapshots also include roots, budgets, project/rendered byte totals and errors.
`changed`/`changed_cwd_count` count file metadata or root changes; increasing a
budget alone does not mark a directory whose files remain unchanged.

By default, exit 0 means the comparison completed, even with changes or budget
loss. Add `--fail-on-loss` for a baseline-relative CI gate:

```sh
agentwrench agentsmd impact --before-home ./config-before --after-home ./config-after \
  --cwd ./my-repo --cwd ./my-repo/pkg --fail-on-loss --json
```

The gate's contract is per working directory:

- Identical assembled UTF-8 instruction bytes always pass, even if discovery,
  file selection, global whitespace or cut metadata differ. Bodies are compared
  in memory and never included in impact output.
- Otherwise, compare each `(scope, path)` independently. Project identities use
  lexical absolute paths; global identities use home-relative filenames so moving
  between homes does not itself lose a file. Retention is loaded source bytes
  for project files and trimmed output bytes for global files.
- Newly lost bytes for a file are the larger of its retention decrease and its
  increase in budget-excluded bytes, floored at zero. Budget-excluded bytes are
  `size - cut.byte_offset` for a file with a cut, zero otherwise. A
  whitespace-only prefix inside the raw cut is not excluded, even though it
  consumes no budget and retains zero bytes. This also catches
  newly selected files that are truncated. Removed or newly shadowed baseline
  files lose their previously retained bytes. Gains in other files/directories
  cannot cancel loss. Unchanged existing cuts and improved cuts pass.
- A newly lost file previously retained bytes and now retains none, or is newly
  fully dropped by the budget. Already dropped files are not newly lost. An
  effective content change with no retention loss passes; use snapshot `diff`
  to gate all effective changes instead.

For example, a project file `" \n tail"` with budgets 3 then 1 has
zero retained bytes on both sides, but its excluded bytes rise from 4 to 6.
If the global body also changes from `A` to `B`, the gate reports **2 newly lost
bytes, 0 newly lost files** per cwd and exits 1. The reverse comparison passes;
with equal assembled bodies it passes in either direction. This uses the same
raw-prefix accounting as saved v2 `diff --fail-on-loss`. The default impact
report and analysis-only exit behavior are unchanged; invalid inputs and
inspection errors still exit 2 under the rules below.

With this flag, JSON additionally contains `loss_policy` with `name: no-new-loss`,
`passed`, `regressed_cwd_count`, `newly_lost_bytes`, and `newly_lost_files`. Each
directory has `loss` with `effective_changed`, byte/file counts and `regressions`
listing `scope`, `path`, `newly_lost_bytes` and `newly_lost_file`. Global regression
paths are home-relative. Summary counts sum directory results, so an ancestor
file affecting several cwds counts once per cwd. Human output includes PASS/FAIL
and per-directory loss counts without bodies or headings.

The opt-in gate exits **1 only for new loss**, 0 otherwise. Instruction
file errors retain both snapshots, continue other directories, and exit 2 (even
if both configurations have the same errors or another directory regresses).
An errored directory's `loss` and the overall `loss_policy.passed` are null;
summary loss counts describe only successfully inspected directories. Invalid
configuration, a missing working directory or a hard discovery limit exits 2
with no partial report.
Repeat `--cwd` 1–64 times; normalized lexical duplicates are inspected once in
input order. Existing per-inspection resource limits apply independently to
each side/directory. Reads are sequential, with no atomic filesystem snapshot.
The default metadata comparison cannot identify same-size content edits with
identical metadata. The opt-in policy compares the assembled bodies in memory,
but gates retention loss rather than every content edit; it is not a historical diff.

## Save discovery snapshots and gate effective changes

`snapshot` records a single Codex working directory as deterministic JSON for
later comparison. It reads once and exits; no watcher or agent process runs.
It accepts the same Codex discovery/configuration flags as `explain` and always
outputs JSON. Use separate output files to retain the baseline:

```sh
agentwrench agentsmd snapshot --cwd ./my-repo/pkg --codex-home ./ci-home > before.json
# After editing instructions or configuration:
agentwrench agentsmd snapshot --cwd ./my-repo/pkg --codex-home ./ci-home > after.json
agentwrench agentsmd diff before.json after.json --json
```

Artifact format identifiers keep the `agentsmd-scope.` prefix (`agentsmd-scope.snapshot`,
`agentsmd-scope.manifest`, `agentsmd-scope.diff`, `agentsmd-scope.manifest-diff`)
so saved artifacts stay compatible.

Snapshot format `agentsmd-scope.snapshot`, `schema_version: 1`, includes the
pinned semantics, effective settings, relative cwd, `root_found`, ordered
`entries`, and `effective`. Project paths are relative to the discovered root
(cwd if there is no root); global paths are relative to Codex home, in a separate
`scope` namespace. Identical trees in different checkout/home locations produce
identical snapshots. No timestamps, absolute host paths, bodies, or headings are
stored. This is lexical path normalization, not symlink canonicalization.

Each entry has a full raw-file `sha256`, size, discovery order, load status,
`selected`, loaded source/output byte counts, and `hash_error` (normally null).
Project selection includes empty and over-budget winners; an empty global
candidate is unselected because global discovery continues. Regular shadowed
candidates, including a global regular file hidden by an override, are also
fingerprinted. Only snapshot mode opens these optional audit files. Unavailable
shadow metadata may be omitted; a failed optional hash has null `sha256` and an
error category, and does not invalidate the effective result. Per-file/reader
limits still apply, including a shared 64 MiB accounting limit for selected and
optional shadow reads. Selected-file/discovery errors exit 2 with no snapshot.

`effective.sha256` hashes the exact UTF-8 assembled instruction body, including
lossy decoding, global trimming, budget cuts and separators; `rendered_bytes`
records its length. `diff` compares saved artifacts without consulting source
files or configuration. Its JSON format `agentsmd-scope.diff` version 1 contains
`effective_changed`, before/after effective fingerprints, `context_changes`
(cwd, root presence, settings), and added/removed/changed entry states keyed by
`(scope, path)`. Human output lists changed paths without content.

**Diff exits 1 only when the effective body changes**, 0 otherwise. For example,
with `--max-bytes 6`, `AGENTS.override.md=ABCDEFtail` supplies `ABCDEF` and shadows
`AGENTS.md`. Editing the shadowed file or changing `tail` to `gone` changes file
hashes and appears in `changes`, but exits 0. Changing `ABCDEF` to `UVWXYZ` exits 1.
Replacing a selected file with another containing the same rendered text, changing
only trimmed global whitespace, or increasing an unused budget also exits 0.
Unchanged truncation is not a diff failure; use `check` for an absolute retention
policy or opt-in v2 `diff --fail-on-loss` below for baseline-relative loss.

Malformed/missing snapshots, unknown format/version/semantics, duplicate JSON
keys or entry identities, and invalid field types exit 2 without a partial diff.
Both snapshots must use this pinned semantics. Artifacts must be trusted: hashes
are assertions, not signatures, and bodies are unavailable to verify a tampered
artifact. File hashes can reveal guesses about short instruction text. Reads are
sequential, not an atomic filesystem snapshot. No Claude snapshot mode is provided.

## Capture a portable multi-cwd manifest

Opt in with `snapshot --manifest --base-dir DIR --codex-home HOME` and **1–64
explicit `--cwd` arguments**. The base must be an existing directory containing
all supplied cwds lexically. Relative CLI paths use the process cwd. Impact's
normalization and first-seen deduplication apply; the 64-argument limit is checked
before deduplication. Ordinary single-cwd snapshot output remains unchanged;
multiple cwd arguments require manifest mode. Manifest mode requires an explicit
home and base; `--base-dir` requires `--manifest`.

```sh
agentwrench agentsmd snapshot --manifest --base-dir ./my-repo --codex-home ./ci-home \
  --cwd ./my-repo --cwd ./my-repo/pkg --cwd ./my-repo/pkg/api > before.json
# After editing instructions or configuration, repeat capture into after.json:
agentwrench agentsmd diff before.json after.json --json
```

Format `agentsmd-scope.manifest`, `schema_version: 1`, contains `agent: codex`,
the pinned `semantics`, and ordered `directories`. Each row is `{cwd, snapshot}`:
`cwd` is a unique normalized path relative to the explicit base (`.` for the base
itself), and `snapshot` is the existing version-1 single-cwd artifact. Cwd
identities do not depend on discovery roots or the set/order of other cwds.
Separate repositories or directories without roots remain distinct. Neither base
nor home absolute paths are stored; equivalent relocated trees/homes with the
same relative layout and cwd order produce identical JSON. Discovery flags apply
to every cwd; config is read once. Captures are sequential and use the existing
per-cwd read limits. A manifest artifact must fit the existing **16 MiB** artifact
read limit; larger captures fail rather than producing an unreadable baseline.

Manifest diff consumes saved artifacts only. Its format is
`agentsmd-scope.manifest-diff`, version 1, with `effective_changed`, a `summary`
(`before_cwd_count`, `after_cwd_count`, `changed_cwd_count`, `added_cwd_count`,
`removed_cwd_count`), and `directories`. **Only effective changes or membership
changes are listed**. Each row has `cwd`, `kind` (`changed`, `added`, `removed`),
`before_effective` and `after_effective` (null for an absent side). Rows follow
baseline order then newly added cwds. `changed_cwd_count` counts all listed rows,
including membership changes. Reordering unchanged cwds does not fail. Shadowed,
clipped-tail, settings and discovery changes producing the same body fingerprint
are omitted. Added/removed cwd identities always fail, even for empty bodies.
Exit **1** means at least one listed cwd; **0** means none. Human output lists
those same directories, without file metadata noise, bodies or headings.

For example, capture root/pkg/api with project budget 6, `root/AGENTS.md=ROOT`,
`pkg/AGENTS.override.md=ABtail`, and a shadowed `pkg/AGENTS.md`. Editing `tail`
or the shadowed file gives no diff rows (exit 0); changing `AB` to `XY` lists
`pkg` and `pkg/api` only (exit 1). Removing `pkg/api` from the manifest lists it
as removed and exits 1 even if its effective body matched another cwd.

Missing/outside-base directories, missing explicit home/base/cwds, invalid config
or flags, selected-file errors, unsupported/malformed artifacts (including nested
snapshots), duplicate JSON keys or cwd identities, and mixed single/manifest
comparisons exit **2**, with empty stdout and no partial successful capture/diff.
All cwds are validated before inspection. Trusted-artifact and lexical-symlink
limitations of single-cwd snapshots also apply here.

## Retain exact cuts in v2 artifacts

Opt in with `snapshot --snapshot-version 2`, for either a single cwd or a
manifest. The default remains **1**. V2 preserves the same body-free fields and
adds `cut` to **every** snapshot entry: null when no project truncation occurred,
or `{byte_offset, line, splits_utf8_character}`. Offset is the zero-based source
byte boundary, line is one plus the count of LF bytes before that boundary, and
the boolean indicates a cut through an otherwise valid UTF-8 character. There
are no heading strings. Global and shadowed entries always have null cuts, even
when the same lexical file is also loaded as a truncated project instruction.

Cuts describe raw source prefixes, including whitespace-only prefixes that
consume no budget and fully dropped nonempty files at offset 0, line 1. Thus a
cut offset can exceed `bytes_loaded` for a `skipped-empty` entry. Empty files
have no cut, including at zero budget. For example, `A\n€Z` under budget 3 has
`cut: {byte_offset: 3, line: 2, splits_utf8_character: true}`.

V2 validation checks status and retention before any diff or loss verdict, including
nested manifest snapshots and equal effective fingerprints. For each selected
project entry, remaining budget is the maximum minus preceding project
`bytes_loaded` (skipped-empty prefixes charge zero):

- At zero remaining budget, status is `dropped-over-budget`, with both retained
  counts zero. An empty file still has null `cut`.
- With positive budget and no cut, status is `loaded` with
  `bytes_loaded == size > 0`, or `skipped-empty` with both retained counts zero.
  Uncut `partial` and positive-budget `dropped-over-budget` are invalid.
- A cut consumes the remaining source prefix when `partial`; `skipped-empty`
  retains zero source/text bytes. Loaded/partial project `text_bytes` is between
  `bytes_loaded` and three times that count: project whitespace is preserved and
  UTF-8 replacement can expand bytes.
- Globals are loaded, skipped-empty or shadowed, with null cuts. Loaded globals
  retain the full source size and positive trimmed text bytes, at most three
  times the source size; trimming can reduce text below the source size.
  Skipped-empty and shadowed entries retain zero source/text bytes. Optional
  shadow hash failures remain valid.
- At most **one global entry** may be selected (`status: loaded`) in each v2
  snapshot. The pinned loader returns the first nonempty global, so two loaded
  globals are invalid even when their byte counts and rendered total agree.
  An empty override may still fall back to the regular global; a loaded override
  may retain an optional shadow audit row for the regular file.
- At most **one project entry per lexical directory** may be selected. Discovery
  chooses the first regular override/regular/fallback candidate before loading,
  so empty, whitespace-only and over-budget winners also exclude lower-priority
  candidates. Unlike an empty global override, an empty project override still
  blocks fallback. Selections in distinct directories and a global plus a project
  with the same relative path remain valid; optional shadow audit rows do not
  count as selections.
- Every selected project entry's parent must be on the **lexical root-to-cwd
  chain**, inclusive. With no discovery root, only cwd is searched (authentic
  captures store cwd as `.`). Child, sibling and other unsearched directories
  are invalid even with unchanged hashes and byte counts, including empty and
  over-budget winners. Validation uses saved coordinates without resolving
  symlinks or reading source files; distinct lexical aliases remain distinct.
- Selected project directories must also appear in **root-to-cwd discovery
  order**. They may skip directories without selected files, including root or
  cwd itself. A descendant preceding an ancestor is invalid even with identical
  bodies and unchanged hashes, sizes and effective fingerprint. Empty and
  over-budget winners count; optional shadow audit rows do not affect this order.
- Each selected project **basename** must be `AGENTS.override.md`, `AGENTS.md`,
  or a basename in saved `settings.fallback`. An unconfigured filename is invalid
  even with an authentic parent, order, hashes and effective fingerprint. This
  includes empty and over-budget winners. Configured custom names, including
  Linux colon and backslash basenames, remain valid. Capture still trims and
  deduplicates fallback settings. Validation uses exact saved candidates; it
  does not infer file existence or precedence from absent optional shadow audits.

V2 also checks `effective.rendered_bytes` against selected entry `text_bytes`.
Sum the nonempty project text lengths and add **2 bytes** (`\n\n`) between
project bodies. Add the selected global text length and **23 bytes**
(`\n\n--- project-doc ---\n\n`) when both global and project text are present.
Empty, skipped, dropped and shadowed rows add no text or separators. Use decoded
`text_bytes`, which already includes UTF-8 replacement expansion and global
trimming, rather than raw source sizes or charged prefix bytes.

These are metadata consistency checks using only the artifact. They cannot
verify bodies, actual whitespace, cut lines/UTF-8 flags, or hash authenticity.
Violations of these v2 entry, selection or total invariants in either input exit **2 with
empty stdout** in human and JSON modes, including `--fail-on-loss` and nested
manifests. Validation precedes `--cwd-map` projection and effective-equality
suppression, including self-comparisons. Authentic v1 captures remain compatible;
v1 validation is unchanged.

V2 manifests use outer `schema_version: 2` and exclusively v2 nested snapshots;
v1 manifests exclusively contain v1 snapshots. Membership, relative identities,
size bounds and effective-only directory reporting are unchanged. `diff` accepts
v1/v1, v2/v2 and v1/v2 pairs of the same artifact kind. Default diff output remains
version 1. Single-cwd v2/v2 JSON entry states include cuts; cross-version diffs
compare the common v1 fields, omitting cuts on **both** sides because v1 did not
record them. Upgrading the schema alone produces no file-change rows. Default manifest
diffs continue to ignore cut metadata and list only effective/membership changes.
All comparisons work after deleting sources; cut-only changes exit 0, effective
edits or manifest membership changes exit 1. This enables historical cut review,
with the opt-in historical retention-loss policy described below.

Unknown versions, inconsistent nested versions, extra/missing cut keys, invalid
field types, nonpositive lines, impossible offsets, non-null global/shadow cuts,
and missing cuts for truncated selected project entries exit 2 with empty stdout.
Cut offsets must equal the remaining project budget and be strictly below source
size; line is at most offset + 1. At offset zero, line must be 1 and the split flag
false. Captures reuse the loader's cut metadata without extra source reads.

```sh
agentwrench agentsmd snapshot --snapshot-version 2 --manifest --base-dir ./my-repo \
  --codex-home ./ci-home --cwd ./my-repo --cwd ./my-repo/pkg > cuts.json
agentwrench agentsmd diff before.json cuts.json --json
```

## Inspect historical manifest file changes

Use `diff before.json after.json --include-file-changes [--json]` to inspect
saved v1/v2 **manifests** in detail. This opt-in report uses
`agentsmd-scope.manifest-diff`, `schema_version: 2`. It preserves the default
`effective_changed`, `summary` and effective/membership-only `directories` and
adds `file_changes`. Without the flag, the version-1 report remains unchanged.
The flag requires two manifests; single-cwd or mixed artifacts and unsupported
flag values exit 2 with empty stdout.

Each `file_changes` row has `cwd`, `kind` (`changed`, `added`, `removed`),
`before_effective`, `after_effective`, `context_changes`, and `changes`. It is
included when a cwd has any file metadata, context, effective fingerprint or
membership change. Unchanged cwds and membership reorderings are omitted. Rows
follow baseline order then additions. File changes reuse single-cwd diff states,
keyed by `(scope, path)` and ordered by baseline discovery then additions;
selected and shadowed entries are both included. Context changes compare the
nested snapshot's `cwd`, `root_found` and `settings` (the nested cwd may differ
from its manifest identity). Added/removed cwds list every entry on the available
side with null opposite states, all three context fields with null opposite
values, and null opposite effective fingerprints. Empty membership changes still
produce a row, even with no file entries.

V2/v2 states include cuts; matched v1/v2 cwds compare common fields and omit cuts
on both sides. A schema-only upgrade creates no detail rows. Added/removed cwds
retain all fields available in their own version, including v2 cuts. Human output
adds per-cwd file paths and context field names. Neither form contains bodies,
headings or absolute host paths. For example, changing the budget from 3 to 1
for a whitespace-only truncated prefix ` \n tail` reports a cut from byte 3,
line 2 to byte 1, line 1 while retaining exit 0 and an empty `directories` list.

All data comes from saved artifacts, so deleting the source trees has no effect.
Validation of both complete artifacts precedes output; malformed cuts or nested
artifacts exit 2 without partial stdout. **The exit policy remains unchanged**:
0 for metadata/context/cut-only changes, 1 for any effective fingerprint or
membership change. This detail supports historical retention analysis; use the separate opt-in
policy below to gate new loss. Artifact trust, pinned semantics and resource bounds apply.

```sh
agentwrench agentsmd diff before.json after.json --include-file-changes --json
```

## Compare explicitly relocated working directories

For two saved **v2 manifests**, supply `diff --cwd-map FILE`. The file is a
UTF-8 JSON object of exact old base-relative cwd identities to exact new ones:

```json
{"pkg": "services/api", "legacy/tool": "tools/cli"}
```

```sh
agentwrench agentsmd diff before.json after.json --cwd-map moves.json --json
agentwrench agentsmd diff before.json after.json --cwd-map moves.json \
  --include-file-changes --fail-on-loss --json
```

The map has at most 64 pairs; `{}` is valid. Keys and values use the saved
normalized relative spelling (`.` is allowed), without absolute paths, `..`,
redundant separators or leading `./`. Every endpoint must exist on its respective
side, and destinations must be unique. Duplicate JSON keys, malformed/missing
maps, maps over the 16 MiB reader limit, v1/single/mixed/unsupported artifacts,
and invalid projections exit **2 with empty stdout** before any verdict.

Pairing uses exact map lookup, once: moves can swap names and are not chained.
Unmapped cwd membership retains its existing added/removed semantics; descendant
cwd membership is never inferred from a parent move. Removed, unmapped cwds
retain their original file coordinates, including shared ancestor files.
Comparison projects saved
project file coordinates through the explicitly mapped subtrees. Ancestors
outside those subtrees stay fixed; global paths remain home-relative. For example,
`pkg/AGENTS.md` becomes `services/api/AGENTS.md`, while root `AGENTS.md` stays
fixed. Consistent overlapping moves such as `pkg -> new` and `pkg/api -> new/api`
are accepted. Conflicting overlaps, projected cwd/file collisions (including with
unmapped baseline files), ambiguous original-file projections, changed mapped
`root_found`, root mismatches and paths outside the target root are errors.
The nested root-relative cwd must be a suffix of its base-relative identity
(or `.`); a discovery root above the saved base cannot be located unambiguously
from this format and is rejected with a nonempty map. Capture with a base at or
above discovery roots when planning relocation review. A map cannot assert
arbitrary file renames or otherwise rebase roots.

Mapped reports use `agentsmd-scope.manifest-diff`, **schema_version: 4**. Existing
`directories`, `summary`, optional `file_changes` and optional `losses` use the
after-side comparison coordinates. `relocations` lists every supplied pair in
baseline order, even for unchanged bodies, with `before_cwd`, `after_cwd`,
`before_snapshot_cwd`, `after_snapshot_cwd` and `project_paths`. The latter lists
`{before, after}` for each baseline project entry whose root-relative path was
projected. Both original cwd coordinates remain visible even when discovery
roots move and the root-relative file paths stay identical. Human output also
lists each relocation. No map means exactly the existing report and behavior.

An unchanged moved cwd exits **0**; a moved effective edit exits **1** under the
default gate. `--fail-on-loss` retains the existing byte-loss accounting and
equal-effective suppression, using the projected coordinates, and requires equal
membership **after** mapping. Mixed mapped/unmapped additions and removals remain
visible under the default gate and still invalidate the loss baseline. Mapping
is a user assertion, never inferred or authenticated. Diff reads only the two
artifacts and map, works after deleting source trees, and rewrites neither input.

## Gate historical retention loss

`diff before.json after.json --fail-on-loss [--json]` opts into a baseline-relative
**no-new-loss** gate. Inputs must be two v2 snapshots or two v2 manifests with
identical cwd membership (ordering may differ, or use the explicit v2 relocation
map above). For each paired snapshot,
`cwd` and `root_found` must match so project-relative file identities retain the
same coordinate system after any explicit projection. Settings may change.
V1/mixed versions, mixed artifact
kinds, membership/coordinate changes, malformed artifacts and invalid flags exit
**2 with empty stdout**, even if effective fingerprints match. Missing v1 cuts
are unknown, never inferred from loaded-byte counts. No source files are read.

For each cwd with different effective fingerprints, compare files by saved
`(scope, path)`. Retention is project `bytes_loaded` or global `text_bytes`;
absence/shadowed entries retain zero. Budget-excluded bytes are
`size - cut.byte_offset` for a project entry with a cut, zero otherwise. Using
its raw boundary avoids counting an accepted whitespace-only prefix as excluded,
even when it charged zero bytes. Newly lost bytes per file are the maximum of
retention decrease, excluded-byte increase, and zero. A newly lost file had
positive retention and now none, or is newly fully dropped (previously no
excluded bytes, now positive excluded bytes and `dropped-over-budget`). Gains
in other files/cwds do not cancel loss. Equal effective fingerprints suppress
all loss for that cwd. Unchanged/improved cuts pass; effective content edits
without new loss also pass. These are byte-retention comparisons, not a measure
of semantic information lost; larger replacements may increase excluded bytes.

The opt-in report uses `agentsmd-scope.diff` **version 2** for snapshots and
`agentsmd-scope.manifest-diff` **version 3** for manifests (**4** with
`--cwd-map`). Existing effective
fingerprints, `effective_changed`, `changes`/`directories` and manifest `summary`
retain their meanings. Both formats add `loss_policy` with `name: no-new-loss`,
`passed`, `regressed_cwd_count`, `newly_lost_bytes`, and `newly_lost_files`.
Single snapshots add `loss`; manifests add `losses`, one `{cwd, loss}` per cwd
in baseline order, including unchanged cwds. Each loss contains
`effective_changed`, byte/file totals and ordered `regressions` with `scope`,
`path`, `newly_lost_bytes`, `newly_lost_file`. Regressions follow baseline file
order then additions. Ancestor losses count once per affected cwd. Manifest
`--include-file-changes` composes with this policy and adds its usual detail.
Human output includes PASS/FAIL and per-file byte loss without bodies, headings
or absolute host paths.

With `--fail-on-loss`, exit **1 only for new loss**, 0 otherwise. Without it,
existing report versions and effective/membership exit behavior stay unchanged.
Artifacts remain trusted unsigned assertions; absent bodies cannot verify hashes
or metadata, and lexical layout changes are not file renames.

```sh
agentwrench agentsmd snapshot --snapshot-version 2 --cwd ./my-repo --codex-home ./ci-home > before.json
# Repeat capture after instruction/configuration edits into after.json.
agentwrench agentsmd diff before.json after.json --fail-on-loss --json
```

## Claude import audit

```sh
agentwrench agentsmd explain --agent claude --cwd ./my-repo --json
agentwrench agentsmd check --agent claude --cwd ./my-repo
# Explicitly allow imports from an additional directory:
agentwrench agentsmd explain --agent claude --cwd ./my-repo --allow-import-root ../shared-guidance
```

This is a **docs-derived dependency graph**, not a prediction of Claude's exact
prompt or subagent behavior. Enumerate ancestor `CLAUDE.md`, `.claude/CLAUDE.md`,
and `CLAUDE.local.md` from filesystem root to cwd; local comes last at each level.
Walk recursive `@path` imports relative to the importing file, through four hops.
Imports inside code spans, fenced code, and HTML comments are ignored. Quoted
paths are literals; backslash-escaped spaces work. Repeated canonical files are
reported `already-seen`; an active recursion revisit is a cycle error.

Imports may read only within the nearest Git root (or cwd if none) and explicit
`--allow-import-root` directories, after resolving symlinks. Absolute local imports
work within those roots. Missing files, cycles, depth overflow, and boundary
violations fail the audit. `~` imports require replacing the spelling with an
explicit local path; there is no shell, variable, glob, or URL expansion. No
scripts are executed. `render --agent claude` is explicitly unsupported.

## Development

From the repository root:

```sh
uv run --extra test pytest tests/agentsmd
python tests/agentsmd/compare_budgets.py
```

The budget comparison uses the same 35,940 project source bytes at budgets 32,768
and 65,536: the first loses 3,172 bytes, one whole file and three headings; the
second loses none. It changes an explicit analysis setting, not files or Codex
settings. This measures instruction retention, not tokens, speed, or model
quality. Its results are asserted by the test suite; no timing benchmarks are
claimed.

## Compatibility and boundaries

Codex semantics are pinned to [openai/codex
54b90f98](https://github.com/openai/codex/tree/54b90f98a5724b688bf78793de5c21aca29ecec2),
not inferred from a running session. Current upstream already has budget warnings;
older Codex versions truncated silently. This independent tool provides
structured accounting and a CI gate. It is not endorsed by OpenAI or Anthropic.
[agentsmd/SEMANTICS.md](agentsmd/SEMANTICS.md) records source locations, verified
behavior, and deliberate differences;
[agentsmd/upstream-sources.json](agentsmd/upstream-sources.json) lists the pinned
upstream source links and content digests.

- Assumes a fresh, trusted, single local Linux environment. Does not model host/
  thread instructions, cached refreshes, sandbox permissions, untrusted sessions,
  multiple environments, managed/system config, project `.codex/config.toml`, or
  profiles. It renders the instruction **body**, not the surrounding full prompt.
- Only the three documented top-level config keys affect analysis. Root markers
  must be basenames; path-shaped markers and invalid marker types are rejected
  rather than imitating upstream's permissive marker/error fallback. Invalid
  fallback paths are ignored with a note, matching Linux upstream discovery.
- On I/O errors, continue analytical enumeration and fail the gate. Upstream
  may instead abort a whole environment's project load; no exact render is offered
  for this case. Files can change during inspection; no atomic snapshot is claimed.
- To explain otherwise-unread content, inspect dropped project files under bounded
  read limits. Maximums: 16 MiB/file, 64 MiB per reader, 512 instruction entries/file
  reads, 128 fallback/marker entries, 4,096 headings per truncated file, 2,048
  Claude import edges. Exceeding them is an explicit error. Budget integers are
  restricted to `[0, 2^63-1]`. Files must be regular; symlink loops/FIFOs fail.
- Heading annotations recognize ATX headings, not Setext headings or a full
  CommonMark AST. Claude ordering within the two shared-file locations and import
  parsing are a documented static approximation. No system/user Claude memory,
  `.claude/rules`, on-demand descendants, excludes, settings, or AGENTS.md fallback
  is modeled. The graph never claims an exact Claude prompt.
- No editing/splitting rules, token estimation, publishing, rule synchronization,
  or external workflow execution. Results have not been differentially checked
  against an installed Codex binary, because its debug startup could not be
  guaranteed network-free.

Apache-2.0. See [LICENSE](../LICENSE), [NOTICE](../NOTICE), and
[THIRD_PARTY.md](../THIRD_PARTY.md) for attribution and source boundaries.
