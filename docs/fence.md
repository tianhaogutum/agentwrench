# agentwrench fence

`agentwrench fence` is a scope gate for agent-generated TypeScript patches. Give
it an immutable baseline (a directory or a Git revision), a unified patch and
the exact symbols the agent was allowed to edit; it reports the accepted symbol
edits and every change outside those syntax nodes, including whitespace. It
never applies the patch to your checkout and never executes candidate code.

Which tool to use: `agentwrench patch` to apply or convert a patch,
`agentwrench gate` to check what an agent run did from its log, and
`agentwrench fence` to check that a unified diff to TypeScript only edits the
symbols you allowed.

## Install

Requires Python 3.11 or newer. Parsing uses Tree-sitter's TypeScript and TSX
grammars, installed with the `fence` extra:

```sh
pip install 'agentwrench[fence]'
agentwrench fence --help
```

Without the extra, `agentwrench fence --help` still prints the subcommands and
the install hint. Every command also works as `python -m agentwrench fence ...`.
Git is needed only for `--repo` baselines.

## Quick start

The example inputs live in `examples/fence/` in the agentwrench repository; run
the commands from the repository root (or adjust the paths).

```sh
agentwrench fence symbols --base-dir examples/fence/base --file calculator.ts   # tab-separated
# calculator.ts::add       function_declaration    1:8-3:2
# calculator.ts::multiply  function_declaration    5:8-7:2

agentwrench fence check --base-dir examples/fence/base \
  --patch examples/fence/allowed.patch --allow 'calculator.ts::add'
# PASS: 1 accepted symbol edit(s), 0 outside change(s)
#   allowed calculator.ts::add

agentwrench fence check --base-dir examples/fence/base \
  --patch examples/fence/outside.patch --allow-file examples/fence/allow.json
# REJECT: 1 accepted symbol edit(s), 2 outside change(s)
#   allowed calculator.ts::add
#   outside calculator.ts:6:12 outside_syntax * baseline
#   outside calculator.ts:6:12 outside_syntax ** candidate
# (exit 1)
```

`outside.patch` also edits `multiply`, so it is rejected. Add `--json` for a
machine-readable report.

## Select exact symbols

List selectors against the **baseline**, then copy the ones you want into the
task contract. Use repeated `--allow` options or `--allow-file FILE` (a JSON
array such as [`examples/fence/allow.json`](../examples/fence/allow.json)).
Without permissions, every change is outside scope. Empty patches are valid,
but every supplied selector is still validated. There are no globs and no
model interpretation of the contract.

Selectors have the form `path.ts::qualified.name`, for example
`src/api.ts::getUser`, `src/cache.ts::Cache.get`,
`src/options.ts::options.timeout` and `src/main.ts::outer.helper`.

- Variables include their initializer, so selecting an arrow-function variable works.
- Named classes, interfaces, type aliases, enums and enum initializers, import
  aliases, namespaces, methods, properties and object keys are supported.
- Computed keys use their exact source spelling (`Bag[Symbol.iterator]`,
  `settings[KEY]`); private members use `Bag.#work`.
- Anonymous default-export values use the selector `default`; their
  `export default` wrapper stays outside scope.
- Overloads, getter/setter pairs and duplicate lexical names get occurrence
  suffixes (`pipe#1`, `pipe#2`, `Store.value#1`). A bare ambiguous name is an error.
- Overlapping parent/child permissions are errors; the enclosing permission
  already covers its descendants.
- Destructuring requires selecting a named enclosing declaration.

Run `symbols` to see exact ranges and grammar node kinds.

## Use a Git baseline

Generate a standard Git unified patch relative to a known revision:

```sh
git -C /path/to/repo diff --no-ext-diff --no-textconv --binary HEAD > candidate.patch
agentwrench fence symbols --repo /path/to/repo --base HEAD --file src/api.ts
agentwrench fence check --repo /path/to/repo --base HEAD \
  --patch candidate.patch --allow 'src/api.ts::getUser' --json
```

The revision resolves once to an immutable Git tree; only blob reads follow, and
the index and working tree are untouched. Git lazy fetching and remote
protocols are disabled, so missing objects in a partial clone produce an error
rather than a download. Inherited `GIT_DIR`, `GIT_WORK_TREE`, object-directory
and namespace variables are ignored. Pass a commit SHA for reproducible runs.
File paths are relative to the repository root, even when `--repo` points into
a subdirectory. Capture staged plus unstaged changes with `git diff HEAD`;
untracked files are absent from Git's normal diff and must be reviewed
separately. The gate checks the patch you provide, not the completeness of the
agent's workspace.

