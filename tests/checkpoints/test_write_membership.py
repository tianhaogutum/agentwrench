"""Exact v2 pending-write membership evidence; legacy/unknown is never empty."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

import pytest

from agentwrench.checkpoints.plan_status import plan_status
from agentwrench.checkpoints.prune import prune
from agentwrench.checkpoints.prune_plan import WRITE_KEY
from .test_row_localization import legacy, small_database
from .test_scope import execute, run


def write_id(*identity):
    return dict(zip(WRITE_KEY, identity))


def inputs(folder):
    # SQLite may create empty WAL/SHM sidecars when reading a closed WAL DB.
    # Compare all plan/main bytes and every nonempty (committed) WAL byte.
    return {p.name: p.read_bytes() for p in folder.iterdir()
            if p.is_file() and not p.name.endswith('-shm')
            and not (p.name.endswith('-wal') and p.stat().st_size == 0)}


# The refused `prune --apply-plan` below must leave the WAL database byte-identical. With
# SQLite < 3.51.0 its connection checkpoints the committed WAL into the main file and removes
# the WAL on close even though this test's writer connection is still open, so the raw bytes
# change (contents are equal). Verified: fails on 3.46.1-3.50.4, passes on 3.51.0+.
@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 51, 0),
                    reason=f"needs SQLite >= 3.51.0 (have {sqlite3.sqlite_version}): older SQLite checkpoints "
                           "and removes the WAL when the refused prune connection closes, changing raw DB bytes")
def test_stable_checkpoint_write_membership_wal_and_legacy(tmp_path, monkeypatch):
    db = small_database(tmp_path / 'history.db')
    execute(db, 'INSERT INTO writes SELECT thread_id,checkpoint_ns,checkpoint_id,task_id,task_path,-1,channel,type,value FROM writes')
    saved, old = tmp_path / 'v2.json', tmp_path / 'v1.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    old.write_text(json.dumps(legacy(json.loads(saved.read_text()))))

    def forbidden(*args, **kwargs):
        pytest.fail('stale membership diagnosis must not decode or plan')

    monkeypatch.setattr('agentwrench.checkpoints.plan_status.plan', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.codec.SERDE.loads_typed', forbidden)
    with closing(sqlite3.connect(db)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('DELETE FROM writes WHERE idx=-1')
        writer.execute("INSERT INTO writes SELECT thread_id,checkpoint_ns,checkpoint_id,'new-task',task_path,2,channel,type,value FROM writes")
        writer.execute("UPDATE writes SET value=?,type='unknown' WHERE idx=0", (b'private-write',))
        writer.commit()
        assert Path(str(db) + '-wal').stat().st_size > 0
        writer.execute('BEGIN IMMEDIATE')
        before = inputs(tmp_path)
        expected = dict(format='lgckpt-scope.plan-status/v2', binding_matches=False,
                        path_changed=False, schema_changed=False, rows_changed=True,
                        scope={'thread': None}, added_checkpoints=[], removed_checkpoints=[],
                        added_database_checkpoints=[], removed_database_checkpoints=[],
                        row_localization='database', changed_checkpoints=[],
                        changed_writes=[write_id('t', '', 'c2', 'task', 0)],
                        added_writes=[write_id('t', '', 'c2', 'new-task', 2)],
                        removed_writes=[write_id('t', '', 'c2', 'task', -1)])
        assert plan_status(db, saved) == expected
        for plan, version in ((saved, 'v2'), (old, 'v1')):
            proc = run('plan-status', db, '--plan', plan, '--json')
            assert proc.returncode == 3 and not proc.stderr
            if version == 'v1':
                expected.update(format='lgckpt-scope.plan-status/v1',
                                row_localization='unavailable-v1', changed_checkpoints=None,
                                changed_writes=None, added_writes=None, removed_writes=None,
                                added_database_checkpoints=None, removed_database_checkpoints=None)
            assert json.loads(proc.stdout) == expected
            assert 'private-write' not in proc.stdout and not re.search(r'[0-9a-f]{64}', proc.stdout)
            human = run('plan-status', db, '--plan', plan)
            assert human.returncode == 3 and not human.stderr
            assert 'private-write' not in human.stdout and 'sha256' not in human.stdout
            if version == 'v1':
                assert 'write membership localization: unavailable (v1 plan)' in human.stdout
                assert 'added writes: unavailable' in human.stdout
                assert 'removed writes: unavailable' in human.stdout
            else:
                assert 'added writes: 1' in human.stdout and 'removed writes: 1' in human.stdout
            assert inputs(tmp_path) == before
        writer.rollback()
        for plan in (saved, old):
            refused = run('prune', db, '--apply-plan', plan, '--backup', tmp_path / 'never.bak', '--json')
            assert refused.returncode == 2 and not refused.stdout and 'stale prune plan' in refused.stderr
            assert inputs(tmp_path) == before and not (tmp_path / 'never.bak').exists()


@pytest.mark.parametrize('field,new', [('thread_id', 'other'), ('checkpoint_ns', 'ns'),
                                      ('checkpoint_id', 'c1'), ('task_id', 'other'), ('idx', -1)])
def test_each_write_identity_component_is_membership_not_content(tmp_path, field, new):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'v2.json'
    prune(db, 1, thread='t', dry_run=True, export_plan=saved)
    execute(db, f'UPDATE writes SET {field}=?', (new,))
    before = inputs(tmp_path)
    result = plan_status(db, saved)
    removed = write_id('t', '', 'c2', 'task', 0)
    assert result['removed_writes'] == [removed]
    assert result['added_writes'] == [{**removed, field: new}]
    assert result['changed_writes'] == []
    assert result['added_checkpoints'] == result['removed_checkpoints'] == []
    assert result['rows_changed'] and not result['schema_changed']
    assert inputs(tmp_path) == before


def test_global_literal_keys_numeric_order_disjoint_and_escaped(tmp_path):
    db = small_database(tmp_path / 'history.db')
    removed = [('t', '', 'c1', 'task', -2), ('u', 'ns', 'c2', 'task', 0),
               ('u', '', 'c2', 'task', 0), ('u', 'ns', 'c2', '', -(2**63))]
    with closing(sqlite3.connect(db)) as con:
        # Unselected/namespace writes can be orphaned; raw diagnosis still works.
        con.executemany('INSERT INTO writes (thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value,task_path) VALUES (?,?,?,?,?,?,?,?,?)',
                        [(*k, 'x', 'json', b'1', '') for k in removed])
        con.commit()
    saved = tmp_path / 'v2.json'
    prune(db, 1, thread='t', dry_run=True, export_plan=saved)
    added = [('u', 'ns', 'c2', 'unsafe\x1b\n\u202e', 2**63-1),
             ('u', 'ns', 'c2', '*', 10), ('u', 'ns', 'c2', '*', -2),
             ('t', '', 'c1', 'task', 1), ('u', 'NS', 'c2', '*', 2)]
    with closing(sqlite3.connect(db)) as con:
        con.executemany('DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=? AND task_id=? AND idx=?', removed)
        con.executemany('INSERT INTO writes (thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value,task_path) VALUES (?,?,?,?,?,?,?,?,?)',
                        [(*k, 'private-channel', 'unknown', b'private-value', 'private-path') for k in added])
        con.execute("UPDATE writes SET task_path='private-path' WHERE task_id='task' AND checkpoint_id='c2'")
        con.commit()
    before = inputs(tmp_path)
    result = plan_status(db, saved)
    assert result['scope'] == {'thread': 't'} and result['row_localization'] == 'database'
    assert result['added_writes'] == [write_id(*k) for k in sorted(added)]
    assert result['removed_writes'] == [write_id(*k) for k in sorted(removed)]
    assert result['changed_writes'] == [write_id('t', '', 'c2', 'task', 0)]
    human = run('plan-status', db, '--plan', saved)
    assert human.returncode == 3 and not human.stderr
    assert '\\u001b\\n\\u202e' in human.stdout and '\x1b' not in human.stdout
    assert 'private' not in human.stdout and 'private' not in json.dumps(result)
    assert inputs(tmp_path) == before


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_empty_schema_only_and_unknown_write_membership(tmp_path, version):
    db = small_database(tmp_path / 'history.db')
    execute(db, 'DELETE FROM writes')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    if version == 'v1':
        saved.write_text(json.dumps(legacy(json.loads(saved.read_text()))))
    for schema_only in (False, True):
        if schema_only:
            execute(db, 'PRAGMA user_version=19')
        before = inputs(tmp_path)
        result = plan_status(db, saved)
        assert result['schema_changed'] == schema_only and not result['rows_changed']
        assert result['added_writes'] == result['removed_writes'] == ([] if version == 'v2' else None)
        assert inputs(tmp_path) == before
    execute(db, 'ALTER TABLE writes RENAME COLUMN value TO unsupported')
    before = inputs(tmp_path)
    result = plan_status(db, saved)
    assert result['row_localization'] == 'unavailable-layout'
    assert result['added_writes'] is result['removed_writes'] is result['changed_writes'] is None
    assert result['rows_changed'] is None and result['schema_changed']
    assert inputs(tmp_path) == before
