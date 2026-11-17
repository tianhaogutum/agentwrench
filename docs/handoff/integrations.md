# agentwrench handoff: composing with other handoff tools

`agentwrench handoff` supplies a machine-checkable evidence attachment for a
session handoff. Keep your existing tool's context extraction, decisions and
summaries; attach the handoff package and have the receiver verify it.
See [../handoff.md](../handoff.md) for the command reference.

## agentwrench export summary

`agentwrench export summary SESSION` writes a redacted Markdown brief from a
Claude Code, Codex or Cline session. Put the handoff JSON next to it and add one
line to the brief telling the receiver to run
`agentwrench handoff verify handoff.json --repo .` before relying on any
"tests pass" statement in the brief.

## Portable Handoff 0.1.0

Portable Handoff's finalizer deliberately downgrades a draft's verification
claim to a claim; keep that behavior. Attach the evidence package instead of
trying to promote a model-written claim to verified.
[examples/handoff/portable_handoff_recipe.py](../../examples/handoff/portable_handoff_recipe.py)
runs the whole workflow below against a throwaway repository: it records a
check, captures the attachment, finalizes and validates a capsule, and verifies
the attachment independently.

1. Record local checks with `agentwrench handoff record`, or keep your existing
   in-toto `.link` files. Store generated files in a Git-ignored directory (for
   example `.handoff/`) or outside the checkout.
2. Run `agentwrench handoff capture` with the task claims and those logs. The
   task JSON is intentionally simple: copy `task.goal`, pending or blocking
   items into `unresolved`, and relevant file paths into `artifacts`. This
   mapping is a semantic decision, not a claim that all capsule fields are
   interchangeable.
3. Add an ordinary evidence reference to the Portable Handoff draft:

   ```json
   {
     "evidence_id": "local-command-evidence",
     "kind": "agentwrench-handoff",
     "source": ".handoff/evidence/handoff.json",
     "summary": "Local in-toto receipts and Git state; re-run agentwrench handoff verify before relying on them",
     "provenance": "file",
     "trust": "claimed"
   }
   ```

   Do not put an evidence `digest` in the draft; the Portable Handoff finalizer
   rejects model-authored hashes. List the attachment in the draft's `files` if
   you also want its own deterministic file hashing.
4. Complete the usual `portable-handoff preflight`, `finalize` and `validate`
   workflow. On receipt, use its briefing to recover meaning, then independently
   run:

   ```bash
   agentwrench handoff verify .handoff/evidence/handoff.json --repo .
   ```

   Add `--require-current-pass` only when your workflow requires a passing
   invocation bound to the current source fingerprint. For author
   authentication add `--key KEY.json --require-signed` with an independently
   trusted public key.

`agentwrench handoff` does not rewrite capsule trust labels, install agent
hooks, alter host histories, or start another agent. A summary's claimed
completion stays separate from the verifier's file and invocation observations.

## AgentCarry and Agents Handoff

Use the handoff package as a file attachment or an ordinary referenced artifact.
No native plugin or adapter for their capsule schemas is provided.

## Existing in-toto workflows

Repeated `capture --log STEP.link` imports both traditional (Metablock) and DSSE
Link metadata. Keep your in-toto layouts and `in-toto-verify` policies:
`agentwrench handoff` does not replace threshold rules, signer authorization or
artifact rules. It reports a source-state binding only for the environment
extension written by `agentwrench handoff record` (key `anchored_handoff`).
Otherwise it reports declared-file matches and `source_binding: unbound`, even
when the signature is valid.

Record upstream logs with explicit repository-relative artifact paths. Directory
or OSTree resolver URIs, absolute paths, Git submodules and artifacts outside the
repository are not imported. An imported command is never run, and a key carried
inside a package is never accepted as a trust root.
