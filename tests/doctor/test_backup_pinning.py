"""Locked full manifest-byte pins for explicit backup verification and recovery."""
import json
import os
import tempfile

import pytest

from agentwrench.doctor import recovery
from agentwrench.doctor.cli import main
from agentwrench.doctor.engine import DoctorError
from .test_backup_verification import inventory
from .test_recovery import prepare, sha

COMMANDS = ["verify-backup", "recover"]
MISMATCH = "manifest SHA-256 differs from --expect-manifest-sha256; no backup opened"
INVALID = "--expect-manifest-sha256 requires exactly 64 lowercase hex characters"


def invoke(capsys, command, manifest, backup, output, *options):
    args = [command, str(manifest), "--backup", str(backup)]
    if command == "recover":
        args += ["--out", str(output)]
    code = main([*args, *options])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def refusal(result, manifest, error, json_output):
    code, out, err = result
    assert code == 2 and err == f"error: {error}\n"
    assert (json.loads(out) == {"file": str(manifest), "error": error}) if json_output else out == ""


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("fmt", ["codex", "claude"])
def test_wrong_pin_before_backup_open_or_staging(tmp_path, capsys, monkeypatch, command, fmt):
    _, manifest, backup, _ = prepare(tmp_path, fmt, apply=False)
    before = inventory(tmp_path)
    opened = []
    real_open = os.open

    def manifest_only(path, flags, *args, **kwargs):
        assert path == manifest, "wrong pin must never open backup or current source"
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("wrong pin attempted parse, staging or filesystem mutation")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", manifest_only)
        patch.setattr(recovery, "_read", forbidden)
        patch.setattr(tempfile, "mkstemp", forbidden)
        for name in ("link", "replace", "unlink", "mkdir"):
            patch.setattr(os, name, forbidden)
        for options in ([], ["--json"]):
            refusal(invoke(capsys, command, manifest, backup, tmp_path / "new",
                           "--expect-manifest-sha256", "0" * 64, *options), manifest, MISMATCH, options)
    assert opened == [manifest, manifest]
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("pin", ["", "0" * 63, "0" * 65, "A" * 64, "g" * 64,
                                 "0" * 63 + "\n", " " + "0" * 64, "0" * 64 + " ",
                                 "é" * 64, "０" * 64, "sha256:" + "a" * 64])
def test_invalid_pin_before_any_input_or_staging(tmp_path, capsys, monkeypatch, command, pin):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid pin attempted input open or staging")

    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(tempfile, "mkstemp", forbidden)
    for options in ([], ["--json"]):
        refusal(invoke(capsys, command, tmp_path / "missing", tmp_path / "backup", tmp_path / "new",
                       "--expect-manifest-sha256", pin, *options), tmp_path / "missing", INVALID, options)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("pin", [False, 0, [], {}, b"0" * 64])
def test_api_pin_type_is_strict(tmp_path, monkeypatch, command, pin):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid API pin opened input")

    monkeypatch.setattr(os, "open", forbidden)
    args = [tmp_path / "manifest", tmp_path / "backup"]
    if command == "recover":
        args += [tmp_path / "new"]
    function = recovery.recover if command == "recover" else recovery.verify_backup
    with pytest.raises(DoctorError, match="requires exactly 64 lowercase hex characters"):
        function(*args, expect_manifest_sha256=pin)


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_pin_value_is_argument_error(tmp_path, capsys, command):
    result = invoke(capsys, command, tmp_path / "plan", tmp_path / "backup", tmp_path / "new",
                    "--json", "--expect-manifest-sha256")
    assert result == (2, "", "error: argument --expect-manifest-sha256: expected one argument\n")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("current", ["missing", "modified"])
