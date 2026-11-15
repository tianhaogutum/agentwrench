"""Opt-in display limits never reduce complete budget evaluation or validation."""
import json

from agentwrench.checkpoints.cli import main
from .test_budget_policy import accounting, save


def test_zero_limit_keeps_hidden_failure_and_independent_totals(tmp_path, capsys):
    before = save(tmp_path / 'before.json', accounting({('t', '', 'data'): 1}))
    after = save(tmp_path / 'after.json', accounting({('t', '', 'data'): 4}))
    assert main(['budget', str(before), str(after), '--max-channel-growth-bytes', '0',
                 '--max-results', '0', '--json']) == 3
    output = capsys.readouterr()
    assert not output.err
    result = json.loads(output.out)
    assert result == {
        'passed': False, 'scope': {'thread': None}, 'comparisons': [], 'violations': [],
        'max_results': 0, 'result_summary': {
            'comparisons': {'total': 1, 'shown': 0, 'truncated': True},
            'violations': {'total': 1, 'shown': 0, 'truncated': True}}}

from copy import deepcopy

import pytest

from agentwrench.checkpoints.budget import budget_report
from agentwrench.checkpoints.db import ScopeError


def artifacts():
    before = accounting({('', '', ''): 0, ('*', '', '*'): 1, ('T', '', 'data'): 1,
                         ('removed', 'child', 'data'): 9, ('t', '', 'Data'): 1,
                         ('t', '', 'data'): 1, ('t', 'child', 'data'): 1,
                         ('т\x1b', '子\n', 'data\t'): 1})
    after = accounting({('', '', ''): 0, ('*', '', '*'): 4, ('T', '', 'data'): 4,
                        ('new', '', ''): 0, ('t', '', 'Data'): 4, ('t', '', 'data'): 4,
                        ('t', 'child', 'data'): 4, ('т\x1b', '子\n', 'data\t'): 4})
    value = {'format': 'lgckpt-scope.budget-policy/v1',
             'default': {'max_thread_growth_bytes': 100, 'max_channel_growth_bytes': 2},
             'threads': [{'thread_id': 't', 'checkpoint_ns': '', 'max_growth_bytes': 0}],
             'channels': [{'thread_id': '*', 'checkpoint_ns': '', 'channel': '*', 'max_growth_bytes': 3},
                          {'thread_id': 't', 'checkpoint_ns': '', 'channel': 'data', 'max_growth_bytes': 0}]}
    return before, after, value


def invoke(tmp_path, capsys, before, after, policy=None, flags=(), limit=None, human=False):
    b, c = save(tmp_path / 'b.json', before), save(tmp_path / 'c.json', after)
    args = ['budget', str(b), str(c), *map(str, flags)]
    if policy is not None:
        args += ['--policy', str(save(tmp_path / 'p.json', policy))]
    if limit is not None:
        args += ['--max-results', str(limit)]
    if not human:
        args += ['--json']
    code = main(args)
    return code, capsys.readouterr()


