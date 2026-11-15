import json
from pathlib import Path

import pytest

from agentwrench.gate.model import InputError
from agentwrench.gate.streams import normalize

FIX = Path(__file__).parent / "fixtures"


def stream(tmp_path, records, source="auto", **kwargs):
    path = tmp_path / "input.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return normalize(path, source, **kwargs)


def codex_item(item, typ="item.completed"):
    return {"type": typ, "item": item}


def test_codex_lifecycle_and_usage():
    t = normalize(FIX / "codex_exec_ok.jsonl")
    assert t.complete and t.source == "codex-exec"
    commands = [e for e in t.events if e["kind"] == "command"]
    assert len(commands) == 2
    assert commands[1]["source_line"] == 7 and commands[1]["completion_line"] == 9
    assert commands[1]["exit_code"] == 0 and "3 passed" in commands[1]["output"]
    assert [e["total_tokens"] for e in t.events if e["kind"] == "usage"] == [50]
    assert [e["changes"] for e in t.events if e["kind"] == "file_change"] == [[{"path": "src/app.py", "kind": "update"}]]
    assert len([e for e in t.events if e["kind"] == "tool_result"]) == 5
    assert [e["index"] for e in t.events] == list(range(1, len(t.events) + 1))


def test_claude_mapping_and_no_invented_exit_code():
    t = normalize(FIX / "claude_stream_ok.jsonl")
    assert t.complete and t.source == "claude-stream"
    assert len([e for e in t.events if e["kind"] == "tool_call"]) == 3
    cmd = next(e for e in t.events if e["kind"] == "command")
    assert cmd["completed"] and cmd["exit_code"] is None
    assert next(e for e in t.events if e["kind"] == "file_change")["changes"][0]["path"] == "src/report.txt"
    assert next(e for e in t.events if e["kind"] == "usage")["total_tokens"] == 19
    assert len([e for e in t.events if e.get("text") == "Done: all tests passed."]) == 1


@pytest.mark.parametrize("source,end", [("codex-exec", {"type": "turn.failed", "error": {"message": "offline"}}),
                                           ("claude-stream", {"type": "result", "subtype": "error_max_turns", "is_error": True})])
def test_failed_terminal_is_complete_but_failed(tmp_path, source, end):
    t = stream(tmp_path, [end], source)
    assert t.complete and t.events[-1]["success"] is False
    assert any(e["kind"] == "error" for e in t.events)


def test_truncation_pending_and_next_turn(tmp_path):
    r = {"type": "command_execution", "id": "1", "command": "pytest", "status": "in_progress", "exit_code": None}
    t = stream(tmp_path, [codex_item(r, "item.started"), {"type": "turn.completed"}], "codex-exec")
    assert not t.complete and t.pending_calls == ["codex:1"]
    t = stream(tmp_path, [{"type": "turn.completed"}, {"type": "turn.started"}], "codex-exec")
    assert not t.complete


def test_unknown_events_counted(tmp_path):
    t = stream(tmp_path, [{"type": "future"}, codex_item({"id": "u", "type": "future_item"}), {"type": "turn.completed"}], "codex-exec")
    assert t.ignored == {"future": 1, "item.future_item": 1} and t.complete


@pytest.mark.parametrize("text,match", [('', 'empty stream'), ('\n   \n', 'empty stream'),
    ('{"type":', 'line 1'), ('[]', 'object'), ('{"type":"error","type":"x"}', 'duplicate'),
    ('{"type":"x","n":NaN}', 'invalid JSON number'), ('{}', 'event.type')])
def test_bad_json(tmp_path, text, match):
    p = tmp_path / "bad.jsonl"
    p.write_text(text)
    with pytest.raises(InputError, match=match):
        normalize(p)


