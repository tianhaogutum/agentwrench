"""Opt-in coverage rejects a typo before a permissive default can hide it."""
from copy import deepcopy
import json

import pytest

from agentwrench.checkpoints.budget import budget_report
from agentwrench.checkpoints.db import ScopeError
from .test_budget_policy import accounting, policy, save
from .test_scope import digest, run


@pytest.mark.parametrize("json_output", [False, True])
def test_typo_passes_by_default_but_strict_coverage_refuses(tmp_path, json_output):
    baseline = save(tmp_path / "b.json", accounting({("t", "", "data"): 9}))
    current = save(tmp_path / "c.json", accounting({("t", "", "data"): 28}))
    value = policy()
    value["default"]["max_channel_growth_bytes"] = 4096
    value["channels"] = [{"thread_id": "t", "checkpoint_ns": "", "channel": "dtaa", "max_growth_bytes": 0}]
    path = save(tmp_path / "p.json", value)
    inputs = [baseline, current, path]
    before = list(map(digest, inputs))
    permissive = run("budget", baseline, current, "--policy", path, "--json")
    assert permissive.returncode == 0, permissive.stderr
    row = next(r for r in json.loads(permissive.stdout)["comparisons"] if r["group"] == "channel")
    assert row["growth_bytes"] == 19 and row["limit_bytes"] == 4096 and row["limit_source"] == "default"
    proc = run("budget", baseline, current, "--policy", path, "--require-policy-match",
               *(["--json"] if json_output else []))
    assert proc.returncode == 2 and not proc.stdout
    assert proc.stderr == 'error: unused budget policy channel selector: thread_id="t", checkpoint_ns="", channel="dtaa"\n'
    assert list(map(digest, inputs)) == before


def test_new_removed_zero_byte_and_independent_kinds_are_covered():
    before = accounting({("removed", "child", "data"): 9, ("t", "", "old"): 0})
    after = accounting({("new", "child", "data"): 3, ("t", "", "new"): 0})
    value = policy()
    value["threads"] = [{"thread_id": t, "checkpoint_ns": ns, "max_growth_bytes": 3}
                        for t, ns in [("removed", "child"), ("new", "child"), ("t", "")]]
    value["channels"] = [{"thread_id": t, "checkpoint_ns": ns, "channel": ch, "max_growth_bytes": 3}
                         for t, ns, ch in [("removed", "child", "data"), ("new", "child", "data"),
                                           ("t", "", "old"), ("t", "", "new")]]
    original = deepcopy((before, after, value))
    report = budget_report(before, after, policy=value, require_policy_match=True)
    assert report == budget_report(before, after, policy=value)
    assert report["passed"] and all(r["limit_source"] == "override" for r in report["comparisons"])
    assert [r["growth_bytes"] for r in report["comparisons"] if r["thread_id"] == "removed"] == [-9, -9]
    value["channels"][1]["max_growth_bytes"] = 2
    failed = budget_report(before, after, policy=value, require_policy_match=True)
    assert not failed["passed"] and failed["violations"][0]["exceeded_bytes"] == 1
    value["channels"][1]["max_growth_bytes"] = 3
    assert (before, after, value) == original


@pytest.mark.parametrize("kind,selector", [
    ("threads", ("T", "")), ("threads", ("t", "other")),
    ("channels", ("T", "", "data")), ("channels", ("t", "other", "data")),
    ("channels", ("t", "", "Data")), ("channels", ("t", "", "*")),
    ("channels", ("t", "", "")), ("channels", ("t", "", "dаta"))])
def test_near_matches_do_not_cover_exact_selectors(kind, selector):
    snapshot = accounting({("t", "", "data"): 0, ("other", "other", "other"): 0})
    value = policy()
    value["threads"], value["channels"] = [], []
    fields = ("thread_id", "checkpoint_ns", "channel")[:len(selector)]
    value[kind] = [{**dict(zip(fields, selector)), "max_growth_bytes": 0}]
    with pytest.raises(ScopeError, match="unused budget policy"):
        budget_report(snapshot, snapshot, policy=value, require_policy_match=True)


def test_existing_thread_does_not_cover_missing_channel_and_reverse():
    snapshot = accounting({("t", "", "data"): 0})
    value = policy()
    value["threads"] = [{"thread_id": "t", "checkpoint_ns": "", "max_growth_bytes": 0}]
    value["channels"] = [{"thread_id": "t", "checkpoint_ns": "", "channel": "missing", "max_growth_bytes": 0}]
    with pytest.raises(ScopeError, match="unused budget policy channel"):
        budget_report(snapshot, snapshot, policy=value, require_policy_match=True)
    # A matching channel override cannot cover a thread in another namespace.
    value["channels"][0]["channel"] = "data"
    value["threads"][0]["checkpoint_ns"] = "missing"
    with pytest.raises(ScopeError, match="unused budget policy thread"):
        budget_report(snapshot, snapshot, policy=value, require_policy_match=True)


