# Relocation selection manifest, version 1

Contract for `agentwrench mcp-config relocate --plan` / `--manifest` (see
[Reuse reviewed selections](../mcp-config.md#reuse-reviewed-selections)). All
inputs in examples are synthetic. A manifest is an editable selection artifact, not a signature or
authorization token. Review and protect it as well as the source config.

## Input and output

Generate a manifest by adding `--plan` to an explicit relocation:

```sh
agentwrench mcp-config relocate --format codex examples/mcp-config/relocation/source.toml \
  --from-root /old/project --to-root /new/project \
  --path '["worker","cwd"]' --path '["worker","args",1]' --plan > plan.json
agentwrench mcp-config relocate examples/mcp-config/relocation/source.toml --manifest plan.json --dry-run
agentwrench mcp-config relocate examples/mcp-config/relocation/source.toml --manifest plan.json -o relocated.toml
```

`--plan` validates the entire relocation, including native rendering, then exits
0 with one JSON object on stdout, empty stderr and no file writes. Shell
redirection, if used, creates the manifest. It cannot be combined with `--dry-run`,
`-o`, `--in-place` or `--manifest`. The exact keys are:

```json
{
  "schemaVersion": 1,
  "operation": "relocation-selection",
  "scope": "project",
  "format": "codex",
  "sourceSha256": "<64 lowercase hex characters: SHA-256 of exact source bytes>",
  "sourceRoot": "/old/project",
  "targetRoot": "/new/project",
  "paths": [["worker", "cwd"], ["worker", "args", 1]]
}
```

The digest covers the complete regular UTF-8 source file, including comments,
whitespace and CRLF bytes. It is neither a semantic digest nor a source filename;
an identical byte copy at another path is accepted. Paths preserve selection
order and literal server names. Argument indexes are zero-based integers; booleans
are invalid. At least one selection is required; duplicates are invalid.

`--manifest FILE` obtains format, roots and selectors exclusively from that file.
It first validates the strict versioned JSON envelope and compares the source
digest, then runs the same credential, selection, containment, native-render and
output checks as explicit relocation. CLI format/roots/paths overrides are
forbidden even when they agree. Only the input and output mode remain selectable.
Dry-run emits the existing relocation provenance on stdout and writes nothing;
apply emits native config to stdout or the requested output, and provenance to
stderr. Provenance keeps its existing schema and includes exact before/after
paths. `--in-place` creates the existing exclusive mode-0600 `.bak`.

## Negative cases and boundaries

- Any source byte change, including an argument inserted before a reviewed
  index or a comment-only edit, exits 2 with `source digest mismatch`, empty
  stdout, masked diagnostics and no output/backup/temp file. Restore the reviewed
  source or generate and review a new manifest; indexes are never remapped.
- Malformed JSON, duplicate keys, missing/extra keys, unsupported version or
  operation/scope/format, wrong types, invalid digest spelling, empty/duplicate/
  invalid selectors, unsafe roots, missing fields and noncontained values exit 2
  without partial output or writes. Manifest fields are never echoed on errors.
- Manifest files use the same regular UTF-8, no-symlink, 1 MiB and 64-container
  nesting limits as config inputs; nonfinite numbers are refused. JSONC is invalid.
  Generation also refuses manifests exceeding 1 MiB, including JSON formatting,
  so it never emits an artifact too large for the reader. Reduce selections and
  generate a new plan if this boundary is reached.
- A matching digest does not bypass credential refusal, output preflight,
  exclusive backup or atomic write stale-content checks. Existing output,
  symlink destination, missing parent and existing backup guards still apply.
- The digest binds selectors to the loaded snapshot; it does not authenticate
  the manifest or lock files against concurrent editors. In-place writes retain
  the existing last-check/rename race; coordinate writers. With a separate output,
  the validated loaded snapshot is used even if the source later changes.
- Paths remain lexical POSIX project paths with the existing supported-field and
  native-format restrictions. No client scope discovery, filesystem relocation,
  execution, network access or secret expansion occurs. The manifest contains
  supplied roots and selector names; store it accordingly.