@pytest.mark.parametrize("kind", ["original", "empty", "binary", "multichunk"])
def test_exact_pin_keeps_receipts_and_arbitrary_bytes(tmp_path, capsys, command, fmt, current, kind):
    source, manifest, backup, original = prepare(tmp_path, fmt, apply=False)
    if kind != "original":
        data = {"empty": b"", "binary": b"PRIVATE\x00\xff\r\n",
                "multichunk": b"PRIVATE\x00\xff" + b"x" * (2 * 1024 * 1024) + b"tail"}[kind]
        value = json.loads(manifest.read_text())
        value["source"]["sha256"] = sha(data)
        manifest.write_text(json.dumps(value))
        backup.write_bytes(data)
    else:
        data = original
    if current == "missing":
        source.unlink()
    else:
        source.write_bytes(b"PRIVATE CURRENT SESSION")
    before = inventory(tmp_path)
    out = tmp_path / "new"
    pin = sha(manifest.read_bytes())
    for options in ([], ["--json"]):
        baseline = invoke(capsys, command, manifest, backup, out, *options)
        assert baseline[0] == 0 and baseline[2] == ""
        if command == "recover":
            assert out.read_bytes() == data and out.stat().st_mode & 0o777 == 0o600
            out.unlink()
        result = invoke(capsys, command, manifest, backup, out, "--expect-manifest-sha256", pin, *options)
        assert result == baseline
        if command == "recover":
            expected = {"status": "recovered", "version": 2, "manifest": str(manifest), "backup": str(backup),
                        "source": {"path": str(source), "sha256": sha(data)},
                        "output": {"path": str(out), "sha256": sha(data)}, "restored_bytes": len(data)}
            human = f"recovered: {out} ({len(data)} bytes) SHA-256 {sha(data)}\n"
            assert out.read_bytes() == data and out.stat().st_mode & 0o777 == 0o600
            out.unlink()
        else:
            expected = {"status": "verified", "version": 2, "manifest": str(manifest), "backup": str(backup),
                        "expected_sha256": sha(data), "actual_sha256": sha(data), "verified_bytes": len(data)}
            human = f"verified: {backup} ({len(data)} bytes) SHA-256 {sha(data)}\n"
        assert (json.loads(result[1]) == expected) if options else result[1] == human
        assert before == inventory(tmp_path)


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("change", ["whitespace", "keys", "newline", "historical_report", "multichunk"])
def test_full_manifest_bytes_not_semantic_json(tmp_path, capsys, monkeypatch, command, fmt, change):
    _, manifest, backup, _ = prepare(tmp_path, fmt, apply=False)
    original = manifest.read_bytes()
    value = json.loads(original)
    if change == "whitespace":
        altered = json.dumps(value, separators=(",", ":")).encode()
    elif change == "keys":
        altered = json.dumps(dict(reversed(list(value.items()))), indent=2).encode() + b"\n"
    elif change == "newline":
        altered = original.rstrip(b"\n")
    elif change == "multichunk":
        altered = original + b" " * (2 * 1024 * 1024) + b"\n"
    else:
        value["report"]["issues"] = ["PRIVATE replaced historical metadata"]
        altered = json.dumps(value).encode()
    assert altered != original and json.loads(altered) == value
    manifest.write_bytes(altered)
    before = inventory(tmp_path)
    out = tmp_path / "new"
    real_open = os.open

    def manifest_only(path, *args, **kwargs):
        assert path == manifest
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", manifest_only)
        refusal(invoke(capsys, command, manifest, backup, out, "--expect-manifest-sha256", sha(original), "--json"),
                manifest, MISMATCH, True)
    assert before == inventory(tmp_path)
    assert invoke(capsys, command, manifest, backup, out, "--expect-manifest-sha256", sha(altered), "--json")[0] == 0
    if command == "recover":
        out.unlink()
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("command", COMMANDS)
def test_single_open_existing_hash_passes_locks_and_publication(tmp_path, capsys, monkeypatch, command):
    import fcntl
    from agentwrench.doctor import manifest as manifest_module

    source, manifest, backup, original = prepare(tmp_path, apply=False)
    pin = sha(manifest.read_bytes())
    before = inventory(tmp_path)
    out = tmp_path / "new"
    opened, hashes, reads, staged, linked = [], [], [], [], []
    real_open, real_hash, real_bytes = os.open, recovery.fingerprint, recovery._read_backup
    real_temp, real_link = tempfile.mkstemp, os.link

    def open_file(path, flags, *args, **kwargs):
        if not flags & (os.O_CREAT | os.O_DIRECTORY):
            assert path in (manifest, backup), "never open current session or reopen another path"
            assert flags & (os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND) == 0
            opened.append(path)
        else:
            assert command == "recover"
        return real_open(path, flags, *args, **kwargs)

    def hash_file(stream):
        hashes.append(stream.name)
        return real_hash(stream)

    def locked():
        for path in (manifest, backup):
            with path.open("rb") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def read_bytes(stream, target=None):
        locked()
        reads.append(stream.name)
        return real_bytes(stream, target)

    def mkstemp(*args, **kwargs):
        assert command == "recover" and opened == [manifest, backup]
        locked()
        staged.append(True)
        return real_temp(*args, **kwargs)

    def link(temp, destination, **kwargs):
        locked()
        assert len(hashes) == 3 and destination == out
        assert temp.read_bytes() == original and temp.stat().st_mode & 0o777 == 0o600
        assert kwargs == {"follow_symlinks": False}
        linked.append(True)
        return real_link(temp, destination, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("backup pin attempted source scan or repair replay")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_file)
        patch.setattr(recovery, "fingerprint", hash_file)
        patch.setattr(recovery, "_read_backup", read_bytes)
        patch.setattr(tempfile, "mkstemp", mkstemp)
        patch.setattr(os, "link", link)
        patch.setattr(recovery, "scan", forbidden)
        for name in ("scan", "apply", "make_plan", "validate_plan", "render"):
            patch.setattr(manifest_module, name, forbidden)
        assert invoke(capsys, command, manifest, backup, out, "--expect-manifest-sha256", pin, "--json")[0] == 0
    assert opened == [manifest, backup] and len(hashes) == 3 and len(reads) == 1
    assert staged == linked == ([True] if command == "recover" else [])
    if command == "recover":
        out.unlink()
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("command,stage", [(c, s) for c in COMMANDS for s in
    ("initial_hash", "manifest_parse", "backup_read", "final_hash", "staged_fsync")
    if c == "recover" or s != "staged_fsync"])
