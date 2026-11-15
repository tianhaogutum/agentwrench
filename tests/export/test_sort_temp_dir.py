"""Explicit scratch roots: placement, privacy, lifetime and publication contracts."""
from contextlib import contextmanager
import errno
from pathlib import Path

import pytest

from .test_merge import cli, step, write
from agentwrench.export import merge
from agentwrench.export.cli import main
from agentwrench.export.model import InputError

ROOT_ERROR = 'sort temporary directory must be an existing writable directory'


def inputs(tmp_path, *, many=False, conflict=False):
    rows = ([step(n, n % 4, f'A{n}\u2028\u0085\u2029') for n in range(65, 0, -1)]
            if many else [step(2, 2), step(1, 0)])
    if conflict:
        rows.append(step(rows[0]['step_id'], 3, 'SECRET-duplicate'))
    return [write(tmp_path / 'a', 'A', rows), write(tmp_path / 'b', 'B', [step(1, 1)])]


def scratch(tmp_path):
    root = tmp_path / 'SECRET scratch'
    root.mkdir(mode=0o750)
    (root / 'keep').write_bytes(b'user scratch content\x00')
    (root / 'nested').mkdir()
    (root / 'nested' / 'keep').write_text('nested')
    (root / 'link').symlink_to(root / 'keep')
    return root


def assert_preserved(root):
    assert sorted(p.name for p in root.iterdir()) == ['keep', 'link', 'nested']
    assert (root / 'keep').read_bytes() == b'user scratch content\x00'
    assert (root / 'nested' / 'keep').read_text() == 'nested'
    assert (root / 'link').is_symlink()
    assert root.stat().st_mode & 0o777 == 0o750


def test_cli_selection_matches_default_bytes_and_provenance(tmp_path):
    paths = inputs(tmp_path)
    root = scratch(tmp_path)
    default = cli(*paths, '--external-sort', '--sort-buffer-bytes', 1)
    selected = cli(*paths, '--external-sort', '--sort-buffer-bytes', 1, '--sort-temp-dir', root)
    assert default.returncode == selected.returncode == 0, selected.stderr
    assert default.stdout == selected.stdout and default.stderr == selected.stderr
    saved = tmp_path / 'out'
    result = cli(*paths, '--external-sort', '--sort-temp-dir', root, '-o', saved)
    assert result.returncode == 0 and not result.stdout
    assert saved.read_bytes() == selected.stdout.encode()
    assert saved.stat().st_mode & 0o777 == 0o600
    assert_preserved(root)


@pytest.mark.parametrize('as_string', [False, True])
def test_api_observes_private_runs_and_index_and_early_close(tmp_path, monkeypatch, as_string):
    paths = inputs(tmp_path, many=True)
    root = scratch(tmp_path)
    default = list(merge.merge_jsonl(paths, external_sort=True, sort_buffer_bytes=1))
    original_run, original_connect = merge._private_run, merge.sqlite3.connect
    runs, indexes = [], []

    @contextmanager
    def observe_run(path):
        assert path.parent.parent == root
        assert path.parent.stat().st_mode & 0o777 == 0o700
        with original_run(path) as stream:
            assert path.stat().st_mode & 0o777 == 0o600
            runs.append(path)
            yield stream

    def observe_index(path):
        assert path.parent.parent == root
        assert path.parent.stat().st_mode & 0o777 == 0o700
        db = original_connect(path)
        assert path.stat().st_mode & 0o777 == 0o600
        indexes.append(path)
        return db

    monkeypatch.setattr(merge, '_private_run', observe_run)
    monkeypatch.setattr(merge.sqlite3, 'connect', observe_index)
    options = dict(external_sort=True, sort_buffer_bytes=1,
                   sort_temp_dir=str(root) if as_string else root)
    assert list(merge.merge_jsonl(paths, **options)) == default
    assert any(p.name.startswith('run-1-') for p in runs)
    assert indexes and all(not p.parent.exists() for p in indexes)
    for advance in (1, 2):  # Close after header, or while final run streams are open.
        rows = merge.merge_jsonl(paths, **options)
        for _ in range(advance):
            next(rows)
        assert indexes[-1].exists()
        rows.close()
        assert not indexes[-1].parent.exists()
    assert_preserved(root)


