"""Explicit v1 migration preserves history, accepted boundaries and file safety."""
from io import BytesIO
import hashlib
import json
import os

import pytest

import agentwrench.doctor.audit as audit_module
from agentwrench.doctor.engine import DoctorError, scan
from .make_fixtures import cnode, item, result, use
from .test_cli import cli
from .test_engine import raw
from .test_incremental_audit import SEQUENCES


def v1(path, checkpoint, prefix, fmt="auto"):
    """Independent legacy schema and diagnostic hashes, without a v2 checkpoint."""
    report = scan(BytesIO(prefix), str(path), fmt)
    locations, offset = {}, 0
    for line, row in enumerate(prefix.splitlines(keepends=True), 1):
        locations[line] = (offset, offset + len(row))
        offset += len(row)
    emitted = {"f" * 64}  # A resolved historical finding must also survive.
    for issue in report.issues:
        value = issue.as_dict()
        value["byte_offset"], value["end_byte_offset"] = locations[issue.line]
        emitted.add(hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest())
    st = path.stat()
    state = {"version": 1, "file": str(path.resolve()), "device": st.st_dev, "inode": st.st_ino,
             "requested_format": fmt, "offset": len(prefix), "digest": hashlib.sha256(prefix).hexdigest(),
             "emitted": sorted(emitted)}
    checkpoint.write_text(json.dumps(state))
    return state


def frozen(path):
    st = path.stat()
    return path.read_bytes(), st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def migrate(path, old, new, *args):
    return cli("migrate-audit", path, "--checkpoint", old, "--out", new, "--json", *args)


@pytest.mark.parametrize("fmt,records", [("codex", SEQUENCES[0]), ("claude", SEQUENCES[1])])
def test_migration_cli_preserves_prefix_and_history(tmp_path, fmt, records):
    path, old, new = [tmp_path / name for name in ("s.jsonl", "v1.json", "v2.json")]
    prefix = raw(records[0]).replace(b'\n', b'\r\n')
    path.write_bytes(prefix + b'{"unaccepted":')
    previous = v1(path, old, prefix, fmt)
    before = frozen(path), frozen(old)
    run = migrate(path, old, new, "--format", fmt)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"file": str(path), "format": fmt, "status": "migrated",
        "checkpoint": str(new), "previous_checkpoint": str(old), "version": 2,
        "offset": len(prefix), "emitted_count": len(previous["emitted"])}
    state = json.loads(new.read_text())
    assert state == {**previous, "version": 2, "pairing": state["pairing"]}
    assert new.stat().st_mode & 0o777 == 0o600
    assert before == (frozen(path), frozen(old))
    path.write_bytes(prefix)
    assert audit_module.audit(path, new, fmt)["issues"] == []
    for record in records[1:]:
        with path.open("ab") as stream:
            stream.write(raw(record))
        actual = audit_module.audit(path, new, fmt)
        fresh = tmp_path / f"fresh-{path.stat().st_size}.json"
        replay = audit_module.audit(path, fresh, fmt)
        assert (actual["format"], actual["issue_count"], actual["offset"]) == (
            replay["format"], replay["issue_count"], replay["offset"])
        # Dedup filtering uses history from BEFORE this invocation.
        expected = [i for i in replay["issues"] if hashlib.sha256(
            json.dumps(i, sort_keys=True, ensure_ascii=True).encode()).hexdigest() not in previous["emitted"]]
        assert actual["issues"] == expected
        previous = json.loads(new.read_text())
    assert "f" * 64 in previous["emitted"]


@pytest.mark.parametrize("change", [
    {"version": 2}, {"version": True}, {"offset": True}, {"offset": -1}, {"offset": "1"},
    {"inode": -1}, {"device": False}, {"file": None}, {"requested_format": "other"},
    {"digest": "a" * 63}, {"digest": "A" * 64}, {"emitted": {}}, {"emitted": [None]},
    {"emitted": ["z" * 64]}, {"extra": 1},
])
def test_migration_refuses_malformed_legacy_state(tmp_path, change):
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    prefix = raw(item("function_call", "c"))
    path.write_bytes(prefix)
    state = v1(path, old, prefix)
    old.write_text(json.dumps({**state, **change}))
    before = frozen(path), frozen(old)
    run = migrate(path, old, new)
    assert run.returncode == 2 and "error" in json.loads(run.stdout)
    assert not new.exists() and before == (frozen(path), frozen(old))


