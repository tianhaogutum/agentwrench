"""Opt-in exact membership evidence must not change the growth gate."""
import json

from agentwrench.checkpoints.cli import main
from .test_budget_policy import accounting, save


def test_cli_new_zero_group_is_present_despite_zero_byte_fallback(tmp_path, capsys):
    before = save(tmp_path / 'b.json', accounting({}))
    after = save(tmp_path / 'c.json', accounting({('t', '', 'data'): 0}))
    assert main(['budget', str(before), str(after), '--max-channel-growth-bytes', '0',
                 '--show-group-presence', '--json']) == 0
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out) == {
        'passed': True, 'scope': {'thread': None}, 'comparisons': [{
            'group': 'channel', 'thread_id': 't', 'checkpoint_ns': '', 'channel': 'data',
            'baseline_bytes': 0, 'current_bytes': 0, 'growth_bytes': 0, 'limit_bytes': 0,
            'exceeded_bytes': 0, 'baseline_present': False, 'current_present': True}],
        'violations': []}

from copy import deepcopy
import pytest

from agentwrench.checkpoints.budget import budget_report
from .test_budget_limits import invoke


def artifacts():
    before = accounting({('', '', ''): 0, ('*', '', '*'): 0, ('gone', '', ''): 0,
                         ('t', '', 'data'): 0, ('t', 'child', 'data'): 0,
                         ('t', '', 'Data'): 7, ('t', '', 'é'): 0,
                         ('т\x1b', '子\n', 'data\t'): 1})
    after = accounting({('', '', ''): 0, ('*', '', '*'): 0, ('new', '', ''): 0,
                        ('t', '', 'data'): 3, ('T', '', 'data'): 0,
                        ('t', 'child', 'data'): 0, ('t', 'Child', 'data'): 0,
                        ('t', '', 'e\u0301'): 0, ('т\x1b', '子\n', 'data\t'): 4})
    # Channels can disappear independently while their thread remains, or appear
    # on a thread that previously had no channels. Thread bytes aren't channel sums.
    before['threads'] += [dict(thread_id=t, checkpoint_ns='', checkpoint_bytes=0, write_bytes=0, bytes=0)
                          for t in ['only-thread', 'gone-thread']]
    after['threads'] += [dict(thread_id=t, checkpoint_ns='', checkpoint_bytes=0, write_bytes=0, bytes=0)
                         for t in ['only-thread', 'new-thread']]
    value = {'format': 'lgckpt-scope.budget-policy/v1',
             'default': {'max_thread_growth_bytes': 0, 'max_channel_growth_bytes': 0},
             'threads': [{'thread_id': 't', 'checkpoint_ns': '', 'max_growth_bytes': 1}],
             'channels': [{'thread_id': 't', 'checkpoint_ns': '', 'channel': 'data', 'max_growth_bytes': 2}]}
    return before, after, value


def without_presence(report):
    result = deepcopy(report)
    for name in ['comparisons', 'violations']:
        result[name] = [{k: v for k, v in row.items() if k not in ('baseline_present', 'current_present')}
                        for row in result[name]]
    return result


@pytest.mark.parametrize('cap', [None, 0, 1, 5, 99])
@pytest.mark.parametrize('mode', ['policy', 'threads', 'channels', 'both'])
def test_exact_independent_presence_and_caps(tmp_path, capsys, cap, mode):
    before, after, policy = artifacts()
    original = deepcopy((before, after, policy))
    kwargs = {'policy': policy} if mode == 'policy' else {}
    flags = ['--show-group-presence']
    for kind in ['thread', 'channel']:
        if mode in (kind + 's', 'both'):
            kwargs['max_' + kind + '_growth_bytes'] = 0
            flags += ['--max-' + kind + '-growth-bytes', 0]
    default = budget_report(before, after, max_results=cap, **kwargs)
    full = budget_report(before, after, show_group_presence=True, **kwargs)
    for row in full['comparisons'] + full['violations']:
        name = row['group'] + 's'
        fields = ['thread_id', 'checkpoint_ns'] + (['channel'] if name == 'channels' else [])
        identity = tuple(row[f] for f in fields)
        for snapshot, label in [(before, 'baseline_present'), (after, 'current_present')]:
            expected = identity in {tuple(r[f] for f in fields) for r in snapshot[name]}
            assert type(row[label]) is bool and row[label] == expected
        assert row['baseline_present'] or row['current_present']
    if mode in ['policy', 'both']:
        rows = {(r['group'], r['thread_id'], r['checkpoint_ns'], r.get('channel')): r for r in full['comparisons']}
        assert rows['thread', 'only-thread', '', None]['baseline_present']
        assert rows['thread', 'only-thread', '', None]['current_present']
        assert not any(r.get('channel') is not None and r['thread_id'] == 'only-thread' for r in full['comparisons'])
        assert rows['channel', 't', '', 'é']['current_present'] is False
        assert rows['channel', 't', '', 'e\u0301']['baseline_present'] is False
        assert rows['channel', 't', '', 'Data']['current_present'] is False
        assert rows['channel', 't', '', 'data']['baseline_present'] is True
        assert rows['channel', 't', 'Child', 'data']['baseline_present'] is False
        assert rows['channel', 't', 'child', 'data']['baseline_present'] is True
    expected = deepcopy(full)
    if cap is not None:
        expected.update(max_results=cap, result_summary={})
        for name in ['comparisons', 'violations']:
            total = len(full[name])
            expected[name] = full[name][:cap]
            expected['result_summary'][name] = dict(total=total, shown=min(total, cap), truncated=total > cap)
    assert without_presence(expected) == default
    assert (before, after, policy) == original
    for snapshot in (before, after, policy):
        for name in ['threads', 'channels']:
            snapshot[name].reverse()
    code, output = invoke(tmp_path, capsys, before, after, policy if mode == 'policy' else None, flags, cap)
    assert code == 3 and not output.err and json.loads(output.out) == expected
    if mode == 'policy':
        # Thread replacement does not affect the channel override.
        violation = next(r for r in full['violations'] if r.get('channel') == 'data')
        assert violation['limit_source'] == 'override' and violation['limit_bytes'] == 2
        assert violation['exceeded_bytes'] == 1


