# agentwrench cache

`agentwrench cache` finds the JSON path where consecutive captured LLM requests stop
sharing a prompt prefix, which is where provider prompt caching stops helping. It
classifies tool, system, history, cache-control and model changes, can normalize tool
order and schema key order, and replays its own analysis locally to compare stability
before and after a change.

```sh
pip install agentwrench            # or: uvx agentwrench cache --help
agentwrench cache analyze examples/cache/anthropic_tool_reorder.jsonl
agentwrench cache sessions examples/cache/sessions/*.jsonl
```

Everything is offline: no traffic capture, API calls, credentials, tokenizers or paid
services. You supply the captured request bodies. The examples in
[`examples/cache/`](../examples/cache) are synthetic.

## Usage

```text
agentwrench cache analyze CAPTURE [--format FORMAT|jsonl] [--json]
    [--input-format FORMAT (requires --format jsonl)]
    [--fail-on CLASS,...] [--min-stability 0..1]
    [--volatile-fields JSON_FILE] [--policy-view raw|configured]
agentwrench cache canonicalize CAPTURE --out NEW_FILE [--rules RULE,...] [--format FORMAT]
agentwrench cache compare BEFORE AFTER [--json] [--format FORMAT] [--volatile-fields JSON_FILE]
agentwrench cache sessions CAPTURE... [--json] [--format FORMAT]
    [--fail-on CLASS,...] [--min-stability 0..1]
    [--volatile-fields JSON_FILE] [--policy-view raw|configured]
```

Capture formats: `auto` (default), `anthropic`, `openai-chat`, `openai-responses`.

Exit status: **0** success; **1** an `analyze`/`sessions` policy failed (`--fail-on`
matched, or stability below `--min-stability`); **2** invalid options, malformed or
unsupported input, or an I/O error. On exit 1 the complete report is still printed to
stdout and a one-line reason goes to stderr, for example
`agentwrench cache: policy failed: --fail-on matched tool_reordered (1 pair)`. On exit 2
stdout is empty. `compare` and `canonicalize` exit 0 or 2 only.

## Input

UTF-8 JSONL, one complete outbound request per line, **in order, from a single
conversation**. Blank lines are ignored; pair numbers are physical line numbers. Each
line is a bare request body or this wrapper:

```json
{"request":{"model":"claude-example","tools":[],"system":"Stable policy","messages":[{"role":"user","content":"Hello"}]},"usage":{"cache_read_input_tokens":0,"cache_creation_input_tokens":1000},"ts":"2026-01-01T00:00:00Z"}
```

`usage` is the response usage for that same request. `ts` and other wrapper fields are
kept but not interpreted. Bodies need `model` and `messages` (Anthropic, Chat
Completions) or `model` and `input` (Responses); tools and a system prompt are
optional. A string and a content-block array are **not** treated as equivalent.

Auto-detection: `input` means Responses; otherwise `system`, a `claude-` model or a
tool `input_schema` means Anthropic; other `messages` bodies are Chat (also the default
for an ambiguous messages-only request). Use `--format` for a provider-compatible
endpoint whose shape does not identify it. Mixed detected formats fail; a forced format
applies to every line and still validates the required shape.

Supply full history: non-null `previous_response_id`, `conversation`, server-side
`prompt` references and legacy `functions`/`function_call` are rejected because their
hidden context cannot be analyzed.

## Analyze one conversation

```sh
agentwrench cache analyze examples/cache/anthropic_tool_reorder.jsonl
```

```text
format: anthropic; 3 requests
pair	shared	segments	bytes	est.tokens	first divergence	class
1->2	100.00%	5/5	1965	492	"-"	append_only
  usage_chain_ok: expected=1000 actual=1000 delta=0; correlated=append_only; previous cache_read + cache_creation
2->3	2.21%	1/7	46	12	"/tools/0"	tool_reordered
  usage_break: expected=1100 actual=0 delta=-1100; correlated=tool_reordered; previous cache_read + cache_creation
stability: 0.511052
classes: {"append_only":1,"tool_reordered":1}
captured usage compared pairs: 2
captured usage breaks: 1
UTF-8 serialized bytes / 4 is an estimate, not provider tokens or measured savings.
```

Each pair shows the share of the previous request's modeled bytes that is still a
common prefix, the fully shared segments, the shared bytes and estimated tokens
(rounded up in text; JSON keeps `sharedEstimatedTokens = sharedBytes / 4`), the exact
RFC 6901 pointer of the first divergence (JSON-escaped) and its class. Overall
stability is the mean over adjacent pairs. A single request prints `nothing to
compare` and has JSON stability `null`.

| Class | Meaning of the earliest divergence |
| --- | --- |
| `append_only` | Equal request, or history appended after the complete previous prefix |
| `tool_reordered` | Same tool definitions (ignoring key order), different order |
| `tool_added` / `tool_removed` | Tools appear or disappear (removal wins for a replacement) |
| `tool_schema_changed` | An existing tool definition changed, including its description or key order |
| `system_changed` | Anthropic `system`, Responses `instructions`, or a system/developer message changed |
| `history_rewritten` | Earlier messages or input items changed, were truncated or changed representation |
| `cache_control_moved` | `cache_control` placement, value or TTL changed |
| `model_changed` | Different model |

