"""Reusable policies can expose unused selectors without strict refusal."""
from copy import deepcopy
import json

import pytest

from agentwrench.checkpoints.budget import budget_report
from agentwrench.checkpoints.db import ScopeError
from .test_budget_policy import accounting, policy, save
from .test_scope import digest, run


def counts(total, covered, unused):
    return {"total_overrides": total, "covered_overrides": covered,
            "unused_overrides": len(unused), "unused_selectors": unused}


@pytest.mark.parametrize("allowance,code", [(17, 3), (19, 0)])
def test_cli_audits_unused_policy_without_rejecting(tmp_path, allowance, code):
    before = accounting({("t", "", "data"): 9, ("removed", "child", "data"): 9,
                         ("zero", "", ""): 0})
    after = accounting({("t", "", "data"): 28, ("new", "child", "data"): 3,
                        ("zero", "", ""): 0})
    value = policy()
    value["threads"] = [{"thread_id": t, "checkpoint_ns": ns, "max_growth_bytes": 4096}
                        for t, ns in [("removed", "child"), ("new", "child"), ("zero", ""), ("T", "")]]
    value["channels"] = [{"thread_id": t, "checkpoint_ns": ns, "channel": ch, "max_growth_bytes": allowance}
                         for t, ns, ch in [("t", "", "data"), ("removed", "child", "data"),
                                           ("new", "child", "data"), ("zero", "", ""),
                                           ("t", "", "Data"), ("t", "", "absent")]]
    b, c, p = [save(tmp_path / name, data) for name, data in
               [("b.json", before), ("c.json", after), ("p.json", value)]]
    hashes = list(map(digest, [b, c, p]))
    default = run("budget", b, c, "--policy", p, "--json")
    assert default.returncode == code and not default.stderr
    default_report = json.loads(default.stdout)
    assert set(default_report) == {"passed", "scope", "comparisons", "violations", "policy_format"}
    proc = run("budget", b, c, "--policy", p, "--show-policy-coverage", "--json")
    assert proc.returncode == code, proc.stderr
    assert not proc.stderr
    report = json.loads(proc.stdout)
    assert report.pop("policy_coverage") == {
        "threads": counts(4, 3, [{"thread_id": "T", "checkpoint_ns": ""}]),
        "channels": counts(6, 4, [{"thread_id": "t", "checkpoint_ns": "", "channel": ch}
                                  for ch in ["Data", "absent"]])}
    assert report == default_report
    assert list(map(digest, [b, c, p])) == hashes
    assert sorted(path.name for path in tmp_path.iterdir()) == ["b.json", "c.json", "p.json"]


def test_evidence_matching_is_exact_independent_and_deterministic():
    before = accounting({("t", "", "data"): 0, ("*", "子", "*"): 0, ("", "", ""): 0})
    # A thread group need not have any channels, and cannot cover a channel override.
    before["threads"].append({"thread_id": "only-thread", "checkpoint_ns": "", "checkpoint_bytes": 0,
                              "write_bytes": 0, "bytes": 0})
    after = deepcopy(before)
    value = policy()
    value["threads"] = [{"thread_id": t, "checkpoint_ns": ns, "max_growth_bytes": 0}
                        for t, ns in [("t", "other"), ("T", ""), ("*", "子"), ("", "")]]
    unused = [("only-thread", "", "data"), ("t", "other", "data"), ("t", "", "Data"),
              ("t", "", "*"), ("t", "", ""), ("t", "", "dаta")]
    value["channels"] = [{"thread_id": t, "checkpoint_ns": ns, "channel": ch, "max_growth_bytes": 0}
                         for t, ns, ch in [*unused, ("t", "", "data"), ("*", "子", "*"), ("", "", "")]]
    original = deepcopy((before, after, value))
    report = budget_report(before, after, policy=value, show_policy_coverage=True)
    assert report["policy_coverage"] == {
        "threads": counts(4, 2, [{"thread_id": "T", "checkpoint_ns": ""},
                                 {"thread_id": "t", "checkpoint_ns": "other"}]),
        "channels": counts(9, 3, [dict(zip(("thread_id", "checkpoint_ns", "channel"), group))
                                  for group in sorted(unused)])}
    assert (before, after, value) == original
    for artifact in (before, after, value):
        for name in ("threads", "channels"):
            artifact[name].reverse()
    assert budget_report(before, after, policy=value, show_policy_coverage=True) == report


