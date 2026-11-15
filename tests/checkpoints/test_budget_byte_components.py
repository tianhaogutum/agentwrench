"""Component evidence explains total decisions, including offsetting changes."""
import json

from agentwrench.checkpoints.cli import main
from .test_budget_policy import accounting, save


def test_cli_byte_components_option_and_offset_contract(tmp_path, capsys):
    before = accounting({('t', '', 'data'): 0})
    after = accounting({('t', '', 'data'): 0})
    for value, cp, channel, write in [(before, 10, 3, 1), (after, 2, 1, 9)]:
        value['threads'][0].update(checkpoint_bytes=cp, write_bytes=write, bytes=cp + write)
        value['channels'][0].update(checkpoint_value_bytes=channel, write_bytes=write, bytes=channel + write)
    b, c = save(tmp_path / 'b.json', before), save(tmp_path / 'c.json', after)
    assert main(['budget', str(b), str(c), '--max-thread-growth-bytes', '0',
                 '--max-channel-growth-bytes', '0', '--show-byte-components', '--json']) == 3
    output = capsys.readouterr()
    assert not output.err
    report = json.loads(output.out)
    thread, channel = report['comparisons']
    assert thread['byte_components'] == {
        'checkpoint_bytes': {'baseline_bytes': 10, 'current_bytes': 2, 'growth_bytes': -8},
        'write_bytes': {'baseline_bytes': 1, 'current_bytes': 9, 'growth_bytes': 8}}
    assert channel['byte_components'] == {
        'checkpoint_value_bytes': {'baseline_bytes': 3, 'current_bytes': 1, 'growth_bytes': -2},
        'write_bytes': {'baseline_bytes': 1, 'current_bytes': 9, 'growth_bytes': 8}}
    assert thread['growth_bytes'] == 0 and thread['exceeded_bytes'] == 0
    assert channel['growth_bytes'] == channel['exceeded_bytes'] == 6
    assert report['violations'] == [channel]

from copy import deepcopy
import pytest

from agentwrench.checkpoints.budget import budget_report, validate_snapshot
from .test_budget_limits import invoke


def artifacts():
    def snapshot(groups):
        result = dict(format='lgckpt-scope.growth/v1', scope={'thread': None}, threads=[], channels=[])
        for (t, ns), (cp, channels) in groups.items():
            write = sum(w for v, w in channels.values())
            result['threads'].append(dict(thread_id=t, checkpoint_ns=ns, checkpoint_bytes=cp,
                                          write_bytes=write, bytes=cp + write))
            for ch, (v, w) in channels.items():
                result['channels'].append(dict(thread_id=t, checkpoint_ns=ns, channel=ch,
                    checkpoint_value_bytes=v, write_bytes=w, bytes=v + w))
        validate_snapshot(result)
        return result
    before = snapshot({('', ''): (0, {'': (0, 0)}), ('gone', 'child'): (5, {'data': (2, 3)}),
        ('gone-zero', ''): (0, {'data': (0, 0)}), ('only-thread', ''): (7, {}),
        ('t', ''): (10, {'data': (3, 1), 'removed': (0, 0)}),
        ('t', 'child'): (1, {'data': (0, 10)}), ('T', ''): (0, {'Data': (0, 0)}),
        ('т\x1b', '子\n'): (2, {'é\t': (1, 4)})})
    after = snapshot({('', ''): (0, {'': (0, 0)}), ('new', ''): (5, {'data': (2, 3)}),
        ('new-zero', ''): (0, {'data': (0, 0)}), ('only-thread', ''): (8, {}),
        ('t', ''): (2, {'data': (1, 9), 'added': (0, 0)}),
        ('t', 'child'): (9, {'data': (12, 1)}), ('t', 'Child'): (0, {'Data': (0, 0)}),
        ('т\x1b', '子\n'): (1, {'e\u0301\t': (1, 5)})})
    policy = dict(format='lgckpt-scope.budget-policy/v1',
        default=dict(max_thread_growth_bytes=0, max_channel_growth_bytes=0),
        threads=[dict(thread_id='new', checkpoint_ns='', max_growth_bytes=8)],
        channels=[dict(thread_id='t', checkpoint_ns='', channel='data', max_growth_bytes=5)])
    return before, after, policy


def without_components(report):
    result = deepcopy(report)
    for name in ['comparisons', 'violations']:
        for row in result[name]:
            row.pop('byte_components', None)
    return result


