"""Pairwise prefix comparison, divergence pointers, classification and usage chains."""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from .capture import Record, pointer, tool_identity
from .errors import CacheError
from .jscompat import (
    JS_NOT_S,
    JS_S,
    JS_WHITESPACE,
    from_code_units,
    is_object,
    is_safe_integer,
    js_object,
    prop,
    stringify,
    to_code_units,
    utf16_key,
)
from .volatile import mask_volatile_fields

CLASSES: tuple[str, ...] = (
    "append_only",
    "tool_reordered",
    "tool_added",
    "tool_removed",
    "tool_schema_changed",
    "system_changed",
    "history_rewritten",
    "cache_control_moved",
    "model_changed",
)

_RANK = {"model": 0, "cache": 1, "tool": 2, "system": 3, "history": 4}
_SYSTEM_ROLES = ("system", "developer")
_INDEX = re.compile(r"0|[1-9][0-9]*")
VOLATILITY_NOTE = (
    'Analysis-only scalar masking (string="", number=0, boolean=false, null=null). Types, presence and key order '
    "remain significant. Usage describes raw requests. Captured requests are unchanged."
)
HISTORY_REASON = (
    "Earlier history changed, was removed, or changed representation; "
    "strings and content-block arrays are not assumed equivalent."
)
USAGE_NOTE = "No chain comparison: both records need the relevant usage counters."


def _same(a: Any, b: Any) -> bool:
    return stringify(a) == stringify(b)


def sorted_object(value: Any) -> Any:
    """Recursively sort object keys by UTF-16 code units (JavaScript order)."""
    if isinstance(value, list):
        return [sorted_object(v) for v in value]
    if not is_object(value):
        return value
    return js_object((k, sorted_object(value[k])) for k in sorted(value, key=utf16_key))


class Segment(NamedTuple):
    path: str
    kind: str
    value: Any
    bytes: bytes


def segments(body: dict[str, Any], format: str) -> list[Segment]:
    """Split a request into ordered prefix segments with their serialized UTF-8 bytes."""
    out: list[Segment] = []

    def add(path: str, kind: str, value: Any) -> None:
        out.append(Segment(path, kind, value, (stringify([kind, value]) + "\n").encode("utf-8")))

    def blocks(key: str, kind: str) -> None:
        if body.get(key) is None:
            return
        value = body[key]
        if isinstance(value, list):
            for i, item in enumerate(value):
                role = prop(item, "role")
                add(f"/{key}/{i}", "system" if key == "input" and role in _SYSTEM_ROLES else kind, item)
        else:
            add(f"/{key}", kind, value)

    add("/model", "model", body.get("model"))
    if "cache_control" in body:
        add("/cache_control", "cache", body["cache_control"])
    for i, tool in enumerate(body.get("tools") or []):
        add(f"/tools/{i}", "tool", tool)
    if format == "anthropic":
        blocks("system", "system")
    if format == "openai-responses":
        blocks("instructions", "system")
        blocks("input", "history")
    else:
        for i, message in enumerate(body.get("messages") or []):
            add(f"/messages/{i}", "system" if message.get("role") in _SYSTEM_ROLES else "history", message)
    return out


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    """Return a resolvable pointer, or the missing key/index for additions/removals."""
    if _same(a, b):
        return None
    if isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            if i >= len(a) or i >= len(b):
                return f"{path}/{i}"
            found = first_difference(a[i], b[i], f"{path}/{i}")
            if found is not None:
                return found
    if is_object(a) and is_object(b):
        a_keys, b_keys = list(a), list(b)
        for i in range(max(len(a_keys), len(b_keys))):
            ak = a_keys[i] if i < len(a_keys) else None
            bk = b_keys[i] if i < len(b_keys) else None
            if ak != bk:
                return f"{path}/{pointer(ak if ak is not None else bk)}"
            found = first_difference(a[ak], b[bk], f"{path}/{pointer(ak)}")
            if found is not None:
                return found
    return path


def _without_cache(segment: Segment) -> Any:
    def omit(value: Any) -> Any:
        return {k: v for k, v in value.items() if k != "cache_control"} if is_object(value) else value

    value = omit(segment.value)
    # Messages/input items contain provider content blocks. Tool schemas and arbitrary
    # nested JSON are data: a property named cache_control there is not a breakpoint.
    if segment.kind in ("system", "history") and is_object(value) and isinstance(value.get("content"), list):
        value = {**value, "content": [omit(v) for v in value["content"]]}
    return value


def _tool_class(a: dict[str, Any], b: dict[str, Any]) -> str:
    a_tools, b_tools = a.get("tools") or [], b.get("tools") or []
    a_names = [tool_identity(t) for t in a_tools]
    b_names = [tool_identity(t) for t in b_tools]
    if any(n not in b_names for n in a_names):
        return "tool_removed"
    if any(n not in a_names for n in b_names):
        return "tool_added"
    by_id = {tool_identity(t): t for t in b_tools}
    unchanged = all(_same(sorted_object(t), sorted_object(by_id.get(tool_identity(t)))) for t in a_tools)
    if unchanged and not _same(a_names, b_names):
        return "tool_reordered"
    return "tool_schema_changed"


