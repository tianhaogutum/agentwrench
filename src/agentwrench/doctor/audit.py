"""Checkpointed, read-only audits. Parses append bytes; never caches payloads."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .engine import DoctorError, Report, add_record, decode, pair, sort_issues
from .audit_state import VERSION, pack, unpack
from .files import NO_LOCKS, _fsync_dir, fcntl, open_session


class _Incomplete(Exception):
    pass


class _Invalid(Exception):
    pass


def incomplete_object(raw: bytes) -> bool:
    """True only for a proper prefix of a strict JSON object, including split UTF-8.

    A small grammar recognizer distinguishes truncation from syntax errors. It is
    only used after normal decoding failed; complete records use engine.decode.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data" or exc.end != len(raw):
            return False
        # A split code point can only continue inside a JSON string.
        text = raw[:exc.start].decode("utf-8") + "\ufffd"
    pos = 0

    def peek():
        if pos == len(text):
            raise _Incomplete
        return text[pos]

    def space():
        nonlocal pos
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1

    def expect(char):
        nonlocal pos
        if peek() != char:
            raise _Invalid
        pos += 1

    def string():
        nonlocal pos
        start = pos
        expect('"')
        while True:
            char = peek()
            pos += 1
            if char == '"':
                return json.loads(text[start:pos])
            if ord(char) < 32:
                raise _Invalid
            if char == "\\":
                char = peek()
                pos += 1
                if char == "u":
                    for _ in range(4):
                        if peek() not in "0123456789abcdefABCDEF":
                            raise _Invalid
                        pos += 1
                elif char not in '\\"/bfnrt':
                    raise _Invalid

    def digits():
        nonlocal pos
        if peek() not in "0123456789":
            raise _Invalid
        while pos < len(text) and text[pos] in "0123456789":
            pos += 1

    def value():
        nonlocal pos
        space()
        char = peek()
        if char == '{':
            container(True)
        elif char == '[':
            container(False)
        elif char == '"':
            string()
        elif char in "tfn":
            for letter in {"t": "true", "f": "false", "n": "null"}[char]:
                expect(letter)
        elif char in "-0123456789":
            if char == '-':
                pos += 1
            if peek() == '0':
                pos += 1
            else:
                digits()
            if pos < len(text) and text[pos] == '.':
                pos += 1
                digits()
            if pos < len(text) and text[pos] in 'eE':
                pos += 1
                if peek() in '+-':
                    pos += 1
                digits()
        else:
            raise _Invalid

    def container(obj):
        nonlocal pos
        expect('{' if obj else '[')
        close = '}' if obj else ']'
        keys = set()
        space()
        if peek() == close:
            pos += 1
            return
        while True:
            space()
            if obj:
                key = string()
                if key in keys:
                    raise _Invalid
                keys.add(key)
                space()
                expect(':')
            value()
            space()
            if peek() == close:
                pos += 1
                return
            expect(',')

    if not text.lstrip(" \t\r\n").startswith('{'):
        return False
    try:
        value()
        return False  # A complete object plus junk is never truncation.
    except _Incomplete:
        return True
    except (_Invalid, ValueError, RecursionError):
        return False


def _hash_prefix(source, length):
    source.seek(0)
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = source.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise DoctorError("audit source was truncated; checkpoint unchanged")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest


def _load(checkpoint):
    if not os.path.lexists(checkpoint):
        return None
    try:
        with open_session(checkpoint) as source:
            state = decode(source.read())
        _validate_state(state)
        return state
    except (TypeError, ValueError, RecursionError) as exc:
        raise DoctorError("invalid audit checkpoint; use a new checkpoint path") from exc


def _validate_state(state, version=VERSION):
    """Common strict checkpoint header; v1 has no cached pairing state."""
    fields = {"version", "file", "device", "inode", "requested_format", "offset", "digest", "emitted", "pairing"}
    if version == 1:
        fields.remove("pairing")
    if set(state) != fields or type(state["version"]) is not int or state["version"] != version:
        raise ValueError
    if any(type(state[k]) is not int or state[k] < 0 for k in ("device", "inode", "offset")):
        raise ValueError
    if not isinstance(state["file"], str) or state["requested_format"] not in ("auto", "codex", "claude"):
        raise ValueError
    if not isinstance(state["emitted"], list):
        raise ValueError
    for digest in [state["digest"], *state["emitted"]]:
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError
    if version == VERSION:
        unpack(state["pairing"], state["file"], state["requested_format"], state["offset"])


