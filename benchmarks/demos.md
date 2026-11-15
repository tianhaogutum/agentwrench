# Demos: `cache` and `doctor` on the repo's own examples

Each demo runs `agentwrench` on the small synthetic fixtures in `examples/`, from the
repository root. No third-party data is involved. The outputs below are exact
(agentwrench 0.1.0, captured 2026-11-15). The `[exit N]` lines are the exit status
and aren't part of the output.

## 1. `cache analyze`: which JSON path broke the prompt cache, and why

`examples/cache/anthropic_tool_reorder.jsonl` holds three captured Anthropic Messages
requests with the provider's usage numbers. Request 3 sends the same two tools as
request 2, but in a different order.

```console
$ uv run agentwrench cache analyze examples/cache/anthropic_tool_reorder.jsonl
format: anthropic; 3 requests
pair	shared	segments	bytes	est.tokens	first divergence	class
1->2	100.00%	5/5	1965	491.25	"-"	append_only
  usage_chain_ok: expected=1000 actual=1000 delta=0; correlated=append_only; previous cache_read + cache_creation
2->3	2.21%	1/7	46	11.5	"/tools/0"	tool_reordered
  usage_break: expected=1100 actual=0 delta=-1100; correlated=tool_reordered; previous cache_read + cache_creation
stability: 0.511052
classes: {"append_only":1,"tool_reordered":1}
captured usage compared pairs: 2
captured usage breaks: 1
UTF-8 serialized bytes / 4 is an estimate, not provider tokens or measured savings.
[exit 0]
```

How to read it:

- `1->2` only appends messages, so request 2 reuses the whole cached prefix. The
  captured usage agrees: 1,000 tokens expected from the cache, 1,000 read.
- `2->3` diverges at JSON pointer **`/tools/0`**, class **`tool_reordered`**. The
  tools come before the messages, so only 46 bytes (2.21%) of the prefix are
  still shared. The captured usage confirms the miss: 1,100 cache-read tokens
  expected, 0 read.

To gate CI on this, add `--fail-on`. The output is the same, but the exit status
is 1:

```console
$ uv run agentwrench cache analyze examples/cache/anthropic_tool_reorder.jsonl --fail-on tool_reordered
...
[exit 1]
```

Sorting tools in a stable order fixes it. `cache canonicalize` shows the effect
offline. It rewrites the requests, keeps the historical usage numbers as they
were, and doesn't replay anything to the provider:

```console
$ uv run agentwrench cache canonicalize examples/cache/anthropic_tool_reorder.jsonl --rules sort-tools --out /tmp/canon.jsonl
wrote 3 requests; historical usage preserved, not replayed
[exit 0]
$ uv run agentwrench cache analyze /tmp/canon.jsonl
format: anthropic; 3 requests
pair	shared	segments	bytes	est.tokens	first divergence	class
1->2	100.00%	5/5	1965	491.25	"-"	append_only
  usage_chain_ok: expected=1000 actual=1000 delta=0; correlated=append_only; previous cache_read + cache_creation
2->3	100.00%	7/7	2081	520.25	"-"	append_only
  usage_break: expected=1100 actual=0 delta=-1100; correlated=append_only; previous cache_read + cache_creation
stability: 1.000000
classes: {"append_only":2}
captured usage compared pairs: 2
captured usage breaks: 1
UTF-8 serialized bytes / 4 is an estimate, not provider tokens or measured savings.
[exit 0]
```

The `usage_break` line remains because the usage numbers are the historical
ones. The requests themselves now share 100% of their prefix.

A second cause, a timestamp and a counter inside the system prompt
(`examples/cache/openai_chat_volatile_system.jsonl`):

```console
$ uv run agentwrench cache analyze examples/cache/openai_chat_volatile_system.jsonl
format: openai-chat; 2 requests
pair	shared	segments	bytes	est.tokens	first divergence	class
1->2	23.21%	3/5	477	119.25	"/messages/0/content"	system_changed
  differing span: {"start":31,"before":"ed at 2026-01-01T00:00:00Z, counter=1","after":"ed at 2026-01-01T00:00:01Z, counter=2","volatile":["iso_timestamp","counter"]}
  usage_break: expected=1024 actual=0 delta=-1024; correlated=system_changed; previous prompt_tokens (assumed reusable; heuristic)
stability: 0.232117
classes: {"system_changed":1}
captured usage compared pairs: 1
captured usage breaks: 1
UTF-8 serialized bytes / 4 is an estimate, not provider tokens or measured savings.
[exit 0]
```