@pytest.mark.parametrize("change", ["bytes", "replace", "unlink", "symlink", "metadata", "restore_mtime"])
def test_pinned_manifest_mutations_refuse(tmp_path, capsys, monkeypatch, command, stage, change):
    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    original = manifest.read_bytes()
    pin = sha(original)
    saved = manifest.stat()
    before_source = inventory(tmp_path)[source]
    injected = False

    def mutate():
        nonlocal injected
        if injected:
            return
        injected = True
        if change == "bytes":
            manifest.write_bytes(original + b" ")
        elif change == "replace":
            other = tmp_path / "replacement"
            other.write_bytes(original)
            os.replace(other, manifest)
        elif change == "unlink":
            manifest.unlink()
        elif change == "symlink":
            other = tmp_path / "moved"
            manifest.rename(other)
            manifest.symlink_to(other)
        elif change == "metadata":
            manifest.chmod(0o400)
        else:
            manifest.write_bytes(original)
            os.utime(manifest, ns=(saved.st_atime_ns, saved.st_mtime_ns))

    real_hash, real_parse, real_bytes, real_fsync = recovery.fingerprint, recovery._read, recovery._read_backup, os.fsync
    hashes = 0

    def hash_file(stream):
        nonlocal hashes
        hashes += 1
        result = real_hash(stream)
        if (stage == "initial_hash" and hashes == 1) or (stage == "final_hash" and hashes == 3):
            mutate()
        return result

    def parse(stream):
        result = real_parse(stream)
        if stage == "manifest_parse":
            mutate()
        return result

    def read_bytes(stream, target=None):
        result = real_bytes(stream, target)
        if stage == "backup_read":
            mutate()
        return result

    def fsync(fd):
        real_fsync(fd)
        if stage == "staged_fsync":
            mutate()

    monkeypatch.setattr(recovery, "fingerprint", hash_file)
    monkeypatch.setattr(recovery, "_read", parse)
    monkeypatch.setattr(recovery, "_read_backup", read_bytes)
    monkeypatch.setattr(os, "fsync", fsync)
    out = tmp_path / "new"
    code, stdout, err = invoke(capsys, command, manifest, backup, out, "--expect-manifest-sha256", pin, "--json")
    assert injected and code == 2 and set(json.loads(stdout)) == {"file", "error"}
    assert err.startswith("error: ") and "Traceback" not in err
    assert not out.exists() and not list(tmp_path.glob(".rollout-doctor-*"))
    assert inventory(tmp_path)[source] == before_source


