"""Verify evidence against the original, without rerunning selection or a backend tool."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from typing import Any

import tiktoken

from .core import (
    BOM,
    ENCODINGS,
    MAX_BUDGET,
    MAX_LINES,
    NOTICE,
    OFFSETS,
    SCHEMA,
    BudgetError,
    InputError,
    Source,
    _index_json,
    _raw_source,
    _strict_json,
    _type,
)

TOP_KEYS = frozenset(
    {
        "schema",
        "format",
        "source",
        "offsets",
        "encoding",
        "notice",
        "truncated",
        "structure",
        "blocks",
        "omitted",
    }
)


def _range(value: Any, size: int) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(v) is not int for v in value)
        or not 0 <= value[0] < value[1] <= size
    ):
        raise InputError("evidence range must be a nonempty [start,end] within source bytes")
    return value[0], value[1]


def verify(data: str | bytes, serialized: str | bytes, *, budget: int | None = None) -> int:
    """Verify exact bytes, pointer/line identities, structure and the omission partition.

    Return the token count of the supplied serialization, including its whitespace.
    ``budget`` checks that exact representation, not a reserialized approximation.
    """
    if budget is not None and (type(budget) is not int or not 1 <= budget <= MAX_BUDGET):
        raise InputError(f"budget must be an integer from 1 to {MAX_BUDGET}")
    raw, text = _raw_source(data)
    _, serialized_text = _raw_source(serialized)
    document = _strict_json(serialized_text)
    if not isinstance(document, dict) or document.keys() != TOP_KEYS:
        raise InputError("summary must contain exactly the tool-sift/v1 envelope fields")
    if document["schema"] != SCHEMA or document["format"] not in ("json", "log"):
        raise InputError("unrecognized summary schema or format")
    if document["offsets"] != OFFSETS or document["notice"] != NOTICE:
        raise InputError("summary offset units or incompleteness notice were changed")
    encoding = document["encoding"]
    if encoding not in ENCODINGS:
        raise InputError("unsupported summary encoding")
    expected_source = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    source_info = document["source"]
    if (
        not isinstance(source_info, dict)
        or type(source_info.get("bytes")) is not int
        or source_info != expected_source
    ):
        raise InputError("source changed: byte length or SHA-256 does not match summary")
    if type(document["truncated"]) is not bool or any(
        not isinstance(document[key], list) for key in ("blocks", "omitted", "structure")
    ):
        raise InputError("invalid summary collection or truncation flag type")
    source = Source(raw, text, document["format"], digest=expected_source["sha256"])
    line_starts = []
    line_start_set = set()
    line_ends = set()
    if source.format == "json":
        _index_json(source)
        source.index_bytes({v for bounds in source.locations.values() for v in bounds})
    else:
        lines = text.splitlines(keepends=True)
        if len(lines) > MAX_LINES:
            raise InputError(f"log exceeds {MAX_LINES} lines")
        cursor = 0
        for line in lines:
            line_starts.append(cursor)
            line_start_set.add(cursor)
            cursor += len(line.encode("utf-8"))
            line_ends.add(cursor)
    cursor = 0
    expected_omitted = []
    scopes = {""} if source.format == "json" else set()
    for block in document["blocks"]:
        if (
            not isinstance(block, dict)
            or type(block.get("complete")) is not bool
            or not isinstance(block.get("text"), str)
        ):
            raise InputError("invalid evidence block")
        keys = {"bytes", "text", "complete", "pointer" if source.format == "json" else "lines"}
        if source.format == "json" and not block["complete"]:
            keys.add("node_bytes")
        if block.keys() != keys:
            raise InputError("unexpected or missing evidence block fields")
        start, end = _range(block["bytes"], len(raw))
        if start < cursor:
            raise InputError("evidence blocks overlap or are out of source order")
        try:
            encoded_text = block["text"].encode("utf-8")
        except UnicodeError as exc:
            raise InputError("evidence text contains an unpaired Unicode surrogate") from exc
        if raw[start:end] != encoded_text:
            raise InputError("evidence text does not match its original byte range")
        if start > cursor:
            expected_omitted.append([cursor, start])
        cursor = end
        if source.format == "json":
            pointer = block["pointer"]
            if not isinstance(pointer, str) or pointer not in source.locations:
                raise InputError("evidence pointer does not identify an original JSON value")
            char_start, char_end = source.locations[pointer]
            bounds = [source.byte_at[char_start], source.byte_at[char_end]]
            if block["complete"]:
                if [start, end] != bounds and not (
                    pointer == ""
                    and start == (3 if text.startswith(BOM) else 0)
                    and end == len(raw)
                ):
                    raise InputError(
                        "complete JSON block does not match its original value boundary"
                    )
            else:
                if block["node_bytes"] != bounds or not bounds[0] <= start < end <= bounds[1]:
                    raise InputError("partial JSON block has an incorrect original value boundary")
                _range(block["node_bytes"], len(raw))
            scopes.update(source.parents[pointer])
            if isinstance(source.values[pointer], (dict, list)):
                scopes.add(pointer)
        else:
            expected_lines = [bisect_right(line_starts, start), bisect_left(line_starts, end)]
            if (
                block["lines"] != expected_lines
                or not isinstance(block["lines"], list)
                or any(type(v) is not int for v in block["lines"])
            ):
                raise InputError("log block has an incorrect original line range")
            if block["complete"] and (start not in line_start_set or end not in line_ends):
                raise InputError("complete log block cuts through a line")
    if cursor < len(raw):
        expected_omitted.append([cursor, len(raw)])
    for span in document["omitted"]:
        _range(span, len(raw))
    if document["omitted"] != expected_omitted or document["truncated"] != bool(expected_omitted):
        raise InputError("omitted ranges do not exactly complement retained source bytes")
    expected_structure = []
    for pointer in sorted(scopes, key=lambda p: source.locations[p][0]):
        value = source.values[pointer]
        shape = {"pointer": pointer, "type": _type(value)}
        if isinstance(value, (dict, list)):
            shape["size"] = len(value)
        expected_structure.append(shape)
    # Canonical JSON comparison distinguishes false/0 and integer/float metadata.
    if json.dumps(document["structure"], sort_keys=True) != json.dumps(
        expected_structure, sort_keys=True
    ):
        raise InputError("JSON structure does not match the original types, sizes, or scopes")
    tokens = len(tiktoken.get_encoding(encoding).encode_ordinary(serialized_text))
    if budget is not None and tokens > budget:
        raise BudgetError(budget, tokens)
    return tokens
