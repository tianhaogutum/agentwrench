"""Read-only drift evidence for v1 saved prune plans, without payload disclosure."""
from contextlib import closing
import json
import shutil
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from agentwrench.checkpoints import db as db_module
from agentwrench.checkpoints.db import KEY, ScopeError, connect
from agentwrench.checkpoints.plan_status import plan_status
from .test_prune_plan import export
from .test_scope import digest, execute, query, run


def identity(row):
    return dict(zip(KEY, row))


def test_status_added_removed_ids_exact_and_read_only(db, tmp_path):
    saved = export(db, tmp_path, thread="t-1")
    removed = query(db, "SELECT thread_id,checkpoint_ns,checkpoint_id FROM checkpoints WHERE thread_id='t-1' ORDER BY checkpoint_id LIMIT 1")[0]
    execute(db, "UPDATE checkpoints SET checkpoint_id='new-checkpoint' WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?", removed)
    before = digest(db), saved.read_bytes()
    proc = run("plan-status", db, "--plan", saved, "--json")
    assert proc.returncode == 3, proc.stderr
    assert not proc.stderr
    assert json.loads(proc.stdout) == {
        "format": "lgckpt-scope.plan-status/v2", "binding_matches": False,
        "path_changed": False, "schema_changed": False, "rows_changed": True,
        "scope": {"thread": "t-1"},
        "added_checkpoints": [identity((*removed[:2], "new-checkpoint"))],
        "removed_checkpoints": [identity(removed)],
        "added_database_checkpoints": [identity((*removed[:2], "new-checkpoint"))],
        "removed_database_checkpoints": [identity(removed)],
        "row_localization": "database", "changed_checkpoints": [], "changed_writes": [],
        "added_writes": [], "removed_writes": [],
    }
    assert (digest(db), saved.read_bytes()) == before


def status(db, saved, code=3):
    before = digest(db), saved.read_bytes()
    proc = run("plan-status", db, "--plan", saved, "--json")
    assert proc.returncode == code, proc.stderr
    assert not proc.stderr
    assert (digest(db), saved.read_bytes()) == before
    return json.loads(proc.stdout)


def test_unchanged_and_edited_plan_are_distinguished(db, tmp_path):
    saved = export(db, tmp_path)
    result = status(db, saved, code=0)
    assert result["binding_matches"]
    assert not any(result[f"{part}_changed"] for part in ("path", "schema", "rows"))
    assert result["added_checkpoints"] == result["removed_checkpoints"] == []
    value = json.loads(saved.read_text())
    value["checkpoints"][0]["action"] = "keep"
    saved.write_text(json.dumps(value))
    before = digest(db), saved.read_bytes()
    proc = run("plan-status", db, "--plan", saved, "--json")
    assert proc.returncode == 2 and not proc.stdout
    assert "checkpoint actions or summary" in proc.stderr
    assert (digest(db), saved.read_bytes()) == before


@pytest.mark.parametrize("change", [
    "UPDATE checkpoints SET metadata=x'7061796c6f61642d736563726574' WHERE thread_id='t-1'",
    "UPDATE checkpoints SET checkpoint=x'7061796c6f61642d736563726574', type='unknown' WHERE thread_id='t-1'",
    "UPDATE checkpoints SET parent_checkpoint_id='payload-secret' WHERE thread_id='t-1'",
    "UPDATE writes SET value=x'7061796c6f61642d736563726574', type='unknown' WHERE thread_id='t-1'",
    "UPDATE writes SET task_path='payload-secret' WHERE thread_id='t-1'",
    "DELETE FROM writes WHERE thread_id='t-1'",
    "UPDATE checkpoints SET metadata=x'7061796c6f61642d736563726574' WHERE thread_id='t-2'",
    "UPDATE checkpoints SET checkpoint_id='unselected-new-id' WHERE thread_id='t-2' AND checkpoint_id=(SELECT min(checkpoint_id) FROM checkpoints WHERE thread_id='t-2')",
])
def test_stable_selected_ids_raw_row_changes_without_values(db, tmp_path, change):
    saved = export(db, tmp_path, thread="t-1")
    execute(db, change)
    result = status(db, saved)
    assert result["rows_changed"] and not result["schema_changed"] and not result["path_changed"]
    assert result["added_checkpoints"] == result["removed_checkpoints"] == []
    assert "payload-secret" not in json.dumps(result)
    human = run("plan-status", db, "--plan", saved)
    assert human.returncode == 3 and not human.stderr
    assert "rows_changed: true" in human.stdout and "payload-secret" not in human.stdout
    backup = tmp_path / "never-created.db"
    applied = run("prune", db, "--apply-plan", saved, "--backup", backup)
    assert applied.returncode == 2 and "stale prune plan" in applied.stderr
    assert not backup.exists()


