"""Typed evidence errors. Every invalid input maps to a stable ``code``."""
from __future__ import annotations

import errno as _errno

# Whitespace and line terminators as defined by ECMAScript (``\s``, ``trim()``).
# The report format and patch envelope rules were defined against these exact
# sets; Python's ``str.isspace`` differs (it also accepts U+001C..U+001F and
# U+0085, but not U+FEFF).
JS_WHITESPACE = ("\t\n\v\f\r         "
                 "         　﻿")


def js_trim(text: str) -> str:
    return text.strip(JS_WHITESPACE)


def utf16_slice(text: str, start: int, end: int | None = None) -> str:
    """Slice ``text`` by UTF-16 code units, as JavaScript's ``String.prototype.slice``."""
    data = text.encode("utf-16-le", "surrogatepass")
    stop = len(data) if end is None else 2 * end
    return data[2 * start:stop].decode("utf-16-le", "surrogatepass")


class FenceError(Exception):
    """Invalid or unsupported evidence. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def require_that(condition, code: str, message: str, details: dict | None = None) -> None:
    if not condition:
        raise FenceError(code, message, details)


# Descriptions of common operating-system errors, phrased like Node.js/libuv so
# that error codes and messages keep the original report format.
_DESCRIPTIONS = {
    "ENOENT": "no such file or directory",
    "EACCES": "permission denied",
    "EPERM": "operation not permitted",
    "ENOTDIR": "not a directory",
    "EISDIR": "illegal operation on a directory",
    "ELOOP": "too many symbolic links encountered",
    "ENAMETOOLONG": "name too long",
    "EMFILE": "too many open files",
    "ENXIO": "no such device or address",
    "EIO": "i/o error",
}


class SystemCallError(Exception):
    """An operating-system error with a Node.js-style ``code`` such as ``ENOENT``."""

    def __init__(self, error: OSError, syscall: str, path: str | None = None):
        number = error.errno if isinstance(error.errno, int) else None
        self.code = _errno.errorcode.get(number, "EIO") if number is not None else "EIO"
        description = _DESCRIPTIONS.get(self.code)
        if description is None:
            text = error.strerror or str(error)
            description = text[:1].lower() + text[1:]
        self.message = f"{self.code}: {description}, {syscall}" + (f" '{path}'" if path is not None else "")
        super().__init__(self.message)


def error_message(error: BaseException) -> str:
    message = getattr(error, "message", None)
    return message if isinstance(message, str) else str(error)
