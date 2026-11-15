"""External-sort contracts: opt-in ordering, original locations and safe failures."""
import json

import pytest

from .test_merge import cli, header, step, write


def records(text):
    return [json.loads(line) for line in text.split('\n') if line]


def test_external_sort_opt_in_preserves_payloads_locations_and_timestamp_ties(tmp_path):
    # A's equal instants cross tiny disk runs; step IDs deliberately differ from
    # physical order. Blank lines, BOM, CRLF and Unicode are source data.
    a_rows = [step(9, 2, 'A-first\u2028\u0085\u2029', extra={'extension': [1, 'kept']}),
              step(7, 0, 'A-early'), step(3, 2, 'A-second')]
    b_rows = [step(8, 2, 'B-tie', timestamp='2026-01-01T11:02:00+01:00'),
              step(4, 1, 'B-early')]
    a = tmp_path / 'a'
    a.write_bytes(('\ufeff' + json.dumps(header('A')) + '\r\n\r\n'
                   + '\r\n'.join(json.dumps(row, ensure_ascii=False) for row in a_rows)).encode())
    b = write(tmp_path / 'b', 'B', b_rows)
    default = cli(a, b)
    assert default.returncode == 2 and not default.stdout
    assert 'timestamps are not nondecreasing' in default.stderr
    expected = [(1, 4, 'A', a_rows[1]), (2, 3, 'B', b_rows[1]),
                (1, 3, 'A', a_rows[0]), (1, 5, 'A', a_rows[2]), (2, 2, 'B', b_rows[0])]
    outputs = []
    for budget in (1, 700, 8388608):
        result = cli(a, b, '--external-sort', '--sort-buffer-bytes', budget)
        assert result.returncode == 0, result.stderr
        rows = records(result.stdout)
        assert rows[0] == {'type': 'merge', 'schema_version': 'trajport/merge-v1', 'sources': [
            {'input': 1, 'trajectory': header('A')}, {'input': 2, 'trajectory': header('B')}]}
        assert rows[1:] == [{'type': 'event', 'step': row, 'provenance': {
            'input': number, 'line': line, 'session_id': session, 'step_id': row['step_id']}}
            for number, line, session, row in expected]
        assert json.loads(result.stderr.removeprefix('agentwrench export: ')) == {'merged_events': 5, 'sources': 2}
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    reverse = cli(b, a, '--external-sort', '--sort-buffer-bytes', 1)
    assert reverse.returncode == 0, reverse.stderr
    assert [row['step']['message'] for row in records(reverse.stdout)[1:]][-3:] == [
        'B-tie', a_rows[0]['message'], 'A-second']
    output = tmp_path / 'out'
    saved = cli(a, b, '--external-sort', '--sort-buffer-bytes', 1, '-o', output)
    assert saved.returncode == 0 and not saved.stdout
    assert output.read_text() == outputs[0]
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('same_payload', [True, False])
@pytest.mark.parametrize('within_file', [True, False])
def test_external_sort_identity_conflicts_keep_original_locations(tmp_path, same_payload, within_file):
    original = step(8, 2, 'SECRET-content')
    duplicate = original if same_payload else step(8, 3, 'changed')
    a = write(tmp_path / 'private-a', 'SECRET-session', [original, step(1, 0)])
    b = write(tmp_path / 'private-b', 'B' if within_file else 'SECRET-session',
              [step(2, 1)] if within_file else [duplicate])
    if within_file:
        with a.open('a') as stream:
            stream.write('\n' + json.dumps(duplicate) + '\n')
    expected = ('input 1 line 2 conflicts with input 1 line 5' if within_file else
                'input 1 line 2 conflicts with input 2 line 2')
    for destination in ([], ['-o', tmp_path / 'out']):
        result = cli(a, b, '--external-sort', '--sort-buffer-bytes', 1, *destination)
        assert result.returncode == 2 and not result.stdout
        assert result.stderr == f'agentwrench export: error: duplicate event identity: {expected}\n'
        assert 'SECRET' not in result.stderr and 'private-' not in result.stderr
        assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('bad', ['timestamp', 'missing_time', 'tail', 'duplicate_field',
                                 'nonfinite', 'header', 'empty'])