@contextmanager
def _checkpoint_lock(checkpoint):
    # Keep a stable inode for the lock while the JSON checkpoint is atomically replaced.
    if fcntl is None:
        raise DoctorError(NO_LOCKS)
    lock = checkpoint.with_name(checkpoint.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DoctorError("checkpoint lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DoctorError("audit checkpoint is locked by another process") from exc
        yield
    finally:
        os.close(fd)


def _save(checkpoint, state, *, exclusive=False, before_publish=None):
    fd, name = tempfile.mkstemp(prefix=".rollout-doctor-audit-", dir=checkpoint.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(state, target, ensure_ascii=True, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        if before_publish is not None:
            before_publish()
        if exclusive:
            os.link(temp, checkpoint, follow_symlinks=False)
            temp.unlink()
        else:
            os.replace(temp, checkpoint)
        _fsync_dir(checkpoint.parent)
    finally:
        temp.unlink(missing_ok=True)


def audit(path: Path, checkpoint: Path, format="auto", allow_incomplete_final=False) -> dict:
    """Verify prefix bytes, parse append records and pair persisted metadata."""
    if format not in ("auto", "codex", "claude"):
        raise DoctorError("format must be auto, codex or claude")
    if path.resolve() == checkpoint.resolve() or (
        checkpoint.exists() and path.exists() and os.path.samefile(path, checkpoint)
    ):
        raise DoctorError("checkpoint must be separate from the session file")
    with _checkpoint_lock(checkpoint):
        previous = _load(checkpoint)
        with open_session(path) as source:
            start = os.fstat(source.fileno())
            identity = {"file": str(path.resolve()), "device": start.st_dev, "inode": start.st_ino,
                        "requested_format": format}
            resumed_from = previous["offset"] if previous else 0
            digest = _hash_prefix(source, resumed_from)
            if previous:
                if any(previous[k] != v for k, v in identity.items()):
                    raise DoctorError("audit source identity or format changed; use a new checkpoint")
                if start.st_size < resumed_from or digest.hexdigest() != previous["digest"]:
                    raise DoctorError("audit source prefix changed or truncated; checkpoint unchanged")
                metadata, offsets, terminated = unpack(previous["pairing"], str(path), format, resumed_from)
            else:
                metadata = Report(str(path), "empty" if format == "auto" else format)
                offsets, terminated = {}, True
            offset = resumed_from
            incomplete = None
            remaining = start.st_size - offset

            # A previously valid final object can only be extended by a record
            # separator. Treating concatenated objects as separate lines is unsafe.
            if not terminated and remaining:
                separator = source.readline(remaining)
                if not separator or separator.strip(b" \t\r\n"):
                    raise DoctorError("append after an unterminated record requires a newline separator; checkpoint unchanged")
                digest.update(separator)
                offset += len(separator)
                remaining -= len(separator)
                if metadata.lines in offsets:
                    offsets[metadata.lines] = (offsets[metadata.lines][0], offset)
                terminated = separator.endswith(b"\n")

            while remaining:
                record = source.readline(remaining)
                if not record:
                    raise DoctorError("audit source changed while reading; checkpoint unchanged")
                remaining -= len(record)
                line = metadata.lines + 1
                end = offset + len(record)
                try:
                    value = decode(record)
                except (TypeError, ValueError, UnicodeError, RecursionError):
                    if (allow_incomplete_final and not remaining and not record.endswith(b"\n")
                            and incomplete_object(record)):
                        incomplete = {"line": line, "byte_offset": offset, "end_byte_offset": end}
                        break
                    raise DoctorError(f"invalid JSONL record at line {line}, byte {offset}; checkpoint unchanged") from None
                add_record(metadata, value, line, format)
                if metadata.issues and metadata.issues[-1].code == "RD003":
                    raise DoctorError(f"invalid session record at line {line}, byte {offset}; checkpoint unchanged")
                # Only records referenced by pairing metadata need byte ranges.
                if (metadata.events and metadata.events[-1].line == line or
                        metadata.nodes and next(reversed(metadata.nodes.values())).line == line or
                        metadata.issues and metadata.issues[-1].line == line):
                    offsets[line] = (offset, end)
                digest.update(record)
                offset = end
                terminated = record.endswith(b"\n")
            if metadata.format == "empty" and metadata.lines:
                raise DoctorError(f"{path}: cannot detect format; use --format codex or --format claude")
            # Keep structural findings separate: dangling parents, missing outputs,
            # and early/duplicate outputs can all change after an append.
            report = replace(metadata, issues=list(metadata.issues), missing=[])
            pair(report)
            sort_issues(report)
            finish = os.fstat(source.fileno())
            current = path.lstat()
            if ((start.st_size, start.st_mtime_ns, start.st_ctime_ns) !=
                    (finish.st_size, finish.st_mtime_ns, finish.st_ctime_ns) or
                    (current.st_dev, current.st_ino) != (start.st_dev, start.st_ino)):
                raise DoctorError("audit source changed while reading; checkpoint unchanged")
            emitted = set(previous["emitted"]) if previous else set()
            issues = []
            for issue in report.issues:
                value = issue.as_dict()
                value["byte_offset"], value["end_byte_offset"] = offsets[issue.line]
                key = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
                if key not in emitted:
                    issues.append(value)
                    emitted.add(key)
            _save(checkpoint, {"version": VERSION, **identity, "offset": offset,
                               "digest": digest.hexdigest(), "emitted": sorted(emitted),
                               "pairing": pack(metadata, offsets, terminated)})
            return {"file": str(path), "format": report.format, "issues": issues,
                    "issue_count": len(report.issues), "checkpoint": str(checkpoint),
                    "offset": offset, "resumed_from": resumed_from, "incomplete_final": incomplete}