## 2. `doctor`: a session with a tool call that never got a result

`examples/doctor/claude/orphan_tool_use.jsonl` is a Claude Code session whose last
assistant turn issued a `Read` tool call, `u1`, and was then interrupted. No
`tool_result` follows it. An unpaired call like this can poison the
conversation when the session is resumed.

```console
$ uv run agentwrench doctor check examples/doctor/claude/orphan_tool_use.jsonl
examples/doctor/claude/orphan_tool_use.jsonl [claude]: 1 issues
  L1 RD201 "u1": tool call has no later output in its chain
[exit 1]
```

`repair --dry-run` shows the planned change as a diff and writes nothing:

```console
$ uv run agentwrench doctor repair --dry-run examples/doctor/claude/orphan_tool_use.jsonl
examples/doctor/claude/orphan_tool_use.jsonl [claude]: 1 changes (dry-run)
  L1 insert_stub: insert synthetic error output immediately after call
--- examples/doctor/claude/orphan_tool_use.jsonl
+++ examples/doctor/claude/orphan_tool_use.jsonl (planned)
@@ -1,1 +1,2 @@
-{"type":"assistant","uuid":"a","parentUuid":null,"sessionId":"synthetic","isSidechain":false,"message":{"role":"assistant","content":[{"type":"text","text":"Reading"},{"type":"tool_use","id":"u1","name":"Read","input":{"file_path":"example.txt"}}]}}
+{"type":"assistant","uuid":"a","parentUuid":null,"sessionId":"synthetic","isSidechain":false,"message":{"role":"assistant","content":[{"type":"text","text":"Reading"},{"type":"tool_use","id":"u1","name":"Read","input":{"file_path":"example.txt"}}]}}
+{"sessionId":"synthetic","isSidechain":false,"type":"user","uuid":"6fcd072c-3ed1-564b-a22d-3e95cda40949","parentUuid":"a","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"u1","content":"[rollout-doctor] tool output missing: call was interrupted","is_error":true}]}}
[exit 0]
```

The original line is kept byte for byte; the diff re-lists it as `-`/`+` around
the insertion. The repair adds one synthetic `is_error` tool result, chained to
the call through `parentUuid`. Run with `--out FILE` to write the repaired
copy. `doctor check` on that copy reports `clean`.

The Codex example (`examples/doctor/codex/orphan.jsonl`) shows both kinds of
damage: an output with no `call_id`, which gets dropped, and a call with no
output, which gets a stub:

```console
$ uv run agentwrench doctor check examples/doctor/codex/orphan.jsonl
examples/doctor/codex/orphan.jsonl [codex]: 2 issues
  L1 RD103: tool output missing usable call_id
  L2 RD101 "c1": tool call has no later output in its chain
[exit 1]
$ uv run agentwrench doctor repair --dry-run examples/doctor/codex/orphan.jsonl
examples/doctor/codex/orphan.jsonl [codex]: 2 changes (dry-run)
  L1 drop_line: tool output missing usable call_id
  L2 insert_stub: insert synthetic error output immediately after call
--- examples/doctor/codex/orphan.jsonl
+++ examples/doctor/codex/orphan.jsonl (planned)
@@ -1,1 +0,0 @@
-{"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"function_call_output","output":"bad missing id"}}
@@ -2,1 +1,2 @@
-{"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{\"command\":\"pwd\"}","call_id":"c1"}}
+{"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{\"command\":\"pwd\"}","call_id":"c1"}}
+{"type":"response_item","payload":{"type":"function_call_output","call_id":"c1","output":"[rollout-doctor] tool output missing: call was interrupted"},"timestamp":"2026-01-01T00:00:00Z"}
[exit 0]
```
