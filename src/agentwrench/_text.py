"""Small helpers for human-readable output shared by the subcommands."""
from __future__ import annotations


def plural(count: int, noun: str, plural_form: str | None = None) -> str:
    """``plural(1, "issue")`` -> ``"1 issue"``; ``plural(2, "issue")`` -> ``"2 issues"``."""
    return f"{count} {noun if count == 1 else plural_form or noun + 's'}"


_OS_ERRORS = {
    "ENOENT": "no such file",
    "EACCES": "permission denied",
    "EPERM": "permission denied",
    "EISDIR": "is a directory",
    "ENOTDIR": "a parent is not a directory",
    "ELOOP": "is a symlink (not followed)",
    "ENAMETOOLONG": "name too long",
}


def os_reason(error: BaseException) -> str:
    """Short, path-free reason for an OS error: ``no such file``, ``permission denied``..."""
    import errno
    import os

    number = getattr(error, "errno", None)
    code = errno.errorcode.get(number) if isinstance(number, int) else None
    if code in _OS_ERRORS:
        return _OS_ERRORS[code]
    if isinstance(number, int) and number:
        return os.strerror(number).lower()
    return type(error).__name__


def safe_path(path: object) -> str:
    """A user-supplied path for a one-line message, with control characters escaped."""
    import json

    return json.dumps(str(path), ensure_ascii=True)[1:-1]


def cannot_read(path: object, error: BaseException) -> str:
    """``cannot read x.json: no such file``."""
    return f"cannot read {safe_path(path)}: {os_reason(error)}"
