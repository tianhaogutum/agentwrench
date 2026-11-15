"""Saved plans are reviewed offline and applied under the existing writer lock."""
from contextlib import closing
import json
import shutil
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from .make_fixture import build_graph
from agentwrench.checkpoints.db import ScopeError
from agentwrench.checkpoints.prune import prune
from agentwrench.checkpoints import prune_plan
from .test_scope import digest, execute, query, run


def test_export_review_and_apply_exact_ids_with_backup(db, tmp_path):
    artifact = tmp_path / "plan.json"
    backup = tmp_path / "before.db"
    unselected = query(db, "SELECT * FROM checkpoints WHERE thread_id='t-2'")
    expected_ids = query(db, "SELECT checkpoint_id FROM checkpoints WHERE thread_id='t-1' ORDER BY checkpoint_id DESC LIMIT 2")
    config = {"configurable": {"thread_id": "t-1"}}
    with closing(sqlite3.connect(db, check_same_thread=False)) as con:
        before_state = build_graph(SqliteSaver(con)).get_state(config)
    initial_hash = digest(db)
    exported = run("prune", db, "--keep-latest", 2, "--thread", "t-1",
                   "--dry-run", "--export-plan", artifact, "--json")
    assert exported.returncode == 0, exported.stderr
    saved = json.loads(artifact.read_text())
    assert saved["format"] == "lgckpt-scope.prune/v2"
    assert saved["summary"]["delete_checkpoints"] == 10
    assert len(saved["checkpoints"]) == 12
    assert {r["checkpoint_id"] for r in saved["checkpoints"] if r["action"] == "keep"} == {r[0] for r in expected_ids}
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert digest(db) == initial_hash
    review = run("prune", db, "--apply-plan", artifact, "--dry-run", "--backup", backup, "--vacuum", "--json")
    assert review.returncode == 0, review.stderr
    assert json.loads(review.stdout)["delete_checkpoints"] == 10
    assert digest(db) == initial_hash and not backup.exists()
    missing_choice = run("prune", db, "--apply-plan", artifact)
    assert missing_choice.returncode == 2 and "requires --backup" in missing_choice.stderr
    applied = run("prune", db, "--apply-plan", artifact, "--backup", backup, "--json")
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["verified"]
    assert query(backup, "SELECT count(*) FROM checkpoints")[0][0] == 24
    assert query(db, "SELECT checkpoint_id FROM checkpoints WHERE thread_id='t-1' ORDER BY checkpoint_id DESC") == expected_ids
    assert query(db, "SELECT * FROM checkpoints WHERE thread_id='t-2'") == unselected
    replay = run("prune", db, "--apply-plan", artifact, "--no-backup")
    assert replay.returncode == 2 and "stale prune plan" in replay.stderr
    with closing(sqlite3.connect(db, check_same_thread=False)) as con:
        graph = build_graph(SqliteSaver(con))
        assert graph.get_state(config) == before_state
        assert graph.invoke({"items": []}, config)["counter"] == 5


def export(db, tmp_path, **options):
    destination = tmp_path / "plan.json"
    prune(db, 2, dry_run=True, export_plan=destination, **options)
    return destination


@pytest.mark.parametrize("change", [
    "UPDATE checkpoints SET metadata=x'7b7d' WHERE thread_id='t-1'",
    "UPDATE checkpoints SET parent_checkpoint_id='changed' WHERE thread_id='t-1'",
    "UPDATE checkpoints SET checkpoint_id='changed' WHERE checkpoint_id=(SELECT min(checkpoint_id) FROM checkpoints)",
    "DELETE FROM checkpoints WHERE checkpoint_id=(SELECT min(checkpoint_id) FROM checkpoints)",
    "UPDATE writes SET value=x'00' WHERE thread_id='t-1'",
    "UPDATE writes SET task_path='changed' WHERE thread_id='t-1'",
    "DELETE FROM writes WHERE thread_id='t-1'",
    "UPDATE checkpoints SET metadata=x'7b7d' WHERE thread_id='t-2'",
    "PRAGMA user_version=1",
    "CREATE INDEX new_index ON checkpoints(thread_id)",
])
def test_stale_rows_schema_and_unselected_changes_refuse_before_backup(db, tmp_path, change):
    saved = export(db, tmp_path, thread="t-1")
    execute(db, change)
    before = digest(db)
    backup = tmp_path / "must-not-exist.db"
    proc = run("prune", db, "--apply-plan", saved, "--backup", backup, "--json")
    assert proc.returncode == 2 and "error:" in proc.stderr
    assert not proc.stdout and "Traceback" not in proc.stderr
    assert "stale prune plan" in proc.stderr or "unsupported schema" in proc.stderr
    assert digest(db) == before and not backup.exists()


