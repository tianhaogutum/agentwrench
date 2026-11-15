"""Saved action comparisons require no database, even for legacy artifacts."""
from copy import deepcopy
import json

import pytest

from agentwrench.checkpoints.cli import main
from agentwrench.checkpoints.db import KEY
from agentwrench.checkpoints.prune_plan import FORMAT, LEGACY_FORMAT
from .test_row_localization import small_database
from agentwrench.checkpoints.prune import prune


def identity(thread='t', ns='', cid='c1'):
    return dict(zip(KEY, (thread, ns, cid)))


def saved_plan(*, keep=2, version=FORMAT):
    value = {
        'format': version,
        'database': {'path': '/absent/private-history.db',
                     'schema_sha256': 'a' * 64, 'rows_sha256': 'b' * 64},
        'scope': {'thread': None}, 'keep_latest': keep,
        'summary': {'delete_checkpoints': 2 - keep, 'delete_writes': 0,
                    'reclaimable_payload_bytes': (2 - keep) * 20,
                    'reclaimable_metadata_bytes': (2 - keep) * 2,
                    'keep_latest': keep, 'groups': 1,
                    'delete_path': 'local-transaction (pinned SqliteSaver.prune is unimplemented)'},
        'checkpoints': [{**identity(cid=cid), 'action': 'delete' if keep == 1 and cid == 'c1' else 'keep'}
                        for cid in ('c1', 'c2')],
    }
    if version == FORMAT:
        value['raw_rows'] = {'checkpoints': [{**identity(cid=cid), 'sha256': 'c' * 64}
                                            for cid in ('c1', 'c2')], 'writes': []}
    return value


@pytest.fixture
def paired_plans(tmp_path):
    paths = tmp_path / 'before.json', tmp_path / 'after.json'
    for path, keep in zip(paths, (2, 1)):
        path.write_text(json.dumps(saved_plan(keep=keep)))
    return paths


def test_changed_retention_without_database(paired_plans, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('comparison must read only plan artifacts')

    monkeypatch.setattr('sqlite3.connect', forbidden)
    before = [path.read_bytes() for path in paired_plans]
    assert main(['compare-plans', *map(str, paired_plans), '--json']) == 3
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out) == {
        'format': 'lgckpt-scope.plan-comparison/v1', 'actions_identical': False,
        'scope': {'thread': None}, 'newly_selected': [], 'no_longer_selected': [],
        'keep_to_delete': [identity()], 'delete_to_keep': [],
    }
    assert [path.read_bytes() for path in paired_plans] == before


def compare(tmp_path, baseline, current, capsys, *, human=False, max_differences=None):
    paths = tmp_path / 'left.json', tmp_path / 'right.json'
    for path, value in zip(paths, (baseline, current)):
        path.write_text(json.dumps(value))
    flags = [] if max_differences is None else ['--max-differences', str(max_differences)]
    code = main(['compare-plans', *map(str, paths), *flags, *([] if human else ['--json'])])
    return code, capsys.readouterr()


@pytest.mark.parametrize('versions', [(FORMAT, FORMAT), (LEGACY_FORMAT, LEGACY_FORMAT),
                                   (LEGACY_FORMAT, FORMAT), (FORMAT, LEGACY_FORMAT)])
def test_reordered_equivalent_and_reverse_retention(tmp_path, capsys, versions):
    left, right = [saved_plan(version=version) for version in versions]
    right['checkpoints'].reverse()
    if 'raw_rows' in right:
        right['raw_rows']['checkpoints'].reverse()
    code, output = compare(tmp_path, left, right, capsys)
    assert code == 0 and not output.err
    result = json.loads(output.out)
    assert result['actions_identical']
    assert all(result[f] == [] for f in ('newly_selected', 'no_longer_selected', 'keep_to_delete', 'delete_to_keep'))
    right['keep_latest'] = right['summary']['keep_latest'] = 99
    code, output = compare(tmp_path, left, right, capsys)
    assert code == 0 and json.loads(output.out)['actions_identical']
    left = saved_plan(keep=1, version=versions[0])
    code, output = compare(tmp_path, left, right, capsys)
    assert code == 3 and not output.err
    result = json.loads(output.out)
    assert result['delete_to_keep'] == [identity()] and result['keep_to_delete'] == []