@pytest.mark.parametrize("change", [
    "PRAGMA user_version=17", "PRAGMA application_id=11", "PRAGMA schema_version=90",
    "CREATE INDEX added_index ON checkpoints(thread_id)",
    "CREATE TABLE unrelated (secret TEXT)",
    "CREATE VIEW unrelated AS SELECT 'payload-secret'",
    "CREATE TRIGGER unrelated AFTER DELETE ON checkpoints BEGIN SELECT 'payload-secret'; END",
])
def test_schema_changes_are_separate_and_do_not_enable_prune(db, tmp_path, change):
    saved = export(db, tmp_path)
    execute(db, change)
    result = status(db, saved)
    assert result["schema_changed"] and not result["rows_changed"]
    assert result["added_checkpoints"] == result["removed_checkpoints"] == []
    assert "payload-secret" not in json.dumps(result)
    backup = tmp_path / "never-created.db"
    proc = run("prune", db, "--apply-plan", saved, "--backup", backup)
    assert proc.returncode == 2 and not backup.exists()


def test_schema_and_rows_can_both_change(db, tmp_path):
    saved = export(db, tmp_path)
    execute(db, "PRAGMA user_version=1")
    execute(db, "DELETE FROM writes")
    result = status(db, saved)
    assert result["rows_changed"] and result["schema_changed"]


@pytest.mark.parametrize("change", [
    "ALTER TABLE writes RENAME COLUMN task_path TO incompatible",
    "ALTER TABLE checkpoints ADD COLUMN unsupported TEXT",
    "DROP TABLE checkpoints", "DROP TABLE writes",
])
def test_incompatible_layout_has_unknown_rows_not_false_or_traceback(db, tmp_path, change):
    saved = export(db, tmp_path)
    execute(db, change)
    result = status(db, saved)
    assert result["schema_changed"] and not result["binding_matches"]
    assert result["rows_changed"] is None
    assert result["added_checkpoints"] is result["removed_checkpoints"] is None
    assert result["rows_unavailable_reason"] == "unsupported checkpoint/write table layout"


def test_replaced_table_view_is_not_queried(db, tmp_path):
    saved = export(db, tmp_path)
    execute(db, "ALTER TABLE checkpoints RENAME TO original")
    execute(db, "CREATE VIEW checkpoints AS SELECT * FROM original")
    result = status(db, saved)
    assert result["rows_changed"] is None and result["schema_changed"]


def test_copy_reports_only_path_change(db, tmp_path):
    saved = export(db, tmp_path)
    copied = tmp_path / "copy.db"
    shutil.copyfile(db, copied)
    result = status(copied, saved)
    assert result["path_changed"] and not result["schema_changed"] and not result["rows_changed"]
    assert result["added_checkpoints"] == result["removed_checkpoints"] == []


def test_deleted_selected_thread_lists_all_removed_ids(db, tmp_path):
    saved = export(db, tmp_path, thread="t-1")
    previous = [{f: row[f] for f in KEY} for row in json.loads(saved.read_text())["checkpoints"]]
    execute(db, "DELETE FROM checkpoints WHERE thread_id='t-1'")
    result = status(db, saved)
    assert result["added_checkpoints"] == [] and result["removed_checkpoints"] == previous


def test_full_scope_identity_includes_namespace_and_escapes_controls(db, tmp_path):
    saved = export(db, tmp_path)
    previous = query(db, "SELECT thread_id,checkpoint_ns,checkpoint_id FROM checkpoints ORDER BY thread_id,checkpoint_id LIMIT 1")[0]
    unsafe = 'ns\x1b\n\u202e'
    execute(db, "UPDATE checkpoints SET checkpoint_ns=? WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?", (unsafe, *previous))
    result = status(db, saved)
    assert result["scope"] == {"thread": None}
    assert result["removed_checkpoints"] == [identity(previous)]
    assert result["added_checkpoints"] == [identity((previous[0], unsafe, previous[2]))]
    human = run("plan-status", db, "--plan", saved)
    assert human.returncode == 3 and not human.stderr
    assert unsafe not in human.stdout and '\\u001b\\n\\u202e' in human.stdout