A pair reports one **primary cause at the earliest divergence**; later simultaneous
changes are not listed. For a changed system text, `differing span` shows up to 120
characters of before/after context and flags ISO timestamps, dates, UUIDs, absolute
paths and labeled counters (`volatile`). Key/token/password-looking spans are redacted,
but this is **not a secret scrubber**: reports can contain prompt excerpts, so keep
them local. Parser errors never echo raw payloads.

`--json` prints the full report: `format`, `requests`, `pairs`, `stability`,
`classCounts` (every observed class, summing to `pairs`), `ignoredFields` (unmodeled
top-level fields such as `max_tokens` or `temperature`), `usageBreaks`,
`usageComparedPairs` and one `rows` entry per pair.

### CI gates

```sh
agentwrench cache analyze capture.jsonl \
  --fail-on tool_reordered,tool_removed,system_changed,usage_break --min-stability 0.95
```

`--fail-on` takes any of the classes above plus `usage_break`; `--min-stability` fails
strictly below the threshold (a capture with no pairs passes). With the example
capture this exits 1 and prints, on stderr:

```text
agentwrench cache: policy failed: --fail-on matched tool_reordered (1 pair), usage_break (1 pair); stability 0.511052 is below --min-stability 0.95
```

### JSONL export

`analyze CAPTURE --format jsonl` writes the same analysis as compact JSON lines for
pipelines: first `{"schemaVersion":1,"type":"summary","raw":{...}}` (the JSON report
without `rows`, plus `configured` and `volatility` when an allowlist is active), then
one `{"schemaVersion":1,"type":"pair","pairIndex":N,"raw":{...}}` per pair in order
(with `configured` when enabled). `pairIndex` is zero-based; `from`/`to` remain
physical line numbers. `--format jsonl` conflicts with `--json`; use
`--input-format FORMAT` to choose the capture dialect in this mode. The whole capture is
validated before the first line is written.

```sh
agentwrench cache analyze examples/cache/anthropic_tool_reorder.jsonl --format jsonl
```

## Known volatile fields

An optional allowlist of JSON pointers names request fields your application knows to
be volatile, such as a timestamp in the system prompt. A second, **configured** analysis
masks those scalar values in memory (`""`, `0`, `false`, `null`); captures are never
rewritten. Raw results stay unchanged next to it.

```sh
agentwrench cache analyze examples/cache/openai_chat_volatile_system.jsonl \
  --volatile-fields examples/cache/volatile-fields.json
```

With `["/messages/0/content"]`, the raw pair stays `system_changed` (stability
0.232117) while the configured pair is `append_only` with stability 1:

```text
configured analysis (scalar values masked):
pair	shared	segments	bytes	est.tokens	first divergence	class
1->2	100.00%	5/5	2011	503	"-"	append_only
configured stability: 1.000000
configured classes: {"append_only":1}
```

CI gates use the raw view unless you select the configured one:

```sh
# exit 1: raw stability 0.232117 is below 1
agentwrench cache analyze examples/cache/openai_chat_volatile_system.jsonl \
  --volatile-fields examples/cache/volatile-fields.json --min-stability 1
# exit 0: configured classes and stability
agentwrench cache analyze examples/cache/openai_chat_volatile_system.jsonl \
  --volatile-fields examples/cache/volatile-fields.json --policy-view configured \
  --min-stability 1 --fail-on system_changed
```

`usage_break` always checks the original captured usage, in both views; masking never
fabricates usage evidence.

Pointers use RFC 6901 syntax relative to the **request body** (also for wrapped
captures): `/system/0/text`, `/messages/0/content`, `/instructions`,
`/input/0/content/0/text`, `/tools/0/description`. Escape `/` as `~1` and `~` as `~0`;
array indices are exact decimal positions. Only existing scalar values under modeled
top-level fields can be masked; `/model`, message `role` and tool identity fields are
protected. A pointer that matches no request scalar, targets an object or array,
or is invalid or duplicated exits 2. The file is a JSON array of at most 128 pointers
(64 KiB). Masking a text field hides **all** of its text, including instructions; put
volatile metadata in its own content block when you can. Allowlisting is not
redaction.

## Compare independent sessions

`sessions` analyzes several capture files independently (no pair crosses files) and
ranks the earliest breaking paths across them:

```sh
agentwrench cache sessions examples/cache/sessions/a.jsonl examples/cache/sessions/b.jsonl \
  examples/cache/sessions/c.jsonl --json
```

In this example `a` and `b` rewrite the first user message and `c` changes the model.
The report has 6 requests, 3 pairs, pooled stability 0.259259 and:

```json
[
  {"rank":1,"path":"/messages/0/content","pairs":2,"sessions":2,"unsharedBytes":208,"classCounts":{"history_rewritten":2}},
  {"rank":2,"path":"/model","pairs":1,"sessions":1,"unsharedBytes":152,"classCounts":{"model_changed":1}}
]
```