@pytest.mark.parametrize("case", ["stale", "truncated", "rotated", "format", "path", "split", "joined"])
def test_migration_refuses_source_and_boundary_changes(tmp_path, case):
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    prefix = raw(item("function_call", "c"))
    path.write_bytes(prefix)
    state = v1(path, old, prefix)
    if case == "stale":
        path.write_bytes(prefix.replace(b'"c"', b'"d"'))
    elif case == "truncated":
        path.write_bytes(prefix[:10])
    elif case == "rotated":
        path.rename(tmp_path / "rotated")
        path.write_bytes(prefix)
    elif case == "path":
        state["file"] = str(tmp_path / "elsewhere")
    elif case == "split":
        state["offset"] = len(prefix) - 3
        state["digest"] = hashlib.sha256(prefix[:-3]).hexdigest()
    elif case == "joined":
        prefix = prefix.rstrip(b'\n')
        path.write_bytes(prefix + b'{}\n')
        state = v1(path, old, prefix)
    old.write_text(json.dumps(state))
    before = frozen(path), frozen(old)
    run = migrate(path, old, new, *(('--format', 'codex') if case == 'format' else ()))
    assert run.returncode == 2 and "error" in json.loads(run.stdout)
    assert not new.exists() and before == (frozen(path), frozen(old))


@pytest.mark.parametrize("records", SEQUENCES)
@pytest.mark.parametrize("explicit", [False, True])
def test_migration_decodes_only_prefix_once_and_resumes_graphs(tmp_path, monkeypatch, records, explicit):
    import agentwrench.doctor.audit_migration as migration
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    fmt = ("codex" if records[0]["type"] == "response_item" else "claude") if explicit else "auto"
    chunks = [raw(r) for r in records]
    # More than one record demonstrates a single pass; the invalid unaccepted
    # tail must never reach the decoder, nor be hashed into the accepted prefix.
    prefix = b"".join(chunks[:2])
    path.write_bytes(prefix + b'not accepted\n')
    previous = v1(path, old, prefix, fmt)
    original, decoded = migration.decode, []
    def watch(data):
        if data != old.read_bytes():
            decoded.append(data)
        return original(data)
    monkeypatch.setattr(migration, "decode", watch)
    migration.migrate_audit(path, old, new, fmt)
    assert decoded == chunks[:2]
    assert json.loads(new.read_text())["emitted"] == previous["emitted"]
    path.write_bytes(b"".join(chunks))
    actual = audit_module.audit(path, new, fmt)
    fresh = audit_module.audit(path, tmp_path / "fresh", fmt)
    assert actual["issue_count"] == fresh["issue_count"]
    assert actual["issues"] == [i for i in fresh["issues"] if hashlib.sha256(
        json.dumps(i, sort_keys=True, ensure_ascii=True).encode()).hexdigest() not in previous["emitted"]]
    assert json.loads(new.read_text())["pairing"] == json.loads((tmp_path / "fresh").read_text())["pairing"]


@pytest.mark.parametrize("fmt,first,last", [
    ("codex", item("function_call", "c", arguments="PRIVATE-ARG"), item("function_call_output", "c")),
    ("claude", cnode("assistant", "a", blocks=[use("c")]), cnode("user", "b", "a", [result("c")]))])
