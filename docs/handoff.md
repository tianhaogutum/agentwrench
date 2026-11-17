# agentwrench handoff

When an agent (or a person) hands work over and says "tests pass" or "the
parser is fixed", the next one cannot tell whether that still applies to the
code in front of it. `agentwrench handoff` writes a **handoff pack** from the Git
worktree and the task, optionally with records of real command runs, and later
tells the receiver whether the repository has changed since. Use it whenever
work moves between models, coding clients or sessions; switch to **in-toto
receipts** when the receiver must also authenticate who ran a check.

It is intentionally small: no transcript discovery, summaries, agent launching,
hooks or network access. Nothing it carries is ever executed by `verify`.

## Plain pack or in-toto receipts?

Both halves bind evidence to a fingerprint of the Git worktree and keep task
text labelled as unverified claims. They differ in what the receiver gets and
how strong the evidence is.

| | Plain pack (default) | In-toto receipts (optional) |
| --- | --- | --- |
| Install | nothing beyond agentwrench | `pip install 'agentwrench[handoff]'` (in-toto, Pydantic) |
| Write | `run` (optional) then `pack` | `record` (optional) then `capture` |
| Output | directory with `handoff.json` and a readable `handoff.md` brief | one JSON package |
| Command runs | run records: exit status and redacted, bounded stdout/stderr | standard in-toto Links with a source-state receipt; no output streams |
| Secrets | `.env*`, keys, credential files and symlinks are left out; task text, diffs, logs and commands are redacted | everything tracked or non-ignored is hashed; imported logs are kept byte for byte so signatures still verify |
| Authenticity | SHA-256 detects accidental edits only | signatures with independently trusted keys (`--key`, `--require-signed`), DSSE |
| Policy | `verify` exit 1 when anything is stale | `--require-current-pass`, `--require-signed` |

Use a **plain pack** for everyday handoffs to another agent: it is readable,
redacted and needs no extra packages. Use **in-toto receipts** when a check's
author must be authenticated, when CI must enforce "a passing run bound to this
exact source state", or when you already keep in-toto Links. They combine with
`agentwrench export summary`, which writes a redacted Markdown brief from a
session log: put the pack or the receipt package next to the brief.

## Install

Requires Python 3.11 or newer, Git, and macOS or Linux.

```bash
pip install agentwrench              # plain packs: pack, run, verify, render
pip install 'agentwrench[handoff]'   # adds in-toto receipts: record, capture, inspect, diff, schema
```

`agentwrench handoff --help` works without the extra. The in-toto subcommands
(and `verify` of an in-toto package) print a one-line install hint and exit 2
when it is missing. `python -m agentwrench handoff ...` is equivalent.

## Quick start: plain pack

The self-contained demo creates a synthetic repository below
`examples/handoff/demo-output/pack/`, runs a failing test for real, writes a pack,
edits the source to show that `verify` reports the pack as stale, then records
the fixed test in a new pack:

```bash
python examples/handoff/pack_demo.py
```

For your own repository, put outputs **outside the repository** or at a
**Git-ignored** path inside it (for example `.handoff/`, listed in
`.gitignore`). Outputs must be new paths whose parent directory exists.

```bash
# Optional: run a real check. Only `run` executes anything: the argv after --, without a shell.
agentwrench handoff run --repo /path/to/repo --output /tmp/handoff/test-01.json \
  --label "unit tests" -- python -m pytest -q

# Write the pack: a new directory with handoff.json and handoff.md.
agentwrench handoff pack --repo /path/to/repo --goal "Fix the import flow; hand over to the next agent" \
  --evidence /tmp/handoff/test-01.json --output /tmp/handoff/pack-01

# On the receiving side: is it still current? Exit 0 current, 1 stale.
agentwrench handoff verify /tmp/handoff/pack-01 --repo /path/to/repo

# Print the Markdown brief (or write it with --output FILE).
agentwrench handoff render /tmp/handoff/pack-01
```

