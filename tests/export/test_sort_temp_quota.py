"""Live run-byte quota: pre-write enforcement, merge overlap and CLI atomicity."""
from contextlib import contextmanager

import pytest

from .test_external_sort import track_temporary_directories
from .test_merge import cli, step, write
from agentwrench.export import merge
from agentwrench.export.cli import main
from agentwrench.export.model import InputError, dumps

QUOTA_ERROR = 'sort temporary-run byte quota exceeded'


def event_sizes(rows, number, session):
    return [len((dumps({'type': 'event', 'step': row, 'provenance': {
        'input': number, 'line': line, 'session_id': session, 'step_id': row['step_id']}})
        + '\n').encode('utf-8')) for line, row in enumerate(rows, 2)]


def watch_writes(monkeypatch, quota):
    """Observe actual writes; an over-quota write must never reach the stream."""
    original = merge._private_run
    writes = []

    @contextmanager
    def tracked(path):
        with original(path) as stream:
            class Writer:
                def write(self, raw):
                    stream.flush()
                    live = sum(p.stat().st_size for p in path.parent.glob('run-*.jsonl'))
                    assert live + len(raw) <= quota
                    writes.append((path.name, live, len(raw)))
                    return stream.write(raw)
            yield Writer()

    monkeypatch.setattr(merge, '_private_run', tracked)
    return writes