def test_defaults_only_empty_snapshots_and_empty_identifiers():
    value = policy()
    value["threads"], value["channels"] = [], []
    empty = accounting({})
    assert budget_report(empty, empty, policy=value, require_policy_match=True)["comparisons"] == []
    value["threads"] = [{"thread_id": "", "checkpoint_ns": "", "max_growth_bytes": 0}]
    value["channels"] = [{"thread_id": "", "checkpoint_ns": "", "channel": "", "max_growth_bytes": 0}]
    with pytest.raises(ScopeError, match="unused budget policy thread"):
        budget_report(empty, empty, policy=value, require_policy_match=True)
    zero = accounting({("", "", ""): 0})
    assert budget_report(empty, zero, policy=value, require_policy_match=True)["passed"]


@pytest.mark.parametrize("kind", ["threads", "channels"])
def test_diagnostic_is_escaped_and_deterministic(tmp_path, kind):
    empty = save(tmp_path / "b.json", accounting({}))
    value = policy()
    value["threads"], value["channels"] = [], []
    fields = ("thread_id", "checkpoint_ns", "channel") if kind == "channels" else ("thread_id", "checkpoint_ns")
    selector = {"thread_id": 'a\x1b\n"\\é', "checkpoint_ns": "子\r\t"}
    if kind == "channels":
        selector["channel"] = "data\x00\x7f"
    value[kind] = [{**dict.fromkeys(fields, "z"), "max_growth_bytes": 0},
                   {**selector, "max_growth_bytes": 0}]
    # A lexically earlier channel still follows any unused thread.
    if kind == "threads":
        value["channels"] = [{"thread_id": "", "checkpoint_ns": "", "channel": "", "max_growth_bytes": 0}]
    path = save(tmp_path / "p.json", value)
    proc = run("budget", empty, empty, "--policy", path, "--require-policy-match", "--json")
    expected = ", ".join(f"{field}={json.dumps(selector[field], ensure_ascii=True)}" for field in fields)
    label = "channel" if kind == "channels" else "thread"
    assert proc.returncode == 2 and not proc.stdout
    assert proc.stderr == f"error: unused budget policy {label} selector: {expected}\n"
    assert proc.stderr.isascii() and "\x1b" not in proc.stderr
    value[kind].reverse()
    save(path, value)
    assert run("budget", empty, empty, "--policy", path, "--require-policy-match").stderr == proc.stderr


@pytest.mark.parametrize("limits", [[], ["--max-channel-growth-bytes", 0]])
def test_flag_requires_policy(tmp_path, limits):
    empty = save(tmp_path / "b.json", accounting({}))
    proc = run("budget", empty, empty, *limits, "--require-policy-match", "--json")
    assert proc.returncode == 2 and not proc.stdout
    assert "--require-policy-match requires --policy" in proc.stderr
    with pytest.raises(ScopeError, match="requires --policy"):
        budget_report(accounting({}), accounting({}), max_thread_growth_bytes=0, require_policy_match=True)


@pytest.mark.parametrize("bad", ["policy", "policy-json", "snapshot", "snapshot-json", "scope", "mixed-limits"])
def test_strict_coverage_preserves_input_validation(tmp_path, bad):
    before, after, value = accounting({}), accounting({}), policy()  # All overrides unused.
    expected = {"policy": "invalid budget policy", "policy-json": "invalid budget policy JSON",
                "snapshot": "invalid growth snapshot", "snapshot-json": "invalid growth snapshot JSON",
                "scope": "incompatible growth snapshot scopes", "mixed-limits": "cannot be combined"}[bad]
    if bad == "policy":
        value["channels"].append(value["channels"][0])
    elif bad == "snapshot":
        after["threads"] = [None]
    elif bad == "scope":
        after["scope"]["thread"] = "t"
    b, c, p = (save(tmp_path / name, data) for name, data in [("b.json", before), ("c.json", after), ("p.json", value)])
    if bad == "policy-json":
        p.write_text('{"format":1,"format":2}')
    elif bad == "snapshot-json":
        c.write_text('{')
    proc = run("budget", b, c, "--policy", p, "--require-policy-match", "--json",
               *(["--max-thread-growth-bytes", 0] if bad == "mixed-limits" else []))
    assert proc.returncode == 2 and not proc.stdout and expected in proc.stderr
    assert "unused budget policy" not in proc.stderr and "Traceback" not in proc.stderr


def test_strict_cli_never_opens_database_or_loads_graph(tmp_path, monkeypatch, capsys):
    import builtins
    import sqlite3
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
    before = save(tmp_path / "b.json", accounting({("t", "", "data"): 9}))
    after = save(tmp_path / "c.json", accounting({("t", "", "data"): 28}))
    value = policy()
    value["channels"] = value["channels"][:1]
    path = save(tmp_path / "p.json", value)
    inputs = [before, after, path]
    hashes = list(map(digest, inputs))
    arguments = ["budget", str(before), str(after), "--policy", str(path), "--require-policy-match", "--json"]
    assert cli.main(arguments) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["violations"][0]["exceeded_bytes"] == 2
    value["channels"][0]["max_growth_bytes"] = 19
    save(path, value)
    hashes[2] = digest(path)
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["passed"]
    assert list(map(digest, inputs)) == hashes
