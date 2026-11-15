# Relocation application receipt, version 1

Contract for `agentwrench mcp-config relocate --receipt` (see
[Archive an application receipt](../mcp-config.md#archive-an-application-receipt)).
All examples use synthetic files. Add `--receipt` to `relocate INPUT --manifest FILE`. Explicit roots/paths
and `--plan` cannot be used with this mode; a reviewed manifest is required.
Without `--receipt`, all existing provenance and error behavior stays unchanged.

## Exact envelope and streams

Every post-parse receipt outcome has exactly these keys:

```json
{
  "schemaVersion": 1,
  "operation": "relocation-application",
  "status": "planned",
  "manifestSha256": "<64 lowercase hex characters>",
  "sourceSha256": "<64 lowercase hex characters>",
  "outputSha256": "<64 lowercase hex characters>",
  "provenance": {"schemaVersion": 1, "operation": "relocate", "scope": "project", "format": "codex", "sourceRoot": "/old/project", "targetRoot": "/new/project", "relocations": [{"path": ["worker", "cwd"], "basis": "explicit-selection", "relativePath": "work", "before": "/old/project/work", "after": "/new/project/work"}]},
  "error": null
}
```

The nested `provenance` is the complete existing version-1 relocation record;
its roots and selected path values are deliberately disclosed on success.
`status` is `planned` for `--dry-run` and `applied` only after the native output
write returns successfully. Both exit 0. The receipt goes to stdout for dry-run,
with empty stderr. For applications it goes to stderr; stdout contains only the
native config when no output path was requested, otherwise stdout is empty.
New output (`-o`) and `--in-place` retain the exclusive output/backup and mode-0600
atomic-write rules. Receipts are stream output, not separately managed files;
shell redirection can archive them.

All three hashes use **exact bytes**, never normalized JSON or config semantics.
`manifestSha256` covers the complete loaded UTF-8 artifact including whitespace,
key order, non-ASCII encoding and any final newline. Reformatting/reordering an
otherwise identical valid manifest changes its identity, but not source/output
digests or provenance. CRLF is not normalized. `sourceSha256` covers the exact
loaded input bytes and matches the validated manifest. `outputSha256` covers the
exact rendered UTF-8 bytes written to the output or binary stdout; no newline is
added. Dry-run hashes those same prospective bytes without writing them.

## Errors and negative cases

Post-parse failures exit 2 and use the same envelope, with `status: "error"`.
`manifestSha256`, `sourceSha256`, `outputSha256` and `provenance` are **all null**;
`error` is a nonempty masked message without file values, paths or partial claims.
The stream follows the requested mode: dry-run errors on stdout, application
errors on stderr. Application errors before stdout output leave stdout empty.
Missing/unknown CLI arguments remain text parser usage on stderr, outside this
contract. Incompatible receipt flags are post-parse errors.

- Stale source (including comments/line endings), malformed/unsupported/duplicate/
  missing/extra manifest fields, unsafe selectors/roots, source credentials,
  unreadable input and manifest, or native rendering failure: no config output,
  no file write, no partial provenance and no applied receipt.
- Existing output/backup, symlink destination or missing parent: refusal before
  writing, including dry-run. Preview never creates a temporary file or backup.
- Failed temporary writes/fsync, exclusive creation or replacement: no applied
  receipt. Before publication the destination stays original/absent, temporary
  files are cleaned, and an already-created exclusive backup may remain intact.
  An error after atomic publication (e.g. directory fsync) can leave the complete
  output, never a partially rendered config. Inspect the filesystem after errors.
- Failed stdout writes/flushes do not emit an applied receipt. A pipe/stream can
  already contain a prefix; stdout cannot be rolled back. Success attests that
  the process wrote/flushed bytes, not that a downstream consumer persisted them.

The receipt is not signed, an authorization token, a lock, or a certificate of
runtime path availability. A loaded artifact/snapshot may change on disk later;
existing in-place stale checks and last-check/rename coordination limits remain.
Archive/protect the manifest and receipt together. No timestamp, input/output
filename, host identity, credentials, client execution or network access is added.
