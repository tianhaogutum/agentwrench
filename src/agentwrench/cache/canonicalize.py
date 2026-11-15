"""Opt-in canonicalization (tool order, schema key order) and safe capture writing."""

from __future__ import annotations

import copy
import errno
import os
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from .capture import Record, tool_identity
from .errors import CacheError, FsError
from .jscompat import is_object, js_object, prop, stringify, utf16_key

RULES: tuple[str, ...] = ("sort-tools", "sort-schema-keys")

# Traverse schema locations only. Unknown extension and instance values stay intact.
_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_VALUES = {
    "items",
    "additionalItems",
    "additionalProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
}
_SCHEMA_ARRAYS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def _sorted_keys(value: dict[str, Any]) -> list[str]:
    return sorted(value, key=utf16_key)


def _sort_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_sort_schema(v) for v in value]  # legacy tuple items
    if not is_object(value):
        return value
    entries = []
    for key in _sorted_keys(value):
        child = value[key]
        if key in _SCHEMA_MAPS and is_object(child):
            child = js_object((name, _sort_schema(child[name])) for name in _sorted_keys(child))
        elif key in _SCHEMA_VALUES:
            child = _sort_schema(child)
        elif key in _SCHEMA_ARRAYS and isinstance(child, list):
            child = [_sort_schema(v) for v in child]
        elif key == "dependencies" and is_object(child):
            child = js_object(
                (name, child[name] if isinstance(child[name], list) else _sort_schema(child[name]))
                for name in _sorted_keys(child)
            )
        entries.append((key, child))
    return js_object(entries)


def _is_named_function(tool: dict[str, Any], format: str) -> bool:
    if format == "anthropic":
        return isinstance(tool.get("name"), str) and ("type" not in tool or tool["type"] == "custom")
    if format == "openai-chat":
        return tool.get("type") == "function" and isinstance(prop(tool.get("function"), "name"), str)
    return tool.get("type") == "function" and isinstance(tool.get("name"), str)


def canonicalize(records: Sequence[Record], rules: Sequence[str] = RULES) -> list[dict[str, Any]]:
    """Return canonicalized copies of capture entries; inputs are never mutated."""
    if not rules or any(r not in RULES for r in rules) or len(set(rules)) != len(rules):
        raise CacheError("rules must be a nonempty, unique list of sort-tools,sort-schema-keys")
    entries = []
    for record in records:
        entry = copy.deepcopy(record.entry)
        body = entry["request"] if record.wrapped else entry
        tools = body.get("tools")
        if not tools and not isinstance(tools, list):
            entries.append(entry)
            continue
        if "sort-tools" in rules and any("cache_control" in t for t in tools):
            raise CacheError(
                f"line {record.line}: refusing to move tool-level cache_control boundaries; use --rules sort-schema-keys"
            )
        for tool in tools:
            if not _is_named_function(tool, record.format):
                raise CacheError(
                    f"line {record.line}: canonicalization supports named function tools only; "
                    "built-in/anonymous tools are analysis-only"
                )
            if "sort-schema-keys" in rules:
                if "input_schema" in tool:
                    tool["input_schema"] = _sort_schema(tool["input_schema"])
                if "parameters" in tool:
                    tool["parameters"] = _sort_schema(tool["parameters"])
                if is_object(tool.get("function")) and "parameters" in tool["function"]:
                    tool["function"]["parameters"] = _sort_schema(tool["function"]["parameters"])
        if "sort-tools" in rules:
            tools.sort(key=lambda t: utf16_key(tool_identity(t)))
        entries.append(entry)
    return entries


def write_capture(path: str, entries: Iterable[Any]) -> None:
    """Install a new JSONL file atomically; never overwrite or follow an existing path.

    ``link()`` installs a completed private temporary file without overwriting an
    existing path, following symlinks, or truncating input.
    """
    destination = os.path.abspath(os.fspath(path))
    temporary = os.path.join(os.path.dirname(destination), f".{os.path.basename(destination)}.{uuid.uuid4()}.tmp")
    fd: int | None = None
    created = False
    try:
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise FsError(error, "open", temporary) from None
        created = True
        for entry in entries:
            data = memoryview((stringify(entry) + "\n").encode("utf-8"))
            while data:
                try:
                    written = os.write(fd, data)
                except OSError as error:
                    raise FsError(error, "write") from None
                data = data[written:]
        try:
            os.fsync(fd)
        except OSError as error:
            raise FsError(error, "fsync") from None
        os.close(fd)
        fd = None
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except OSError as error:
            raise FsError(error, "link", temporary, destination) from None
    finally:
        if fd is not None:
            os.close(fd)
        if created:
            try:
                os.unlink(temporary)
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise FsError(error, "unlink", temporary) from None
