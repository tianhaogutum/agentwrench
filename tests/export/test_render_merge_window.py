"""Render-time UTC window: inclusive since, exclusive until, after validation.

Both endpoint strings appear in Markdown/stderr (null is unbounded). With active
bounds, excluded_events counts the union of time/source exclusions exactly once;
rendered_events/sources remain export counts. Endpoints fail safely before any
I/O; equal bounds are valid. Omitted bounds keep existing bytes. All excluded
records still consume input limits and strict validation, even beyond until.
"""
import io
import json

import pytest

from .test_render_merge import FIXTURE, GOLDEN, cli, rows, write
from .test_render_merge_sources import SELECTED, diagnostics
from agentwrench.export import cli as command
from agentwrench.export import render_merge as renderer
from agentwrench.export.model import InputError

SINCE = '2026-01-01T11:00:00+01:00'
UNTIL = '2026-01-01T10:00:00.000001Z'
WINDOW = FIXTURE.with_name('transcript-window.md')
COMBINED = FIXTURE.with_name('transcript-source-2-window.md')


def test_timestamp_window_golden(tmp_path):
    result = cli(FIXTURE, '--since', SINCE, '--until', UNTIL)
    assert result.returncode == 0, result.stderr
    assert result.stdout == WINDOW.read_bytes()
    assert diagnostics(result) == {'rendered_events': 2, 'sources': 2, 'excluded_events': 1,
                                   'since': SINCE, 'until': UNTIL}
    output = tmp_path / 'transcript.md'
    combined = cli(FIXTURE, '--source', 2, '--since', SINCE, '--until', UNTIL, '-o', output)
    assert combined.returncode == 0 and not combined.stdout, combined.stderr
    assert output.read_bytes() == COMBINED.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    assert diagnostics(combined) == {'rendered_events': 1, 'sources': 2, 'excluded_events': 2,
                                     'selected_sources': [2], 'since': SINCE, 'until': UNTIL}


@pytest.mark.parametrize('since,until,ids', [
    (SINCE, UNTIL, [1, 1]),
    (None, '2026-01-01T10:00:00Z', []),
    ('2026-01-01T10:00:00.000001Z', None, [2]),
    ('2026-01-01T09:59:59.999999Z', UNTIL, [1, 1]),
    ('2026-01-01T09:00:00-01:00', '2026-01-01T11:01:00+01:00', [1, 1]),
    (SINCE, '2026-01-01T10:00:00Z', []),
    ('2027-01-01T00:00:00Z', None, []),
    (None, '2025-01-01T00:00:00Z', []),
    (SINCE, None, [1, 1, 2]),
])
def test_window_boundaries_and_original_steps(since, until, ids):
    args = ([] if since is None else ['--since', since]) + ([] if until is None else ['--until', until])
    result = cli(FIXTURE, *args)
    assert result.returncode == 0, result.stderr
    assert diagnostics(result) == {'rendered_events': len(ids), 'sources': 2,
                                   'excluded_events': 3-len(ids), 'since': since, 'until': until}
    assert result.stdout.count(b'### Source / session') == len(ids)
    assert b'A&lt;session&gt;' in result.stdout and 'B`雪'.encode() in result.stdout
    assert b'All declared sources are selected.' in result.stdout
    assert b'input_tokens' not in result.stdout and b'token_usage' not in result.stdout
    if not ids:
        assert b'No events matching the active selection recorded in this merged export.' in result.stdout
    if ids == [2]:
        assert b'## System \xc2\xb7 2' in result.stdout
        assert b'Hello' not in result.stdout and b'Independent reply' not in result.stdout


@pytest.mark.parametrize('selection,expected', [([1], 1), ([2], 1), ([2, 1, 2], 2)])
def test_conjunction_retains_equal_time_file_order_and_nonconsecutive_ids(tmp_path, selection, expected):
    values = rows()
    values[1:3] = reversed(values[1:3])
    for e in values[1:]:
        e['step']['step_id'] *= 7
        e['provenance']['step_id'] *= 7
    path = write(tmp_path / 'input', values)
    result = cli(path, '--since', SINCE, '--until', UNTIL,
                 *[v for n in selection for v in ('--source', n)])
    assert result.returncode == 0, result.stderr
    assert diagnostics(result) == {'rendered_events': expected, 'sources': 2, 'excluded_events': 3-expected,
                                   'selected_sources': sorted(set(selection)), 'since': SINCE, 'until': UNTIL}
    assert result.stdout.count(b'### Source / session') == expected
    assert result.stdout.count(b'"step_id": 7') == expected
    assert b'unmatched result' not in result.stdout
    if expected == 2:
        assert result.stdout.index(b'Independent reply') < result.stdout.index('Hello 雪'.encode())
        assert b'2026-01-01T11:00:00+01:00' in result.stdout
        assert b'2026-01-01T10:00:00.000Z' in result.stdout


