"""Source-selection contract for a strict merged transcript.

--source is repeatable, positive and declared; repeats are idempotent. Explicit
selection is a sorted unique list in Markdown and diagnostics; sources still
counts every declared header, excluded_events counts validated omitted events.
Default output/diagnostics are byte-identical. Selection never changes event
order, identities, content, timestamps or the full source catalogue. Header
validation precedes unknown-source checks; excluded events remain fully checked.
"""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from .test_render_merge import FIXTURE, GOLDEN, ROOT, cli, rows, write

SELECTED = FIXTURE.with_name('transcript-source-2.md')


def diagnostics(result):
    return json.loads(result.stderr.removeprefix(b'agentwrench export: '))


def test_selected_source_golden(tmp_path):
    result = cli(FIXTURE, '--source', '2')
    assert result.returncode == 0, result.stderr
    assert result.stdout == SELECTED.read_bytes()
    assert diagnostics(result) == {'rendered_events': 1, 'sources': 2,
                                   'selected_sources': [2], 'excluded_events': 2}
    output = tmp_path / 'selected.md'
    result = cli(FIXTURE, '--source', '2', '-o', output)
    assert result.returncode == 0 and not result.stdout, result.stderr
    assert output.read_bytes() == SELECTED.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('selection', [(1, 2), (2, 1), (2, 1, 2, 1)])
def test_multiple_repeated_sources_keep_equal_time_file_order(tmp_path, selection):
    values = rows()
    values[1:3] = reversed(values[1:3])  # equal instants, opposite ordinal order
    path = write(tmp_path / 'reversed', values)
    result = cli(path, *[v for n in selection for v in ('--source', n)])
    assert result.returncode == 0, result.stderr
    assert diagnostics(result) == {'rendered_events': 3, 'sources': 2,
                                   'selected_sources': [1, 2], 'excluded_events': 0}
    text = result.stdout.decode()
    assert text.index('Independent reply') < text.index('Hello 雪') < text.index('unmatched result')
    assert text.count('## Assistant · 1') == text.count('## User · 1') == 1
    assert text.count('### Source / session') == 3
    assert '2026-01-01T11:00:00+01:00' in text and '2026-01-01T10:00:00.000Z' in text
    again = cli(path, '--source', '1', '--source', '2')
    assert (result.stdout, result.stderr) == (again.stdout, again.stderr)


def test_omitted_selection_is_byte_identical():
    result = cli(FIXTURE)
    assert result.returncode == 0, result.stderr
    assert result.stdout == GOLDEN.read_bytes()
    assert result.stderr == b'agentwrench export: {"rendered_events": 3, "sources": 2}\n'


@pytest.mark.parametrize('value', ['0', '-1', 'True', 'true', 'False', 'false', '1.0', 'abc'])
def test_invalid_source_before_input_io(tmp_path, value):
    result = cli(tmp_path / 'absent', '--source', value, '-o', tmp_path / 'out')
    assert result.returncode == 2 and not result.stdout
    assert b'positive integer' in result.stderr or b'invalid int value' in result.stderr
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('file_output', [False, True])
@pytest.mark.parametrize('header_error', [False, True])
def test_unknown_source_after_complete_header_validation(tmp_path, file_output, header_error):
    values = rows()
    if header_error:
        values[0]['sources'][-1]['trajectory']['agent'] = []
    path = write(tmp_path / 'input', values)
    # Unknown selection is diagnosed before trying to read the bad tail.
    path.write_bytes(path.read_bytes() + b'{broken}\n')
    output = tmp_path / 'out'
    result = cli(path, '--source', '3', *(['-o', output] if file_output else []))
    assert result.returncode == 2 and not result.stdout and not output.exists()
    reason = b'expected normalized trajectory header' if header_error else b'selected source ordinal is not declared'
    assert b'render-merge input line 1:' in result.stderr and reason in result.stderr


@pytest.mark.parametrize('failure', ['malformed', 'unterminated', 'provenance', 'identity',
                                    'location', 'timestamp', 'decreasing', 'message', 'metrics'])