_PATTERNS = [
    ("iso_timestamp", re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})\b", re.A)),
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b", re.A)),
    ("uuid", re.compile(r"\b[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}\b", re.A | re.I)),
    ("absolute_path", re.compile(r"(?:[A-Za-z]:\\|/(?!/))[\w.~-]+(?:[/\\][\w. ~-]+)+", re.A)),
    (
        "counter",
        re.compile(
            rf"\b(?:counter|count|step|turn|iteration|request|seq)(?:{JS_S}*[:=#]{JS_S}*|{JS_S}+)\d+\b", re.A | re.I
        ),
    ),
]
_REDACT_TOKENS = re.compile(rf"\b(?:sk-[\w-]+|Bearer{JS_S}+[^{JS_WHITESPACE}\"']+)", re.A | re.I)
_REDACT_ASSIGNMENTS = re.compile(
    rf"((?:api[_-]?key|token|password|secret){JS_S}*[=:]{JS_S}*)[^{JS_WHITESPACE},;\"']+", re.A | re.I
)
_SECRET = re.compile(
    rf"(?:sk-[\w-]+|Bearer{JS_S}+{JS_NOT_S}+|(?:api[_-]?key|token|password|secret){JS_S}*[=:])", re.A | re.I
)
_REDACTED_SPAN = "[REDACTED: sensitive-looking system text]"


def _redact(text: str) -> str:
    return _REDACT_ASSIGNMENTS.sub(r"\1[REDACTED]", _REDACT_TOKENS.sub("[REDACTED]", text))


def differing_span(before: str, after: str) -> dict[str, Any]:
    """Locate the changed region of two strings (UTF-16 indexes) and flag volatile tokens."""
    before, after = to_code_units(before), to_code_units(after)
    start, end_a, end_b = 0, len(before), len(after)
    while start < min(end_a, end_b) and before[start] == after[start]:
        start += 1
    while end_a > start and end_b > start and before[end_a - 1] == after[end_b - 1]:
        end_a -= 1
        end_b -= 1
    kinds: list[str] = []
    for kind, regex in _PATTERNS:
        for text, end in ((before, end_a), (after, end_b)):
            for match in regex.finditer(text):
                if match.start() <= end and match.end() > start and kind not in kinds:
                    kinds.append(kind)

    def clip(text: str, end: int) -> str:
        # Redact before selecting the display context to avoid displaying fragments of a secret.
        return from_code_units(_redact(text)[max(0, start - 24) : max(start + 1, end) + 24][:120])

    secret = _SECRET.search(before + after) is not None
    return {
        "start": start,
        "before": _REDACTED_SPAN if secret else clip(before, end_a),
        "after": _REDACTED_SPAN if secret else clip(after, end_b),
        "volatile": kinds,
    }


def _at_path(value: Any, path: str) -> Any:
    """``value?.[k]`` for each pointer token, including string code-unit indexing."""
    for token in path.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, (list, str)) and _INDEX.fullmatch(key):
            items = to_code_units(value) if isinstance(value, str) else value
            value = items[int(key)] if int(key) < len(items) else None
        else:
            value = None
    return value


def _count(value: Any) -> bool:
    return is_safe_integer(value) and value >= 0


def usage_chain(prev: Any, next: Any, format: str, payload_class: str) -> dict[str, Any] | None:
    """Compare captured cache counters of consecutive requests (heuristic)."""
    if not prev or not next:
        return None
    if format == "anthropic":
        values = (
            prev.get("cache_read_input_tokens"),
            prev.get("cache_creation_input_tokens"),
            next.get("cache_read_input_tokens"),
        )
        if not all(_count(v) for v in values):
            return None
        expected = values[0] + values[1]
        actual = values[2]
        basis = "previous cache_read + cache_creation"
    else:
        key = "prompt_tokens_details" if format == "openai-chat" else "input_tokens_details"
        total = "prompt_tokens" if format == "openai-chat" else "input_tokens"
        p, n = prev.get(key), next.get(key)
        if not p or not n or not _count(prop(p, "cached_tokens")) or not _count(prop(n, "cached_tokens")):
            return None
        if _count(p.get("cache_write_tokens")):
            expected = p["cached_tokens"] + p["cache_write_tokens"]
            basis = "previous cached_tokens + cache_write_tokens"
        elif _count(prev.get(total)):
            expected = prev[total]
            basis = f"previous {total} (assumed reusable; heuristic)"
        else:
            expected = p["cached_tokens"]
            basis = "previous cached_tokens only (writes unknown; heuristic)"
        actual = n["cached_tokens"]
    if not is_safe_integer(expected):
        return None
    return {
        "expected": expected,
        "actual": actual,
        "delta": actual - expected,
        "usageBreak": actual != expected,
        "payloadClass": payload_class,
        "basis": basis,
        "heuristic": True,
    }


def _common_prefix(a: bytes, b: bytes) -> int:
    limit = min(len(a), len(b))
    low, high = 0, limit
    while low < high:  # longest equal prefix by bisection on slice equality
        middle = (low + high + 1) // 2
        if a[:middle] == b[:middle]:
            low = middle
        else:
            high = middle - 1
    return low


