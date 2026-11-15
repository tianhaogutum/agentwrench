"""Local synthetic integration fixtures; no image decoder or remote URL is used."""
import hashlib
import json
from pathlib import Path

import pytest

from agentwrench.gate.cli import main
from agentwrench.gate.streams import normalize

FIX = Path(__file__).parent / "fixtures"


def test_normalize_reports_nontext_and_unknown_fields():
    trajectory = normalize(FIX / "claude_attachments.jsonl")
    assert trajectory.complete
    losses = {(x["source_line"], x["pointer"]): x for x in trajectory.as_dict()["losses"]}
    assert losses[2, "/message/content/0/future~1field~0"]["reason"] == "unsupported_field"
    assert losses[2, "/tool_use_result"]["reason"] == "partially_normalized"
    assert losses[3, "/message/content/0"]["reason"] == "unsupported_content"
    assert losses[3, "/message/content/1"]["reason"] == "unsupported_content"
    assert losses[3, "/message/content/2/citations"]["reason"] == "unsupported_field"
    assert losses[4, "/future_end"]["reason"] == "unsupported_field"
    result = next(e for e in trajectory.events if e["kind"] == "tool_result")
    assert result["content"][1]["source"]["data"] == "c3ludGhldGlj"


def test_export_sidecar_preserves_exact_input_and_accounts_for_loss(tmp_path, capsys):
    # Preserve blank lines, CRLF, Unicode, spacing and absent final newline too.
    raw = b' \r\n' + (FIX / "claude_attachments.jsonl").read_bytes().replace(b'Done', 'Done \u2603'.encode()).rstrip(b'\n')
    stream = tmp_path / "input.jsonl"
    stream.write_bytes(raw)
    output, sidecar = tmp_path / "messages.json", tmp_path / "originals.json"
    assert main(["export-agentevals", str(stream), "-o", str(output), "--sidecar", str(sidecar)]) == 0
    report = json.loads(sidecar.read_bytes())
    assert report["source"] == "claude-stream" and report["target"] == "agentevals"
    assert report["original"]["jsonl"].encode("utf-8") == raw
    assert report["original"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    losses = {(x["source_line"], x["pointer"]): x for x in report["losses"]}
    assert losses[3, "/message/content/0/content"]["reason"] == "structured_tool_result"
    assert losses[4, "/message/content/1"]["reason"] == "unsupported_content"
    assert losses[5, "/usage"]["reason"] == "not_in_message_export"
    messages = json.loads(output.read_bytes())
    assert json.loads(messages[1]["content"])[1]["source"]["data"] == "c3ludGhldGlj"
    assert messages[-1]["content"] == 'Done \u2603'
    assert output.stat().st_mode & 0o777 == sidecar.stat().st_mode & 0o777 == 0o600
    assert "loss" in capsys.readouterr().err


def test_normalize_sidecar_can_reconstruct_original_stream(tmp_path, capsys):
    source = FIX / "claude_attachments.jsonl"
    sidecar = tmp_path / "originals.json"
    assert main(["normalize", str(source), "--sidecar", str(sidecar)]) == 0
    captured = capsys.readouterr()
    normalized = json.loads(captured.out)
    report = json.loads(sidecar.read_bytes())
    assert report["target"] == "normalize" and report["losses"] == normalized["losses"]
    assert "original_jsonl" not in normalized and "export_losses" not in normalized
    assert report["output_sha256"] == hashlib.sha256(captured.out.encode()).hexdigest()
    restored = tmp_path / "restored.jsonl"
    restored.write_bytes(report["original"]["jsonl"].encode("utf-8"))
    assert restored.read_bytes() == source.read_bytes()
    assert normalize(restored).as_dict() == normalized


def test_codex_nontext_and_nested_unknown_fields(tmp_path, capsys):
    records = [
        {"type": "item.completed", "extra": 1, "item": {
            "id": "m", "type": "mcp_tool_call", "server": "s", "tool": "t", "arguments": {},
            "status": "completed", "result": {"content": [{"type": "image", "data": "c3ludGhldGlj", "mimeType": "image/png"}]},
            "_meta": {"future": True}}},
        {"type": "item.completed", "item": {"id": "new", "type": "future_attachment", "url": "file:///never-opened"}},
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2, "new_counter": 7}},
        {"type": "future_event", "binary": [1, 2, 3]},
    ]
    stream = tmp_path / "codex.jsonl"
    stream.write_text("\n".join(json.dumps(r) for r in records))
    t = normalize(stream)
    losses = {(x["source_line"], x["pointer"]): x for x in t.losses}
    assert losses[1, "/extra"]["reason"] == "unsupported_field"
    assert losses[1, "/item/_meta"]["reason"] == "unsupported_field"
    assert losses[2, "/item"]["reason"] == "unsupported_item"
    assert losses[4, "/usage/new_counter"]["reason"] == "unsupported_field"
    assert losses[5, ""]["reason"] == "unsupported_event"
    sidecar = tmp_path / "codex.sidecar.json"
    assert main(["export-agentevals", str(stream), "--sidecar", str(sidecar)]) == 0
    captured = capsys.readouterr()
    messages = json.loads(captured.out)
    assert json.loads(messages[1]["content"]) == records[0]["item"]["result"]
    report = json.loads(sidecar.read_bytes())
    assert report["original"]["jsonl"].encode() == stream.read_bytes()
    assert any(x["pointer"] == "/item/result" and x["disposition"] == "json_string" for x in report["losses"])
    assert report["losses"] == t.export_losses


