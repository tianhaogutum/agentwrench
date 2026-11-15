"""Independent coverage prefixes retain full matching, validation and decisions."""
import json

from agentwrench.checkpoints.cli import main
from .test_budget_policy import accounting, policy, save


def test_parser_accepts_counts_only_coverage(tmp_path, capsys):
    empty = save(tmp_path / 'empty.json', accounting({}))
    p = save(tmp_path / 'policy.json', policy())
    assert main(['budget', str(empty), str(empty), '--policy', str(p), '--show-policy-coverage',
                 '--max-unused-selectors', '0', '--json']) == 0
    output = capsys.readouterr()
    assert not output.err
    result = json.loads(output.out)
    assert result['max_unused_selectors'] == 0
    assert result['policy_coverage']['channels'] == {
        'total_overrides': 2, 'covered_overrides': 0, 'unused_overrides': 2, 'unused_selectors': [],
        'unused_selectors_summary': {'total': 2, 'shown': 0, 'truncated': True}}
    assert result['policy_coverage']['threads']['unused_selectors_summary'] == {
        'total': 0, 'shown': 0, 'truncated': False}

from copy import deepcopy

import pytest

from agentwrench.checkpoints.budget import budget_report
from agentwrench.checkpoints.db import ScopeError


def artifacts():
    before = accounting({('', '', ''): 0, ('*', '子', '*'): 0, ('removed', '', 'data'): 0,
                         ('t', '', 'data'): 9})
    after = accounting({('', '', ''): 0, ('*', '子', '*'): 0, ('new', '', ''): 0,
                        ('t', '', 'data'): 28})
    before['threads'].append(dict(thread_id='only-thread', checkpoint_ns='', checkpoint_bytes=0,
                                  write_bytes=0, bytes=0))
    value = policy()
    threads = [('', ''), ('*', '子'), ('removed', ''), ('new', ''), ('only-thread', ''),
               ('T', ''), ('t', 'other'), ('a\x1b\n"\\é', '子\t')]
    channels = [('', '', ''), ('*', '子', '*'), ('removed', '', 'data'), ('new', '', ''),
                ('t', '', 'data'), ('only-thread', '', 'data'), ('t', '', 'Data'),
                ('t', '', '*'), ('t', '', ''), ('t', '', 'dаta'), ('a\x1b\n"\\é', '子\t', '\x00\x7f')]
    value['threads'] = [dict(thread_id=t, checkpoint_ns=ns, max_growth_bytes=4096) for t, ns in threads]
    value['channels'] = [dict(thread_id=t, checkpoint_ns=ns, channel=ch, max_growth_bytes=17)
                         for t, ns, ch in channels]
    return before, after, value


def invoke(tmp_path, capsys, before, after, value, flags=(), human=False):
    b, c, p = [save(tmp_path / name, obj) for name, obj in
               [('b.json', before), ('c.json', after), ('p.json', value)]]
    args = ['budget', str(b), str(c), '--policy', str(p), '--show-policy-coverage', *map(str, flags)]
    if not human:
        args += ['--json']
    code = main(args)
    return code, capsys.readouterr()


@pytest.mark.parametrize('cap', [0, 1, 3, 6, 99])
@pytest.mark.parametrize('row_cap', [None, 0, 1])
@pytest.mark.parametrize('allowance,code', [(17, 3), (19, 0)])
def test_independent_prefixes_keep_counts_presence_decisions_and_identities(tmp_path, capsys, cap, row_cap, allowance, code):
    before, after, value = artifacts()
    for row in value['channels']:
        row['max_growth_bytes'] = allowance
    original = deepcopy((before, after, value))
    kwargs = dict(policy=value, show_policy_coverage=True, max_results=row_cap,
                  show_group_presence=True, show_group_presence_summary=True)
    full = budget_report(before, after, **kwargs)
    expected = deepcopy(full)
    expected['max_unused_selectors'] = cap
    for name, totals in [('threads', (8, 5, 3)), ('channels', (11, 5, 6))]:
        coverage = expected['policy_coverage'][name]
        assert (coverage['total_overrides'], coverage['covered_overrides'], coverage['unused_overrides']) == totals
        all_unused = coverage['unused_selectors']
        coverage['unused_selectors'] = all_unused[:cap]
        coverage['unused_selectors_summary'] = dict(total=totals[2], shown=min(totals[2], cap), truncated=cap < totals[2])
    assert budget_report(before, after, **kwargs, max_unused_selectors=cap) == expected
    flags = ['--max-unused-selectors', cap, '--show-group-presence', '--show-group-presence-summary']
    if row_cap is not None:
        flags += ['--max-results', row_cap]
    observed, output = invoke(tmp_path, capsys, before, after, value, flags)
    assert observed == code and not output.err and json.loads(output.out) == expected
    assert (before, after, value) == original
    # Policy/snapshot input order never changes the literal sorted prefix or matching.
    for obj in (before, after, value):
        for name in ('threads', 'channels'):
            obj[name].reverse()
    assert budget_report(before, after, **kwargs, max_unused_selectors=cap) == expected


