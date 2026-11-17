from __future__ import annotations

import hashlib
import json
import math
import re
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

import json_source_map
import jsonpointer
import shrinkfit
import tiktoken
from strip_ansi import strip_ansi

MAX_BYTES = 8 * 1024 * 1024
MAX_NODES = 50_000
MAX_DEPTH = 96
MAX_LINES = 100_000
MAX_BUDGET = 32_768
MAX_POINTER = 4096
ENCODINGS = ("cl100k_base", "o200k_base")
SCHEMA = "tool-sift/v1"
OFFSETS = "zero-based UTF-8 bytes; end exclusive; lines one-based inclusive"
NOTICE = "Evidence only. Omitted bytes were returned by the tool; absent fields here are unknown."
BOM = "\ufeff"
ERROR = re.compile(
    r"(?i)(?:\b(?:FAIL(?:ED)?|FAILURE[S]?|ERROR[S]?|FATAL|PANIC(?:KED)?|EXCEPTION|TRACEBACK)\b"
    r"|\b\w+(?:Error|Exception)\b|\bERR_[A-Z_]+\b|^\s*E\s{2,}|\bnot ok\b|\bBail out!|^\s*✖)"
)
SUMMARY = re.compile(
    r"(?i)(?:short test summary|\b\d+ (?:failed|passed|errors?|skipped)\b"
    r"|^# (?:tests|pass|fail)\b|^1\.\.\d+|tests?:\s+\d+)"
)
IMPORTANT = frozenset(
    {"error", "errors", "failure", "failures", "exception", "traceback", "stack", "iserror"}
)


class InputError(ValueError):
    """Invalid source, rule, or recovery request; no partial result is returned."""


class BudgetError(ValueError):
    """The budget cannot hold mandatory evidence and its metadata."""

    def __init__(self, budget: int, needed: int):
        self.budget, self.needed = budget, needed
        super().__init__(f"budget {budget} cannot hold required evidence; need {needed} tokens")


@dataclass(frozen=True)
class Rules:
    """Exact RFC 6901 pointers and inclusive, one-based log line ranges."""

    keep: tuple[str, ...] = ()
    keep_lines: tuple[tuple[int, int], ...] = ()
    priority_keys: tuple[str, ...] = tuple(sorted(IMPORTANT))
    context_lines: int = 2
    keep_patterns: tuple[str, ...] = ()


DEFAULT_RULES = Rules()


@dataclass(frozen=True)
class Summary:
    text: str
    tokens: int
    document: dict[str, Any]


@dataclass(frozen=True)
class Section:
    start: int
    end: int
    pointer: str | None = None
    complete: bool = True
    lines: tuple[int, int] | None = None
    score: int = 0


@dataclass
class Source:
    raw: bytes
    text: str
    format: str
    byte_at: dict[int, int] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    parents: dict[str, tuple[str, ...]] = field(default_factory=dict)
    locations: dict[str, tuple[int, int]] = field(default_factory=dict)
    digest: str = ""
    failure_roots: set[str] = field(default_factory=set)
    record_states: dict[str, bool] = field(default_factory=dict)

    def index_bytes(self, positions: set[int]) -> None:
        cursor = total = 0
        for position in sorted(positions | {0, len(self.text)}):
            total += len(self.text[cursor:position].encode("utf-8"))
            self.byte_at[position] = total
            cursor = position


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _raw_source(data: str | bytes) -> tuple[bytes, str]:
    try:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if not isinstance(raw, bytes):
            raise InputError("source must be str or bytes")
        if len(raw) > MAX_BYTES:
            raise InputError(f"source exceeds {MAX_BYTES} bytes")
        return raw, raw.decode("utf-8")
    except UnicodeError as exc:
        raise InputError("source must be valid UTF-8 without unpaired surrogates") from exc


