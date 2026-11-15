"""Ordered scratch selection must retain streaming validation and publication rules."""

import pytest

from .test_merge import cli, step, write
from .test_merge_check import no_output_io
from .test_sort_temp_dir import assert_preserved, scratch
from agentwrench.export import cli as command, merge
from agentwrench.export.model import dumps

BOUND = '2026-01-01T10:02:00Z'


def ordered(tmp_path):
    return [write(tmp_path / 'a', 'A', [step(1, 0, 'é雪🙂\u2028\u0085\u2029'), step(2, 2)]),
            write(tmp_path / 'b', 'B', [step(1, 1)], ending='\r\n')]


def options(mode):
    return {'export': [], 'interval': ['--since', BOUND, '--until', BOUND],
            'check': ['--check']}[mode]


@pytest.mark.parametrize('mode', ['export', 'interval', 'check'])
@pytest.mark.parametrize('source_count', [2, 64])
def test_private_index_no_runs_full_consumption(tmp_path, monkeypatch, capsys, mode, source_count):
    paths = ordered(tmp_path)
    paths += [write(tmp_path / f'source-{n}', f'S{n}', [step(1, 2)])
              for n in range(2, source_count)]
    before = [p.read_bytes() for p in paths]
    root = scratch(tmp_path)
    baseline = cli(*paths, *options(mode))
    assert baseline.returncode == 0
    indexes, eof = [], []
    original_connect, original_record = merge.sqlite3.connect, merge._Source.record

    def connect(path):
        assert path.parent.parent == root
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        assert list(path.parent.iterdir()) == [path]
        indexes.append(path)
        return original_connect(path)

    def record(source):
        result = original_record(source)
        if result is None:
            eof.append(source.number)
        return result

    def no_runs(*args, **kwargs):
        pytest.fail('ordered merge must never create sort runs')

    monkeypatch.setattr(merge.sqlite3, 'connect', connect)
    monkeypatch.setattr(merge._Source, 'record', record)
    monkeypatch.setattr(merge, '_private_run', no_runs)
    monkeypatch.setattr(merge, '_external_events', no_runs)
    if mode == 'check':
        no_output_io(monkeypatch)
    assert command.main(['merge', *map(str, paths), '--sort-temp-dir', str(root), *options(mode)]) == 0
    actual = capsys.readouterr()
    assert (actual.out, actual.err) == (baseline.stdout, baseline.stderr)
    counts = {'merged_events': 0 if mode == 'interval' else source_count + 1, 'sources': source_count}
    if mode == 'interval':
        counts['excluded_events'] = source_count + 1
    assert actual.err == 'agentwrench export: ' + dumps(counts) + '\n'
    assert sorted(eof) == list(range(1, source_count + 1))
    assert len(indexes) == 1 and not indexes[0].parent.exists()
    assert [p.read_bytes() for p in paths] == before
    assert_preserved(root)


@pytest.mark.parametrize('mode', ['export', 'interval', 'check'])
@pytest.mark.parametrize('failure', ['tail', 'unterminated', 'duplicate', 'duplicate-across', 'timestamp', 'decreasing'])
def test_late_excluded_failures_remove_output_and_child(tmp_path, capsys, mode, failure):
    paths = ordered(tmp_path)
    if failure in ('tail', 'unterminated'):
        with paths[0].open('ab') as stream:
            stream.write(b'{SECRET' + (b'\n' if failure == 'tail' else b''))
        expected = 'merge input 1 line 4: invalid UTF-8 or JSON record'
    else:
        last = step(2, 3)
        if failure == 'duplicate-across':
            write(paths[1], 'A', [step(2, 4)])
        if failure == 'timestamp':
            last['timestamp'] = 'SECRET'
        if failure == 'decreasing':
            last['timestamp'] = '2026-01-01T10:01:00Z'
        write(paths[0], 'A', [step(1, 0), step(2, 2), last] if failure != 'duplicate-across'
              else [step(1, 0), step(2, 2)])
        expected = {
            'duplicate': 'duplicate event identity: input 1 line 3 conflicts with input 1 line 4',
            'duplicate-across': 'duplicate event identity: input 1 line 3 conflicts with input 2 line 2',
            'timestamp': 'merge input 1 line 4: timestamp requires RFC3339 with timezone and at most 6 fractional digits',
            'decreasing': 'merge input 1 line 4: timestamps are not nondecreasing',
        }[failure]
    root = scratch(tmp_path)
    before = [p.read_bytes() for p in paths]
    out = tmp_path / 'out'
    # All valid steps are excluded by the equal bounds. Late errors still win.
    for output_args in ([[]] if mode == 'check' else [[], ['-o', str(out)]]):
        assert command.main(['merge', *map(str, paths), '--sort-temp-dir', str(root),
                             *options(mode), *output_args]) == 2
        result = capsys.readouterr()
        assert result.out == '' and result.err == f'agentwrench export: error: {expected}\n'
        assert not out.exists()
        assert_preserved(root)
    assert [p.read_bytes() for p in paths] == before