@pytest.mark.parametrize('cap', [None, 0, 1, 7, 99])
@pytest.mark.parametrize('mode', ['policy', 'threads', 'channels', 'both'])
def test_independent_components_caps_and_unchanged_gate(tmp_path, capsys, cap, mode):
    before, after, policy = artifacts()
    original = deepcopy((before, after, policy))
    kwargs = dict(policy=policy) if mode == 'policy' else {}
    flags = ['--show-byte-components', '--show-group-presence', '--show-group-presence-summary']
    for kind in ['thread', 'channel']:
        if mode in (kind + 's', 'both'):
            kwargs['max_' + kind + '_growth_bytes'] = 0
            flags += ['--max-' + kind + '-growth-bytes', 0]
    if mode == 'policy':
        kwargs.update(show_policy_coverage=True, max_unused_selectors=0, require_policy_match=True)
        flags += ['--show-policy-coverage', '--max-unused-selectors', 0, '--require-policy-match']
    kwargs.update(show_group_presence=True, show_group_presence_summary=True)
    default = budget_report(before, after, max_results=cap, **kwargs)
    full = budget_report(before, after, show_byte_components=True, **kwargs)
    indexes = [validate_snapshot(s) for s in (before, after)]
    for row in full['comparisons'] + full['violations']:
        name = row['group'] + 's'
        fields = ['thread_id', 'checkpoint_ns'] + (['channel'] if name == 'channels' else [])
        identity = tuple(row[f] for f in fields)
        names = ['checkpoint_bytes' if name == 'threads' else 'checkpoint_value_bytes', 'write_bytes']
        assert list(row['byte_components']) == names
        for component, values in row['byte_components'].items():
            old, new = [i[name].get(identity, {}).get(component, 0) for i in indexes]
            assert values == dict(baseline_bytes=old, current_bytes=new, growth_bytes=new-old)
        for f in ['baseline_bytes', 'current_bytes', 'growth_bytes']:
            assert sum(v[f] for v in row['byte_components'].values()) == row[f]
    expected = deepcopy(full)
    if cap is not None:
        expected.update(max_results=cap, result_summary={})
        for name in ['comparisons', 'violations']:
            total = len(full[name])
            expected[name] = full[name][:cap]
            expected['result_summary'][name] = dict(total=total, shown=min(total, cap), truncated=total > cap)
    assert without_components(expected) == default
    assert (before, after, policy) == original
    for value in (before, after, policy):
        for name in ['threads', 'channels']:
            value[name].reverse()
    code, output = invoke(tmp_path, capsys, before, after, policy if mode == 'policy' else None, flags, cap)
    assert code == 3 and not output.err and json.loads(output.out) == expected
    if cap == 1:
        assert expected['comparisons'][0]['exceeded_bytes'] == 0
        assert expected['violations'][0]['exceeded_bytes'] > 0  # independent prefixes
    if mode == 'policy':
        row = next(r for r in full['comparisons'] if r['group'] == 'thread' and r['thread_id'] == 'new')
        assert row['limit_source'] == 'override' and row['growth_bytes'] == row['limit_bytes'] == 8
        row = next(r for r in full['violations'] if r.get('channel') == 'data' and r['checkpoint_ns'] == '' and r['thread_id'] == 't')
        assert row['limit_source'] == 'override' and row['limit_bytes'] == 5 and row['exceeded_bytes'] == 1


@pytest.mark.parametrize('kind', ['thread', 'channel'])
@pytest.mark.parametrize('cap', [None, 0, 1, 99])
def test_human_exact_suffix_and_default_identity(tmp_path, capsys, kind, cap):
    before = accounting({('t\x1bé', '子\n', 'data\t'): 0})
    after = deepcopy(before)
    for value, cp, channel, write in [(before, 10, 3, 1), (after, 2, 1, 9)]:
        value['threads'][0].update(checkpoint_bytes=cp, write_bytes=write, bytes=cp + write)
        value['channels'][0].update(checkpoint_value_bytes=channel, write_bytes=write, bytes=channel + write)
    flags = ['--max-' + kind + '-growth-bytes', 0, '--show-group-presence']
    code, default = invoke(tmp_path, capsys, before, after, flags=flags, limit=cap, human=True)
    actual, output = invoke(tmp_path, capsys, before, after, flags=[*flags, '--show-byte-components'], limit=cap, human=True)
    suffix = ('; byte_components: checkpoint_bytes 10 -> 2 bytes (growth -8)' if kind == 'thread' else
              '; byte_components: checkpoint_value_bytes 3 -> 1 bytes (growth -2)')
    suffix += ', write_bytes 1 -> 9 bytes (growth +8)'
    assert actual == code == (0 if kind == 'thread' else 3) and not output.err
    assert output.out.isascii() and '\x1b' not in output.out
    assert output.out.replace(suffix, '') == default.out
    assert output.out.count(suffix) == (0 if cap == 0 else 2 if cap is not None and kind == 'channel' else 1)


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('cap', [None, 0, 1])
def test_zero_and_empty_groups_still_pass(tmp_path, capsys, empty, cap):
    before = accounting({} if empty else {('gone', '', ''): 0})
    after = accounting({} if empty else {('new', '', ''): 0})
    flags = ['--show-byte-components', '--max-channel-growth-bytes', 0]
    code, output = invoke(tmp_path, capsys, before, after, flags=flags, limit=cap)
    assert code == 0 and not output.err
    report = json.loads(output.out)
    assert report['passed'] and not report['violations']
    for row in report['comparisons']:
        assert all(all(v == 0 for v in values.values()) for values in row['byte_components'].values())


