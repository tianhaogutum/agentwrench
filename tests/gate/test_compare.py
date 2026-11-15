"""Comparison integration fixtures use local synthetic agent stdout only."""
import hashlib
import json
from pathlib import Path

import pytest

from agentwrench.gate.cli import main

FIX = Path(__file__).parent / "fixtures"


def manifest(tmp_path, name, tasks):
    path = tmp_path / name
    path.write_text(json.dumps({"schema_version": 1, "tasks": tasks}), encoding="utf-8")
    return path


def task(task_id, stream="codex_exec_ok.jsonl", **kwargs):
    return {"id": task_id, "stream": str(FIX / stream), **kwargs}


def invoke(baseline, candidate, policy, *extra):
    return main(["compare", str(baseline), str(candidate), "--policy", str(policy), *extra])


def test_compare_per_task_evidence_threshold_and_reproducibility(tmp_path, capsys):
    baseline = manifest(tmp_path, "baseline.json", [task("tests"), task("lint")])
    candidate = manifest(tmp_path, "candidate.json", [task("lint"), task("tests", "codex_exec_violation.jsonl")])
    policy = FIX / "policy.yaml"
    assert invoke(baseline, candidate, policy) == 1
    first = capsys.readouterr().out
    report = json.loads(first)
    assert report["passed"] is False
    assert report["threshold"] == {"max_regressions": 0, "observed_regressions": 1}
    assert report["summary"] == {"total": 2, "regressed": 1, "improved": 0, "unchanged_pass": 1, "unchanged_fail": 0}
    assert [row["id"] for row in report["tasks"]] == ["lint", "tests"]
    regression = report["tasks"][1]
    assert regression["outcome"] == "regressed"
    assert regression["baseline"]["passed"] is True and regression["candidate"]["passed"] is False
    failed = [r for r in regression["candidate"]["results"] if not r["passed"]]
    assert len(failed) == 4 and all(r["evidence"] for r in failed)
    assert regression["candidate"]["complete"] is True
    assert report["policy_sha256"] == hashlib.sha256(policy.read_bytes()).hexdigest()
    assert len(regression["baseline"]["trajectory_sha256"]) == 64
    assert regression["baseline"]["trajectory_sha256"] != regression["candidate"]["trajectory_sha256"]
    assert invoke(baseline, candidate, policy) == 1
    assert capsys.readouterr().out == first
    # Manifest order, location and the caller's working directory do not affect output.
    baseline.write_text(json.dumps({"schema_version": 1, "tasks": [task("lint"), task("tests")]}))
    assert invoke(baseline, candidate, policy) == 1
    assert capsys.readouterr().out == first
    assert invoke(baseline, candidate, policy, "--max-regressions", "1") == 0
    allowed = json.loads(capsys.readouterr().out)
    assert allowed["passed"] is True and allowed["summary"] == report["summary"]
    assert allowed["tasks"] == report["tasks"]


def test_compare_improvement_does_not_offset_regression_and_existing_failures(tmp_path, capsys):
    baseline = manifest(tmp_path, "baseline.json", [task("improve", "codex_exec_violation.jsonl"), task("regress"), task("existing", "codex_exec_violation.jsonl")])
    candidate = manifest(tmp_path, "candidate.json", [task("improve"), task("regress", "codex_exec_violation.jsonl"), task("existing", "codex_exec_violation.jsonl")])
    assert invoke(baseline, candidate, FIX / "policy.yaml") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {"total": 3, "regressed": 1, "improved": 1, "unchanged_pass": 0, "unchanged_fail": 1}
    assert [r["outcome"] for r in report["tasks"]] == ["unchanged_fail", "improved", "regressed"]
    # This is a regression gate: a failure already in baseline does not fail by itself.
    assert invoke(baseline, baseline, FIX / "policy.yaml") == 0
    assert json.loads(capsys.readouterr().out)["summary"]["unchanged_fail"] == 2