@pytest.mark.parametrize('as_string', [False, True])
def test_api_close_after_header_or_event_cleans_private_index(tmp_path, monkeypatch, as_string):
    paths = ordered(tmp_path)
    root = scratch(tmp_path)
    before = [p.read_bytes() for p in paths]
    original_open = merge.os.fdopen
    streams = []

    def observe(fd, *args, **kwargs):
        stream = original_open(fd, *args, **kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(merge.os, 'fdopen', observe)
    expected = list(merge.merge_jsonl(paths))
    selected = str(root) if as_string else root
    assert list(merge.merge_jsonl(paths, sort_temp_dir=selected)) == expected
    for advances in (1, 2):
        rows = merge.merge_jsonl(paths, sort_temp_dir=selected)
        for _ in range(advances):
            next(rows)
        children = [p for p in root.iterdir() if p.name.startswith('trajport-merge-')]
        assert len(children) == 1
        assert children[0].stat().st_mode & 0o777 == 0o700
        assert [p.name for p in children[0].iterdir()] == ['identity.sqlite']
        assert (children[0] / 'identity.sqlite').stat().st_mode & 0o777 == 0o600
        assert not all(stream.closed for stream in streams)
        rows.close()
        assert all(stream.closed for stream in streams)
        assert_preserved(root)
    assert [p.read_bytes() for p in paths] == before


@pytest.mark.parametrize('file_output', [False, True])
@pytest.mark.parametrize('failure', ['write', 'write_io', 'limit', 'index', 'child'])
def test_output_and_storage_failures_cleanup(tmp_path, monkeypatch, capsys, file_output, failure):
    paths = ordered(tmp_path)
    root = scratch(tmp_path)
    before = [p.read_bytes() for p in paths]
    seen_index = []
    original_connect = merge.sqlite3.connect

    def connect(path):
        seen_index.append(path)
        if failure == 'index':
            raise merge.sqlite3.OperationalError('SECRET')
        return original_connect(path)

    monkeypatch.setattr(merge.sqlite3, 'connect', connect)
    if failure == 'write':
        original_dumps = command.dumps
        def fail(row):
            if row.get('type') == 'event':
                raise OSError('SECRET disk failure after header write')
            return original_dumps(row)
        monkeypatch.setattr(command, 'dumps', fail)
    elif failure == 'write_io':
        # Fail the actual output stream after a successful header write.
        class FailingOutput:
            def __init__(self, stream):
                self.stream, self.writes = stream, 0
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return self.stream.__exit__(*exc)
            def write(self, text):
                self.writes += 1
                if self.writes == 2:
                    raise OSError('SECRET output write failure')
                return self.stream.write(text)
        original_fdopen, original_stage = command.os.fdopen, command.tempfile.TemporaryFile
        def fdopen(fd, mode, **kwargs):
            stream = original_fdopen(fd, mode, **kwargs)
            return FailingOutput(stream) if mode == 'w' else stream
        monkeypatch.setattr(command.os, 'fdopen', fdopen)
        monkeypatch.setattr(command.tempfile, 'TemporaryFile',
                            lambda **kwargs: FailingOutput(original_stage(**kwargs)))
    elif failure == 'child':
        def fail(**kwargs):
            raise OSError('SECRET child creation failure')
        monkeypatch.setattr(merge.tempfile, 'TemporaryDirectory', fail)
    out = tmp_path / 'out'
    extra = ['--max-output-bytes', '1'] if failure == 'limit' else []
    assert command.main(['merge', *map(str, paths), '--sort-temp-dir', str(root), *extra,
                         *(['-o', str(out)] if file_output else [])]) == 2
    captured = capsys.readouterr()
    expected = {'write': 'cannot process input/output (OSError)', 'write_io': 'cannot process input/output (OSError)', 'limit': 'merge output byte limit exceeded',
                'index': 'cannot process input/output (OperationalError)',
                'child': 'sort temporary directory must be an existing writable directory'}[failure]
    assert captured.out == '' and captured.err == f'agentwrench export: error: {expected}\n'
    assert bool(seen_index) == (failure != 'child')
    assert all(not p.parent.exists() for p in seen_index)
    assert not out.exists()
    assert [p.read_bytes() for p in paths] == before
    assert_preserved(root)


def test_new_file_bytes_permissions_and_existing_outputs_preserved(tmp_path):
    paths = ordered(tmp_path)
    root = scratch(tmp_path)
    before = [p.read_bytes() for p in paths]
    out = tmp_path / 'out'
    for mode in ('export', 'interval'):
        baseline = cli(*paths, *options(mode))
        result = cli(*paths, *options(mode), '--sort-temp-dir', root, '-o', out)
        assert result.returncode == 0 and result.stdout == ''
        assert out.read_bytes() == baseline.stdout.encode() and result.stderr == baseline.stderr
        assert out.stat().st_mode & 0o777 == 0o600
        out.unlink()
        dangling = tmp_path / 'dangling'
        dangling.symlink_to(tmp_path / 'absent')
        for target in (paths[0], root / 'keep', root / 'link', dangling):
            result = cli(*paths, *options(mode), '--sort-temp-dir', root, '-o', target)
            assert result.returncode == 2 and result.stdout == ''
            assert result.stderr == 'agentwrench export: error: cannot process input/output (FileExistsError)\n'
        assert dangling.is_symlink() and not dangling.exists()
        dangling.unlink()
        assert_preserved(root)
    assert [p.read_bytes() for p in paths] == before
