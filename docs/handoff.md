# agentwrench handoff

When an agent (or a person) hands work over and says "tests pass", the next
person cannot tell whether that run happened against the code now in the
checkout. `agentwrench handoff` joins **in-toto command receipts** to a **Git
source-state fingerprint** and an explicit task description, writes one JSON
package, and later verifies that package against the current repository without
running any carried command. Use it when the receiver needs checkable evidence
of which source state a check ran against, and which claims are still only claims.

Which hand-off tool to use: `agentwrench export summary` writes a redacted
Markdown brief from a session log so the next agent can pick up the context;
`agentwrench handoff` packages Git state, in-toto command receipts and task
claims so the receiver can verify them later. They combine well: attach the
handoff JSON to the brief.

It is intentionally smaller than a session manager: no transcript discovery,
summaries, agent launching, hooks, or network access.

## Install

Requires Python 3.11 or newer and Git. This command needs the `handoff` extra,
which installs [in-toto](https://in-toto.io/) and Pydantic:

```bash
pip install 'agentwrench[handoff]'
agentwrench handoff --help
```

Without the extra, `agentwrench handoff --help` still lists the subcommands and
any other invocation prints a one-line install hint and exits 2.
`python -m agentwrench handoff ...` is equivalent to `agentwrench handoff ...`.
`record` needs macOS or Linux (POSIX process groups); `capture`, `verify`,
`inspect`, `diff` and `schema` do not.

## Quick start

Run the self-contained demo from the agentwrench repository root. It creates a
throwaway Git repository below `examples/handoff/demo-output/basic/`, records a
real Python check, captures a handoff, verifies it, then edits the source and
shows that `verify` reports drift:

```bash
python examples/handoff/demo.py
```

For your own checkout, write a task file. Everything in it is an unverified
claim and stays labelled as such:

```json
{
  "goal": "Cap retries at five",
  "claimed_done": ["Implemented the cap"],
  "unresolved": ["Decide whether jitter belongs in this change"],
  "artifacts": ["src/retry.py"],
  "verification_commands": [["python", "-m", "pytest", "-q"]]
}
```

Put outputs **outside the repository** or in a Git-ignored directory. Tracked or
non-ignored outputs are refused: writing one would change the very state the
package just captured.

```bash
agentwrench handoff record --repo /path/to/repo --output /path/to/evidence/check.link \
  --timeout 60 -- python -m pytest -q
agentwrench handoff capture --repo /path/to/repo --task task.json \
  --log /path/to/evidence/check.link --output /path/to/evidence/handoff.json
agentwrench handoff verify /path/to/evidence/handoff.json --repo /path/to/repo \
  --require-current-pass
agentwrench handoff inspect /path/to/evidence/handoff.json
```

`agentwrench handoff schema task` and `agentwrench handoff schema manifest`
print the JSON Schemas of the task file and the handoff package.

## Subcommands

| Subcommand | What it does |
| --- | --- |
| `record [--repo DIR] -o LINK [--name NAME] [--product PATH]... [--timeout S] [--signing-key PEM] [--dsse] [--force] -- CMD...` | Runs one command under in-toto and adds a Git source-state receipt to the Link. |
| `capture [--repo DIR] --task TASK -o OUT [--log LINK]... [--base REF] [--diff PATCH] [--force]` | Packages task claims, the source fingerprint, staged/unstaged diffs, observed artifacts and the given in-toto logs into one JSON. |
| `verify HANDOFF [--repo DIR] [--bundle-only] [--key KEY.json]... [--require-signed] [--require-current-pass]` | Checks package integrity, current Git/file state and each receipt. Never executes commands. |
| `inspect HANDOFF` | Validates the package and prints task claims, artifacts and receipt results without touching a repository. |
| `diff [--repo DIR] -o PATCH [--force]` | Exports the exact normalized HEAD-to-worktree binary diff that `capture --diff` accepts. |
| `schema [manifest\|task]` | Prints a JSON Schema (default: `manifest`). |

`--repo` defaults to the current directory. Existing traditional or DSSE in-toto
Link files can be imported with repeated `--log`; such logs stay **unbound**
unless they contain a valid source-state receipt written by `record`. Matching
their declared files is useful but does not show that unrecorded dependencies
were unchanged.

`capture --base REF` adds the native binary diff from that commit to HEAD.
Staged and unstaged diffs are always captured separately. To embed a local diff,
export it with `diff` (outside the repository or in an ignored directory) and
pass `--diff change.patch`; a patch whose bytes do not match the current
checkout is rejected.

`inspect` reports the repository as `not_checked`. Use `verify --repo ...` for
current-state evidence. The JSON contains diffs and fingerprints, not a
repository backup: recipients need the checkout and artifact files. The
generated verification command in the package keeps the sender's local paths as
hints that recipients replace with their own.

## Meaning of verification

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

## Output and exit codes

Operational subcommands print one JSON object on stdout; errors are a JSON
object on stderr (`{"error": ...}`). Help and `--version` are plain text.

| Exit | Meaning |
| --- | --- |
| 0 | Success: package written, check passed, or recorded command exited 0 |
| 1 | Drift or unmet policy in `verify`; nonzero recorded command or a missing `--product` in `record` |
| 2 | Invalid input, operational error, or recorded command timeout |
| 130 | Interrupted; the recorded command's process group was cleaned up |

Error messages do not echo input contents, unknown JSON keys, command arguments
or input paths. Filesystem errors include the OS `errno`.

## Signatures

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

## Scope and limitations

The source fingerprint covers HEAD, index paths/modes/object IDs, Git's binary
staged and unstaged patches, executable bits, and SHA-256 hashes of all tracked
and non-ignored untracked files. Final symlinks are fingerprinted as link text
and never followed. `record` rejects source symlinks because in-toto follows
them. Submodules, unresolved index conflicts, unborn repositories, and non-UTF-8
or non-portable paths are rejected explicitly.

Ignored dependencies and configuration, environment variables, toolchain state,
network services and files outside the repository are outside the source scope.
Task `artifacts` and receipt products may be ignored regular files; all of them,
including their absence, are observed at capture time and later changes fail
verification. No hash proves that a test is sufficient, that a log is honest, or
that a command had no other side effects. Run `capture` and `verify` against a
quiet working tree: optimistic double checks catch observed concurrent changes
but do not freeze the filesystem.

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
unchanged and must not be mistaken for a new invocation.

Git inspection disables external diff drivers, text conversion, filesystem
monitors, lazy fetching and configured clean/process filters (`--no-textconv`
alone still lets clean filters run). Patch prefixes, context and algorithm are
normalized so presentation settings do not cause false drift. Blob sizes of
HEAD, the index and a requested `--base` are checked before native diffs run,
so a large deleted historical blob can be rejected even when the worktree is
small.

## Stable formats

The package format (`schema_version: "1"`, source scope
`git-tracked-and-nonignored-v1`) and the source-state receipt stored in each
recorded Link's in-toto `environment` under the key `anchored_handoff` keep the
names used when this tool was the standalone `anchored-handoff` project, so
existing packages and receipts verify unchanged. The verification command that
`capture` writes into new packages is now `agentwrench handoff verify ...`.

## Working with other tools

[handoff/integrations.md](handoff/integrations.md) shows how to attach a handoff
package to a Portable Handoff capsule and how imported in-toto workflows map to
this command. A runnable version of that recipe is
[examples/handoff/portable_handoff_recipe.py](../examples/handoff/portable_handoff_recipe.py).
The in-toto fixtures used by the tests, and how they were generated, are
described in [tests/handoff/fixtures/README.md](../tests/handoff/fixtures/README.md).

## Related tools

- [in-toto](https://in-toto.io/) provides the Link format, hashing, signing and
  command execution this command builds on. Keep your in-toto layouts and
  `in-toto-verify` policies for supply-chain rules; this command adds the Git
  source-state binding and task claims, not a new attestation format.
- [Portable Handoff](https://github.com/legoambarish/portable-handoff),
  [AgentCarry](https://github.com/Jettlin927/agentcarry) and
  [Agents Handoff](https://github.com/Ryujin-Labs/agents-handoff) carry session
  context, Git facts and file hashes between agents. They do not consume in-toto
  command receipts or establish which full source state a check ran against;
  use this package as an evidence attachment alongside them.
- [Proofline](https://pypi.org/project/proofline/) records command runs, Git
  revision facts and signed run bundles; its verifier checks the stored bundle
  rather than whether it still applies to the current checkout.
