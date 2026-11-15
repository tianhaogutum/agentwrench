"""Display caps preserve complete read-only drift evidence and strict refusals."""
import json

from agentwrench.checkpoints.cli import main
from agentwrench.checkpoints.prune import prune
from .test_row_localization import small_database
from .test_scope import execute
from .test_write_membership import inputs


FIELDS = ('added_checkpoints', 'removed_checkpoints', 'added_database_checkpoints',
          'removed_database_checkpoints', 'added_writes', 'removed_writes',
          'changed_checkpoints', 'changed_writes')


def test_zero_limit_preserves_stale_exit_and_exact_totals(tmp_path, capsys):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    execute(db, "UPDATE checkpoints SET checkpoint_id='c3' WHERE checkpoint_id='c1'")
    before = inputs(tmp_path)
    assert main(['plan-status', str(db), '--plan', str(saved), '--max-differences', '0', '--json']) == 3
    output = capsys.readouterr()
    assert not output.err
    result = json.loads(output.out)
    assert result['binding_matches'] is False and result['rows_changed'] is True
    assert result['max_differences'] == 0
    assert all(result[f] == [] for f in FIELDS)
    assert result['difference_summary'] == {
        f: {'total': int(f in FIELDS[:4]), 'shown': 0, 'truncated': f in FIELDS[:4]} for f in FIELDS}
    assert inputs(tmp_path) == before

from contextlib import closing
from pathlib import Path
import re
import shutil
import sqlite3

import pytest

from agentwrench.checkpoints.db import KEY, ScopeError
from agentwrench.checkpoints.plan_status import plan_status
from agentwrench.checkpoints.prune_plan import WRITE_KEY
from .plan_status_limits_helpers import (
    GROUPS, INDICES, assert_cap, change_database, create_database,
)
from .test_row_localization import legacy


@pytest.mark.parametrize('thread', ['t-1', None])
@pytest.mark.parametrize('limit', [0, 1, 2, 6, 9, 99])
def test_every_list_exact_sorted_independent_prefix(tmp_path, capsys, thread, limit):
    db = create_database(tmp_path / 'history.db')
    groups = GROUPS if thread is not None else GROUPS[:2]
    if thread is None:
        execute(db, "DELETE FROM checkpoints WHERE checkpoint_ns<>''")
        execute(db, "DELETE FROM writes WHERE checkpoint_ns<>''")
    saved = tmp_path / 'plan.json'
    prune(db, 1, thread=thread, dry_run=True, export_plan=saved)
    with closing(sqlite3.connect(db)) as con:
        change_database(con)
    expected = {}
    for kind, ids in [('added', ('a1', 'a2')), ('removed', ('c1', 'c2')), ('changed', ('c3', 'c4'))]:
        keys = sorted((*g, cid) for g in groups for cid in ids)
        expected[f'{kind}_database_checkpoints' if kind != 'changed' else 'changed_checkpoints'] = [dict(zip(KEY, k)) for k in keys]
        if kind != 'changed':
            expected[f'{kind}_checkpoints'] = [dict(zip(KEY, k)) for k in keys if thread is None or k[0] == thread]
    for kind, task in [('added', 'added'), ('removed', 'removed'), ('changed', 'stable')]:
        expected[f'{kind}_writes'] = [dict(zip(WRITE_KEY, (*g, 'c4', task, idx))) for g in groups for idx in INDICES]
    full = plan_status(db, saved)
    assert 'difference_summary' not in full and 'max_differences' not in full
    assert {f: full[f] for f in FIELDS} == expected
    value = json.loads(saved.read_text())
    value['checkpoints'].reverse()
    for rows in value['raw_rows'].values():
        rows.reverse()
    saved.write_text(json.dumps(value))
    before = inputs(tmp_path)
    result = plan_status(db, saved, max_differences=limit)
    assert_cap(full, result, limit)
    # Same-key changes cannot also be additions or removals in global evidence.
    for table, membership in [('checkpoints', 'database_checkpoints'), ('writes', 'writes')]:
        changed = {tuple(r.values()) for r in full[f'changed_{table}']}
        for kind in ('added', 'removed'):
            assert not changed.intersection(tuple(r.values()) for r in full[f'{kind}_{membership}'])
    assert main(['plan-status', str(db), '--plan', str(saved), '--max-differences', str(limit)]) == 3
    human = capsys.readouterr()
    assert not human.err and 'STALE' in human.out
    for field in FIELDS:
        label = field.replace('_', ' ')
        count = len(expected[field])
        assert f'{label}: {count} total, {min(count, limit)} shown, truncated: {str(count > limit).lower()}' in human.out
    assert '\x1b' not in human.out and '\x00' not in human.out and '雪' not in human.out
    if thread and limit > 2:
        assert '\\u0000\\u001b\\n\\u96ea' in human.out
    for text in (json.dumps(result), human.out):
        assert 'private-' not in text and 'sha256' not in text and str(db) not in text
        assert not re.search(r'[0-9a-f]{64}', text)
    assert inputs(tmp_path) == before


