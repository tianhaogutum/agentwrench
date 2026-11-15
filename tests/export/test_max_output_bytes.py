"""Serialized merge output bounds: UTF-8, pre-write limits and cleanup."""
from contextlib import contextmanager

import pytest

from .test_merge import cli, header, step, write
from .test_sort_temp_dir import assert_preserved, scratch
from agentwrench.export import merge
from agentwrench.export import cli as command
from agentwrench.export.model import dumps

LIMIT_ERROR = 'agentwrench export: error: merge output byte limit exceeded\n'


def inputs(tmp_path, *, unsorted=False, conflict=False):
    a_rows = [step(1, 0, 'é雪🙂\u2028\u0085\u2029'), step(2, 2, 'SECRET payload')]
    if unsorted:
        a_rows.reverse()
    if conflict:
        a_rows.append(step(1, 3, 'SECRET duplicate'))
    return [write(tmp_path / 'SECRET-a', 'A', a_rows),
            write(tmp_path / 'b', 'B', [step(1, 1)], ending='\r\n')]


def serialize(rows):
    return ''.join(dumps(row) + '\n' for row in rows)


def options(mode, root):
    if mode == 'stream':
        return [], {}
    api = dict(external_sort=True, sort_buffer_bytes=1)
    args = ['--external-sort', '--sort-buffer-bytes', '1']
    if mode == 'selected':
        args += ['--sort-temp-dir', str(root)]
        api['sort_temp_dir'] = root
    return args, api


@pytest.mark.parametrize('mode', ['stream', 'platform', 'selected'])
@pytest.mark.parametrize('file_output', [False, True])
def test_exact_utf8_total_and_one_byte_short(tmp_path, mode, file_output):
    paths = inputs(tmp_path, unsorted=mode != 'stream')
    root = scratch(tmp_path)
    args, api = options(mode, root)
    rows = list(merge.merge_jsonl(paths, **api))
    expected = serialize(rows)
    total = len(expected.encode('utf-8'))
    assert total > len(expected)  # Count bytes, not characters.
    assert rows[0]['sources'][0]['trajectory'] == header('A')
    assert [row['step']['step_id'] for row in rows[1:]] == [1, 1, 2]
    assert [row['provenance']['line'] for row in rows[1:]] == (
        [2, 2, 3] if mode == 'stream' else [3, 2, 2])
    saved = tmp_path / 'out'
    destination = ['-o', saved] if file_output else []
    success = cli(*paths, *args, '--max-output-bytes', total, *destination)
    assert success.returncode == 0, success.stderr
    assert success.stderr == 'agentwrench export: {"merged_events": 3, "sources": 2}\n'
    if file_output:
        assert not success.stdout and saved.read_bytes() == expected.encode()
        assert saved.stat().st_mode & 0o777 == 0o600
        saved.unlink()
    else:
        assert success.stdout == expected
    for limit in (total - 1, len(expected), 1):
        failed = cli(*paths, *args, '--max-output-bytes', limit, *destination)
        assert failed.returncode == 2 and not failed.stdout
        assert failed.stderr == LIMIT_ERROR
        assert not saved.exists()
    assert_preserved(root)


@pytest.mark.parametrize('file_output', [False, True])
@pytest.mark.parametrize('limit', ['0', '-1', '1.5', 'bad'])
def test_invalid_limit_before_io(tmp_path, monkeypatch, capsys, file_output, limit):
    def unexpected(*args, **kwargs):
        pytest.fail('invalid limit must be rejected before input or output I/O')

    monkeypatch.setattr(command, 'merge_jsonl', unexpected)
    monkeypatch.setattr(command.os, 'open', unexpected)
    monkeypatch.setattr(command.tempfile, 'TemporaryFile', unexpected)
    args = ['merge', str(tmp_path / 'absent-a'), str(tmp_path / 'absent-b'),
            '--max-output-bytes', limit]
    if file_output:
        args += ['-o', str(tmp_path / 'out')]
    if limit in ('0', '-1'):
        assert command.main(args) == 2
        captured = capsys.readouterr()
        assert not captured.out
        assert captured.err == 'agentwrench export: error: max output bytes must be a positive integer\n'
    else:
        with pytest.raises(SystemExit) as exc:
            command.main(args)
        assert exc.value.code == 2


