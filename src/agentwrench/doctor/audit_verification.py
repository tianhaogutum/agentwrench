"""Opt-in replay of v2 parsing metadata, without changing cursor or history."""
import hashlib
import os
from pathlib import Path

from .audit import _checkpoint_lock, _hash_prefix, _validate_state
from .audit_migration import _separate
from .audit_state import VERSION, pack
from .engine import DoctorError, Report, add_record, decode
from .files import open_session


def _unchanged(stream, path, start):
    def signature(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)
    if (signature(os.fstat(stream.fileno())) != signature(start) or
            signature(path.lstat()) != signature(start)):
        raise DoctorError("verification input changed while reading; checkpoint unchanged")


def verify_audit(path: Path, checkpoint: Path, format="auto") -> dict:
    """Verify exactly the saved accepted prefix; never pair or hash diagnostics.

    Well-formed cached metadata that disagrees with replay is a mismatch. Invalid
    schema, source/hash/boundary disagreement and unstable inputs are refusals.
    Emitted hashes are schema-checked, but their history is never authenticated.
    """
    if format not in ("auto", "codex", "claude"):
        raise DoctorError("format must be auto, codex or claude")
    try:
        _separate([path, checkpoint, checkpoint.with_name(checkpoint.name + ".lock")])
    except RuntimeError as exc:
        raise DoctorError("verification paths must resolve without symlink loops") from exc
    with _checkpoint_lock(checkpoint):
        with open_session(checkpoint) as cached, open_session(path) as source:
            cached_start, start = os.fstat(cached.fileno()), os.fstat(source.fileno())
            try:
                previous = decode(cached.read())
                _validate_state(previous)
            except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                raise DoctorError("verification requires a valid version 2 audit checkpoint") from exc
            identity = {"file": str(path.resolve()), "device": start.st_dev, "inode": start.st_ino,
                        "requested_format": format}
            if any(previous[k] != v for k, v in identity.items()):
                raise DoctorError("verification source identity or requested format changed")
            limit = previous["offset"]
            if start.st_size < limit:
                raise DoctorError("verification source was truncated")
            if _hash_prefix(source, limit).hexdigest() != previous["digest"]:
                raise DoctorError("verification source prefix SHA-256 changed")

            metadata = Report(str(path), "empty" if format == "auto" else format)
            source.seek(0)
            digest, offsets, offset, terminated = hashlib.sha256(), {}, 0, True
            while offset < limit:
                record = source.readline(limit - offset)
                if not record:
                    raise DoctorError("verification source changed while reading")
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
                raise DoctorError("verification source prefix changed during replay")
            if metadata.format == "empty" and metadata.lines:
                raise DoctorError("cannot detect accepted prefix format")
            if not terminated:
                # Inspect only the separator, never decode or accept a tail record.
                # Whitespace to EOF also permits a separator to be finished later.
                for _ in range(start.st_size - limit):
                    char = source.read(1)
                    if char == b"\n":
                        break
                    if not char or char not in (b" ", b"\t", b"\r"):
                        raise DoctorError("ambiguous cursor boundary: unterminated record requires a newline separator")
            replay = pack(metadata, offsets, terminated)
            mismatches = sorted(key for key in replay if replay[key] != previous["pairing"][key])
            _unchanged(source, path, start)
            _unchanged(cached, checkpoint, cached_start)
            return {"file": str(path), "checkpoint": str(checkpoint), "version": VERSION,
                    "format": metadata.format, "offset": limit,
                    "status": "mismatch" if mismatches else "verified",
                    "mismatches": mismatches, "history_verified": False}
