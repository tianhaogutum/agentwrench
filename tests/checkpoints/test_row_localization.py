"""V2 raw-row localization and explicit legacy fallback."""
from contextlib import closing
import json
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from agentwrench.checkpoints.codec import SERDE
from agentwrench.checkpoints.prune import prune
from .test_scope import digest, execute, run


def small_database(path):
    with closing(sqlite3.connect(path)) as con:
        SqliteSaver(con).setup()
        for cid in ('c1', 'c2'):
            kind, payload = SERDE.dumps_typed({
                'v': 4, 'id': cid, 'ts': '2026-11-11T12:00:00+00:00',
                'channel_values': {'x': 1}, 'channel_versions': {}, 'versions_seen': {}})
            con.execute('INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)',
                        ('t', '', cid, None, kind, payload, b'{}'))
        kind, value = SERDE.dumps_typed(1)
        con.execute('INSERT INTO writes (thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value,task_path) VALUES (?,?,?,?,?,?,?,?,?)',
                    ('t', '', 'c2', 'task', 0, 'x', kind, value, ''))
        con.commit()
    return path


def legacy(value):
    value = {k: v for k, v in value.items() if k != 'raw_rows'}
    value['format'] = 'lgckpt-scope.prune/v1'
    return value


def test_stable_id_metadata_and_write_localization_with_legacy_fallback(tmp_path):
    db = small_database(tmp_path / 'history.db')
    saved, old = tmp_path / 'v2.json', tmp_path / 'v1.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    value = json.loads(saved.read_text())
    old.write_text(json.dumps(legacy(value)))
    execute(db, "UPDATE checkpoints SET metadata=? WHERE checkpoint_id='c1'", (b'metadata-secret',))
    execute(db, "UPDATE writes SET value=?,type='unknown' WHERE checkpoint_id='c2'", (b'write-secret',))
    before = digest(db), saved.read_bytes(), old.read_bytes()
    for plan, version in ((saved, 'v2'), (old, 'v1')):
        proc = run('plan-status', db, '--plan', plan, '--json')
        assert proc.returncode == 3, proc.stderr
        result = json.loads(proc.stdout)
        assert result['rows_changed'] and not result['schema_changed']
        assert result['added_checkpoints'] == result['removed_checkpoints'] == []
        if version == 'v2':
            assert result['changed_checkpoints'] == [dict(thread_id='t', checkpoint_ns='', checkpoint_id='c1')]
            assert result['changed_writes'] == [dict(thread_id='t', checkpoint_ns='', checkpoint_id='c2', task_id='task', idx=0)]
            assert result['row_localization'] == 'database'
        else:
            assert result['changed_checkpoints'] is result['changed_writes'] is None
            assert result['row_localization'] == 'unavailable-v1'
        assert result['format'] == f'lgckpt-scope.plan-status/{version}'
        assert 'secret' not in proc.stdout and 'sha256' not in proc.stdout
        human = run('plan-status', db, '--plan', plan)
        assert human.returncode == 3 and 'secret' not in human.stdout
        if version == 'v1':
            assert 'per-row localization: unavailable (v1 plan)' in human.stdout
        backup = tmp_path / f'{version}.bak'
        refused = run('prune', db, '--apply-plan', plan, '--backup', backup)
        assert refused.returncode == 2 and 'stale prune plan' in refused.stderr
        assert not backup.exists()
    assert (digest(db), saved.read_bytes(), old.read_bytes()) == before


import pytest

from agentwrench.checkpoints import prune_plan
from agentwrench.checkpoints.db import KEY, ScopeError
from agentwrench.checkpoints.plan_status import plan_status


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_unchanged_version_applies_and_revalidates_actions(tmp_path, version):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    if version == 'v1':
        saved.write_text(json.dumps(legacy(json.loads(saved.read_text()))))
    before = digest(db), saved.read_bytes()
    result = plan_status(db, saved)
    assert result['binding_matches'] and not result['rows_changed']
    assert result['format'] == f'lgckpt-scope.plan-status/{version}'
    assert (digest(db), saved.read_bytes()) == before
    value = json.loads(saved.read_text())
    edited = tmp_path / 'edited.json'
    value['checkpoints'][0]['action'] = 'keep'
    edited.write_text(json.dumps(value))
    with pytest.raises(ScopeError, match='invalid prune plan'):
        plan_status(db, edited)
    with pytest.raises(ScopeError, match='invalid prune plan'):
        prune(db, apply_plan=edited, backup=tmp_path / 'never.bak')
    assert not (tmp_path / 'never.bak').exists()
    assert prune(db, apply_plan=saved, backup=tmp_path / 'backup.db')['verified']
    with closing(sqlite3.connect(db)) as con:
        assert con.execute('SELECT checkpoint_id FROM checkpoints').fetchall() == [('c2',)]
        assert con.execute('SELECT count(*) FROM writes').fetchone() == (1,)


