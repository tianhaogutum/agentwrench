"""Explicit, verified replay of a legacy accepted prefix into a new checkpoint."""
import hashlib
import os
from pathlib import Path

from .audit import _checkpoint_lock, _save, _validate_state
from .audit_state import VERSION, pack
from .engine import DoctorError, Report, add_record, decode
from .files import open_session


def _separate(paths):
    for index, path in enumerate(paths):
        for other in paths[:index]:
            if path.resolve() == other.resolve() or (
                path.exists() and other.exists() and os.path.samefile(path, other)
            ):
                raise DoctorError("session, checkpoints and checkpoint locks must be separate files")


def _unchanged(source, path, start):
    finish, current = os.fstat(source.fileno()), path.lstat()
    if ((start.st_size, start.st_mtime_ns, start.st_ctime_ns) !=
            (finish.st_size, finish.st_mtime_ns, finish.st_ctime_ns) or
            (current.st_dev, current.st_ino) != (start.st_dev, start.st_ino)):
        raise DoctorError("migration input changed while reading; no checkpoint published")


def migrate_audit(path: Path, checkpoint: Path, destination: Path, format="auto") -> dict:
    """Decode accepted records once, preserving the v1 cursor and all history.

    No current finding is emitted or added to the historical hash list here.
    The unaccepted tail is not parsed, even if it has become complete or invalid.
    """
    if format not in ("auto", "codex", "claude"):
        raise DoctorError("format must be auto, codex or claude")
    # Resolve parents only: never turn a final symlink into an allowed endpoint.
    destination = destination.parent.resolve(strict=True) / destination.name
    _separate([path, checkpoint, destination,
               checkpoint.with_name(checkpoint.name + ".lock"),
               destination.with_name(destination.name + ".lock")])
    if os.path.lexists(destination):
        raise DoctorError("migration --out must be a new path (refusing overwrite)")
    with _checkpoint_lock(checkpoint), _checkpoint_lock(destination):
        with open_session(checkpoint) as old, open_session(path) as source:
            old_start, start = os.fstat(old.fileno()), os.fstat(source.fileno())
            try:
                previous = decode(old.read())
                _validate_state(previous, version=1)
            except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                raise DoctorError("migration requires a valid version 1 audit checkpoint") from exc
            identity = {"file": str(path.resolve()), "device": start.st_dev, "inode": start.st_ino,
                        "requested_format": format}
            if any(previous[k] != v for k, v in identity.items()):
                raise DoctorError("migration source identity or requested format changed")
            limit = previous["offset"]
            if start.st_size < limit:
                raise DoctorError("migration source was truncated")
            metadata = Report(str(path), "empty" if format == "auto" else format)
            digest, offsets, offset, terminated = hashlib.sha256(), {}, 0, True
            while offset < limit:
                record = source.readline(limit - offset)
                if not record:
                    raise DoctorError("migration source changed while reading")
                line, end = metadata.lines + 1, offset + len(record)
                try:
                    value = decode(record)
                except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                    raise DoctorError(f"invalid accepted record or cursor boundary at line {line}, byte {offset}") from exc
                add_record(metadata, value, line, format)
                if metadata.issues and metadata.issues[-1].code == "RD003":
                    raise DoctorError(f"invalid accepted session record at line {line}, byte {offset}")
                if (metadata.events and metadata.events[-1].line == line or
                        metadata.nodes and next(reversed(metadata.nodes.values())).line == line or
                        metadata.issues and metadata.issues[-1].line == line):
                    offsets[line] = (offset, end)
                digest.update(record)
                offset, terminated = end, record.endswith(b"\n")
            if digest.hexdigest() != previous["digest"]:
                raise DoctorError("migration source prefix SHA-256 changed")
            if metadata.format == "empty" and metadata.lines:
                raise DoctorError("cannot detect accepted prefix format; requested format must match v1 checkpoint")
            if not terminated and start.st_size > limit:
                # EOF may have been a valid unterminated object in v1. If more
                # bytes now exist, they must finish its separator, not its JSON.
                separator = source.readline(start.st_size - limit)
                if not separator or separator.strip(b" \t\r\n"):
                    raise DoctorError("ambiguous cursor boundary: unterminated record requires a newline separator")

            def verify_inputs():
                _unchanged(source, path, start)
                _unchanged(old, checkpoint, old_start)

            _save(destination, {**previous, "version": VERSION,
                                "pairing": pack(metadata, offsets, terminated)},
                  exclusive=True, before_publish=verify_inputs)
            return {"file": str(path), "format": metadata.format, "status": "migrated",
                    "checkpoint": str(destination), "previous_checkpoint": str(checkpoint),
                    "version": VERSION, "offset": limit, "emitted_count": len(previous["emitted"])}
