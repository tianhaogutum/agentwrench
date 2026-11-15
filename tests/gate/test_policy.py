from copy import deepcopy
from pathlib import Path

import pytest

from agentwrench.gate.model import InputError
from agentwrench.gate.policy import check, compile_policy, glob, load_policy, matches, pattern
from agentwrench.gate.streams import normalize

FIX = Path(__file__).parent / "fixtures"


def one(policy, t=None, **kwargs):
    t = t or normalize(FIX / "codex_exec_ok.jsonl")
    return check(t, compile_policy(policy), **kwargs)["results"][-1]


@pytest.mark.parametrize("policy,passes,fails", [
    ({"require_command": {"pattern": "pytest", "exit_code": 0}}, True, False),
    ({"forbid_command": {"pattern": "pytest"}}, False, False),
    ({"forbid_file_change": {"glob": ".github/**"}}, True, False),
    ({"allow_file_change_only": ["src/**"]}, True, False),
    ({"max_tool_calls": 5}, True, True),
    ({"max_commands": 1}, False, False),
    ({"max_total_tokens": 49}, False, False),
    ({"require_final_message": {"pattern": "^Done:"}}, True, True),
    ({"forbid_error_events": True}, True, False),
    ({"require_success": True}, True, True),
    ({"order": [["git status", "pytest"]]}, True, True),
])
def test_rule_evidence_on_real_fixtures(policy, passes, fails):
    for name, expected in [("codex_exec_ok.jsonl", passes), ("codex_exec_violation.jsonl", fails)]:
        t = normalize(FIX / name)
        result = one(policy, t)
        assert result["passed"] is expected
        # Rules with matching events carry real, resolvable indexes, including failures.
        if result["evidence"]:
            assert all(t.events[index - 1]["index"] == index for index in result["evidence"])
        if not expected:
            assert result["evidence"]


@pytest.mark.parametrize("good,bad", [
    ({"require_command": {"pattern": "pytest", "exit_code": 0}}, {"require_command": {"pattern": "pytest", "exit_code": 9}}),
    ({"forbid_command": {"pattern": "curl"}}, {"forbid_command": {"pattern": "pytest"}}),
    ({"forbid_file_change": {"glob": "docs/**"}}, {"forbid_file_change": {"glob": "src/**"}}),
    ({"allow_file_change_only": ["src/**"]}, {"allow_file_change_only": []}),
    ({"max_tool_calls": 5}, {"max_tool_calls": 4}),
    ({"max_commands": 2}, {"max_commands": 1}),
    ({"max_total_tokens": 50}, {"max_total_tokens": 49}),
    ({"require_final_message": {"pattern": "Done"}}, {"require_final_message": {"pattern": "not done"}}),
    ({"order": [["git status", "pytest"]]}, {"order": [["pytest", "git status"]]}),
])
def test_each_rule_pass_and_fail(good, bad):
    assert one(good)["passed"]
    result = one(bad)
    assert not result["passed"] and result["evidence"]


def test_success_errors_missing_terminal_and_override():
    t = normalize(FIX / "codex_exec_ok.jsonl")
    t.events[-1]["success"] = False
    assert not one({"require_success": True}, t)["passed"]
    t.complete = False
    result = check(t, compile_policy({"max_commands": 2}))
    assert result["results"][0]["id"] == "incomplete_stream"
    assert result["summary"]["failed"] == 1
    assert check(t, compile_policy({"max_commands": 2}), allow_incomplete=True)["passed"]
    assert not one({"require_success": True}, t, allow_incomplete=True)["passed"]


def test_claude_budget_dedup_and_missing_exit_code():
    t = normalize(FIX / "claude_stream_ok.jsonl")
    assert one({"max_tool_calls": 3}, t)["passed"]
    assert not one({"max_tool_calls": 2}, t)["passed"]
    assert one({"require_command": {"pattern": "pytest"}}, t)["passed"]
    assert not one({"require_command": {"pattern": "pytest", "exit_code": 0}}, t)["passed"]
    assert one({"max_total_tokens": 19}, t)["passed"]
    assert not one({"max_total_tokens": 18}, t)["passed"]


def test_tokens_unknown_and_missing_terminal():
    t = normalize(FIX / "codex_exec_ok.jsonl")
    next(e for e in t.events if e["kind"] == "usage")["total_tokens"] = None
    result = one({"max_total_tokens": 1000}, t)
    assert not result["passed"] and "unknown" in result["message"]


def test_final_message_is_after_actions():
    t = normalize(FIX / "codex_exec_ok.jsonl")
    t.emit("tool_call", 99, call_id="last", name="last", arguments={})
    assert not one({"require_final_message": {"pattern": "Done"}}, t)["passed"]