def test_full_identities_membership_and_deterministic_escaping(tmp_path, capsys):
    left = saved_plan(version=LEGACY_FORMAT)
    rows = [
        {**identity('t', '', 'same'), 'action': 'keep'},
        {**identity('t', 'ns', 'same'), 'action': 'delete'},
        {**identity('T', 'ns', 'same'), 'action': 'keep'},
        {**identity('', '\x1b\n\x00雪', ''), 'action': 'delete'},
    ]
    left['checkpoints'] = rows
    right = deepcopy(left)
    right['checkpoints'] = [
        {**rows[1], 'action': 'keep'}, {**rows[0], 'action': 'delete'},
        {**identity('t', 'NS', 'same'), 'action': 'keep'},
        {**identity('', '*', ''), 'action': 'delete'},
    ]
    code, output = compare(tmp_path, left, right, capsys)
    assert code == 3 and not output.err
    result = json.loads(output.out)
    assert result['keep_to_delete'] == [identity('t', '', 'same')]
    assert result['delete_to_keep'] == [identity('t', 'ns', 'same')]
    assert result['newly_selected'] == [right['checkpoints'][3], right['checkpoints'][2]]
    assert result['no_longer_selected'] == [rows[3], rows[2]]
    first = output.out
    left['checkpoints'].reverse()
    right['checkpoints'].reverse()
    assert compare(tmp_path, left, right, capsys)[1].out == first
    code, human = compare(tmp_path, left, right, capsys, human=True)
    assert code == 3 and 'DIFFERENT' in human.out
    assert '\x1b' not in human.out and '\x00' not in human.out and '雪' not in human.out
    assert '\\u001b\\n\\u0000\\u96ea' in human.out
    for output_text in (first, human.out):
        assert 'sha256' not in output_text and left['database']['path'] not in output_text


@pytest.mark.parametrize('limit', [None, 0])
@pytest.mark.parametrize('field', ['path', 'schema_sha256', 'rows_sha256', 'scope', 'raw-checkpoint', 'raw-write'])
def test_incompatible_snapshot_or_scope_no_report(tmp_path, capsys, field, limit):
    left, right = saved_plan(), saved_plan()
    if field == 'scope':
        right['scope']['thread'] = 't'
    elif field == 'raw-checkpoint':
        right['raw_rows']['checkpoints'][0]['sha256'] = 'd' * 64
    elif field == 'raw-write':
        right['raw_rows']['writes'].append({**identity(cid='c2'), 'task_id': 'task', 'idx': -1, 'sha256': 'd' * 64})
    else:
        right['database'][field] = '/absent/other.db' if field == 'path' else 'd' * 64
    code, output = compare(tmp_path, left, right, capsys, max_differences=limit)
    assert code == 2 and not output.out and 'incomparable prune plans' in output.err
    assert 'Traceback' not in output.err and 'private-history' not in output.err


@pytest.mark.parametrize('limit', [None, 0])
@pytest.mark.parametrize('kind', ['version', 'missing', 'extra', 'action', 'duplicate-id', 'duplicate-raw-id',
                                 'duplicate-write', 'bool-idx', 'bad-hash', 'raw-scope-mismatch',
                                 'bool-keep', 'negative-summary', 'wrong-summary', 'legacy-outside-scope'])
@pytest.mark.parametrize('side', [0, 1])
def test_invalid_plans_before_any_report(tmp_path, capsys, kind, side, limit):
    values = [saved_plan(), saved_plan(keep=1)]
    value = values[side]
    if kind == 'version':
        value['format'] = 'lgckpt-scope.prune/v99'
    elif kind == 'missing':
        del value['summary']
    elif kind == 'extra':
        value['backup'] = '/must-not-create'
    elif kind == 'action':
        value['checkpoints'][0]['action'] = 'ignore'
    elif kind == 'duplicate-id':
        value['checkpoints'].append(deepcopy(value['checkpoints'][0]))
    elif kind == 'duplicate-raw-id':
        value['raw_rows']['checkpoints'].append(deepcopy(value['raw_rows']['checkpoints'][0]))
    elif kind in ('duplicate-write', 'bool-idx'):
        row = {**identity(), 'task_id': '', 'idx': True if kind == 'bool-idx' else 0, 'sha256': 'c' * 64}
        value['raw_rows']['writes'] = [row, deepcopy(row)] if kind == 'duplicate-write' else [row]
    elif kind == 'bad-hash':
        value['database']['rows_sha256'] = 'not-a-digest'
    elif kind == 'raw-scope-mismatch':
        value['checkpoints'].pop()
    elif kind == 'bool-keep':
        value['keep_latest'] = True
    elif kind == 'negative-summary':
        value['summary']['delete_writes'] = -1
    elif kind == 'wrong-summary':
        value['summary']['keep_latest'] = 99
    else:
        value.pop('raw_rows')
        value['format'] = LEGACY_FORMAT
        value['scope']['thread'] = 'outside'
    code, output = compare(tmp_path, *values, capsys, max_differences=limit)
    assert code == 2 and not output.out and 'invalid prune plan' in output.err
    assert 'Traceback' not in output.err


