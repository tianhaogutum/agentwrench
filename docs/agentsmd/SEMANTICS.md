# agentwrench agentsmd: verified semantics and compatibility contract

This page records the upstream behavior that
[`agentwrench agentsmd`](../agentsmd.md) reproduces, and where it deliberately differs.

Checked 2026-11-11 against Codex main commit
`54b90f98a5724b688bf78793de5c21aca29ecec2`. Re-fetchable source links and
content digests are in [`upstream-sources.json`](upstream-sources.json). The Python
implementation is original; behavioral provenance is Apache-2.0 OpenAI Codex (see
[THIRD_PARTY.md](../../THIRD_PARTY.md)).

| Area | Verified upstream behavior | Local coverage |
| --- | --- | --- |
| Root | `agents_md_paths` calls `find_nearest_ancestor_with_markers`; `.git` default, empty list disables traversal, no root means cwd only | nearest/nested roots, Git marker file, disabled/outside roots |
| Walk | reverse lexical ancestors from nearest root to cwd, inclusive; no canonicalization for discovery | ordered nested chain; symlink files allowed |
| Selection | first regular file: override, regular, deduplicated fallbacks; empty override still selected | override/empty/fallback/shadowed tests |
| Fallbacks | config trims names; Linux ignores empty, `.`, `..`, slash and NUL names; backslash/colon are literal Linux basename characters | config normalization, explicit flag replacement; unsafe-path note |
| Project loading | one raw-byte remaining counter; truncate raw bytes before UTF-8 lossy decoding; charge only prefixes whose decoded text is not whitespace-only | exact cumulative cut, whitespace prefixes, invalid UTF-8 and character split |
| Zero budget | `load_project_instructions` returns without discovery | audit enumerates what would be dropped, loads no project bytes |
| Global | `CodexHomeUserInstructionsProvider` tries override then regular until nonempty; trims decoded text; independent of project byte limit | global priority, empty override fallback, trimming, independent budget |
| Assembly | `LoadedAgentsMd::legacy_text`: global-to-first-project marker; project-to-project two newlines; no added ending newline | exact bytes including CRLF, newline boundaries, decoding expansion |
| Warnings | current project loader warns on truncation/exhaustion; manager also warns when combined rendered instructions exceed budget | tool reports structured cuts and policy failure; historic silent truncation is not claimed for this revision |
| I/O | project errors propagate to caller; global provider can continue to another file and cache last good content | analytical file skipping plus diagnostics, gate failure, exact render refused |

A single selected environment is important: upstream outer environment accounting
also subtracts decoded entry lengths. That does not alter the inner single-local-
environment raw-byte calculation reproduced here. The global provider and manager
have refresh caches; this tool models a fresh read only.

`render` represents `LoadedAgentsMd::text()` for one trusted local project, one
Codex-home source and no extra host/thread instructions. It does not represent
`contextual_user_fragment` wrappers or any other developer/user prompt messages.
The simulator does not run Codex, execute commands from instruction files, or
consult credentials. It does not resolve project profiles, trust policies,
managed requirements or layered configuration.

Diagnostic additions are intentionally distinct from emulation: shadowed entries
are metadata-only; dropped files are read to annotate lost headings; empty files
and file errors receive explicit rows. In zero-budget mode the upstream would
not discover files at all; the audit still shows selected files as dropped.
When I/O fails, any text retained internally is analytical only and is never
available through successful `render`.

Input boundaries intentionally stricter than upstream: malformed TOML/types exit
2; invalid marker types are not replaced with defaults; root markers containing
paths are rejected (basenames only); integer budgets stop at 2^63-1. Fallback
path syntax is ignored with an explanatory note as upstream does. Bounded reader,
entry, marker, heading and graph limits are documented in
[Compatibility and boundaries](../agentsmd.md#compatibility-and-boundaries). Exceeding limits
never yields a successful exact result.

Heading/line annotations are our diagnostics, not Codex behavior: offsets are
zero-based raw byte positions of the first dropped byte; line numbers count LF
bytes and start at one. ATX headings outside fenced code are recognized, including
a partially retained nearest preceding heading; fully dropped headings begin at
or after the cut. Setext headings are outside this version's annotation grammar.

## Claude public-documentation mapping

Source: https://code.claude.com/docs/en/memory (retrieved 2026-11-11). This is an
independent docs-derived graph auditor; no proprietary code or prompt serializer
was inspected or reused. Relevant docs describe ancestor files ordered root to
cwd, local files after shared files, imports relative to the importer, escaped
spaces, code exclusion, and recursive imports through four hops.

The auditor visits both shared spellings (`CLAUDE.md`, then `.claude/CLAUDE.md`)
before `CLAUDE.local.md`. The exact relative precedence of the two shared
spellings is not established here. It deduplicates by canonical file identity via
resolved path (symlink aliases, not hard-link inode identity), with cycles checked
against the active recursion stack. Code spans/fences and HTML comments are
masked before import scanning. This is a bounded Markdown scanner, not a complete
CommonMark parser. Paths run through whitespace, with backslash-space support;
quoted, escaped-at, email and URL references are ignored.

Only imports within the nearest Git root/cwd and explicitly allowed roots can be
read; this intentionally differs from Claude's unrestricted absolute/parent/home
imports. Home shorthand, settings/excludes, managed or user memory, rules,
on-demand child files, AGENTS.md fallback and actual subagent expansion are not
simulated. Unsupported `render` and inappropriate Codex flags are explicit errors.

## Installed-binary differential: not performed

There is no differential check against an installed Codex binary. Codex's
`codex debug prompt-input` (`prompt_debug.rs`) runs startup/model/extension
initialization before building the prompt, even though it does not enter the
model turn loop, and those initialization paths were not verified to be
network-free. The semantics above therefore rest on the pinned source revision,
not on comparison with a running Codex.