def test_compare_mixed_adapters_relative_paths_roots_and_incomplete(tmp_path, capsys, monkeypatch):
    policy = tmp_path / "policy.yaml"
    policy.write_text('require_success: true\nallow_file_change_only: ["src/**"]\n')
    (tmp_path / "claude.jsonl").write_bytes((FIX / "claude_stream_ok.jsonl").read_bytes())
    baseline = manifest(tmp_path, "baseline.json", [task("case")])
    candidate = manifest(tmp_path, "candidate.json", [{"id": "case", "stream": "claude.jsonl", "source": "claude-stream", "root": "/workspace/demo"}])
    monkeypatch.chdir(tmp_path.parent)
    assert invoke(baseline, candidate, policy) == 0
    row = json.loads(capsys.readouterr().out)["tasks"][0]
    assert row["outcome"] == "unchanged_pass"
    assert row["candidate"]["source"] == "claude-stream"
    # Incomplete is a failed evaluation, not a malformed-input error or a pass.
    (tmp_path / "claude.jsonl").write_text('{"type":"system","subtype":"init"}\n')
    assert invoke(baseline, candidate, policy) == 1
    row = json.loads(capsys.readouterr().out)["tasks"][0]
    assert row["outcome"] == "regressed" and not row["candidate"]["complete"]
    assert row["candidate"]["results"][0]["id"] == "incomplete_stream"
    # Invalid JSON aborts with no report, even if some tasks were evaluated.
    (tmp_path / "claude.jsonl").write_text('{bad json}\n')
    output = tmp_path / "must-not-exist.json"
    assert invoke(baseline, candidate, policy, "-o", str(output)) == 2
    captured = capsys.readouterr()
    assert not captured.out and "candidate task case" in captured.err and not output.exists()


@pytest.mark.parametrize("change,diagnostic", [
    ({"tasks": []}, "nonempty"),
    ({"tasks": [task("same"), task("same")]}, "duplicate task"),
    ({"tasks": [task("different")]}, "task IDs differ"),
    ({"tasks": [task("case", typo=True)]}, "unknown key"),
    ({"tasks": [{"id": "case"}]}, "missing key"),
    ({"tasks": [task(9)]}, "string"),
    ({"tasks": [task("case", source="unknown")]}, "source"),
    ({"tasks": [task("case", root="relative")]}, "absolute"),
    ({"schema_version": True}, "schema_version"),
    ({"schema_version": 2}, "schema_version"),
    ({"typo": 1}, "unknown key"),
])
def test_compare_invalid_manifest_fails_without_report(tmp_path, capsys, change, diagnostic):
    baseline = manifest(tmp_path, "baseline.json", [task("case")])
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"schema_version": 1, "tasks": [task("case")], **change}))
    output = tmp_path / "report.json"
    assert invoke(baseline, candidate, FIX / "policy.yaml", "-o", str(output)) == 2
    captured = capsys.readouterr()
    assert diagnostic in captured.err and not captured.out and not output.exists()


@pytest.mark.parametrize("raw,diagnostic", [
    ('{"schema_version":1,"schema_version":1,"tasks":[]}', "duplicate JSON"),
    ('{"schema_version":1,"tasks":[],"x":NaN}', "JSON number"),
    ('{"schema_version":1,"tasks":[],"x":1e999}', "nonfinite"),
    ('[]', "object"), ('{', "JSON"), ('a' * (65536 + 1), "64 KiB"),
])
def test_compare_manifest_json_boundary(tmp_path, capsys, raw, diagnostic):
    baseline = manifest(tmp_path, "baseline.json", [task("case")])
    candidate = tmp_path / "candidate.json"
    candidate.write_text(raw)
    assert invoke(baseline, candidate, FIX / "policy.yaml") == 2
    captured = capsys.readouterr()
    assert diagnostic in captured.err and not captured.out