@pytest.mark.parametrize("tail", [b"", b"\r\n", b" \t\r\n", b" \t"])
def test_migration_valid_unterminated_prefix(tmp_path, fmt, first, last, tail):
    import agentwrench.doctor.audit_migration as migration
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    prefix = raw(first).rstrip(b'\n')
    path.write_bytes(prefix + tail)
    v1(path, old, prefix)
    migration.migrate_audit(path, old, new)
    saved = json.loads(new.read_text())
    assert saved["offset"] == len(prefix) and saved["pairing"]["terminated"] is False
    assert "PRIVATE-" not in new.read_text()
    with path.open('ab') as stream:
        stream.write((b'' if tail.endswith(b'\n') else b'\n') + raw(last))
    actual = audit_module.audit(path, new)
    fresh = audit_module.audit(path, tmp_path / "fresh")
    assert actual["issues"] == fresh["issues"] == [] and actual["issue_count"] == 0
    assert json.loads(new.read_text())["pairing"] == json.loads((tmp_path / "fresh").read_text())["pairing"]


@pytest.mark.parametrize("fmt", ["auto", "codex", "claude"])
def test_zero_cursor_ignores_tail_and_does_not_invent_history(tmp_path, fmt):
    import agentwrench.doctor.audit_migration as migration
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    path.write_bytes(b'{"type":')
    state = v1(path, old, b"", fmt)
    migration.migrate_audit(path, old, new, fmt)
    assert json.loads(new.read_text())["emitted"] == state["emitted"]
    assert audit_module.audit(path, new, fmt, True)["offset"] == 0


def test_migration_preserves_unseen_current_finding_for_next_audit(tmp_path):
    import agentwrench.doctor.audit_migration as migration
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    prefix = raw(item("function_call_output"))
    path.write_bytes(prefix)
    state = v1(path, old, prefix)
    state["emitted"] = []
    old.write_text(json.dumps(state))
    migration.migrate_audit(path, old, new)
    assert json.loads(new.read_text())["emitted"] == []
    assert [i["code"] for i in audit_module.audit(path, new)["issues"]] == ["RD103"]


@pytest.mark.parametrize("accepted", [b'[]\n', b'{bad}\n', b'{"x":1,"x":2}\n', b'\xff\n',
    b'{"type":"response_item","payload":null}\n', b'{"unknown":true}\n'])
def test_invalid_accepted_prefix_is_not_migrated(tmp_path, accepted):
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    prefix = raw(item("function_call", "c"))
    path.write_bytes(prefix)
    state = v1(path, old, prefix)
    path.write_bytes(accepted)
    state.update(offset=len(accepted), digest=hashlib.sha256(accepted).hexdigest())
    old.write_text(json.dumps(state))
    before = frozen(path), frozen(old)
    assert migrate(path, old, new).returncode == 2
    assert not new.exists() and before == (frozen(path), frozen(old))


@pytest.mark.parametrize("endpoint", ["source", "old", "new", "old-lock", "new-lock"])
@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_migration_refuses_unsafe_endpoints(tmp_path, endpoint, kind):
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    prefix = raw(item("function_call", "c"))
    path.write_bytes(prefix)
    v1(path, old, prefix)
    target = {"source": path, "old": old, "new": new,
              "old-lock": tmp_path / "old.lock", "new-lock": tmp_path / "new.lock"}[endpoint]
    if target.exists():
        target.rename(tmp_path / "saved")
    if kind == "symlink":
        target.symlink_to(tmp_path / "missing")
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    before = {p.name: (p.lstat().st_ino, p.lstat().st_mode, p.lstat().st_size, p.lstat().st_mtime_ns)
              for p in tmp_path.iterdir()}
    assert migrate(path, old, new).returncode == 2
    for p in tmp_path.iterdir():
        if p.name in before:
            assert before[p.name] == (p.lstat().st_ino, p.lstat().st_mode, p.lstat().st_size, p.lstat().st_mtime_ns)
    if endpoint != "new":
        assert not new.exists()