def test_order_selectors_all_and_missing():
    policy = {"order": [[{"kind": "file_change", "glob": "src/**"}, {"kind": "tool_call", "name": "docs__lookup"}]]}
    assert one(policy)["passed"]
    assert not one({"order": [["missing", "pytest"]]})["passed"]
    assert not one({"order": [["pytest", "pytest"]]})["passed"]
    t = normalize(FIX / "codex_exec_ok.jsonl")
    t.emit("command", 99, command="git status --short", completed=True)
    assert not one({"order": [["git status", "pytest"]]}, t)["passed"]


@pytest.mark.parametrize("path", ["../secret", "src/../../secret", "/etc/passwd"])
def test_allowlist_does_not_allow_escape(path):
    t = normalize(FIX / "codex_exec_ok.jsonl")
    # Paths are already canonicalized by adapters; these examples exercise the guard itself.
    next(e for e in t.events if e["kind"] == "file_change")["changes"][0]["path"] = path if path != "src/../../secret" else "../secret"
    assert not one({"allow_file_change_only": ["**"]}, t)["passed"]


@pytest.mark.parametrize("raw,match", [
    ({"typo": True}, 'typo'), ({}, 'at least one'), ({"max_commands": True}, 'integer'),
    ({"max_commands": -1}, 'integer'), ({"forbid_error_events": "yes"}, 'boolean'),
    ({"require_command": {"pattern": "x", "typo": 1}}, 'typo'),
    ({"require_command": {"pattern": "["}}, 'invalid regex'),
    ({"forbid_file_change": {"glob": "["}}, 'invalid glob'),
    ({"allow_file_change_only": ["a/**b"]}, 'whole path segment'),
    ({"require_command": []}, 'nonempty'), ({"order": [["a"]]}, 'exactly two'),
    ({"order": [[{"kind": "bad"}, "b"]]}, 'unknown event kind'),
    ({"order": [[{"kind": "usage", "pattern": "x"}, "b"]]}, 'requires kind'),
    ({"require_final_message": {"exit_code": 0}}, 'unknown key'),
    ({"max_total_tokens": 1.0}, 'integer'),
    ({"require_command": [{"pattern": "x"}] * 201}, '200 rules'),
])
def test_invalid_policy(raw, match):
    with pytest.raises(InputError, match=match):
        compile_policy(raw)


@pytest.mark.parametrize("text,match", [
    ('max_commands: 1\nmax_commands: 2', 'duplicate'),
    ('1: yes', 'strings'), ('max_commands: &x 2', 'anchors'),
    ('max_commands: *x', 'aliases'), ('!!python/object/apply:os.system ["touch dangerous"]', 'YAML'),
    ('max_commands: [', 'YAML'), ('[]', 'object'), ('a' * 65537, '64 KiB'),
])
def test_yaml_boundary(tmp_path, text, match):
    p = tmp_path / "policy.yaml"
    p.write_text(text)
    with pytest.raises(InputError, match=match):
        load_policy(p)
    assert not (tmp_path / "dangerous").exists()


@pytest.mark.parametrize("g,path,expected", [
    ('src/*.py', 'src/a.py', True), ('src/*.py', 'src/deep/a.py', False),
    ('**/*.py', 'a.py', True), ('**/*.py', 'src/deep/a.py', True),
    ('.github/**', '.github/workflows/ci.yml', True),
    ('src/?.[py]', 'src/a.p', True), ('src/[!a].py', 'src/b.py', True),
    ('src/[!a].py', 'src/a.py', False), ('**', '.hidden', True),
])
def test_globs(g, path, expected):
    assert matches(glob(g), path) is expected


@pytest.mark.parametrize("g", ['[]', '[!]', '[z-a]', '[abc', 'abc]', 'a{b,c}', 'a\\b', '***'])
def test_invalid_globs(g):
    with pytest.raises(InputError, match="glob"):
        glob(g)


def test_regex_has_time_budget():
    with pytest.raises(InputError, match="budget"):
        matches(pattern('(a+)+$', 'test'), 'a' * 100000 + '!')


def test_globs_cannot_be_bypassed_by_newlines():
    assert matches(glob('.github/**'), '.github/a\nb')
    t = normalize(FIX / 'codex_exec_ok.jsonl')
    next(e for e in t.events if e['kind'] == 'file_change')['changes'][0]['path'] = '.github/a\nb'
    assert not one({'forbid_file_change': {'glob': '.github/**'}}, t)['passed']


def test_invalid_selector_type_is_input_error():
    with pytest.raises(InputError, match='unknown event kind'):
        compile_policy({'order': [[{'kind': []}, 'pytest']]})
