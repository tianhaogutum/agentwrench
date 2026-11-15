"""Exact manifest-byte apply pins, before source open or filesystem writes."""
import hashlib
import json
import os
import tempfile

import pytest

from agentwrench.doctor import manifest as manifests
from agentwrench.doctor.cli import main
from .test_backup_verification import inventory

MISMATCH = "manifest SHA-256 differs from --expect-manifest-sha256; no source opened"
INVALID = "--expect-manifest-sha256 requires exactly 64 lowercase hex characters"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(root, fmt, version, *, clean=False, strategy="stub"):
    source, output, manifest = (root / name for name in ("session", "output", "plan"))
    fixture = ("clean.jsonl" if clean else "orphan.jsonl" if fmt == "codex" else "orphan_tool_use.jsonl")
    from pathlib import Path
    source.write_bytes((Path(__file__).parent / "fixtures" / fmt / fixture).read_bytes())
    if version == 2:
        output = source
        manifests.preview(source, None, manifest, strategy=strategy, in_place=True, backup=True)
    else:
        manifests.preview(source, output, manifest, strategy=strategy)
    return source, output, manifest


def invoke(capsys, manifest, *options):
    code = main(["apply", str(manifest), *options])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def refusal(result, manifest, error, json_output):
    code, out, err = result
    assert code == 2 and err == f"error: {error}\n"
    assert (json.loads(out) == {"file": str(manifest), "error": error}) if json_output else out == ""


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("version", [1, 2])
def test_wrong_pin_before_source_open_or_staging(tmp_path, capsys, monkeypatch, fmt, version):
    _, _, manifest = prepare(tmp_path, fmt, version)
    before = inventory(tmp_path)
    opened = []
    real_open = os.open

    def manifest_only(path, flags, *args, **kwargs):
        assert path == manifest, "wrong pin must never open source/output/backup"
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("wrong pin attempted parsing, staging or filesystem mutation")

    monkeypatch.setattr(os, "open", manifest_only)
    monkeypatch.setattr(manifests, "_read", forbidden)
    monkeypatch.setattr(tempfile, "mkstemp", forbidden)
    monkeypatch.setattr(os, "link", forbidden)
    monkeypatch.setattr(os, "replace", forbidden)
    for options in ([], ["--json"]):
        refusal(invoke(capsys, manifest, "--expect-manifest-sha256", "0" * 64, *options),
                manifest, MISMATCH, options)
    assert opened == [manifest, manifest]
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("pin", ["", "0" * 63, "0" * 65, "A" * 64, "g" * 64,
                                 "0" * 63 + "\n", " " + "0" * 64, "0" * 64 + " ",
                                 "é" * 64, "０" * 64, "sha256:" + "a" * 64])
def test_invalid_pin_before_any_open(tmp_path, capsys, monkeypatch, pin):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid pin opened input or staged bytes")

    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(tempfile, "mkstemp", forbidden)
    for options in ([], ["--json"]):
        refusal(invoke(capsys, tmp_path / "missing", "--expect-manifest-sha256", pin, *options),
                tmp_path / "missing", INVALID, options)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("pin", [False, 0, [], {}, b"0" * 64])
def test_api_pin_types(tmp_path, monkeypatch, pin):
    from agentwrench.doctor.engine import DoctorError

    def forbidden(*args, **kwargs):
        pytest.fail("invalid API pin opened input")

    monkeypatch.setattr(os, "open", forbidden)
    with pytest.raises(DoctorError, match="requires exactly 64 lowercase hex characters"):
        manifests.apply(tmp_path / "missing", expect_manifest_sha256=pin)


def test_missing_hash_is_argument_error(tmp_path, capsys):
    assert invoke(capsys, tmp_path / "plan", "--json", "--expect-manifest-sha256") == (
        2, "", "error: argument --expect-manifest-sha256: expected one argument\n")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("clean,strategy", [(False, "stub"), (False, "drop"), (True, "stub")])