@pytest.mark.parametrize('flags', [[], ['--max-differences', '0']])
@pytest.mark.parametrize('data', [b'{"format":1,"format":2}', b'{"format":NaN}', b'[' * 1100, b'\xff'])
def test_malformed_json_and_duplicate_keys(paired_plans, capsys, data, flags):
    paired_plans[1].write_bytes(data)
    assert main(['compare-plans', *map(str, paired_plans), *flags, '--json']) == 2
    output = capsys.readouterr()
    assert not output.out and 'invalid prune plan JSON' in output.err and 'Traceback' not in output.err


@pytest.mark.parametrize('flags', [[], ['--max-differences', '0']])
def test_resource_and_missing_file_errors(paired_plans, capsys, monkeypatch, flags):
    monkeypatch.setattr('agentwrench.checkpoints.prune_plan.MAX_PLAN_BYTES', 16)
    assert main(['compare-plans', *map(str, paired_plans), *flags, '--json']) == 2
    output = capsys.readouterr()
    assert not output.out and 'input limit' in output.err
    monkeypatch.undo()
    monkeypatch.setattr('agentwrench.checkpoints.prune_plan.MAX_ROWS', 1)
    assert main(['compare-plans', *map(str, paired_plans), *flags, '--json']) == 2
    output = capsys.readouterr()
    assert not output.out and 'invalid prune plan' in output.err
    paired_plans[0].unlink()
    assert main(['compare-plans', *map(str, paired_plans), *flags, '--json']) == 2
    output = capsys.readouterr()
    assert not output.out and 'file operation failed' in output.err


@pytest.mark.parametrize('limit', [None, 0])
def test_real_exports_after_source_removed_and_only_artifact_reads(tmp_path, monkeypatch, capsys, limit):
    db = small_database(tmp_path / 'history.db')
    paths = tmp_path / 'keep2.json', tmp_path / 'keep1.json'
    for path, keep in zip(paths, (2, 1)):
        prune(db, keep, dry_run=True, export_plan=path)
    db.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail('comparison attempted database, decode, write or path-resolution work')

    from pathlib import Path
    original = Path.open
    reads = []

    def read_only(path, *args, **kwargs):
        assert path in paths and args == ('rb',) and not kwargs
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', read_only)
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr('sqlite3.connect', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.cli.connect', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.cli.prune', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.codec.SERDE.loads_typed', forbidden)
    monkeypatch.setattr('agentwrench.checkpoints.prune.write_backup', forbidden)
    flags = [] if limit is None else ['--max-differences', str(limit)]
    assert main(['compare-plans', *map(str, paths), *flags, '--json']) == 3
    output = capsys.readouterr()
    assert not output.err and json.loads(output.out)['keep_to_delete'] == ([identity()] if limit is None else [])
    assert reads == list(paths)


@pytest.mark.parametrize('version', [FORMAT, LEGACY_FORMAT])
def test_empty_scope_no_actions(tmp_path, capsys, version):
    left = saved_plan(version=version)
    left['scope']['thread'] = ''
    left['checkpoints'] = []
    left['summary']['groups'] = 0
    if version == FORMAT:
        left['raw_rows']['checkpoints'] = []
    code, output = compare(tmp_path, left, left, capsys, human=True)
    assert code == 0 and not output.err and 'IDENTICAL' in output.out
