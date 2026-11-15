from io import BytesIO
import json
from pathlib import Path
import random

import pytest

from agentwrench.doctor.engine import DoctorError, STUB_TEXT, scan
from agentwrench.doctor.repair import make_plan, render, validate_plan
from .make_fixtures import cnode, item, result, use

FIXTURES = Path(__file__).parent / "fixtures"


def raw(*records):
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


def inspect(data, fmt="auto"):
    return scan(BytesIO(data), "synthetic.jsonl", fmt)


def repaired(data, strategy="stub", fmt="auto", drop_invalid=False):
    source = BytesIO(data)
    report = scan(source, "synthetic.jsonl", fmt)
    plan = make_plan(report, strategy, drop_invalid)
    validate_plan(source, plan)
    return b"".join(render(source, plan)), report


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/RD*.jsonl")), ids=lambda p: p.parent.name + "/" + p.stem)
def test_every_issue_has_real_fixture(path):
    report = inspect(path.read_bytes())
    assert path.stem in {issue.code for issue in report.issues}
    assert all(issue.line > 0 for issue in report.issues)


@pytest.mark.parametrize("strategy", ["stub", "drop"])
@pytest.mark.parametrize("path", sorted(p for p in FIXTURES.glob("*/*.jsonl") if p.stem != "RD207"),
                         ids=lambda p: p.parent.name + "/" + p.stem)
def test_every_supported_fixture_repairs_clean_and_idempotently(path, strategy):
    fixed, report = repaired(path.read_bytes(), strategy, drop_invalid=True)
    assert not inspect(fixed, report.format).issues
    again, repeated = repaired(fixed, strategy, report.format, drop_invalid=True)
    assert again == fixed
    assert repeated.changes == []


@pytest.mark.parametrize("data,fmt", [(b"", "auto"), (b"", "codex"), (b"", "claude")])
def test_empty_file_clean(data, fmt):
    assert inspect(data, fmt).issues == []


def test_unknown_format_requires_explicit_hint():
    with pytest.raises(DoctorError, match="cannot detect format"):
        inspect(b'{"custom":"metadata"}\n')
    assert not inspect(b'{"custom":"metadata"}\n', "codex").issues


def test_mixed_format_refused():
    with pytest.raises(DoctorError, match="mixed or mismatched"):
        inspect(raw(item("message", role="user", content=[]), cnode("user", "u")))


@pytest.mark.parametrize("invalid", [b'{bad}\n', b'\xff\n', b'[]\n', b'{"x":NaN}\n', b'{"x":1,"x":2}\n'])
def test_middle_invalid_requires_explicit_opt_in(invalid):
    data = raw(item("message", role="user", content=[])) + invalid
    with pytest.raises(DoctorError, match="drop-invalid"):
        repaired(data)
    fixed, report = repaired(data, drop_invalid=True)
    assert len(report.changes) == 1
    assert fixed == data[:len(data) - len(invalid)]


def test_valid_final_line_without_newline_is_not_truncation():
    data = raw(item("message", role="user", content=[])).rstrip(b"\n")
    fixed, report = repaired(data)
    assert fixed == data
    assert report.issues == []


def test_untouched_bytes_and_crlf_preserved_in_order():
    before = b'{ "type" : "session_meta", "payload" : {"id":"s"} }\r\n'
    call = raw(item("function_call", "c", name="shell", arguments="{}" )).replace(b"\n", b"\r\n")
    after = b'{ "type" : "event_msg", "payload" : {"type":"task_complete"} }\n'
    fixed, report = repaired(before + call + after)
    assert fixed.startswith(before + call)
    assert fixed.endswith(after)
    assert STUB_TEXT.encode() in fixed
    assert fixed.count(b"\r\n") == 3
    assert len(report.changes) == 1


def test_stub_after_unterminated_call_adds_only_required_separator():
    data = raw(item("custom_tool_call", "c", name="patch", input="hello")).rstrip(b"\n")
    fixed, _ = repaired(data)
    assert fixed.startswith(data + b"\n")
    rows = [json.loads(line) for line in fixed.splitlines()]
    assert rows[1]["payload"] == {"type": "custom_tool_call_output", "call_id": "c", "output": STUB_TEXT}