@pytest.mark.parametrize('version', ['v1', 'v2'])
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('change', ['unchanged', 'copy', 'schema', 'checkpoint-layout', 'write-layout', 'view'])
def test_complete_empty_and_unavailable_evidence(tmp_path, capsys, version, empty, change):
    db = small_database(tmp_path / 'history.db')
    if empty:
        execute(db, 'DELETE FROM checkpoints')
        execute(db, 'DELETE FROM writes')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    if version == 'v1':
        saved.write_text(json.dumps(legacy(json.loads(saved.read_text()))))
    target = db
    if change == 'copy':
        target = tmp_path / 'copy.db'
        shutil.copyfile(db, target)
    elif change == 'schema':
        execute(db, 'PRAGMA user_version=17')
    elif change == 'checkpoint-layout':
        execute(db, 'ALTER TABLE checkpoints RENAME COLUMN metadata TO incompatible')
    elif change == 'write-layout':
        execute(db, 'ALTER TABLE writes RENAME COLUMN value TO incompatible')
    elif change == 'view':
        execute(db, 'ALTER TABLE checkpoints RENAME TO original')
        execute(db, 'CREATE VIEW checkpoints AS SELECT * FROM original')
    before = inputs(tmp_path)
    unknown = change in ('checkpoint-layout', 'write-layout', 'view')
    full = plan_status(target, saved)
    for limit in (0, 1):
        result = plan_status(target, saved, max_differences=limit)
        assert_cap(full, result, limit)
        assert result['format'] == f'lgckpt-scope.plan-status/{version}'
        assert result['binding_matches'] == (change == 'unchanged')
        assert result['rows_changed'] is (None if unknown else False)
        assert result['path_changed'] == (change == 'copy')
        assert result['schema_changed'] == (change not in ('unchanged', 'copy'))
        for field in FIELDS:
            unavailable = unknown or (version == 'v1' and field not in FIELDS[:2])
            assert result[field] == (None if unavailable else [])
            assert result['difference_summary'][field] == (None if unavailable else {
                'total': 0, 'shown': 0, 'truncated': False})
        for flag in ([], ['--json']):
            assert main(['plan-status', str(target), '--plan', str(saved), '--max-differences', str(limit), *flag]) == (0 if change == 'unchanged' else 3)
            output = capsys.readouterr()
            assert not output.err
            if flag:
                assert json.loads(output.out) == result
            else:
                for field in FIELDS:
                    label = field.replace('_', ' ')
                    assert f'{label}: ' + ('unavailable' if result[field] is None else '0 total, 0 shown, truncated: false') in output.out
    assert inputs(tmp_path) == before


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_bounded_committed_wal_snapshot_no_decoding_or_stale_apply(tmp_path, monkeypatch, version):
    db = create_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, thread='t-1', dry_run=True, export_plan=saved)
    if version == 'v1':
        saved.write_text(json.dumps(legacy(json.loads(saved.read_text()))))

    def forbidden(*args, **kwargs):
        pytest.fail('stale diagnosis must not decode or plan')
    monkeypatch.setattr('agentwrench.checkpoints.plan_status.plan', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.codec.SERDE.loads_typed', forbidden)
    with closing(sqlite3.connect(db)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        change_database(writer)
        assert Path(str(db) + '-wal').stat().st_size > 0
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("DELETE FROM checkpoints WHERE thread_id='t-1'")
        before = inputs(tmp_path)
        full = plan_status(db, saved)
        for limit in (0, 1):
            result = plan_status(db, saved, max_differences=limit)
            assert_cap(full, result, limit)
            assert result['difference_summary']['removed_checkpoints']['total'] == 2
            assert result['difference_summary']['added_checkpoints']['total'] == 2
            assert not result['binding_matches'] and result['rows_changed']
        assert inputs(tmp_path) == before
        writer.rollback()
        with pytest.raises(ScopeError, match='stale prune plan'):
            prune(db, apply_plan=saved, backup=tmp_path / 'never.bak')
        assert not (tmp_path / 'never.bak').exists() and inputs(tmp_path) == before


@pytest.mark.parametrize('value', ['-1', '+1', '1.0', '1e2', ' 1', '1 ', '', 'true', '١', '１', '9' * 5000, None])
def test_invalid_cli_limit_before_any_reads(capsys, monkeypatch, value):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid limit must not read DB or plan')
    monkeypatch.setattr('agentwrench.checkpoints.plan_status.connect', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    option = ['--max-differences'] if value is None else [f'--max-differences={value}']
    with pytest.raises(SystemExit) as error:
        main(['plan-status', 'absent.db', '--plan', 'absent.json', '--json', *option])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and 'Traceback' not in output.err
    assert ('expected one argument' if value is None else 'nonnegative ASCII decimal integer') in output.err


@pytest.mark.parametrize('value', [True, False, -1, 1.0, '1', [], {}])
def test_invalid_api_limit_before_reads(value, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid limit must not read DB or plan')
    monkeypatch.setattr('agentwrench.checkpoints.plan_status.load_plan', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.plan_status.connect', forbidden)
    with pytest.raises(ScopeError, match='max-differences must be a nonnegative integer'):
        plan_status('absent.db', 'absent.json', max_differences=value)


def test_leading_zero_limit_and_deleted_scope(tmp_path, capsys):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, thread='t', dry_run=True, export_plan=saved)
    execute(db, 'DELETE FROM checkpoints')
    assert main(['plan-status', str(db), '--plan', str(saved), '--max-differences', '002', '--json']) == 3
    result = json.loads(capsys.readouterr().out)
    assert result['max_differences'] == 2
    assert result['added_checkpoints'] == []
    assert len(result['removed_checkpoints']) == 2
    assert result['difference_summary']['removed_checkpoints'] == {'total': 2, 'shown': 2, 'truncated': False}


@pytest.mark.parametrize('limit', [0, 1])
@pytest.mark.parametrize('kind', ['duplicate-json', 'duplicate-raw', 'raw-idx', 'missing-raw', 'unsupported',
                                  'edited-action', 'edited-summary', 'edited-hash', 'missing-plan',
                                  'missing-db', 'not-sqlite', 'invalid-checkpoint-key', 'invalid-write-key'])
def test_display_cap_never_hides_invalid_inputs(tmp_path, capsys, limit, kind):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    value = json.loads(saved.read_text())
    if kind == 'duplicate-json':
        saved.write_text('{"format":0,"format":1}')
    elif kind == 'duplicate-raw':
        value['raw_rows']['writes'].append(value['raw_rows']['writes'][0])
    elif kind == 'raw-idx':
        value['raw_rows']['writes'][0]['idx'] = True
    elif kind == 'missing-raw':
        del value['raw_rows']
    elif kind == 'unsupported':
        value['format'] = 'unsupported'
    elif kind == 'edited-action':
        value['checkpoints'][0]['action'] = 'keep'
    elif kind == 'edited-summary':
        value['summary']['delete_writes'] += 1
    elif kind == 'edited-hash':
        value['raw_rows']['writes'][0]['sha256'] = '0' * 64
    elif kind == 'missing-plan':
        saved.unlink()
    elif kind == 'missing-db':
        db.unlink()
    elif kind == 'not-sqlite':
        db.write_bytes(b'not a database')
    elif kind == 'invalid-checkpoint-key':
        execute(db, "UPDATE checkpoints SET checkpoint_id=x'01' WHERE checkpoint_id='c1'")
    elif kind == 'invalid-write-key':
        execute(db, "UPDATE writes SET task_id=x'01'")
    if kind in ('duplicate-raw', 'raw-idx', 'missing-raw', 'unsupported', 'edited-action', 'edited-summary', 'edited-hash'):
        saved.write_text(json.dumps(value))
    before = inputs(tmp_path)
    assert main(['plan-status', str(db), '--plan', str(saved), '--max-differences', str(limit), '--json']) == 2
    output = capsys.readouterr()
    assert not output.out and output.err.startswith('error:') and 'Traceback' not in output.err
    assert inputs(tmp_path) == before


@pytest.mark.parametrize('limit', [0, 1])
@pytest.mark.parametrize('module,field', [('db', 'MAX_ROWS'), ('db', 'MAX_PAYLOAD_BYTES'), ('db', 'MAX_BLOB_BYTES'),
                                        ('prune_plan', 'MAX_ROWS'), ('prune_plan', 'MAX_PLAN_BYTES')])
def test_display_cap_never_hides_resource_bounds(tmp_path, monkeypatch, capsys, limit, module, field):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    before = inputs(tmp_path)
    monkeypatch.setattr(f'agentwrench.checkpoints.{module}.{field}', 1)
    assert main(['plan-status', str(db), '--plan', str(saved), '--max-differences', str(limit), '--json']) == 2
    output = capsys.readouterr()
    assert not output.out and output.err.startswith('error:') and 'Traceback' not in output.err
    assert inputs(tmp_path) == before
