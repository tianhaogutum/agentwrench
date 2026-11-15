"""CLI interval is a projection after full validation, with unchanged envelopes."""
import json

import pytest

from .test_merge import ROOT, cli, step, write
from .test_sort_temp_dir import assert_preserved, scratch
from agentwrench.export import cli as command, merge
from agentwrench.export.model import dumps

SINCE = '2026-01-01T10:00:00Z'
UNTIL = '2026-01-01T10:02:00Z'
FIXTURES = ROOT / 'tests/export/fixtures/merge'


def rows(text):
    return [json.loads(line) for line in text.split('\n') if line]


def diagnostic(retained, excluded):
    return 'agentwrench export: ' + dumps({'merged_events': retained, 'excluded_events': excluded, 'sources': 2}) + '\n'


def sort_args(external, root):
    return (['--external-sort', '--sort-buffer-bytes', '1', '--sort-temp-dir', root,
             '--max-sort-temp-bytes', '1000000'] if external else [])


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('bounds,selected', [
    (['--since', SINCE, '--until', UNTIL], [2, 3, 1]),
    (['--since', UNTIL], [4, 2]),
    (['--until', SINCE], [1]),
    (['--since', SINCE, '--until', SINCE], []),
    (['--since', '2027-01-01T00:00:00Z'], []),
    (['--until', '2025-01-01T00:00:00Z'], []),
    (['--since', '2026-01-01T11:00:00+01:00', '--until', '2026-01-01T09:02:00-01:00'], [2, 3, 1]),
    (['--since', '2026-01-01T09:59:59.999999Z', '--until', '2026-01-01T10:00:00.000001Z'], [1, 2]),
])
def test_bounds_ties_exact_payload_and_zero_result(tmp_path, external, bounds, selected):
    root = scratch(tmp_path)
    paths = [FIXTURES / 'interval.jsonl', FIXTURES / 'b.jsonl']
    before = [p.read_bytes() for p in paths]
    args = sort_args(external, root)
    original = cli(*paths, *args)
    result = cli(*paths, *args, *bounds)
    assert result.returncode == 0, result.stderr
    baseline, actual = rows(original.stdout), rows(result.stdout)
    assert actual[0] == baseline[0]  # All headers, no new fields or recomputed metrics.
    assert [e['step']['step_id'] for e in actual[1:]] == selected
    assert result.stderr == diagnostic(len(selected), 6 - len(selected))
    assert all(e in baseline[1:] for e in actual[1:])
    assert actual[1:] == [e for e in baseline[1:] if e in actual[1:]]
    assert [p.read_bytes() for p in paths] == before
    assert_preserved(root)


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('file_output', [False, True])
@pytest.mark.parametrize('failure', ['tail', 'unterminated', 'timestamp', 'step', 'header',
                                     'duplicate-within', 'duplicate-across', 'decreasing'])
def test_excluded_late_errors_still_fail_and_cleanup(tmp_path, monkeypatch, capsys,
                                                    external, file_output, failure):
    root = scratch(tmp_path)
    platform = tmp_path / 'platform'
    platform.mkdir()
    monkeypatch.setattr(merge.tempfile, 'tempdir', str(platform))
    a_rows = [step(1, 0), step(2, 2), step(3, 3)]
    b_rows = [step(1, 1)]
    session = 'B'
    if failure == 'timestamp':
        a_rows[-1]['timestamp'] = 'SECRET'
    if failure == 'step':
        a_rows[-1]['source'] = 'SECRET'
    if failure == 'duplicate-within':
        a_rows[-1]['step_id'] = 2  # Both duplicate occurrences are excluded.
    if failure == 'duplicate-across':
        session, b_rows = 'A', [step(2, 4)]
    if failure == 'decreasing':
        a_rows[-1]['timestamp'] = SINCE
    a = write(tmp_path / 'a', 'A', a_rows)
    b = write(tmp_path / 'b', session, b_rows)
    if failure in ('tail', 'unterminated'):
        with a.open('ab') as stream:
            stream.write(b'{SECRET' + (b'\n' if failure == 'tail' else b''))
    if failure == 'header':
        b.write_text('{"type":"trajectory","schema_version":"SECRET"}\n')
    before = [p.read_bytes() for p in (a, b)]
    output = tmp_path / 'output'
    args = ['merge', str(a), str(b), '--until', SINCE, *map(str, sort_args(external, root))]
    if file_output:
        args += ['-o', str(output)]
    code = command.main(args)
    captured = capsys.readouterr()
    if failure == 'decreasing' and external:
        assert code == 0
        assert len(rows(output.read_text() if file_output else captured.out)) == 1
        assert captured.err == diagnostic(0, 4)
        if file_output:
            assert len(rows(output.read_text())) == 1
            output.unlink()
    else:
        assert code == 2 and captured.out == ''
        expected = {
            'tail': 'merge input 1 line 5: invalid UTF-8 or JSON record',
            'unterminated': 'merge input 1 line 5: invalid UTF-8 or JSON record',
            'timestamp': 'merge input 1 line 4: timestamp requires RFC3339 with timezone and at most 6 fractional digits',
            'step': 'merge input 1 line 4: expected step with positive integer ID, source and text message',
            'header': 'merge input 2 line 1: expected trajport/v1 header with a known session ID and agent',
            'duplicate-within': 'duplicate event identity: input 1 line 3 conflicts with input 1 line 4',
            'duplicate-across': 'duplicate event identity: input 1 line 3 conflicts with input 2 line 2',
            'decreasing': 'merge input 1 line 4: timestamps are not nondecreasing',
        }[failure]
        assert captured.err == f'agentwrench export: error: {expected}\n'
    assert not output.exists() and not list(platform.iterdir())
    assert [p.read_bytes() for p in (a, b)] == before
    assert_preserved(root)