@pytest.mark.parametrize('file_output', [False, True])
def test_tiny_quota_safe_cli_failure_and_cleanup(tmp_path, monkeypatch, capsys, file_output):
    a = write(tmp_path / 'SECRET-path', 'SECRET-session', [step(1, 2, 'SECRET-content'), step(2, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    directories = track_temporary_directories(monkeypatch, tmp_path)
    writes = watch_writes(monkeypatch, 1)
    out = tmp_path / 'out'
    args = ['merge', str(a), str(b), '--external-sort', '--max-sort-temp-bytes', '1']
    assert main(args + (['-o', str(out)] if file_output else [])) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == f'agentwrench export: error: {QUOTA_ERROR}\n'
    assert writes == []  # No oversized-event exception to the disk quota.
    assert not out.exists()
    assert directories and all(not path.exists() for path in directories)


def test_tiny_quota_subprocess_contract(tmp_path):
    a = write(tmp_path / 'a', 'A', [step(1, 2), step(2, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    result = cli(a, b, '--external-sort', '--max-sort-temp-bytes', 1)
    assert result.returncode == 2 and not result.stdout
    assert result.stderr == f'agentwrench export: error: {QUOTA_ERROR}\n'


@pytest.mark.parametrize('quota', [0, -1, True, False, 1.5, '100', float('inf')])
def test_api_quota_requires_positive_integer_before_io(tmp_path, quota):
    with pytest.raises(InputError, match='max sort temp bytes must be a positive integer'):
        list(merge.merge_jsonl([tmp_path / 'absent-a', tmp_path / 'absent-b'],
                              external_sort=True, max_sort_temp_bytes=quota))


@pytest.mark.parametrize('quota', ['0', '-1', '1.5', 'bad'])
def test_cli_invalid_quota_creates_no_output(tmp_path, quota):
    result = cli(tmp_path / 'absent-a', tmp_path / 'absent-b', '--external-sort',
                 '--max-sort-temp-bytes', quota, '-o', tmp_path / 'out')
    assert result.returncode == 2 and not result.stdout
    assert not (tmp_path / 'out').exists()


def test_quota_requires_external_sort_before_io(tmp_path):
    with pytest.raises(InputError, match='sort temporary-run quota requires --external-sort'):
        list(merge.merge_jsonl([tmp_path / 'a', tmp_path / 'b'], max_sort_temp_bytes=100))
    result = cli(tmp_path / 'a', tmp_path / 'b', '--max-sort-temp-bytes', 100)
    assert result.returncode == 2 and not result.stdout
    assert 'sort temporary-run quota requires --external-sort' in result.stderr


@pytest.mark.parametrize('buffer', [1, 8388608])
def test_exact_initial_run_quota_and_one_byte_short(tmp_path, monkeypatch, buffer):
    a_rows = [step(9, 2, 'Unicode\u2028\u0085\u2029'), step(7, 0)]
    b_rows = [step(4, 1)]
    a = write(tmp_path / 'a', 'A', a_rows)
    b = write(tmp_path / 'b', 'B', b_rows, ending='\r\n')
    total = sum(event_sizes(a_rows, 1, 'A') + event_sizes(b_rows, 2, 'B'))
    expected = cli(a, b, '--external-sort', '--sort-buffer-bytes', buffer)
    assert expected.returncode == 0, expected.stderr
    directories = track_temporary_directories(monkeypatch, tmp_path)
    writes = watch_writes(monkeypatch, total)
    result = list(merge.merge_jsonl([a, b], external_sort=True,
                  sort_buffer_bytes=buffer, max_sort_temp_bytes=total))
    assert ''.join(dumps(row) + '\n' for row in result) == expected.stdout
    assert sum(size for _, _, size in writes) == total
    # The SQLite index is deliberately outside this quota (even its initial
    # pages exceed these tiny run files); output header/provenance remain exact.
    assert total < 8192
    with pytest.raises(InputError, match=QUOTA_ERROR):
        list(merge.merge_jsonl([a, b], external_sort=True,
             sort_buffer_bytes=buffer, max_sort_temp_bytes=total - 1))
    assert all(not path.exists() for path in directories)


def test_real_fan_in_overlap_fails_only_during_merge_and_releases_deleted_bytes(tmp_path, monkeypatch, capsys):
    # 65 one-event runs force three intermediate groups at the production fan-in.
    # Peak is all original bytes plus the largest group's replacement bytes.
    assert merge.SORT_FAN_IN == 32
    a_rows = [step(n, n % 5, f'event {n}\u2028kept') for n in range(64, 0, -1)]
    b_rows = [step(1, 1)]
    a = write(tmp_path / 'a', 'A', a_rows)
    b = write(tmp_path / 'b', 'B', b_rows)
    sizes = event_sizes(a_rows, 1, 'A') + event_sizes(b_rows, 2, 'B')
    total = sum(sizes)
    peak = total + max(sum(sizes[n:n + 32]) for n in range(0, len(sizes), 32))
    expected = cli(a, b, '--external-sort', '--sort-buffer-bytes', 1)
    assert expected.returncode == 0, expected.stderr
    for quota in (total, peak - 1, peak):
        with monkeypatch.context() as patch:
            directories = track_temporary_directories(patch, tmp_path)
            writes = watch_writes(patch, quota)
            args = ['merge', str(a), str(b), '--external-sort', '--sort-buffer-bytes', '1',
                    '--max-sort-temp-bytes', str(quota), '-o', str(tmp_path / 'out')]
            code = main(args)
            captured = capsys.readouterr()
            assert not captured.out
            if quota < peak:
                assert code == 2 and captured.err == f'agentwrench export: error: {QUOTA_ERROR}\n'
                assert not (tmp_path / 'out').exists()
                if quota == total:
                    assert len(writes) == 65
                    assert all(name.startswith('run-0-') for name, _, _ in writes)
                else:
                    assert any(name.startswith('run-1-') for name, _, _ in writes)
            else:
                assert code == 0, captured.err
                assert (tmp_path / 'out').read_text() == expected.stdout
                assert (tmp_path / 'out').stat().st_mode & 0o777 == 0o600
                assert max(live + size for _, live, size in writes) == peak
                rows = merge.merge_jsonl([a, b], external_sort=True, sort_buffer_bytes=1,
                                        max_sort_temp_bytes=peak)
                assert next(rows)['type'] == 'merge'
                assert next(rows)['type'] == 'event'
                rows.close()
            assert directories and all(not path.exists() for path in directories)


@pytest.mark.parametrize('quota', [1, 1000000])
def test_quota_preserves_existing_files_and_symlinks(tmp_path, quota):
    a = write(tmp_path / 'a', 'A', [step(1, 2), step(2, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    existing = tmp_path / 'existing'
    existing.write_text('keep')
    link = tmp_path / 'link'
    link.symlink_to(existing)
    dangling = tmp_path / 'dangling'
    dangling.symlink_to(tmp_path / 'absent')
    for out in (existing, link, dangling, a):
        before = out.read_bytes() if out.exists() else None
        result = cli(a, b, '--external-sort', '--max-sort-temp-bytes', quota, '-o', out)
        assert result.returncode == 2 and not result.stdout
        assert (out.read_bytes() if out.exists() else None) == before
    assert link.is_symlink() and dangling.is_symlink()
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('file_output', [False, True])
def test_enough_quota_preserves_late_identity_conflict(tmp_path, monkeypatch, capsys, file_output):
    a_rows = [step(n, 2) for n in range(1, 66)] + [step(1, 2, 'SECRET-changed')]
    a = write(tmp_path / 'a', 'SECRET-session', a_rows)
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    directories = track_temporary_directories(monkeypatch, tmp_path)
    args = ['merge', str(a), str(b), '--external-sort', '--sort-buffer-bytes', '1',
            '--max-sort-temp-bytes', '1000000']
    assert main(args + (['-o', str(tmp_path / 'out')] if file_output else [])) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == ('agentwrench export: error: duplicate event identity: '
                            'input 1 line 2 conflicts with input 1 line 67\n')
    assert not (tmp_path / 'out').exists()
    assert directories and all(not path.exists() for path in directories)