def test_diagnostics_include_committed_wal_without_writer_lock(db, tmp_path):
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        saved = export(db, tmp_path)
        writer.execute("UPDATE writes SET task_path='committed-wal-change'")
        writer.commit()
        before = query(db, "SELECT * FROM writes"), saved.read_bytes()
        writer.execute("BEGIN IMMEDIATE")
        result = status(db, saved)
        assert result["rows_changed"] and not result["schema_changed"]
        assert (query(db, "SELECT * FROM writes"), saved.read_bytes()) == before


@pytest.mark.parametrize("limit,value,error", [
    ("MAX_ROWS", 1, "selection exceeds"),
    ("MAX_PAYLOAD_BYTES", 1, "selection exceeds"),
    ("MAX_BLOB_BYTES", 1, "row exceeds"),
])
def test_whole_database_limits_even_for_filtered_plan(db, tmp_path, monkeypatch, limit, value, error):
    saved = export(db, tmp_path, thread="t-1")
    monkeypatch.setattr(db_module, limit, value)
    before = digest(db), saved.read_bytes()
    with pytest.raises(ScopeError, match=error):
        plan_status(db, saved)
    assert (digest(db), saved.read_bytes()) == before


@pytest.mark.parametrize("content", ['{"format":0,"format":1}', '{"format":NaN}', '[' * 1100, '\udcff'])
def test_malformed_plan_has_no_report(db, tmp_path, content):
    saved = tmp_path / "invalid.json"
    saved.write_bytes(content.encode("utf-8", errors="surrogateescape"))
    before = digest(db), saved.read_bytes()
    proc = run("plan-status", db, "--plan", saved, "--json")
    assert proc.returncode == 2 and not proc.stdout and "invalid prune plan JSON" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert (digest(db), saved.read_bytes()) == before


@pytest.mark.parametrize("extra", [
    ("--backup", "must-not-create.db"), ("--no-backup",), ("--vacuum",),
    ("--keep-latest", "1"), ("--thread", "t-2"), ("--export-plan", "must-not-create.json"),
])
def test_diagnostic_rejects_execution_or_scope_flags(db, tmp_path, extra):
    saved = export(db, tmp_path)
    before = digest(db), saved.read_bytes()
    proc = run("plan-status", db, "--plan", saved, *extra)
    assert proc.returncode == 2 and not proc.stdout
    assert (digest(db), saved.read_bytes()) == before


def test_empty_database_and_missing_or_invalid_inputs(tmp_path):
    db = tmp_path / "empty.db"
    with closing(sqlite3.connect(db)) as con:
        SqliteSaver(con).setup()
    saved = export(db, tmp_path)
    assert status(db, saved, code=0)["added_checkpoints"] == []
    missing = tmp_path / "missing.db"
    proc = run("plan-status", missing, "--plan", saved, "--json")
    assert proc.returncode == 2 and not proc.stdout and not missing.exists()
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"payload-secret")
    proc = run("plan-status", broken, "--plan", saved, "--json")
    assert proc.returncode == 2 and not proc.stdout and "payload-secret" not in proc.stderr
    proc = run("plan-status", db, "--plan", missing, "--json")
    assert proc.returncode == 2 and not proc.stdout and not missing.exists()


def test_schema_validation_bypass_is_read_only(db):
    with pytest.raises(ScopeError, match="writable connections require schema validation"):
        with connect(db, writable=True, validate_schema=False):
            pytest.fail("unsafe writer opened")
    with connect(db, validate_schema=False) as con:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM checkpoints")


def test_broken_replacement_view_does_not_hide_schema_evidence(db, tmp_path):
    saved = export(db, tmp_path)
    execute(db, "DROP TABLE checkpoints")
    execute(db, "CREATE VIEW checkpoints AS SELECT * FROM missing_table")
    result = status(db, saved)
    assert result["schema_changed"] and result["rows_changed"] is None


def test_invalid_raw_keys_and_oversized_plan_have_no_report(db, tmp_path, monkeypatch):
    from agentwrench.checkpoints import prune_plan
    saved = export(db, tmp_path)
    monkeypatch.setattr(prune_plan, "MAX_PLAN_BYTES", 16)
    with pytest.raises(ScopeError, match="input limit"):
        plan_status(db, saved)
    execute(db, "UPDATE checkpoints SET checkpoint_id=x'00' WHERE checkpoint_id=(SELECT min(checkpoint_id) FROM checkpoints) AND thread_id='t-1'")
    before = digest(db), saved.read_bytes()
    proc = run("plan-status", db, "--plan", saved, "--json")
    assert proc.returncode == 2 and not proc.stdout and "checkpoint keys must be text" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert (digest(db), saved.read_bytes()) == before
