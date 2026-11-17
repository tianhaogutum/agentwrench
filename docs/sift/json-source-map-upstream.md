# Draft upstream report: json-source-map pointer construction

Status: draft, **not submitted**. Kept as reference for why `agentwrench sift` does
not use `json_source_map.calculate()` directly.

Target: [open-alchemy/json-source-map](https://github.com/open-alchemy/json-source-map),
release **1.0.5**. The same raw-key construction was seen by source inspection of the
upstream default branch in November 2026 (not a run of that branch).

Proposed title: **Decode JSON object keys and RFC 6901-escape reference tokens before
constructing pointers**

Reproduction:

```python
import json_source_map

source = r'{"a/b":1,"a":{"b":2},"a~b":3,"x":4}'
print(json_source_map.calculate(source))
```

Observed: `a/b` and nested `a.b` both produce `/a/b`, so converting entries to a dict
loses one location; `/a~b` is not escaped; a JSON-escaped key becomes `/x`
rather than `/x`.

Expected distinct pointers: `/a~1b`, `/a`, `/a/b`, `/a~0b`, `/x`.

Cause: `handle.object_` takes a raw source substring as `key_value`. Decode the JSON
string with the same stdlib decoder already used for primitive strings, then escape
`~` and `/` in RFC 6901 order. The adjacent
[`json-source-map-1.0.5-rfc6901.patch`](json-source-map-1.0.5-rfc6901.patch) makes
that change. It modifies json-source-map, which is MIT-licensed (Copyright (c) 2021
open-alchemy); its license text is kept in
[`licenses/json-source-map-LICENSE.txt`](../../licenses/json-source-map-LICENSE.txt).
The patch is not applied anywhere in agentwrench.

## How `sift` works around it

`sift` neither mutates the installed package nor monkeypatches it. Until a fixed
release exists, it consumes the parser's ordered entries
(`json_source_map.handle.value`) before `calculate()` converts them to a lossy
dictionary, and pairs them with its own strictly parsed tree in preorder, checking the
entry count and every decoded key spelling. Because this reads a module that is not
documented as public API, the `sift` extra caps json-source-map below 1.1. The
regression tests in `tests/sift/test_core.py` cover escaped `/`, `~`, quotes,
backslashes, Unicode escapes, nested collisions and empty keys.