@pytest.mark.parametrize('mode', ['stream', 'selected'])
@pytest.mark.parametrize('file_output', [False, True])
@pytest.mark.parametrize('boundary', ['header', 'last-event'])
def test_no_over_limit_write_and_private_storage_cleanup(tmp_path, monkeypatch, capsys,
                                                       mode, file_output, boundary):
    paths = inputs(tmp_path, unsorted=mode != 'stream')
    root = scratch(tmp_path)
    platform = tmp_path / 'platform'
    platform.mkdir()
    args, api = options(mode, root)
    rows = list(merge.merge_jsonl(paths, **api))
    encoded = [(dumps(row) + '\n').encode() for row in rows]
    limit = (len(encoded[0]) if boundary == 'header' else sum(map(len, encoded))) - 1
    original_stage, original_fdopen = command.tempfile.TemporaryFile, command.os.fdopen
    original_connect = merge.sqlite3.connect
    writes, indexes, stages = [], [], []
    monkeypatch.setattr(merge.tempfile, 'tempdir', str(platform))

    class Writer:
        def __init__(self, stream):
            self.stream, self.used = stream, 0

        def write(self, text):
            self.used += len(text.encode('utf-8'))
            assert self.used <= limit  # Observe actual stream calls, not the counter.
            writes.append(text)
            return self.stream.write(text)

    @contextmanager
    def stage(**kwargs):
        assert kwargs.get('dir') is None
        stages.append(True)
        with original_stage(**kwargs) as stream:
            yield Writer(stream)

    def fdopen(fd, *args, **kwargs):
        stream = original_fdopen(fd, *args, **kwargs)
        if kwargs.get('encoding') == 'utf-8':  # Requested output, not input/run.
            @contextmanager
            def output():
                with stream:
                    yield Writer(stream)
            return output()
        return stream

    def connect(path):
        assert path.parent.parent == (root if mode == 'selected' else platform)
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        indexes.append(path)
        return original_connect(path)

    monkeypatch.setattr(command.tempfile, 'TemporaryFile', stage)
    monkeypatch.setattr(command.os, 'fdopen', fdopen)
    monkeypatch.setattr(merge.sqlite3, 'connect', connect)
    saved = tmp_path / 'out'
    argv = ['merge', *map(str, paths), *args, '--max-output-bytes', str(limit)]
    if file_output:
        argv += ['-o', str(saved)]
    assert command.main(argv) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err == LIMIT_ERROR
    assert [text.encode() for text in writes] == ([] if boundary == 'header' else encoded[:-1])
    assert len(stages) == (0 if file_output else 1)
    assert indexes and all(not p.parent.exists() for p in indexes)
    assert not saved.exists() and not list(platform.iterdir())
    assert_preserved(root)


@pytest.mark.parametrize('file_output', [False, True])
@pytest.mark.parametrize('failure', ['quota', 'identity'])
def test_large_output_limit_keeps_run_quota_and_identity_failures(tmp_path, file_output, failure):
    paths = inputs(tmp_path, unsorted=True, conflict=failure == 'identity')
    root = scratch(tmp_path)
    saved = tmp_path / 'out'
    args = ['--external-sort', '--sort-temp-dir', root, '--sort-buffer-bytes', 1,
            '--max-output-bytes', 1000000, '--max-sort-temp-bytes',
            1 if failure == 'quota' else 1000000]
    result = cli(*paths, *args, *(['-o', saved] if file_output else []))
    assert result.returncode == 2 and not result.stdout
    expected = ('sort temporary-run byte quota exceeded' if failure == 'quota' else
                'duplicate event identity: input 1 line 3 conflicts with input 1 line 4')
    assert result.stderr == f'agentwrench export: error: {expected}\n'
    assert not saved.exists()
    assert_preserved(root)


@pytest.mark.parametrize('limit', [1, 1000000])
def test_existing_outputs_symlinks_inputs_and_root_contents_preserved(tmp_path, limit):
    paths = inputs(tmp_path, unsorted=True)
    before = [p.read_bytes() for p in paths]
    root = scratch(tmp_path)
    dangling = root / 'dangling'
    dangling.symlink_to(tmp_path / 'absent')
    for out in (root / 'keep', root / 'link', dangling, paths[0]):
        result = cli(*paths, '--external-sort', '--sort-temp-dir', root,
                     '--max-output-bytes', limit, '-o', out)
        assert result.returncode == 2 and not result.stdout
        assert result.stderr == 'agentwrench export: error: cannot process input/output (FileExistsError)\n'
    assert dangling.is_symlink() and not (tmp_path / 'absent').exists()
    dangling.unlink()
    assert [p.read_bytes() for p in paths] == before
    assert_preserved(root)