def test_exact_pin_first_apply_and_unchanged_reapply_receipts(tmp_path, capsys, fmt, version, clean, strategy):
    source, output, manifest = prepare(tmp_path, fmt, version, clean=clean, strategy=strategy)
    original, saved = source.read_bytes(), manifest.read_bytes()
    value, pin = json.loads(saved), sha(saved)
    source.chmod(0o640)
    before = inventory(tmp_path)
    code, out, err = invoke(capsys, manifest, "--expect-manifest-sha256", pin, "--json")
    assert code == 0 and not err
    result = json.loads(out)
    status = "already_applied" if clean and version == 2 else "applied"
    assert result == {"status": status, "manifest": str(manifest), "backup": result["backup"], **value}
    assert sha(output.read_bytes()) == value["output"]["sha256"]
    if version == 2 and not clean:
        from pathlib import Path
        backup = Path(result["backup"])
        assert backup.read_bytes() == original and backup.stat().st_mode & 0o777 == 0o600
        assert output.stat().st_mode & 0o777 == 0o640
    else:
        assert result["backup"] is None
        assert not list(tmp_path.glob("*.bak-*"))
        if version == 1:
            assert source.read_bytes() == original and output.stat().st_mode & 0o777 == 0o600
        else:
            assert inventory(tmp_path) == before
    after = inventory(tmp_path)
    for options in ([], ["--json"]):
        baseline = invoke(capsys, manifest, *options)
        pinned = invoke(capsys, manifest, "--expect-manifest-sha256", pin, *options)
        assert pinned == baseline and pinned[0] == 0 and not pinned[2]
        if options:
            assert json.loads(pinned[1]) == {"status": "already_applied", "manifest": str(manifest), "backup": None, **value}
        assert inventory(tmp_path) == after
    assert manifest.read_bytes() == saved
    # Wrong pins refuse even on already-applied/no-op branches without changes.
    refusal(invoke(capsys, manifest, "--expect-manifest-sha256", "0" * 64, "--json"), manifest, MISMATCH, True)
    assert inventory(tmp_path) == after


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["whitespace", "reordered", "multichunk"])
def test_complete_bytes_not_semantics(tmp_path, capsys, monkeypatch, version, kind):
    source, _, manifest = prepare(tmp_path, "codex", version)
    saved = manifest.read_bytes()
    changed = (saved + (b" " * (2 * 1024 * 1024) + b"\n" if kind == "multichunk" else b" \n")
               if kind != "reordered" else json.dumps(json.loads(saved), separators=(",", ":")).encode())
    assert json.loads(changed) == json.loads(saved) and changed != saved
    manifest.write_bytes(changed)
    before = inventory(tmp_path)
    real_open = os.open

    def no_source(path, *args, **kwargs):
        assert path != source, "old pin opened source"
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", no_source)
        refusal(invoke(capsys, manifest, "--expect-manifest-sha256", sha(saved), "--json"), manifest, MISMATCH, True)
    assert before == inventory(tmp_path)
    assert invoke(capsys, manifest, "--expect-manifest-sha256", sha(changed))[0] == 0


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["unknown", "bool_version", "backup", "duplicate", "report", "output_hash", "policy"])
def test_pin_does_not_relax_schema_or_replay(tmp_path, capsys, monkeypatch, version, kind):
    _, _, manifest = prepare(tmp_path, "codex", version)
    value = json.loads(manifest.read_bytes())
    if kind == "unknown":
        value["extra"] = True
    elif kind == "bool_version":
        value["version"] = True
    elif kind == "backup":
        value["options"]["backup"] = False
    elif kind == "report":
        value["report"]["changes"] = []
    elif kind == "output_hash":
        value["output"]["sha256"] = "0" * 64
    elif kind == "policy":
        value["options"]["strategy"] = "drop"
    manifest.write_text(json.dumps(value))
    if kind == "duplicate":
        manifest.write_bytes(b'{"version":1,' + manifest.read_bytes()[1:])
    before = inventory(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid/drifted manifest staged bytes")
    monkeypatch.setattr(tempfile, "mkstemp", forbidden)
    result = invoke(capsys, manifest, "--expect-manifest-sha256", sha(manifest.read_bytes()), "--json")
    assert result[0] == 2 and set(json.loads(result[1])) == {"file", "error"}
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("stage", ["first", "already", "noop"])
def test_same_descriptor_and_manifest_lock_through_apply(tmp_path, monkeypatch, version, stage):
    import fcntl
    from agentwrench.doctor import files
    source, output, manifest = prepare(tmp_path, "codex", version, clean=stage == "noop")
    if stage == "already":
        manifests.apply(manifest)
    pin = sha(manifest.read_bytes())
    real_open, real_read, real_scan, real_temp = os.open, manifests._read, manifests.scan, tempfile.mkstemp
    opened, locked_fd = [], []

    def assert_locked():
        fd = real_open(manifest, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def watched_open(path, *args, **kwargs):
        opened.append(path)
        if path != manifest:
            assert_locked()
        return real_open(path, *args, **kwargs)

    def read(stream):
        assert_locked()
        locked_fd.append(stream.fileno())
        return real_read(stream)

    def scan(*args, **kwargs):
        assert_locked()
        os.fstat(locked_fd[0])
        return real_scan(*args, **kwargs)

    def stage_temp(*args, **kwargs):
        assert_locked()
        return real_temp(*args, **kwargs)

    monkeypatch.setattr(os, "open", watched_open)
    monkeypatch.setattr(manifests, "_read", read)
    monkeypatch.setattr(manifests, "scan", scan)
    monkeypatch.setattr(tempfile, "mkstemp", stage_temp)
    manifests.apply(manifest, expect_manifest_sha256=pin)
    assert opened.count(manifest) == 1 and len(locked_fd) == 1
    # The shared lock is released on return.
    fd = real_open(manifest, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("stage", ["parsed", "scanned", "staged", "already", "noop"])
@pytest.mark.parametrize("kind", ["append", "rewrite", "restore", "rotate", "unlink", "chmod"])
def test_manifest_mutations_refuse_before_next_write(tmp_path, capsys, monkeypatch, version, stage, kind):
    from agentwrench.doctor import files
    source, output, manifest = prepare(tmp_path, "codex", version, clean=stage == "noop")
    if stage == "already":
        manifests.apply(manifest)
    pin, saved = sha(manifest.read_bytes()), manifest.read_bytes()
    source_before = source.read_bytes()
    output_before = output.read_bytes() if output.exists() else None
    fired = []

    def mutate():
        if fired:
            return
        fired.append(True)
        start = manifest.stat()
        if kind == "append":
            manifest.write_bytes(saved + b" ")
        elif kind == "rewrite":
            # Same size, restore mtime: ctime and/or digest must catch it.
            manifest.write_bytes(saved.replace(b'"stub"', b'"drop"'))
            os.utime(manifest, ns=(start.st_atime_ns, start.st_mtime_ns))
        elif kind == "restore":
            manifest.write_bytes(saved + b" ")
            manifest.write_bytes(saved)
            os.utime(manifest, ns=(start.st_atime_ns, start.st_mtime_ns))
        elif kind == "rotate":
            temp = tmp_path / "rotated"
            temp.write_bytes(saved)
            os.replace(temp, manifest)
        elif kind == "unlink":
            manifest.unlink()
        else:
            manifest.chmod(0o640)

    if stage == "parsed":
        real = manifests._read
        def read(*args, **kwargs):
            value = real(*args, **kwargs)
            mutate()
            return value
        monkeypatch.setattr(manifests, "_read", read)
        real_open = os.open
        def no_source(path, *args, **kwargs):
            assert path == manifest, "manifest changed before source open"
            return real_open(path, *args, **kwargs)
        monkeypatch.setattr(os, "open", no_source)
    elif stage == "staged":
        real = files.render
        def render(*args, **kwargs):
            yield from real(*args, **kwargs)
            mutate()
        monkeypatch.setattr(files, "render", render)
    else:
        real = manifests.scan
        def scan(*args, **kwargs):
            report = real(*args, **kwargs)
            mutate()
            return report
        monkeypatch.setattr(manifests, "scan", scan)
    result = invoke(capsys, manifest, "--expect-manifest-sha256", pin, "--json")
    assert fired and result[0] == 2 and set(json.loads(result[1])) == {"file", "error"}
    assert source.read_bytes() == source_before
    assert (output.read_bytes() if output.exists() else None) == output_before
    assert not list(tmp_path.glob(".rollout-doctor-*"))
    assert len(list(tmp_path.glob("*.bak-*"))) == (1 if stage == "already" and version == 2 else 0)


def test_manifest_change_after_backup_refuses_replace_and_retains_backup(tmp_path, capsys, monkeypatch):
    from agentwrench.doctor import files
    source, _, manifest = prepare(tmp_path, "claude", 2)
    original, pin = source.read_bytes(), sha(manifest.read_bytes())
    real = files._fsync_dir
    def changed(parent):
        real(parent)
        manifest.write_bytes(manifest.read_bytes() + b" ")
    monkeypatch.setattr(files, "_fsync_dir", changed)
    result = invoke(capsys, manifest, "--expect-manifest-sha256", pin, "--json")
    assert result[0] == 2 and "manifest changed during apply" in result[2]
    backups = list(tmp_path.glob("*.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == original and not list(tmp_path.glob(".rollout-doctor-*"))


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["source_change", "output_race", "fsync_failure"])
def test_pinned_publication_preserves_existing_guards(tmp_path, capsys, monkeypatch, version, kind):
    from agentwrench.doctor import files
    source, output, manifest = prepare(tmp_path, "codex", version)
    original, pin = source.read_bytes(), sha(manifest.read_bytes())
    if kind == "source_change":
        real = manifests.publish
        def changed(*args, **kwargs):
            source.write_bytes(original + b"\n")
            return real(*args, **kwargs)
        monkeypatch.setattr(manifests, "publish", changed)
    elif kind == "output_race":
        if version == 2:
            def fail_backup(path, flags, *args, **kwargs):
                if ".bak-" in str(path):
                    raise OSError("injected backup failure")
                return real(path, flags, *args, **kwargs)
            real = os.open
            monkeypatch.setattr(os, "open", fail_backup)
        else:
            real = os.link
            def race(src, dst, *args, **kwargs):
                output.write_bytes(b"concurrent winner")
                return real(src, dst, *args, **kwargs)
            monkeypatch.setattr(os, "link", race)
    else:
        def fail(*args, **kwargs):
            raise OSError("injected staging fsync failure")
        monkeypatch.setattr(os, "fsync", fail)
    result = invoke(capsys, manifest, "--expect-manifest-sha256", pin, "--json")
    assert result[0] == 2 and set(json.loads(result[1])) == {"file", "error"}
    assert source.read_bytes() == original + (b"\n" if kind == "source_change" else b"")
    assert not list(tmp_path.glob("*.bak-*")) and not list(tmp_path.glob(".rollout-doctor-*"))
    if version == 1:
        assert output.read_bytes() == b"concurrent winner" if kind == "output_race" else not output.exists()


@pytest.mark.parametrize("endpoint", ["manifest", "source"])
@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "locked"])
def test_pinned_input_boundaries(tmp_path, capsys, endpoint, kind):
    import fcntl
    source, _, manifest = prepare(tmp_path, "codex", 1)
    pin = sha(manifest.read_bytes())
    target = manifest if endpoint == "manifest" else source
    lock = None
    if kind == "locked":
        lock = target.open("rb")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        data = target.read_bytes()
        target.unlink()
        if kind == "symlink":
            other = tmp_path / "target"
            other.write_bytes(data)
            target.symlink_to(other)
        elif kind == "fifo":
            os.mkfifo(target)
    before = inventory(tmp_path)
    try:
        result = invoke(capsys, manifest, "--expect-manifest-sha256", pin, "--json")
        assert result[0] == 2 and set(json.loads(result[1])) == {"file", "error"}
        assert inventory(tmp_path) == before
    finally:
        if lock:
            lock.close()


def test_manifest_mutation_during_existing_output_verification(tmp_path, capsys, monkeypatch):
    _, output, manifest = prepare(tmp_path, "claude", 1)
    manifests.apply(manifest)
    before_output = output.read_bytes()
    pin = sha(manifest.read_bytes())
    real = manifests.fingerprint
    def changed(stream):
        result = real(stream)
        if os.fstat(stream.fileno()).st_ino == output.stat().st_ino:
            manifest.write_bytes(manifest.read_bytes() + b" ")
        return result
    monkeypatch.setattr(manifests, "fingerprint", changed)
    result = invoke(capsys, manifest, "--expect-manifest-sha256", pin, "--json")
    assert result[0] == 2 and "manifest changed during apply" in result[2]
    assert output.read_bytes() == before_output and not list(tmp_path.glob(".rollout-doctor-*"))


@pytest.mark.parametrize("version", [1, 2])
def test_manifest_mutation_after_publication_emits_no_success_receipt(tmp_path, capsys, monkeypatch, version):
    from agentwrench.doctor import files
    _, output, manifest = prepare(tmp_path, "codex", version)
    saved, pin = json.loads(manifest.read_bytes()), sha(manifest.read_bytes())
    real = files._fsync_dir
    def changed(parent):
        real(parent)
        if sha(output.read_bytes()) == saved["output"]["sha256"]:
            manifest.write_bytes(manifest.read_bytes() + b" ")
    monkeypatch.setattr(files, "_fsync_dir", changed)
    result = invoke(capsys, manifest, "--expect-manifest-sha256", pin, "--json")
    assert result[0] == 2 and set(json.loads(result[1])) == {"file", "error"}
    assert "manifest changed during apply" in result[2]
    # Final failure cannot undo completed atomic publication; no fabricated rollback.
    assert sha(output.read_bytes()) == saved["output"]["sha256"]
    assert not list(tmp_path.glob(".rollout-doctor-*"))