@pytest.mark.parametrize('kind', [
    'duplicate-checkpoint', 'duplicate-write', 'missing-raw', 'extra-raw',
    'missing-hash', 'bad-hash', 'uppercase-hash', 'bool-idx', 'float-idx',
    'string-idx', 'overflow-idx', 'bad-task', 'bad-key', 'extra-field',
    'scope-mismatch', 'raw-not-list', 'v1-with-raw', 'edited-hash', 'omitted-write',
])
def test_malformed_or_edited_raw_fingerprints_refuse_without_writes(tmp_path, kind):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    value = json.loads(saved.read_text())
    raw = value['raw_rows']
    if kind.startswith('duplicate-'):
        table = 'writes' if kind.endswith('write') else 'checkpoints'
        raw[table].append(raw[table][0].copy())
    elif kind == 'missing-raw': del value['raw_rows']
    elif kind == 'extra-raw': raw['other'] = []
    elif kind == 'missing-hash': del raw['checkpoints'][0]['sha256']
    elif kind == 'bad-hash': raw['checkpoints'][0]['sha256'] = 'g' * 64
    elif kind == 'uppercase-hash': raw['checkpoints'][0]['sha256'] = 'A' * 64
    elif kind in ('bool-idx', 'float-idx', 'string-idx', 'overflow-idx'):
        raw['writes'][0]['idx'] = {'bool-idx': True, 'float-idx': 0.0, 'string-idx': '0', 'overflow-idx': 2**63}[kind]
    elif kind == 'bad-task': raw['writes'][0]['task_id'] = None
    elif kind == 'bad-key': raw['checkpoints'][0]['thread_id'] = 1
    elif kind == 'extra-field': raw['writes'][0]['task_path'] = 'private'
    elif kind == 'scope-mismatch': raw['checkpoints'].pop()
    elif kind == 'raw-not-list': raw['writes'] = {}
    elif kind == 'v1-with-raw': value['format'] = 'lgckpt-scope.prune/v1'
    elif kind == 'edited-hash': raw['writes'][0]['sha256'] = '0' * 64
    elif kind == 'omitted-write': raw['writes'] = []
    saved.write_text(json.dumps(value))
    before = digest(db), saved.read_bytes()
    for command in (('plan-status', db, '--plan', saved, '--json'),
                    ('prune', db, '--apply-plan', saved, '--backup', tmp_path / 'never.bak', '--json')):
        proc = run(*command)
        assert proc.returncode == 2 and not proc.stdout and 'invalid prune plan' in proc.stderr
        assert 'Traceback' not in proc.stderr and 'private' not in proc.stderr
    assert not (tmp_path / 'never.bak').exists()
    assert (digest(db), saved.read_bytes()) == before


def test_combined_fingerprint_limit_and_duplicate_json_keys(tmp_path, monkeypatch):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    # Two checkpoints fit by themselves, but the third entry (write) exceeds 2.
    monkeypatch.setattr(prune_plan, 'MAX_ROWS', 2)
    with pytest.raises(ScopeError, match='invalid prune plan'):
        prune_plan.load_plan(saved)
    text = saved.read_text().replace('"sha256":', '"sha256": "' + '0' * 64 + '", "sha256":', 1)
    saved.write_text(text)
    with pytest.raises(ScopeError, match='invalid prune plan JSON'):
        prune_plan.load_plan(saved)


