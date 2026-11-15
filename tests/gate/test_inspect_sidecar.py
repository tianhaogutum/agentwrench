"""Inspection validates one sidecar, discloses only metadata, and performs no writes."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentwrench.gate.cli import main

FIX = Path(__file__).parent / "fixtures"
SECRET = "PAYLOAD_DO_NOT_PRINT_☃"


def envelope(raw=SECRET.encode(), losses=None):
    return {"schema_version": 1, "source": "claude-stream", "target": "normalize",
            "original": {"encoding": "utf-8", "jsonl": raw.decode(),
                         "sha256": hashlib.sha256(raw).hexdigest()},
            "output_sha256": "1" * 64, "losses": [] if losses is None else losses}


def loss(**changes):
    return dict(source_line=1, pointer="/opaque~1path~0", reason="unsupported_field",
                disposition="omitted") | changes


def expected(report):
    return {"schema_version": 1, "sidecar_schema_version": 1,
            "source": report["source"], "target": report["target"],
            "original": {"encoding": "utf-8", "bytes": len(report["original"]["jsonl"].encode()),
                         "sha256": report["original"]["sha256"], "verified": True},
            "primary": {"encoding": "utf-8", "expected_sha256": report["output_sha256"]},
            "primary_verified": False,
            "losses": {"total": len(report["losses"]), "by_disposition": {
                kind: sum(x["disposition"] == kind for x in report["losses"])
                for kind in ("omitted", "json_string")}}}


def guard_io(monkeypatch, allowed=None):
    """Only the supplied sidecar file may be opened; no embedded or primary paths."""
    import agentwrench.gate.cli as cli
    real_open = Path.open
    calls = []

    def read_only(path, mode="r", *args, **kwargs):
        assert path == allowed and mode == "rb", f"unexpected file access: {path} {mode}"
        calls.append(path)
        return real_open(path, mode, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("inspection invoked a primary verifier, adapter or writer")

    monkeypatch.setattr(Path, "open", read_only)
    for name in ("write_new", "write_new_reports", "verified_original", "verify_sidecar", "normalize"):
        monkeypatch.setattr(cli, name, forbidden)
    return calls


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("source", ["claude_attachments.jsonl", "codex_exec_ok.jsonl"])
def test_inspect_missing_primary(action, source, tmp_path, capsys, monkeypatch):
    raw = b' \r\n' + (FIX / source).read_bytes().replace(b'Done', 'Done ☃😀'.encode()).rstrip(b'\n')
    stream, primary, sidecar = (tmp_path / name for name in ("raw.jsonl", "primary.json", "sidecar.json"))
    stream.write_bytes(raw)
    assert main([action, str(stream), "-o", str(primary), "--sidecar", str(sidecar)]) == 0
    capsys.readouterr()
    report = json.loads(sidecar.read_bytes())
    primary.unlink()
    stream.unlink()
    before = sidecar.read_bytes(), sidecar.stat().st_mtime_ns
    calls = guard_io(monkeypatch, sidecar)
    assert main(["inspect-sidecar", str(sidecar)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected(report)
    assert captured.out == json.dumps(expected(report), indent=2, sort_keys=True) + "\n"
    assert not captured.err and calls == [sidecar]
    assert "c3ludGhldGlj" not in captured.out and "Done" not in captured.out
    monkeypatch.undo()
    assert (sidecar.read_bytes(), sidecar.stat().st_mtime_ns) == before
    assert set(tmp_path.iterdir()) == {sidecar}


class Pipe:
    def __init__(self, raw, fail=False):
        self.data = io.BytesIO(b"consumed" + raw)
        self.data.read(8)
        self.requests = []
        self.fail = fail

    def read(self, size):
        assert 0 < size <= 1024 * 1024
        self.requests.append(size)
        if self.fail:
            raise OSError(SECRET)
        return self.data.read(min(size, 7))


def stdin(monkeypatch, raw, fail=False):
    import agentwrench.gate.cli as cli
    pipe = Pipe(raw, fail)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=pipe))
    return pipe


@pytest.mark.parametrize("raw", [b"", ' \r\n雪☃😀\r\nno-final-newline'.encode()])
@pytest.mark.parametrize("losses", [[], [loss(), loss(disposition="json_string", reason=SECRET), loss()]])
def test_inspect_short_stdin_at_inclusive_caps(monkeypatch, capsys, raw, losses):
    import agentwrench.gate.sidecar as integrity
    report = envelope(raw, losses)
    encoded = json.dumps(report).encode()
    monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", len(encoded))
    monkeypatch.setattr(integrity, "MAX_STREAM_BYTES", len(raw))
    pipe = stdin(monkeypatch, encoded)
    guard_io(monkeypatch)
    assert main(["inspect-sidecar", "-"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected(report) and not captured.err
    assert pipe.data.tell() == 8 + len(encoded) and not pipe.data.closed
    assert pipe.requests[-1] == 1
    assert SECRET not in captured.out and "reason" not in captured.out and "pointer" not in captured.out


INVALID_FIELDS = [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
    ("source_version", 1), ("target_version", 1), ("original.extra", SECRET),
    ("source", []), ("source", "future-v2"), ("target", {}), ("target", "export-agentevals"),
    ("original", None), ("original.encoding", "base64"), ("original.encoding", "utf-16"),
    ("original.jsonl", 1), ("original.jsonl", "\ud800"), ("original.jsonl", "tampered"),
    ("original.sha256", "0" * 64), ("original.sha256", "A" * 64),
    ("output_sha256", 0), ("output_sha256", "invalid"),
    ("losses", {}), ("losses", [None]), ("losses", [loss(extra=SECRET)]),
    ("losses", [loss(source_line=True)]), ("losses", [loss(source_line=1.0)]),
    ("losses", [loss(source_line=0)]), ("losses", [loss(source_line=100001)]),
    ("losses", [loss(pointer=1)]), ("losses", [loss(pointer="bad")]),
    ("losses", [loss(pointer="/bad~2")]), ("losses", [loss(reason="")]),
    ("losses", [loss(reason=[])]), ("losses", [loss(disposition=[])]),
    ("losses", [loss(disposition=SECRET)]),
]


@pytest.mark.parametrize("field,value", INVALID_FIELDS)
@pytest.mark.parametrize("mode", ["file", "stdin"])
def test_inspect_invalid_envelope_no_disclosure(monkeypatch, tmp_path, capsys, field, value, mode):
    report = envelope()
    parent = report
    *parents, key = field.split(".")
    for name in parents:
        parent = parent[name]
    parent[key] = value
    assert_rejected(monkeypatch, tmp_path, capsys, json.dumps(report).encode(), mode)


MALFORMED = [b"", b"{", b"[]", b"\xff", b'[' * 2000,
             b'{"schema_version":1}', b'{"extra":"PAYLOAD_DO_NOT_PRINT"}',
             b'{"PAYLOAD_DO_NOT_PRINT":1,"PAYLOAD_DO_NOT_PRINT":2}',
             b'{"original":{"jsonl":"PAYLOAD_DO_NOT_PRINT","jsonl":"x"}}',
             b'{"extra":NaN}', b'{"extra":1e999}']


@pytest.mark.parametrize("raw", MALFORMED)
@pytest.mark.parametrize("mode", ["file", "stdin"])
def test_inspect_malformed_no_disclosure(monkeypatch, tmp_path, capsys, raw, mode):
    assert_rejected(monkeypatch, tmp_path, capsys, raw, mode)


def assert_rejected(monkeypatch, tmp_path, capsys, raw, mode):
    sidecar = tmp_path / "sidecar.json"
    if mode == "file":
        sidecar.write_bytes(raw)
    else:
        stdin(monkeypatch, raw)
    guard_io(monkeypatch, sidecar if mode == "file" else None)
    assert main(["inspect-sidecar", str(sidecar) if mode == "file" else "-"]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err.startswith("agentwrench gate:")
    assert "PAYLOAD_DO_NOT_PRINT" not in captured.err and "Traceback" not in captured.err
    monkeypatch.undo()
    assert set(tmp_path.iterdir()) == ({sidecar} if mode == "file" else set())
    if mode == "file":
        assert sidecar.read_bytes() == raw


@pytest.mark.parametrize("which", ["sidecar", "original"])
def test_inspect_one_over_limit(monkeypatch, tmp_path, capsys, which):
    import agentwrench.gate.sidecar as integrity
    raw = json.dumps(envelope()).encode()
    monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES" if which == "sidecar" else "MAX_STREAM_BYTES",
                        len(raw) - 1 if which == "sidecar" else len(SECRET.encode()) - 1)
    assert_rejected(monkeypatch, tmp_path, capsys, raw, "stdin")


def test_inspect_real_64mib_overflow(tmp_path, capsys):
    sidecar = tmp_path / "oversized.json"
    with sidecar.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)
    assert main(["inspect-sidecar", str(sidecar)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err.startswith("agentwrench gate:")
    assert set(tmp_path.iterdir()) == {sidecar}


def test_inspect_read_failure(monkeypatch, capsys):
    pipe = stdin(monkeypatch, b"", fail=True)
    guard_io(monkeypatch)
    assert main(["inspect-sidecar", "-"]) == 2
    captured = capsys.readouterr()
    assert not captured.out and SECRET not in captured.err and "Traceback" not in captured.err
    assert not pipe.data.closed


def test_inspect_missing_sidecar(tmp_path, capsys):
    assert main(["inspect-sidecar", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err.startswith("agentwrench gate:") and not list(tmp_path.iterdir())


def test_inspect_dash_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(b"invalid")
    stdin(monkeypatch, json.dumps(envelope()).encode())
    assert main(["inspect-sidecar", "-"]) == 0
    assert json.loads(capsys.readouterr().out) == expected(envelope())
    assert main(["inspect-sidecar", "./-"]) == 2
    assert not capsys.readouterr().out
    assert Path("-").read_bytes() == b"invalid" and set(tmp_path.iterdir()) == {tmp_path / "-"}


@pytest.mark.parametrize("extra", [["primary.json"], ["-o", "out.json"], ["--stdout"]])
def test_inspect_accepts_only_one_input(extra, monkeypatch, capsys):
    class NoStdin:
        @property
        def buffer(self):
            pytest.fail("argument error accessed stdin")
    import agentwrench.gate.cli as cli
    monkeypatch.setattr(cli.sys, "stdin", NoStdin())
    with pytest.raises(SystemExit) as exc:
        main(["inspect-sidecar", "-"] + extra)
    assert exc.value.code == 2 and not capsys.readouterr().out
