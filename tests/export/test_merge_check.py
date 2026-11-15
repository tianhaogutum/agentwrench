"""Validation-only merge: full consumption, no output I/O, safe late failures."""
import os
from pathlib import Path

import pytest

from .test_merge import cli, header, step, write
from .test_sort_temp_dir import assert_preserved, scratch
from agentwrench.export import cli as command, merge
from agentwrench.export.model import dumps


def no_output_io(monkeypatch):
    original_open = os.open

    def unexpected(*args, **kwargs):
        pytest.fail('merge --check must not stage or copy stdout')

    def guarded_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            assert Path(path).name == 'identity.sqlite' or Path(path).name.startswith('run-')
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(command.tempfile, 'TemporaryFile', unexpected)
    monkeypatch.setattr(command.shutil, 'copyfileobj', unexpected)
    monkeypatch.setattr(command.os, 'open', guarded_open)


@pytest.mark.parametrize('external', [False, True])
def test_all_events_consumed_without_output_io(tmp_path, monkeypatch, capsys, external):
    root = scratch(tmp_path)
    platform = tmp_path / 'platform'
    platform.mkdir()
    monkeypatch.setattr(merge.tempfile, 'tempdir', str(platform))
    events = [step(n, 0, f'é雪🙂\u2028\u0085\u2029 {n}') for n in range(1, 66)]
    if external:
        events.reverse()
        events[0]['timestamp'] = '2026-01-01T10:02:00Z'
    paths = [write(tmp_path / 'a', 'A', events),
             write(tmp_path / 'b', 'B', [step(1, 1)], ending='\r\n')]
    before = [p.read_bytes() for p in paths]
    original_record, original_connect, original_run = merge._Source.record, merge.sqlite3.connect, merge._private_run
    eof, indexes, runs = [], [], []

    def record(source):
        row = original_record(source)
        if row is None:
            eof.append(source.number)
        return row

    def connect(path):
        indexes.append(path)
        assert path.parent.parent == (root if external else platform)
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        return original_connect(path)

    def run(path):
        runs.append(path)
        return original_run(path)

    monkeypatch.setattr(merge._Source, 'record', record)
    monkeypatch.setattr(merge.sqlite3, 'connect', connect)
    monkeypatch.setattr(merge, '_private_run', run)
    no_output_io(monkeypatch)
    # CLI serialization of merge envelopes is unnecessary (sort-run encoding is still required).
    original_dumps = command.dumps
    def diagnostics_only(row, **kwargs):
        assert 'type' not in row
        return original_dumps(row, **kwargs)
    monkeypatch.setattr(command, 'dumps', diagnostics_only)
    options = ['--external-sort', '--sort-buffer-bytes', '1', '--max-sort-temp-bytes', '1000000',
               '--sort-temp-dir', str(root)] if external else []
    assert command.main(['merge', *map(str, paths), '--check', *options]) == 0
    result = capsys.readouterr()
    assert result.out == ''
    assert result.err == 'agentwrench export: {"merged_events": 66, "sources": 2}\n'
    assert sorted(eof) == [1, 2]
    assert indexes and all(not path.parent.exists() for path in indexes)
    assert bool(runs) == external
    if external:
        assert any(path.name.startswith('run-1-') for path in runs)
    assert [p.read_bytes() for p in paths] == before
    assert not list(platform.iterdir())
    assert_preserved(root)


