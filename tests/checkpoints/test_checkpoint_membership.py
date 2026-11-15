"""Whole-database checkpoint membership without widening scoped evidence."""
from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import sqlite3

import pytest

from agentwrench.checkpoints.db import KEY
from agentwrench.checkpoints.plan_status import plan_status
from agentwrench.checkpoints.prune import prune
from .test_row_localization import legacy, small_database
from .test_scope import execute, run
from .test_write_membership import inputs


def checkpoint_id(*identity):
    return dict(zip(KEY, identity))


def database(path, *, namespaced=True):
    db = small_database(path)
    with closing(sqlite3.connect(db)) as con:
        con.execute("UPDATE checkpoints SET thread_id='t-1'")
        con.execute("UPDATE writes SET thread_id='t-1'")
        for ns in (('', 'ns') if namespaced else ('',)):
            con.execute("INSERT INTO checkpoints SELECT 't-2',?,checkpoint_id,parent_checkpoint_id,type,checkpoint,metadata FROM checkpoints WHERE thread_id='t-1'", (ns,))
        con.commit()
    return db


# The refused `prune --apply-plan` below must leave the WAL database byte-identical. With
# SQLite < 3.51.0 its connection checkpoints the committed WAL into the main file and removes
# the WAL on close even though this test's writer connection is still open, so the raw bytes
# change (contents are equal). Verified: fails on 3.46.1-3.50.4, passes on 3.51.0+.
@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 51, 0),
                    reason=f"needs SQLite >= 3.51.0 (have {sqlite3.sqlite_version}): older SQLite checkpoints "
                           "and removes the WAL when the refused prune connection closes, changing raw DB bytes")