@pytest.mark.parametrize('limit', [0, 1, 5, 8, 9, 17, 18, 99])
@pytest.mark.parametrize('mode', ['policy', 'threads', 'channels', 'both'])
def test_independent_exact_sorted_prefixes(tmp_path, capsys, limit, mode):
    before, after, policy = artifacts()
    kwargs = {'policy': policy} if mode == 'policy' else {}
    flags = []
    if mode in ('threads', 'both'):
        kwargs['max_thread_growth_bytes'] = 2
        flags += ['--max-thread-growth-bytes', 2]
    if mode in ('channels', 'both'):
        kwargs['max_channel_growth_bytes'] = 2
        flags += ['--max-channel-growth-bytes', 2]
    full = budget_report(before, after, **kwargs)
    original = deepcopy((before, after, policy))
    # First comparison passes; violations are an independent prefix, not filtered shown comparisons.
    assert full['comparisons'][0]['growth_bytes'] == 0
    assert len(full['comparisons']) == ({'policy': 17, 'both': 17, 'threads': 8, 'channels': 9}[mode])
    assert len(full['violations']) == {'policy': 6, 'threads': 5, 'channels': 6, 'both': 11}[mode]
    for row in full['comparisons']:
        assert row['exceeded_bytes'] == max(0, row['growth_bytes'] - row['limit_bytes'])
        if row['thread_id'] == 'removed':
            assert row['growth_bytes'] == -9 and row['current_bytes'] == 0
    keys = [(r['group'], r['thread_id'], r['checkpoint_ns'], r.get('channel', '')) for r in full['comparisons']]
    assert keys == sorted(keys, key=lambda k: (k[0] == 'channel', *k[1:]))
    if mode == 'policy':
        thread = next(r for r in full['comparisons'] if r['group'] == 'thread' and r['thread_id'] == 't' and r['checkpoint_ns'] == '')
        channel = next(r for r in full['comparisons'] if r.get('channel') == 'data' and r['thread_id'] == 't' and r['checkpoint_ns'] == '')
        assert thread['limit_source'] == channel['limit_source'] == 'override'
        assert thread['limit_bytes'] == channel['limit_bytes'] == 0
        assert thread['exceeded_bytes'] == 6 and channel['exceeded_bytes'] == 3
    expected = {**full, 'max_results': limit, 'result_summary': {}}
    for name in ('comparisons', 'violations'):
        total = len(full[name])
        expected[name] = full[name][:limit]
        expected['result_summary'][name] = {'total': total, 'shown': min(total, limit), 'truncated': total > limit}
    assert budget_report(before, after, max_results=limit, **kwargs) == expected
    assert (before, after, policy) == original
    for artifact in (before, after, policy):
        for name in ('threads', 'channels'):
            artifact[name].reverse()
    assert budget_report(before, after, max_results=limit, **kwargs) == expected
    code, output = invoke(tmp_path, capsys, before, after, policy if mode == 'policy' else None, flags, limit)
    assert code == 3 and not output.err and json.loads(output.out) == expected


@pytest.mark.parametrize('limit', [0, 1, 99])
@pytest.mark.parametrize('empty', [False, True])
def test_passing_removed_and_zero_groups(tmp_path, capsys, limit, empty):
    before = accounting({}) if empty else accounting({('gone', '', ''): 4, ('zero', '', ''): 0})
    after = accounting({}) if empty else accounting({('zero', '', ''): 0, ('new', '', ''): 0})
    code, output = invoke(tmp_path, capsys, before, after, flags=['--max-channel-growth-bytes', 0], limit=limit)
    result = json.loads(output.out)
    assert code == 0 and not output.err and result['passed']
    assert result['violations'] == []
    assert result['result_summary']['violations'] == {'total': 0, 'shown': 0, 'truncated': False}
    assert result['result_summary']['comparisons'] == {'total': 0 if empty else 3,
        'shown': 0 if empty else min(3, limit), 'truncated': not empty and limit < 3}


def test_human_contract_and_default_compatibility(tmp_path, capsys):
    before = accounting({('т\x1b', '子\n', 'data\t'): 1})
    after = accounting({('т\x1b', '子\n', 'data\t'): 4})
    flags = ['--max-channel-growth-bytes', 0]
    row = '  channel "\\u0442\\u001b" "\\u5b50\\n" "data\\t": 1 -> 4 bytes; growth +3, limit 0, exceeded by 3 bytes\n'
    code, output = invoke(tmp_path, capsys, before, after, flags=flags, human=True)
    assert code == 3 and not output.err and output.out == 'growth budget: EXCEEDED\n' + row
    code, output = invoke(tmp_path, capsys, before, after, flags=flags, limit=1, human=True)
    assert code == 3 and output.out.isascii() and '\x1b' not in output.out
    assert output.out == ('growth budget: EXCEEDED\ncomparisons: 1 total, 1 shown, truncated: false\n' + row +
                          'violations: 1 total, 1 shown, truncated: false\n' + row)
    code, output = invoke(tmp_path, capsys, before, after, flags=flags, limit=0, human=True)
    assert code == 3 and output.out == ('growth budget: EXCEEDED\n'
        'comparisons: 1 total, 0 shown, truncated: true\nviolations: 1 total, 0 shown, truncated: true\n')
    code, output = invoke(tmp_path, capsys, before, after, flags=flags)
    full = json.loads(output.out)
    assert set(full) == {'passed', 'scope', 'comparisons', 'violations'}
    assert len(full['comparisons']) == len(full['violations']) == 1