@pytest.mark.parametrize('external', [False, True])
def test_default_root_and_staged_stdout_stay_platform_selected(tmp_path, monkeypatch, capsys, external):
    paths = [write(tmp_path / 'a', 'A', [step(1, 0)]), write(tmp_path / 'b', 'B', [step(1, 1)])]
    root = scratch(tmp_path)
    platform = tmp_path / 'platform'
    platform.mkdir()
    # tempfile caches its environment-derived root; set the resolved default.
    monkeypatch.setattr(merge.tempfile, 'tempdir', str(platform))
    original_connect, original_stage = merge.sqlite3.connect, merge.tempfile.TemporaryFile
    indexes, stages = [], []

    def observe_index(path):
        indexes.append(path)
        return original_connect(path)

    def observe_stage(**kwargs):
        assert kwargs.get('dir') is None
        stream = original_stage(**kwargs)
        assert Path(f'/proc/self/fd/{stream.fileno()}').resolve().parent == platform
        stages.append(True)
        return stream

    monkeypatch.setattr(merge.sqlite3, 'connect', observe_index)
    monkeypatch.setattr(merge.tempfile, 'TemporaryFile', observe_stage)
    args = ['merge', *map(str, paths)] + (['--external-sort', '--sort-buffer-bytes', '1'] if external else [])
    assert main(args) == 0
    default = capsys.readouterr()
    assert indexes[-1].parent.parent == platform
    assert main(args + ['--sort-temp-dir', str(root)]) == 0
    selected = capsys.readouterr()
    assert indexes[-1].parent.parent == root
    assert selected == default and len(stages) == 2
    assert not list(platform.iterdir())
    assert_preserved(root)


@pytest.mark.parametrize('option,keyword,error', [
    ('--sort-buffer-bytes', 'sort_buffer_bytes', 'sort buffer requires --external-sort'),
    ('--max-sort-temp-bytes', 'max_sort_temp_bytes', 'sort temporary-run quota requires --external-sort'),
])
def test_sort_only_options_still_require_sort_before_io(tmp_path, option, keyword, error):
    paths = [tmp_path / 'absent-a', tmp_path / 'absent-b']
    root = tmp_path / 'absent-root'
    with pytest.raises(InputError, match=error):
        list(merge.merge_jsonl(paths, sort_temp_dir=root, **{keyword: 1}))
    result = cli(*paths, '--sort-temp-dir', root, option, 1, '-o', tmp_path / 'out')
    assert result.returncode == 2 and not result.stdout
    assert result.stderr == f'agentwrench export: error: {error}\n'
    assert not root.exists() and not (tmp_path / 'out').exists()


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('bad', ['', True, 123, b'/tmp', object(), 'SECRET\x00root'])
def test_invalid_api_roots_are_safe_input_errors(tmp_path, bad, external):
    with pytest.raises(InputError, match=ROOT_ERROR):
        list(merge.merge_jsonl(inputs(tmp_path), external_sort=external, sort_temp_dir=bad))


@pytest.mark.parametrize('mode', ['export', 'interval', 'check', 'external'])
@pytest.mark.parametrize('kind', ['missing', 'file', 'dangling', 'unwritable'])
@pytest.mark.parametrize('file_output', [False, True])
def test_unusable_root_has_safe_diagnostic_and_no_output(tmp_path, kind, file_output, mode):
    # Ordered inputs allow each mode to reach scratch creation.
    paths = [write(tmp_path / 'a', 'A', [step(1, 0)]), write(tmp_path / 'b', 'B', [step(1, 1)])]
    mode_args = {'export': [], 'interval': ['--until', '2025-01-01T00:00:00Z'],
                 'check': ['--check'], 'external': ['--external-sort']}[mode]
    root = tmp_path / 'SECRET-root'
    if kind == 'file':
        root.write_text('keep')
    elif kind == 'dangling':
        root.symlink_to(tmp_path / 'absent')
    elif kind == 'unwritable':
        root.mkdir(mode=0o500)
    try:
        with pytest.raises(InputError, match=ROOT_ERROR):
            list(merge.merge_jsonl(paths, external_sort=mode == 'external', sort_temp_dir=root))
        out = tmp_path / 'out'
        result = cli(*paths, *mode_args, '--sort-temp-dir', root,
                     *(['-o', out] if file_output and mode != 'check' else []))
        assert result.returncode == 2 and not result.stdout
        assert result.stderr == f'agentwrench export: error: {ROOT_ERROR}\n'
        assert not out.exists()
        if kind == 'file':
            assert root.read_text() == 'keep'
        elif kind == 'dangling':
            assert root.is_symlink() and not root.exists()
        elif kind == 'unwritable':
            assert not list(root.iterdir())
        else:
            assert not root.exists()
    finally:
        if kind == 'unwritable':
            root.chmod(0o700)