@pytest.mark.parametrize("command,case", [(c, s) for c in COMMANDS for s in
    ("v1", "schema", "policy", "backup_hash", "backup_source_alias", "manifest_source_alias",
     "manifest_backup_alias", "backup_symlink", "manifest_symlink", "backup_lock", "manifest_lock",
     "output_source", "output_backup", "output_existing", "output_race")
    if c == "recover" or not s.startswith("output_")])
def test_exact_pin_retains_policies_and_no_clobber(tmp_path, capsys, monkeypatch, command, case):
    import fcntl

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    value = json.loads(manifest.read_text())
    if case == "v1":
        value["version"] = 1
        del value["options"]["in_place"], value["options"]["backup"]
        value["output"]["path"] = str(tmp_path / "v1-output")
    elif case == "schema":
        value["extra"] = True
    elif case == "policy":
        value["options"]["backup"] = False
    elif case == "manifest_source_alias":
        value["source"]["path"] = value["output"]["path"] = value["report"]["file"] = str(manifest)
    manifest.write_text(json.dumps(value))
    if case == "backup_hash":
        backup.write_bytes(b"PRIVATE incorrect backup")
    elif case == "backup_source_alias":
        backup.unlink()
        backup.hardlink_to(source)
    elif case == "manifest_backup_alias":
        backup = manifest
    elif case in ("backup_symlink", "manifest_symlink"):
        target = backup if case == "backup_symlink" else manifest
        moved = tmp_path / "moved"
        target.rename(moved)
        target.symlink_to(moved)
    elif case == "output_source":
        source.unlink()
        out = source
    elif case == "output_backup":
        out.hardlink_to(backup)
    elif case == "output_existing":
        out.write_bytes(b"PRIVATE concurrent winner")
    elif case == "output_race":
        real_link = os.link

        def race(temp, destination, **kwargs):
            destination.write_bytes(b"PRIVATE concurrent winner")
            return real_link(temp, destination, **kwargs)

        monkeypatch.setattr(os, "link", race)
    pin = sha(manifest.read_bytes())
    before = inventory(tmp_path)
    lock = None
    try:
        if case in ("backup_lock", "manifest_lock"):
            lock = (backup if case == "backup_lock" else manifest).open("rb")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, stdout, err = invoke(capsys, command, manifest, backup, out, "--expect-manifest-sha256", pin, "--json")
    finally:
        if lock:
            lock.close()
    assert code == 2 and set(json.loads(stdout)) == {"file", "error"}
    assert err.startswith("error: ") and "PRIVATE" not in stdout + err
    assert not list(tmp_path.glob(".rollout-doctor-*"))
    if case == "output_race":
        assert out.read_bytes() == b"PRIVATE concurrent winner"
        out.unlink()
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("command,missing", [("verify-backup", "--backup"), ("recover", "--backup"), ("recover", "--out")])
def test_pins_do_not_add_discovery_or_implicit_output(tmp_path, capsys, command, missing):
    _, manifest, backup, _ = prepare(tmp_path, apply=False)
    args = [command, str(manifest), "--expect-manifest-sha256", sha(manifest.read_bytes()), "--json"]
    if missing != "--backup":
        args += ["--backup", str(backup)]
    if command == "recover" and missing != "--out":
        args += ["--out", str(tmp_path / "new")]
    before = inventory(tmp_path)
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and missing in captured.err
    assert before == inventory(tmp_path)