Instead of `--goal`, `--task task.json` takes the full task. Lists that are
left out are recorded as `unknown`; an empty list means the user said "none".
Every string is stored as `source: user_input, verification: unverified`.

```json
{
  "goal": "Fix the import flow",
  "completed": ["Changed the parser"],
  "pending": ["Confirm the error message"],
  "blockers": [],
  "next": ["Review the diff and rerun the tests"]
}
```

Hand `handoff.md` and `handoff.json` to the next agent; it should run `verify`
before deciding which commands to rerun. `--evidence` is repeatable and accepts
only run records written by `run` (or by the original relayproof tool); a
generic terminal log has no reliable state binding and is not accepted.

## Quick start: in-toto receipts

Needs `pip install 'agentwrench[handoff]'`. The demo records a real Python check,
captures a package, verifies it, then edits the source and shows the drift:

```bash
python examples/handoff/demo.py
```

The task file of `capture` has its own format (`agentwrench handoff schema task`):

```json
{
  "goal": "Cap retries at five",
  "claimed_done": ["Implemented the cap"],
  "unresolved": ["Decide whether jitter belongs in this change"],
  "artifacts": ["src/retry.py"],
  "verification_commands": [["python", "-m", "pytest", "-q"]]
}
```

```bash
agentwrench handoff record --repo /path/to/repo --output /path/to/evidence/check.link \
  --timeout 60 -- python -m pytest -q
agentwrench handoff capture --repo /path/to/repo --task task.json \
  --log /path/to/evidence/check.link --output /path/to/evidence/handoff.json
agentwrench handoff verify /path/to/evidence/handoff.json --repo /path/to/repo \
  --require-current-pass
agentwrench handoff inspect /path/to/evidence/handoff.json
```

## Subcommands

`--repo` defaults to the current directory everywhere.

| Subcommand | Needs the extra | What it does |
| --- | --- | --- |
| `pack [--repo DIR] (--goal TEXT \| --task TASK.json) [--evidence RECORD.json]... -o DIR` | no | Writes a new pack directory (`0700`) with `handoff.json` and `handoff.md` (`0600`). |
| `run [--repo DIR] -o RECORD.json [--timeout-ms MS] [--label TEXT] -- CMD...` | no | Runs one command in the repository root without a shell and saves a run record. |
| `verify HANDOFF [--repo DIR]` | no for a pack | Compares a pack (directory or its `handoff.json`) with the repository now. |
| `verify HANDOFF [--repo DIR] [--bundle-only] [--key KEY.json]... [--require-signed] [--require-current-pass]` | yes | Checks an in-toto package: integrity, current Git/file state and each receipt. |
| `render PACK [-o FILE]` | no | Prints (or writes to a new file) the `handoff.md` of a pack, regenerated from `handoff.json`. |
| `record [--repo DIR] -o LINK [--name NAME] [--product PATH]... [--timeout S] [--signing-key PEM] [--dsse] [--force] -- CMD...` | yes | Runs one command under in-toto and adds a Git source-state receipt to the Link. |
| `capture [--repo DIR] --task TASK -o OUT [--log LINK]... [--base REF] [--diff PATCH] [--force]` | yes | Packages task claims, the source fingerprint, staged/unstaged diffs, observed artifacts and in-toto logs into one JSON. |
| `inspect HANDOFF` | yes | Validates an in-toto package and prints its claims and receipt results without touching a repository. |
| `diff [--repo DIR] -o PATCH [--force]` | yes | Exports the exact normalized HEAD-to-worktree binary diff that `capture --diff` accepts. |
| `schema [manifest\|task]` | yes | Prints the JSON Schema of the in-toto package or its task file (default: `manifest`). |

`verify` detects the format from the file: a directory or a JSON whose
`schema` is `relayproof-handoff/1` is a pack; a JSON with `schema_version` is
an in-toto package. `--bundle-only`, `--key`, `--require-signed` and
`--require-current-pass` apply to in-toto packages only.

