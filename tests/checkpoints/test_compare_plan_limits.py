"""Output caps apply only after complete saved-plan comparison and validation."""
import json
from pathlib import Path

import pytest

from agentwrench.checkpoints.compare_plans import DIFFERENCES, compare_plans
from agentwrench.checkpoints.db import KEY, ScopeError
from agentwrench.checkpoints.prune_plan import FORMAT, LEGACY_FORMAT
from agentwrench.checkpoints.cli import main
from .test_compare_plans import identity, paired_plans, saved_plan  # noqa: F401


def test_zero_limit_preserves_difference_exit_and_exact_totals(paired_plans, capsys):
    before = [path.read_bytes() for path in paired_plans]
    assert main(['compare-plans', *map(str, paired_plans), '--max-differences', '0', '--json']) == 3
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out) == {
        'format': 'lgckpt-scope.plan-comparison/v1', 'actions_identical': False,
        'scope': {'thread': None}, 'newly_selected': [], 'no_longer_selected': [],
        'keep_to_delete': [], 'delete_to_keep': [], 'max_differences': 0,
        'difference_summary': {
            'newly_selected': {'total': 0, 'shown': 0, 'truncated': False},
            'no_longer_selected': {'total': 0, 'shown': 0, 'truncated': False},
            'keep_to_delete': {'total': 1, 'shown': 0, 'truncated': True},
            'delete_to_keep': {'total': 0, 'shown': 0, 'truncated': False},
        },
    }
    assert [path.read_bytes() for path in paired_plans] == before


def write_pair(tmp_path, left, right):
    paths = tmp_path / 'left.json', tmp_path / 'right.json'
    for path, value in zip(paths, (left, right)):
        path.write_text(json.dumps(value))
    return paths


def raw_for(value):
    value['raw_rows'] = {'checkpoints': [{**{f: row[f] for f in KEY}, 'sha256': 'c' * 64}
                                        for row in value['checkpoints']], 'writes': []}


@pytest.mark.parametrize('versions', [(LEGACY_FORMAT, LEGACY_FORMAT), (LEGACY_FORMAT, FORMAT),
                                      (FORMAT, LEGACY_FORMAT)])