def test_unselected_checkpoint_membership_wal_and_legacy(tmp_path, monkeypatch):
    db = database(tmp_path / 'history.db')
    saved, old = tmp_path / 'v2.json', tmp_path / 'v1.json'
    prune(db, 1, thread='t-1', dry_run=True, export_plan=saved)
    old.write_text(json.dumps(legacy(json.loads(saved.read_text()))))

    def forbidden(*args, **kwargs):
        pytest.fail('stale membership diagnosis must not decode or plan')

    monkeypatch.setattr('agentwrench.checkpoints.plan_status.plan', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.codec.SERDE.loads_typed', forbidden)
    with closing(sqlite3.connect(db)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE checkpoints SET checkpoint_id='c3',metadata=? WHERE thread_id='t-2' AND checkpoint_id='c1'", (b'private-removed-content',))
        writer.execute("UPDATE checkpoints SET metadata=? WHERE thread_id='t-2' AND checkpoint_ns='' AND checkpoint_id='c2'", (b'private-same-key-content',))
        writer.commit()
        assert Path(str(db) + '-wal').stat().st_size > 0
        writer.execute('BEGIN IMMEDIATE')
        # Uncommitted selected changes must not leak into the read snapshot.
        writer.execute("UPDATE checkpoints SET checkpoint_id='uncommitted' WHERE thread_id='t-1' AND checkpoint_id='c1'")
        before = inputs(tmp_path)
        for plan, version in ((saved, 'v2'), (old, 'v1')):
            result = plan_status(db, plan)
            assert result['added_database_checkpoints'] == ([checkpoint_id('t-2', ns, 'c3') for ns in ('', 'ns')] if version == 'v2' else None)
            assert result['removed_database_checkpoints'] == ([checkpoint_id('t-2', ns, 'c1') for ns in ('', 'ns')] if version == 'v2' else None)
            assert result['changed_checkpoints'] == ([checkpoint_id('t-2', '', 'c2')] if version == 'v2' else None)
            assert result['added_checkpoints'] == result['removed_checkpoints'] == []
            assert result['scope'] == {'thread': 't-1'}
            assert result['row_localization'] == ('database' if version == 'v2' else 'unavailable-v1')
            assert result['format'] == f'lgckpt-scope.plan-status/{version}'
            assert result['rows_changed'] and not result['schema_changed'] and not result['path_changed']
            proc = run('plan-status', db, '--plan', plan, '--json')
            assert proc.returncode == 3 and not proc.stderr
            assert json.loads(proc.stdout) == result
            human = run('plan-status', db, '--plan', plan)
            assert human.returncode == 3 and not human.stderr
            if version == 'v1':
                assert 'database checkpoint membership localization: unavailable (v1 plan)' in human.stdout
                assert 'added database checkpoints: unavailable' in human.stdout
                assert 'removed database checkpoints: unavailable' in human.stdout
            else:
                assert 'database checkpoint membership localization: whole database' in human.stdout
                assert 'added database checkpoints: 2' in human.stdout
                assert 'removed database checkpoints: 2' in human.stdout
            for output in (proc.stdout, human.stdout):
                assert 'private-' not in output and 'uncommitted' not in output
                assert 'sha256' not in output and str(db) not in output
                assert not re.search(r'[0-9a-f]{64}', output)
            assert inputs(tmp_path) == before
        writer.rollback()
        for plan in (saved, old):
            refused = run('prune', db, '--apply-plan', plan, '--backup', tmp_path / 'never.bak', '--json')
            assert refused.returncode == 2 and not refused.stdout and 'stale prune plan' in refused.stderr
            assert inputs(tmp_path) == before and not (tmp_path / 'never.bak').exists()


@pytest.mark.parametrize('thread', [None, 't-1'])
@pytest.mark.parametrize('field,new', [('thread_id', 't-3'), ('checkpoint_ns', 'new-ns'), ('checkpoint_id', 'c3')])
def test_each_checkpoint_component_preserves_scoped_compatibility(tmp_path, thread, field, new):
    db = database(tmp_path / 'history.db', namespaced=thread is not None)
    saved = tmp_path / 'v2.json'
    prune(db, 1, thread=thread, dry_run=True, export_plan=saved)
    execute(db, f"UPDATE checkpoints SET {field}=? WHERE thread_id='t-2' AND checkpoint_ns='' AND checkpoint_id='c1'", (new,))
    before = inputs(tmp_path)
    result = plan_status(db, saved)
    removed = checkpoint_id('t-2', '', 'c1')
    assert result['removed_database_checkpoints'] == [removed]
    assert result['added_database_checkpoints'] == [{**removed, field: new}]
    assert result['changed_checkpoints'] == []
    for kind in ('added', 'removed'):
        assert result[f'{kind}_checkpoints'] == (result[f'{kind}_database_checkpoints'] if thread is None else [])
    assert inputs(tmp_path) == before


def test_literal_keys_sort_escaping_and_deleted_selected_scope(tmp_path):
    db = database(tmp_path / 'history.db')
    saved = tmp_path / 'v2.json'
    prune(db, 1, thread='t-1', dry_run=True, export_plan=saved)
    added = [('a', 'bc', 'd'), ('ab', 'c', 'd'), ('t-2', 'NS', 'c1'),
             ('t-2', '', ''), ('t-2', 'ns\x00\x1b\n\u202e', 'c1')]
    with closing(sqlite3.connect(db)) as con:
        con.execute("DELETE FROM checkpoints WHERE thread_id='t-1'")
        con.executemany('INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)',
                        [(*k, 'private-parent', 'unknown', b'private-payload', b'private-metadata') for k in added])
        con.commit()
    # Saved raw array order must not affect deterministic output on stale input.
    value = json.loads(saved.read_text())
    value['raw_rows']['checkpoints'].reverse()
    saved.write_text(json.dumps(value))
    before = inputs(tmp_path)
    result = plan_status(db, saved)
    removed = [checkpoint_id('t-1', '', cid) for cid in ('c1', 'c2')]
    assert result['removed_checkpoints'] == result['removed_database_checkpoints'] == removed
    assert result['added_checkpoints'] == []
    assert result['added_database_checkpoints'] == [checkpoint_id(*k) for k in sorted(added)]
    assert result['changed_checkpoints'] == []
    human = run('plan-status', db, '--plan', saved)
    assert human.returncode == 3 and not human.stderr
    assert '\\u0000\\u001b\\n\\u202e' in human.stdout and '\x1b' not in human.stdout
    assert 'private-' not in human.stdout and 'private-' not in json.dumps(result)
    assert inputs(tmp_path) == before


@pytest.mark.parametrize('version', ['v1', 'v2'])
@pytest.mark.parametrize('empty', [False, True])
def test_unchanged_schema_copy_and_unknown_membership(tmp_path, version, empty):
    db = database(tmp_path / 'history.db', namespaced=False)
    if empty:
        execute(db, 'DELETE FROM checkpoints')
        execute(db, 'DELETE FROM writes')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    if version == 'v1':
        saved.write_text(json.dumps(legacy(json.loads(saved.read_text()))))
    for change in ('unchanged', 'copy', 'schema', 'checkpoint-layout', 'write-layout'):
        target = db
        if change == 'copy':
            target = tmp_path / 'copy.db'
            shutil.copyfile(db, target)
        elif change == 'schema':
            execute(db, 'PRAGMA user_version=19')
        elif change == 'checkpoint-layout':
            execute(db, 'ALTER TABLE checkpoints RENAME COLUMN metadata TO incompatible')
        elif change == 'write-layout':
            execute(db, 'ALTER TABLE checkpoints RENAME COLUMN incompatible TO metadata')
            execute(db, 'ALTER TABLE writes RENAME COLUMN value TO incompatible')
        before = inputs(tmp_path)
        result = plan_status(target, saved)
        unknown = change.endswith('layout')
        expected = [] if version == 'v2' and not unknown else None
        assert result['added_database_checkpoints'] == result['removed_database_checkpoints'] == expected
        assert result['rows_changed'] is (None if unknown else False)
        assert result['schema_changed'] == (change in ('schema', 'checkpoint-layout', 'write-layout'))
        assert result['path_changed'] == (change == 'copy')
        assert result['binding_matches'] == (change == 'unchanged')
        assert result['added_checkpoints'] == result['removed_checkpoints'] == (None if unknown else [])
        if unknown:
            assert result['row_localization'] == 'unavailable-layout'
            human = run('plan-status', target, '--plan', saved)
            assert human.returncode == 3 and not human.stderr
            assert 'database checkpoint membership localization: unavailable (table layout)' in human.stdout
            assert 'added database checkpoints: unavailable' in human.stdout
        assert inputs(tmp_path) == before