def test_compare_output_is_exclusive_and_threshold_is_nonnegative(tmp_path, capsys):
    baseline = manifest(tmp_path, "baseline.json", [task("case")])
    report = tmp_path / "report.json"
    assert invoke(baseline, baseline, FIX / "policy.yaml", "-o", str(report)) == 0
    original = report.read_bytes()
    assert json.loads(original)["passed"] is True
    assert report.stat().st_mode & 0o777 == 0o600
    assert not capsys.readouterr().out
    assert invoke(baseline, baseline, FIX / "policy.yaml", "-o", str(report)) == 2
    assert report.read_bytes() == original
    link = tmp_path / "link.json"
    link.symlink_to(report)
    assert invoke(baseline, baseline, FIX / "policy.yaml", "-o", str(link)) == 2
    assert report.read_bytes() == original
    for threshold in ("-1", "1.5"):
        with pytest.raises(SystemExit) as exc:
            invoke(baseline, baseline, FIX / "policy.yaml", "--max-regressions", threshold)
        assert exc.value.code == 2


def test_supplied_comparison_example(tmp_path, capsys):
    assert invoke(FIX / "compare_baseline.json", FIX / "compare_candidate.json", FIX / "policy.yaml") == 1
    report = json.loads(capsys.readouterr().out)
    assert [(row["id"], row["outcome"]) for row in report["tasks"]] == [("lint", "unchanged_pass"), ("tests", "regressed")]


def test_compare_keeps_ignored_counters_and_pending_call_failures(tmp_path, capsys):
    pending = tmp_path / "pending.jsonl"
    pending.write_text('\n'.join([
        '{"type":"item.started","item":{"id":"c","type":"command_execution","command":"pytest","status":"in_progress"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
        '{"type":"future_event"}',
    ]) + '\n')
    baseline = manifest(tmp_path, "baseline.json", [task("case")])
    candidate = manifest(tmp_path, "candidate.json", [{"id": "case", "stream": "pending.jsonl"}])
    assert invoke(baseline, candidate, FIX / "policy.yaml") == 1
    row = json.loads(capsys.readouterr().out)["tasks"][0]
    assert row["outcome"] == "regressed"
    assert row["candidate"]["pending_calls"] == ["codex:c"]
    assert row["candidate"]["ignored"] == {"future_event": 1}
    assert row["candidate"]["ignored_count"] == 1
    assert not row["candidate"]["results"][0]["passed"]


def test_compare_task_cap_and_mismatch_before_stream_reads(tmp_path, capsys):
    baseline = manifest(tmp_path, "baseline.json", [{"id": "missing", "stream": "does-not-exist"}])
    candidate = manifest(tmp_path, "candidate.json", [task(str(i)) for i in range(201)])
    assert invoke(baseline, candidate, FIX / "policy.yaml") == 2
    assert "at most 200" in capsys.readouterr().err
    candidate = manifest(tmp_path, "candidate.json", [{"id": "extra", "stream": "also-missing"}])
    assert invoke(baseline, candidate, FIX / "policy.yaml") == 2
    error = capsys.readouterr().err
    assert 'missing in candidate=["missing"]' in error and 'extra in candidate=["extra"]' in error


def test_compare_invalid_policy_aborts_without_report(tmp_path, capsys):
    baseline = manifest(tmp_path, "baseline.json", [task("case")])
    policy = tmp_path / "bad.yaml"
    policy.write_text('require_success: true\nrequire_success: false\n')
    output = tmp_path / "report.json"
    assert invoke(baseline, baseline, policy, "-o", str(output)) == 2
    captured = capsys.readouterr()
    assert "duplicate policy key" in captured.err and not captured.out and not output.exists()


