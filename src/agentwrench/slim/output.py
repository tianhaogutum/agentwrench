"""Atomic catalogue publication for ``discover --output``."""

from __future__ import annotations

import os
import uuid
from typing import Any

from .jsonio import error_code

TEMPORARY_PREFIX = '.agentwrench-slim-'


class CatalogueOutputError(Exception):
    @property
    def message(self) -> str:
        return str(self)


class _FileHandle:
    def __init__(self, fd: int) -> None:
        self.fd: int | None = fd

    def write_file(self, data: str) -> None:
        view = memoryview(data.encode('utf-8'))
        while view:
            written = os.write(self.fd, view)
            view = view[written:]

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)


class FileSystem:
    """The filesystem operations used for publication; tests inject failures."""

    def realpath(self, path: str) -> str:
        return os.path.realpath(path, strict=True)

    def open(self, path: str, flags: str, mode: int) -> Any:
        assert flags == 'wx'
        return _FileHandle(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC | getattr(os, 'O_CLOEXEC', 0), mode))

    def rename(self, source: str, target: str) -> None:
        os.rename(source, target)

    def unlink(self, path: str) -> None:
        os.unlink(path)


def node_dirname(path: str) -> str:
    """POSIX ``path.dirname`` from Node.js (ignores trailing separators)."""
    if not path:
        return '.'
    has_root = path[0] == '/'
    end = -1
    matched_slash = True
    for i in range(len(path) - 1, 0, -1):
        if path[i] == '/':
            if not matched_slash:
                end = i
                break
        else:
            matched_slash = False
    if end == -1:
        return '/' if has_root else '.'
    if has_root and end == 1:
        return '//'
    return path[:end]


def write_catalogue_file(path: str, snapshot: str, io: Any = None) -> None:
    """Keep the temporary inode on the destination filesystem. The only operation
    that touches the destination entry is rename, after write and close succeed.

    The destination spelling is kept: the filesystem must traverse ``symlink/..``
    components itself and enforce trailing directory separators. The temporary
    file's parent is resolved through the filesystem before joining its name.
    """
    io = FileSystem() if io is None else io
    destination = path
    temporary: str | None = None
    handle: Any = None
    created = False
    failure: BaseException | None = None
    try:
        temporary = os.path.join(io.realpath(node_dirname(destination)), f'{TEMPORARY_PREFIX}{uuid.uuid4()}.tmp')
        handle = io.open(temporary, 'wx', 0o600)
        created = True
        handle.write_file(snapshot)
        handle.close()
        handle = None
        io.rename(temporary, destination)
        created = False
    except Exception as error:
        failure = error
    finally:
        try:
            if handle is not None:
                handle.close()
        except Exception as error:
            failure = failure or error
        if created:
            try:
                io.unlink(temporary)
            except Exception as error:
                failure = failure or error
    if failure is not None:
        raise CatalogueOutputError(f"discovery output failed ({error_code(failure) or 'filesystem error'})")
