"""Normalize/export raw binary stdin contracts, using borrowed streams with deliberately short reads."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentwrench.gate.cli as cli
import agentwrench.gate.sidecar as integrity
import agentwrench.gate.streams as streams

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(params=["normalize", "export-agentevals"])
def action(request):
    return request.param


class BinaryPipe:
    # No readline, seek, close or context-manager interface is available.
    def __init__(self, data, *, chunk=7, fail_after=None, prefix=b""):
        self.data = io.BytesIO(prefix + data)
        self.data.seek(len(prefix))
        self.chunk = chunk
        self.fail_after = fail_after
        self.requests = []
        self.consumed = 0

    def read(self, size):
        assert 0 < size <= 1024 * 1024 + 1
        self.requests.append(size)
        if self.fail_after is not None and self.consumed >= self.fail_after:
            raise OSError("synthetic raw stdin read failure")
        raw = self.data.read(min(size, self.chunk))
        self.consumed += len(raw)
        return raw


def install(monkeypatch, raw, **kwargs):
    pipe = BinaryPipe(raw, **kwargs)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=pipe))
    return pipe


def snapshot(root):
    return {p.name: (p.lstat().st_mode, p.lstat().st_mtime_ns,
                     str(p.readlink()) if p.is_symlink() else p.read_bytes())
            for p in root.iterdir()}


def no_writer(*args, **kwargs):
    pytest.fail("invalid input must fail before staging or writing")


@pytest.mark.parametrize("fixture,source", [("codex_exec_ok.jsonl", "codex-exec"),
                                           ("claude_attachments.jsonl", "claude-stream")])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("destination", ["stdout", "file"])
@pytest.mark.parametrize("sidecar", [False, True])
def test_raw_stdin_matches_file(action, tmp_path, monkeypatch, capsys, fixture, source,
                                      explicit, destination, sidecar):
    raw = b" \r\n" + (FIX / fixture).read_bytes().replace(b"Done", "Done ☃😀".encode()).replace(b"\n", b"\r\n").rstrip(b"\r\n")
    raw += b'\r\n{"type":"future"}'
    stream = tmp_path / "input.jsonl"
    stream.write_bytes(raw)
    expected_primary, expected_sidecar = tmp_path / "expected.json", tmp_path / "expected.originals.json"
    assert cli.main([action, str(stream), "-o", str(expected_primary), "--sidecar", str(expected_sidecar), "--root", "/workspace/demo"]) == 0
    expected_warnings = capsys.readouterr().err
    pipe = install(monkeypatch, raw, prefix=b"already consumed garbage\xff")
    primary, originals = tmp_path / "output.json", tmp_path / "originals.json"
    args = [action, "-", "--root", "/workspace/demo"]
    if explicit:
        args += ["--from", source]
    if destination == "file":
        args += ["-o", str(primary)]
    if sidecar:
        args += ["--sidecar", str(originals)]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    if action == "normalize":
        assert not captured.err
    else:
        assert json.loads(captured.err.splitlines()[0].split("ignored events: ", 1)[1])["future"] == 1
        assert "warning: loss accounting:" in captured.err
        assert ("use --sidecar" in captured.err) == (not sidecar)
        assert captured.err.split("agentwrench gate: use --sidecar")[0] == expected_warnings
    actual = primary.read_bytes() if destination == "file" else captured.out.encode()
    assert actual == expected_primary.read_bytes()
    assert pipe.consumed == len(raw) and pipe.requests
    assert pipe.read(1) == b""  # Still usable after consumption.
    if destination == "file":
        assert not captured.out and primary.stat().st_mode & 0o777 == 0o600
    if sidecar:
        assert originals.read_bytes() == expected_sidecar.read_bytes()
        assert originals.stat().st_mode & 0o777 == 0o600
        verified = integrity.verify_sidecar(originals, io.BytesIO(actual))
        assert verified.original == raw
        assert verified.as_dict()["original"] == {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        # Both actual restore destinations are exercised by the installed pipelines.
        assert integrity.verified_original(originals, io.BytesIO(actual)) == raw


@pytest.mark.parametrize("raw,diagnostic", [
    (b"", "empty stream"), (b" \r\n\t\n", "empty stream"),
    (b'\n{"type":', "line 2"), (b'{"type":"turn.started"}\n\xff', "line 2"),
    (b'[]', "expected an object"), (b'{"type":"turn.started","type":"turn.completed"}', "duplicate JSON key"),
    (b'{"type":"turn.completed","extra":NaN}', "invalid JSON number"),
    (b'{"type":"future"}', "auto-detect"),
    (b'{"type":"turn.started"}\n{"type":"assistant"}', "auto-detect"),
    (b'{"type":"item.completed","item":{}}', "line 1"),
])
@pytest.mark.parametrize("destination", ["stdout", "file"])
def test_raw_stdin_invalid_has_no_prefix_or_files(action, tmp_path, monkeypatch, capsys, raw, diagnostic, destination):
    install(monkeypatch, raw)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    before = snapshot(tmp_path)
    args = [action, "-", "--sidecar", str(tmp_path / "originals.json")]
    if destination == "file":
        args += ["-o", str(tmp_path / "output.json")]
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("fail_after", [0, 7, 28, 75])
def test_raw_stdin_read_failure(action, tmp_path, monkeypatch, capsys, fail_after):
    install(monkeypatch, b'{"type":"turn.completed"}\n' * 3, fail_after=fail_after)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main([action, "-", "--sidecar", str(tmp_path / "originals.json")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "synthetic raw stdin read failure" in captured.err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("limit", ["MAX_STREAM_BYTES", "MAX_LINE_BYTES", "MAX_LINES"])
@pytest.mark.parametrize("terminated", [False, True])
def test_raw_stdin_inclusive_limits(action, tmp_path, monkeypatch, capsys, limit, terminated):
    raw = b' \r\n{"type":"turn.completed"}' + (b'\r\n' if terminated else b'')
    cap = {"MAX_STREAM_BYTES": len(raw), "MAX_LINE_BYTES": len(raw) - 3, "MAX_LINES": 2}[limit]
    monkeypatch.setattr(streams, limit, cap)
    pipe = install(monkeypatch, raw, chunk=1)  # Split CRLF, JSON and EOF boundaries.
    assert cli.main([action, "-"]) == 0
    result = json.loads(capsys.readouterr().out)
    if action == "normalize":
        assert result["events"][-1]["source_line"] == 2
    else:
        assert result == []
    assert pipe.consumed == len(raw)
    monkeypatch.setattr(streams, limit, cap - 1)
    pipe = install(monkeypatch, raw)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main([action, "-", "--sidecar", str(tmp_path / "originals.json")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "stream exceeds limit" in captured.err
    if limit == "MAX_STREAM_BYTES":
        assert pipe.consumed == cap  # New cap + one overflow-detection byte.
    assert not list(tmp_path.iterdir())


def test_raw_stdin_dash_and_literal_file(action, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    literal = b'{"type":"item.completed","item":{"id":"a","type":"agent_message","text":"file"}}\n{"type":"turn.completed"}'
    piped = literal.replace(b'"file"', b'"stdin"')
    Path("-").write_bytes(literal)
    pipe = install(monkeypatch, piped)
    assert cli.main([action, "-"]) == 0
    first = json.loads(capsys.readouterr().out)
    requests = list(pipe.requests)
    assert cli.main([action, "./-"]) == 0
    second = json.loads(capsys.readouterr().out)
    if action == "normalize":
        assert first["events"][0]["text"] == "stdin" and second["events"][0]["text"] == "file"
    else:
        assert first == [{"role": "assistant", "content": "stdin"}]
        assert second == [{"role": "assistant", "content": "file"}]
    assert pipe.requests == requests
    # Python callers opt in with a handle; a dash path remains literal.
    assert streams.normalize("-").events[0]["text"] == "file"


def test_normalize_binary_api_current_position():
    raw = '\n{"type":"result","subtype":"success","is_error":false,"result":"☃😀"}'.encode()
    pipe = BinaryPipe(raw, chunk=1, prefix=b"consumed\xff")
    trajectory = streams.normalize(pipe, preserve_original=True)
    assert trajectory.complete and trajectory.original_jsonl.encode() == raw
    assert trajectory.events[-1]["source_line"] == 2 and pipe.read(1) == b""


@pytest.mark.parametrize("artifact", ["primary", "sidecar"])
@pytest.mark.parametrize("destination", ["stdout", "file"])
def test_raw_stdin_serialized_pair_caps(action, tmp_path, monkeypatch, capsys, artifact, destination):
    raw = b'{"type":"turn.completed"}'
    primary, sidecar = tmp_path / "measure.json", tmp_path / "measure.originals.json"
    install(monkeypatch, raw)
    assert cli.main([action, "-", "-o", str(primary), "--sidecar", str(sidecar)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", primary.stat().st_size)
    monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", sidecar.stat().st_size)
    output, originals = tmp_path / "fresh.json", tmp_path / "fresh.originals.json"
    args = [action, "-", "--sidecar", str(originals)]
    if destination == "file":
        args += ["-o", str(output)]
    install(monkeypatch, raw)
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert (output.read_bytes() if destination == "file" else captured.out.encode()) == primary.read_bytes()
    assert originals.read_bytes() == sidecar.read_bytes()
    setting = "MAX_OUTPUT_BYTES" if artifact == "primary" else "MAX_SIDECAR_BYTES"
    monkeypatch.setattr(integrity, setting, getattr(integrity, setting) - 1)
    install(monkeypatch, raw)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    before = snapshot(tmp_path)
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out and "exceeds 64 MiB" in captured.err
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("blocked,destination", [
    (blocked, destination)
    for blocked in ("output", "sidecar", "symlink", "same", "missing-parent", "second-link", "fsync")
    for destination in ("stdout", "file")
    if destination == "file" or blocked not in {"output", "same", "second-link"}
])
def test_raw_stdin_writer_rollback(action, tmp_path, monkeypatch, capsys, blocked, destination):
    output, sidecar = tmp_path / "output.json", tmp_path / "originals.json"
    if blocked in {"output", "sidecar"}:
        (output if blocked == "output" else sidecar).write_bytes(b"keep")
    elif blocked == "symlink":
        sidecar.symlink_to(tmp_path / "absent")
    elif blocked == "same":
        sidecar = output
    elif blocked == "missing-parent":
        sidecar = tmp_path / "missing" / "originals.json"
    elif blocked == "second-link":
        real_link = cli.os.link
        calls = []

        def fail_second(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("synthetic second-link failure")
            return real_link(src, dst)

        monkeypatch.setattr(cli.os, "link", fail_second)
    else:
        def fail_fsync(*args):
            raise OSError("synthetic fsync failure")
        monkeypatch.setattr(cli.os, "fsync", fail_fsync)
    before = snapshot(tmp_path)
    install(monkeypatch, b'{"type":"turn.completed"}')
    args = [action, "-", "--sidecar", str(sidecar)]
    if destination == "file":
        args += ["-o", str(output)]
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and snapshot(tmp_path) == before
    diagnostic = {"second-link": "synthetic second-link failure", "fsync": "synthetic fsync failure",
                  "missing-parent": "No such file or directory"}.get(blocked, "File exists")
    assert diagnostic in captured.err


@pytest.mark.parametrize("records,diagnostic", [
    ([{"type": "turn.started"}], "incomplete"),
    ([{"type": "assistant", "message": {"content": [{"type": "text", "text": "unfinished"}]}}], "incomplete"),
    ([{"type": "item.completed", "item": {"id": "m", "type": "mcp_tool_call", "server": "s", "tool": "t", "arguments": {}, "status": "completed"}},
      {"type": "turn.completed"}], "missing tool outcome"),
    ([{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": "Read", "input": {}}]}},
      {"type": "assistant", "message": {"content": [{"type": "text", "text": "interleaved"}]}},
      {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
      {"type": "result", "subtype": "success", "is_error": False}], "interleaved"),
])
@pytest.mark.parametrize("destination", ["stdout", "file"])
@pytest.mark.parametrize("sidecar", [False, True])
def test_export_stdin_unsupported_has_no_prefix_or_files(tmp_path, monkeypatch, capsys, records, diagnostic, destination, sidecar):
    raw = b"\n".join(json.dumps(record).encode() for record in records)
    install(monkeypatch, raw)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    args = ["export-agentevals", "-"]
    if destination == "file":
        args += ["-o", str(tmp_path / "messages.json")]
    if sidecar:
        args += ["--sidecar", str(tmp_path / "originals.json")]
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err
    assert not list(tmp_path.iterdir())