## Output and exit codes

Every subcommand prints JSON on stdout (`render` prints Markdown) and reports
errors as one JSON object on stderr (`{"error": ...}`). Error messages do not
echo input contents, command arguments or input paths; filesystem errors carry
the OS error code.

| Subcommand | 0 | 1 | 2 | other |
| --- | --- | --- | --- | --- |
| `pack`, `render` | written | | error | 130 interrupted |
| `run` | command exited 0 | command's own exit code (also any other nonzero code) | error before the command started (no record written; stderr has the JSON error) | 124 timeout, 127 could not start, 128 killed by a signal |
| `verify` | current / `ok` | stale, drifted or policy not met | error, or extra missing for an in-toto package | 130 interrupted |
| `record` | command exited 0 | nonzero command or a missing `--product` | error or timeout | 130 interrupted |
| `capture`, `inspect`, `diff`, `schema` | done | | error or extra missing | 130 interrupted |

Help and `--version` are plain text.

## Plain packs in detail

### What a pack contains

`handoff.json` (schema `relayproof-handoff/1`) holds the task as unverified
claims, a snapshot of the worktree, redacted diffs of up to 100 changed tracked
files, sanitized copies of the run records with their `result` and `freshness`,
`testCoverage` (`unknown_not_run` without run records) and the limits below. Its
`integrity` field is the SHA-256 of everything else. It is not a repository
backup: untracked files appear only as names and hashes, and the repository root
is not exported. `handoff.md` presents the same content for a reader, with
every variable part in a code fence; it is not covered by `integrity` and can be
regenerated with `render`.

The snapshot (`relayproof-scope-v1`) records HEAD (null before the first
commit), the branch (null when detached), and for every tracked and non-ignored
untracked path: its Git status letters, its index entries (mode, object ID,
stage), whether it is a file or missing, its SHA-256 and executable bit. Its
`fingerprint` is the SHA-256 of a canonical JSON form. Left out, with the reason
in `omitted`: sensitive paths (`.env*`, `.git`, `.ssh`, `.aws`, `.npmrc`,
`.netrc`, `credentials*`, `secrets*`, `id_rsa*`, `*.pem`, `*.key`, ...),
symlinks (never followed), directories and submodules, other special files and
files over 8 MiB. A path name that looks like a credential is stored redacted
with a short hash ID. Git-ignored files are not listed. File modification times
and the binary `.git/index` are not part of the fingerprint, so a plain
`git status` never makes a pack stale.

### Meaning of the results

| Value | Meaning |
| --- | --- |
| `result: passed` | The record reports a normal exit with code 0. Not proof that the tests are sufficient or that an imported record is genuine. |
| `failed`, `timeout`, `error`, `signaled` | Nonzero exit, timeout, could not start, killed by a signal. |
| `matches_captured_scope` | HEAD, branch, file contents, executable bits and index entries in scope are as captured. No test was rerun. |
| `stale` | The scope differs from when the pack or the run was captured. `verify` lists up to 100 changes. |
| `mutated_during_run` | The repository changed while the command ran, so the result cannot be bound to any final state. |
| `unknown_not_run` | No run record was supplied; nothing is shown as passing. |

`verify` exits 0 only when the pack's snapshot **and** every attached run record
match the repository now (`status: matches_captured_scope`), otherwise 1
(`stale_or_unbound`). A real failing test can be perfectly current evidence, so
exit 0 does not mean the tests pass.

### Scope and safety

- Only `run` executes anything: exactly the argv after `--`, without a shell,
  in the repository root, with your environment and permissions. It is not a
  sandbox. The command runs in its own process group, which is killed at the
  timeout (default 30 s, `--timeout-ms` up to one hour); a process that starts
  its own session can outlive it. Up to 128 KiB of each output stream is read,
  redacted, cut back to the last complete line when truncated, and limited to
  64,000 characters.
