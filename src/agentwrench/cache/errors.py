"""Error types. Messages match the Node tool, including Node's file-system wording."""

from __future__ import annotations

import errno
import os

# libuv's uv_strerror() texts, which Node uses in fs error messages.
_UV_DESCRIPTIONS = {
    "E2BIG": "argument list too long",
    "EACCES": "permission denied",
    "EAGAIN": "resource temporarily unavailable",
    "EBADF": "bad file descriptor",
    "EBUSY": "resource busy or locked",
    "EDQUOT": "disk quota exceeded",
    "EEXIST": "file already exists",
    "EFAULT": "bad address in system call argument",
    "EFBIG": "file too large",
    "EINTR": "interrupted system call",
    "EINVAL": "invalid argument",
    "EIO": "i/o error",
    "EISDIR": "illegal operation on a directory",
    "ELOOP": "too many symbolic links encountered",
    "EMFILE": "too many open files",
    "EMLINK": "too many links",
    "ENAMETOOLONG": "name too long",
    "ENFILE": "file table overflow",
    "ENODEV": "no such device",
    "ENOENT": "no such file or directory",
    "ENOMEM": "not enough memory",
    "ENOSPC": "no space left on device",
    "ENOTDIR": "not a directory",
    "ENOTSUP": "operation not supported on socket",
    "ENXIO": "no such device or address",
    "EOVERFLOW": "value too large for defined data type",
    "EPERM": "operation not permitted",
    "EROFS": "read-only file system",
    "ESPIPE": "invalid seek",
    "ETXTBSY": "text file is busy",
    "EXDEV": "cross-device link not permitted",
}


class CacheError(Exception):
    """An input, option, policy-independent or I/O error (CLI exit 2)."""

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


class FsError(CacheError):
    """A file-system failure, worded like Node: ``CODE: description, syscall 'path'``."""

    def __init__(self, error: OSError, syscall: str, path: str | None = None, dest: str | None = None) -> None:
        number = error.errno or 0
        self.errno = number
        self.code = errno.errorcode.get(number, "UNKNOWN")
        self.syscall = syscall
        self.path = path
        description = _UV_DESCRIPTIONS.get(self.code) or (os.strerror(number).lower() if number else "unknown error")
        message = f"{self.code}: {description}, {syscall}"
        if path is not None:
            message += f" '{path}'"
        if dest is not None:
            message += f" -> '{dest}'"
        super().__init__(message)