@pytest.mark.parametrize('file_output', [False, True])
@pytest.mark.parametrize('endpoint', ['--since', '--until'])
@pytest.mark.parametrize('value', ['', 'SECRET', '2026-01-01', '2026-01-01T10:00:00',
    '2026-01-01T10:00:00.1234567Z', '2026-02-30T10:00:00Z', '2026-01-01T10:00:00+24:00',
    '0001-01-01T00:00:00+01:00'])
def test_invalid_endpoint_before_io(tmp_path, monkeypatch, capsys, endpoint, value, file_output):
    def unexpected(*args, **kwargs):
        pytest.fail('endpoint must fail before input, output or stage I/O')
    monkeypatch.setattr(command.os, 'open', unexpected)
    monkeypatch.setattr(command.tempfile, 'TemporaryFile', unexpected)
    args = ['-o', str(tmp_path / 'out')] if file_output else ['--output-temp-dir', 'absent-root']
    assert command.main(['render-merge', 'absent', endpoint, value, *args]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == (f'agentwrench export: error: {endpoint} requires RFC3339 with timezone '
                            'and at most 6 fractional digits\n')


@pytest.mark.parametrize('since,until', [(UNTIL, SINCE),
    ('2026-01-01T10:00:00Z', '2026-01-01T10:30:00+01:00')])
def test_reversed_endpoints_before_io(monkeypatch, capsys, since, until):
    def unexpected(*args, **kwargs): pytest.fail('reversed window must fail before I/O')
    monkeypatch.setattr(command.os, 'open', unexpected)
    monkeypatch.setattr(command.tempfile, 'TemporaryFile', unexpected)
    assert command.main(['render-merge', 'absent', '--since', since, '--until', until]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err == 'agentwrench export: error: --since must be before or equal to --until\n'


@pytest.mark.parametrize('failure,reason', [
    ('malformed', 'invalid UTF-8 or JSON record'), ('unterminated', 'invalid UTF-8 or JSON record'),
    ('provenance', 'provenance does not match'), ('identity', 'duplicate event identity'),
    ('location', 'duplicate source location'), ('timestamp', 'timestamp requires RFC3339'),
    ('decreasing', 'timestamps are not nondecreasing'), ('message', 'expected step'),
])
@pytest.mark.parametrize('file_output', [False, True])
def test_errors_beyond_until_prevent_publication(tmp_path, failure, reason, file_output):
    values = rows()
    # Last event remains beyond until even for the decreasing-time error.
    values[-1]['step']['timestamp'] = '2026-01-01T09:59:59Z' if failure == 'decreasing' else '2026-01-01T10:01:00Z'
    event = values[-1]
    if failure == 'provenance': event['provenance']['session_id'] = 'SECRET'
    elif failure == 'identity': event['step']['step_id'] = event['provenance']['step_id'] = 1
    elif failure == 'location': event['provenance']['line'] = 2
    elif failure == 'timestamp': event['step']['timestamp'] = 'SECRET'
    elif failure == 'message': event['step']['message'] = []
    path = write(tmp_path / 'input', values)
    if failure in ('malformed', 'unterminated'):
        path.write_bytes(path.read_bytes() + b'{SECRET}' + (b'\n' if failure == 'malformed' else b''))
    original = path.read_bytes()
    root = tmp_path / 'scratch'; root.mkdir(mode=0o750)
    (root / 'keep').write_bytes(b'user data')
    output = tmp_path / 'out'
    args = ['-o', output] if file_output else ['--output-temp-dir', root]
    # All events excluded in decreasing case; others render a valid prefix.
    until = '2025-01-01T00:00:00Z' if failure == 'decreasing' else UNTIL
    result = cli(path, '--until', until, '--source', 2, *args)
    assert result.returncode == 2 and not result.stdout and not output.exists()
    assert reason.encode() in result.stderr and b'SECRET' not in result.stderr
    assert path.read_bytes() == original
    assert [(p.name, p.read_bytes()) for p in root.iterdir()] == [('keep', b'user data')]
    assert root.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('file_output', [False, True])
def test_output_limit_includes_endpoints_context_diagnostics_and_utf8(tmp_path, empty, file_output):
    args = ['--source', 2, '--since', SINCE, '--until', SINCE if empty else UNTIL]
    baseline = cli(FIXTURE, *args)
    assert baseline.returncode == 0, baseline.stderr
    size = len(baseline.stdout)
    assert empty or size > len(baseline.stdout.decode())
    for limit in (size, size-1, 1):
        output = tmp_path / f'out-{limit}'
        result = cli(FIXTURE, *args, '--max-output-bytes', limit, *(['-o', output] if file_output else []))
        if limit == size:
            assert result.returncode == 0 and result.stderr == baseline.stderr
            assert (output.read_bytes() if file_output else result.stdout) == baseline.stdout
        else:
            assert result.returncode == 2 and not result.stdout and not output.exists()
            assert result.stderr == b'agentwrench export: error: render-merge output byte limit exceeded\n'


@pytest.mark.parametrize('limit', ['file', 'line', 'records'])
def test_input_limits_include_excluded_records(tmp_path, monkeypatch, capsys, limit):
    values = rows(); values[-1]['step']['message'] = 'x' * 10000
    path = write(tmp_path / 'input', values)
    if limit == 'file': monkeypatch.setattr(renderer, 'MAX_FILE_BYTES', path.stat().st_size-1)
    elif limit == 'line': monkeypatch.setattr(renderer, 'MAX_LINE_BYTES', 2000)
    else: monkeypatch.setattr(renderer, 'MAX_RECORDS', 3)
    assert command.main(['render-merge', str(path), '--until', UNTIL]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert ('record limit' if limit == 'records' else f'{limit} byte limit') in captured.err


@pytest.mark.parametrize('selected', [False, True])
def test_omission_unchanged_and_header_only_window(tmp_path, selected):
    result = cli(FIXTURE, *(['--source', 2] if selected else []))
    assert result.returncode == 0 and result.stdout == (SELECTED if selected else GOLDEN).read_bytes()
    path = write(tmp_path / 'empty', rows()[:1])
    result = cli(path, '--since', SINCE, *(['--source', 2] if selected else []))
    assert result.returncode == 0, result.stderr
    expected = {'rendered_events': 0, 'sources': 2, 'excluded_events': 0, 'since': SINCE, 'until': None}
    if selected: expected['selected_sources'] = [2]
    assert diagnostics(result) == expected
    assert b'No events matching the active selection recorded in this merged export.' in result.stdout


@pytest.mark.parametrize('kind', ['input', 'file', 'link', 'dangling'])
def test_existing_paths_preserved_with_window(tmp_path, kind):
    path = tmp_path / 'input'; path.write_bytes(FIXTURE.read_bytes())
    output = tmp_path / 'out'
    if kind == 'input': output = path
    elif kind == 'file': output.write_bytes(b'keep')
    else: output.symlink_to(path if kind == 'link' else tmp_path / 'absent')
    result = cli(path, '--since', SINCE, '-o', output)
    assert result.returncode == 2 and not result.stdout
    assert path.read_bytes() == FIXTURE.read_bytes()
    if kind in ('link', 'dangling'): assert output.is_symlink()
    if kind == 'file': assert output.read_bytes() == b'keep'
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('kwargs,error', [({'since': 'SECRET'}, '--since requires RFC3339'),
    ({'until': True}, '--until requires RFC3339'),
    ({'since': UNTIL, 'until': SINCE}, '--since must be before or equal')])
def test_internal_renderer_endpoint_guards_before_io(monkeypatch, kwargs, error):
    def unexpected(*args, **kw): pytest.fail('internal endpoint validation must precede input I/O')
    monkeypatch.setattr(renderer.os, 'open', unexpected)
    with pytest.raises(InputError, match=error): renderer.render_merge('absent', io.StringIO(), **kwargs)
