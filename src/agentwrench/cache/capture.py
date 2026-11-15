"""Bounded capture reading, parsing, format detection and request validation."""

from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass
from typing import Any

from .errors import CacheError, FsError
from .jscompat import (
    JSONSyntaxError,
    is_blank,
    is_object,
    is_safe_integer,
    parse_json,
    prop,
)

FORMATS: tuple[str, ...] = ("auto", "anthropic", "openai-chat", "openai-responses")


@dataclass(frozen=True)
class Limits:
    file_bytes: int = 32 * 1024 * 1024
    line_bytes: int = 4 * 1024 * 1024
    records: int = 10000
    depth: int = 64
    nodes: int = 250000


LIMITS = Limits()


@dataclass
class Record:
    """One parsed capture line. ``entry`` is the original JSON; ``body`` the request."""

    entry: dict[str, Any]
    body: dict[str, Any]
    wrapped: bool
    usage: Any
    line: int
    format: str


def pointer(key: Any) -> str:
    """Escape one RFC 6901 reference token."""
    return str(key).replace("~", "~0").replace("/", "~1")


def utf8_length(text: str) -> int:
    """``Buffer.byteLength(text)``; lone surrogates count as 3-byte U+FFFD."""
    return len(text.encode("utf-8", "surrogatepass"))


def _check_tree(value: Any) -> None:
    count = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        count += 1
        if count > LIMITS.nodes or depth > LIMITS.depth:
            raise CacheError("JSON exceeds node/depth limit")
        if isinstance(current, float) and not math.isfinite(current):
            raise CacheError("non-finite JSON number")
        if isinstance(current, list):
            stack.extend((child, depth + 1) for child in reversed(current))
        elif isinstance(current, dict):
            stack.extend((child, depth + 1) for child in reversed(list(current.values())))


def detect_format(body: dict[str, Any]) -> str:
    if "input" in body:
        return "openai-responses"
    if not isinstance(body.get("messages"), list):
        raise CacheError("unsupported request: expected messages or input")
    model, tools = body.get("model"), body.get("tools")
    if (
        "system" in body
        or (isinstance(model, str) and model.startswith("claude-"))
        or (isinstance(tools, list) and any(is_object(t) and "input_schema" in t for t in tools))
    ):
        return "anthropic"
    return "openai-chat"


def tool_identity(tool: Any) -> Any:
    """``tool.function?.name ?? tool.name ?? tool.type``."""
    for value in (prop(prop(tool, "function"), "name"), prop(tool, "name"), prop(tool, "type")):
        if value is not None:
            return value
    return None


def _validate(body: dict[str, Any], format: str) -> None:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise CacheError("model must be a nonempty string")
    for key in ("previous_response_id", "conversation", "prompt", "functions", "function_call"):
        if body.get(key) is not None:
            raise CacheError(f"unsupported /{key}: supply complete, expanded request history and modern tools")
    if "tools" in body:
        tools = body["tools"]
        if not isinstance(tools, list):
            raise CacheError("tools must be an array")
        names: set[str] = set()
        for tool in tools:
            identity = tool_identity(tool) if is_object(tool) else None
            if not isinstance(identity, str) or not identity:
                raise CacheError("each tool needs a name, function.name, or type")
            if identity in names:
                raise CacheError("duplicate tool identities are unsupported")
            names.add(identity)
    if format == "openai-responses":
        if not isinstance(body.get("input"), (str, list)):
            raise CacheError("Responses input must be a string or array")
        instructions = body.get("instructions")
        if instructions is not None and not isinstance(instructions, (str, list)):
            raise CacheError("instructions must be a string or array")
        if isinstance(body["input"], list) and any(not is_object(v) for v in body["input"]):
            raise CacheError("input items must be objects")
    else:
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise CacheError("messages must be an array")
        if any(not is_object(m) or not isinstance(m.get("role"), str) or not m["role"] for m in messages):
            raise CacheError("message items need a role")
        for message in messages:
            content = message.get("content")
            if content is not None and not isinstance(content, (str, list)):
                raise CacheError("message content must be string, block array, or null")
        system = body.get("system")
        if format == "anthropic" and system is not None and not isinstance(system, (str, list)):
            raise CacheError("system must be string or block array")