@pytest.mark.parametrize('file_output', [False, True])
def test_excluded_late_error_still_prevents_publication(tmp_path, failure, file_output):
    values = rows()
    event = values[-1]  # source 1, excluded after source 2 already rendered
    if failure == 'provenance': event['provenance']['session_id'] = 'SECRET'
    elif failure == 'identity': event['step']['step_id'] = event['provenance']['step_id'] = 1
    elif failure == 'location': event['provenance']['line'] = 2
    elif failure == 'timestamp': event['step']['timestamp'] = 'SECRET'
    elif failure == 'decreasing': event['step']['timestamp'] = '2025-01-01T00:00:00Z'
    elif failure == 'message': event['step']['message'] = None
    elif failure == 'metrics': event['step']['metrics'] = []
    path = write(tmp_path / 'input', values)
    if failure in ('malformed', 'unterminated'):
        path.write_bytes(path.read_bytes() + b'{SECRET}' + (b'\n' if failure == 'malformed' else b''))
    original = path.read_bytes()
    root = tmp_path / 'scratch'; root.mkdir(mode=0o750)
    (root / 'keep').write_bytes(b'keep')
    output = tmp_path / 'out'
    result = cli(path, '--source', '2', *(['-o', output] if file_output else ['--output-temp-dir', root]))
    assert result.returncode == 2 and not result.stdout and not output.exists()
    assert b'render-merge input line ' in result.stderr
    assert b'SECRET' not in result.stderr and b'Traceback' not in result.stderr
    assert path.read_bytes() == original
    assert [(p.name, p.read_bytes()) for p in root.iterdir()] == [('keep', b'keep')]
    assert root.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize('limit', ['file', 'line', 'records'])
def test_input_limits_include_excluded_events(tmp_path, monkeypatch, capsys, limit):
    from agentwrench.export import cli as module
    import agentwrench.export.render_merge as render
    values = rows()
    values[-1]['step']['message'] = 'excluded' * 300
    path = write(tmp_path / 'input', values)
    raw = path.read_bytes()
    constant, exact = {'file': ('MAX_FILE_BYTES', len(raw)),
                       'line': ('MAX_LINE_BYTES', max(len(p) + 1 for p in raw.split(b'\n')[:-1])),
                       'records': ('MAX_RECORDS', 4)}[limit]
    monkeypatch.setattr(render, constant, exact)
    assert module.main(['render-merge', str(path), '--source', '2']) == 0
    assert capsys.readouterr().out == SELECTED.read_text()
    monkeypatch.setattr(render, constant, exact - 1)
    for args in ([], ['-o', str(tmp_path / 'out')]):
        assert module.main(['render-merge', str(path), '--source', '2', *args]) == 2
        captured = capsys.readouterr()
        assert not captured.out and 'limit exceeded' in captured.err
        assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('file_output', [False, True])
def test_output_limit_counts_only_selected_rendering(tmp_path, file_output):
    expected = SELECTED.read_bytes()
    assert len(expected) < len(GOLDEN.read_bytes())
    for limit in (len(expected), len(expected) - 1, 1):
        output = tmp_path / f'out-{limit}'
        result = cli(FIXTURE, '--source', '2', '--max-output-bytes', limit,
                     *(['-o', output] if file_output else []))
        assert result.returncode == (0 if limit == len(expected) else 2), result.stderr
        if result.returncode:
            assert not result.stdout and not output.exists()
            assert b'render-merge output byte limit exceeded' in result.stderr
        elif file_output: assert output.read_bytes() == expected
        else: assert result.stdout == expected


@pytest.mark.parametrize('existing', ['file', 'symlink', 'input'])
def test_selected_output_preserves_existing_targets(tmp_path, existing):
    path = tmp_path / 'input'; path.write_bytes(FIXTURE.read_bytes())
    output = tmp_path / 'out'
    if existing == 'file': output.write_bytes(b'keep')
    elif existing == 'symlink': output.symlink_to(path)
    else: output = path
    before = output.read_bytes()
    result = cli(path, '--source', '2', '-o', output)
    assert result.returncode == 2 and not result.stdout
    assert output.read_bytes() == before and path.read_bytes() == FIXTURE.read_bytes()
    if existing == 'symlink': assert output.is_symlink()


