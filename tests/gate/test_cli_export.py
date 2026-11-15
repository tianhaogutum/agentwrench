import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from agentwrench.gate.cli import junit, main
from agentwrench.gate.export import export_agentevals
from agentwrench.gate.model import InputError
from agentwrench.gate.policy import check, load_policy
from agentwrench.gate.streams import normalize

FIX = Path(__file__).parent / "fixtures"


def test_cli_pass_json(capsys):
    assert main(["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", str(FIX / "policy.yaml"), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {"failed": 0, "passed": 12, "total": 12}


def test_cli_failure_and_junit(tmp_path, capsys):
    p = tmp_path / "report.xml"
    assert main(["check", str(FIX / "codex_exec_violation.jsonl"), "--policy", str(FIX / "policy.yaml"), "--junit", str(p), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["failed"] == 4
    xml = ET.parse(p).getroot()
    assert int(xml.get("tests")) == report["summary"]["total"]
    assert int(xml.get("failures")) == len(xml.findall("testcase/failure")) == 4
    assert "evidence event indexes=" in xml.find("testcase/failure").text


def test_export_claude_parallel_calls():
    messages = export_agentevals(normalize(FIX / "claude_stream_ok.jsonl"))
    assert [m["role"] for m in messages] == ["assistant", "assistant", "tool", "assistant", "tool", "tool", "assistant"]
    assert messages[3]["tool_calls"][0]["function"] == {"name": "Bash", "arguments": '{"command": "python -m pytest -q"}'}
    assert len(messages[3]["tool_calls"]) == 2
    assert messages[4]["tool_call_id"] == "bash_1"


def test_export_codex():
    messages = export_agentevals(normalize(FIX / "codex_exec_ok.jsonl"))
    calls = [c for m in messages for c in m.get("tool_calls", [])]
    assert [c["function"]["name"] for c in calls] == ["command_execution", "file_change", "command_execution", "docs__lookup", "web_search"]
    assert len([m for m in messages if m["role"] == "tool"]) == 5
    assert messages[-1]["content"] == "Done: all tests passed."
    assert not any("Check files" in m.get("content", "") for m in messages)


def test_export_incomplete_rejected():
    t = normalize(FIX / "codex_exec_ok.jsonl")
    t.complete = False
    with pytest.raises(InputError, match="incomplete"):
        export_agentevals(t)


def test_cli_output_is_new_file_and_never_follows_symlinks(tmp_path, capsys):
    target = tmp_path / "out.json"
    args = ["normalize", str(FIX / "codex_exec_ok.jsonl"), "-o", str(target)]
    assert main(args) == 0
    original = target.read_bytes()
    assert json.loads(original)["schema_version"] == 1
    assert main(args) == 2
    assert "File exists" in capsys.readouterr().err
    assert target.read_bytes() == original
    link = tmp_path / "link"
    link.symlink_to(target)
    args[-1] = str(link)
    assert main(args) == 2 and target.read_bytes() == original
    assert not list(tmp_path.glob('.trajgate-*'))


def test_cli_exit_two(tmp_path, capsys):
    p = tmp_path / "policy.yaml"
    p.write_text("misspelled: 1")
    assert main(["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", str(p)]) == 2
    assert "misspelled" in capsys.readouterr().err
    assert main(["normalize", str(tmp_path / "missing")]) == 2


def test_cli_truncated_and_override(tmp_path, capsys):
    p = tmp_path / "truncated.jsonl"
    p.write_text('{"type":"turn.started"}\n')
    policy = tmp_path / "p.yaml"
    policy.write_text('max_commands: 1')
    args = ["check", str(p), "--policy", str(policy), "--json"]
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["results"][0]["id"] == "incomplete_stream"
    assert main(args + ["--allow-incomplete"]) == 0


def test_cli_module_and_console_entrypoint():
    for command in ([sys.executable, "-m", "agentwrench", "gate"], [str(Path(sys.executable).parent / "agentwrench"), "gate"]):
        process = subprocess.run(command + ["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", str(FIX / "policy.yaml")], capture_output=True, text=True)
        assert process.returncode == 0, process.stderr
        assert "0 failed, 12 passed" in process.stdout
        bad = subprocess.run(command + ["no-such-subcommand"], capture_output=True)
        assert bad.returncode == 2


def test_junit_escapes_invalid_control_chars():
    t = normalize(FIX / "codex_exec_violation.jsonl")
    report = check(t, load_policy(FIX / "policy.yaml"))
    report["results"][1]["message"] = 'x<&\x00'
    root = ET.fromstring(junit(report))
    assert root.find("testcase/failure").get("message") == 'x<&\ufffd'


def test_real_agentevals_strict_and_mismatch():
    # This import is intentionally required, never importorskip: acceptance must exercise it.
    from agentevals.trajectory.match import create_trajectory_match_evaluator
    messages = export_agentevals(normalize(FIX / "claude_stream_ok.jsonl"))
    # Independent, hand-authored expected trajectory rather than comparing output to itself.
    reference = json.loads((FIX / "claude_reference.json").read_text())
    evaluator = create_trajectory_match_evaluator(trajectory_match_mode="strict")
    assert evaluator(outputs=messages, reference_outputs=reference)["score"] is True
    reference[3]["tool_calls"][0]["function"]["arguments"] = '{"command":"rm -rf src"}'
    assert evaluator(outputs=messages, reference_outputs=reference)["score"] is False


def test_export_does_not_fake_missing_mcp_outcome():
    t = normalize(FIX / "codex_exec_ok.jsonl")
    result = next(e for e in t.events if e["kind"] == "tool_result" and e["call_id"] == "codex:m0")
    result["content"] = None
    with pytest.raises(InputError, match="missing tool outcome"):
        export_agentevals(t)


def test_export_rejects_interleaving():
    t = normalize(FIX / "claude_stream_ok.jsonl")
    # Swap a second parallel call behind the first call's result: cannot form one batch.
    first_call = next(i for i, e in enumerate(t.events) if e.get("call_id") == "write_1")
    result = next(i for i, e in enumerate(t.events) if e["kind"] == "tool_result" and e.get("call_id") == "bash_1")
    t.events[first_call], t.events[result] = t.events[result], t.events[first_call]
    # This particular ordering can be serialized sequentially (first call is already done).
    messages = export_agentevals(t)
    assert len([c for m in messages for c in m.get("tool_calls", [])]) == 3
    # A normal message while a call is unresolved is explicitly rejected.
    t = normalize(FIX / "claude_stream_ok.jsonl")
    t.events.insert(2, {"kind": "message", "role": "assistant", "text": "interleaved", "completed": True})
    with pytest.raises(InputError, match="interleaved"):
        export_agentevals(t)


def test_export_warns_about_unknown_events(tmp_path, capsys):
    p = tmp_path / "unknown.jsonl"
    p.write_text((FIX / "codex_exec_ok.jsonl").read_text() + '{"type":"future"}\n')
    assert main(["export-agentevals", str(p)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) and '"future": 1' in captured.err


def test_console_escapes_terminal_controls(tmp_path, capsys):
    p = tmp_path / "policy.yaml"
    p.write_text('forbid_command:\n  pattern: "\\e"')
    assert main(["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", str(p)]) == 0
    output = capsys.readouterr().out
    assert '\x1b' not in output and '\\x1b' in output


def test_missing_mcp_arguments_rejected():
    t = normalize(FIX / "codex_exec_ok.jsonl")
    call = next(e for e in t.events if e["kind"] == "tool_call")
    call["arguments"] = None
    with pytest.raises(InputError, match="missing/non-object tool arguments"):
        export_agentevals(t)


def test_non_object_mcp_arguments_rejected():
    t = normalize(FIX / "codex_exec_ok.jsonl")
    call = next(e for e in t.events if e["kind"] == "tool_call")
    call["arguments"] = ["not", "an", "object"]
    with pytest.raises(InputError, match="missing/non-object tool arguments"):
        export_agentevals(t)