def test_unknown_fields_warn_even_without_sidecar(tmp_path, capsys):
    assert main(["export-agentevals", str(FIX / "claude_attachments.jsonl")]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)[-1]["content"] == "Done"
    assert '"pointer": "/message/content/0/future~1field~0"' in captured.err
    assert "structured_tool_result" in captured.err
    assert "c3ludGhldGlj" not in captured.err  # warning contains locations, not payloads


@pytest.mark.parametrize("blocked", ["output", "sidecar", "same", "symlink", "missing_parent"])
def test_sidecar_pair_refuses_paths_without_partial_output(tmp_path, capsys, blocked):
    output, sidecar = tmp_path / "out.json", tmp_path / "sidecar.json"
    if blocked == "output":
        output.write_bytes(b"keep output")
    elif blocked == "sidecar":
        sidecar.write_bytes(b"keep sidecar")
    elif blocked == "same":
        sidecar = output
    elif blocked == "symlink":
        sidecar.symlink_to(tmp_path / "missing")
    else:
        sidecar = tmp_path / "missing" / "sidecar.json"
    assert main(["normalize", str(FIX / "claude_attachments.jsonl"), "-o", str(output), "--sidecar", str(sidecar)]) == 2
    assert not capsys.readouterr().out
    if blocked == "output":
        assert output.read_bytes() == b"keep output" and not sidecar.exists()
    elif blocked == "sidecar":
        assert sidecar.read_bytes() == b"keep sidecar" and not output.exists()
    else:
        assert not output.exists() and not sidecar.exists()
    assert not list(tmp_path.glob(".trajgate-*"))


