"""Bounded reading of UTF-8 JSON files (``lib/io.js``)."""

from __future__ import annotations

import errno
import os
import stat
from typing import Any

from .bounds import validate_json_tree
from .config import InputError
from .jsjson import decode_utf8, parse

MAX_FILE_BYTES = 16 * 1024 * 1024


def error_code(error: BaseException) -> str | None:
    """The Node.js-style ``error.code`` (e.g. ``ENOENT``) of an OS error."""
    number = getattr(error, 'errno', None)
    if isinstance(number, int):
        return errno.errorcode.get(number)
    return None


def read_json(path: str) -> Any:
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InputError(f'{path}: expected a regular file')
        if info.st_size > MAX_FILE_BYTES:
            raise InputError(f'{path}: exceeds 16 MiB file limit')
        wanted = min(info.st_size + 1, MAX_FILE_BYTES + 1)
        chunks: list[bytes] = []
        received = 0
        while received < wanted:
            chunk = os.pread(fd, wanted - received, received)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        if received > info.st_size or received > MAX_FILE_BYTES:
            raise InputError(f'{path}: file changed while reading or exceeds size limit')
        try:
            text = decode_utf8(b''.join(chunks))
        except UnicodeDecodeError:
            raise InputError(f'{path}: ERR_ENCODING_INVALID_ENCODED_DATA') from None
        return validate_json_tree(parse(text))
    except InputError:
        raise
    except Exception as error:
        raise InputError(f"{path}: {error_code(error) or 'invalid JSON or UTF-8'}") from None
    finally:
        if fd is not None:
            os.close(fd)
