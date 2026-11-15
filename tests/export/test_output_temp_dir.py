"""Stdout staging root is private, independent, and used only after option checks."""
from pathlib import Path
import os

import pytest

from .test_merge import cli, step, write
from .test_sort_temp_dir import assert_preserved, scratch
from agentwrench.export import cli as command, merge

ERROR = 'output temporary directory must be an existing writable directory'
BOUND = '2026-01-01T10:02:00Z'


def inputs(tmp_path, external=False):
    rows = [step(n, n % 4, f'é雪🙂\u2028\u0085\u2029 {n}') for n in range(1, 66)] if external else [step(1, 0), step(2, 2)]
    if external:
        rows.reverse()
    return [write(tmp_path / 'a', 'A', rows), write(tmp_path / 'b', 'B', [step(1, 1)], ending='\r\n')]


def roots(tmp_path):
    result = []
    for name in ('output', 'index'):
        parent = tmp_path / name
        parent.mkdir()
        result.append(scratch(parent))
    return result


def modes(mode):
    return {'ordered': [], 'interval': ['--since', '2026-01-01T10:01:00Z', '--until', BOUND],
            'empty': ['--since', BOUND, '--until', BOUND],
            'external': ['--external-sort', '--sort-buffer-bytes', '1', '--max-sort-temp-bytes', '1000000']}[mode]