@pytest.mark.parametrize('cap', [None, 0, 1, 99])
@pytest.mark.parametrize('empty', [False, True])
def test_passing_zero_membership_is_not_a_membership_budget(tmp_path, capsys, cap, empty):
    before = accounting({}) if empty else accounting({('gone', '', ''): 0, ('common', '', ''): 0})
    after = accounting({}) if empty else accounting({('new', '', ''): 0, ('common', '', ''): 0})
    code, output = invoke(tmp_path, capsys, before, after,
                          flags=['--show-group-presence', '--max-channel-growth-bytes', 0], limit=cap)
    result = json.loads(output.out)
    assert code == 0 and not output.err and result['passed'] and result['violations'] == []
    full = budget_report(before, after, max_channel_growth_bytes=0, show_group_presence=True)
    assert [(r['thread_id'], r['baseline_present'], r['current_present']) for r in full['comparisons']] == (
        [] if empty else [('common', True, True), ('gone', True, False), ('new', False, True)])
    if cap == 0:
        assert all(not result[name] for name in ['comparisons', 'violations'])
        assert result == budget_report(before, after, max_channel_growth_bytes=0, max_results=0)


def test_presence_can_be_channel_new_on_existing_thread_and_scoped(tmp_path, capsys):
    before = accounting({('t', '', 'gone'): 0})
    after = accounting({('t', '', 'new'): 1})
    before['scope']['thread'] = after['scope']['thread'] = 't'
    report = budget_report(before, after, max_thread_growth_bytes=0, max_channel_growth_bytes=0,
                           show_group_presence=True)
    assert [(r['baseline_present'], r['current_present']) for r in report['comparisons']] == [
        (True, True), (True, False), (False, True)]
    assert [(r['baseline_present'], r['current_present']) for r in report['violations']] == [
        (True, True), (False, True)]


@pytest.mark.parametrize('cap', [None, 0, 1, 99])
def test_human_presence_exact_and_ascii_escaped(tmp_path, capsys, cap):
    before = accounting({})
    after = accounting({('t\x1bé', '子\n', '*\t'): 1})
    flags = ['--max-channel-growth-bytes', 0]
    code, default = invoke(tmp_path, capsys, before, after, flags=flags, limit=cap, human=True)
    code, output = invoke(tmp_path, capsys, before, after, flags=[*flags, '--show-group-presence'], limit=cap, human=True)
    assert code == 3 and not output.err and output.out.isascii()
    suffix = '; baseline_present: false, current_present: true'
    assert output.out.replace(suffix, '') == default.out
    assert output.out.count(suffix) == (1 if cap is None else 0 if cap == 0 else 2)
    if cap != 0:
        assert 'channel "t\\u001b\\u00e9" "\\u5b50\\n" "*\\t": 0 -> 1 bytes' in output.out
    if cap is None:
        assert output.out == ('growth budget: EXCEEDED\n'
            '  channel "t\\u001b\\u00e9" "\\u5b50\\n" "*\\t": 0 -> 1 bytes; '
            'growth +1, limit 0, exceeded by 1 bytes' + suffix + '\n')


