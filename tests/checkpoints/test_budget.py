"""Growth gates use actual pinned saver/serializer bytes, never graph estimates."""
from copy import deepcopy
import json

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from .test_scope import digest, execute, run


def put(path, identity, value, *, thread="t", namespace="", writes=()):
    with SqliteSaver.from_conn_string(str(path)) as saver:
        cp = empty_checkpoint()
        cp.update(v=4, id=identity, channel_values={"data": value})
        config = saver.put({"configurable": {"thread_id": thread, "checkpoint_ns": namespace}},
                           cp, {"source": "input", "step": 0, "parents": {}}, {})
        if writes:
            saver.put_writes(config, writes, "task")
        return len(saver.serde.dumps_typed(cp)[1])


def snapshot(path, target, *args):
    before = digest(path)
    proc = run("snapshot", path, *args, "--json")
    assert proc.returncode == 0, proc.stderr
    assert digest(path) == before
    target.write_text(proc.stdout)
    return json.loads(proc.stdout)


def test_gate_bytes_maps_exact_evidence_and_boundary(tmp_path):
    db = tmp_path / "data.db"
    baseline, current = tmp_path / "before.json", tmp_path / "after.json"
    put(db, "c1", {b"key": b"x"})  # actual upstream msgpack payload: 9 bytes
    before = snapshot(db, baseline)
    added_bytes = put(db, "c2", {b"key": b"x" * 11})  # 19 bytes, retained alongside c1
    after = snapshot(db, current)
    assert before["channels"][0]["bytes"] == 9
    assert after["channels"][0]["bytes"] == 28
    db_hash = digest(db)
    proc = run("budget", baseline, current, "--max-thread-growth-bytes", added_bytes,
               "--max-channel-growth-bytes", 17, "--json")
    assert proc.returncode == 3, proc.stderr
    result = json.loads(proc.stdout)
    assert result["passed"] is False
    assert result["violations"] == [{"group": "channel", "thread_id": "t", "checkpoint_ns": "",
        "channel": "data", "baseline_bytes": 9, "current_bytes": 28, "growth_bytes": 19,
        "limit_bytes": 17, "exceeded_bytes": 2}]
    proc = run("budget", baseline, current, "--max-thread-growth-bytes", added_bytes - 1, "--json")
    assert proc.returncode == 3
    violation = json.loads(proc.stdout)["violations"][0]
    assert violation["group"] == "thread" and violation["exceeded_bytes"] == 1
    proc = run("budget", baseline, current, "--max-thread-growth-bytes", added_bytes,
               "--max-channel-growth-bytes", 19, "--json")
    assert proc.returncode == 0 and json.loads(proc.stdout)["passed"]
    human = run("budget", baseline, current, "--max-channel-growth-bytes", 17)
    assert human.returncode == 3 and "exceeded by 2 bytes" in human.stdout
    assert digest(db) == db_hash


def test_snapshot_grouping_writes_namespaces_and_new_removed_groups(tmp_path):
    db = tmp_path / "groups.db"
    baseline, current = tmp_path / "before.json", tmp_path / "after.json"
    put(db, "c1", {b"key": b"x"}, writes=[("data", b"abc"), ("write-only", b"de")])
    put(db, "c1", "other", thread="other", namespace="child\x1b")
    put(db, "c1", "namespaced", namespace="child")
    execute(db, "UPDATE writes SET checkpoint_id='orphan' WHERE channel='write-only'")
    before = snapshot(db, baseline)
    assert len(before["threads"]) == 3
    channel = next(r for r in before["channels"] if r["thread_id"] == "t" and not r["checkpoint_ns"] and r["channel"] == "data")
    assert channel["checkpoint_value_bytes"] == 9 and channel["write_bytes"] == 3 and channel["bytes"] == 12
    assert next(r for r in before["threads"] if r["thread_id"] == "t" and not r["checkpoint_ns"])["write_bytes"] == 5
    execute(db, "DELETE FROM checkpoints WHERE thread_id='other'")
    put(db, "c1", b"new", thread="new")
    after = snapshot(db, current)
    proc = run("budget", baseline, current, "--max-channel-growth-bytes", 0, "--json")
    assert proc.returncode == 3
    result = json.loads(proc.stdout)
    assert result["violations"] == [{"group": "channel", "thread_id": "new", "checkpoint_ns": "", "channel": "data",
        "baseline_bytes": 0, "current_bytes": 3, "growth_bytes": 3, "limit_bytes": 0, "exceeded_bytes": 3}]
    removed = next(r for r in result["comparisons"] if r["thread_id"] == "other")
    assert removed["current_bytes"] == 0 and removed["growth_bytes"] < 0
    human = run("budget", baseline, current, "--max-channel-growth-bytes", 3)
    assert human.returncode == 0 and "\x1b" not in human.stdout
    selected = snapshot(db, tmp_path / "selected.json", "--thread", "t")
    assert selected["scope"] == {"thread": "t"}
    assert all(r["thread_id"] == "t" for r in selected["channels"])
    assert before != after


@pytest.mark.parametrize("table,field", [("checkpoints", "checkpoint"), ("writes", "value")])
def test_snapshot_opaque_refuses_without_output_or_mutation(tmp_path, table, field):
    db = tmp_path / "opaque.db"
    put(db, "c1", {}, writes=[("data", b"abc")])
    execute(db, f"UPDATE {table} SET type='unknown', {field}=?", (b"secret",))
    before = digest(db)
    proc = run("snapshot", db, "--json")
    assert proc.returncode == 2 and not proc.stdout
    assert "opaque" in proc.stderr and "secret" not in proc.stderr
    assert digest(db) == before