@pytest.mark.parametrize('cap', [0, 1, 99])
def test_human_contract_and_no_cap_compatibility(tmp_path, capsys, cap):
    before, after, value = artifacts()
    flags = ['--max-results', 0]
    _, uncapped = invoke(tmp_path, capsys, before, after, value, flags, human=True)
    code, output = invoke(tmp_path, capsys, before, after, value, [*flags, '--max-unused-selectors', cap], human=True)
    expected = ('growth budget: EXCEEDED\ncomparisons: 11 total, 0 shown, truncated: true\n'
                'violations: 1 total, 0 shown, truncated: true\npolicy coverage:\n')
    full = budget_report(before, after, policy=value, show_policy_coverage=True)
    for name in ('threads', 'channels'):
        cov = full['policy_coverage'][name]
        n = cov['unused_overrides']
        expected += f"  {name}: {cov['total_overrides']} total overrides, {cov['covered_overrides']} covered, {n} unused\n"
        expected += f"    unused selectors: {n} total, {min(n, cap)} shown, truncated: {str(cap < n).lower()}\n"
        for row in cov['unused_selectors'][:cap]:
            expected += '    unused: ' + ', '.join(f'{k}={json.dumps(v, ensure_ascii=True)}' for k, v in row.items()) + '\n'
    assert code == 3 and not output.err and output.out == expected
    assert output.out.isascii() and '\x1b' not in output.out
    assert 'unused selectors:' not in uncapped.out and uncapped.out.count('    unused:') == 9
    if cap == 99:
        projected = '\n'.join(line for line in output.out.split('\n') if 'unused selectors:' not in line)
        assert projected == uncapped.out


@pytest.mark.parametrize('cap', [0, 1])
@pytest.mark.parametrize('mode', ['empty', 'covered'])
def test_complete_empty_lists_and_strict_success(cap, mode):
    empty = accounting({})
    value = policy()
    value['threads'], value['channels'] = [], []
    if mode == 'covered':
        empty = accounting({('', '', ''): 0})
        value['threads'] = [dict(thread_id='', checkpoint_ns='', max_growth_bytes=0)]
        value['channels'] = [dict(thread_id='', checkpoint_ns='', channel='', max_growth_bytes=0)]
    result = budget_report(empty, empty, policy=value, show_policy_coverage=True,
                           require_policy_match=True, max_unused_selectors=cap)
    assert result['passed']
    for coverage in result['policy_coverage'].values():
        assert coverage['unused_selectors'] == []
        assert coverage['unused_overrides'] == 0
        assert coverage['unused_selectors_summary'] == dict(total=0, shown=0, truncated=False)


@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('cap', [0, 1])
@pytest.mark.parametrize('kind', ['threads', 'channels'])
def test_strict_refusal_is_identical_before_any_capped_output(tmp_path, capsys, human, cap, kind):
    before, after, value = artifacts()
    if kind == 'channels':
        value['threads'] = []
    else:
        value['threads'] = [row for row in value['threads'] if '\x1b' in row['thread_id']]
    code, full = invoke(tmp_path, capsys, before, after, value, ['--require-policy-match'], human)
    observed, output = invoke(tmp_path, capsys, before, after, value,
                             ['--require-policy-match', '--max-unused-selectors', cap, '--max-results', 0], human)
    assert observed == code == 2 and not full.out and not output.out and output.err == full.err
    assert ('thread' if kind == 'threads' else 'channel') + ' selector' in output.err
    assert '\\u001b' in output.err and '\x1b' not in output.err


@pytest.mark.parametrize('value', ['', '-1', '+1', '1.0', '1e2', ' 1', '1 ', '１', '١', 'true', 'None', '9' * 5000])
def test_invalid_cli_cap_is_rejected_before_reads(monkeypatch, capsys, value):
    import agentwrench.checkpoints.cli as cli
    monkeypatch.setattr(cli, 'load_snapshot', lambda *a: pytest.fail('must parse cap before input reads'))
    with pytest.raises(SystemExit) as error:
        main(['budget', 'missing', 'missing', '--policy', 'missing', '--show-policy-coverage',
              '--max-unused-selectors=' + value, '--json'])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and 'nonnegative ASCII decimal integer' in output.err and 'Traceback' not in output.err


@pytest.mark.parametrize('cap', [False, True, -1, 1.0, '0', [], {}])
def test_invalid_api_cap_is_scope_error(cap):
    with pytest.raises(ScopeError, match='--max-unused-selectors must be a nonnegative integer'):
        budget_report(None, None, max_unused_selectors=cap)


