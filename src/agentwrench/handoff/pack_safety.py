"""Hashing, redaction, paths and JSON files for plain handoff packs (standard library only).

Port of the original relayproof ``safety.js``. Packs and run records are hashed
over a canonical JSON text and written with ``JSON.stringify(value, null, 2)``;
both are reproduced byte for byte through :mod:`agentwrench.cache.jscompat`, so
packs written by the Node original verify here and the other way round. String
lengths are UTF-16 code units, as in JavaScript.
"""

from __future__ import annotations

import hashlib
import math
import os
import posixpath
import re
import stat
from pathlib import Path
from typing import Any

from ..cache.jscompat import (
    JS_WHITESPACE,
    from_code_units,
    is_number,
    parse_json,
    quote,
    stringify,
    to_code_units,
    utf16_key,
    utf16_length,
    well_formed,
)
from .io import HandoffError, atomic_bytes

# Packs are bounded by the 16 MiB read limit of load_pack; never refuse a write the
# original could make.
MAX_WRITE = 1024 * 1024 * 1024


class SymlinkPathError(HandoffError):
    """A path or one of its ancestors is a symlink."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise HandoffError(message)


def sha256_hex(value: str | bytes) -> str:
    """``createHash('sha256').update(value)``: strings hash as UTF-8 (lone surrogates as U+FFFD)."""
    data = value if isinstance(value, bytes) else well_formed(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> str:
    """Keys sorted by UTF-16 code units, no whitespace, ``JSON.stringify`` scalars."""
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{quote(key)}:{canonical(value[key])}" for key in sorted(value, key=utf16_key)) + "}"
    return stringify(value)


def digest(value: Any) -> str:
    return sha256_hex(canonical(value))


_HASH = re.compile(r"[a-f0-9]{64}")


def is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def is_finite_number(value: Any) -> bool:
    """``Number.isFinite``."""
    return is_number(value) and (isinstance(value, int) or math.isfinite(value))


def is_integer(value: Any) -> bool:
    """``Number.isInteger``."""
    return is_number(value) and (isinstance(value, int) or (math.isfinite(value) and value.is_integer()))


_TRIM = re.compile(f"\\A[{JS_WHITESPACE}]+|[{JS_WHITESPACE}]+\\Z")


def js_trim(text: str) -> str:
    return _TRIM.sub("", text)


def js_slice(text: str, end: int) -> str:
    """``text.slice(0, end)`` in UTF-16 code units (may leave a lone surrogate)."""
    return from_code_units(to_code_units(text)[:end])


# --------------------------------------------------------------------------- redaction
# Defense in depth, not a general secret detector. Applied before size truncation.
# JavaScript regular expressions without the `u` flag: \w, \b and /i are ASCII-only,
# \s is the ECMAScript whitespace set, `$` is the end of input and `.` stops at
# \n, \r, U+2028 and U+2029.
_ASCII_I = re.ASCII | re.IGNORECASE
_WORDS = r"(?:password|passwd|secret|token|api[_-]?key|authorization|credential)"
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----(?s:.)*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)", re.ASCII),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{12,}"
                r"|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b", re.ASCII),
     "[REDACTED TOKEN]"),
    (re.compile(rf"\bBearer[{JS_WHITESPACE}]+[A-Za-z0-9._~+/=-]+", _ASCII_I), "Bearer [REDACTED]"),
    (re.compile(rf"((?:proxy-)?authorization[{JS_WHITESPACE}]*[:=][{JS_WHITESPACE}]*)[^\r\n]+", _ASCII_I),
     r"\g<1>[REDACTED]"),
    (re.compile(rf"((?:[\"']?)[\w.-]*{_WORDS}[\w.-]*(?:[\"']?)[{JS_WHITESPACE}]*[:=][{JS_WHITESPACE}]*)"
                rf"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^{JS_WHITESPACE},;\r\n]+)", _ASCII_I),
     r"\g<1>[REDACTED]"),
    (re.compile(rf"([a-z][a-z0-9+.-]*://)[^{JS_WHITESPACE}/@]+:[^{JS_WHITESPACE}/@]+@", _ASCII_I),
     r"\g<1>[REDACTED]@"),
    (re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"), ""),
    (re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"), ""),
)
_SECRET_FLAG = re.compile(rf"--?[\w.-]*{_WORDS}[\w.-]*", _ASCII_I)


def redact(value: object) -> str:
    text = str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact_command(command: list[str]) -> list[str]:
    hide_next = False
    result = []
    for arg in command:
        if hide_next:
            hide_next = False
            result.append("[REDACTED ARGUMENT]")
            continue
        if _SECRET_FLAG.fullmatch(arg):
            hide_next = True
        result.append(redact(arg))
    return result


def safe_name(value: str) -> str:
    clean = redact(value)
    return value if clean == value else f"{clean} [path-id:{sha256_hex(value)[:12]}]"


_ANY = "[^\n\r  ]"  # JavaScript `.`
_SENSITIVE_NAME = re.compile(
    rf"(?:\.env{_ANY}*|\.git|\.ssh|\.aws|\.azure|\.gnupg|\.npmrc|\.yarnrc(?:\.{_ANY}*)?|\.netrc|\.pypirc"
    rf"|credentials?(?:\.{_ANY}*)?|secrets?(?:\.{_ANY}*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.{_ANY}*)?)",
    _ASCII_I,
)
_SENSITIVE_SUFFIX = re.compile(r"\.(?:pem|key|p12|pfx|jks|keystore)\Z", _ASCII_I)


def sensitive_path(name: str) -> bool:
    return any(_SENSITIVE_NAME.fullmatch(part) or _SENSITIVE_SUFFIX.search(part) for part in re.split(r"[\\/]", name))


def clipped(value: object, maximum: int = 32000) -> str:
    clean = redact(value)
    return clean if utf16_length(clean) <= maximum else f"{js_slice(clean, maximum)}\n[TRUNCATED]"


# --------------------------------------------------------------------------- paths


def resolve(*segments: str) -> str:
    """Node's POSIX ``path.resolve``: absolute, normalized, trailing spaces kept."""
    joined = ""
    for segment in reversed((os.getcwd(), *map(str, segments))):
        if not segment:
            continue
        joined = f"{segment}/{joined}" if joined else segment
        if segment.startswith("/"):
            break
    parts: list[str] = []
    for part in joined.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def inside(root: str, candidate: str) -> bool:
    return candidate == root or candidate.startswith(root.rstrip("/") + "/")