@pytest.mark.parametrize('failure', ['quota', 'identity', 'run_io', 'index_io', 'root_io'])
@pytest.mark.parametrize('file_output', [False, True])
def test_failures_cleanup_child_and_preserve_root(tmp_path, monkeypatch, capsys, failure, file_output):
    paths = inputs(tmp_path, many=True, conflict=failure == 'identity')
    root = scratch(tmp_path)
    if failure == 'run_io':
        original = merge._private_run
        def fail(path):
            if path.name == 'run-0-1.jsonl':
                raise OSError('SECRET disk info')
            return original(path)
        monkeypatch.setattr(merge, '_private_run', fail)
    elif failure == 'index_io':
        def fail(path):
            raise merge.sqlite3.OperationalError('SECRET database info')
        monkeypatch.setattr(merge.sqlite3, 'connect', fail)
    elif failure == 'root_io':
        def fail(**kwargs):
            raise OSError(errno.ENOSPC, 'SECRET filesystem info')
        monkeypatch.setattr(merge.tempfile, 'TemporaryDirectory', fail)
    args = ['merge', *map(str, paths), '--external-sort', '--sort-buffer-bytes', '1',
            '--sort-temp-dir', str(root), '--max-sort-temp-bytes', '1' if failure == 'quota' else '1000000']
    out = tmp_path / 'out'
    assert main(args + (['-o', str(out)] if file_output else [])) == 2
    captured = capsys.readouterr()
    assert not captured.out and not out.exists()
    expected = {
        'quota': 'sort temporary-run byte quota exceeded',
        'identity': 'duplicate event identity: input 1 line 2 conflicts with input 1 line 67',
        'run_io': 'cannot process input/output (OSError)',
        'index_io': 'cannot process input/output (OperationalError)',
        'root_io': ROOT_ERROR,
    }[failure]
    assert captured.err == f'agentwrench export: error: {expected}\n'
    assert_preserved(root)


@pytest.mark.parametrize('quota', [1, 1000000])
def test_existing_outputs_and_symlinks_preserved(tmp_path, quota):
    paths = inputs(tmp_path)
    root = scratch(tmp_path)
    existing = root / 'keep'
    dangling = tmp_path / 'dangling'
    dangling.symlink_to(tmp_path / 'absent')
    for out in (existing, root / 'link', dangling, paths[0]):
        before = out.read_bytes() if out.exists() else None
        result = cli(*paths, '--external-sort', '--sort-temp-dir', root,
                     '--max-sort-temp-bytes', quota, '-o', out)
        assert result.returncode == 2 and not result.stdout
        assert (out.read_bytes() if out.exists() else None) == before
    assert dangling.is_symlink() and not (tmp_path / 'absent').exists()
    assert_preserved(root)


@pytest.mark.parametrize('external', [False, True])
def test_relative_and_directory_symlink_roots(tmp_path, monkeypatch, external):
    paths = [write(tmp_path / 'a', 'A', [step(1, 0)]), write(tmp_path / 'b', 'B', [step(1, 1)])]
    root = scratch(tmp_path)
    link = tmp_path / 'root-link'
    link.symlink_to(root, target_is_directory=True)
    expected = list(merge.merge_jsonl(paths, external_sort=external))
    monkeypatch.chdir(tmp_path)
    for selected in (root.name, link):
        assert list(merge.merge_jsonl(paths, external_sort=external, sort_temp_dir=selected)) == expected
        assert_preserved(root)
    assert link.is_symlink()