def test_real_new_checkpoint_stales_plan(db, tmp_path):
    saved = export(db, tmp_path)
    with closing(sqlite3.connect(db, check_same_thread=False)) as con:
        build_graph(SqliteSaver(con)).invoke({"items": []}, {"configurable": {"thread_id": "t-1"}})
    before = digest(db)
    proc = run("prune", db, "--apply-plan", saved, "--no-backup")
    assert proc.returncode == 2 and "stale prune plan" in proc.stderr
    assert digest(db) == before


def test_database_copy_at_another_path_is_not_the_saved_target(db, tmp_path):
    saved = export(db, tmp_path)
    copied = tmp_path / "copy.db"
    shutil.copyfile(db, copied)
    before = digest(copied)
    proc = run("prune", copied, "--apply-plan", saved, "--no-backup")
    assert proc.returncode == 2 and "stale prune plan" in proc.stderr
    assert digest(copied) == before


@pytest.mark.parametrize("kind", ["action", "summary", "version", "missing", "duplicate-id", "bool-keep", "extra-options"])
def test_plan_edits_are_validated_and_never_execute(db, tmp_path, kind):
    saved = export(db, tmp_path)
    value = json.loads(saved.read_text())
    if kind == "action":
        value["checkpoints"][0]["action"] = "keep"
    elif kind == "summary":
        value["summary"]["delete_writes"] += 1
    elif kind == "version":
        value["format"] = "lgckpt-scope.prune/v99"
    elif kind == "missing":
        del value["checkpoints"]
    elif kind == "duplicate-id":
        value["checkpoints"].append(value["checkpoints"][0])
    elif kind == "bool-keep":
        value["keep_latest"] = True
    else:
        value["no_backup"] = True
    saved.write_text(json.dumps(value))
    before = digest(db)
    backup = tmp_path / "must-not-exist.db"
    proc = run("prune", db, "--apply-plan", saved, "--backup", backup)
    assert proc.returncode == 2 and "invalid prune plan" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert digest(db) == before and not backup.exists()


@pytest.mark.parametrize("content", ['{"format":0,"format":1}', '{"format":NaN}', '[' * 1100, '\udcff'])
def test_malformed_saved_json_is_bounded_and_reported(db, tmp_path, content):
    saved = tmp_path / "invalid.json"
    saved.write_bytes(content.encode("utf-8", errors="surrogateescape"))
    before = digest(db)
    proc = run("prune", db, "--apply-plan", saved, "--no-backup")
    assert proc.returncode == 2 and "invalid prune plan JSON" in proc.stderr
    assert "Traceback" not in proc.stderr and digest(db) == before


def test_plan_io_limits_before_writing_or_loading(db, tmp_path, monkeypatch):
    saved = export(db, tmp_path)
    monkeypatch.setattr(prune_plan, "MAX_PLAN_BYTES", 16)
    before = digest(db)
    with pytest.raises(ScopeError, match="input limit"):
        prune(db, apply_plan=saved, no_backup=True)
    output = tmp_path / "too-large.json"
    with pytest.raises(ScopeError, match="exceeds 64 MiB"):
        prune(db, 2, dry_run=True, export_plan=output)
    assert not output.exists() and digest(db) == before


def test_export_requires_dry_run_and_safe_new_destination(db, tmp_path):
    saved = tmp_path / "plan.json"
    before = digest(db)
    proc = run("prune", db, "--keep-latest", 2, "--export-plan", saved, "--no-backup")
    assert proc.returncode == 2 and "requires --dry-run" in proc.stderr
    assert not saved.exists()
    for destination in (db, *(str(db) + suffix for suffix in ("-wal", "-shm", "-journal"))):
        with pytest.raises(ScopeError, match="sidecars"):
            prune(db, 2, dry_run=True, export_plan=destination)
    export(db, tmp_path)
    intact = saved.read_bytes()
    with pytest.raises(FileExistsError):
        prune(db, 2, dry_run=True, export_plan=saved)
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        prune(db, 2, dry_run=True, export_plan=symlink)
    assert saved.read_bytes() == intact and digest(db) == before