def test_exact_global_keys_raw_types_and_wal_without_decoding(db, tmp_path, monkeypatch):
    # Add equal IDs in a separate namespace on an unselected thread. Export still
    # binds them without requiring that unselected state be prunable/decodable.
    with closing(sqlite3.connect(db)) as con:
        con.execute("INSERT INTO checkpoints SELECT thread_id,'ns',checkpoint_id,parent_checkpoint_id,type,checkpoint,metadata FROM checkpoints WHERE thread_id='t-2'")
        con.execute("INSERT INTO writes SELECT thread_id,'ns',checkpoint_id,task_id,task_path,idx,channel,type,value FROM writes WHERE thread_id='t-2'")
        con.commit()
    saved = tmp_path / 'plan.json'
    prune(db, 2, thread='t-1', dry_run=True, export_plan=saved)
    value = json.loads(saved.read_text())
    cp_keys = [tuple(r[f] for f in KEY) for r in value['raw_rows']['checkpoints']]
    assert cp_keys == sorted(cp_keys) and len(cp_keys) == 36
    write_keys = [tuple(r[f] for f in prune_plan.WRITE_KEY) for r in value['raw_rows']['writes']]
    assert write_keys == sorted(write_keys)
    target = next(k for k in write_keys if k[:2] == ('t-2', 'ns'))
    cp = next(k for k in cp_keys if k[:2] == ('t-2', 'ns'))
    def forbidden(*args, **kwargs):
        pytest.fail('stale diagnostics must not decode or recompute a prune plan')
    monkeypatch.setattr('agentwrench.checkpoints.plan_status.plan', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.codec.SERDE.loads_typed', forbidden)
    with closing(sqlite3.connect(db)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        # Text '{}' and blob b'{}' are different raw rows even with equal bytes.
        writer.execute('UPDATE checkpoints SET metadata=CAST(metadata AS TEXT) WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?', cp)
        writer.execute('UPDATE writes SET value=?,type=? WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=? AND task_id=? AND idx=?', (b'opaque-secret', 'unknown', *target))
        writer.commit()
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
        writer.execute('BEGIN IMMEDIATE')
        result = plan_status(db, saved)
        assert result['row_localization'] == 'database'
        assert result['changed_checkpoints'] == [dict(zip(KEY, cp))]
        assert result['changed_writes'] == [dict(zip(prune_plan.WRITE_KEY, target))]
        assert result['added_checkpoints'] == result['removed_checkpoints'] == []
        # SQLite may change SHM lock bytes, but main DB/WAL and plans stay intact.
        for name, content in before.items():
            if not name.endswith('-shm'):
                assert (tmp_path / name).read_bytes() == content
        assert 'opaque-secret' not in json.dumps(result)


def test_changed_keys_sort_and_do_not_include_added_removed(tmp_path):
    db = small_database(tmp_path / 'history.db')
    with closing(sqlite3.connect(db)) as con:
        for task, idx in [('task', -1), ('other\x1b\n', 2), ('task', 1)]:
            con.execute('INSERT INTO writes SELECT thread_id,checkpoint_ns,checkpoint_id,?,task_path,?,channel,type,value FROM writes LIMIT 1', (task, idx))
        con.commit()
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    execute(db, "UPDATE writes SET task_path='secret'")
    execute(db, "UPDATE checkpoints SET metadata=x'7b7d20' WHERE checkpoint_id='c2'")
    execute(db, "DELETE FROM checkpoints WHERE checkpoint_id='c1'")
    execute(db, "UPDATE writes SET task_id='added' WHERE task_id='task' AND idx=1")
    result = plan_status(db, saved)
    assert result['changed_checkpoints'] == [dict(thread_id='t', checkpoint_ns='', checkpoint_id='c2')]
    assert [(r['task_id'], r['idx']) for r in result['changed_writes']] == [('other\x1b\n', 2), ('task', -1), ('task', 0)]
    human = run('plan-status', db, '--plan', saved)
    assert human.returncode == 3 and '\\u001b\\n' in human.stdout
    assert '\x1b' not in human.stdout and 'secret' not in human.stdout


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_layout_unknown_localization_and_schema_only(tmp_path, version):
    db = small_database(tmp_path / 'history.db')
    saved = tmp_path / 'plan.json'
    prune(db, 1, dry_run=True, export_plan=saved)
    if version == 'v1': saved.write_text(json.dumps(legacy(json.loads(saved.read_text()))))
    execute(db, 'PRAGMA user_version=12')
    result = plan_status(db, saved)
    assert result['schema_changed'] and not result['rows_changed']
    if version == 'v2': assert result['changed_checkpoints'] == result['changed_writes'] == []
    execute(db, 'ALTER TABLE writes RENAME COLUMN value TO unsupported')
    result = plan_status(db, saved)
    assert result['row_localization'] == 'unavailable-layout'
    assert result['changed_checkpoints'] is result['changed_writes'] is result['rows_changed'] is None