- Redaction is best effort: private key blocks, common token formats (OpenAI,
  GitHub, AWS, Google), `Bearer` values, `Authorization` headers,
  `password=`/`token:`-style assignments, credentials in URLs, terminal escape
  sequences and control characters, and the value after flags such as
  `--api-key`. Review a pack before sharing it. Redacted commands may not be
  replayable.
- Run records imported with `--evidence` must have exactly the known fields and
  a matching ID; the result is recomputed from the termination fields, never
  taken from the file. Records are sanitized again on import; the pack keeps
  `originalRecordId` and `sanitizedOnImport`, not the removed text.
- Git runs with optional locks, fsmonitor, the untracked cache, the pager, lazy
  fetching and all configured clean/smudge/process filters disabled; diffs use
  `--no-ext-diff --no-textconv`. Because filters are off, status can differ from
  a plain `git status` in repositories that use them. File names and Git output
  must be valid UTF-8.
- The snapshot is taken twice and must agree, which catches observed concurrent
  writes but is not a filesystem freeze. Stop writers while packing. A change
  that is reverted during a run is not detected.
- Outputs are never overwritten; inside the repository they must be untracked
  and Git-ignored, so they cannot change the scope they describe.
- Ignored dependencies, environment variables, tool versions, external services
  and secrets are outside the scope and can change without making a pack stale.
  SHA-256 detects changes, not deliberate forgery: anyone can write a matching
  record. Bounds: 20,000 paths per repository, 8 MiB per hashed file, 16 MiB per
  pack read back, 1 MiB per run record.

## In-toto receipts in detail

### Meaning of verification

- `ok` means the package is intact, the checked repository and artifacts still
  match, and any requested policy passed. It does **not** mean the task is
  complete or that every command passed. Failed invocations are legitimate
  handoff evidence.
- `repository` is `match`, `drifted` (with a `differences` list: `head`,
  `source_file`, `index`, `staged_diff`, `unstaged_diff`, `artifact`) or
  `not_checked`.
- `reported_result` (`passed`, `failed`, `unknown`) comes only from the
  receipt's integer return value.
- `declared_files_match` compares the receipt's recorded materials and products
  with the files now on disk.
- `source_binding: same_source_state` means `record` saw the same full source
  fingerprint before and after the command, and that fingerprint matches now.
  Other values: `unbound`, `not_checked`, `different_source_state`,
  `inconsistent_commit`, `source_changed_during_command`.
- `current_pass` requires that binding, a zero return value, a nonempty argv,
  no missing requested products and matching declared file hashes. It requires
  a trusted signature only when `--key` is given.
- `task_completion` is always `not_assessed`. Claimed work, unresolved
  questions and suggested commands stay explicit input text.

`--bundle-only` checks the embedded data without reading a repository; it cannot
establish current applicability, so `--require-current-pass` then fails.
`inspect` reports the repository as `not_checked`. Existing traditional or DSSE
in-toto Link files can be imported with repeated `--log`; such logs stay
**unbound** unless they contain a source-state receipt written by `record`.
`capture --base REF` adds the native binary diff from that commit to HEAD; to
embed a local diff, export it with `diff` and pass `--diff change.patch` (a patch
whose bytes do not match the current checkout is rejected). The generated
verification command in the package keeps the sender's local paths as hints
that recipients replace with their own.

### Signatures

`record --signing-key key.pem` accepts an unencrypted PKCS8 PEM key (RSA, ECDSA
or Ed25519) and signs the standard in-toto Link, including the source-state
receipt in its environment. `--dsse` writes the upstream DSSE envelope instead.
To authenticate receipts, obtain the public key independently and pass its
in-toto JSON form:

```bash
agentwrench handoff verify handoff.json --repo /path/to/repo \
  --key trusted-public-key.json --require-signed --require-current-pass
```

