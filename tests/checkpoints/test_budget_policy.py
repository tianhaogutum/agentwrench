"""Per-group policy contracts: exact selection, strict input, and byte evidence."""
from copy import deepcopy
import json

import pytest

from .test_budget import put, snapshot
from .test_scope import digest, run


POLICY_FORMAT = "lgckpt-scope.budget-policy/v1"


def policy():
    return {"format": POLICY_FORMAT,
            "default": {"max_thread_growth_bytes": 4096, "max_channel_growth_bytes": 0},
            "threads": [], "channels": [
                {"thread_id": "t", "checkpoint_ns": "", "channel": "data", "max_growth_bytes": 17},
                {"thread_id": "t", "checkpoint_ns": "", "channel": "__internal__", "max_growth_bytes": 4}]}


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_policy_exact_byte_evidence_and_boundary(tmp_path):
    db = tmp_path / "state.db"
    baseline, current = tmp_path / "baseline.json", tmp_path / "current.json"
    put(db, "c1", {b"key": b"x"}, writes=[("__internal__", b"a")])
    snapshot(db, baseline)
    put(db, "c2", {b"key": b"x" * 11}, writes=[("__internal__", b"abcd")])
    snapshot(db, current)
    path = save(tmp_path / "policy.json", policy())
    inputs = [db, baseline, current, path]
    hashes = list(map(digest, inputs))
    proc = run("budget", baseline, current, "--policy", path, "--json")
    assert proc.returncode == 3, proc.stderr
    result = json.loads(proc.stdout)
    assert result["policy_format"] == POLICY_FORMAT and not result["passed"]
    assert result["violations"] == [{
        "group": "channel", "thread_id": "t", "checkpoint_ns": "", "channel": "data",
        "baseline_bytes": 9, "current_bytes": 28, "growth_bytes": 19, "limit_bytes": 17,
        "exceeded_bytes": 2, "limit_source": "override"}]
    internal = next(r for r in result["comparisons"] if r.get("channel") == "__internal__")
    assert internal["growth_bytes"] == internal["limit_bytes"] == 4 and not internal["exceeded_bytes"]
    assert result["comparisons"][0]["limit_source"] == "default"
    assert list(map(digest, inputs)) == hashes
    human = run("budget", baseline, current, "--policy", path)
    assert human.returncode == 3 and "exceeded by 2 bytes" in human.stdout and "override" in human.stdout
    value = policy()
    value["channels"][0]["max_growth_bytes"] = 19
    save(path, value)
    db.unlink()  # Comparison needs no DB and executes no graph.
    proc = run("budget", baseline, current, "--policy", path, "--json")
    assert proc.returncode == 0 and json.loads(proc.stdout)["passed"]


def accounting(groups):
    """Construct saved accounting artifacts; no state payload or DB is required."""
    threads = {}
    channels = []
    for (thread, ns, channel), size in groups.items():
        threads[(thread, ns)] = threads.get((thread, ns), 0) + size
        channels.append({"thread_id": thread, "checkpoint_ns": ns, "channel": channel,
                         "checkpoint_value_bytes": size, "write_bytes": 0, "bytes": size})
    return {"format": "lgckpt-scope.growth/v1", "scope": {"thread": None}, "channels": channels,
            "threads": [{"thread_id": t, "checkpoint_ns": ns, "checkpoint_bytes": size,
                         "write_bytes": 0, "bytes": size} for (t, ns), size in threads.items()]}


