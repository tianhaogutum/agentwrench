"""Exact RFC 6901 volatile-field allowlists and analysis-only scalar masking."""

from __future__ import annotations

import copy
import dataclasses
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .capture import Record, _Grew, _read_stable, decode_utf8
from .errors import CacheError, FsError
from .jscompat import JSONSyntaxError, is_number, parse_json, quote, utf16_length

_BAD_ESCAPE = re.compile(r"~(?![01])")
_INDEX = re.compile(r"0|[1-9][0-9]*")
_MAX_FILE_BYTES = 65536


@dataclass(frozen=True)
class Field:
    path: str
    keys: tuple[str, ...]


def _is_structural(path: str, keys: tuple[str, ...]) -> bool:
    """These values determine segment kinds, model partitions and tool identities."""
    return (
        path == "/model"
        or (keys[0] in ("messages", "input") and len(keys) == 3 and keys[2] == "role")
        or (
            keys[0] == "tools"
            and (
                (len(keys) == 3 and keys[2] in ("name", "type"))
                or (len(keys) == 4 and keys[2:] == ("function", "name"))
            )
        )
    )


def parse_fields(fields: Any) -> list[Field]:
    """Validate an allowlist: an array of at most 128 unique, non-root pointers."""
    if not isinstance(fields, (list, tuple)) or len(fields) > 128:
        raise CacheError("volatile fields must be an array of at most 128 unique JSON pointers")
    seen: set[str] = set()
    parsed = []
    for path in fields:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or utf16_length(path) > 1024
            or _BAD_ESCAPE.search(path)
        ):
            raise CacheError("invalid volatile field: expected a non-root RFC 6901 pointer of at most 1024 characters")
        keys = tuple(k.replace("~1", "/").replace("~0", "~") for k in path[1:].split("/"))
        if len(keys) > 64 or path in seen:
            raise CacheError("volatile fields must be unique and at most 64 levels deep")
        seen.add(path)
        if _is_structural(path, keys):
            raise CacheError(f"volatile field {quote(path)} is a protected structural field")
        parsed.append(Field(path, keys))
    return parsed


def read_volatile_fields(path: str) -> list[Any]:
    """Read and validate a volatile-fields JSON file (at most 64 KiB)."""
    display = os.fspath(path)
    try:
        fd = os.open(display, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as error:
        raise FsError(error, "open", display, reading=True) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CacheError("volatile fields input must be a regular file")
        if info.st_size > _MAX_FILE_BYTES:
            raise CacheError("volatile fields file exceeds 64 KiB limit")
        try:
            data = _read_stable(fd, info.st_size, info.st_size + 1)
        except _Grew:
            raise CacheError("volatile fields input grew during read; retry with a stable file") from None
        text = decode_utf8(data, "volatile fields file is not valid UTF-8")
        try:
            fields = parse_json(text)
        except JSONSyntaxError:
            raise CacheError("volatile fields file contains malformed JSON") from None
        parse_fields(fields)
        return fields
    finally:
        os.close(fd)


@dataclass
class _Target:
    parent: Any
    key: str | int
    value: Any


def _locate(body: Any, keys: tuple[str, ...]) -> _Target | None:
    """Resolve own properties only: no prototypes, pseudo-properties, leading zeros or '-'."""
    parent = body
    for i, key in enumerate(keys):
        slot: str | int
        if isinstance(parent, dict):
            if key not in parent:
                return None
            slot = key
        elif isinstance(parent, list):
            if not _INDEX.fullmatch(key) or int(key) >= len(parent):
                return None
            slot = int(key)
        else:
            return None
        if i == len(keys) - 1:
            return _Target(parent, slot, parent[slot])
        parent = parent[slot]
    return None


def _scalar_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    return "number" if is_number(value) else "string"


def _masked(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return False
    return 0 if is_number(value) else ""


@dataclass
class Projection:
    records: list[Record]
    fields: list[dict[str, Any]]
    changes: list[list[dict[str, str]]]


def mask_volatile_fields(records: list[Record], pointers: Any, modeled: Iterable[str]) -> Projection:
    """Clone bodies and mask allowlisted scalars; report per-pair known-volatile changes."""
    fields = parse_fields(pointers)
    modeled = set(modeled)
    for field in fields:
        if field.keys[0] not in modeled:
            raise CacheError(f"volatile field {quote(field.path)} is unmodeled for this format")
    matched = [0] * len(fields)
    observations: list[list[tuple[Any, str] | None]] = []
    projected = records
    if fields:
        projected = []
        # Clone bodies only: the capture entries, wrapper metadata and usage stay intact.
        for record in records:
            body = copy.deepcopy(record.body)
            seen: list[tuple[Any, str] | None] = []
            for i, field in enumerate(fields):
                target = _locate(body, field.keys)
                if target is None:
                    seen.append(None)
                    continue
                if isinstance(target.value, (dict, list)):
                    raise CacheError(
                        f"line {record.line}: volatile field {quote(field.path)} must target a scalar, not an object or array"
                    )
                matched[i] += 1
                target.parent[target.key] = _masked(target.value)
                seen.append((target.value, _scalar_type(target.value)))
            observations.append(seen)
            projected.append(dataclasses.replace(record, body=body))
    for field, count in zip(fields, matched):
        if not count:
            raise CacheError(f"volatile field {quote(field.path)} matches no request scalar")
    changes = []
    for previous_row, current_row in zip(observations, observations[1:]):
        changes.append(
            [
                {"path": field.path, "classification": "known_volatile"}
                for field, previous, current in zip(fields, previous_row, current_row)
                if previous and current and previous[1] == current[1] and previous[0] != current[0]
            ]
        )
    return Projection(
        projected, [{"path": f.path, "matchedRequests": count} for f, count in zip(fields, matched)], changes
    )