`--patch -` reads the patch from stdin. A baseline directory (`--base-dir`) is
also supported; keep it stable while checking. Symlinks anywhere on a baseline
path are rejected. Standard `a/` and `b/` prefixes and bare paths are accepted.
Paths are repository-relative and must not contain traversal, control
characters, backslashes or `.git` components.

## Policy

By default **all outside text, including whitespace, must stay unchanged**. This
catches incidental whole-file formatting.

`--ignore-formatting` permits whitespace changes outside the selected nodes only
when the outside concrete syntax tree (CST) and comment-adjacent line breaks are
unchanged. Node structure, field roles, punctuation, comments and exact literal
spelling are retained, so automatic-semicolon-insertion changes and meaningful
spaces inside strings, template text and JSX text are still caught. Moving an
inline comment onto its own line is rejected (`outside_comment_boundary`)
because it can change JSDoc attachment or `@ts-expect-error` suppression even
though the CST is identical; ordinary comment indentation stays ignorable.
Quote changes and added or removed semicolons are conservatively treated as
syntax changes.

Allowed nodes are replaced with stable placeholders in both CST event streams;
a Myers diff of the remaining events locates outside changes. The selected node
must still exist at a structurally compatible position with the same selector;
removing or renaming it needs a new review (`symbol_disappeared`). Parent
wrappers stay outside scope: selecting an exported function does **not**
authorize changing its `export` keyword. Leading JSDoc, decorators outside a
parser node and surrounding indentation are outside that node's permission.
Hunk labels (`@@ ... @@ function allowed()`) never authorize anything: the
candidate is reconstructed from the baseline and parsed independently.

## Output and exit codes

| Code | Meaning |
| --- | --- |
| 0 | All provided changes accepted under the contract |
| 1 | Valid evidence contains outside changes |
| 2 | Invalid or unsupported evidence, or the boundary could not be established |

`--json` writes one JSON object to stdout, including errors
(`{"schemaVersion": 1, "ok": false, "error": {"code": ..., "message": ...}}`).
A successful analysis has `schemaVersion`, `ok`, `evidence`, `policy`,
`allowed`, `files`, `accepted`, `violations` and `ignoredFormatting`.

- `evidence.patchSha256` and each file's `baselineSha256`/`candidateSha256`
  bind the result to the exact patch and texts; Git mode also records the
  resolved tree (`evidence.baseline = {"kind": "git", "tree": ...}`).
- Each violation names the `file`, `side` (`baseline` or `candidate`), grammar
  `kind`, `range`, `category`, a `text` excerpt and a `reason`:
  `outside_syntax`, `outside_whitespace`, `outside_comment_boundary` or
  `unsupported_file` (a changed non-TypeScript file).
- Ranges are one-based lines and columns, end-exclusive, with UTF-16 indices.
- Diagnostics are capped at 100 per file; `outsideChanges` and `truncated`
  disclose the total. If a large syntax edit exceeds the detailed diff budget,
  the result stays rejected and reports the first incompatible events,
  `countIsLowerBound: true` and `diagnosticLimit`.
- An accepted edit in a rejected patch is still listed in `accepted`; the
  overall result remains rejected.

Error codes include `usage`, `invalid_patch`, `unsupported_patch`,
`patch_mismatch`, `unsafe_path`, `unsafe_baseline`, `invalid_baseline`,
`git_baseline`, `baseline_read`, `invalid_encoding`, `invalid_allow`,
`unknown_symbol`, `overlapping_allow`, `symbol_disappeared`, `syntax_error`,
`syntax_limit`, `parse_timeout`, `source_limit`, `patch_limit` and
`input_limit`. Operating-system failures use the errno name (for example
`ENOENT`).

## Library

```python
from agentwrench.fence import check_patch, git_baseline

report = check_patch(
    patch=unified_patch,
    allow=["src/api.ts::getUser"],
    read_base=git_baseline("/path/to/repo", "reviewed-base-sha"),
    ignore_formatting=False,
)
# report["ok"] is False for a scope rejection; invalid evidence raises FenceError
# (error.code, error.message, error.details).
```

`directory_baseline(root)` and any callable `read_base(file) -> str` also work;
an optional `identity` attribute on the reader is recorded as
`evidence.baseline`. `parse_source(text, file)` and `list_symbols(parsed)`
expose the selector listing.