def test_early_output_is_dropped_and_call_stubbed():
    fixed, report = repaired(raw(item("function_call_output", "c", output="early"),
                                 item("function_call", "c", name="shell", arguments="{}")))
    assert {i.code for i in report.issues} == {"RD105", "RD101"}
    assert b"early" not in fixed
    assert STUB_TEXT.encode() in fixed


def test_later_valid_output_preserved_after_early_output():
    data = raw(item("function_call_output", "c", output="early"),
               item("function_call", "c", name="shell", arguments="{}"),
               item("function_call_output", "c", output="real"))
    fixed, report = repaired(data)
    assert [i.code for i in report.issues] == ["RD105"]
    assert b"real" in fixed and STUB_TEXT.encode() not in fixed


def test_other_codex_tool_types_are_not_fabricated_as_pairs():
    data = raw(item("web_search_call", id="w", status="completed"),
               item("image_generation_call", id="i", status="completed", result="tiny"),
               item("future_call", "f"))
    fixed, report = repaired(data)
    assert fixed == data and not report.issues


def test_legacy_local_shell_id_maps_to_function_output():
    data = raw(item("local_shell_call", id="legacy", action={"type": "exec", "command": ["pwd"]}, status="completed"))
    fixed, _ = repaired(data)
    assert json.loads(fixed.splitlines()[1])["payload"]["call_id"] == "legacy"


def test_client_and_server_tool_search_boundaries():
    data = raw(item("tool_search_call", execution="server", status="completed", arguments={}),
               item("tool_search_output", execution="server", status="completed", tools=[]),
               item("tool_search_call", "search", execution="client", arguments={}))
    report = inspect(data)
    assert [(i.code, i.call_id) for i in report.issues] == [("RD101", "search")]
    with pytest.raises(DoctorError, match="strategy drop"):
        repaired(data)
    fixed, _ = repaired(data, "drop")
    assert len(fixed.splitlines()) == 2
    assert not inspect(fixed).issues


def test_claude_stub_reparents_children_and_preserves_unknown_fields():
    child = cnode("assistant", "child", "a", [{"type": "text", "text": "continued"}])
    child["customMetadata"] = {"x": [1, {"future": True}]}
    data = raw(cnode("assistant", "a", blocks=[use("c1"), use("c2")]), child)
    fixed, _ = repaired(data)
    rows = [json.loads(line) for line in fixed.splitlines()]
    stub = rows[1]
    assert stub["type"] == "user" and stub["parentUuid"] == "a"
    assert len(stub["message"]["content"]) == 2
    assert all(b["is_error"] and b["content"] == STUB_TEXT for b in stub["message"]["content"])
    assert rows[2]["parentUuid"] == stub["uuid"]
    assert rows[2]["message"]["content"][0]["text"] == "continued"
    assert rows[2]["customMetadata"] == child["customMetadata"]


def test_claude_drop_retains_chain_node_and_other_blocks():
    a = cnode("assistant", "a", blocks=[{"type": "text", "text": "keep"}, use()])
    fixed, _ = repaired(raw(a), "drop")
    assert json.loads(fixed)["message"]["content"] == [{"type": "text", "text": "keep"}]
    fixed, _ = repaired(raw(cnode("assistant", "a", blocks=[use()])), "drop")
    assert json.loads(fixed)["message"]["content"][0]["type"] == "text"


@pytest.mark.parametrize("side,session,parent", [(True, "s", "a"), (False, "other", "a"), (False, "s", None)])
def test_claude_cross_chain_result_never_satisfies_call(side, session, parent):
    data = raw(cnode("assistant", "a", blocks=[use()], session="s"),
               cnode("user", "r", parent, [result()], side, session))
    report = inspect(data)
    assert {i.code for i in report.issues} == {"RD201", "RD202"}
    fixed, _ = repaired(data)
    assert not inspect(fixed).issues


def test_two_sidechains_with_same_id_pair_only_by_ancestry():
    report = inspect((FIXTURES / "claude/sidechains.jsonl").read_bytes())
    assert [(i.code, i.line) for i in report.issues] == [("RD201", 2)]


@pytest.mark.parametrize("records", [
    [cnode("user", "a", "a")],
    [cnode("user", "a", "b"), cnode("user", "b", "a")],
    [cnode("user", "a"), cnode("user", "a")],
])
def test_ambiguous_graph_explicitly_refuses_repair(records):
    data = raw(*records)
    assert "RD207" in {i.code for i in inspect(data).issues}
    with pytest.raises(DoctorError, match="ambiguous"):
        repaired(data, drop_invalid=True)