def test_apply_uses_saved_scope_without_cli_override(db, tmp_path):
    saved = export(db, tmp_path)
    before = digest(db)
    for extra in (("--thread", "t-1"), ("--keep-latest", 1), ("--export-plan", tmp_path / "other.json")):
        proc = run("prune", db, "--apply-plan", saved, "--no-backup", *extra)
        assert proc.returncode == 2 and not proc.stdout
    assert digest(db) == before


def test_saved_plan_checks_wal_and_backup_contains_committed_rows(db, tmp_path):
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE checkpoints SET metadata=x'7b7d'")
        writer.commit()
        saved = export(db, tmp_path)
        # A second connection is still holding the WAL open. Backup must include it.
        original = query(db, "SELECT * FROM checkpoints ORDER BY thread_id,checkpoint_id")
        backup = tmp_path / "wal-backup.db"
        assert prune(db, apply_plan=saved, backup=backup)["verified"]
        assert query(backup, "SELECT * FROM checkpoints ORDER BY thread_id,checkpoint_id") == original


def test_wal_pending_write_change_stales_plan(db, tmp_path):
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        saved = export(db, tmp_path)
        writer.execute("UPDATE writes SET task_path='committed in wal'")
        writer.commit()
        before = query(db, "SELECT * FROM checkpoints")
        with pytest.raises(ScopeError, match="stale prune plan"):
            prune(db, apply_plan=saved, no_backup=True)
        assert query(db, "SELECT * FROM checkpoints") == before


def test_application_holds_writer_lock_during_revalidation(db, tmp_path, monkeypatch):
    saved = export(db, tmp_path)
    original = prune_plan.database_binding
    locked = []

    def binding(*args):
        with closing(sqlite3.connect(db, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
            locked.append(True)
        return original(*args)

    monkeypatch.setattr("agentwrench.checkpoints.prune.database_binding", binding)
    assert prune(db, apply_plan=saved, no_backup=True)["verified"]
    assert locked == [True]


def test_failed_verification_rolls_back_and_saved_plan_can_be_retried(db, tmp_path, monkeypatch):
    import agentwrench.checkpoints.prune as module
    saved = export(db, tmp_path)
    original = module.fingerprints
    calls = 0

    def fail(cps, writes):
        nonlocal calls
        calls += 1
        latest, raw = original(cps, writes)
        if calls == 3:
            latest.clear()
        return latest, raw

    before = digest(db)
    monkeypatch.setattr(module, "fingerprints", fail)
    with pytest.raises(ScopeError, match="verification failed"):
        prune(db, apply_plan=saved, no_backup=True)
    assert digest(db) == before
    monkeypatch.setattr(module, "fingerprints", original)
    assert prune(db, apply_plan=saved, no_backup=True)["verified"]


@pytest.mark.parametrize("keep", [2, 99])
def test_empty_or_noop_saved_plan_can_apply_without_deleting_rows(tmp_path, keep):
    db = tmp_path / "empty.db"
    with closing(sqlite3.connect(db)) as con:
        SqliteSaver(con).setup()
    saved = tmp_path / "empty.json"
    prune(db, keep, dry_run=True, export_plan=saved)
    result = prune(db, apply_plan=saved, no_backup=True)
    assert result["verified"] and result["delete_checkpoints"] == 0
    assert prune(db, apply_plan=saved, no_backup=True)["verified"]


def test_export_does_not_create_artifact_for_unsafe_selected_payload(db, tmp_path):
    execute(db, "UPDATE checkpoints SET type='unknown' WHERE thread_id='t-1'")
    destination = tmp_path / "unsafe.json"
    before = digest(db)
    with pytest.raises(ScopeError, match="opaque"):
        prune(db, 2, dry_run=True, thread="t-1", export_plan=destination)
    assert not destination.exists() and digest(db) == before


def test_opaque_unselected_rows_are_bound_without_blocking_selected_prune(db, tmp_path):
    execute(db, "UPDATE checkpoints SET type='unknown' WHERE thread_id='t-2'")
    before = query(db, "SELECT * FROM checkpoints WHERE thread_id='t-2'")
    saved = export(db, tmp_path, thread="t-1")
    assert prune(db, apply_plan=saved, no_backup=True)["verified"]
    assert query(db, "SELECT * FROM checkpoints WHERE thread_id='t-2'") == before