@pytest.mark.parametrize('mode', ['ordered', 'interval', 'empty', 'external'])
@pytest.mark.parametrize('selected_index', [False, True])
def test_private_stage_full_validation_before_copy(tmp_path, monkeypatch, capsys, mode, selected_index):
    paths = inputs(tmp_path, mode == 'external')
    # Upper source boundary also uses the staging option.
    if mode == 'ordered':
        paths += [write(tmp_path / f'source-{n}', f'S{n}', [step(1, 3)]) for n in range(2, 64)]
    before = [p.read_bytes() for p in paths]
    output, index = roots(tmp_path)
    platform = tmp_path / 'platform'
    platform.mkdir()
    baseline = cli(*paths, *modes(mode))
    assert baseline.returncode == 0
    original_stage, original_record = command.tempfile.TemporaryFile, merge._Source.record
    original_connect, original_run = merge.sqlite3.connect, merge._private_run
    original_copy = command.shutil.copyfileobj
    eof, stages, indexes, runs, copies = [], [], [], [], []
    monkeypatch.setattr(command.tempfile, 'tempdir', str(platform))

    def stage(**kwargs):
        assert Path(kwargs['dir']).resolve() == output
        stream = original_stage(**kwargs)
        assert os.fstat(stream.fileno()).st_mode & 0o777 == 0o600
        # Linux TemporaryFile may be anonymous/unlinked. Inspect its live fd.
        assert Path(f'/proc/self/fd/{stream.fileno()}').resolve().parent == output
        stages.append(stream)
        assert capsys.readouterr().out == ''
        return stream

    def record(source):
        assert capsys.readouterr().out == ''
        row = original_record(source)
        if row is None:
            eof.append(source.number)
        return row

    def connect(path):
        assert path.parent.parent == (index if selected_index else platform)
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        indexes.append(path)
        return original_connect(path)

    def run(path):
        assert path.parent == indexes[-1].parent
        runs.append(path)
        return original_run(path)

    def copy(src, dst):
        assert sorted(eof) == list(range(1, len(paths) + 1))
        assert all(not p.parent.exists() for p in indexes)
        assert capsys.readouterr().out == ''
        assert src.read() == baseline.stdout
        src.seek(0)
        copies.append(True)
        return original_copy(src, dst)

    monkeypatch.setattr(command.tempfile, 'TemporaryFile', stage)
    monkeypatch.setattr(merge._Source, 'record', record)
    monkeypatch.setattr(merge.sqlite3, 'connect', connect)
    monkeypatch.setattr(merge, '_private_run', run)
    monkeypatch.setattr(command.shutil, 'copyfileobj', copy)
    # Relative roots and symlink-to-directory roots both select the same root.
    link = tmp_path / 'stage-link'
    link.symlink_to(output, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    args = ['merge', *map(str, paths), *modes(mode), '--output-temp-dir', 'stage-link',
            '--max-output-bytes', str(len(baseline.stdout.encode()))]
    if selected_index:
        args += ['--sort-temp-dir', str(index)]
    assert command.main(args) == 0
    result = capsys.readouterr()
    assert (result.out, result.err) == (baseline.stdout, baseline.stderr)
    assert copies == [True] and len(stages) == 1 and stages[0].closed
    assert bool(runs) == (mode == 'external')
    if runs:
        assert any(p.name.startswith('run-1-') for p in runs)
    assert [p.read_bytes() for p in paths] == before
    assert not list(platform.iterdir())
    assert_preserved(output)
    assert_preserved(index)


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('failure', ['tail', 'unterminated', 'duplicate', 'duplicate-across', 'limit', 'quota'])
def test_late_excluded_errors_and_limits_never_publish(tmp_path, monkeypatch, capsys, external, failure):
    paths = inputs(tmp_path)
    if failure in ('tail', 'unterminated'):
        with paths[0].open('ab') as stream:
            stream.write(b'{SECRET' + (b'\n' if failure == 'tail' else b''))
        expected = 'merge input 1 line 4: invalid UTF-8 or JSON record'
    elif failure == 'duplicate':
        write(paths[0], 'A', [step(1, 0), step(2, 2), step(2, 3)])
        expected = 'duplicate event identity: input 1 line 3 conflicts with input 1 line 4'
    elif failure == 'duplicate-across':
        write(paths[1], 'A', [step(2, 3)])
        expected = 'duplicate event identity: input 1 line 3 conflicts with input 2 line 2'
    else:
        expected = ('merge output byte limit exceeded' if failure == 'limit' else
                    'sort temporary-run byte quota exceeded' if external else
                    'sort temporary-run quota requires --external-sort')
    before = [p.read_bytes() for p in paths]
    output, index = roots(tmp_path)
    stages = []
    original_stage = command.tempfile.TemporaryFile

    def stage(**kwargs):
        stream = original_stage(**kwargs)
        stages.append(stream)
        return stream

    def no_copy(*args):
        pytest.fail('validation/limit failure must not publish stdout')

    monkeypatch.setattr(command.tempfile, 'TemporaryFile', stage)
    monkeypatch.setattr(command.shutil, 'copyfileobj', no_copy)
    extra = ['--max-output-bytes', '1'] if failure == 'limit' else ['--max-sort-temp-bytes', '1'] if failure == 'quota' else []
    assert command.main(['merge', *map(str, paths), '--output-temp-dir', str(output),
                         '--sort-temp-dir', str(index), '--since', BOUND, '--until', BOUND,
                         *(['--external-sort', '--sort-buffer-bytes', '1'] if external else []), *extra]) == 2
    result = capsys.readouterr()
    assert result.out == '' and result.err == f'agentwrench export: error: {expected}\n'
    assert len(stages) == 1 and stages[0].closed
    assert [p.read_bytes() for p in paths] == before
    assert_preserved(output)
    assert_preserved(index)


@pytest.mark.parametrize('kind', ['empty', 'nul', 'missing', 'file', 'dangling', 'unwritable'])
@pytest.mark.parametrize('external', [False, True])
def test_bad_roots_safe_no_fallback(tmp_path, capsys, kind, external):
    paths = inputs(tmp_path)
    before = [p.read_bytes() for p in paths]
    root = tmp_path / 'SECRET-root'
    if kind == 'file':
        root.write_bytes(b'keep')
    elif kind == 'dangling':
        root.symlink_to(tmp_path / 'absent')
    elif kind == 'unwritable':
        root.mkdir(mode=0o500)
    value = '' if kind == 'empty' else 'SECRET\0root' if kind == 'nul' else str(root)
    try:
        assert command.main(['merge', *map(str, paths), '--output-temp-dir', value,
                             *(['--external-sort'] if external else [])]) == 2
        result = capsys.readouterr()
        assert result.out == '' and result.err == f'agentwrench export: error: {ERROR}\n'
        assert [p.read_bytes() for p in paths] == before
        if kind == 'file':
            assert root.read_bytes() == b'keep'
        elif kind == 'dangling':
            assert root.is_symlink() and not root.exists()
        elif kind == 'unwritable':
            assert root.stat().st_mode & 0o777 == 0o500 and not list(root.iterdir())
        else:
            assert not root.exists()
    finally:
        if kind == 'unwritable':
            root.chmod(0o700)


@pytest.mark.parametrize('option', ['--check', '-o', '--output'])
@pytest.mark.parametrize('root_value', ['absent', ''])
def test_conflicts_before_any_io(tmp_path, monkeypatch, capsys, option, root_value):
    output = tmp_path / 'output'
    output.write_text('keep')
    args = ['merge', str(tmp_path / 'absent-a'), str(tmp_path / 'absent-b'),
            '--sort-temp-dir', str(tmp_path / 'absent-index'), '--output-temp-dir', root_value,
            option, *([] if option == '--check' else [str(output)])]
    def no_io(*args, **kwargs):
        pytest.fail('conflicting output options must fail before any I/O')
    with monkeypatch.context() as patch:
        patch.setattr(command, 'merge_jsonl', no_io)
        patch.setattr(command.os, 'open', no_io)
        patch.setattr(command.tempfile, 'TemporaryFile', no_io)
        patch.setattr(command.tempfile, 'TemporaryDirectory', no_io)
        patch.setattr(Path, 'stat', no_io)
        patch.setattr(Path, 'open', no_io)
        assert command.main(args) == 2
    result = capsys.readouterr()
    conflict = '--check' if option == '--check' else '--output'
    assert result.out == '' and result.err == f'agentwrench export: error: --output-temp-dir cannot be combined with {conflict}\n'
    assert output.read_text() == 'keep'


@pytest.mark.parametrize('failure', ['create', 'write', 'seek', 'copy', 'broken-pipe', 'index'])
@pytest.mark.parametrize('external', [False, True])
def test_staging_and_copy_failure_cleanup(tmp_path, monkeypatch, capsys, failure, external):
    paths = inputs(tmp_path)
    before = [p.read_bytes() for p in paths]
    output, index = roots(tmp_path)
    original_stage = command.tempfile.TemporaryFile
    stages, copies = [], []

    class Stage:
        def __init__(self, stream):
            self.stream, self.writes = stream, 0
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return self.stream.__exit__(*exc)
        def write(self, text):
            self.writes += 1
            if failure == 'write' and self.writes == 2:
                raise OSError('SECRET write failure')
            return self.stream.write(text)
        def seek(self, pos):
            if failure == 'seek':
                raise OSError('SECRET seek failure')
            return self.stream.seek(pos)

    def stage(**kwargs):
        if failure == 'create':
            raise OSError('SECRET creation failure')
        stream = original_stage(**kwargs)
        stages.append(stream)
        return Stage(stream)

    def copy(src, dst):
        copies.append(True)
        if failure == 'broken-pipe':
            raise BrokenPipeError('SECRET pipe failure')
        raise OSError('SECRET copy failure')

    monkeypatch.setattr(command.tempfile, 'TemporaryFile', stage)
    monkeypatch.setattr(command.shutil, 'copyfileobj', copy)
    if failure == 'index':
        def connect(*args):
            raise merge.sqlite3.OperationalError('SECRET index failure')
        monkeypatch.setattr(merge.sqlite3, 'connect', connect)
    result = command.main(['merge', *map(str, paths), '--output-temp-dir', str(output),
                           '--sort-temp-dir', str(index), *(['--external-sort'] if external else [])])
    captured = capsys.readouterr()
    assert result == (0 if failure == 'broken-pipe' else 2)
    expected = ERROR if failure == 'create' else f'cannot process input/output ({"OperationalError" if failure == "index" else "OSError"})'
    assert captured.out == ''
    assert captured.err == ('' if failure == 'broken-pipe' else f'agentwrench export: error: {expected}\n')
    assert len(stages) == (failure != 'create') and all(s.closed for s in stages)
    assert bool(copies) == (failure in ('copy', 'broken-pipe'))
    assert [p.read_bytes() for p in paths] == before
    assert_preserved(output)
    assert_preserved(index)


@pytest.mark.parametrize('mode', ['ordered', 'interval', 'empty', 'external'])
def test_exact_output_boundary_checked_before_each_stage_write(tmp_path, monkeypatch, capsys, mode):
    paths = inputs(tmp_path, mode == 'external')
    output, index = roots(tmp_path)
    baseline = cli(*paths, *modes(mode))
    assert baseline.returncode == 0
    limit = len(baseline.stdout.encode()) - 1
    original_stage = command.tempfile.TemporaryFile
    streams, written = [], []

    class Stage:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return self.stream.__exit__(*exc)
        def write(self, text):
            written.append(text.encode())
            assert sum(map(len, written)) <= limit
            return self.stream.write(text)

    def stage(**kwargs):
        stream = original_stage(**kwargs)
        streams.append(stream)
        return Stage(stream)

    def no_copy(*args):
        pytest.fail('crossing output limit must not publish stdout')

    monkeypatch.setattr(command.tempfile, 'TemporaryFile', stage)
    monkeypatch.setattr(command.shutil, 'copyfileobj', no_copy)
    assert command.main(['merge', *map(str, paths), *modes(mode), '--output-temp-dir', str(output),
                         '--sort-temp-dir', str(index), '--max-output-bytes', str(limit)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err == 'agentwrench export: error: merge output byte limit exceeded\n'
    assert bool(written) == (mode != 'empty')
    assert len(streams) == 1 and streams[0].closed
    assert_preserved(output)
    assert_preserved(index)


@pytest.mark.parametrize('external', [False, True])
def test_copy_failure_after_validated_prefix_still_cleans(tmp_path, monkeypatch, capsys, external):
    paths = inputs(tmp_path)
    output, index = roots(tmp_path)
    baseline = cli(*paths)
    assert baseline.returncode == 0
    original_copy, original_stage = command.shutil.copyfileobj, command.tempfile.TemporaryFile
    original_record = merge._Source.record
    stages, eof = [], []

    def stage(**kwargs):
        stream = original_stage(**kwargs)
        stages.append(stream)
        return stream

    def record(source):
        row = original_record(source)
        if row is None:
            eof.append(source.number)
        return row

    class FailingStdout:
        def __init__(self, dst):
            self.dst, self.writes = dst, 0
        def write(self, text):
            assert sorted(eof) == [1, 2]
            self.writes += 1
            if self.writes == 2:
                raise OSError('SECRET stdout write failure after publication begins')
            return self.dst.write(text)

    monkeypatch.setattr(command.tempfile, 'TemporaryFile', stage)
    monkeypatch.setattr(merge._Source, 'record', record)
    monkeypatch.setattr(command.shutil, 'copyfileobj', lambda src, dst: original_copy(src, FailingStdout(dst), length=32))
    assert command.main(['merge', *map(str, paths), '--output-temp-dir', str(output),
                         '--sort-temp-dir', str(index), *(['--external-sort'] if external else [])]) == 2
    captured = capsys.readouterr()
    assert captured.out == baseline.stdout[:32]
    assert captured.err == 'agentwrench export: error: cannot process input/output (OSError)\n'
    assert len(stages) == 1 and stages[0].closed
    assert_preserved(output)
    assert_preserved(index)