A key inside an imported package is never treated as trusted. The package's own
SHA-256 checksum (`integrity_sha256`) detects corruption and accidental edits; it
is not an author signature. With trusted keys supplied, a receipt with an
invalid signature cannot be a current pass, and recomputing the package's
unkeyed checksum does not repair it.

### Scope and limitations

The source fingerprint (`git-tracked-and-nonignored-v1`) covers HEAD, index
paths/modes/object IDs, Git's binary staged and unstaged patches, executable
bits, and SHA-256 hashes of all tracked and non-ignored untracked files. Final
symlinks are fingerprinted as link text and never followed. `record` rejects
source symlinks because in-toto follows them. Submodules, unresolved index
conflicts, unborn repositories, and non-UTF-8 or non-portable paths are rejected
explicitly.

Task `artifacts` and receipt products may be ignored regular files; all of them,
including their absence, are observed at capture time and later changes fail
verification. No hash proves that a test is sufficient, that a log is honest, or
that a command had no other side effects. Run `capture` and `verify` against a
quiet working tree.

Bounds: 10,000 source paths and 10,000 observed artifact paths; 128 MiB per
file; 512 MiB total per source/artifact observation; 8 MiB per Git output or
patch; 4 MiB per log, 8 MiB combined, at most 100 logs; 24 MiB per package.
Git subprocesses time out after 20 seconds; recorded commands after 30 seconds
by default (`--timeout`, 1 to 300). Command output streams are not recorded,
avoiding in-toto's unbounded buffer; imported logs that contain streams remain
importable and are preserved byte for byte (no redaction) so their signatures
still verify.

`record` runs the command in its own POSIX process group and removes leftover
descendants after success, failure or timeout; processes that deliberately
start a new session are outside that guarantee. `--product PATH` declares an
expected regular-file output; a missing product prevents `current_pass` even
when the command exits 0. The destination is checked before the command starts.
Writes are private (`0600`), atomic, and never overwrite unless `--force` is
given; if a command fails to launch or times out, any previous receipt stays
unchanged.

Git inspection disables external diff drivers, text conversion, filesystem
monitors, lazy fetching and configured clean/process filters. Patch prefixes,
context and algorithm are normalized so presentation settings do not cause
false drift. Blob sizes of HEAD, the index and a requested `--base` are checked
before native diffs run.

## Two fingerprints, on purpose

Both halves fingerprint the Git worktree, but not the same thing, so there are
two implementations: `pack_scope.py` (`relayproof-scope-v1`) and `git.py`
(`git-tracked-and-nonignored-v1`).

- A pack is meant to be read and shared, so its scope **leaves out** secrets,
  symlinks and large files and still works before the first commit. A receipt
  must bind an in-toto Link to the full source state, so its scope **hashes
  everything** tracked or non-ignored, including symlink text and the binary
  staged/unstaged patches, and rejects what it cannot bind (unborn HEAD,
  submodules, conflicts).
- Both digests are stored in files that already exist (packs, run records,
  receipts in in-toto `environment`), and each is computed over its own
  canonical JSON form. Changing either definition would make every existing
  pack or receipt report a false drift.

What is shared: one `HandoffError` type, the private atomic no-overwrite file
writer, one output rule (outside the repository, or untracked and Git-ignored
inside it), the JavaScript-compatible JSON layer of `agentwrench cache`, and one
CLI with one `verify` entry point.

## Stable formats

Every file either predecessor wrote stays readable and keeps its names:

- Plain packs: directory with `handoff.json` (`relayproof-handoff/1`) and
  `handoff.md`; run records `relayproof-evidence/1` (`source:
  relayproof-local-execution`); snapshots `relayproof-scope-v1`; the `verify`
  report `relayproof-verification/1`. Field names, key order, number formatting,
  2-space indentation, trailing newline and the UTF-16 length limits are those of
  the Node.js original, so packs and records verify in either direction. New
  packs record `tool: {"name": "agentwrench", ...}`, and the first line of
  `handoff.md` and its verify hint name `agentwrench handoff`; every other line
  of the Markdown layout is unchanged.