def no_symlink_path(candidate: str | Path) -> str:
    """Reject symlinked ancestors, including a link that points back into the repository."""
    absolute = resolve(str(candidate))
    current = "/"
    for part in filter(None, absolute.split("/")):
        current = posixpath.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise SymlinkPathError("Symlink paths are not accepted")
    return absolute


def new_output(candidate: str | Path) -> str:
    """A new path whose parent directory exists, reached without symlinks."""
    absolute = no_symlink_path(candidate)
    require(stat.S_ISDIR(os.stat(posixpath.dirname(absolute)).st_mode), "Output parent must be an existing directory")
    require(not os.path.lexists(absolute), "Output already exists; choose a new path")
    return absolute


def read_json(filename: str | Path, max_bytes: int = 16 * 1024 * 1024) -> Any:
    absolute = no_symlink_path(filename)
    info = os.lstat(absolute)
    require(stat.S_ISREG(info.st_mode) and info.st_size <= max_bytes,
            "JSON input must be a regular file within the size limit")
    try:
        with open(absolute, "rb") as handle:
            text = handle.read().decode("utf-8", "replace")
        return parse_json(text)
    except (OSError, ValueError, RecursionError):
        raise HandoffError("Invalid JSON input") from None


def json_text(value: Any) -> str:
    """``JSON.stringify(value, null, 2) + '\\n'``."""
    return stringify(value, 2) + "\n"


def utf8(text: str) -> bytes:
    """Bytes Node writes for a string: UTF-8 with lone surrogates as U+FFFD."""
    return well_formed(text).encode("utf-8")


def write_new(filename: str, text: str) -> None:
    """Create a new private (0600) file; never replace one. Written atomically."""
    atomic_bytes(Path(filename), utf8(text), maximum=MAX_WRITE)