def test_external_sort_still_rejects_invalid_inputs_after_spilling(tmp_path, bad):
    a = write(tmp_path / 'a', 'A', [step(1, 2), step(2, 0)])
    b = write(tmp_path / 'private-b', 'B', [step(1, 1)])
    if bad == 'timestamp':
        write(b, 'B', [step(1, 1, timestamp='2026-01-01T10:00:00.1234567Z')])
    elif bad == 'missing_time':
        write(b, 'B', [step(1, 1, timestamp=None)])
    elif bad == 'header':
        write(b, 'unknown', [step(1, 1)])
    elif bad == 'empty':
        write(b, 'B', [])
    else:
        with b.open('a') as stream:
            stream.write({'tail': '{"SECRET":', 'duplicate_field': '{"x":1,"x":2}\n',
                          'nonfinite': '{"x":NaN}\n'}[bad])
    for destination in ([], ['-o', tmp_path / 'out']):
        result = cli(a, b, '--external-sort', '--sort-buffer-bytes', 1, *destination)
        assert result.returncode == 2 and not result.stdout
        assert 'merge input 2 line ' in result.stderr
        assert 'SECRET' not in result.stderr and 'private-' not in result.stderr
        assert 'Traceback' not in result.stderr
        assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('budget', ['0', '-1', '67108865', 'bad'])