@pytest.mark.parametrize("json_output", [False, True])
def test_evidence_does_not_bypass_strict_refusal(tmp_path, json_output):
    empty = save(tmp_path / "empty.json", accounting({}))
    path = save(tmp_path / "p.json", policy())
    args = ["budget", empty, empty, "--policy", path, "--require-policy-match"]
    flags = ["--json"] if json_output else []
    strict = run(*args, *flags)
    both = run(*args, "--show-policy-coverage", *flags)
    assert both.returncode == strict.returncode == 2
    assert both.stderr == strict.stderr and not both.stdout


def test_empty_defaults_only_and_fully_covered_strict_evidence():
    value = policy()
    value["threads"], value["channels"] = [], []
    empty = accounting({})
    report = budget_report(empty, empty, policy=value, show_policy_coverage=True, require_policy_match=True)
    assert report["passed"] and report["comparisons"] == []
    assert report["policy_coverage"] == {name: counts(0, 0, []) for name in ("threads", "channels")}
    value["threads"] = [{"thread_id": "", "checkpoint_ns": "", "max_growth_bytes": 0}]
    value["channels"] = [{"thread_id": "", "checkpoint_ns": "", "channel": "", "max_growth_bytes": 0}]
    report = budget_report(empty, empty, policy=value, show_policy_coverage=True)
    assert report["policy_coverage"] == {
        "threads": counts(1, 0, [{"thread_id": "", "checkpoint_ns": ""}]),
        "channels": counts(1, 0, [{"thread_id": "", "checkpoint_ns": "", "channel": ""}])}
    for size, passed in [(0, True), (1, False)]:
        report = budget_report(empty, accounting({("", "", ""): size}), policy=value,
                               show_policy_coverage=True, require_policy_match=True)
        assert report["passed"] == passed
        assert report["policy_coverage"] == {name: counts(1, 1, []) for name in ("threads", "channels")}


def test_human_evidence_escapes_unused_identifiers(tmp_path):
    empty = save(tmp_path / "b.json", accounting({}))
    value = policy()
    selector = {"thread_id": 'a\x1b\n"\\é', "checkpoint_ns": "子\r\t"}
    value["threads"] = [{**selector, "max_growth_bytes": 0},
                        {"thread_id": "z", "checkpoint_ns": "", "max_growth_bytes": 0}]
    value["channels"] = [{**selector, "channel": "data\x00\x7f", "max_growth_bytes": 0}]
    path = save(tmp_path / "p.json", value)
    proc = run("budget", empty, empty, "--policy", path, "--show-policy-coverage")
    quoted = ", ".join(f"{k}={json.dumps(v, ensure_ascii=True)}" for k, v in selector.items())
    assert proc.returncode == 0 and not proc.stderr
    assert proc.stdout == ('growth budget: PASS\npolicy coverage:\n'
        '  threads: 2 total overrides, 0 covered, 2 unused\n'
        f'    unused: {quoted}\n'
        '    unused: thread_id="z", checkpoint_ns=""\n'
        '  channels: 1 total overrides, 0 covered, 1 unused\n'
        f'    unused: {quoted}, channel="data\\u0000\\u007f"\n')
    assert proc.stdout.isascii() and "\x1b" not in proc.stdout
    report = run("budget", empty, empty, "--policy", path, "--show-policy-coverage", "--json")
    assert json.loads(report.stdout)["policy_coverage"]["channels"]["unused_selectors"] == [
        {**selector, "channel": "data\x00\x7f"}]
    assert run("budget", empty, empty, "--policy", path).stdout == "growth budget: PASS\n"


