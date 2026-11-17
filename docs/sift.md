# agentwrench sift

`agentwrench sift` turns one large JSON tool result or test log into token-bounded,
source-locatable evidence before it enters an agent's context. Every retained excerpt
is copied byte-for-byte from the input and carries its original byte range plus a JSON
Pointer or line range. Every omitted byte range is listed, and the whole envelope is
measured with a real tokenizer. Use it when a test run, API response or MCP tool
result is too big to paste whole, but the agent still needs the failures and must be
able to tell what it did not see.

Which one to use: `agentwrench slim` shrinks the MCP tool *definitions* that are sent
on every turn; `agentwrench sift` shrinks one large tool *result* or test log into
bounded evidence.

## Install

Requires Python 3.11 or newer. This command needs the `sift` extra, which installs
[shrinkfit](https://github.com/timesler/shrinkfit), [tiktoken](https://github.com/openai/tiktoken),
json-source-map, jsonpointer and strip-ansi:

```sh
pip install 'agentwrench[sift]'
agentwrench sift --help
```

Without the extra, `agentwrench sift --help` still lists the subcommands, and any other
invocation prints a one-line install hint and exits 2.
`python -m agentwrench sift ...` is equivalent to `agentwrench sift ...`.

> **Network: the tokenizer vocabulary.** `sift` sends no input data anywhere, and
> shrinkfit's hosted scoring is explicitly disabled. However, the **first** use of a
> tiktoken encoding (`cl100k_base` by default, or `o200k_base`) downloads that
> encoding's public vocabulary file from OpenAI's public blob storage and caches it.
> To run fully offline, set `TIKTOKEN_CACHE_DIR` to a directory that already contains
> the vocabulary: run any `sift` command once on a connected machine with the same
> `TIKTOKEN_CACHE_DIR`, then copy that directory. Every other `agentwrench` command is
> offline and does not use tiktoken.

## Quick start

From the agentwrench repository root (the fixtures are real pytest output from an
intentionally failing suite):

```sh
agentwrench sift summarize tests/sift/fixtures/pytest-color.log --format log --budget 1600 --stats > log-evidence.json
agentwrench sift summarize tests/sift/fixtures/pytest-result.json --format json --budget 1000 --keep /exit_code > result-evidence.json
agentwrench sift verify tests/sift/fixtures/pytest-result.json --summary result-evidence.json --budget 1000
agentwrench sift recover tests/sift/fixtures/pytest-result.json --summary result-evidence.json --bytes 0:10
```

Saved envelopes for these two fixtures are in
[`examples/sift/json-evidence.json`](../examples/sift/json-evidence.json) and
[`examples/sift/log-evidence.json`](../examples/sift/log-evidence.json).

## Usage

```text
agentwrench sift summarize INPUT --format {json,log} --budget N
    [--encoding {cl100k_base,o200k_base}] [--keep POINTER]... [--keep-pattern PATTERN]...
    [--keep-line START:END]... [--context N] [--rules RULES.json] [--stats]
agentwrench sift recover INPUT --summary EVIDENCE.json --bytes START:END
agentwrench sift verify INPUT --summary EVIDENCE.json [--budget N]
```

`INPUT` may be `-` for stdin (but not together with `--rules -` or `--summary -`).
`summarize` writes only compact UTF-8 JSON to stdout, with no trailing newline: that
exact string is what was measured. `--stats` writes `{"tokens": N, "budget": N}` to
stderr.

Exit status: **0** success, **2** invalid input or rules, **3** impossible budget
(`{"error":"budget_exceeded","message":...,"needed":N}` on stderr, nothing on stdout),
**4** I/O failure (including a closed output pipe). Once the arguments are parsed,
every error is one JSON object on stderr (`invalid_input`, `budget_exceeded` or
`io_error`); argparse usage errors exit 2 with a usage message. No background
processes are started.

## The evidence envelope

The output is an explicit evidence envelope with schema id `tool-sift/v1` (the id is
kept from the tool's earlier standalone name so saved envelopes stay valid):

| Field | Meaning |
| --- | --- |
| `schema`, `format`, `encoding` | `tool-sift/v1`, `json` or `log`, and the tokenizer used for the budget |
| `source` | `sha256` and `bytes` of the original input; recovery refuses a changed file |
| `offsets`, `notice` | fixed strings stating the offset units and that absent fields are unknown |
| `truncated` | `true` when any byte range was omitted |
| `structure` | JSON only: `pointer`, `type` and container `size` for every container around a shown value |
| `blocks` | retained excerpts: `bytes` `[start, end)`, exact `text`, `complete`, plus `pointer` (JSON) or `lines` `[first, last]` (log) |
| `omitted` | the exact complementary byte intervals |

Byte ranges are zero-based, half-open UTF-8 offsets; log line ranges are one-based and
inclusive. `complete: true` marks a whole original JSON value or complete log lines.
An oversized value or line may instead appear as exact excerpts marked
`complete: false`; JSON excerpts then include `node_bytes` for the full value's
boundary. Such a fragment is raw source evidence, not a parseable JSON value.

**An absent field in a summary is unknown.** It does not mean the tool omitted that
field. The envelope is a new schema: never validate it as, or substitute it for, the
upstream tool's schema-bearing MCP `structuredContent`.

The budget covers the whole serialized envelope, including pointers, structure,
omission ranges and the notice, in the selected encoding. It does not cover chat
framing or a provider's own accounting.

## Keep rules

Required values are exact and atomic: a required container keeps its entire original
subtree. If required evidence and metadata cannot fit, `summarize` fails with exit 3
(library: `BudgetError`, whose `needed` is the measured requirement). Missing or
noncanonical required pointers fail explicitly.

- `--keep /exit_code`: an exact RFC 6901 pointer; escape `~` as `~0` and `/` as `~1`.
- `--keep-pattern '/reports/*/outcome'`: pins every matching original value. Patterns
  are anchored and `*` matches exactly one path segment. At most 64 patterns; a
  pattern matching nothing (including a wildcard over an empty array) fails. Use an
  exact `--keep` to address a literal `*` key.
- `--keep-line 20:24`: log only; requires all five original lines.
- `--context N` (0..10, default 2): lines kept around detected errors in logs.

`--rules rules.json` accepts `keep`, `keep_patterns`, `keep_lines`, `priority_keys`
and `context_lines`; unknown or duplicate keys fail. CLI keep rules are added to the
file's rules:

```json
{"keep":["/exit_code"],"priority_keys":["error","failures","longrepr"],"context_lines":2}
```

Without rules, selection prefers error and failure records, exit codes, MCP `isError`,
test summaries, nearby context, and head/tail lines. Failed test records share space
round-robin so one verbose traceback cannot take every slot. ANSI colors are removed
only for classification; retained text and offsets refer to the untouched input.
Shrinkfit's schema-aware and first/last array samples are used only as preferences:
unchanged unique values are joined back to the source, and sampled output indices are
never trusted as original indices. Classification is heuristic, so use keep rules for
evidence that must be present.

## Recover and verify

`recover` prints raw original bytes for any retained or omitted range after checking
the input's SHA-256 and length against the envelope. Ranges must end on UTF-8
character boundaries. It does not re-run a tool and keeps no hidden copy: keep the
original file yourself.

`verify` checks the source digest, every retained byte, pointers or line ranges,
complete/partial boundaries, container types and sizes, and the exact omission
partition, then prints `{"verified": true, "tokens": N, "budget": N}`. It does not
call shrinkfit. The token count is of the supplied serialization including its
whitespace, so a pretty-printed envelope can exceed a budget the compact one met.

## Library API

```python
from pathlib import Path
from agentwrench.sift import Rules, summarize, verify

raw = Path("tests/sift/fixtures/pytest-result.json").read_bytes()
result = summarize(raw, format="json", budget=1200, rules=Rules(keep=("/exit_code",)))
assert result.tokens <= 1200
verify(raw, result.text, budget=1200)
agent_observation = result.text
```

`agentwrench.sift` also exports `recover`, `Summary`, `BudgetError` and `InputError`.
They are loaded on first use, so importing the package itself does not require the
`sift` extra.

[`examples/sift/agent_adapter.py`](../examples/sift/agent_adapter.py) shows the
intended integration: it archives the captured JSON bytes (refusing to overwrite an
existing file) *before* creating the lossy observation, so an impossible budget never
forces a backend call to be repeated. It pins MCP `isError` when present and never
synthesizes it when absent.

```sh
mkdir -p .tmp
python examples/sift/agent_adapter.py tests/sift/fixtures/pytest-result.json \
    --archive .tmp/capture-001.json --budget 1200 > .tmp/observation-001.json
```

## Limits

- Inputs are UTF-8, at most 8 MiB. A leading UTF-8 BOM on JSON is accepted and
  reported as omitted bytes.
- JSON is strict: duplicate keys, non-finite constants, floating-point overflow and
  unpaired surrogates are rejected. At most 50,000 nodes, depth 96, and 4,096
  characters per encoded pointer.
- Logs: at most 100,000 lines; CR, LF, CRLF and Unicode line separators are
  recognized, and byte ranges remain authoritative.
- Budgets are 1 to 32,768 tokens. Nonempty input must yield at least one evidence
  block, or the call fails with a budget error.
- Greedy selection considers at most 1,024 optional source sections and their bounded
  fallback excerpts, and stops after 128 failed fits when fewer than 64 tokens remain.
  This is a bounded search, not an optimal packing.
- Supported formats are JSON and plain or ANSI-colored test logs (pytest, unittest,
  node:test TAP and similar). Not YAML, XML, images, JSONL streams or a live MCP
  transport.
- Omitted JSON ranges include punctuation and whitespace between shown values, so a
  value-level summary can omit bytes even when every scalar is shown.

## Source maps and json-source-map

Pointers are mapped to source offsets with json-source-map 1.0.5. Its `calculate()`
builds pointers from raw key text and collapses entries into a dictionary, so keys
such as `a/b` and nested `a`/`b` collide. `sift` therefore reads the library's ordered
entries (`json_source_map.handle.value`) and pairs them with its own strictly parsed
tree, checking entry counts and decoded key spellings; installed packages are never
patched. A draft upstream report and patch are in
[`docs/sift/json-source-map-upstream.md`](sift/json-source-map-upstream.md).

## Related tools

- [shrinkfit](https://github.com/timesler/shrinkfit): general JSON and log shrinking
  within a character cap, with omission counters. `sift` uses it for selection
  proposals; use it directly if marked, parseable previews are enough.
- [MCP Compressor](https://github.com/atlassian-labs/mcp-compressor): an MCP proxy that
  applies shrinkfit and keeps recoverable originals. Its budget is in characters and it
  does not expose source ranges.
- [contextlens-mcp](https://github.com/prasad-a-abhishek/contextlens) and
  [tool-output-truncate](https://docs.rs/tool-output-truncate): approximate token
  estimates and head/tail text truncation, without JSON structure or source spans.
