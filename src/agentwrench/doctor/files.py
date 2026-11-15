"""Linux regular-file boundaries, cooperative locks, and atomic publication."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO, Iterator

from .engine import DoctorError
from .repair import Plan, render


@contextmanager
def open_session(path: Path, write: bool = False) -> Iterator[BinaryIO]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        # O_NOFOLLOW reports a final-component symlink as ELOOP; name the real refusal instead.
        if exc.errno == errno.ELOOP:
            raise DoctorError("symlink inputs are not supported") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DoctorError(f"{path}: only regular files are supported (no symlinks/FIFOs/devices)")
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DoctorError(f"{path}: file is locked by another process") from exc
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


def fingerprint(source: BinaryIO) -> str:
    source.seek(0)
    digest = hashlib.sha256()
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def assert_unchanged(source: BinaryIO, path: Path, plan: Plan) -> None:
    st = path.lstat()
    current = os.fstat(source.fileno())
    if (st.st_dev, st.st_ino) != (current.st_dev, current.st_ino) or fingerprint(source) != plan.report.digest:
        raise DoctorError("source changed during repair; no replacement performed")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(source: BinaryIO, path: Path, destination: Path, plan: Plan, in_place: bool,
            expected_digest: str | None = None, *, before_write=None) -> str | None:
    if not in_place and os.path.lexists(destination):
        raise DoctorError(f"{destination}: --out must be a new path (refusing overwrite)")
    parent = destination.parent.resolve(strict=True)
    if not parent.is_dir():
        raise DoctorError("output parent must be an existing directory")
    # Explicit output destinations may be anywhere the caller chooses. Generated backup
    # names are always siblings; no path ever comes from session payloads.
    backup: Path | None = None
    if before_write is not None:
        before_write()
    fd, temp_name = tempfile.mkstemp(prefix=".rollout-doctor-", dir=parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as target:
            digest = hashlib.sha256()
            for raw in render(source, plan):
                target.write(raw)
                if expected_digest is not None:
                    digest.update(raw)
            target.flush()
            os.fsync(target.fileno())
        if expected_digest is not None and digest.hexdigest() != expected_digest:
            raise DoctorError("rendered output differs from preview; no output published")
        assert_unchanged(source, path, plan)
        if in_place:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = path.with_name(path.name + ".bak-" + stamp)
            # O_EXCL prevents following/replacing a pre-existing backup or symlink.
            if before_write is not None:
                before_write()
            backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(backup_fd, "wb") as target:
                source.seek(0)
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            _fsync_dir(parent)
            assert_unchanged(source, path, plan)
            os.chmod(temp, stat.S_IMODE(os.fstat(source.fileno()).st_mode) & 0o777)
            if before_write is not None:
                before_write()
            os.replace(temp, destination)
        else:
            # Atomic no-clobber publication: link fails if another writer created PATH.
            if before_write is not None:
                before_write()
            os.link(temp, destination, follow_symlinks=False)
            temp.unlink()
        _fsync_dir(parent)
        return str(backup) if backup else None
    finally:
        temp.unlink(missing_ok=True)