@pytest.mark.parametrize('limit', [0, 1, 99])
def test_policy_coverage_is_complete_and_strict_refusal_unchanged(tmp_path, capsys, limit):
    before, after, policy = artifacts()
    # Removed/zero-byte groups still cover overrides even if their comparisons are hidden.
    policy['channels'] += [{'thread_id': t, 'checkpoint_ns': ns, 'channel': ch, 'max_growth_bytes': 0}
                          for t, ns, ch in [('removed', 'child', 'data'), ('', '', ''), ('new', '', '')]]
    policy['channels'] += [{'thread_id': 'unused\x1b', 'checkpoint_ns': '子', 'channel': str(i), 'max_growth_bytes': 0}
                          for i in range(4)]
    full = budget_report(before, after, policy=policy, show_policy_coverage=True)
    assert full['policy_coverage']['channels']['covered_overrides'] == 5
    assert full['policy_coverage']['channels']['unused_overrides'] == 4
    code, output = invoke(tmp_path, capsys, before, after, policy, ['--show-policy-coverage'], limit)
    assert code == 3 and json.loads(output.out)['policy_coverage'] == full['policy_coverage']
    code, output = invoke(tmp_path, capsys, before, after, policy, ['--show-policy-coverage'], limit, human=True)
    assert code == 3 and output.out.count('    unused:') == 4 and output.out.isascii()
    flags = ['--show-policy-coverage', '--require-policy-match']
    code, uncapped = invoke(tmp_path, capsys, before, after, policy, flags)
    assert code == 2 and not uncapped.out
    code, capped = invoke(tmp_path, capsys, before, after, policy, flags, limit)
    assert code == 2 and not capped.out and capped.err == uncapped.err
    assert 'unused budget policy channel selector' in capped.err and '\\u001b' in capped.err


@pytest.mark.parametrize('value', ['', '-1', '+1', '1.0', '1e2', ' 1', '1 ', '１', '١', 'true', 'None', '9' * 5000])
def test_bad_cli_limit_refused_before_reads(tmp_path, monkeypatch, capsys, value):
    import agentwrench.checkpoints.cli as cli
    monkeypatch.setattr(cli, 'load_snapshot', lambda *a: pytest.fail('must parse limit before input reads'))
    with pytest.raises(SystemExit) as error:
        main(['budget', 'missing', 'missing', '--max-channel-growth-bytes', '0', '--max-results=' + value, '--json'])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and 'nonnegative ASCII decimal integer' in output.err and 'Traceback' not in output.err


@pytest.mark.parametrize('limit', [False, True, -1, 1.0, '0', [], {}])
def test_bad_api_limit_is_scope_error(limit):
    with pytest.raises(ScopeError, match='--max-results must be a nonnegative integer'):
        budget_report(None, None, max_results=limit)