def test_private_stage_only_publishes_after_excluded_eof(tmp_path, monkeypatch, capsys):
    import os
    from agentwrench.export import cli as module
    import agentwrench.export.render_merge as render
    root = tmp_path / 'scratch'; root.mkdir(mode=0o750)
    (root / 'keep').write_bytes(b'keep')
    root_link = tmp_path / 'link'; root_link.symlink_to(root, target_is_directory=True)
    stage, original, events = [], module.tempfile.TemporaryFile, render._Reader.events
    validated = []
    def reader(self, sources):
        for s, p in events(self, sources):
            validated.append(p['input'])
            yield s, p
        validated.append('EOF')
    def temp(**kwargs):
        assert Path(kwargs['dir']) == root_link
        f = original(**kwargs); stage.append(f)
        assert os.fstat(f.fileno()).st_mode & 0o777 == 0o600
        return f
    copy = module.shutil.copyfileobj
    def publish(source, target):
        assert validated == [1, 2, 1, 'EOF']
        assert source.read() == SELECTED.read_text(); source.seek(0)
        copy(source, target)
    monkeypatch.setattr(render._Reader, 'events', reader)
    monkeypatch.setattr(module.tempfile, 'TemporaryFile', temp)
    monkeypatch.setattr(module.shutil, 'copyfileobj', publish)
    assert module.main(['render-merge', str(FIXTURE), '--source', '2', '--output-temp-dir', str(root_link)]) == 0
    assert capsys.readouterr().out == SELECTED.read_text()
    assert len(stage) == 1 and stage[0].closed
    assert [(p.name, p.read_bytes()) for p in root.iterdir()] == [('keep', b'keep')]
    assert root.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize('bounds, rendered, excluded', [
    ([], 2, 2),
    (['--since', '2026-01-01T10:00:00Z', '--until', '2026-01-01T10:01:00Z'], 0, 1),
    (['--since', '2026-01-01T10:02:00Z', '--until', '2026-01-01T10:02:00Z'], 0, 0),
])
def test_real_merge_interval_selection_and_no_match(tmp_path, bounds, rendered, excluded):
    path = tmp_path / 'merge.jsonl'
    result = subprocess.run([sys.executable, '-m', 'agentwrench', 'export', 'merge',
                             str(FIXTURE.with_name('a.jsonl')), str(FIXTURE.with_name('b.jsonl')),
                             *bounds, '-o', str(path)], cwd=ROOT, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    original = path.read_bytes()
    result = cli(path, '--source', '2')
    assert result.returncode == 0, result.stderr
    assert diagnostics(result) == {'rendered_events': rendered, 'sources': 2,
                                   'selected_sources': [2], 'excluded_events': excluded}
    if bounds:
        assert b'No events from the selected sources recorded in this merged export.' in result.stdout
    else:
        assert result.stdout.index(b'B1') < result.stdout.index(b'B2')
        assert b'A1' not in result.stdout and b'A2' not in result.stdout
    assert path.read_bytes() == original


def test_single_selection_retains_nonconsecutive_ids_and_all_source_context(tmp_path):
    values = rows()
    values[-1]['step']['step_id'] = values[-1]['provenance']['step_id'] = 19
    values[0]['sources'] = list(reversed(values[0]['sources']))
    result = cli(write(tmp_path / 'input', values), '--source', '1', '--source', '1')
    assert result.returncode == 0, result.stderr
    assert diagnostics(result) == {'rendered_events': 2, 'sources': 2,
                                   'selected_sources': [1], 'excluded_events': 1}
    assert b'## System \xc2\xb7 19' in result.stdout and b'"step_id": 19' in result.stdout
    assert b'Independent reply' not in result.stdout
    assert b'codex' in result.stdout and b'claude' in result.stdout and 'B`雪'.encode() in result.stdout
    assert b'999' not in result.stdout


def test_select_maximum_declared_ordinal(tmp_path):
    values = rows()
    template = values[0]['sources'][0]['trajectory']
    # Header-only exports still declare the full allowed source range.
    header = dict(values[0], sources=[{'input': n, 'trajectory': dict(template, session_id=f'session-{n}')}
                                    for n in range(1, 65)])
    path = write(tmp_path / 'input', [header])
    result = cli(path, '--source', '64', '--source', '3', '--source', '64')
    assert result.returncode == 0, result.stderr
    assert diagnostics(result) == {'rendered_events': 0, 'sources': 64,
                                   'selected_sources': [3, 64], 'excluded_events': 0}
    assert b'session-1"' in result.stdout and b'session-64"' in result.stdout
    result = cli(path, '--source', '65')
    assert result.returncode == 2 and not result.stdout
    assert b'selected source ordinal is not declared' in result.stderr


@pytest.mark.parametrize('selection', [[], [True], [False], [0], [-1], [1.0], ['1']])
def test_internal_renderer_rejects_noninteger_selection_before_io(selection):
    from io import StringIO
    from agentwrench.export.render_merge import render_merge
    from agentwrench.export.model import InputError
    stream = StringIO()
    with pytest.raises(InputError, match='source ordinal must be a positive integer'):
        render_merge('/absent', stream, selected_sources=selection)
    assert not stream.getvalue()