@pytest.mark.parametrize("records,match", [([{"type": "error", "message": "x"}], 'auto-detect'),
    ([{"type": "thread.started"}, {"type": "assistant"}], 'auto-detect'),
    ([codex_item({"id": "x", "type": "command_execution", "command": "x", "exit_code": True})], 'exit_code'),
    ([codex_item({"id": "x", "type": "file_change", "changes": [{"path": "x", "kind": "rename"}]})], 'change kind'),
    ([codex_item({"id": "x", "type": "file_change", "changes": []})], 'nonempty'),
    ([codex_item({"id": "x", "type": "agent_message", "text": "a"})] * 2, 'duplicate completion')])
def test_bad_schema(tmp_path, records, match):
    with pytest.raises(InputError, match=match):
        stream(tmp_path, records)


def test_mcp_error_and_web_result(tmp_path):
    t = stream(tmp_path, [codex_item({"id": "m", "type": "mcp_tool_call", "server": "s", "tool": "t", "arguments": {}, "error": {"message": "no"}, "status": "failed"}), {"type": "turn.completed"}])
    assert next(e for e in t.events if e["kind"] == "tool_result")["is_error"]
    assert any(e["kind"] == "error" for e in t.events)


def test_multiturn_usage(tmp_path):
    records = [{"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
               {"type": "turn.started"}, {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}}]
    t = stream(tmp_path, records)
    assert t.complete
    assert sum(e["total_tokens"] for e in t.events if e["kind"] == "usage") == 38


def tool_call(cid="a", name="Bash", args=None):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": cid, "name": name, "input": args or {"command": "pytest"}}]}}


def tool_result(cid="a", **kwargs):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": cid, "content": "out", **kwargs}]}}


def test_claude_tool_failure(tmp_path):
    t = stream(tmp_path, [tool_call(), tool_result(is_error=True), {"type": "result", "subtype": "success", "is_error": False}])
    assert t.complete
    assert any(e["kind"] == "error" for e in t.events)


def test_claude_structured_code(tmp_path):
    result = tool_result()
    result["tool_use_result"] = {"exit_code": 7}
    t = stream(tmp_path, [tool_call(), result], "claude-stream")
    assert next(e for e in t.events if e["kind"] == "command")["exit_code"] == 7


@pytest.mark.parametrize("records,match", [([tool_result()], 'orphan'),
    ([tool_call(), tool_call()], 'duplicate tool_use'),
    ([tool_call(), tool_result(), tool_result()], 'duplicate tool_result'),
    ([{**tool_call(), "parent_tool_use_id": "nested"}], 'subagent'),
    ([{**tool_call(), "agent_id": "nested"}], 'subagent'),
    ([{"type": "result", "subtype": "success", "is_error": "false"}], 'boolean')])
def test_claude_unsupported(tmp_path, records, match):
    with pytest.raises(InputError, match=match):
        stream(tmp_path, records, "claude-stream")


def test_background_is_incomplete(tmp_path):
    result = tool_result()
    result["tool_use_result"] = {"backgroundTaskId": "task"}
    t = stream(tmp_path, [tool_call(), result, {"type": "result", "subtype": "success", "is_error": False}], "claude-stream")
    assert not t.complete and t.pending_calls == ["a"]