def test_external_sort_invalid_cli_budgets(tmp_path, budget):
    a = write(tmp_path / 'a', 'A', [step(1, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    result = cli(a, b, '--external-sort', '--sort-buffer-bytes', budget, '-o', tmp_path / 'out')
    assert result.returncode == 2 and not result.stdout
    assert not (tmp_path / 'out').exists()


def test_sort_budget_requires_opt_in_and_existing_outputs_are_preserved(tmp_path):
    a = write(tmp_path / 'a', 'A', [step(1, 2), step(2, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    rejected = cli(a, b, '--sort-buffer-bytes', 100)
    assert rejected.returncode == 2 and not rejected.stdout
    assert 'sort buffer requires --external-sort' in rejected.stderr
    existing = tmp_path / 'existing'
    existing.write_text('keep')
    link = tmp_path / 'link'
    link.symlink_to(existing)
    for output in (existing, link, a):
        before = output.read_bytes()
        failed = cli(a, b, '--external-sort', '-o', output)
        assert failed.returncode == 2 and not failed.stdout
        assert output.read_bytes() == before
    assert link.is_symlink()


@pytest.mark.parametrize('limit', ['MAX_FILE_BYTES', 'MAX_LINE_BYTES', 'MAX_RECORDS'])
def test_external_sort_retains_reader_bounds(tmp_path, monkeypatch, limit):
    from agentwrench.export import merge
    from agentwrench.export.model import InputError
    a = write(tmp_path / 'a', 'A', [step(1, 2), step(2, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    monkeypatch.setattr(merge, limit, 1)
    with pytest.raises(InputError, match='limit'):
        list(merge.merge_jsonl([a, b], external_sort=True, sort_buffer_bytes=1))


@pytest.mark.parametrize('budget', [True, 1.5, '1'])
def test_external_sort_api_rejects_noninteger_budget(tmp_path, budget):
    from agentwrench.export.merge import merge_jsonl
    from agentwrench.export.model import InputError
    with pytest.raises(InputError, match='sort buffer bytes must be between'):
        list(merge_jsonl([tmp_path / 'a', tmp_path / 'b'], external_sort=True, sort_buffer_bytes=budget))


def track_temporary_directories(monkeypatch, tmp_path):
    from pathlib import Path
    from agentwrench.export import merge
    original = merge.tempfile.TemporaryDirectory
    directories = []

    def temporary(**kwargs):
        directory = original(dir=tmp_path, **kwargs)
        directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(merge.tempfile, 'TemporaryDirectory', temporary)
    return directories


@pytest.mark.parametrize('budget', [1, 900])
def test_bounded_runs_multiple_passes_and_early_close_cleanup(tmp_path, monkeypatch, budget):
    from contextlib import contextmanager
    from agentwrench.export import merge
    a_rows = [step(n, n % 5, f'A{n}') for n in range(61, 0, -1)]
    b_rows = [step(1, 0, 'B')]
    a = write(tmp_path / 'a', 'A', a_rows)
    b = write(tmp_path / 'b', 'B', b_rows)
    directories = track_temporary_directories(monkeypatch, tmp_path)
    # A small fan-in forces several generations using a compact synthetic input.
    monkeypatch.setattr(merge, 'SORT_FAN_IN', 3)
    original_write, original_read = merge._private_run, merge._read_runs
    written, groups = [], []

    @contextmanager
    def tracked_write(path):
        with original_write(path) as stream:
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
            yield stream
        raw = path.read_bytes()
        written.append((path.name, len(raw), len(raw.splitlines())))
        if path.name.startswith('run-0-'):
            assert len(raw) <= budget or len(raw.splitlines()) == 1

    def tracked_read(paths):
        groups.append([path.name for path in paths])
        assert len(paths) <= 3
        yield from original_read(paths)

    monkeypatch.setattr(merge, '_private_run', tracked_write)
    monkeypatch.setattr(merge, '_read_runs', tracked_read)
    result = list(merge.merge_jsonl([a, b], external_sort=True, sort_buffer_bytes=budget))
    expected = sorted([(1, n + 2, row) for n, row in enumerate(a_rows)] + [(2, 2, b_rows[0])],
                      key=lambda item: (item[2]['timestamp'], item[0], item[1]))
    assert [row['step'] for row in result[1:]] == [item[2] for item in expected]
    assert any(name.startswith('run-2-') for name, _, _ in written)
    assert groups and not directories[0].exists()
    # Closing after one event must close open final-run streams before removing
    # the private directory, including when not all events have been consumed.
    rows = merge.merge_jsonl([a, b], external_sort=True, sort_buffer_bytes=budget)
    assert next(rows)['type'] == 'merge'
    assert next(rows)['type'] == 'event'
    assert directories[-1].exists()
    rows.close()
    assert all(not path.exists() for path in directories)


@pytest.mark.parametrize('file_output', [True, False])
def test_external_sort_disk_failure_cleans_runs_and_output(tmp_path, monkeypatch, capsys, file_output):
    from agentwrench.export import merge
    from agentwrench.export.cli import main
    a = write(tmp_path / 'a', 'A', [step(1, 2), step(2, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    directories = track_temporary_directories(monkeypatch, tmp_path)
    original = merge._private_run
    calls = 0

    def fail_after_spill(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('SECRET path or disk diagnostic')
        return original(path)

    monkeypatch.setattr(merge, '_private_run', fail_after_spill)
    output = tmp_path / 'out'
    args = ['merge', str(a), str(b), '--external-sort', '--sort-buffer-bytes', '1']
    if file_output:
        args += ['-o', str(output)]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == 'agentwrench export: error: cannot process input/output (OSError)\n'
    assert not output.exists()
    assert directories and all(not path.exists() for path in directories)


def test_external_sort_is_byte_equivalent_for_already_sorted_inputs(tmp_path):
    a = write(tmp_path / 'a', 'A', [step(1, 0), step(2, 2)])
    # Disjoint fragments of the same session are still allowed.
    b = write(tmp_path / 'b', 'A', [step(3, 1), step(4, 2)])
    ordered = cli(a, b)
    sorted_result = cli(a, b, '--external-sort', '--sort-buffer-bytes', 1)
    assert ordered.returncode == sorted_result.returncode == 0
    assert ordered.stdout == sorted_result.stdout
    assert ordered.stderr == sorted_result.stderr


def test_identity_conflict_after_multiple_passes_removes_all_temporaries(tmp_path, monkeypatch, capsys):
    from agentwrench.export.cli import main
    a_rows = [step(1, 0), *[step(n, 2) for n in range(2, 67)], step(1, 0)]
    a = write(tmp_path / 'a', 'A', a_rows)
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    directories = track_temporary_directories(monkeypatch, tmp_path)
    output = tmp_path / 'out'
    assert main(['merge', str(a), str(b), '--external-sort', '--sort-buffer-bytes', '1',
                 '-o', str(output)]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == ('agentwrench export: error: duplicate event identity: '
                            'input 1 line 2 conflicts with input 1 line 68\n')
    assert not output.exists()
    assert directories and all(not path.exists() for path in directories)
