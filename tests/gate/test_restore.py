"""Sidecar integrity checks precede output; verification alone never writes files."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentwrench.gate.cli import main

FIX = Path(__file__).parent / "fixtures"


def integrity_args(action, sidecar, primary, destination):
    args = [action, str(sidecar), str(primary)]
    return args + ["-o", str(destination)] if action == "restore" else args


@pytest.fixture
def bundle(tmp_path, capsys):
    def create(action="normalize", source="claude_attachments.jsonl"):
        raw = b' \r\n' + (FIX / source).read_bytes().replace(b'Done', 'Done ☃'.encode()).replace(b'\n', b'\r\n').rstrip(b'\r\n')
        stream = tmp_path / "input.jsonl"
        stream.write_bytes(raw)
        primary, sidecar = tmp_path / "primary.json", tmp_path / "sidecar.json"
        assert main([action, str(stream), "-o", str(primary), "--sidecar", str(sidecar)]) == 0
        capsys.readouterr()
        return raw, primary, sidecar
    return create


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("source", ["claude_attachments.jsonl", "codex_exec_ok.jsonl"])
def test_restore_exact_original_bytes(bundle, tmp_path, capsys, action, source):
    raw, primary, sidecar = bundle(action, source)
    restored = tmp_path / "restored.jsonl"
    assert main(["restore", str(sidecar), str(primary), "-o", str(restored)]) == 0
    assert restored.read_bytes() == raw
    assert restored.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert captured.out == "Restored verified original JSONL.\n" and not captured.err
    assert not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("tamper, diagnostic", [
    ("original", "original SHA-256 mismatch"),
    ("original_hash", "original SHA-256 mismatch"),
    ("primary", "output SHA-256 mismatch"),
    ("output_hash", "output SHA-256 mismatch"),
])
@pytest.mark.parametrize("integrity_action", ["restore", "verify-sidecar"])
def test_restore_tampered_bytes_or_hashes_create_nothing(bundle, tmp_path, capsys, tamper, diagnostic, integrity_action):
    _, primary, sidecar = bundle()
    report = json.loads(sidecar.read_bytes())
    if tamper == "original":
        report["original"]["jsonl"] += "\n"
    elif tamper == "original_hash":
        report["original"]["sha256"] = "0" * 64
    elif tamper == "primary":
        primary.write_bytes(primary.read_bytes() + b"\n")
    else:
        report["output_sha256"] = "0" * 64
    sidecar.write_text(json.dumps(report))
    restored = tmp_path / "restored.jsonl"
    assert main(integrity_args(integrity_action, sidecar, primary, restored)) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err
    assert not restored.exists() and not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("primary_mode", ["file", "stdin", "sidecar-stdin"])
@pytest.mark.parametrize("blocked", ["file", "symlink", "dangling", "directory", "primary", "sidecar", "missing_parent"])
def test_restore_refuses_destination_without_changing_inputs(bundle, tmp_path, capsys, monkeypatch, blocked, primary_mode):
    _, primary, sidecar = bundle()
    before = {p: p.read_bytes() for p in (primary, sidecar)}
    restored = tmp_path / "restored.jsonl"
    sentinel = tmp_path / "keep"
    sentinel.write_bytes(b"keep")
    if blocked == "file":
        restored.write_bytes(b"keep")
    elif blocked in ("symlink", "dangling"):
        restored.symlink_to(sentinel if blocked == "symlink" else tmp_path / "absent")
    elif blocked == "directory":
        restored.mkdir()
    elif blocked in ("primary", "sidecar"):
        restored = primary if blocked == "primary" else sidecar
    else:
        restored = tmp_path / "missing" / "restored.jsonl"
    primary_arg = str(primary)
    sidecar_arg = str(sidecar)
    if primary_mode == "stdin":
        install_primary_pipe(monkeypatch, primary.read_bytes(), restore=True)
        primary_arg = "-"
    elif primary_mode == "sidecar-stdin":
        install_primary_pipe(monkeypatch, sidecar.read_bytes(), restore=True)
        sidecar_arg = "-"
    assert main(["restore", sidecar_arg, primary_arg, "-o", str(restored)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err.startswith("agentwrench gate:")
    assert all(p.read_bytes() == data for p, data in before.items())
    assert sentinel.read_bytes() == b"keep"
    if blocked == "file":
        assert restored.read_bytes() == b"keep"
    elif blocked in ("symlink", "dangling"):
        assert restored.is_symlink()
        assert not (tmp_path / "absent").exists()
    elif blocked == "directory":
        assert restored.is_dir() and not list(restored.iterdir())
    elif blocked == "missing_parent":
        assert not restored.parent.exists()
    assert not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("field, value, diagnostic", [
    ("schema_version", True, "expected integer 1"),
    ("schema_version", 1.0, "expected integer 1"),
    ("schema_version", 2, "expected integer 1"),
    ("source", [], "unsupported source"),
    ("target", "export-agentevals", "unsupported target"),
    ("original", None, "expected an object"),
    ("original.encoding", "utf-16", "expected utf-8"),
    ("original.jsonl", 1, "expected a string"),
    ("original.jsonl", "\ud800", "surrogates"),
    ("original.sha256", "A" * 64, "expected 64 lowercase"),
    ("output_sha256", "invalid", "expected 64 lowercase"),
    ("losses", {}, "expected a list"),
    ("losses", [None], "expected an object"),
    ("losses", [{"source_line": True, "pointer": "", "reason": "x", "disposition": "omitted"}], "expected nonnegative integer"),
    ("losses", [{"source_line": 0, "pointer": "", "reason": "x", "disposition": "omitted"}], "expected 1..100000"),
    ("losses", [{"source_line": 1, "pointer": "/bad~", "reason": "x", "disposition": "omitted"}], "expected RFC 6901"),
    ("losses", [{"source_line": 1, "pointer": "", "reason": "", "disposition": "omitted"}], "expected a nonempty string"),
    ("losses", [{"source_line": 1, "pointer": "", "reason": "x", "disposition": "other"}], "unsupported disposition"),
])
@pytest.mark.parametrize("integrity_action", ["restore", "verify-sidecar"])
def test_restore_invalid_envelope_creates_nothing(bundle, tmp_path, capsys, field, value, diagnostic, integrity_action):
    _, primary, sidecar = bundle()
    report = json.loads(sidecar.read_bytes())
    container = report
    *parents, key = field.split(".")
    for parent in parents:
        container = container[parent]
    container[key] = value
    sidecar.write_text(json.dumps(report))
    restored = tmp_path / "restored.jsonl"
    assert main(integrity_args(integrity_action, sidecar, primary, restored)) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err
    assert not restored.exists() and not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("malformed, diagnostic", [
    (b'{', "invalid sidecar JSON"),
    (b'\xff', "invalid sidecar JSON"),
    (b'[]', "expected an object"),
    (b'{"schema_version":1,"schema_version":1}', "duplicate JSON key"),
    (b'{"original":{"jsonl":"a","jsonl":"b"}}', "duplicate JSON key"),
    (b'{"extra":NaN}', "invalid JSON number"),
    (b'{"extra":1e999}', "nonfinite JSON number"),
    (b'{"schema_version":1}', "missing key"),
    (b'{"extra":1}', "unknown key"),
    (b'[' * 2000, "invalid sidecar JSON"),
])
@pytest.mark.parametrize("integrity_action", ["restore", "verify-sidecar"])
def test_restore_malformed_json_creates_nothing(bundle, tmp_path, capsys, malformed, diagnostic, integrity_action):
    _, primary, sidecar = bundle()
    sidecar.write_bytes(malformed)
    restored = tmp_path / "restored.jsonl"
    assert main(integrity_args(integrity_action, sidecar, primary, restored)) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err
    assert not restored.exists() and not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("which", ["sidecar", "primary"])
@pytest.mark.parametrize("integrity_action", ["restore", "verify-sidecar"])
def test_restore_real_64mib_input_cap(bundle, tmp_path, capsys, which, integrity_action):
    _, primary, sidecar = bundle()
    path = sidecar if which == "sidecar" else primary
    # Sparse files exercise the actual public byte limit without huge fixtures.
    with path.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)
    restored = tmp_path / "restored.jsonl"
    assert main(integrity_args(integrity_action, sidecar, primary, restored)) == 2
    captured = capsys.readouterr()
    assert not captured.out and "exceeds 64 MiB" in captured.err
    assert not restored.exists() and not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("integrity_action", ["restore", "verify-sidecar"])
def test_restore_limits_are_inclusive(bundle, tmp_path, capsys, monkeypatch, integrity_action):
    import agentwrench.gate.sidecar as verifier
    raw, primary, sidecar = bundle()
    # Scale caps down to the real byte lengths; exact limit must still succeed.
    monkeypatch.setattr(verifier, "MAX_SIDECAR_BYTES", sidecar.stat().st_size)
    monkeypatch.setattr(verifier, "MAX_OUTPUT_BYTES", primary.stat().st_size)
    monkeypatch.setattr(verifier, "MAX_STREAM_BYTES", len(raw))
    restored = tmp_path / "restored.jsonl"
    assert main(integrity_args(integrity_action, sidecar, primary, restored)) == 0
    if integrity_action == "restore":
        assert restored.read_bytes() == raw
    else:
        assert not restored.exists()
    capsys.readouterr()
    monkeypatch.setattr(verifier, "MAX_STREAM_BYTES", len(raw) - 1)
    rejected = tmp_path / "too-big.jsonl"
    assert main(integrity_args(integrity_action, sidecar, primary, rejected)) == 2
    captured = capsys.readouterr()
    assert not captured.out and "original exceeds 64 MiB" in captured.err
    assert not rejected.exists()


@pytest.mark.parametrize("which", ["sidecar", "primary"])
@pytest.mark.parametrize("integrity_action", ["restore", "verify-sidecar"])
def test_restore_missing_inputs_do_not_create_output(bundle, tmp_path, capsys, which, integrity_action):
    _, primary, sidecar = bundle()
    (sidecar if which == "sidecar" else primary).unlink()
    restored = tmp_path / "restored.jsonl"
    assert main(integrity_args(integrity_action, sidecar, primary, restored)) == 2
    captured = capsys.readouterr()
    assert not captured.out and "No such file" in captured.err
    assert not restored.exists()


@pytest.mark.parametrize("primary_mode", ["file", "stdin", "sidecar-stdin"])
def test_restore_link_race_preserves_other_writer_file(bundle, tmp_path, capsys, monkeypatch, primary_mode):
    import agentwrench.gate.cli as cli
    _, primary, sidecar = bundle()
    primary_arg = str(primary)
    sidecar_arg = str(sidecar)
    if primary_mode == "stdin":
        install_primary_pipe(monkeypatch, primary.read_bytes(), restore=True)
        primary_arg = "-"
    elif primary_mode == "sidecar-stdin":
        install_primary_pipe(monkeypatch, sidecar.read_bytes(), restore=True)
        sidecar_arg = "-"
    restored = tmp_path / "restored.jsonl"
    real_link = cli.os.link

    def race(source, destination):
        Path(destination).write_bytes(b"another writer")
        real_link(source, destination)

    monkeypatch.setattr(cli.os, "link", race)
    assert main(["restore", sidecar_arg, primary_arg, "-o", str(restored)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "File exists" in captured.err
    assert restored.read_bytes() == b"another writer"
    assert not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("primary_mode", ["file", "stdin", "sidecar-stdin"])
def test_restore_write_failure_creates_nothing(bundle, tmp_path, capsys, monkeypatch, primary_mode):
    import agentwrench.gate.cli as cli
    _, primary, sidecar = bundle()
    primary_arg = str(primary)
    sidecar_arg = str(sidecar)
    if primary_mode == "stdin":
        install_primary_pipe(monkeypatch, primary.read_bytes(), restore=True)
        primary_arg = "-"
    elif primary_mode == "sidecar-stdin":
        install_primary_pipe(monkeypatch, sidecar.read_bytes(), restore=True)
        sidecar_arg = "-"
    restored = tmp_path / "restored.jsonl"

    def fail(_):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(cli.os, "fsync", fail)
    assert main(["restore", sidecar_arg, primary_arg, "-o", str(restored)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "synthetic fsync failure" in captured.err
    assert not restored.exists() and not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("source", ["claude_attachments.jsonl", "codex_exec_ok.jsonl"])
def test_verify_sidecar_exact_deterministic_report_without_writes(bundle, tmp_path, capsys, monkeypatch, action, source):
    import agentwrench.gate.cli as cli
    raw, primary, sidecar = bundle(action, source)
    before = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in tmp_path.iterdir()}

    def no_write(*args, **kwargs):
        pytest.fail("verification must not invoke any report/restore writer")

    monkeypatch.setattr(cli, "write_new", no_write)
    monkeypatch.setattr(cli, "write_new_reports", no_write)
    expected = {
        "schema_version": 1, "verified": True,
        "source": "claude-stream" if source.startswith("claude") else "codex-exec",
        "target": "normalize" if action == "normalize" else "agentevals",
        "original": {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
        "output": {"sha256": hashlib.sha256(primary.read_bytes()).hexdigest(), "bytes": primary.stat().st_size},
    }
    for _ in range(2):
        assert main(["verify-sidecar", str(sidecar), str(primary)]) == 0
        captured = capsys.readouterr()
        assert captured.out == json.dumps(expected, indent=2, sort_keys=True) + "\n"
        assert not captured.err
        assert {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("action, extra", [("restore", []), ("verify-sidecar", ["-o"]), ("verify-sidecar", ["--output"])])
def test_sidecar_output_argument_contract(bundle, tmp_path, capsys, action, extra):
    _, primary, sidecar = bundle()
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    args = [action, str(sidecar), str(primary)]
    if extra:
        args += extra + [str(tmp_path / "unexpected.jsonl")]
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out and "error:" in captured.err
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


class PrimaryPipe:
    """Binary short reads with no seek/context-manager/text-stream interface."""
    def __init__(self, data, *, fail=False):
        self.data = io.BytesIO(data)
        self.requests = []
        self.fail = fail

    def read(self, size):
        assert 0 < size <= 1024 * 1024
        self.requests.append(size)
        if self.fail:
            raise OSError("synthetic stdin read failure")
        return self.data.read(min(size, 7))


def install_primary_pipe(monkeypatch, data, *, fail=False, restore=False):
    import agentwrench.gate.cli as cli
    pipe = PrimaryPipe(data, fail=fail)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=pipe))

    def no_write(*args, **kwargs):
        pytest.fail("stdin verification must not invoke a writer")

    if not restore:
        monkeypatch.setattr(cli, "write_new", no_write)
        monkeypatch.setattr(cli, "write_new_reports", no_write)
    return pipe


def directory_snapshot(root):
    return {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in root.iterdir()}


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("source", ["claude_attachments.jsonl", "codex_exec_ok.jsonl"])
def test_verify_stdin_matches_file_at_exact_cap(bundle, tmp_path, capsys, monkeypatch, action, source):
    import agentwrench.gate.sidecar as integrity
    _, primary, sidecar = bundle(action, source)
    assert main(["verify-sidecar", str(sidecar), str(primary)]) == 0
    expected = capsys.readouterr()
    monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", primary.stat().st_size)
    pipe = install_primary_pipe(monkeypatch, primary.read_bytes())
    before = directory_snapshot(tmp_path)
    assert main(["verify-sidecar", str(sidecar), "-"]) == 0
    captured = capsys.readouterr()
    assert captured.out == expected.out and not captured.err
    assert pipe.data.tell() == primary.stat().st_size and not pipe.data.closed
    assert len(pipe.requests) > 1 and pipe.requests[-1] == 1  # EOF probe at the inclusive cap
    assert directory_snapshot(tmp_path) == before


@pytest.mark.parametrize("integrity_action", ["verify-sidecar", "restore"])
@pytest.mark.parametrize("payload", [b"\xff\x00 \r\n\xc3\xa9", b""])
def test_verify_stdin_hashes_exact_binary_bytes(bundle, tmp_path, capsys, monkeypatch, payload, integrity_action):
    import agentwrench.gate.sidecar as integrity
    _, _, sidecar = bundle()
    report = json.loads(sidecar.read_bytes())
    report["output_sha256"] = hashlib.sha256(payload).hexdigest()
    sidecar.write_text(json.dumps(report))
    monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", len(payload))
    pipe = install_primary_pipe(monkeypatch, payload, restore=integrity_action == "restore")
    before = directory_snapshot(tmp_path)
    restored = tmp_path / "restored.jsonl"
    assert main(integrity_args(integrity_action, sidecar, "-", restored)) == 0
    captured = capsys.readouterr()
    assert not captured.err
    if integrity_action == "restore":
        assert restored.read_bytes() == json.loads(sidecar.read_bytes())["original"]["jsonl"].encode("utf-8")
        assert captured.out == "Restored verified original JSONL.\n"
        restored.unlink()
    else:
        assert json.loads(captured.out)["output"] == {"sha256": report["output_sha256"], "bytes": len(payload)}
    assert pipe.data.tell() == len(payload) and not pipe.data.closed
    assert directory_snapshot(tmp_path) == before


@pytest.mark.parametrize("integrity_action", ["verify-sidecar", "restore"])
@pytest.mark.parametrize("failure, diagnostic", [
    ("mismatch", "output SHA-256 mismatch"),
    ("empty", "output SHA-256 mismatch"),
    ("primary-over", "primary output exceeds 64 MiB"),
    ("malformed-sidecar", "invalid sidecar JSON"),
    ("sidecar-over", "sidecar exceeds 64 MiB"),
    ("original-tampered", "original SHA-256 mismatch"),
    ("invalid-loss", "unsupported disposition"),
    ("io-error", "synthetic stdin read failure"),
])
def test_verify_stdin_rejection_creates_nothing(bundle, tmp_path, capsys, monkeypatch, failure, diagnostic, integrity_action):
    import agentwrench.gate.sidecar as integrity
    _, primary, sidecar = bundle()
    data = primary.read_bytes()
    if failure == "mismatch":
        data += b"\n"
    elif failure == "empty":
        data = b""
    elif failure == "primary-over":
        monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", len(data) - 1)
        data += b"unread trailing bytes"
    elif failure == "malformed-sidecar":
        sidecar.write_bytes(b"{")
    elif failure == "sidecar-over":
        monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", sidecar.stat().st_size - 1)
    elif failure in ("original-tampered", "invalid-loss"):
        report = json.loads(sidecar.read_bytes())
        if failure == "original-tampered":
            report["original"]["jsonl"] += "\n"
        else:
            report["losses"][0]["disposition"] = "invalid"
        sidecar.write_text(json.dumps(report))
    pipe = install_primary_pipe(monkeypatch, data, fail=failure == "io-error")
    before = directory_snapshot(tmp_path)
    assert main(integrity_args(integrity_action, sidecar, "-", tmp_path / "restored.jsonl")) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err
    assert not pipe.data.closed and directory_snapshot(tmp_path) == before
    if failure in ("malformed-sidecar", "sidecar-over", "original-tampered", "invalid-loss"):
        assert not pipe.requests  # Invalid metadata must not consume/wait for stdin.
    elif failure == "primary-over":
        assert pipe.data.tell() == integrity.MAX_OUTPUT_BYTES + 1


def test_verify_stdin_dash_and_literal_filename(bundle, tmp_path, capsys, monkeypatch):
    _, primary, sidecar = bundle()
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(b"literal file")
    pipe = install_primary_pipe(monkeypatch, primary.read_bytes())
    assert main(["verify-sidecar", str(sidecar), "-"]) == 0
    capsys.readouterr()
    # ./- explicitly names a file; restore still requires an output destination.
    Path("-").write_bytes(primary.read_bytes())
    reads = list(pipe.requests)
    for args in (["verify-sidecar", str(sidecar), "./-"], ["restore", str(sidecar), "-"]):
        if args[0] == "restore":
            with pytest.raises(SystemExit) as exc:
                main(args)  # Output remains required, even if stdin is available.
            assert exc.value.code == 2
        else:
            assert main(args) == 0
    assert pipe.requests == reads


def test_restore_explicit_dot_dash_is_a_file_path(bundle, tmp_path, capsys, monkeypatch):
    raw, primary, sidecar = bundle()
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(primary.read_bytes())
    pipe = PrimaryPipe(b"", fail=True)
    import agentwrench.gate.cli as cli
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=pipe))
    restored = tmp_path / "restored.jsonl"
    assert main(["restore", str(sidecar), "./-", "-o", str(restored)]) == 0
    assert restored.read_bytes() == raw and not pipe.requests


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("source", ["claude_attachments.jsonl", "codex_exec_ok.jsonl"])
def test_restore_stdin_exact_bytes_at_cap(bundle, tmp_path, capsys, monkeypatch, action, source):
    import agentwrench.gate.sidecar as integrity
    raw, primary, sidecar = bundle(action, source)
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(b"bare dash must read stdin, not this file")
    before = directory_snapshot(tmp_path)
    monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", primary.stat().st_size)
    pipe = install_primary_pipe(monkeypatch, b"skip" + primary.read_bytes(), restore=True)
    pipe.data.read(4)  # Borrowed streams are consumed from their current position.
    restored = tmp_path / "restored.jsonl"
    assert main(["restore", str(sidecar), "-", "-o", str(restored)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Restored verified original JSONL.\n" and not captured.err
    assert restored.read_bytes() == raw and restored.stat().st_mode & 0o777 == 0o600
    assert not pipe.data.closed and pipe.data.tell() == primary.stat().st_size + 4
    assert len(pipe.requests) > 1 and pipe.requests[-1] == 1
    restored.unlink()
    assert directory_snapshot(tmp_path) == before


def test_restore_stdin_output_required_before_read(bundle, tmp_path, capsys, monkeypatch):
    _, _, sidecar = bundle()
    pipe = install_primary_pipe(monkeypatch, b"", fail=True)
    before = directory_snapshot(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["restore", str(sidecar), "-"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out and "required" in captured.err
    assert not pipe.requests and not pipe.data.closed
    assert directory_snapshot(tmp_path) == before


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("source", ["claude_attachments.jsonl", "codex_exec_ok.jsonl"])
@pytest.mark.parametrize("integrity_action", ["verify-sidecar", "restore"])
def test_sidecar_stdin_matches_file_with_short_reads_at_cap(bundle, tmp_path, capsys, monkeypatch, action, source, integrity_action):
    import agentwrench.gate.sidecar as integrity
    raw, primary, sidecar = bundle(action, source)
    # Actual UTF-8 and CRLF in the envelope; hash metadata must stay identical.
    data = json.dumps(json.loads(sidecar.read_bytes()), ensure_ascii=False, indent=2).replace("\n", "\r\n").encode()
    sidecar.write_bytes(data)
    assert main(["verify-sidecar", str(sidecar), str(primary)]) == 0
    expected = capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(b"bare sidecar dash must select stdin")
    before = directory_snapshot(tmp_path)
    monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", len(data))
    pipe = install_primary_pipe(monkeypatch, b"skip" + data, restore=integrity_action == "restore")
    pipe.data.read(4)
    restored = tmp_path / "restored.jsonl"
    assert main(integrity_args(integrity_action, "-", primary, restored)) == 0
    captured = capsys.readouterr()
    assert not captured.err
    if integrity_action == "restore":
        assert captured.out == "Restored verified original JSONL.\n"
        assert restored.read_bytes() == raw and restored.stat().st_mode & 0o777 == 0o600
        restored.unlink()
    else:
        assert captured.out == expected.out
    assert pipe.data.tell() == len(data) + 4 and not pipe.data.closed
    assert len(pipe.requests) > 1 and pipe.requests[-1] == 1
    assert directory_snapshot(tmp_path) == before


@pytest.mark.parametrize("failure, diagnostic", [
    ("json", "invalid sidecar JSON"),
    ("utf8", "invalid sidecar JSON"),
    ("duplicate", "duplicate JSON key"),
    ("schema", "expected integer 1"),
    ("loss", "unsupported disposition"),
    ("original", "original SHA-256 mismatch"),
    ("primary", "output SHA-256 mismatch"),
    ("missing-primary", "No such file"),
    ("over", "sidecar exceeds 64 MiB"),
    ("read", "synthetic stdin read failure"),
])
@pytest.mark.parametrize("integrity_action", ["verify-sidecar", "restore"])
def test_sidecar_stdin_rejection_creates_nothing(bundle, tmp_path, capsys, monkeypatch, failure, diagnostic, integrity_action):
    _, primary, sidecar = bundle()
    report = json.loads(sidecar.read_bytes())
    data = sidecar.read_bytes()
    if failure == "json":
        data = b"{"
    elif failure == "utf8":
        data = b"\xff"
    elif failure == "duplicate":
        data = b'{"schema_version":1,"schema_version":1}'
    elif failure in ("schema", "loss", "original"):
        if failure == "schema":
            report["schema_version"] = True
        elif failure == "loss":
            report["losses"][0]["disposition"] = "invalid"
        else:
            report["original"]["jsonl"] += "\n"
        data = json.dumps(report).encode()
    elif failure == "primary":
        primary.write_bytes(primary.read_bytes() + b"\n")
    elif failure == "missing-primary":
        primary.unlink()
    elif failure == "over":
        import agentwrench.gate.sidecar as integrity
        monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", len(data) - 1)
        data += b"unread trailing bytes"
    pipe = install_primary_pipe(monkeypatch, data, fail=failure == "read", restore=integrity_action == "restore")
    before = directory_snapshot(tmp_path)
    real_open = Path.open

    def check_primary(path, *args, **kwargs):
        if path == primary:
            assert failure in ("primary", "missing-primary"), "validate sidecar before opening primary"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", check_primary)
    assert main(integrity_args(integrity_action, "-", primary, tmp_path / "restored.jsonl")) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err
    assert not pipe.data.closed
    # Undo open guard so the snapshot can read the unchanged primary.
    monkeypatch.setattr(Path, "open", real_open)
    assert directory_snapshot(tmp_path) == before
    if failure == "over":
        assert pipe.data.tell() == integrity.MAX_SIDECAR_BYTES + 1


@pytest.mark.parametrize("integrity_action", ["verify-sidecar", "restore"])
def test_sidecar_stdin_conflicting_dashes_rejected_before_access(tmp_path, capsys, monkeypatch, integrity_action):
    import agentwrench.gate.cli as cli

    class NoStdin:
        @property
        def buffer(self):
            pytest.fail("two stdin arguments must be rejected before accessing stdin")

    monkeypatch.setattr(cli.sys, "stdin", NoStdin())
    before = directory_snapshot(tmp_path)
    assert main(integrity_args(integrity_action, "-", "-", tmp_path / "restored.jsonl")) == 2
    captured = capsys.readouterr()
    assert not captured.out and "only one input may use stdin" in captured.err
    assert directory_snapshot(tmp_path) == before


def test_sidecar_stdin_literal_dot_dash_and_restore_compatibility(bundle, tmp_path, capsys, monkeypatch):
    raw, primary, sidecar = bundle()
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(sidecar.read_bytes())
    pipe = install_primary_pipe(monkeypatch, b"", fail=True, restore=True)
    assert main(["verify-sidecar", "./-", str(primary)]) == 0
    assert not capsys.readouterr().err
    restored = tmp_path / "restored.jsonl"
    # Explicit ./- remains a file for both commands, with no stdin access.
    assert main(["restore", "./-", str(primary), "-o", str(restored)]) == 0
    assert restored.read_bytes() == raw and not pipe.requests


@pytest.mark.parametrize("primary_arg", ["primary.json", "-"])
def test_restore_sidecar_stdin_output_required_before_access(tmp_path, capsys, monkeypatch, primary_arg):
    import agentwrench.gate.cli as cli

    class NoStdin:
        @property
        def buffer(self):
            pytest.fail("missing output must fail before accessing stdin")

    monkeypatch.setattr(cli.sys, "stdin", NoStdin())
    before = directory_snapshot(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["restore", "-", primary_arg])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out and "required" in captured.err
    assert directory_snapshot(tmp_path) == before


@pytest.mark.parametrize("failure", ["read", "over"])
def test_restore_sidecar_stdin_primary_file_failure(bundle, tmp_path, capsys, monkeypatch, failure):
    from contextlib import nullcontext
    import agentwrench.gate.sidecar as integrity

    _, primary, sidecar = bundle()
    before = directory_snapshot(tmp_path)
    pipe = install_primary_pipe(monkeypatch, sidecar.read_bytes(), restore=True)
    primary_reader = PrimaryPipe(primary.read_bytes(), fail=failure == "read")
    monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", primary.stat().st_size - 1)
    real_open = Path.open

    def open_primary(path, *args, **kwargs):
        if path == primary:
            assert args == ("rb",)  # Keep primary reads binary and bounded.
            return nullcontext(primary_reader)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_primary)
    assert main(["restore", "-", str(primary), "-o", str(tmp_path / "restored.jsonl")]) == 2
    captured = capsys.readouterr()
    diagnostic = "synthetic stdin read failure" if failure == "read" else "primary output exceeds 64 MiB"
    assert not captured.out and diagnostic in captured.err
    assert primary_reader.requests and not pipe.data.closed
    if failure == "over":
        assert primary_reader.data.tell() == integrity.MAX_OUTPUT_BYTES + 1
    monkeypatch.setattr(Path, "open", real_open)
    assert directory_snapshot(tmp_path) == before


class BinaryOutput:
    """Borrowed binary sink: short writes, no text/seek/close interface."""
    def __init__(self, *, failure=None):
        self.data = bytearray()
        self.writes = 0
        self.flushes = 0
        self.failure = failure

    def write(self, data):
        self.writes += 1
        if self.failure == "write":
            raise BrokenPipeError("synthetic broken stdout")
        if self.failure == "zero":
            return 0
        if self.failure == "none":
            return None
        chunk = bytes(data[:7])
        self.data.extend(chunk)
        return len(chunk)

    def flush(self):
        self.flushes += 1
        if self.failure == "flush":
            raise OSError("synthetic stdout flush failure")


def install_restore_stdout(monkeypatch, *, failure=None):
    import agentwrench.gate.cli as cli
    output = BinaryOutput(failure=failure)
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(buffer=output))

    def no_write(*args, **kwargs):
        pytest.fail("stdout restoration must not create/stage files")

    monkeypatch.setattr(cli, "write_new", no_write)
    monkeypatch.setattr(cli, "write_new_reports", no_write)
    monkeypatch.setattr(cli.tempfile, "NamedTemporaryFile", no_write)
    return output


@pytest.mark.parametrize("layout", ["files", "primary-stdin", "sidecar-stdin"])
@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("source", ["claude_attachments.jsonl", "codex_exec_ok.jsonl"])
@pytest.mark.parametrize("empty", [False, True])
def test_restore_stdout_exact_bytes_without_files(bundle, tmp_path, capsys, monkeypatch, layout, action, source, empty):
    import agentwrench.gate.sidecar as integrity
    raw, primary, sidecar = bundle(action, source)
    if empty:
        raw = b""
        report = json.loads(sidecar.read_bytes())
        report["original"].update(jsonl="", sha256=hashlib.sha256(raw).hexdigest())
        sidecar.write_text(json.dumps(report))
    monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", sidecar.stat().st_size)
    monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", primary.stat().st_size)
    monkeypatch.setattr(integrity, "MAX_STREAM_BYTES", len(raw))
    sidecar_arg, primary_arg = str(sidecar), str(primary)
    pipe = None
    if layout != "files":
        data = (primary if layout == "primary-stdin" else sidecar).read_bytes()
        pipe = install_primary_pipe(monkeypatch, b"skip" + data, restore=True)
        pipe.data.read(4)
        if layout == "primary-stdin":
            primary_arg = "-"
        else:
            sidecar_arg = "-"
    before = directory_snapshot(tmp_path)
    output = install_restore_stdout(monkeypatch)
    assert main(["restore", sidecar_arg, primary_arg, "--stdout"]) == 0
    assert bytes(output.data) == raw
    assert capsys.readouterr().err == "Restored verified original JSONL.\n"
    assert output.flushes == 1
    if pipe:
        assert not pipe.data.closed and pipe.data.tell() == len(data) + 4
        assert pipe.requests[-1] == 1  # Inclusive limit: one-byte EOF probe.
    assert directory_snapshot(tmp_path) == before


@pytest.mark.parametrize("action", ["restore", "verify-sidecar"])
@pytest.mark.parametrize("target", ["normalize", "export-agentevals"])
def test_empty_original_shared_verification_and_file_restore(bundle, tmp_path, capsys, action, target):
    _, primary, sidecar = bundle(target)
    report = json.loads(sidecar.read_bytes())
    report["original"].update(jsonl="", sha256=hashlib.sha256(b"").hexdigest())
    sidecar.write_text(json.dumps(report))
    restored = tmp_path / "empty.jsonl"
    assert main(integrity_args(action, sidecar, primary, restored)) == 0
    captured = capsys.readouterr()
    assert not captured.err
    if action == "restore":
        assert restored.read_bytes() == b"" and restored.stat().st_mode & 0o777 == 0o600
    else:
        assert json.loads(captured.out)["original"]["bytes"] == 0
        assert not restored.exists()


@pytest.mark.parametrize("layout", ["files", "primary-stdin", "sidecar-stdin"])
@pytest.mark.parametrize("failure, diagnostic", [
    ("json", "invalid sidecar JSON"),
    ("duplicate", "duplicate JSON key"),
    ("schema", "expected integer 1"),
    ("loss", "unsupported disposition"),
    ("original", "original SHA-256 mismatch"),
    ("output", "output SHA-256 mismatch"),
    ("sidecar-over", "sidecar exceeds 64 MiB"),
    ("original-over", "original exceeds 64 MiB"),
    ("primary-over", "primary output exceeds 64 MiB"),
    ("sidecar-read", "synthetic stdin read failure"),
    ("primary-read", "synthetic stdin read failure"),
])
def test_restore_stdout_rejects_before_any_output(bundle, tmp_path, capsys, monkeypatch, layout, failure, diagnostic):
    from contextlib import nullcontext
    import agentwrench.gate.sidecar as integrity
    raw, primary, sidecar = bundle()
    report = json.loads(sidecar.read_bytes())
    if failure == "json":
        sidecar.write_bytes(b"{")
    elif failure == "duplicate":
        sidecar.write_bytes(b'{"schema_version":1,"schema_version":1}')
    elif failure in ("schema", "loss", "original", "output"):
        if failure == "schema":
            report["schema_version"] = True
        elif failure == "loss":
            report["losses"][0]["disposition"] = "invalid"
        elif failure == "original":
            report["original"]["sha256"] = "0" * 64
        else:
            report["output_sha256"] = "0" * 64
        sidecar.write_text(json.dumps(report))
    elif failure == "sidecar-over":
        monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", sidecar.stat().st_size - 1)
    elif failure == "original-over":
        monkeypatch.setattr(integrity, "MAX_STREAM_BYTES", len(raw) - 1)
    elif failure == "primary-over":
        monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", primary.stat().st_size - 1)
    before = directory_snapshot(tmp_path)
    sidecar_arg, primary_arg = str(sidecar), str(primary)
    pipe = None
    if layout != "files":
        data = (primary if layout == "primary-stdin" else sidecar).read_bytes()
        pipe = install_primary_pipe(monkeypatch, data, restore=True,
                                    fail=failure == ("primary-read" if layout == "primary-stdin" else "sidecar-read"))
        if layout == "primary-stdin":
            primary_arg = "-"
        else:
            sidecar_arg = "-"
    real_open = Path.open

    def open_input(path, *args, **kwargs):
        if path == (primary if failure == "primary-read" else sidecar) and failure.endswith("-read"):
            return nullcontext(PrimaryPipe(b"", fail=True))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_input)
    output = install_restore_stdout(monkeypatch)
    assert main(["restore", sidecar_arg, primary_arg, "--stdout"]) == 2
    captured = capsys.readouterr()
    assert diagnostic in captured.err and "Restored" not in captured.err
    assert not output.data and output.writes == output.flushes == 0
    if pipe:
        assert not pipe.data.closed
        if layout == "primary-stdin" and failure not in ("output", "primary-over", "primary-read"):
            assert not pipe.requests  # Sidecar errors precede primary stdin reads.
    monkeypatch.setattr(Path, "open", real_open)
    assert directory_snapshot(tmp_path) == before


@pytest.mark.parametrize("extra", [["--stdout", "-o", "fresh.jsonl"], ["--output", "fresh.jsonl", "--stdout"], []])
def test_restore_stdout_destination_contract_before_io(tmp_path, capsys, monkeypatch, extra):
    import agentwrench.gate.cli as cli

    class NoStdio:
        @property
        def buffer(self):
            pytest.fail("invalid destinations must fail before stdio access")

    def no_open(*args, **kwargs):
        pytest.fail("invalid destinations must fail before opening inputs")

    monkeypatch.setattr(cli.sys, "stdin", NoStdio())
    monkeypatch.setattr(cli.sys, "stdout", NoStdio())
    monkeypatch.setattr(Path, "open", no_open)
    with pytest.raises(SystemExit) as exc:
        main(["restore", "-", "missing-primary.json", *extra])
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_restore_stdout_two_stdin_inputs_before_access(tmp_path, capsys, monkeypatch):
    import agentwrench.gate.cli as cli

    class NoStdio:
        @property
        def buffer(self):
            pytest.fail("two stdin inputs must fail before stdio access")

    monkeypatch.setattr(cli.sys, "stdin", NoStdio())
    monkeypatch.setattr(cli.sys, "stdout", NoStdio())
    assert main(["restore", "-", "-", "--stdout"]) == 2
    assert "only one input may use stdin" in capsys.readouterr().err


@pytest.mark.parametrize("failure, diagnostic", [
    ("write", "synthetic broken stdout"),
    ("flush", "synthetic stdout flush failure"),
    ("zero", "stdout write made no progress"),
    ("none", "stdout write made no progress"),
])
def test_restore_stdout_write_failure_is_io_error(bundle, tmp_path, capsys, monkeypatch, failure, diagnostic):
    _, primary, sidecar = bundle()
    before = directory_snapshot(tmp_path)
    output = install_restore_stdout(monkeypatch, failure=failure)
    assert main(["restore", str(sidecar), str(primary), "--stdout"]) == 2
    captured = capsys.readouterr()
    assert diagnostic in captured.err and "Restored" not in captured.err and "Traceback" not in captured.err
    if failure != "flush":
        assert not output.data
    assert directory_snapshot(tmp_path) == before


def test_restore_stdout_primary_failure_after_partial_read(bundle, tmp_path, capsys, monkeypatch):
    import agentwrench.gate.cli as cli
    _, _, sidecar = bundle()

    class LateFailure(PrimaryPipe):
        def read(self, size):
            if self.requests:
                raise OSError("late primary read failure")
            return super().read(size)

    pipe = LateFailure(b"partial primary")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=pipe))
    output = install_restore_stdout(monkeypatch)
    assert main(["restore", str(sidecar), "-", "--stdout"]) == 2
    assert pipe.data.tell() == 7 and not pipe.data.closed
    assert not output.data and output.writes == output.flushes == 0
    assert "late primary read failure" in capsys.readouterr().err