@pytest.mark.parametrize('option', ['-o', '--output', '--max-output-bytes'])
@pytest.mark.parametrize('external', [False, True])
def test_output_options_conflict_before_io(tmp_path, monkeypatch, capsys, option, external):
    def unexpected(*args, **kwargs):
        pytest.fail('conflicting options must fail before iterator/input/output/temp I/O')
    monkeypatch.setattr(command, 'merge_jsonl', unexpected)
    monkeypatch.setattr(command.os, 'open', unexpected)
    monkeypatch.setattr(command.tempfile, 'TemporaryFile', unexpected)
    monkeypatch.setattr(command.tempfile, 'TemporaryDirectory', unexpected)
    value = '1' if option == '--max-output-bytes' else str(tmp_path / 'out')
    options = ['--external-sort', '--sort-temp-dir', str(tmp_path / 'missing-root')] if external else []
    assert command.main(['merge', str(tmp_path / 'absent-a'), str(tmp_path / 'absent-b'),
                         '--check', option, value, *options]) == 2
    result = capsys.readouterr()
    conflict = '--max-output-bytes' if option == '--max-output-bytes' else '--output'
    assert result.out == ''
    assert result.err == f'agentwrench export: error: --check cannot be combined with {conflict}\n'


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('failure', ['tail', 'unterminated', 'timestamp', 'step', 'header', 'duplicate', 'quota'])
def test_validation_errors_cleanup_without_staging(tmp_path, monkeypatch, capsys, external, failure):
    if failure == 'quota' and not external:
        # Ordered mode must retain the existing option dependency error.
        expected = 'sort temporary-run quota requires --external-sort'
    else:
        expected = {
            'tail': 'merge input 1 line 5: invalid UTF-8 or JSON record',
            'unterminated': 'merge input 1 line 5: invalid UTF-8 or JSON record',
            'timestamp': 'merge input 1 line 4: timestamp requires RFC3339 with timezone and at most 6 fractional digits',
            'step': 'merge input 1 line 4: expected step with positive integer ID, source and text message',
            'header': 'merge input 2 line 1: expected trajport/v1 header with a known session ID and agent',
            'duplicate': 'duplicate event identity: input 1 line 2 conflicts with input 1 line 4',
            'quota': 'sort temporary-run byte quota exceeded',
        }[failure]
    rows = [step(1, 0), step(2, 2)]
    if failure == 'timestamp':
        rows[1]['timestamp'] = 'SECRET-invalid'
    elif failure == 'step':
        rows[1]['source'] = 'SECRET-invalid'
    elif failure == 'duplicate':
        rows[1]['step_id'] = 1
    a = write(tmp_path / 'SECRET-a', 'SECRET-session', rows)
    # Ensure diagnostics count blank physical lines too, and the error is late.
    raw = a.read_bytes().split(b'\n')
    a.write_bytes(b'\n'.join([*raw[:2], b'', *raw[2:]]))
    if failure in ('tail', 'unterminated'):
        with a.open('ab') as stream:
            stream.write(b'{SECRET' + (b'\n' if failure == 'tail' else b''))
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    if failure == 'header':
        b.write_text(dumps(dict(header('B'), schema_version='SECRET')) + '\n')
    paths = [a, b]
    before = [p.read_bytes() for p in paths]
    root = scratch(tmp_path)
    platform = tmp_path / 'platform'
    platform.mkdir()
    monkeypatch.setattr(merge.tempfile, 'tempdir', str(platform))
    no_output_io(monkeypatch)
    options = ['--external-sort', '--sort-buffer-bytes', '1', '--sort-temp-dir', str(root)] if external else []
    if failure == 'quota':
        options += ['--max-sort-temp-bytes', '1']
    assert command.main(['merge', *map(str, paths), '--check', *options]) == 2
    result = capsys.readouterr()
    assert result.out == '' and result.err == f'agentwrench export: error: {expected}\n'
    assert [p.read_bytes() for p in paths] == before
    assert not list(platform.iterdir())
    assert_preserved(root)


def test_unsorted_requires_external_sort_and_default_bytes_stay_identical(tmp_path):
    a = write(tmp_path / 'a', 'A', [step(2, 2), step(1, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    failed = cli(a, b, '--check')
    assert failed.returncode == 2 and not failed.stdout
    assert failed.stderr == 'agentwrench export: error: merge input 1 line 3: timestamps are not nondecreasing\n'
    before = cli(a, b, '--external-sort', '--sort-buffer-bytes', 1)
    checked = cli(a, b, '--check', '--external-sort', '--sort-buffer-bytes', 1)
    after = cli(a, b, '--external-sort', '--sort-buffer-bytes', 1)
    assert checked.returncode == before.returncode == after.returncode == 0
    assert checked.stdout == '' and checked.stderr == before.stderr == after.stderr
    assert before.stdout == after.stdout
    assert before.stdout == ''.join(dumps(row) + '\n' for row in merge.merge_jsonl(
        [a, b], external_sort=True, sort_buffer_bytes=1))


@pytest.mark.parametrize('count', [1, 64, 65])
def test_source_count_bounds_and_exact_counts(tmp_path, count):
    paths = [write(tmp_path / str(n), f'session-{n}', [step(1, 0)]) for n in range(count)]
    result = cli(*paths, '--check')
    assert result.stdout == ''
    if count == 64:
        assert result.returncode == 0
        assert result.stderr == 'agentwrench export: {"merged_events": 64, "sources": 64}\n'
    else:
        assert result.returncode == 2
        assert result.stderr == 'agentwrench export: error: merge requires 2 to 64 input files\n'


@pytest.mark.parametrize('options,expected', [
    (['--sort-buffer-bytes', '1'], 'sort buffer requires --external-sort'),
    (['--external-sort', '--sort-buffer-bytes', '0'], 'sort buffer bytes must be between 1 and 67108864'),
    (['--external-sort', '--max-sort-temp-bytes', '0'], 'max sort temp bytes must be a positive integer'),
    (['--sort-temp-dir', 'missing'], 'sort temporary directory must be an existing writable directory'),
    (['--external-sort', '--sort-temp-dir', 'missing'], 'sort temporary directory must be an existing writable directory'),
])
def test_sort_options_retain_validation(tmp_path, monkeypatch, options, expected):
    paths = [write(tmp_path / 'a', 'A', [step(1, 0)]), write(tmp_path / 'b', 'B', [step(1, 1)])]
    monkeypatch.chdir(tmp_path)
    # cli helper runs in checkout, so use an absolute absent root.
    options = [str(tmp_path / 'missing') if value == 'missing' else value for value in options]
    result = cli(*paths, '--check', *options)
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr == f'agentwrench export: error: {expected}\n'
    assert not (tmp_path / 'missing').exists()