def test_large_payload_is_not_retained_in_index():
    data = raw(item("function_call", "c", name="shell", arguments="x" * 2_000_000),
               item("function_call_output", "c", output="y" * 2_000_000))
    report = inspect(data)
    assert not report.issues
    assert len(repr(report.events)) < 500
    assert not hasattr(report, "records")


def test_deep_claude_chain_uses_iterative_ancestry():
    records = [cnode("assistant", "a0", blocks=[use()])]
    records += [cnode("user", f"a{i}", f"a{i-1}", [{"type": "text", "text": "continue"}]) for i in range(1, 4000)]
    records.append(cnode("user", "result", "a3999", [result()]))
    assert not inspect(raw(*records)).issues


@pytest.mark.parametrize("strategy", ["stub", "drop"])
def test_seeded_mutations_repair_pairing_invariant(strategy):
    rng = random.Random(301)
    for _ in range(50):
        records = [item("message", role="user", content=[])]
        for i in range(12):
            call = item("function_call", f"c{i}", name="shell", arguments="{}")
            out = item("function_call_output", f"c{i}", output="synthetic")
            records.extend(rng.choice([[call, out], [out, call], [call], [out], [call, call, out, out]]))
        rng.shuffle(records)
        fixed, _ = repaired(raw(*records), strategy)
        assert not inspect(fixed).issues
        assert repaired(fixed, strategy)[1].changes == []


def test_claude_metadata_uuid_without_parent_is_indexed():
    data = raw({"type": "progress", "uuid": "metadata"},
               cnode("assistant", "a", "metadata", [use()]),
               cnode("user", "r", "a", [result()]))
    assert not inspect(data).issues


def test_duplicate_tool_use_blocks_keep_first_and_single_stub():
    fixed, report = repaired(raw(cnode("assistant", "a", blocks=[use(), use()])))
    assert {issue.code for issue in report.issues} == {"RD201", "RD205"}
    rows = [json.loads(line) for line in fixed.splitlines()]
    assert len(rows[0]["message"]["content"]) == 1
    assert len(rows[1]["message"]["content"]) == 1


def test_schema_invalid_parent_record_dropped_and_child_detached():
    data = raw(cnode("assistant", "a", blocks=[42]), cnode("user", "r", "a", [result()]))
    fixed, _ = repaired(data, drop_invalid=True)
    assert json.loads(fixed)["parentUuid"] is None
    assert not inspect(fixed).issues


def test_scope_boundary_cannot_be_bridged_by_intermediate_node():
    data = raw(cnode("assistant", "a", blocks=[use()]),
               cnode("assistant", "side", "a", [{"type": "text", "text": "side"}], True),
               cnode("user", "r", "side", [result()]))
    assert {issue.code for issue in inspect(data).issues} == {"RD201", "RD202"}
    fixed, _ = repaired(data)
    assert not inspect(fixed).issues


def test_stub_marker_names_agentwrench_and_stub_uuid_is_stable():
    assert STUB_TEXT.startswith("[agentwrench doctor] ")
    data = raw(cnode("assistant", "a", blocks=[use("u1")]))
    fixed, _ = repaired(data)
    stub = json.loads(fixed.splitlines()[1])
    # The uuid5 name ("rollout-doctor:" + node + call ids) is unchanged, so IDs match older repairs.
    assert stub["uuid"] == "6fcd072c-3ed1-564b-a22d-3e95cda40949"
    assert stub["message"]["content"][0]["content"] == STUB_TEXT


@pytest.mark.parametrize("marker", ["[rollout-doctor] tool output missing: call was interrupted", STUB_TEXT])
def test_files_repaired_with_old_or_new_marker_are_clean_and_left_alone(marker):
    codex = raw(item("function_call", "c", name="shell", arguments="{}"),
                item("function_call_output", "c", output=marker))
    claude = raw(cnode("assistant", "a", blocks=[use("u1")]),
                 cnode("user", "s", "a", [{"type": "tool_result", "tool_use_id": "u1",
                                           "content": marker, "is_error": True}]))
    for data in (codex, claude):
        assert not inspect(data).issues
        fixed, report = repaired(data)
        assert fixed == data and not report.changes