## In a review workflow

Run the check as an extra local verification step in CI or in an agent
orchestrator that already manages task contracts. Keep the reviewed baseline SHA
and the allow-file under the reviewer's control:

```sh
agentwrench fence check --repo /path/to/repo --base REVIEWED_COMMIT_SHA \
  --patch /path/to/candidate.patch --allow-file /path/to/reviewed-scope.json --json
```

## Limits

- This proves a syntactic boundary only. An allowed edit can change exported
  behaviour, call arbitrary code or break business logic; run the project's
  tests and review the allowed changes.
- Only existing `.ts`, `.tsx`, `.mts` and `.cts` files are supported. File
  additions, deletions, renames, copies, mode changes, binaries, combined merge
  patches, removed or renamed selected symbols and parser error recovery are
  refused. Unapproved non-TypeScript modifications reject the contract.
- The pinned TypeScript grammar (tree-sitter-typescript 0.23.x) can lag the
  latest TypeScript compiler; unsupported syntax fails closed. There is no type
  checking and no alias or reference resolution across files.
- Patches must match the baseline at their declared old line positions: no
  offset or fuzzy matching, no line-ending conversion. Git patches with their
  original LF/CRLF content work; transport-modified patches may need
  regeneration. Use raw Git or unified diffs; email/format-patch prose and
  unknown extended headers are refused rather than skipped.
- End-of-file newline markers must be canonical; see
  [fence/eof-markers.md](fence/eof-markers.md) for why.
- Resource bounds: 8 MiB patch, 100 patch files, 10,000 hunks per file, 1 MiB
  per source, 16 MiB cumulative baseline text, 1,000 allow entries, 256 KiB
  allow-file, 200,000 named nodes and depth 512 per parse, a one-second parse
  budget, and a one-second / 10,000-edit budget for the detailed outside diff.
  Evidence and parse limits are errors; the detailed-diff limit keeps the known
  rejection with bounded diagnostics. No limit produces a pass.

## Differences from the Node original

`agentwrench fence` was previously a standalone Node.js tool built on the
`tree-sitter` Node binding and npm `diff` (jsdiff). The port keeps the report
format, field names, `schemaVersion`, reason and error codes, exit codes and
text output lines. On the original's own test inputs and the bundled examples
it produces identical reports, including UTF-16 ranges. Known differences:

- Command-line parsing uses argparse with `check` and `symbols` as subcommands.
  Options must follow their subcommand, options of the other subcommand are
  rejected, and argument-parser errors report the code `usage` (the original
  used Node's `ERR_PARSE_ARGS_*` codes). `--help` shows argparse help.
- The jsdiff pieces the gate relies on (`parsePatch`, exact `applyPatch`,
  `diffArrays`) are reimplemented in pure Python in
  `agentwrench/fence/jsdiff.py`.
- The detailed outside diff has the same one-second budget, but Python runs it
  more slowly, so very large syntax rewrites reach the bounded
  "first incompatible events" diagnostics (`countIsLowerBound`) earlier. The
  verdict is unchanged.
- Messages for operating-system and allow-file JSON errors are phrased by
  Python; their codes (`ENOENT`, `invalid_allow`, ...) are unchanged. Allow-file
  JSON rejects `NaN`/`Infinity`, as JavaScript's `JSON.parse` does.
- The parse time limit uses py-tree-sitter's `timeout_micros` (0.23-0.25) or a
  progress callback (0.26 and later).

## Related tools

The gate composes [Tree-sitter](https://tree-sitter.github.io/) with the
[tree-sitter-typescript](https://github.com/tree-sitter/tree-sitter-typescript)
grammars and follows [jsdiff](https://github.com/kpdecker/jsdiff)'s patch
parsing and Myers diff. Structural search and diff tools such as
[ast-grep](https://ast-grep.github.io/), [Difftastic](https://difftastic.wilfred.me.uk/)
and diffsitter find or show syntactic changes but have no per-symbol
permission contract for a supplied patch; task-contract tools such as SpecLock,
CodeTruss and Codify/Hyperfide track file zones or declared-symbol drift. This
gate covers the narrow remaining check: exact before/after CST boundaries for
the patch you supply, with an explicit whitespace policy.

The real-world test fixtures (TypeScript, RxJS, TanStack Query sources and a
Prettier-formatted patch) and their licenses are described in
`tests/fence/fixtures/upstream/SOURCES.md` and
`tests/fence/fixtures/formatter/SOURCES.md`.