@pytest.mark.parametrize('limit', [0, 1, 4, 9])
def test_each_list_independent_sorted_prefix_and_exact_totals(tmp_path, capsys, versions, limit):
    left, right = [saved_plan(version=v) for v in versions]
    left['checkpoints'], right['checkpoints'] = [], []
    triples = [('', '\x1b\n\x00雪', ''), ('T', 'ns', 'same'), ('t', '', 'same'), ('t', 'ns', 'same')]
    expected = {field: [] for field in DIFFERENCES}
    for field in DIFFERENCES:
        for thread, ns, cid in triples:
            row = identity(thread, ns, cid + field)
            if field == 'newly_selected':
                right['checkpoints'].append({**row, 'action': 'delete'})
                expected[field].append({**row, 'action': 'delete'})
            elif field == 'no_longer_selected':
                left['checkpoints'].append({**row, 'action': 'keep'})
                expected[field].append({**row, 'action': 'keep'})
            else:
                before, after = ('keep', 'delete') if field == 'keep_to_delete' else ('delete', 'keep')
                left['checkpoints'].append({**row, 'action': before})
                right['checkpoints'].append({**row, 'action': after})
                expected[field].append(row)
    for value in (left, right):
        if value['format'] == FORMAT:
            raw_for(value)
    paths = write_pair(tmp_path, left, right)
    before_bytes = [p.read_bytes() for p in paths]
    full = compare_plans(*paths)
    assert set(full) == {'format', 'actions_identical', 'scope', *DIFFERENCES}
    assert all(full[f] == expected[f] for f in DIFFERENCES)
    bounded = compare_plans(*paths, max_differences=limit)
    assert not bounded['actions_identical']
    for field in DIFFERENCES:
        assert bounded[field] == expected[field][:limit]
        assert bounded['difference_summary'][field] == {
            'total': 4, 'shown': min(limit, 4), 'truncated': limit < 4}
    for value in (left, right):
        value['checkpoints'].reverse()
        if 'raw_rows' in value:
            value['raw_rows']['checkpoints'].reverse()
    write_pair(tmp_path, left, right)
    assert compare_plans(*paths, max_differences=limit) == bounded
    assert main(['compare-plans', *map(str, paths), '--max-differences', str(limit)]) == 3
    human = capsys.readouterr()
    assert not human.err and 'DIFFERENT' in human.out
    for field in DIFFERENCES:
        assert f'{field}: 4 total, {min(limit, 4)} shown, truncated: {str(limit < 4).lower()}' in human.out
    assert '\x1b' not in human.out and '\x00' not in human.out and '雪' not in human.out
    if limit:
        assert '\\u001b\\n\\u0000\\u96ea' in human.out
    assert 'sha256' not in human.out and left['database']['path'] not in human.out
    # Only the deliberate order reversal above changes the files.
    assert [p.read_bytes() for p in paths] != before_bytes
    after_reorder = [p.read_bytes() for p in paths]
    compare_plans(*paths, max_differences=limit)
    assert [p.read_bytes() for p in paths] == after_reorder


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('versions', [(FORMAT, FORMAT), (LEGACY_FORMAT, LEGACY_FORMAT), (LEGACY_FORMAT, FORMAT)])
def test_bounded_empty_or_equivalent_complete(tmp_path, capsys, empty, versions):
    left, right = [saved_plan(version=v) for v in versions]
    for value in (left, right):
        if empty:
            value['checkpoints'] = []
            value['scope']['thread'] = ''
            if 'raw_rows' in value:
                value['raw_rows']['checkpoints'] = []
        else:
            value['checkpoints'].reverse()
    paths = write_pair(tmp_path, left, right)
    assert main(['compare-plans', *map(str, paths), '--max-differences', '0', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['actions_identical']
    assert all(result[f] == [] and result['difference_summary'][f] == {
        'total': 0, 'shown': 0, 'truncated': False} for f in DIFFERENCES)
    assert main(['compare-plans', *map(str, paths), '--max-differences', '00']) == 0
    assert 'IDENTICAL' in capsys.readouterr().out


def test_v2_multiple_transitions_both_directions(tmp_path):
    left, right = saved_plan(), saved_plan()
    left['checkpoints'] = [{**identity(cid=f'c{i}'), 'action': 'keep' if i % 2 else 'delete'} for i in range(8)]
    right['checkpoints'] = [{**r, 'action': 'delete' if r['action'] == 'keep' else 'keep'}
                            for r in reversed(left['checkpoints'])]
    for value in (left, right):
        raw_for(value)
    paths = write_pair(tmp_path, left, right)
    bounded = compare_plans(*paths, max_differences=1)
    assert bounded['keep_to_delete'] == [identity(cid='c1')]
    assert bounded['delete_to_keep'] == [identity(cid='c0')]
    assert all(bounded['difference_summary'][f] == {'total': 4, 'shown': 1, 'truncated': True}
               for f in ('keep_to_delete', 'delete_to_keep'))


@pytest.mark.parametrize('value', ['-1', '+1', '1.0', '1e2', ' 1', '1 ', '', 'true', '١', '１', '9' * 5000])
def test_invalid_limit_before_artifact_reads(capsys, monkeypatch, value):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid limit must fail before reading artifacts')
    monkeypatch.setattr(Path, 'open', forbidden)
    with pytest.raises(SystemExit) as error:
        main(['compare-plans', 'absent-left', 'absent-right', f'--max-differences={value}', '--json'])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and 'must be a nonnegative ASCII decimal integer' in output.err
    assert 'Traceback' not in output.err


def test_missing_limit(capsys):
    with pytest.raises(SystemExit) as error:
        main(['compare-plans', 'absent-left', 'absent-right', '--json', '--max-differences'])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and 'expected one argument' in output.err


@pytest.mark.parametrize('value', [True, False, -1, 1.0, '1', [], {}])
def test_api_invalid_limit_before_reads(value):
    with pytest.raises(ScopeError, match='max-differences must be a nonnegative integer'):
        compare_plans('absent-left', 'absent-right', max_differences=value)


def test_raw_row_resource_bound_not_hidden_by_zero_cap(tmp_path, monkeypatch, capsys):
    left, right = saved_plan(), saved_plan()
    for value in (left, right):
        value['raw_rows']['writes'] = [{**identity(), 'task_id': '', 'idx': 0, 'sha256': 'c' * 64}]
    paths = write_pair(tmp_path, left, right)
    monkeypatch.setattr('agentwrench.checkpoints.prune_plan.MAX_ROWS', 2)
    assert main(['compare-plans', *map(str, paths), '--max-differences', '0', '--json']) == 2
    output = capsys.readouterr()
    assert not output.out and 'invalid prune plan' in output.err