def test_exact_selectors_defaults_independence_and_new_removed_groups(tmp_path):
    from agentwrench.checkpoints.budget import budget_report

    groups = [("t", "", "data"), ("t", "child", "data"), ("other", "", "data"),
              ("T", "", "data"), ("*", "", "data"), ("t", "", "__internal__"),
              ("t", "child", "__internal__"), ("т", "子\x1b", "DaTa")]
    before = accounting({**dict.fromkeys(groups, 1), ("removed", "", "data"): 9})
    after = accounting({**dict.fromkeys(reversed(groups), 4), ("new", "", "data"): 3})
    value = policy()
    value["default"] = {"max_thread_growth_bytes": 100, "max_channel_growth_bytes": 2}
    value["threads"] = [{"thread_id": "t", "checkpoint_ns": "", "max_growth_bytes": 5}]
    value["channels"][0]["max_growth_bytes"] = 0
    value["channels"][1]["max_growth_bytes"] = 3
    value["channels"].extend([
        {"thread_id": "*", "checkpoint_ns": "", "channel": "data", "max_growth_bytes": 3},
        {"thread_id": "т", "checkpoint_ns": "子\x1b", "channel": "DaTa", "max_growth_bytes": 0},
        {"thread_id": "removed", "checkpoint_ns": "", "channel": "data", "max_growth_bytes": 0},
        {"thread_id": "unused", "checkpoint_ns": "", "channel": "data", "max_growth_bytes": 0}])
    report = budget_report(before, after, policy=value)
    by_group = {(r["thread_id"], r["checkpoint_ns"], r.get("channel")): r for r in report["comparisons"]}
    # Thread override does not apply to another namespace, or propagate to channels.
    assert by_group[("t", "", None)]["exceeded_bytes"] == 1
    assert by_group[("t", "child", None)]["limit_bytes"] == 100
    assert by_group[("t", "", "data")]["limit_bytes"] == 0
    assert by_group[("t", "", "data")]["exceeded_bytes"] == 3
    for group in [("other", "", "data"), ("T", "", "data"),
                  ("t", "child", "data"), ("t", "child", "__internal__"), ("new", "", "data")]:
        assert by_group[group]["limit_source"] == "default"
        assert by_group[group]["limit_bytes"] == 2 and by_group[group]["exceeded_bytes"] == 1
    assert by_group[("*", "", "data")]["limit_bytes"] == 3
    assert by_group[("т", "子\x1b", "DaTa")]["exceeded_bytes"] == 3
    assert by_group[("removed", "", "data")]["growth_bytes"] == -9
    assert by_group[("removed", "", "data")]["exceeded_bytes"] == 0
    # Ordering ignores artifact order, including the unused override.
    assert list(by_group) == sorted(k for k in by_group if k[2] is None) + sorted(k for k in by_group if k[2] is not None)
    human = run("budget", save(tmp_path / "b.json", before), save(tmp_path / "c.json", after),
                "--policy", save(tmp_path / "p.json", value))
    assert human.returncode == 3 and "\x1b" not in human.stdout and "\\u001b" in human.stdout


@pytest.mark.parametrize("mutation", [
    "version", "missing-default", "missing-default-limit", "extra-default", "extra-top", "missing-array",
    "null-array", "object-array", "null-row", "missing-selector", "extra-selector", "channel-on-thread",
    "null-identifier", "integer-identifier", "negative-default", "bool-default", "null-default", "float-default",
    "negative-override", "bool-override", "null-override", "float-override", "missing-limit",
    "duplicate-channel", "duplicate-thread"])
def test_invalid_policy_has_no_passing_report(tmp_path, mutation):
    value = policy()
    row = value["channels"][0]
    if mutation == "version":
        value["format"] = "lgckpt-scope.budget-policy/v2"
    elif mutation == "missing-default":
        del value["default"]
    elif mutation == "missing-default-limit":
        del value["default"]["max_channel_growth_bytes"]
    elif mutation == "extra-default":
        value["default"]["max_growth_bytes"] = 1
    elif mutation == "extra-top":
        value["extra"] = 1
    elif mutation == "missing-array":
        del value["threads"]
    elif mutation == "null-array":
        value["channels"] = None
    elif mutation == "object-array":
        value["channels"] = {}
    elif mutation == "null-row":
        value["channels"].append(None)
    elif mutation == "missing-selector":
        del row["checkpoint_ns"]
    elif mutation == "extra-selector":
        row["pattern"] = "*"
    elif mutation == "channel-on-thread":
        value["threads"] = [deepcopy(row)]
    elif mutation == "null-identifier":
        row["thread_id"] = None
    elif mutation == "integer-identifier":
        row["channel"] = 1
    elif mutation.endswith("-default"):
        value["default"]["max_channel_growth_bytes"] = {
            "negative-default": -1, "bool-default": True, "null-default": None, "float-default": 1.0}[mutation]
    elif mutation.endswith("-override"):
        row["max_growth_bytes"] = {
            "negative-override": -1, "bool-override": False, "null-override": None, "float-override": 0.5}[mutation]
    elif mutation == "missing-limit":
        del row["max_growth_bytes"]
    elif mutation == "duplicate-channel":
        value["channels"].append({**row, "max_growth_bytes": 999})
    elif mutation == "duplicate-thread":
        value["threads"] = [{"thread_id": "t", "checkpoint_ns": "", "max_growth_bytes": n} for n in (1, 2)]
    empty = save(tmp_path / "empty.json", accounting({}))
    path = save(tmp_path / "policy.json", value)
    before = digest(path)
    proc = run("budget", empty, empty, "--policy", path, "--json")
    assert proc.returncode == 2 and not proc.stdout and "budget policy" in proc.stderr
    assert "Traceback" not in proc.stderr and digest(path) == before