def command_log(tmp_path, name, commands, *, complete=True):
    """Synthetic observations; none of the command strings are executed."""
    records = [{"type": "item.completed", "item": {
        "id": str(i), "type": "command_execution", "command": command,
        "exit_code": 0, "status": "completed", "aggregated_output": "",
    }} for i, command in enumerate(commands)]
    if complete:
        records.append({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
    path = tmp_path / name
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


@pytest.mark.parametrize("old_commands", [["a"], ["a", "c"]])
def test_rule_regression_within_failed_task_cannot_be_offset(tmp_path, capsys, old_commands):
    old = command_log(tmp_path, "old.jsonl", old_commands)
    new = command_log(tmp_path, "new.jsonl", ["b", "b"])
    baseline = manifest(tmp_path, "baseline.json", [{"id": "case", "stream": str(old)}])
    candidate = manifest(tmp_path, "candidate.json", [{"id": "case", "stream": str(new)}])
    policy = tmp_path / "policy.yaml"
    policy.write_text("forbid_command:\n  - pattern: '^a$'\n  - pattern: '^b$'\n  - pattern: '^c$'\n")
    # Whole-task comparison misses the newly failing B because both tasks fail.
    assert invoke(baseline, candidate, policy) == 0
    default = json.loads(capsys.readouterr().out)
    assert default["summary"]["unchanged_fail"] == 1
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule") == 1
    first = capsys.readouterr().out
    report = json.loads(first)
    assert report["regression_unit"] == "rule" and report["passed"] is False
    assert report["summary"] == default["summary"]
    assert report["threshold"] == {"max_regressions": 0, "observed_regressions": 1}
    assert report["rule_summary"] == {
        "total": 4, "regressed": 1, "improved": len(old_commands),
        "unchanged_pass": 3 - len(old_commands), "unchanged_fail": 0,
    }
    row = report["tasks"][0]
    assert row["outcome"] == "unchanged_fail"
    assert row["baseline"] == default["tasks"][0]["baseline"]
    assert row["candidate"] == default["tasks"][0]["candidate"]
    assert row["rules"] == [
        {"id": "forbid_command[0:^a$]", "baseline_passed": False, "candidate_passed": True, "outcome": "improved"},
        {"id": "forbid_command[1:^b$]", "baseline_passed": True, "candidate_passed": False, "outcome": "regressed"},
        {"id": "forbid_command[2:^c$]", "baseline_passed": "c" not in old_commands, "candidate_passed": True,
         "outcome": "improved" if "c" in old_commands else "unchanged_pass"},
        {"id": "incomplete_stream", "baseline_passed": True, "candidate_passed": True, "outcome": "unchanged_pass"},
    ]
    # Two violating B events count once for this (task ID, rule ID).
    assert len(row["candidate"]["results"][2]["evidence"]) == 2
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule") == 1
    assert capsys.readouterr().out == first
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule", "--max-regressions", "1") == 0
    allowed = json.loads(capsys.readouterr().out)
    assert allowed["passed"] is True and allowed["rule_summary"] == report["rule_summary"]
    assert allowed["tasks"] == report["tasks"]


def test_rule_counts_are_per_task_and_deterministic_across_manifest_order(tmp_path, capsys):
    a = command_log(tmp_path, "a.jsonl", ["a"])
    b = command_log(tmp_path, "b.jsonl", ["b"])
    clean = command_log(tmp_path, "clean.jsonl", [])
    policy = tmp_path / "policy.yaml"
    policy.write_text("forbid_command:\n  - pattern: '^a$'\n  - pattern: '^b$'\n")
    old = [{"id": name, "stream": str(a)} for name in ("z", "a")]
    old.append({"id": "improve", "stream": str(b)})
    new = [{"id": name, "stream": str(b)} for name in ("a", "z")]
    new.append({"id": "improve", "stream": str(clean)})
    baseline = manifest(tmp_path, "baseline.json", old)
    candidate = manifest(tmp_path, "candidate.json", new)
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule", "--max-regressions", "1") == 1
    first = capsys.readouterr().out
    report = json.loads(first)
    assert report["threshold"] == {"max_regressions": 1, "observed_regressions": 2}
    assert report["rule_summary"] == {"total": 9, "regressed": 2, "improved": 3, "unchanged_pass": 4, "unchanged_fail": 0}
    assert report["summary"] == {"total": 3, "regressed": 0, "improved": 1, "unchanged_pass": 0, "unchanged_fail": 2}
    assert [(t["id"], r["id"]) for t in report["tasks"] for r in t["rules"] if r["outcome"] == "regressed"] == [
        ("a", "forbid_command[1:^b$]"), ("z", "forbid_command[1:^b$]"),
    ]
    baseline = manifest(tmp_path, "reordered.json", list(reversed(old)))
    candidate = manifest(tmp_path, "other-order.json", list(reversed(new)))
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule", "--max-regressions", "1") == 1
    assert capsys.readouterr().out == first
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule", "--max-regressions", "2") == 0
    assert json.loads(capsys.readouterr().out)["threshold"]["observed_regressions"] == 2
    # Existing failures do not become regressions when compared with themselves.
    assert invoke(baseline, baseline, policy, "--regression-unit", "rule") == 0
    same = json.loads(capsys.readouterr().out)
    assert same["rule_summary"] == {"total": 9, "regressed": 0, "improved": 0, "unchanged_pass": 6, "unchanged_fail": 3}


def test_expanded_rules_with_identical_patterns_have_distinct_ids(tmp_path, capsys):
    old = command_log(tmp_path, "old.jsonl", [])
    new = command_log(tmp_path, "new.jsonl", ["b"])
    baseline = manifest(tmp_path, "baseline.json", [{"id": "case", "stream": str(old)}])
    candidate = manifest(tmp_path, "candidate.json", [{"id": "case", "stream": str(new)}])
    policy = tmp_path / "policy.yaml"
    policy.write_text("forbid_command:\n  - pattern: '^b$'\n  - pattern: '^b$'\nrequire_success: false\n")
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule", "--max-regressions", "1") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["rule_summary"] == {"total": 3, "regressed": 2, "improved": 0, "unchanged_pass": 1, "unchanged_fail": 0}
    assert [r["id"] for r in report["tasks"][0]["rules"] if r["outcome"] == "regressed"] == [
        "forbid_command[0:^b$]", "forbid_command[1:^b$]",
    ]


def test_rule_mode_counts_incomplete_stream_with_existing_failure_and_rejects_bad_log(tmp_path, capsys):
    old = command_log(tmp_path, "old.jsonl", ["a"])
    new = command_log(tmp_path, "new.jsonl", ["a"], complete=False)
    baseline = manifest(tmp_path, "baseline.json", [{"id": "case", "stream": str(old)}])
    candidate = manifest(tmp_path, "candidate.json", [{"id": "case", "stream": str(new)}])
    policy = tmp_path / "policy.yaml"
    policy.write_text("forbid_command: {pattern: '^a$'}\nrequire_success: false\n")
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["rule_summary"] == {"total": 2, "regressed": 1, "improved": 0, "unchanged_pass": 0, "unchanged_fail": 1}
    assert report["tasks"][0]["rules"][1] == {
        "id": "incomplete_stream", "baseline_passed": True, "candidate_passed": False, "outcome": "regressed",
    }
    output = tmp_path / "report.json"
    new.write_text("{bad json\n")
    assert invoke(baseline, candidate, policy, "--regression-unit", "rule", "-o", str(output)) == 2
    captured = capsys.readouterr()
    assert "candidate task case" in captured.err and not captured.out and not output.exists()


def test_regression_unit_invalid_choice_creates_no_report(tmp_path, capsys):
    output = tmp_path / "report.json"
    with pytest.raises(SystemExit) as exc:
        invoke("missing", "missing", "missing", "--regression-unit", "rules", "-o", str(output))
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err and not captured.out and not output.exists()


@pytest.mark.parametrize("unit", ["rules", "", None, True, 1, []])
def test_compare_api_rejects_invalid_unit_before_reading_inputs(unit):
    from agentwrench.gate.compare import compare
    from agentwrench.gate.model import InputError

    with pytest.raises(InputError, match="regression_unit"):
        compare("missing", "missing", "missing", regression_unit=unit)
