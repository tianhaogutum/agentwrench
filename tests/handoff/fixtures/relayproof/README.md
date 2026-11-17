# Relayproof fixtures

Output of the original Node.js relayproof 0.1.0 (MIT, same authors), used to
check that `agentwrench handoff` reads and writes the same bytes. Everything is
synthetic.

- `handoff.json`, `handoff.md`: `relayproof capture --task ... --evidence
  run-failed.json` on a throwaway Git repository with a commit, staged and
  unstaged edits, a staged deletion, untracked files, a `.env` file, a symlink,
  a file whose name looks like a credential and non-ASCII text.
- `run-failed.json`: `relayproof run --label "unit tests" -- sh -c ...` of a
  command that prints non-ASCII text and a `token=` line and exits 3.
- `js-expected.json`: inputs and the results of relayproof's own `redact`,
  `redactCommand`, `sensitivePath`, `safeName`, `clipped`, `canonical`,
  `digest`, `JSON.stringify(value, null, 2)`, `taskInput` and
  `Number.isFinite(Date.parse(...))`, computed with Node.js 24.

The pack's `handoff.md` differs from what `agentwrench handoff render` prints
only in its title and in the name of the verify command.