def test_sidecar_link_io_failure_rolls_back(tmp_path, capsys, monkeypatch):
    import agentwrench.gate.cli as cli
    link = cli.os.link
    calls = []

    def fail_second(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("synthetic second-link failure")
        return link(src, dst)

    monkeypatch.setattr(cli.os, "link", fail_second)
    output, sidecar = tmp_path / "out.json", tmp_path / "sidecar.json"
    assert main(["normalize", str(FIX / "claude_attachments.jsonl"), "-o", str(output), "--sidecar", str(sidecar)]) == 2
    assert "synthetic second-link failure" in capsys.readouterr().err
    assert not output.exists() and not sidecar.exists()
    assert not list(tmp_path.glob(".trajgate-*"))


@pytest.mark.parametrize("text", ['{"type":', '{"type":"turn.started"}'])
def test_invalid_or_incomplete_export_creates_no_sidecar(tmp_path, capsys, text):
    source = tmp_path / "input.jsonl"
    source.write_text(text)
    output, sidecar = tmp_path / "out.json", tmp_path / "sidecar.json"
    assert main(["export-agentevals", str(source), "-o", str(output), "--sidecar", str(sidecar)]) == 2
    assert not capsys.readouterr().out
    assert not output.exists() and not sidecar.exists()


def test_original_capture_uses_same_read_as_normalization(tmp_path, monkeypatch):
    source = tmp_path / "input.jsonl"
    original = (FIX / "claude_attachments.jsonl").read_bytes()
    source.write_bytes(original)
    real_open = Path.open
    reads = []

    def once(path, mode="r", *args, **kwargs):
        if path == source:
            reads.append(mode)
            assert reads == ["rb"]
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", once)
    t = normalize(source, preserve_original=True)
    assert t.original_jsonl.encode() == original


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("source", ["codex_exec_ok.jsonl", "claude_attachments.jsonl"])
@pytest.mark.parametrize("stdout", [False, True], ids=["file", "stdout"])
@pytest.mark.parametrize("artifact", ["primary", "sidecar"])
def test_sidecar_generation_size_contract(tmp_path, capsys, monkeypatch, action, source, stdout, artifact):
    import agentwrench.gate.cli as cli
    import agentwrench.gate.sidecar as integrity

    # Measure a real pair; scaled caps avoid huge permanent regression fixtures.
    primary, sidecar = tmp_path / "measure.json", tmp_path / "measure.sidecar.json"
    raw = b' \r\n' + (FIX / source).read_bytes().replace(b'Done', 'Done 😀'.encode()).rstrip(b'\n')
    stream = tmp_path / "input.jsonl"
    stream.write_bytes(raw)
    assert main([action, str(stream), "-o", str(primary), "--sidecar", str(sidecar)]) == 0
    capsys.readouterr()
    primary_bytes, sidecar_bytes = primary.read_bytes(), sidecar.read_bytes()
    monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", len(primary_bytes))
    monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", len(sidecar_bytes))

    output, originals = tmp_path / "exact.json", tmp_path / "exact.sidecar.json"
    args = [action, str(stream), "--sidecar", str(originals)]
    if not stdout:
        args += ["-o", str(output)]
    assert main(args) == 0
    captured = capsys.readouterr()
    if stdout:
        assert captured.out.encode() == primary_bytes and not output.exists()
        output.write_bytes(captured.out.encode())  # test captures primary stdout
    else:
        assert not captured.out and output.read_bytes() == primary_bytes
    assert originals.read_bytes() == sidecar_bytes
    verified = integrity.verify_sidecar(originals, output).as_dict()
    assert verified["original"] == {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    assert verified["output"] == {"sha256": hashlib.sha256(primary_bytes).hexdigest(), "bytes": len(primary_bytes)}
    assert originals.stat().st_mode & 0o777 == 0o600

    # One byte over either cap must fail before the CLI's writer is invoked.
    setting = "MAX_OUTPUT_BYTES" if artifact == "primary" else "MAX_SIDECAR_BYTES"
    monkeypatch.setattr(integrity, setting, getattr(integrity, setting) - 1)
    before = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    calls = []
    real_writer = cli.write_new_reports

    def track_writer(outputs):
        calls.append(outputs)
        return real_writer(outputs)

    monkeypatch.setattr(cli, "write_new_reports", track_writer)
    rejected, rejected_sidecar = tmp_path / "rejected.json", tmp_path / "rejected.sidecar.json"
    args = [action, str(stream), "--sidecar", str(rejected_sidecar)]
    if not stdout:
        args += ["-o", str(rejected)]
    assert main(args) == 2
    captured = capsys.readouterr()
    label = "primary output" if artifact == "primary" else "sidecar"
    assert not captured.out and f"{label} exceeds 64 MiB" in captured.err
    assert not calls
    assert {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
@pytest.mark.parametrize("blocked", ["primary-file", "sidecar-file", "primary-symlink", "sidecar-symlink"])
def test_oversized_sidecar_rejected_before_touching_existing_paths(tmp_path, capsys, monkeypatch, action, blocked):
    import agentwrench.gate.cli as cli
    import agentwrench.gate.sidecar as integrity

    primary, sidecar, sentinel = tmp_path / "out.json", tmp_path / "originals.json", tmp_path / "keep"
    sentinel.write_bytes(b"keep")
    existing = primary if blocked.startswith("primary") else sidecar
    if blocked.endswith("symlink"):
        existing.symlink_to(sentinel)
    else:
        existing.write_bytes(b"existing")
    before = {p: (p.read_bytes(), p.lstat().st_mode, p.lstat().st_mtime_ns) for p in tmp_path.iterdir()}
    monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", 1)

    def no_write(*args):
        pytest.fail("oversized sidecar reached file staging")

    monkeypatch.setattr(cli, "write_new_reports", no_write)
    assert main([action, str(FIX / "claude_attachments.jsonl"), "-o", str(primary), "--sidecar", str(sidecar)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "sidecar exceeds 64 MiB" in captured.err
    assert {p: (p.read_bytes(), p.lstat().st_mode, p.lstat().st_mtime_ns) for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("action", ["normalize", "export-agentevals"])
def test_generation_without_sidecar_keeps_existing_output_contract(tmp_path, capsys, monkeypatch, action):
    import agentwrench.gate.sidecar as integrity

    # Caps protect verifiable pairs; callers without originals keep their API.
    monkeypatch.setattr(integrity, "MAX_OUTPUT_BYTES", 1)
    monkeypatch.setattr(integrity, "MAX_SIDECAR_BYTES", 1)
    output = tmp_path / "out.json"
    assert main([action, str(FIX / "claude_attachments.jsonl"), "-o", str(output)]) == 0
    assert json.loads(output.read_bytes())
    assert set(tmp_path.iterdir()) == {output}
    assert not capsys.readouterr().out