def compare_pair(prev: Record, next: Record) -> dict[str, Any]:
    a, b = segments(prev.body, prev.format), segments(next.body, next.format)
    previous_bytes = sum(len(s.bytes) for s in a)
    next_bytes = sum(len(s.bytes) for s in b)
    shared_segments = shared_bytes = 0
    while (
        shared_segments < min(len(a), len(b))
        and a[shared_segments].kind == b[shared_segments].kind
        and a[shared_segments].bytes == b[shared_segments].bytes
    ):
        shared_bytes += len(a[shared_segments].bytes)
        shared_segments += 1
    left = a[shared_segments] if shared_segments < len(a) else None
    right = b[shared_segments] if shared_segments < len(b) else None
    if left and right and left.kind == right.kind:
        shared_bytes += _common_prefix(left.bytes, right.bytes)
    if not left:
        first = right
    elif not right or _RANK[left.kind] <= _RANK[right.kind]:
        first = left
    else:
        first = right
    classification, first_divergence, span, reason = "append_only", None, None, None
    if first:
        path, kind = first.path, first.kind
        if kind == "model":
            classification = "model_changed"
        elif kind == "cache" or (
            left and right and not _same(left.value, right.value) and _same(_without_cache(left), _without_cache(right))
        ):
            classification = "cache_control_moved"
        elif kind == "tool" or (right and right.kind == "tool"):
            classification = _tool_class(prev.body, next.body)
        elif not left and kind == "history":
            classification = "append_only"
        elif kind == "system" or (right and right.kind == "system"):
            classification = "system_changed"
        else:
            classification = "history_rewritten"
        if left and right and left.path == right.path and classification != "tool_reordered":
            path = first_difference(left.value, right.value, left.path) or left.path
        first_divergence = {"segment": first.path, "path": path}
        if classification == "append_only":
            first_divergence = None
        if classification == "system_changed":
            av, bv = _at_path(prev.body, path), _at_path(next.body, path)
            if isinstance(av, str) and isinstance(bv, str):
                span = differing_span(av, bv)
        if classification == "history_rewritten":
            reason = HISTORY_REASON
    usage = usage_chain(prev.usage, next.usage, prev.format, classification)
    row: dict[str, Any] = {
        "from": prev.line,
        "to": next.line,
        "class": classification,
        "sharedSegments": shared_segments,
        "previousSegments": len(a),
        "nextSegments": len(b),
        "sharedBytes": shared_bytes,
        "previousBytes": previous_bytes,
        "nextBytes": next_bytes,
        "sharedEstimatedTokens": shared_bytes / 4,
        "tokenEstimateMethod": "UTF-8 serialized bytes / 4; not provider tokens",
        "stability": shared_bytes / previous_bytes,
        "firstDivergence": first_divergence,
    }
    if span:
        row["differingSpan"] = span
    if reason:
        row["reason"] = reason
    row["usage"] = usage
    if usage is None:
        row["usageNote"] = USAGE_NOTE
    return row


def _modeled(format: str) -> set[str]:
    if format == "anthropic":
        extra = ("system", "messages")
    elif format == "openai-chat":
        extra = ("messages",)
    else:
        extra = ("instructions", "input")
    return {"model", "cache_control", "tools", *extra}


def ordered_counts(counts: dict[str, int]) -> dict[str, int]:
    return {key: counts[key] for key in sorted(counts, key=utf16_key)}


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    total = 0.0
    for value in values:  # left-to-right like Array.reduce (no compensated summation)
        total += value
    return total / len(values)


def analyze(records: list[Record], volatile_fields: Any = ()) -> dict[str, Any]:
    """Analyze consecutive request pairs of one capture."""
    if not records:
        raise CacheError("capture contains no requests")
    rows = [compare_pair(records[i], record) for i, record in enumerate(records[1:])]
    # Observed payload classes include append-only and zero-loss changes.
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    modeled = _modeled(records[0].format)
    ignored = {key for record in records for key in record.body if key not in modeled}
    report: dict[str, Any] = {
        "format": records[0].format,
        "requests": len(records),
        "pairs": len(rows),
        "stability": _mean([row["stability"] for row in rows]),
        "classCounts": ordered_counts(counts),
        "message": "Offline serialized-prefix estimate; not measured cache savings." if rows else "nothing to compare",
        "ignoredFields": sorted(ignored, key=utf16_key),
        "usageBreaks": sum(1 for row in rows if row["usage"] and row["usage"]["usageBreak"]),
        "usageComparedPairs": sum(1 for row in rows if row["usage"] is not None),
        "rows": rows,
    }
    projected = mask_volatile_fields(records, volatile_fields, modeled)
    if projected.fields:
        configured = analyze(projected.records)
        # Usage is evidence from the original requests, never a measured masked replay.
        for row, raw in zip(configured["rows"], rows):
            row["usage"] = raw["usage"]
        report["configured"] = configured
        report["volatility"] = {"fields": projected.fields, "note": VOLATILITY_NOTE}
        for row, changes in zip(rows, projected.changes):
            row["volatileChanges"] = changes
    return report