@pytest.mark.parametrize("limits", [[], ["--max-channel-growth-bytes", 0]])
def test_evidence_requires_policy(tmp_path, limits):
    empty = save(tmp_path / "b.json", accounting({}))
    proc = run("budget", empty, empty, *limits, "--show-policy-coverage", "--json")
    assert proc.returncode == 2 and not proc.stdout
    assert proc.stderr == "error: --show-policy-coverage requires --policy\n"
    with pytest.raises(ScopeError, match="--show-policy-coverage requires --policy"):
        budget_report(accounting({}), accounting({}), max_thread_growth_bytes=0, show_policy_coverage=True)


@pytest.mark.parametrize("bad", ["policy", "policy-json", "snapshot", "snapshot-json", "scope", "mixed-limits"])
def test_evidence_preserves_input_refusals(tmp_path, bad):
    before, after, value = accounting({}), accounting({}), policy()
    expected = {"policy": "invalid budget policy", "policy-json": "invalid budget policy JSON",
                "snapshot": "invalid growth snapshot", "snapshot-json": "invalid growth snapshot JSON",
                "scope": "incompatible growth snapshot scopes", "mixed-limits": "cannot be combined"}[bad]
    if bad == "policy":
        value["channels"].append(value["channels"][0])
    elif bad == "snapshot":
        after["threads"] = [None]
    elif bad == "scope":
        after["scope"]["thread"] = "t"
    b, c, p = [save(tmp_path / name, data) for name, data in
               [("b.json", before), ("c.json", after), ("p.json", value)]]
    if bad == "policy-json":
        p.write_text('{"format":1,"format":2}')
    elif bad == "snapshot-json":
        c.write_text('{')
    proc = run("budget", b, c, "--policy", p, "--show-policy-coverage", "--json",
               *(["--max-thread-growth-bytes", 0] if bad == "mixed-limits" else []))
    assert proc.returncode == 2 and not proc.stdout and expected in proc.stderr
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("limit", ["MAX_POLICY_BYTES", "MAX_OVERRIDES", "MAX_SNAPSHOT_BYTES", "MAX_GROUPS"])
def test_evidence_preserves_resource_limits_and_json_only_access(tmp_path, monkeypatch, capsys, limit):
    import builtins
    import sqlite3
    import agentwrench.checkpoints.budget as module
    from agentwrench.checkpoints import cli

    def forbidden(*args, **kwargs):
        pytest.fail("budget must not access a database or graph")

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "langgraph.graph" or name.startswith("langgraph.graph."):
            forbidden()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(cli, "connect", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    b = save(tmp_path / "b.json", accounting({("t", "", "data"): 9}))
    c = save(tmp_path / "c.json", accounting({("t", "", "data"): 28}))
    p = save(tmp_path / "p.json", policy())
    arguments = ["budget", str(b), str(c), "--policy", str(p), "--show-policy-coverage", "--json"]
    hashes = list(map(digest, [b, c, p]))
    boundary = {"MAX_POLICY_BYTES": p.stat().st_size, "MAX_OVERRIDES": 2,
                "MAX_SNAPSHOT_BYTES": max(b.stat().st_size, c.stat().st_size), "MAX_GROUPS": 2}[limit]
    monkeypatch.setattr(module, limit, boundary)
    assert cli.main(arguments) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["violations"][0]["exceeded_bytes"] == 2
    assert report["policy_coverage"]["channels"]["unused_overrides"] == 1
    monkeypatch.setattr(module, limit, boundary - 1)
    assert cli.main(arguments) == 2
    captured = capsys.readouterr()
    assert not captured.out and "error:" in captured.err and "Traceback" not in captured.err
    assert list(map(digest, [b, c, p])) == hashes
