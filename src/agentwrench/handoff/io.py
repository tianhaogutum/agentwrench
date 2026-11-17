"""Bounded local I/O and the package's canonical JSON encoding."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_JSON = 24 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024


class HandoffError(Exception):
    """An actionable input or repository error, safe to display without a traceback."""


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise HandoffError("duplicate JSON object key")
        obj[key] = value
    return obj


def parse_json(data: bytes) -> Any:
    try:
        return json.loads(
            data,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(HandoffError("non-finite JSON number")),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HandoffError("invalid JSON (content omitted)") from exc


def read_bytes(path: Path, maximum: int = MAX_JSON) -> bytes:
    """Do not follow a final symlink; never allocate beyond the documented cap."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise HandoffError("input must be a regular file")
            if before.st_size > maximum:
                raise HandoffError(f"input exceeds {maximum} bytes")
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            if len(data) > maximum:
                raise HandoffError(f"input exceeds {maximum} bytes")
            if stamp(before) != stamp(after):
                raise HandoffError("input changed while being read; retry")
            return data
    except OSError as exc:
        raise HandoffError(f"cannot read local file ({exc.strerror})") from exc


def read_json(path: Path, maximum: int = MAX_JSON) -> Any:
    return parse_json(read_bytes(path, maximum))


def stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def relative_path(value: str) -> str:
    """Portable repository-relative file names, including spaces and newlines."""
    parts = value.split("/")
    if (
        not value
        or PurePosixPath(value).is_absolute()
        or "\\" in value
        or "\x00" in value
        or ":" in value
        or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts)
    ):
        raise HandoffError("path must be a portable repository-relative file path without .git")
    return value


def command_argv(value: list[str], *, allow_empty: bool = False) -> list[str]:
    if (
        not isinstance(value, list) or len(value) > 1000
        or any(not isinstance(arg, str) or len(arg) > 16000 or "\x00" in arg for arg in value)
        or (value and not value[0]) or (not value and not allow_empty)
    ):
        raise HandoffError("command argv requires a nonempty executable and at most 1000 string arguments without NUL (16000 characters each)")
    return value


def contained_file(root: Path, value: str) -> Path:
    """Reject ancestor symlinks, including symlinks pointing inside the repository."""
    relative_path(value)
    current = root
    for component in value.split("/")[:-1]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise HandoffError("artifact path has a non-directory or symlink ancestor")
    return root / value


def atomic_json(path: Path, value: Any, *, force: bool = False, maximum: int = MAX_JSON) -> None:
    data = (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    atomic_bytes(path, data, force=force, maximum=maximum)


def atomic_bytes(path: Path, data: bytes, *, force: bool = False, maximum: int = MAX_JSON) -> None:
    if len(data) > maximum:
        raise HandoffError(f"output exceeds {maximum} bytes")
    path = path.absolute()
    if path.is_symlink():
        raise HandoffError("output cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".handoff-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise HandoffError("output already exists; use --force to replace it") from exc
    finally:
        temporary.unlink(missing_ok=True)


def prepare_output(path: Path, *, force: bool) -> Path:
    """Fail before execution if the destination is already occupied or unwritable."""
    path = path.absolute()
    if path.is_symlink():
        raise HandoffError("output cannot be a symlink")
    path = path.parent.resolve() / path.name
    if path.exists():
        if not path.is_file():
            raise HandoffError("output must be a regular file")
        if not force:
            raise HandoffError("output already exists; use --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".handoff-preflight-", dir=path.parent)
    os.close(fd)
    Path(temporary).unlink()
    return path