@pytest.mark.parametrize("mutation", ["version", "scope", "negative", "bool", "duplicate", "missing", "total", "parent", "write-total"])
def test_gate_rejects_invalid_or_incompatible_snapshots(tmp_path, mutation):
    db = tmp_path / "data.db"
    baseline, current = tmp_path / "before.json", tmp_path / "after.json"
    put(db, "c1", {b"key": 1})
    original = snapshot(db, baseline)
    bad = deepcopy(original)
    if mutation == "version":
        bad["format"] = "unknown"
    elif mutation == "scope":
        bad["scope"] = {"thread": "t"}
    elif mutation == "negative":
        bad["threads"][0]["checkpoint_bytes"] = -1
    elif mutation == "bool":
        bad["channels"][0]["bytes"] = True
    elif mutation == "duplicate":
        bad["channels"].append(bad["channels"][0])
    elif mutation == "missing":
        del bad["channels"]
    elif mutation == "total":
        bad["channels"][0]["bytes"] += 1
    elif mutation == "parent":
        bad["channels"][0]["thread_id"] = "missing"
    elif mutation == "write-total":
        bad["channels"][0]["bytes"] += 1
        bad["channels"][0]["write_bytes"] += 1
    current.write_text(json.dumps(bad))
    proc = run("budget", baseline, current, "--max-channel-growth-bytes", 0, "--json")
    assert proc.returncode == 2 and not proc.stdout and "Traceback" not in proc.stderr


@pytest.mark.parametrize("content", [b'{"format": 1, "format": 2}', b'[]', b'null', b'\xff', b'{', b'[' * 1100])
def test_gate_malformed_json_diagnostic(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_bytes(content)
    proc = run("budget", path, path, "--max-channel-growth-bytes", 0, "--json")
    assert proc.returncode == 2 and not proc.stdout and "Traceback" not in proc.stderr


def test_gate_requires_nonnegative_limit_and_empty_snapshot_passes(tmp_path):
    db = tmp_path / "empty.db"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        saver.setup()
    path = tmp_path / "empty.json"
    report = snapshot(db, path)
    assert report["threads"] == report["channels"] == []
    assert run("budget", path, path).returncode == 2
    assert run("budget", path, path, "--max-channel-growth-bytes", -1).returncode == 2
    proc = run("budget", path, path, "--max-channel-growth-bytes", 0, "--json")
    assert proc.returncode == 0 and json.loads(proc.stdout)["comparisons"] == []


def test_real_graph_snapshot_matches_sql_and_upstream_sizes(db, tmp_path):
    from collections import defaultdict
    from contextlib import closing
    import sqlite3
    from agentwrench.checkpoints.codec import SERDE

    report = snapshot(db, tmp_path / "real.json")
    expected = defaultdict(lambda: [0, 0])
    with closing(sqlite3.connect(db)) as con:
        for thread, ns, kind, blob in con.execute("SELECT thread_id,checkpoint_ns,type,checkpoint FROM checkpoints"):
            cp = SERDE.loads_typed((kind, blob))
            for channel, value in cp["channel_values"].items():
                expected[(thread, ns, channel)][0] += len(SERDE.dumps_typed(value)[1])
        for thread, ns, channel, size in con.execute("SELECT thread_id,checkpoint_ns,channel,length(value) FROM writes"):
            expected[(thread, ns, channel)][1] += size
        for row in report["threads"]:
            params = row["thread_id"], row["checkpoint_ns"]
            cp_bytes = con.execute("SELECT sum(length(checkpoint)) FROM checkpoints WHERE thread_id=? AND checkpoint_ns=?", params).fetchone()[0]
            wr_bytes = con.execute("SELECT sum(length(value)) FROM writes WHERE thread_id=? AND checkpoint_ns=?", params).fetchone()[0]
            assert row["bytes"] == cp_bytes + wr_bytes
    assert {(r["thread_id"], r["checkpoint_ns"], r["channel"]): [r["checkpoint_value_bytes"], r["write_bytes"]]
            for r in report["channels"]} == dict(expected)


def test_snapshot_resource_limits_are_explicit(tmp_path, monkeypatch):
    import agentwrench.checkpoints.budget as module
    from agentwrench.checkpoints.db import ScopeError, connect, read_rows

    db = tmp_path / "limit.db"
    put(db, "c1", "value")
    with connect(db) as con:
        cps, writes = read_rows(con)
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_GROUPS", 1)
        with pytest.raises(ScopeError, match="groups"):
            module.snapshot_report(cps, writes)
    wire_size = len(json.dumps(module.snapshot_report(cps, writes), ensure_ascii=True).encode("ascii")) + 1
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_SNAPSHOT_BYTES", wire_size)
        assert module.snapshot_report(cps, writes)
        patch.setattr(module, "MAX_SNAPSHOT_BYTES", wire_size - 1)
        with pytest.raises(ScopeError, match="64 MiB"):
            module.snapshot_report(cps, writes)
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_SNAPSHOT_BYTES", 10)
        with pytest.raises(ScopeError, match="64 MiB"):
            module.snapshot_report(cps, writes)
        path = tmp_path / "large.json"
        path.write_bytes(b" " * 11)
        with pytest.raises(ScopeError, match="input limit"):
            module.load_snapshot(path)
