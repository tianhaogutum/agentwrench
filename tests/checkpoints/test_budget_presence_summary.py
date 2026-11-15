"""Complete opt-in membership counts, independent of displayed evidence."""
import json

from agentwrench.checkpoints.cli import main
from .test_budget_policy import accounting, save


def test_cli_summary_counts_zero_groups_even_with_no_rows(tmp_path, capsys):
    before = save(tmp_path / 'b.json', accounting({('gone', '', ''): 0, ('common', '', ''): 0}))
    after = save(tmp_path / 'c.json', accounting({('new', '', ''): 0, ('common', '', ''): 0}))
    assert main(['budget', str(before), str(after), '--max-channel-growth-bytes', '0',
                 '--show-group-presence-summary', '--max-results', '0', '--json']) == 0
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out) == {
        'passed': True, 'scope': {'thread': None}, 'comparisons': [], 'violations': [],
        'max_results': 0, 'result_summary': {
            'comparisons': {'total': 3, 'shown': 0, 'truncated': True},
            'violations': {'total': 0, 'shown': 0, 'truncated': False}},
        'group_presence_summary': {'channels': {
            'baseline': 2, 'current': 2, 'added': 1, 'removed': 1, 'common': 1}}}

from copy import deepcopy
from hashlib import sha256

import pytest

from agentwrench.checkpoints.budget import budget_report
from .test_budget_limits import invoke
from .test_budget_presence import artifacts


EXPECTED = {
    'threads': dict(baseline=8, current=10, added=4, removed=2, common=6),
    'channels': dict(baseline=8, current=9, added=4, removed=3, common=5)}


@pytest.mark.parametrize('cap', [None, 0, 1, 99])
@pytest.mark.parametrize('mode', ['policy', 'threads', 'channels', 'both'])
@pytest.mark.parametrize('rows', [False, True])
def test_complete_independent_counts_and_unchanged_gate(tmp_path, capsys, cap, mode, rows):
    before, after, policy = artifacts()
    original = deepcopy((before, after, policy))
    kwargs = {'policy': policy} if mode == 'policy' else {}
    flags = ['--show-group-presence-summary']
    kinds = ['threads', 'channels'] if mode in ['policy', 'both'] else [mode]
    for kind in kinds:
        if mode != 'policy':
            kwargs['max_' + kind[:-1] + '_growth_bytes'] = 0
            flags += ['--max-' + kind[:-1] + '-growth-bytes', 0]
    if rows:
        flags += ['--show-group-presence']
    default = budget_report(before, after, show_group_presence=rows, max_results=cap, **kwargs)
    result = budget_report(before, after, show_group_presence=rows, max_results=cap,
                           show_group_presence_summary=True, **kwargs)
    assert result == {**default, 'group_presence_summary': {k: EXPECTED[k] for k in kinds}}
    assert all(type(count) is int for counts in result['group_presence_summary'].values() for count in counts.values())
    assert (before, after, policy) == original
    code, output = invoke(tmp_path, capsys, before, after, policy if mode == 'policy' else None, flags, cap)
    assert code == 3 and not output.err and json.loads(output.out) == result
    # Full union counts, never a prefix or counts of just the displayed violations.
    full = budget_report(before, after, **kwargs)
    assert len(full['comparisons']) == 12 * len(kinds)
    if cap is not None:
        for name in ['comparisons', 'violations']:
            assert result['result_summary'][name] == dict(
                total=len(full[name]), shown=min(cap, len(full[name])), truncated=cap < len(full[name]))
    if mode == 'policy':
        violation = next(r for r in full['violations'] if r.get('channel') == 'data')
        assert violation['limit_source'] == 'override' and violation['limit_bytes'] == 2
        assert violation['exceeded_bytes'] == 1


@pytest.mark.parametrize('cap', [None, 0, 1])
@pytest.mark.parametrize('case', ['empty', 'thread-only', 'scoped'])
def test_empty_thread_only_and_scoped_zero_changes_pass(tmp_path, capsys, cap, case):
    before, after = accounting({}), accounting({})
    if case == 'thread-only':
        for snapshot, names in [(before, ['common', 'gone']), (after, ['common', 'new'])]:
            snapshot['threads'] = [dict(thread_id=t, checkpoint_ns='', checkpoint_bytes=0, write_bytes=0, bytes=0)
                                   for t in names]
    elif case == 'scoped':
        before, after = accounting({('t', '', 'gone'): 0}), accounting({('t', '', 'new'): 0})
        before['scope']['thread'] = after['scope']['thread'] = 't'
    code, output = invoke(tmp_path, capsys, before, after, flags=[
        '--max-thread-growth-bytes', 0, '--max-channel-growth-bytes', 0, '--show-group-presence-summary'], limit=cap)
    result = json.loads(output.out)
    zero = dict(baseline=0, current=0, added=0, removed=0, common=0)
    expected = {'threads': zero, 'channels': zero}
    if case == 'thread-only':
        expected['threads'] = dict(baseline=2, current=2, added=1, removed=1, common=1)
    elif case == 'scoped':
        expected = {'threads': dict(baseline=1, current=1, added=0, removed=0, common=1),
                    'channels': dict(baseline=1, current=1, added=1, removed=1, common=0)}
    assert code == 0 and not output.err and result['passed'] and not result['violations']
    assert result['group_presence_summary'] == expected