@pytest.mark.parametrize('cap', [0, 1, 99])
def test_coverage_uncapped_and_strict_refusal_equal(tmp_path, capsys, cap):
    before, after, policy = artifacts()
    for t in ['gone', 'new', 'only-thread', 'unused\x1b']:
        policy['threads'].append(dict(thread_id=t, checkpoint_ns='', max_growth_bytes=0))
    policy['channels'].append(dict(thread_id='only-thread', checkpoint_ns='', channel='', max_growth_bytes=0))
    flags = ['--show-policy-coverage']
    code, default = invoke(tmp_path, capsys, before, after, policy, flags, cap)
    code, output = invoke(tmp_path, capsys, before, after, policy, [*flags, '--show-group-presence'], cap)
    result = json.loads(output.out)
    assert code == 3 and without_presence(result) == json.loads(default.out)
    assert result['policy_coverage']['threads']['covered_overrides'] == 4
    assert result['policy_coverage']['threads']['unused_overrides'] == 1
    assert result['policy_coverage']['channels']['unused_selectors'] == [
        dict(thread_id='only-thread', checkpoint_ns='', channel='')]
    for human in [False, True]:
        flags = ['--show-policy-coverage', '--require-policy-match']
        code, default = invoke(tmp_path, capsys, before, after, policy, flags, cap, human)
        code, output = invoke(tmp_path, capsys, before, after, policy, [*flags, '--show-group-presence'], cap, human)
        assert code == 2 and not output.out and output.err == default.err and '\\u001b' in output.err


@pytest.mark.parametrize('cap', [0, 1])
@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('bad', ['scope', 'snapshot-row', 'snapshot-duplicate', 'snapshot-json', 'channel-without-thread',
                                'policy-duplicate', 'policy-json', 'mixed-limits', 'missing-limits', 'coverage-no-policy'])
def test_presence_preserves_refusals_with_empty_stdout(tmp_path, capsys, cap, human, bad):
    before, after, policy = artifacts()
    flags = ['--show-group-presence']
    if bad == 'scope':
        after = accounting({})
        after['scope']['thread'] = 't'
    elif bad == 'snapshot-row':
        after['channels'][-1]['bytes'] += 1
    elif bad == 'snapshot-duplicate':
        after['threads'].append(after['threads'][-1])
    elif bad == 'channel-without-thread':
        after['threads'] = []
    elif bad == 'policy-duplicate':
        policy['channels'].append(policy['channels'][-1])
    elif bad == 'mixed-limits':
        flags += ['--max-thread-growth-bytes', 0]
    elif bad in ['missing-limits', 'coverage-no-policy']:
        policy = None
        if bad == 'coverage-no-policy':
            flags += ['--show-policy-coverage']
    b, c, p = [save(tmp_path / n, v) for n, v in [('b.json', before), ('c.json', after), ('p.json', policy)]]
    if bad in ['snapshot-json', 'policy-json']:
        (c if bad == 'snapshot-json' else p).write_text('{"format":1,"format":2}')
    contents = [f.read_bytes() for f in [b, c, p]]
    args = ['budget', str(b), str(c), '--max-results', str(cap), *map(str, flags)]
    if policy is not None:
        args += ['--policy', str(p)]
    if not human:
        args += ['--json']
    assert main(args) == 2
    output = capsys.readouterr()
    assert not output.out and 'error:' in output.err and 'Traceback' not in output.err
    assert [f.read_bytes() for f in [b, c, p]] == contents


@pytest.mark.parametrize('cap', [0, 1])
@pytest.mark.parametrize('resource', ['MAX_POLICY_BYTES', 'MAX_OVERRIDES', 'MAX_SNAPSHOT_BYTES', 'MAX_GROUPS'])
def test_presence_keeps_resource_validation_and_artifact_only_access(tmp_path, monkeypatch, capsys, cap, resource):
    import builtins
    import sqlite3
    import agentwrench.checkpoints.budget as module
    import agentwrench.checkpoints.cli as cli

    def forbidden(*args, **kwargs):
        pytest.fail('budget must not access DB or graph')

    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == 'langgraph.graph' or name.startswith('langgraph.graph.'):
            forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(cli, 'connect', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    before, after, policy = artifacts()
    b, c, p = [save(tmp_path / n, v) for n, v in [('b.json', before), ('c.json', after), ('p.json', policy)]]
    contents = [f.read_bytes() for f in [b, c, p]]
    boundary = {'MAX_POLICY_BYTES': p.stat().st_size, 'MAX_OVERRIDES': 2,
                'MAX_SNAPSHOT_BYTES': max(b.stat().st_size, c.stat().st_size),
                'MAX_GROUPS': max(len(v['threads']) + len(v['channels']) for v in [before, after])}[resource]
    monkeypatch.setattr(module, resource, boundary)
    args = ['budget', str(b), str(c), '--policy', str(p), '--show-group-presence', '--max-results', str(cap), '--json']
    assert main(args) == 3
    assert not json.loads(capsys.readouterr().out)['passed']
    monkeypatch.setattr(module, resource, boundary - 1)
    assert main(args) == 2
    output = capsys.readouterr()
    assert not output.out and 'error:' in output.err and 'Traceback' not in output.err
    assert [f.read_bytes() for f in [b, c, p]] == contents


@pytest.mark.parametrize('args', [
    ['budget', 'b', 'c', '--max-thread-growth-bytes', '0', '--show-group-presence=false'],
    ['snapshot', 'absent.db', '--show-group-presence']])
def test_presence_switch_has_no_value_and_is_budget_only(capsys, args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and ('ignored explicit argument' if args[0] == 'budget' else 'unrecognized arguments') in output.err