@pytest.mark.parametrize("case", ["existing", "missing-parent", "source", "old", "old-lock", "new-lock-source", "hardlink"])
def test_migration_refuses_destination_collisions(tmp_path, case):
    path, old, new = [tmp_path / name for name in ("s", "old", "new")]
    prefix = raw(item("function_call", "c"))
    path.write_bytes(prefix)
    v1(path, old, prefix)
    if case == "existing":
        new.write_text("keep this")
    elif case == "missing-parent":
        new = tmp_path / "missing" / "new"
    elif case == "source":
        new = path
    elif case == "old":
        new = old
    elif case == "old-lock":
        new = tmp_path / "old.lock"
    elif case == "new-lock-source":
        os.link(path, tmp_path / "new.lock")
    elif case == "hardlink":
        os.link(old, new)
    before = frozen(path), frozen(old)
    output_before = frozen(new) if new.exists() else None
    assert migrate(path, old, new).returncode == 2
    assert before == (frozen(path), frozen(old))
    assert frozen(new) == output_before if output_before else not new.exists()


@pytest.mark.parametrize("locked", ["source", "old", "old.lock", "new.lock"])
def test_migration_refuses_locked_inputs(tmp_path, locked):
    import fcntl
    from agentwrench.doctor.audit_migration import migrate_audit
    path, old, new = [tmp_path / name for name in ("source", "old", "new")]
    prefix = raw(item("function_call", "c"))
    path.write_bytes(prefix)
    v1(path, old, prefix)
    with (tmp_path / locked).open('ab') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DoctorError, match="locked"):
            migrate_audit(path, old, new)
    assert not new.exists()


@pytest.mark.parametrize("changed", ["source", "old", "rotate-source", "rotate-old"])
def test_migration_detects_changes_before_publication(tmp_path, monkeypatch, changed):
    import agentwrench.doctor.audit_migration as migration
    path, old, new = [tmp_path / name for name in ("source", "old", "new")]
    prefix = raw(item("function_call", "c"))
    path.write_bytes(prefix)
    v1(path, old, prefix)
    untouched = old if "source" in changed else path
    before = frozen(untouched)
    original = migration.pack
    def change_during_pack(*args):
        target = path if "source" in changed else old
        if changed.startswith("rotate"):
            data = target.read_bytes()
            target.rename(tmp_path / "rotated")
            target.write_bytes(data)
        else:
            with target.open('ab') as stream:
                stream.write(b' ')
        return original(*args)
    monkeypatch.setattr(migration, "pack", change_during_pack)
    with pytest.raises(DoctorError, match="changed while reading"):
        migration.migrate_audit(path, old, new)
    assert not new.exists() and frozen(untouched) == before
    assert not list(tmp_path.glob('.rollout-doctor-audit-*'))


@pytest.mark.parametrize("failure", ["concurrent", "link", "temp-fsync", "dir-fsync"])
def test_migration_publication_is_private_atomic_and_no_clobber(tmp_path, monkeypatch, failure):
    from agentwrench.doctor.audit_migration import migrate_audit
    path, old, new = [tmp_path / name for name in ("source", "old", "new")]
    prefix = raw(item("function_call", "c"))
    path.write_bytes(prefix)
    state = v1(path, old, prefix)
    before = frozen(path), frozen(old)
    link, fsync = os.link, os.fsync
    if failure in ("concurrent", "link"):
        def fail_link(src, dst, **kwargs):
            assert not new.exists()
            assert os.stat(src).st_mode & 0o777 == 0o600
            assert json.loads(open(src).read())["emitted"] == state["emitted"]
            if failure == "concurrent":
                new.write_bytes(b'concurrent writer')
                return link(src, dst, **kwargs)
            raise OSError("injected link failure")
        monkeypatch.setattr(os, "link", fail_link)
    elif failure == "temp-fsync":
        def fail_fsync(fd):
            raise OSError("injected temp fsync failure")
        monkeypatch.setattr(os, "fsync", fail_fsync)
    else:
        def fail_dir(path):
            raise OSError("injected directory fsync failure")
        monkeypatch.setattr(audit_module, "_fsync_dir", fail_dir)
    with pytest.raises(OSError):
        migrate_audit(path, old, new)
    assert before == (frozen(path), frozen(old))
    if failure == "concurrent":
        assert new.read_bytes() == b'concurrent writer'
    elif failure == "dir-fsync":
        assert json.loads(new.read_text())["version"] == 2
    else:
        assert not new.exists()
    assert not list(tmp_path.glob('.rollout-doctor-audit-*'))