- In-toto packages: `schema_version: "1"`, source scope
  `git-tracked-and-nonignored-v1`, and the source-state receipt under the
  in-toto `environment` key `anchored_handoff`, so existing packages and
  receipts verify unchanged.

## Coming from relayproof or anchored-handoff

| Before | Now |
| --- | --- |
| `relayproof run --repo R --out F [--timeout-ms MS] [--label L] -- CMD` | `agentwrench handoff run --repo R -o F [--timeout-ms MS] [--label L] -- CMD` |
| `relayproof capture --repo R (--goal T \| --task F) [--evidence F]... --out D` | `agentwrench handoff pack --repo R (--goal T \| --task F) [--evidence F]... -o D` |
| `relayproof verify --repo R --bundle D` | `agentwrench handoff verify D --repo R` |
| (none) | `agentwrench handoff render D` |
| `anchored-handoff capture/verify/record/inspect/diff/schema` | the same subcommands of `agentwrench handoff` |

Differences from the Node.js relayproof CLI: `verify` exits **1** (not 2) when
stale and errors exit **2** (not 1), matching the rest of agentwrench; errors
are JSON on stderr; outputs may also be Git-ignored paths inside the
repository; `--repo` defaults to the current directory; `--timeout-ms` takes a
decimal integer; run-record timestamps must use the ISO 8601 date-time format
(V8's legacy date formats are not accepted); Windows is not supported.

## In agentwrench scan

`agentwrench scan` looks for plain packs up to three directories below the
project (for example `.handoff/pack-01/`), without following symlinks. When it
finds one, it runs the same read-only check as `verify` against the project's
Git repository and adds section **4. Handoff packs**: a stale pack is a warning,
an invalid one an error, each with the `agentwrench handoff verify` command to
run. Without a pack nothing is run and the section is omitted. In-toto packages
need the extra and are not checked by scan.

## Working with other tools

[handoff/integrations.md](handoff/integrations.md) shows how to attach an
in-toto handoff package to a Portable Handoff capsule and how imported in-toto
workflows map to this command. A runnable version of that recipe is
[examples/handoff/portable_handoff_recipe.py](../examples/handoff/portable_handoff_recipe.py).
The in-toto fixtures used by the tests are described in
[tests/handoff/fixtures/README.md](../tests/handoff/fixtures/README.md); the
relayproof compatibility fixtures in
[tests/handoff/fixtures/relayproof/README.md](../tests/handoff/fixtures/relayproof/README.md).

## Related tools

- [in-toto](https://in-toto.io/) provides the Link format, hashing, signing and
  command execution the receipts build on. Keep your in-toto layouts and
  `in-toto-verify` policies for supply-chain rules; this command adds the Git
  source-state binding and task claims, not a new attestation format.
- [Portable Handoff](https://github.com/legoambarish/portable-handoff),
  [AgentCarry](https://github.com/Jettlin927/agentcarry) and
  [Agents Handoff](https://github.com/Ryujin-Labs/agents-handoff) carry session
  context, Git facts and file hashes between agents. They do not consume in-toto
  command receipts or establish which full source state a check ran against;
  use a handoff package as an evidence attachment alongside them.
- [Proofline](https://pypi.org/project/proofline/) records command runs, Git
  revision facts and signed run bundles; its verifier checks the stored bundle
  rather than whether it still applies to the current checkout.
- [Repomix](https://github.com/yamadashy/repomix) packs a whole repository for a
  model; a handoff pack carries state and evidence, not the code.
- [rewind](https://github.com/tianhaogutum/rewind) snapshots your files at every
  Codex turn so `rewind to N` can undo a turn; a separate Go tool.