@pytest.mark.parametrize('has_policy,has_coverage', [(False, False), (True, False), (False, True)])
def test_dependencies(tmp_path, capsys, has_policy, has_coverage):
    empty = accounting({})
    b = save(tmp_path / 'b.json', empty)
    p = save(tmp_path / 'p.json', policy())
    flags = ['--policy', str(p)] if has_policy else ['--max-channel-growth-bytes', '0']
    if has_coverage:
        flags += ['--show-policy-coverage']
    assert main(['budget', str(b), str(b), *flags, '--max-unused-selectors', '0', '--json']) == 2
    output = capsys.readouterr()
    diagnostic = ('--show-policy-coverage requires --policy' if has_coverage else
                  '--max-unused-selectors requires --show-policy-coverage and --policy')
    assert not output.out and output.err == 'error: ' + diagnostic + '\n'
    with pytest.raises(ScopeError, match=diagnostic):
        budget_report(empty, empty, policy=policy() if has_policy else None,
                      show_policy_coverage=has_coverage, max_unused_selectors=0)


def test_leading_zeros_missing_argument_and_budget_only(tmp_path, capsys):
    empty = save(tmp_path / 'b.json', accounting({}))
    p = save(tmp_path / 'p.json', policy())
    args = ['budget', str(empty), str(empty), '--policy', str(p), '--show-policy-coverage']
    assert main([*args, '--max-unused-selectors', '000', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['max_unused_selectors'] == 0
    for bad_args in [[*args, '--max-unused-selectors'], ['snapshot', 'missing', '--max-unused-selectors', '0'],
                     ['compare-plans', 'missing', 'missing', '--max-unused-selectors', '0']]:
        with pytest.raises(SystemExit) as error:
            main(bad_args)
        assert error.value.code == 2 and not capsys.readouterr().out


@pytest.mark.parametrize('bad', ['scope', 'snapshot-row', 'snapshot-duplicate', 'policy-row', 'policy-duplicate',
                                 'snapshot-json', 'policy-json', 'mixed-limits'])
@pytest.mark.parametrize('cap', [0, 1])
def test_complete_validation_with_hidden_rows(tmp_path, capsys, bad, cap):
    before, after, value = artifacts()
    flags = ['--max-unused-selectors', cap, '--max-results', 0]
    if bad == 'scope':
        after = accounting({}); after['scope']['thread'] = 'other'
    elif bad == 'snapshot-row':
        after['channels'][-1]['bytes'] += 1
    elif bad == 'snapshot-duplicate':
        after['threads'].append(after['threads'][-1])
    elif bad == 'policy-row':
        value['channels'][-1]['max_growth_bytes'] = False
    elif bad == 'policy-duplicate':
        value['channels'].append(value['channels'][-1])
    elif bad == 'mixed-limits':
        flags += ['--max-thread-growth-bytes', 0]
    b, c, p = [save(tmp_path / name, obj) for name, obj in
               [('b.json', before), ('c.json', after), ('p.json', value)]]
    if bad.endswith('json'):
        (c if bad == 'snapshot-json' else p).write_text('{"format":1,"format":2}')
    inputs = [f.read_bytes() for f in (b, c, p)]
    assert main(['budget', str(b), str(c), '--policy', str(p), '--show-policy-coverage',
                 *map(str, flags), '--json']) == 2
    output = capsys.readouterr()
    assert not output.out and 'error:' in output.err and 'Traceback' not in output.err
    assert [f.read_bytes() for f in (b, c, p)] == inputs


@pytest.mark.parametrize('resource', ['MAX_POLICY_BYTES', 'MAX_OVERRIDES', 'MAX_SNAPSHOT_BYTES', 'MAX_GROUPS'])
def test_resource_boundaries_and_artifact_only_access(tmp_path, monkeypatch, capsys, resource):
    import builtins
    import sqlite3
    import agentwrench.checkpoints.budget as module
    import agentwrench.checkpoints.cli as cli
    def forbidden(*a, **kw):
        pytest.fail('budget must not access DB or graph')
    original_import = builtins.__import__
    def guarded_import(name, *a, **kw):
        if name == 'langgraph.graph' or name.startswith('langgraph.graph.'):
            forbidden()
        return original_import(name, *a, **kw)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(cli, 'connect', forbidden)
    before, after, value = artifacts()
    b, c, p = [save(tmp_path / name, obj) for name, obj in
               [('b.json', before), ('c.json', after), ('p.json', value)]]
    inputs = [f.read_bytes() for f in (b, c, p)]
    boundary = {'MAX_POLICY_BYTES': p.stat().st_size, 'MAX_OVERRIDES': 19,
                'MAX_SNAPSHOT_BYTES': max(b.stat().st_size, c.stat().st_size), 'MAX_GROUPS': 9}[resource]
    monkeypatch.setattr(module, resource, boundary)
    args = ['budget', str(b), str(c), '--policy', str(p), '--show-policy-coverage',
            '--max-unused-selectors', '0', '--max-results', '0', '--json']
    assert main(args) == 3
    assert json.loads(capsys.readouterr().out)['policy_coverage']['channels']['unused_overrides'] == 6
    monkeypatch.setattr(module, resource, boundary - 1)
    assert main(args) == 2
    output = capsys.readouterr()
    assert not output.out and 'error:' in output.err and 'Traceback' not in output.err
    assert [f.read_bytes() for f in (b, c, p)] == inputs