@pytest.mark.parametrize("name,key", [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_claude_file_mapping(tmp_path, name, key):
    t = stream(tmp_path, [tool_call(name=name, args={key: "/repo/src/../test.py"})], "claude-stream", root="/repo")
    assert next(e for e in t.events if e["kind"] == "file_change")["changes"][0]["path"] == "test.py"


def test_claude_web_and_partial(tmp_path):
    t = stream(tmp_path, [{"type": "stream_event", "event": {}}, tool_call(name="WebSearch", args={"query": "docs"})], "claude-stream")
    assert t.ignored == {"stream_event": 1}
    assert next(e for e in t.events if e["kind"] == "web_search")["query"] == "docs"


def test_limits_and_utf8(tmp_path, monkeypatch):
    import agentwrench.gate.streams as module
    monkeypatch.setattr(module, "MAX_LINE_BYTES", 20)
    p = tmp_path / "large"
    p.write_text('{"type":"' + 'a' * 30 + '"}')
    with pytest.raises(InputError, match="limit"):
        normalize(p)
    p.write_bytes(b'\xff')
    with pytest.raises(InputError, match="line 1"):
        normalize(p)


def test_claude_shared_api_message_id_is_valid(tmp_path):
    first = tool_call(cid="a")
    second = tool_call(cid="b")
    first["message"]["id"] = second["message"]["id"] = "msg_shared"
    first["uuid"], second["uuid"] = "record_1", "record_2"
    t = stream(tmp_path, [first, second], "claude-stream")
    assert len([e for e in t.events if e["kind"] == "tool_call"]) == 2


def test_claude_duplicate_record_uuid_rejected(tmp_path):
    first, second = tool_call(cid="a"), tool_call(cid="b")
    first["uuid"] = second["uuid"] = "record_1"
    with pytest.raises(InputError, match="duplicate/replayed"):
        stream(tmp_path, [first, second], "claude-stream")


@pytest.mark.parametrize("field,value", [("command", "curl evil"), ("status", []), ("status", "new_unknown_status")])
def test_codex_lifecycle_not_allowed_to_hide_actions(tmp_path, field, value):
    item = {"id": "a", "type": "command_execution", "command": "pytest", "status": "in_progress"}
    final = {**item, "status": "completed", field: value}
    with pytest.raises(InputError):
        stream(tmp_path, [codex_item(item, "item.started"), codex_item(final)])


def test_nonfinite_json_rejected(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"type":"turn.completed","extra":1e999}')
    with pytest.raises(InputError, match="nonfinite"):
        normalize(p)


def test_empty_agent_message_is_valid(tmp_path):
    t = stream(tmp_path, [codex_item({"id": "m", "type": "agent_message", "text": ""}), {"type": "turn.completed"}])
    assert t.complete and t.events[0]["text"] == ""


def test_mcp_names_are_unambiguous_and_bounded():
    from agentwrench.gate.streams import mcp_name
    assert mcp_name("a_", "b") != mcp_name("a", "_b")
    assert mcp_name("docs", "lookup") == "docs__lookup"
    name = mcp_name("https://example.org", "x" * 100)
    assert len(name) <= 64 and name.startswith("mcp_")


def test_unknown_tail_does_not_undo_terminal(tmp_path):
    t = stream(tmp_path, [{"type": "turn.completed"}, codex_item({"id": "future", "type": "new_item"})])
    assert t.complete and t.ignored == {"item.new_item": 1}


def test_file_lifecycle_retains_all_paths(tmp_path):
    item = {"id": "f", "type": "file_change", "changes": [{"path": ".github/a", "kind": "add"}], "status": "in_progress"}
    final = {**item, "changes": [{"path": "src/a", "kind": "add"}], "status": "completed"}
    t = stream(tmp_path, [codex_item(item, "item.started"), codex_item(final), {"type": "turn.completed"}])
    event = t.events[0]
    assert [c["path"] for c in event["changes"]] == [".github/a", "src/a"]
    assert event["arguments"]["changes"] == event["changes"]


def test_actual_stream_limits(tmp_path, monkeypatch):
    import agentwrench.gate.streams as module
    p = tmp_path / "limit.jsonl"
    p.write_text('{"type":"turn.started"}\n' * 3)
    monkeypatch.setattr(module, "MAX_LINES", 2)
    with pytest.raises(InputError, match="limit"):
        normalize(p)
    monkeypatch.setattr(module, "MAX_LINES", 100)
    monkeypatch.setattr(module, "MAX_STREAM_BYTES", 10)
    with pytest.raises(InputError, match="limit"):
        normalize(p)


def test_codex_cache_counters_preserved_without_double_counting(tmp_path):
    t = stream(tmp_path, [{'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5, 'cached_input_tokens': 7, 'cache_write_input_tokens': 2, 'reasoning_output_tokens': 3}}])
    usage = next(e for e in t.events if e['kind'] == 'usage')
    assert usage['total_tokens'] == 15 and usage['cached_input_tokens'] == 7