@pytest.mark.parametrize('cap', [0, 1])
@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('bad', ['scope', 'missing-component', 'bool', 'negative', 'total', 'write-total',
    'duplicate-group', 'missing-parent', 'duplicate-selector', 'snapshot-json', 'policy-json',
    'strict-thread', 'strict-channel', 'mixed-limits', 'no-limits', 'negative-limit'])
def test_component_switch_preserves_complete_refusal(tmp_path, capsys, cap, human, bad):
    before, after, policy = artifacts()
    flags = ['--max-results', str(cap)]
    if bad == 'scope':
        after = accounting({}); after['scope']['thread'] = 't'
    elif bad == 'missing-component':
        del after['threads'][-1]['checkpoint_bytes']
    elif bad in ['bool', 'negative']:
        after['channels'][-1]['write_bytes'] = True if bad == 'bool' else -1
    elif bad == 'total':
        after['channels'][-1]['bytes'] += 1
    elif bad == 'write-total':
        after['channels'][-1]['write_bytes'] += 1; after['channels'][-1]['bytes'] += 1
    elif bad == 'duplicate-group':
        after['channels'].append(after['channels'][-1])
    elif bad == 'missing-parent':
        after['threads'] = []
    elif bad == 'duplicate-selector':
        policy['channels'].append(policy['channels'][-1])
    elif bad.startswith('strict-'):
        kind = bad.removeprefix('strict-')
        policy[kind+'s'].append(dict(thread_id='unused\x1bé', checkpoint_ns='', max_growth_bytes=0,
                                   **({'channel': 'data'} if kind == 'channel' else {})))
        flags += ['--require-policy-match', '--show-policy-coverage', '--max-unused-selectors', '0']
    elif bad == 'mixed-limits':
        flags += ['--max-thread-growth-bytes', '0']
    elif bad in ['no-limits', 'negative-limit']:
        policy = None
        if bad == 'negative-limit':
            flags += ['--max-channel-growth-bytes', '-1']
    b, c, p = [save(tmp_path / n, v) for n, v in [('b.json', before), ('c.json', after), ('p.json', policy)]]
    if bad in ['snapshot-json', 'policy-json']:
        (c if bad == 'snapshot-json' else p).write_text('{"format":1,"format":2}')
    contents = [f.read_bytes() for f in [b, c, p]]
    args = ['budget', str(b), str(c), *flags]
    if policy is not None:
        args += ['--policy', str(p)]
    if not human:
        args += ['--json']
    assert main(args) == 2
    default = capsys.readouterr()
    assert main([*args, '--show-byte-components']) == 2
    output = capsys.readouterr()
    assert not output.out and output == default and 'error:' in output.err and 'Traceback' not in output.err
    assert [f.read_bytes() for f in [b, c, p]] == contents


@pytest.mark.parametrize('cap', [0, 1])
@pytest.mark.parametrize('resource', ['MAX_POLICY_BYTES', 'MAX_OVERRIDES', 'MAX_SNAPSHOT_BYTES', 'MAX_GROUPS'])
def test_component_resource_boundaries_and_artifact_only(tmp_path, monkeypatch, capsys, cap, resource):
    import builtins
    import sqlite3
    import agentwrench.checkpoints.budget as module
    import agentwrench.checkpoints.cli as cli
    def forbidden(*args, **kwargs):
        pytest.fail('budget must not open DB or import graph')
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == 'langgraph.graph' or name.startswith('langgraph.graph.'):
            forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    monkeypatch.setattr(cli, 'connect', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    before, after, policy = artifacts()
    b, c, p = [save(tmp_path / n, v) for n, v in [('b.json', before), ('c.json', after), ('p.json', policy)]]
    contents = [f.read_bytes() for f in [b, c, p]]
    boundary = dict(MAX_POLICY_BYTES=p.stat().st_size, MAX_OVERRIDES=2,
        MAX_SNAPSHOT_BYTES=max(b.stat().st_size, c.stat().st_size),
        MAX_GROUPS=max(len(v['threads']) + len(v['channels']) for v in [before, after]))[resource]
    args = ['budget', str(b), str(c), '--policy', str(p), '--show-byte-components', '--max-results', str(cap), '--json']
    monkeypatch.setattr(module, resource, boundary)
    assert main(args) == 3
    assert not json.loads(capsys.readouterr().out)['passed']
    monkeypatch.setattr(module, resource, boundary - 1)
    assert main(args) == 2
    output = capsys.readouterr()
    assert not output.out and 'error:' in output.err and 'Traceback' not in output.err
    assert [f.read_bytes() for f in [b, c, p]] == contents


@pytest.mark.parametrize('args', [
    ['budget', 'b', 'c', '--max-thread-growth-bytes', '0', '--show-byte-components=false'],
    ['snapshot', 'absent.db', '--show-byte-components']])
def test_component_switch_valueless_and_budget_only(capsys, args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and ('ignored explicit argument' if args[0] == 'budget' else 'unrecognized arguments') in output.err