Each pair with a positive prefix loss contributes `previousBytes - sharedBytes` once, to
its earliest divergent pointer. Rows are ranked by summed unshared bytes, then pair
count, then pointer, and list their `contributors` (file, pairs, bytes, classes). JSON
(`schemaVersion: 1`) also has pooled `classCounts`, `breakingPairs`, `usageBreaks`,
`usageComparedPairs` and a compact summary per file. Overall stability is the mean over
**all within-file pairs**. `--fail-on`, `--min-stability`, `--volatile-fields` (applied
to each file independently) and `--policy-view` work as for `analyze`:

```sh
# exit 1; stderr: agentwrench cache: policy failed: stability 0.259259 is below --min-stability 0.3
agentwrench cache sessions examples/cache/sessions/*.jsonl --min-stability 0.3
```

Limits: 1–32 explicit files and 20,000 requests in total, plus the per-capture limits
below. Paths are not expanded or discovered by the tool, duplicate resolved paths are
rejected, and permuting the same arguments gives byte-identical JSON.

## Canonicalize and compare

```sh
agentwrench cache canonicalize examples/cache/anthropic_tool_reorder.jsonl --out canonical.jsonl
agentwrench cache compare examples/cache/anthropic_tool_reorder.jsonl canonical.jsonl
```

```text
wrote 3 requests; historical usage preserved, not replayed
metric	before	after	delta
stability	0.511052	1.000000	0.488948
Offline replay of the analyzer only. Historical usage is not a measurement of the variant.
```

`--out` must be a **new file** in an existing directory: no overwrite, no following an
output symlink. The file (mode 0600) is linked into place only when complete. Rules
(both by default, select with `--rules`):

- `sort-tools`: sorts tools by name (case-sensitive, locale-independent). Duplicate
  names and tools with a top-level `cache_control` are rejected, since reordering could
  move a cache boundary; built-in/server tools are analysis-only.
- `sort-schema-keys`: sorts object keys at known JSON Schema locations under
  `input_schema`, `function.parameters` or `parameters`. Arrays (`required`, `enum`,
  ...), instance data (`default`, `const`, examples) and unknown extensions are left
  alone; `$ref` is never resolved.

Rules never change messages, history, instructions or wrapper metadata. Presentation
order can still affect model behavior, so opt in only when the new order is acceptable.
Historical usage is copied for traceability and **is not the variant's measured
usage**. `compare` requires the same format and request count on both sides and, with
`--volatile-fields`, adds a configured before/after/improvement.

## What the metric means

| Format | Segment order (after a model guard and an optional top-level cache-control guard) |
| --- | --- |
| Anthropic Messages | each tool, each system block (or the string), each message |
| OpenAI Chat | each tool, then messages in order, including system/developer |
| OpenAI Responses | each tool, instructions, then input items in order |

Anthropic documents its tools/system/messages hierarchy; **both OpenAI orders are
diagnostic assumptions**, not recovered provider serializers. A segment is
`JSON.stringify([kind, value]) + "\n"`; the score counts equal leading segments, then
common bytes in the first differing segment only if its kind matches, divided by the
previous request's modeled bytes. Ordinary history appends score 1.

This is a deterministic **serialized-prefix stability estimate**, not prompt tokens,
real cache hits, savings, cost or latency. It does not model TTLs, minimum cache
lengths, block lookback or routing, and fields listed in `ignoredFields` can still
affect real behavior.

### Usage correlation

Anthropic: expected = previous `cache_read_input_tokens` + `cache_creation_input_tokens`;
actual = next `cache_read_input_tokens`; any difference is a `usage_break` (the chain
check discussed in
[claude-code #81967](https://github.com/anthropics/claude-code/issues/81967), not a
provider guarantee). OpenAI uses `prompt_tokens_details` / `input_tokens_details`:
previous cached + written tokens when `cache_write_tokens` is present, otherwise the
previous prompt/input total as an assumed reusable target. These are heuristics:
thresholds, expiry and quantization can produce false positives. Missing counters give
`usage: null` and a `captured usage evidence` note; malformed counters are rejected.

## Limits

Regular files only (no stdin, network or archives). Per capture: 32 MiB, 4 MiB per line,
10,000 nonblank records, JSON depth 64 and 250,000 nodes per record. Non-finite numbers
fail; usage counters must be nonnegative safe integers. Duplicate JSON keys keep the
last value. Undocumented Claude Code `api-request-*` logs are not supported.

## Python API

```python
from agentwrench.cache import analyze, analyze_sessions, read_capture, read_volatile_fields

records = read_capture("examples/cache/openai_chat_volatile_system.jsonl")
fields = read_volatile_fields("examples/cache/volatile-fields.json")
report = analyze(records, fields)
print(report["stability"], report["configured"]["stability"])   # 0.2321... 1.0

pooled = analyze_sessions(["examples/cache/sessions/a.jsonl", "examples/cache/sessions/c.jsonl"])
print(pooled["rankedPaths"][0]["path"])
```