def test_leading_zero_and_missing_limit(tmp_path, capsys):
    empty = save(tmp_path / 'empty.json', accounting({}))
    assert main(['budget', str(empty), str(empty), '--max-thread-growth-bytes', '0', '--max-results', '000', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['max_results'] == 0
    with pytest.raises(SystemExit) as error:
        main(['budget', str(empty), str(empty), '--max-results'])
    assert error.value.code == 2
    assert not capsys.readouterr().out


@pytest.mark.parametrize('limit', [0, 1])
@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('bad', ['scope', 'snapshot-row', 'snapshot-duplicate', 'snapshot-json', 'policy-duplicate',
                                  'policy-json', 'mixed-limits', 'missing-policy', 'missing-limits', 'coverage-no-policy'])
def test_no_partial_report_on_invalid_input(tmp_path, capsys, limit, human, bad):
    before, after, policy = artifacts()
    flags = []
    expected = 'invalid growth snapshot'
    if bad == 'scope':
        after = accounting({})
        after['scope']['thread'] = 't'
        expected = 'incompatible growth snapshot scopes'
    elif bad == 'snapshot-row':
        after['channels'][-1]['bytes'] += 1
    elif bad == 'snapshot-duplicate':
        after['threads'].append(after['threads'][-1])
    elif bad == 'policy-duplicate':
        policy['channels'].append(policy['channels'][-1])
        expected = 'invalid budget policy'
    elif bad == 'mixed-limits':
        flags = ['--max-thread-growth-bytes', 0]
        expected = 'cannot be combined'
    elif bad == 'missing-limits':
        policy = None
        expected = 'budget requires'
    elif bad == 'coverage-no-policy':
        policy = None
        flags = ['--show-policy-coverage']
        expected = '--show-policy-coverage requires --policy'
    b, c = save(tmp_path / 'b.json', before), save(tmp_path / 'c.json', after)
    p = save(tmp_path / 'p.json', policy)
    if bad == 'snapshot-json':
        c.write_text('{"format":1,"format":2}')
    elif bad == 'policy-json':
        p.write_text('{"format":1,"format":2}')
        expected = 'invalid budget policy JSON'
    elif bad == 'missing-policy':
        p.unlink()
        expected = 'file operation failed'
    files = [f for f in (b, c, p) if f.exists()]
    contents = [f.read_bytes() for f in files]
    args = ['budget', str(b), str(c), '--max-results', str(limit), *map(str, flags)]
    if policy is not None:
        args += ['--policy', str(p)]
    if not human:
        args += ['--json']
    assert main(args) == 2
    output = capsys.readouterr()
    assert not output.out and expected in output.err and 'Traceback' not in output.err
    assert [f.read_bytes() for f in files] == contents


@pytest.mark.parametrize('cap', [0, 1])
@pytest.mark.parametrize('resource', ['MAX_POLICY_BYTES', 'MAX_OVERRIDES', 'MAX_SNAPSHOT_BYTES', 'MAX_GROUPS'])
def test_complete_resource_validation_even_at_zero(tmp_path, monkeypatch, capsys, cap, resource):
    import sqlite3
    import agentwrench.checkpoints.budget as module
    import agentwrench.checkpoints.cli as cli

    def forbidden(*args, **kwargs):
        pytest.fail('budget cannot access DB or graph')

    monkeypatch.setattr(cli, 'connect', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    before, after, policy = artifacts()
    b, c, p = [save(tmp_path / name, value) for name, value in [('b.json', before), ('c.json', after), ('p.json', policy)]]
    inputs = [b.read_bytes(), c.read_bytes(), p.read_bytes()]
    boundary = {'MAX_POLICY_BYTES': p.stat().st_size, 'MAX_OVERRIDES': 3,
                'MAX_SNAPSHOT_BYTES': max(b.stat().st_size, c.stat().st_size), 'MAX_GROUPS': 15}[resource]
    monkeypatch.setattr(module, resource, boundary)
    args = ['budget', str(b), str(c), '--policy', str(p), '--max-results', str(cap), '--json']
    assert main(args) == 3
    result = json.loads(capsys.readouterr().out)
    assert result['result_summary']['comparisons']['total'] == 17
    monkeypatch.setattr(module, resource, boundary - 1)
    assert main(args) == 2
    output = capsys.readouterr()
    assert not output.out and 'error:' in output.err and 'Traceback' not in output.err
    assert [b.read_bytes(), c.read_bytes(), p.read_bytes()] == inputs