_COUNTERS = (
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "prompt_tokens",
    "input_tokens",
    "cached_tokens",
    "cache_write_tokens",
)


def _validate_usage(usage: dict[str, Any]) -> None:
    groups = [usage]
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        if details is not None and not is_object(details):
            raise CacheError(f"usage.{key} must be an object")
        if details is not None:
            groups.append(details)
    for group in groups:
        for key in _COUNTERS:
            if key in group and (not is_safe_integer(group[key]) or group[key] < 0):
                raise CacheError(f"usage.{key} must be a nonnegative safe integer")


def parse_capture(text: str, format: str = "auto") -> list[Record]:
    """Parse JSONL capture text into validated records (physical line numbers)."""
    if format not in FORMATS:
        raise CacheError(f"unknown format: {format}")
    if utf8_length(text) > LIMITS.file_bytes:
        raise CacheError("capture exceeds 32 MiB limit")
    records: list[Record] = []
    detected: str | None = None
    for index, raw in enumerate(text.split("\n")):
        line = raw[:-1] if raw.endswith("\r") else raw
        if is_blank(line):
            continue
        try:
            if utf8_length(line) > LIMITS.line_bytes:
                raise CacheError("line exceeds 4 MiB limit")
            if len(records) >= LIMITS.records:
                raise CacheError("capture exceeds 10000 records")
            try:
                entry = parse_json(line)
            except JSONSyntaxError:
                raise CacheError("malformed JSON") from None
            _check_tree(entry)
            if not is_object(entry):
                raise CacheError("record must be an object")
            wrapped = "request" in entry
            body = entry["request"] if wrapped else entry
            if not is_object(body):
                raise CacheError("request must be an object")
            current = detect_format(body) if format == "auto" else format
            if format == "auto" and detected and current != detected:
                raise CacheError("mixed formats; use --format only if a single interpretation is intended")
            _validate(body, current)
            detected = current
            usage = entry.get("usage") if wrapped else None
            if wrapped and usage is not None and not is_object(usage):
                raise CacheError("usage must be an object")
            if wrapped and usage is not None:
                _validate_usage(usage)
            records.append(Record(entry, body, wrapped, usage, index + 1, current))
        except CacheError as error:
            raise CacheError(f"line {index + 1}: {error.message}") from None
    if not records:
        raise CacheError("capture contains no requests")
    return records


def _read_stable(fd: int, size: int, capacity: int) -> bytes:
    chunks: list[bytes] = []
    length = 0
    while length < capacity:
        chunk = os.read(fd, min(capacity - length, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        length += len(chunk)
    data = b"".join(chunks)
    if len(data) > size:
        raise _Grew()
    return data


class _Grew(CacheError):
    pass


def decode_utf8(data: bytes, message: str) -> str:
    """``new TextDecoder('utf-8', { fatal: true }).decode`` (strips one leading BOM)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CacheError(message) from None


def read_capture(path: str, format: str = "auto") -> list[Record]:
    """Read and parse a capture file with the same limits as the Node tool."""
    display = os.fspath(path)
    try:
        fd = os.open(display, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as error:
        raise FsError(error, "open", display) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CacheError("input must be a regular file")
        if info.st_size > LIMITS.file_bytes:
            raise CacheError("capture exceeds 32 MiB limit")
        try:
            data = _read_stable(fd, info.st_size, min(info.st_size + 1, LIMITS.file_bytes + 1))
        except _Grew:
            raise CacheError("input grew during read; retry with a stable capture") from None
    finally:
        os.close(fd)
    return parse_capture(decode_utf8(data, "input is not valid UTF-8"), format)
