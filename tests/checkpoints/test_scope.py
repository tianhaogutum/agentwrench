from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from agentwrench.checkpoints import db as db_module
from agentwrench.checkpoints.codec import SERDE, decode
from agentwrench.checkpoints.db import ScopeError, connect, expected_schema, read_rows, schema
from agentwrench.checkpoints.inspect import diff_report, stats_report, threads_report
from agentwrench.checkpoints.prune import prune
from .make_fixture import build_graph, make_fixture


def run(*args):
    return subprocess.run([sys.executable, "-m", "agentwrench", "checkpoints", *map(str, args)],
                          text=True, capture_output=True, timeout=15)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def query(path, sql, args=()):
    with closing(sqlite3.connect(path)) as con:
        return con.execute(sql, args).fetchall()


def execute(path, sql, args=()):
    with closing(sqlite3.connect(path)) as con:
        con.execute(sql, args)
        con.commit()


def test_pinned_schema_and_upstream_delete_api():
    with closing(sqlite3.connect(":memory:")) as con:
        SqliteSaver(con).setup()
        assert schema(con) == expected_schema()
        assert SqliteSaver.prune is BaseCheckpointSaver.prune
        with pytest.raises(NotImplementedError):
            SqliteSaver(con).prune(["t-1"])


def test_fixture_is_byte_deterministic_and_real_graph_can_read(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    make_fixture(a, 2, 3)
    make_fixture(b, 2, 3)
    assert digest(a) == digest(b)
    with closing(sqlite3.connect(a, check_same_thread=False)) as con:
        graph = build_graph(SqliteSaver(con))
        state = graph.get_state({"configurable": {"thread_id": "t-1"}})
        assert state.values["counter"] == 3
        assert state.values["items"] == ["event-0001", "event-0002", "event-0003"]


def test_fixture_refuses_overwrite(tmp_path):
    path = tmp_path / "already.db"
    path.write_text("valuable")
    with pytest.raises(FileExistsError):
        make_fixture(path, 1, 1)
    assert path.read_text() == "valuable"


def test_reports_match_sql_and_channel_serialization(db):
    with connect(db) as con:
        cps, writes = read_rows(con)
        threads = threads_report(cps, writes)
        stats = stats_report(cps, writes, 100)
    assert [(r["thread_id"], r["checkpoints"]) for r in threads] == [("t-1", 12), ("t-2", 12)]
    cp_bytes = query(db, "SELECT sum(length(checkpoint)) FROM checkpoints")[0][0]
    wr_bytes = query(db, "SELECT sum(length(value)) FROM writes")[0][0]
    assert stats["bytes"] == cp_bytes + wr_bytes == sum(t["bytes"] for t in threads)
    assert stats["checkpoints"] == query(db, "SELECT count(*) FROM checkpoints")[0][0]
    assert stats["writes"] == query(db, "SELECT count(*) FROM writes")[0][0]
    expected_items = 0
    for row in cps:
        cp = SERDE.loads_typed((row["type"], row["checkpoint"]))
        if "items" in cp["channel_values"]:
            expected_items += len(SERDE.dumps_typed(cp["channel_values"]["items"])[1])
    items = next(c for c in stats["channels"] if c["channel"] == "items")
    assert items["checkpoint_value_bytes"] == expected_items
    assert items["write_bytes"] == query(db, "SELECT sum(length(value)) FROM writes WHERE channel='items'")[0][0]
    assert len(stats["growth_per_step"]) == 24
    assert stats["growth_per_step"][0]["growth_bytes"] is None
    assert stats["growth_per_step"][1]["growth_bytes"] == stats["growth_per_step"][1]["bytes"] - stats["growth_per_step"][0]["bytes"]
    assert stats["largest_checkpoints"][0]["bytes"] == max(r["bytes"] for r in stats["growth_per_step"])
    assert threads[0]["first"] == "2026-01-01T00:00:01+00:00"
    assert threads[0]["last"] == "2026-01-01T00:00:12+00:00"


def test_diff_append_scalar_remove_and_add(db):
    with connect(db) as con:
        cps, _ = read_rows(con, "t-1")
    result = diff_report(cps)
    changes = {c["channel"]: c for c in result["changes"]}
    assert changes["items"]["summary"] == "+1 items"
    assert changes["items"]["appended"] is True
    assert changes["counter"]["before"] == "3"
    assert changes["counter"]["after"] == "4"
    assert changes["branch:to:advance"]["kind"] == "removed"
    reverse = diff_report(cps, from_id=cps[-1]["checkpoint_id"], to_id=cps[-2]["checkpoint_id"])
    assert next(c for c in reverse["changes"] if c["channel"] == "branch:to:advance")["kind"] == "added"
    initial = diff_report(cps, from_id=cps[0]["checkpoint_id"], to_id=cps[-1]["checkpoint_id"])
    assert next(c for c in initial["changes"] if c["channel"] == "payload")["after"].endswith("...")


@pytest.mark.parametrize("keep", [1, 2, 99])
def test_prune_preserves_graph_and_can_continue_all_threads(db, tmp_path, keep):
    configs = [{"configurable": {"thread_id": t}} for t in ("t-1", "t-2")]
    with closing(sqlite3.connect(db, check_same_thread=False)) as con:
        graph = build_graph(SqliteSaver(con))
        before = [graph.get_state(c) for c in configs]
    backup = tmp_path / "backup.db"
    result = prune(db, keep, backup=backup, vacuum=True)
    assert result["verified"] is True
    assert result["vacuum"] == "completed"
    assert query(backup, "SELECT count(*) FROM checkpoints")[0][0] == 24
    assert backup.stat().st_mode & 0o777 == 0o600
    assert query(db, "SELECT count(*) FROM checkpoints")[0][0] == min(keep, 12) * 2
    assert result["delete_checkpoints"] == 24 - min(keep, 12) * 2
    with closing(sqlite3.connect(db, check_same_thread=False)) as con:
        graph = build_graph(SqliteSaver(con))
        for config, old in zip(configs, before):
            # Includes next, metadata, created_at, pending tasks, and parent config.
            assert graph.get_state(config) == old
            state = graph.invoke({"items": []}, config)
            assert state["counter"] == 5
            assert state["items"] == old.values["items"] + ["event-0005"]


def test_thread_filter_retains_pending_writes_and_unselected_rows(db):
    before = query(db, "SELECT * FROM checkpoints WHERE thread_id='t-2'")
    writes_before = query(db, "SELECT * FROM writes WHERE thread_id='t-2'")
    kept_ids = [r[0] for r in query(db, "SELECT checkpoint_id FROM checkpoints WHERE thread_id='t-1' ORDER BY checkpoint_id DESC LIMIT 2")]
    kept_writes = query(db, "SELECT * FROM writes WHERE thread_id='t-1' AND checkpoint_id IN (?,?) ORDER BY checkpoint_id,task_id,idx", kept_ids)
    result = prune(db, 2, thread="t-1", no_backup=True)
    assert result["delete_checkpoints"] == 10
    assert query(db, "SELECT * FROM checkpoints WHERE thread_id='t-2'") == before
    assert query(db, "SELECT * FROM writes WHERE thread_id='t-2'") == writes_before
    assert query(db, "SELECT * FROM writes WHERE thread_id='t-1' ORDER BY checkpoint_id,task_id,idx") == kept_writes


def test_dry_run_file_hash_and_no_backup_or_vacuum_side_effects(db, tmp_path):
    before = digest(db)
    backup = tmp_path / "must-not-exist.db"
    result = prune(db, 2, dry_run=True, backup=backup, vacuum=True)
    assert result["delete_checkpoints"] == 20
    assert result["delete_writes"] > 0
    assert result["verified"] is False
    assert digest(db) == before
    assert not backup.exists()
    assert not Path(str(db) + "-wal").exists()


def test_backup_refuses_existing_and_sidecar_paths(db, tmp_path):
    before = digest(db)
    existing = tmp_path / "existing.db"
    existing.write_text("keep me")
    proc = run("prune", db, "--keep-latest", 2, "--backup", existing)
    assert proc.returncode == 2
    assert existing.read_text() == "keep me"
    with pytest.raises(ScopeError, match="sidecars"):
        prune(db, 2, backup=str(db) + "-wal")
    assert digest(db) == before


@pytest.mark.parametrize("args,code,message", [
    (("prune", "--keep-latest", "0", "--no-backup"), 2, ">= 1"),
    (("prune", "--keep-latest", "2"), 2, "requires --backup"),
    (("stats", "--thread", "missing"), 1, "thread id not found"),
    (("prune", "--keep-latest", "2", "--thread", "missing", "--no-backup"), 1, "thread id not found"),
    (("stats", "--top", "0"), 2, ">= 1"),
    (("diff", "--thread", "t-1", "--from", "a"), 2, "use --from"),
    (("diff", "--thread", "t-1", "--from", "a", "--to", "b"), 1, "checkpoint id not found"),
])
def test_cli_failures_do_not_change_db(db, args, code, message):
    before = digest(db)
    proc = run(args[0], db, *args[1:])
    assert proc.returncode == code
    assert message in proc.stderr
    assert "Traceback" not in proc.stderr
    assert digest(db) == before


@pytest.mark.parametrize("kind", ["text", "sqlite", "missing", "trigger", "old-schema"])
def test_not_supported_db_errors(tmp_path, kind):
    path = tmp_path / "wrong.db"
    if kind == "text":
        path.write_text("not sqlite")
    elif kind == "sqlite":
        execute(path, "CREATE TABLE unrelated(x)")
    elif kind in ("trigger", "old-schema"):
        make_fixture(path, 1, 1)
        if kind == "trigger":
            execute(path, "CREATE TRIGGER forbidden AFTER DELETE ON checkpoints BEGIN DELETE FROM writes; END")
        else:
            execute(path, "ALTER TABLE writes DROP COLUMN task_path")
    proc = run("threads", path)
    assert proc.returncode == 2
    assert "error:" in proc.stderr
    assert "Traceback" not in proc.stderr
    if kind == "missing":
        assert not path.exists()
    if kind in ("sqlite", "trigger", "old-schema"):
        assert "unsupported schema version" in proc.stderr


def test_locked_db_refuses_prune_without_modification(db, tmp_path):
    backup = tmp_path / "backup.db"
    before = digest(db)
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        proc = run("prune", db, "--keep-latest", 2, "--backup", backup)
        writer.rollback()
    assert proc.returncode == 2
    assert "locked/busy" in proc.stderr
    assert digest(db) == before
    assert not backup.exists()


@pytest.mark.parametrize("kind,blob", [("mystery", b"abc"), ("msgpack", b"\xc1"), ("pickle", b"cos\nsystem\n(S'false'\ntR.")])
def test_opaque_blobs_counted_not_executed_and_prune_refused(db, kind, blob):
    execute(db, "UPDATE checkpoints SET type=?,checkpoint=? WHERE thread_id='t-1'", (kind, blob))
    proc = run("stats", db, "--json")
    assert proc.returncode == 0
    report = json.loads(proc.stdout)
    assert report["opaque_checkpoints"] == 12
    assert report["opaque_bytes"] == 12 * len(blob)
    assert run("threads", db).returncode == 0
    assert run("diff", db, "--thread", "t-1").returncode == 2
    before = digest(db)
    proc = run("prune", db, "--keep-latest", 2, "--no-backup")
    assert proc.returncode == 2 and "opaque" in proc.stderr
    assert digest(db) == before


def test_json_constructor_and_msgpack_extension_are_never_revived(tmp_path):
    marker = tmp_path / "must-not-exist"
    payload = json.dumps({"lc": 2, "type": "constructor", "id": ["os", "system"], "args": [f"touch {marker}"]}).encode()
    assert decode("json", payload)[1]
    # Fixext1 is rejected even if its extension code could otherwise revive a type.
    assert decode("msgpack", b"\xd4\x00\x00")[1]
    assert not marker.exists()


def test_opaque_pending_writes_are_reported_and_block_prune(db):
    execute(db, "UPDATE writes SET type='unknown' WHERE thread_id='t-1'")
    report = json.loads(run("stats", db, "--json").stdout)
    assert report["opaque_writes"] == 20
    assert report["opaque_bytes"] > 0
    with pytest.raises(ScopeError, match="opaque pending write"):
        prune(db, 2, no_backup=True)


def test_namespaces_inspect_and_diff_but_prune_refuses(db):
    execute(db, "UPDATE checkpoints SET checkpoint_ns='child:x' WHERE thread_id='t-2'")
    execute(db, "UPDATE writes SET checkpoint_ns='child:x' WHERE thread_id='t-2'")
    report = json.loads(run("threads", db, "--json").stdout)
    assert report[1]["checkpoint_ns"] == ["child:x"]
    proc = run("diff", db, "--thread", "t-2", "--namespace", "child:x", "--last", 2, "--json")
    assert proc.returncode == 0 and json.loads(proc.stdout)["changes"]
    before = digest(db)
    with pytest.raises(ScopeError, match="nonempty namespaces"):
        prune(db, 2, no_backup=True)
    assert digest(db) == before
    # An unsupported unselected thread must not block a safe selected thread.
    assert prune(db, 2, thread="t-1", no_backup=True)["verified"]


def test_delta_metadata_blocks_prune(db):
    execute(db, "UPDATE checkpoints SET metadata=? WHERE thread_id='t-1'", (json.dumps({"counters_since_delta_snapshot": {"items": [1, 1]}}).encode(),))
    with pytest.raises(ScopeError, match="DeltaChannel"):
        prune(db, 2, no_backup=True)


def test_orphan_writes_block_prune_but_still_counted(db):
    execute(db, "UPDATE writes SET checkpoint_id='nonexistent' WHERE thread_id='t-1'")
    assert json.loads(run("stats", db, "--json").stdout)["writes"] == 40
    with pytest.raises(ScopeError, match="orphan"):
        prune(db, 2, no_backup=True)


def test_verification_failure_rolls_back(db, monkeypatch):
    import agentwrench.checkpoints.prune as module
    original = module.fingerprints
    calls = 0

    def tamper(cps, writes):
        nonlocal calls
        calls += 1
        latest, raw = original(cps, writes)
        if calls == 3:
            latest.clear()
        return latest, raw

    monkeypatch.setattr(module, "fingerprints", tamper)
    before = digest(db)
    with pytest.raises(ScopeError, match="verification failed"):
        prune(db, 2, no_backup=True)
    assert digest(db) == before
    assert query(db, "SELECT count(*) FROM checkpoints")[0][0] == 24


def test_selection_limits_fail_before_large_allocation(db, monkeypatch):
    monkeypatch.setattr(db_module, "MAX_ROWS", 1)
    with connect(db) as con, pytest.raises(ScopeError, match="safety limits"):
        read_rows(con)


def test_uri_paths_and_sql_parameters_are_literal(tmp_path):
    path = tmp_path / "literal?#.db"
    make_fixture(path, 1, 2)
    assert run("threads", path).returncode == 0
    proc = run("stats", path, "--thread", "t-1' OR 1=1 --")
    assert proc.returncode == 1
    backup = tmp_path / "backup?#.db"
    assert prune(path, 1, backup=backup)["verified"]
    assert query(backup, "SELECT count(*) FROM checkpoints")[0][0] == 6


def test_empty_supported_db(tmp_path):
    path = tmp_path / "empty.db"
    with closing(sqlite3.connect(path)) as con:
        SqliteSaver(con).setup()
    assert json.loads(run("threads", path, "--json").stdout) == []
    result = prune(path, 1, no_backup=True)
    assert result["delete_checkpoints"] == 0
    assert result["verified"] is True


def test_console_entrypoint_and_module(db):
    proc = subprocess.run([str(Path(sys.executable).parent / "agentwrench"), "checkpoints", "threads", str(db), "--json"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert len(json.loads(proc.stdout)) == 2


def test_real_failed_superstep_pending_writes_survive_and_resume(tmp_path):
    from typing import TypedDict
    from langgraph.graph import END, START, StateGraph

    class ParallelState(TypedDict):
        a: int
        b: int

    attempts = {"a": 0, "b": 0}

    def a(state):
        attempts["a"] += 1
        return {"a": state["a"] + 1}

    def b(state):
        attempts["b"] += 1
        if attempts["b"] == 1:
            raise RuntimeError("synthetic retryable failure")
        return {"b": state["b"] + 1}

    path = tmp_path / "pending.db"
    builder = StateGraph(ParallelState)
    builder.add_node("a_node", a)
    builder.add_node("b_node", b)
    for node in ("a_node", "b_node"):
        builder.add_edge(START, node)
        builder.add_edge(node, END)
    config = {"configurable": {"thread_id": "retry"}}
    with closing(sqlite3.connect(path, check_same_thread=False)) as con:
        graph = builder.compile(checkpointer=SqliteSaver(con))
        with pytest.raises(RuntimeError, match="synthetic"):
            graph.invoke({"a": 0, "b": 0}, config)
        before = graph.get_state(config)
    result = prune(path, 1, no_backup=True)
    assert result["verified"]
    assert query(path, "SELECT count(*) FROM writes")[0][0] > 0
    with closing(sqlite3.connect(path, check_same_thread=False)) as con:
        graph = builder.compile(checkpointer=SqliteSaver(con))
        assert graph.get_state(config) == before
        assert graph.invoke(None, config) == {"a": 1, "b": 1}
        assert attempts == {"a": 1, "b": 2}


def test_real_delta_channel_graph_is_refused(tmp_path):
    import operator
    from typing import Annotated, TypedDict
    from langgraph.channels.delta import DeltaChannel
    from langgraph.graph import END, START, StateGraph

    class DeltaState(TypedDict):
        items: Annotated[list[str], DeltaChannel(operator.add)]

    path = tmp_path / "delta.db"
    builder = StateGraph(DeltaState)
    builder.add_node("add", lambda state: {"items": ["synthetic"]})
    builder.add_edge(START, "add")
    builder.add_edge("add", END)
    with closing(sqlite3.connect(path, check_same_thread=False)) as con:
        graph = builder.compile(checkpointer=SqliteSaver(con))
        graph.invoke({"items": []}, {"configurable": {"thread_id": "delta"}})
    before = query(path, "SELECT * FROM checkpoints")
    with pytest.raises(ScopeError, match="DeltaChannel|opaque"):
        prune(path, 1, no_backup=True)
    assert query(path, "SELECT * FROM checkpoints") == before


def test_map_key_order_does_not_create_false_diff_and_deep_data_is_opaque():
    from agentwrench.checkpoints.codec import encoded, state_hash
    assert encoded({"a": 1, "b": [2]}) == encoded({"b": [2], "a": 1})
    assert state_hash({"a": 1}) != state_hash({"a": True})
    assert decode("json", b"[" * 70 + b"0" + b"]" * 70)[1]


def test_timestamp_offsets_are_compared_chronologically(db):
    with closing(sqlite3.connect(db)) as con:
        row = con.execute("SELECT checkpoint_id,type,checkpoint FROM checkpoints WHERE thread_id='t-1' ORDER BY checkpoint_id LIMIT 1").fetchone()
        cp = SERDE.loads_typed(row[1:])
        cp["ts"] = "2026-01-01T01:00:00+02:00"  # 2025-12-31 23:00 UTC
        kind, blob = SERDE.dumps_typed(cp)
        con.execute("UPDATE checkpoints SET type=?,checkpoint=? WHERE thread_id='t-1' AND checkpoint_id=?", (kind, blob, row[0]))
        con.commit()
    report = json.loads(run("threads", db, "--json").stdout)
    assert report[0]["first"] == cp["ts"]


def test_sqlite_like_prefix_cannot_hide_a_trigger(db):
    execute(db, "CREATE TRIGGER sqliteXhidden AFTER DELETE ON checkpoints BEGIN DELETE FROM writes; END")
    before = digest(db)
    proc = run("prune", db, "--keep-latest", 2, "--no-backup")
    assert proc.returncode == 2
    assert "extra tables" in proc.stderr
    assert digest(db) == before


def test_committed_wal_is_backed_up_and_active_wal_writer_blocks_prune(db, tmp_path):
    backup = tmp_path / "wal-backup.db"
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE checkpoints SET metadata=? WHERE thread_id='t-1'", (b'{"source":"update","step":0,"wal_note":"committed"}',))
        writer.commit()
        assert Path(str(db) + "-wal").stat().st_size > 0
        before = query(db, "SELECT * FROM checkpoints ORDER BY thread_id,checkpoint_id")
        assert prune(db, 2, backup=backup)["verified"]
        assert query(backup, "SELECT * FROM checkpoints ORDER BY thread_id,checkpoint_id") == before
        after = query(db, "SELECT * FROM checkpoints ORDER BY thread_id,checkpoint_id")
        writer.execute("BEGIN IMMEDIATE")
        proc = run("prune", db, "--keep-latest", 1, "--no-backup")
        assert proc.returncode == 2 and "locked/busy" in proc.stderr
        writer.rollback()
        assert query(db, "SELECT * FROM checkpoints ORDER BY thread_id,checkpoint_id") == after