@pytest.mark.parametrize('cap', [None, 0, 1, 99])
@pytest.mark.parametrize('rows', [False, True])
def test_human_exact_summary_independent_of_caps_rows_and_coverage(tmp_path, capsys, cap, rows):
    before, after, policy = artifacts()
    policy['channels'].append(dict(thread_id='unused\x1b', checkpoint_ns='', channel='子', max_growth_bytes=0))
    flags = ['--show-policy-coverage'] + (['--show-group-presence'] if rows else [])
    code, default = invoke(tmp_path, capsys, before, after, policy, flags, cap, True)
    code, output = invoke(tmp_path, capsys, before, after, policy,
                          [*flags, '--show-group-presence-summary'], cap, True)
    block = ('group presence summary:\n'
             '  threads: 8 baseline, 10 current, 4 added, 2 removed, 6 common\n'
             '  channels: 8 baseline, 9 current, 4 added, 3 removed, 5 common\n')
    assert code == 3 and not output.err and output.out.isascii()
    assert output.out.count(block) == 1 and output.out.replace(block, '') == default.out
    assert block + 'policy coverage:\n' in output.out
    if cap == 0:
        assert 'baseline_present:' not in output.out


@pytest.mark.parametrize('cap', [0, 1])
@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('bad', ['scope', 'snapshot-row', 'snapshot-duplicate', 'snapshot-json', 'orphan',
                                'policy-duplicate', 'policy-json', 'mixed', 'missing', 'strict'])
def test_summary_preserves_full_refusals_and_hashes(tmp_path, capsys, cap, human, bad):
    before, after, policy = artifacts()
    flags = []
    if bad == 'scope':
        after = accounting({})
        after['scope']['thread'] = 't'
    elif bad == 'snapshot-row':
        after['channels'][-1]['bytes'] += 1
    elif bad == 'snapshot-duplicate':
        after['threads'].append(after['threads'][-1])
    elif bad == 'orphan':
        after['threads'] = []
    elif bad == 'policy-duplicate':
        policy['channels'].append(policy['channels'][-1])
    elif bad == 'mixed':
        flags += ['--max-thread-growth-bytes', '0']
    elif bad == 'missing':
        policy = None
    elif bad == 'strict':
        policy['channels'].append(dict(thread_id='only-thread', checkpoint_ns='', channel='', max_growth_bytes=0))
        flags += ['--require-policy-match', '--show-policy-coverage']
    paths = [save(tmp_path / n, v) for n, v in [('b.json', before), ('c.json', after), ('p.json', policy)]]
    b, c, p = paths
    if bad in ['snapshot-json', 'policy-json']:
        (c if bad == 'snapshot-json' else p).write_text('{"format":1,"format":2}')
    hashes = lambda: [sha256(f.read_bytes()).hexdigest() for f in paths]
    initial = hashes()
    args = ['budget', str(b), str(c), '--max-results', str(cap), *flags]
    if policy is not None:
        args += ['--policy', str(p)]
    if not human:
        args += ['--json']
    assert main(args) == 2
    default = capsys.readouterr()
    assert main([*args, '--show-group-presence-summary', '--show-group-presence']) == 2
    output = capsys.readouterr()
    assert not output.out and output.err == default.err and 'error:' in output.err and 'Traceback' not in output.err
    assert hashes() == initial


@pytest.mark.parametrize('resource', ['MAX_POLICY_BYTES', 'MAX_OVERRIDES', 'MAX_SNAPSHOT_BYTES', 'MAX_GROUPS'])
def test_summary_resource_boundaries_and_no_db_graph(tmp_path, capsys, monkeypatch, resource):
    # Reuse the established boundary/DB/graph guard, adding the summary flag to
    # every invocation (boundary success and boundary-minus-one refusal).
    from . import test_budget_presence as presence
    old_main = presence.main
    def with_summary(args):
        return old_main([*args, '--show-group-presence-summary'])
    monkeypatch.setattr(presence, 'main', with_summary)
    presence.test_presence_keeps_resource_validation_and_artifact_only_access(
        tmp_path, monkeypatch, capsys, 0, resource)


@pytest.mark.parametrize('args', [
    ['budget', 'b', 'c', '--max-thread-growth-bytes', '0', '--show-group-presence-summary=false'],
    ['snapshot', 'absent.db', '--show-group-presence-summary']])
def test_summary_switch_has_no_value_and_is_budget_only(capsys, args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and ('ignored explicit argument' if args[0] == 'budget' else 'unrecognized arguments') in output.err