@pytest.mark.parametrize("content", [b'null', b'[]', b'{', b'\xff', b'[' * 1100,
    b'{"format":1,"format":2}',
    b'{"default":{"max_thread_growth_bytes":0,"max_thread_growth_bytes":1}}',
    b'{"channels":[{"thread_id":"t","thread_id":"u"}]}'])
def test_invalid_policy_json(tmp_path, content):
    empty = save(tmp_path / "empty.json", accounting({}))
    path = tmp_path / "bad.json"
    path.write_bytes(content)
    proc = run("budget", empty, empty, "--policy", path, "--json")
    assert proc.returncode == 2 and not proc.stdout and "Traceback" not in proc.stderr


@pytest.mark.parametrize("flag", ["--max-thread-growth-bytes", "--max-channel-growth-bytes"])
def test_policy_and_uniform_limits_are_mutually_exclusive(tmp_path, flag):
    empty = save(tmp_path / "empty.json", accounting({}))
    path = save(tmp_path / "policy.json", policy())
    proc = run("budget", empty, empty, "--policy", path, flag, 0, "--json")
    assert proc.returncode == 2 and not proc.stdout and "cannot be combined" in proc.stderr


def test_policy_does_not_bypass_snapshot_validation(tmp_path):
    empty = save(tmp_path / "empty.json", accounting({}))
    value = accounting({})
    value["scope"]["thread"] = "t"
    other = save(tmp_path / "other.json", value)
    path = save(tmp_path / "policy.json", policy())
    proc = run("budget", empty, other, "--policy", path, "--json")
    assert proc.returncode == 2 and not proc.stdout and "scopes" in proc.stderr
    value["format"] = "unknown"
    save(other, value)
    proc = run("budget", empty, other, "--policy", path, "--json")
    assert proc.returncode == 2 and not proc.stdout and "invalid growth snapshot" in proc.stderr


def test_policy_defaults_only_empty_groups_and_resource_limits(tmp_path, monkeypatch):
    import agentwrench.checkpoints.budget as module
    from agentwrench.checkpoints.db import ScopeError

    value = policy()
    value["channels"] = []
    value["default"] = dict.fromkeys(value["default"], 0)
    report = module.budget_report(accounting({}), accounting({}), policy=value)
    assert report["passed"] and report["comparisons"] == []
    after = accounting({("", "", ""): 1})
    report = module.budget_report(accounting({}), after, policy=value)
    assert not report["passed"] and len(report["violations"]) == 2
    assert all(r["limit_source"] == "default" and r["limit_bytes"] == 0 for r in report["violations"])
    value["threads"] = [{"thread_id": "", "checkpoint_ns": "", "max_growth_bytes": 1}]
    value["channels"] = [{"thread_id": "", "checkpoint_ns": "", "channel": "", "max_growth_bytes": 1}]
    assert module.budget_report(accounting({}), after, policy=value)["passed"]
    # Limits include thread and channel overrides together, even unused ones.
    value = policy()
    value["threads"] = [{"thread_id": "t", "checkpoint_ns": "", "max_growth_bytes": 0}]
    path = save(tmp_path / "policy.json", value)
    wire_size = len(path.read_bytes())
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_OVERRIDES", 3)
        patch.setattr(module, "MAX_POLICY_BYTES", wire_size)
        assert module.load_policy(path) == value
        patch.setattr(module, "MAX_OVERRIDES", 2)
        with pytest.raises(ScopeError, match="invalid budget policy"):
            module.load_policy(path)
        patch.setattr(module, "MAX_OVERRIDES", 3)
        patch.setattr(module, "MAX_POLICY_BYTES", wire_size - 1)
        with pytest.raises(ScopeError, match="input limit"):
            module.load_policy(path)
    path.write_bytes(b" " * (1024 * 1024 + 1))
    empty = save(tmp_path / "empty.json", accounting({}))
    proc = run("budget", empty, empty, "--policy", path, "--json")
    assert proc.returncode == 2 and not proc.stdout and "1 MiB input limit" in proc.stderr


def test_missing_policy_file_is_a_clean_usage_failure(tmp_path):
    empty = save(tmp_path / "empty.json", accounting({}))
    proc = run("budget", empty, empty, "--policy", tmp_path / "missing.json", "--json")
    assert proc.returncode == 2 and not proc.stdout and "file operation failed" in proc.stderr