def _strict_json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise InputError("duplicate JSON object key; pointer identity would be ambiguous")
            try:
                key.encode("utf-8")
            except UnicodeError as exc:
                raise InputError("JSON object keys must not contain unpaired surrogates") from exc
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise InputError("non-finite numbers are not valid JSON")

    try:
        return json.loads(text.removeprefix(BOM), object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as exc:
        column = exc.colno + int(text.startswith(BOM) and exc.lineno == 1)
        raise InputError(f"invalid JSON at line {exc.lineno}, column {column}: {exc.msg}") from exc
    except (RecursionError, ValueError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("JSON nesting or numeric literal exceeds parser limits") from exc


def _type(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    return "number"


def _record_state(value: Any) -> bool | None:
    if not isinstance(value, dict):
        return None
    states = {v.casefold() for key in ("outcome", "status") if isinstance(v := value.get(key), str)}
    if states.intersection({"failed", "fail", "failure", "error", "errored", "panic"}):
        return True
    if states.intersection(
        {"passed", "pass", "ok", "success", "successful", "skip", "skipped", "pending", "todo"}
    ):
        return False
    return None


def _source_locations(source: Source) -> dict[str, tuple[int, int]]:
    # json-source-map 1.0.5's calculate() turns entries into a dict before correctly
    # escaping/decoding keys. Distinct paths can collide. Its underlying parser gives
    # ordered entries; pair those with our validated tree's preorder instead.
    offset = int(source.text.startswith(BOM))
    try:
        entries = list(
            json_source_map.handle.value(
                source=source.text[offset:],
                current_location=json_source_map.types.Location(0, 0, 0),
            )
        )
    except (ValueError, RecursionError, IndexError) as exc:
        raise InputError("JSON source-map calculation failed") from exc
    if len(entries) != len(source.values):
        raise InputError("JSON source-map/tree entry count mismatch")
    result = {}
    for pointer, (_, entry) in zip(source.values, entries, strict=True):
        if entry.key_start is not None:
            key = json.loads(
                source.text[entry.key_start.position + offset : entry.key_end.position + offset]
            )
            if key != jsonpointer.JsonPointer(pointer).parts[-1]:
                raise InputError("JSON source-map/tree key mismatch")
        result[pointer] = (entry.value_start.position + offset, entry.value_end.position + offset)
    return result


def _validate_rules(rules: Rules, format: str) -> None:
    if not isinstance(rules, Rules):
        raise InputError("rules must be a Rules instance")
    for name in ("keep", "keep_lines", "priority_keys", "keep_patterns"):
        if not isinstance(getattr(rules, name), (tuple, list)):
            raise InputError(f"rules.{name} must be a tuple or list")
    if type(rules.context_lines) is not int or not 0 <= rules.context_lines <= 10:
        raise InputError("context_lines must be an integer from 0 to 10")
    if any(not isinstance(key, str) for key in rules.priority_keys):
        raise InputError("priority_keys must contain strings")
    if format == "json" and rules.keep_lines:
        raise InputError("keep_lines is only valid for log input")
    if format == "log" and (rules.keep or rules.keep_patterns):
        raise InputError("keep pointers are only valid for JSON input")
    if len(rules.keep_patterns) > 64:
        raise InputError("at most 64 keep patterns are supported")
    for pointer in (*rules.keep, *rules.keep_patterns):
        if not isinstance(pointer, str):
            raise InputError("keep pointers must be strings")
        if len(pointer) > MAX_POINTER:
            raise InputError(f"JSON pointer exceeds {MAX_POINTER} characters")
        try:
            jsonpointer.JsonPointer(pointer)
        except jsonpointer.JsonPointerException as exc:
            raise InputError(f"invalid JSON pointer: {pointer!r}") from exc
    for span in rules.keep_lines:
        if (
            not isinstance(span, (tuple, list))
            or len(span) != 2
            or any(type(v) is not int for v in span)
            or not 1 <= span[0] <= span[1]
        ):
            raise InputError("keep_lines must contain inclusive positive [start,end] pairs")


def _index_json(source: Source) -> list[str]:
    root = _strict_json(source.text)
    stack = [("", root, ())]
    leaves = []
    while stack:
        pointer, value, parents = stack.pop()
        if len(source.values) >= MAX_NODES:
            raise InputError(f"JSON exceeds {MAX_NODES} nodes")
        if len(parents) > MAX_DEPTH:
            raise InputError(f"JSON exceeds depth {MAX_DEPTH}")
        if len(pointer) > MAX_POINTER:
            raise InputError(f"JSON pointer exceeds {MAX_POINTER} characters")
        source.values[pointer] = value
        source.parents[pointer] = parents
        if isinstance(value, float) and not math.isfinite(value):
            raise InputError("JSON number exceeds finite floating-point range")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError as exc:
                raise InputError("JSON strings must not contain unpaired surrogates") from exc
        if isinstance(value, (dict, list)) and value:
            if len(source.values) + len(stack) + len(value) > MAX_NODES:
                raise InputError(f"JSON exceeds {MAX_NODES} nodes")
            entries = value.items() if isinstance(value, dict) else enumerate(value)
            children = []
            for key, child in entries:
                encoded = str(key).replace("~", "~0").replace("/", "~1")
                children.append((pointer + "/" + encoded, child, (*parents, pointer)))
            stack.extend(reversed(children))
        else:
            leaves.append(pointer)
    source.locations = _source_locations(source)
    return leaves


def _json_sections(
    source: Source, rules: Rules, budget: int
) -> tuple[list[Section], list[Section]]:
    leaves = _index_json(source)
    root = source.values[""]
    required = []
    keep = set(rules.keep)
    patterns = [jsonpointer.JsonPointer(pattern).parts for pattern in rules.keep_patterns]
    wanted_depths = {len(parts) for parts in patterns}
    indexed_parts = {
        pointer: jsonpointer.JsonPointer(pointer).parts
        for pointer in source.values
        if len(source.parents[pointer]) in wanted_depths
    }
    for pattern, parts in zip(rules.keep_patterns, patterns, strict=True):
        matches = {
            pointer
            for pointer, actual in indexed_parts.items()
            if len(parts) == len(actual)
            and all(
                wanted == "*" or wanted == found
                for wanted, found in zip(parts, actual, strict=True)
            )
        }
        if not matches:
            raise InputError(f"required keep pattern matched no source paths: {pattern!r}")
        keep.update(matches)
    for pointer in keep:
        try:
            jsonpointer.resolve_pointer(root, pointer)
        except (jsonpointer.JsonPointerException, ValueError, IndexError, OverflowError) as exc:
            raise InputError(f"required pointer is not present in source: {pointer!r}") from exc
        if pointer not in source.locations:
            raise InputError(f"required pointer is not canonical: {pointer!r}")
        start, end = source.locations[pointer]
        required.append(Section(start, end, pointer))
    required = _merge(required, "json")
    required_starts = [section.start for section in required]

    # Let upstream do its native schema/array/text-aware selection. Join unchanged,
    # unique scalar values back to source as *preferences*, never as provenance.
    # In particular, sampled array indices in the proposal are never source indices.
    proposal = shrinkfit.shrink_with_report(
        source.text.removeprefix(BOM),
        max(256, min(budget * 3, 32_768)),
        indent=0,
        format="json",
        input_format="json",
        jev=False,
    )

    def signature(pointer: str, value: Any) -> tuple[str, str, str]:
        parts = jsonpointer.JsonPointer(pointer).parts
        parent = source.values.get(source.parents[pointer][-1]) if source.parents[pointer] else None
        key = "*" if isinstance(parent, list) else (parts[-1] if parts else "")
        return key, _type(value), _dump(value)

    signatures = {p: signature(p, source.values[p]) for p in leaves}
    counts = Counter(signatures.values())
    proposed_scalars = set()
    todo = [("", json.loads(proposal.text))]
    while todo:
        key, value = todo.pop()
        if isinstance(value, dict):
            todo.extend(value.items())
        elif isinstance(value, list):
            todo.extend(("*", child) for child in value)
        else:
            proposed_scalars.add((key, _type(value), _dump(value)))
    hints = {p for p, sig in signatures.items() if counts[sig] == 1 and sig in proposed_scalars}
    priorities = {key.casefold() for key in rules.priority_keys}
    source.record_states = {
        pointer: state
        for pointer, value in source.values.items()
        if (state := _record_state(value)) is not None
    }
    failure_roots = {pointer for pointer, state in source.record_states.items() if state}
    source.failure_roots = failure_roots
    candidates = []
    for pointer in leaves:
        start, end = source.locations[pointer]
        if _covered(required, required_starts, start, end):
            continue
        parts = jsonpointer.JsonPointer(pointer).parts
        score = 40 * (pointer in hints) + 50 * (len(parts) <= 1)
        if any(part.casefold() in priorities for part in parts):
            score += 1000
        value = source.values[pointer]
        record = next(
            (p for p in reversed(source.parents[pointer]) if p in source.record_states), None
        )
        failed_record = record is not None and source.record_states[record]
        if failed_record:
            score += 600
            if parts[-1].casefold() == "nodeid" or (
                parts[-1].casefold() in {"message", "reason"}
                and isinstance(value, str)
                and (
                    len(value) > 32
                    or source.parents[pointer][-1] in failure_roots
                    or (len(parts) > 1 and parts[-2] == "reprcrash")
                )
            ):
                score += 2000
            if parts[-1].casefold() in {"nodeid", "outcome", "when", "message", "path", "lineno"}:
                score += 600
        if isinstance(value, str) and ERROR.search(value):
            score += 1000
        if parts and parts[-1] in {"outcome", "status"} and value in ("failed", "error", "failure"):
            score += 600
        if parts and parts[-1].casefold() in {"exit_code", "exitcode", "returncode"}:
            score += 4000 if value else 100
        if parts and parts[-1] == "isError":
            score += 3000
        if parts and parts[-1].casefold() in {
            "id",
            "name",
            "status",
            "code",
            "message",
            "nextcursor",
        }:
            score += 20
        candidates.append(Section(start, end, pointer, score=score))
    return required, candidates


def _rank_sections(source: Source, candidates: list[Section]) -> list[Section]:
    """Round-robin peer failure records so one verbose traceback cannot take every slot."""
    tiers: dict[int, dict[str, deque[Section]]] = defaultdict(lambda: defaultdict(deque))
    for candidate in sorted(candidates, key=lambda s: (-s.score, s.start)):
        group = ""
        if candidate.pointer is not None:
            record = next(
                (
                    p
                    for p in reversed(source.parents[candidate.pointer])
                    if p in source.record_states
                ),
                None,
            )
            group = record if record is not None and source.record_states[record] else ""
        tiers[candidate.score // 1000][group].append(candidate)
    result = []
    for tier in sorted(tiers, reverse=True):
        active = sorted(tiers[tier].values(), key=lambda queue: (-queue[0].score, queue[0].start))
        while active:
            remaining = []
            for queue in active:
                result.append(queue.popleft())
                if queue:
                    remaining.append(queue)
            active = remaining
    return result


def _fragments(source: Source, section: Section, budget: int) -> list[Section]:
    """Exact source excerpts for oversized scalars/lines, never fabricated JSON values."""
    length = section.end - section.start
    width = max(48, min(240, budget // 2))
    if length <= 48:
        return []
    spans = []
    match = ERROR.search(source.text, section.start, section.end)
    if length > width:
        spans = [(section.start, min(section.end, section.start + width))]
        spans.append((max(section.start, section.end - width), section.end))
        if match:
            start = max(section.start, match.start() - width // 4)
            end = min(section.end, start + width)
            spans.insert(0, (start, end))
    # Keep a budget-independent minimum so retrying a BudgetError's needed count
    # does not move the goalpost by making the smallest excerpt larger.
    tiny_start = max(section.start, match.start() - 12) if match else section.start
    spans.append((tiny_start, min(section.end, tiny_start + 48)))
    return [
        Section(start, end, section.pointer, False, section.lines, section.score - 1 - i)
        for i, (start, end) in enumerate(dict.fromkeys(spans))
    ]


def _log_sections(source: Source, rules: Rules, budget: int) -> tuple[list[Section], list[Section]]:
    lines = source.text.splitlines(keepends=True)
    if len(lines) > MAX_LINES:
        raise InputError(f"log exceeds {MAX_LINES} lines")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    required = []
    for start, end in rules.keep_lines:
        if end > len(lines):
            raise InputError(f"required line {end} exceeds source line count {len(lines)}")
        required.append(Section(offsets[start - 1], offsets[end], lines=(start, end)))
    required = _merge(required, "log")
    required_starts = [section.start for section in required]
    proposal = shrinkfit.shrink_with_report(
        json.dumps(source.text),
        max(256, min(budget * 3, 32_768)),
        indent=0,
        format="json",
        input_format="json",
        jev=False,
    )
    preview = json.loads(proposal.text)
    hints = set(preview.splitlines()) if isinstance(preview, str) else set()
    scores: dict[int, int] = defaultdict(int)
    for i, line in enumerate(lines):
        clean = strip_ansi(line)
        if ERROR.search(clean):
            scores[i] = max(scores[i], 1000)
            for j in range(
                max(0, i - rules.context_lines), min(len(lines), i + rules.context_lines + 1)
            ):
                scores[j] = max(scores[j], 500)
        if SUMMARY.search(clean):
            scores[i] = max(scores[i], 1250)
        if line.rstrip("\r\n") in hints:
            scores[i] = max(scores[i], 100)
        if i < 2 or i >= len(lines) - 4:
            scores[i] = max(scores[i], 200)
    candidates = [
        Section(offsets[i], offsets[i + 1], lines=(i + 1, i + 1), score=scores[i])
        for i in range(len(lines))
        if not _covered(required, required_starts, offsets[i], offsets[i + 1])
    ]
    return required, candidates


def _covered(sections: list[Section], starts: list[int], start: int, end: int) -> bool:
    index = bisect_right(starts, start) - 1
    return index >= 0 and sections[index].end >= end


def _merge(sections: list[Section], format: str) -> list[Section]:
    result: list[Section] = []
    for section in sorted(sections, key=lambda s: (s.start, -s.end)):
        if result and section.start < result[-1].end:
            if section.end <= result[-1].end:
                continue
            if format != "log" and section.pointer != result[-1].pointer:
                raise InputError("overlapping JSON source sections")
        if (
            result
            and section.start <= result[-1].end
            and (
                (
                    format == "log"
                    and (section.start < result[-1].end or section.complete == result[-1].complete)
                )
                or (format == "json" and section.pointer == result[-1].pointer)
            )
        ):
            last = result.pop()
            line_span = (last.lines[0], section.lines[1]) if last.lines and section.lines else None
            result.append(
                Section(
                    last.start,
                    max(last.end, section.end),
                    last.pointer,
                    last.complete and section.complete,
                    line_span,
                )
            )
        else:
            result.append(section)
    return result


def _document(source: Source, sections: list[Section], encoding: str) -> dict[str, Any]:
    sections = _merge(sections, source.format)
    blocks = []
    shown = set()
    omitted = []
    cursor = 0
    for section in sections:
        start, end = source.byte_at[section.start], source.byte_at[section.end]
        if cursor < start:
            omitted.append([cursor, start])
        cursor = end
        block: dict[str, Any] = {
            "bytes": [start, end],
            "text": source.text[section.start : section.end],
            "complete": section.complete,
        }
        if section.pointer is not None:
            block["pointer"] = section.pointer
            if not section.complete:
                node_start, node_end = source.locations[section.pointer]
                block["node_bytes"] = [source.byte_at[node_start], source.byte_at[node_end]]
            shown.update(source.parents[section.pointer])
            if isinstance(source.values[section.pointer], (dict, list)):
                shown.add(section.pointer)
        if section.lines is not None:
            block["lines"] = list(section.lines)
        blocks.append(block)
    if cursor < len(source.raw):
        omitted.append([cursor, len(source.raw)])
    structure = []
    if source.format == "json":
        shown.add("")
        for pointer in sorted(shown, key=lambda p: source.locations[p][0]):
            value = source.values[pointer]
            shape = {"pointer": pointer, "type": _type(value)}
            if isinstance(value, (dict, list)):
                shape["size"] = len(value)
            structure.append(shape)
    return {
        "schema": SCHEMA,
        "format": source.format,
        "source": {"sha256": source.digest, "bytes": len(source.raw)},
        "offsets": OFFSETS,
        "encoding": encoding,
        "notice": NOTICE,
        "truncated": bool(omitted),
        "structure": structure,
        "blocks": blocks,
        "omitted": omitted,
    }


def summarize(
    data: str | bytes,
    *,
    budget: int,
    format: Literal["json", "log"],
    rules: Rules = DEFAULT_RULES,
    encoding: str = "cl100k_base",
) -> Summary:
    """Return a complete compact JSON envelope that fits ``budget`` encoded tokens.

    No synthetic upstream preview value is emitted as evidence. Source data is never sent
    to a model. The tokenizer may download its public encoding table on its first use.
    """
    if type(budget) is not int or not 1 <= budget <= MAX_BUDGET:
        raise InputError(f"budget must be an integer from 1 to {MAX_BUDGET}")
    if not isinstance(format, str) or format not in {"json", "log"}:
        raise InputError("format must be json or log")
    if encoding not in ENCODINGS:
        raise InputError(f"encoding must be one of {ENCODINGS}")
    _validate_rules(rules, format)
    raw, text = _raw_source(data)
    source = Source(raw, text, format, digest=hashlib.sha256(raw).hexdigest())
    required, candidates = (
        _json_sections(source, rules, budget)
        if format == "json"
        else _log_sections(source, rules, budget)
    )
    tokenizer = tiktoken.get_encoding(encoding)

    def measure(sections: list[Section]) -> Summary:
        document = _document(source, sections, encoding)
        result_text = _dump(document)
        return Summary(result_text, len(tokenizer.encode_ordinary(result_text)), document)

    # Limit original source sections, retaining each section's bounded fallback excerpts.
    # A second global cap on expanded candidates could otherwise discard every fragment
    # behind a thousand oversized full values.
    options = [
        [candidate, *_fragments(source, candidate, budget)]
        for candidate in _rank_sections(source, candidates)[:1024]
    ]
    positions = {p for group in options for s in group for p in (s.start, s.end)}
    positions.update(p for s in required for p in (s.start, s.end))
    document_start = int(format == "json" and text.startswith(BOM))
    positions.update((0, document_start, len(text)))
    source.index_bytes(positions)
    # Try the complete original when modest; do not tokenize megabytes just to reject them.
    if len(text) <= max(1024, budget * 6):
        whole = Section(
            document_start,
            len(text),
            "" if format == "json" else None,
            lines=(1, len(text.splitlines())) if format == "log" and text else None,
        )
        full = measure([whole] if text else [])
        if full.tokens <= budget:
            return full
    selected = _merge(required, format)
    result = measure(selected)
    if result.tokens > budget and (selected or not raw):
        raise BudgetError(budget, result.tokens)

    seen_text: set[str] = set()
    least_nonempty_tokens = None
    failed_trials = 0
    saturated = False
    for group in options:
        for candidate in group:
            if any(s.start <= candidate.start and candidate.end <= s.end for s in selected):
                continue
            if candidate.end - candidate.start > max(512, budget * 3):
                continue
            excerpt = source.text[candidate.start : candidate.end]
            if len(excerpt) >= 64 and excerpt in seen_text:
                continue
            trial = measure([*selected, candidate])
            if not selected:
                least_nonempty_tokens = min(least_nonempty_tokens or trial.tokens, trial.tokens)
            if trial.tokens <= budget:
                selected.append(candidate)
                result = trial
                seen_text.add(excerpt)
                failed_trials = 0
                if candidate.complete:
                    break
            else:
                failed_trials += 1
                # Exact accounting still decides whether each candidate is kept. Bound optional searching
                # after sustained failure near the cap; hard pins were already reserved.
                if failed_trials >= 128 and budget - result.tokens < 64:
                    saturated = True
                    break
        if saturated:
            break
    if raw and not result.document["blocks"]:
        raise BudgetError(budget, least_nonempty_tokens or budget + 1)
    return result


def recover(data: str | bytes, document: dict[str, Any], start: int, end: int) -> bytes:
    """Recover an exact byte range only when the original digest and size still match."""
    raw, _ = _raw_source(data)
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise InputError("unrecognized summary schema")
    expected = document.get("source")
    if (
        not isinstance(expected, dict)
        or type(expected.get("bytes")) is not int
        or expected.get("bytes") != len(raw)
        or expected.get("sha256") != hashlib.sha256(raw).hexdigest()
    ):
        raise InputError("source changed: byte length or SHA-256 does not match summary")
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(raw):
        raise InputError("range must satisfy 0 <= start <= end <= source byte length")
    if any(position < len(raw) and raw[position] & 0xC0 == 0x80 for position in (start, end)):
        raise InputError("range splits a UTF-8 character")
    result = raw[start:end]
    try:
        result.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError("range splits a UTF-8 character") from exc
    return result