@pytest.mark.parametrize('endpoint', ['--since', '--until'])
@pytest.mark.parametrize('value', ['', 'SECRET', '2026-01-01', '2026-01-01T10:00:00',
    '2026-01-01T10:00:00.1234567Z', '2026-02-30T10:00:00Z', '2026-01-01T10:00:00+24:00',
    '0001-01-01T00:00:00+01:00'])
def test_invalid_endpoint_before_any_io(tmp_path, monkeypatch, capsys, endpoint, value):
    def unexpected(*args, **kwargs):
        pytest.fail('endpoint must be validated before iterator, input, output or scratch I/O')
    monkeypatch.setattr(command, 'merge_jsonl', unexpected)
    monkeypatch.setattr(command.os, 'open', unexpected)
    monkeypatch.setattr(command.tempfile, 'TemporaryFile', unexpected)
    assert command.main(['merge', 'absent-a', 'absent-b', endpoint, value, '-o', str(tmp_path / 'out'),
                         '--external-sort', '--sort-temp-dir', 'absent-root']) == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == (f'agentwrench export: error: {endpoint} requires RFC3339 with timezone '
                            'and at most 6 fractional digits\n')


@pytest.mark.parametrize('bounds,error', [
    (['--since', UNTIL, '--until', SINCE], '--since must be before or equal to --until'),
    (['--since', '2026-01-01T10:00:00Z', '--until', '2026-01-01T10:30:00+01:00'],
     '--since must be before or equal to --until'),
    (['--check', '--since', SINCE], '--check cannot be combined with --since'),
    (['--check', '--until', UNTIL], '--check cannot be combined with --until'),
])
def test_reversed_and_check_conflicts_before_io(tmp_path, monkeypatch, capsys, bounds, error):
    def unexpected(*args, **kwargs):
        pytest.fail('invalid option combination must fail before I/O')
    monkeypatch.setattr(command, 'merge_jsonl', unexpected)
    monkeypatch.setattr(command.os, 'open', unexpected)
    monkeypatch.setattr(command.tempfile, 'TemporaryFile', unexpected)
    assert command.main(['merge', 'absent-a', 'absent-b', *bounds]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err == f'agentwrench export: error: {error}\n'


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('file_output', [False, True])
def test_output_limit_counts_only_header_and_retained_events(tmp_path, external, empty, file_output):
    paths = [FIXTURES / 'interval.jsonl', FIXTURES / 'b.jsonl']
    root = scratch(tmp_path)
    bounds = ['--since', SINCE, '--until', SINCE if empty else UNTIL]
    args = sort_args(external, root)
    baseline = cli(*paths, *args, *bounds)
    assert baseline.returncode == 0, baseline.stderr
    total = len(baseline.stdout.encode())
    out = tmp_path / 'out'
    for limit in (total, total - 1):
        result = cli(*paths, *args, *bounds, '--max-output-bytes', limit,
                     *(['-o', out] if file_output else []))
        if limit == total:
            assert result.returncode == 0 and result.stderr == baseline.stderr
            assert (out.read_bytes() if file_output else result.stdout.encode()) == baseline.stdout.encode()
            if file_output:
                assert out.stat().st_mode & 0o777 == 0o600
                out.unlink()
        else:
            assert result.returncode == 2 and result.stdout == ''
            assert result.stderr == 'agentwrench export: error: merge output byte limit exceeded\n'
            assert not out.exists()
    assert_preserved(root)


def test_unsorted_multi_pass_zero_result_still_uses_full_run_quota(tmp_path):
    root = scratch(tmp_path)
    paths = [write(tmp_path / 'a', 'A', [step(n, n % 4, f'é雪🙂 {n}') for n in range(66, 0, -1)]),
             write(tmp_path / 'b', 'B', [step(1, 1)])]
    bounds = ['--since', '2027-01-01T00:00:00Z']
    args = sort_args(True, root)
    full = cli(*paths, *args)
    selected = cli(*paths, *args, '--since', SINCE, '--until', '2026-01-01T10:01:00Z')
    assert selected.returncode == 0, selected.stderr
    expected = [e for e in rows(full.stdout)[1:] if e['step']['timestamp'] == SINCE]
    assert rows(selected.stdout)[1:] == expected
    assert selected.stderr == diagnostic(len(expected), 67 - len(expected))
    result = cli(*paths, *args, *bounds)
    assert result.returncode == 0 and len(rows(result.stdout)) == 1
    assert result.stderr == diagnostic(0, 67)
    failed = cli(*paths, *args, *bounds, '--max-sort-temp-bytes', 1)
    assert failed.returncode == 2 and failed.stdout == ''
    assert failed.stderr == 'agentwrench export: error: sort temporary-run byte quota exceeded\n'
    assert_preserved(root)


@pytest.mark.parametrize('external', [False, True])
def test_existing_outputs_symlinks_and_inputs_are_preserved(tmp_path, external):
    root = scratch(tmp_path)
    paths = [write(tmp_path / 'a', 'A', [step(1, 0)]), write(tmp_path / 'b', 'B', [step(1, 1)])]
    before = [p.read_bytes() for p in paths]
    dangling = root / 'dangling'
    dangling.symlink_to(tmp_path / 'absent')
    for output in (paths[0], root / 'keep', root / 'link', dangling):
        result = cli(*paths, *sort_args(external, root), '--since', UNTIL, '-o', output)
        assert result.returncode == 2 and result.stdout == ''
        assert result.stderr == 'agentwrench export: error: cannot process input/output (FileExistsError)\n'
    assert dangling.is_symlink() and not (tmp_path / 'absent').exists()
    dangling.unlink()
    assert [p.read_bytes() for p in paths] == before
    assert_preserved(root)
