"""Merged view contract: prepare retention rules, original event identities.

Conversation keeps nonempty user/agent messages only; tools keeps truthy calls
or observations from any role (even results:[]), clearing messages/reasoning.
All fields validate before filtering. Source/time/view exclusions count once.
Full is byte compatible. Empty nonfull views succeed, and all bytes are bounded.
"""
from copy import deepcopy
import io
import json

import pytest

from .test_render_merge import FIXTURE, GOLDEN, cli, rows, write
from .test_render_merge_sources import diagnostics, SELECTED
from .test_render_merge_window import SINCE, UNTIL, COMBINED
from agentwrench.export import cli as command
from agentwrench.export import render_merge as renderer
from agentwrench.export.export import prepare
from agentwrench.export.model import InputError


@pytest.mark.parametrize('view', ['conversation', 'tools'])
def test_view_golden(tmp_path, view):
    expected = FIXTURE.with_name(f'transcript-{view}.md').read_bytes()
    result = cli(FIXTURE, '--view', view)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert diagnostics(result) == {'rendered_events': 2, 'sources': 2, 'view': view, 'excluded_events': 1}
    output = tmp_path / 'view.md'
    result = cli(FIXTURE, '--view', view, '-o', output)
    assert result.returncode == 0 and not result.stdout, result.stderr
    assert output.read_bytes() == expected and output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('args,golden', [([], GOLDEN), (['--source', 2], SELECTED),
    (['--source', 2, '--since', SINCE, '--until', UNTIL], COMBINED)])
def test_full_is_byte_identical(args, golden):
    omitted, explicit = cli(FIXTURE, *args), cli(FIXTURE, *args, '--view', 'full')
    assert omitted.returncode == explicit.returncode == 0, explicit.stderr
    assert omitted.stdout == explicit.stdout == golden.read_bytes()
    assert omitted.stderr == explicit.stderr


def test_role_message_call_result_retention_matches_prepare(tmp_path, monkeypatch):
    template = rows()[2]['step']
    steps = []
    # Truthiness is intentional: whitespace messages and nonempty observations
    # with empty results/unknown metadata count; {} and [] alone do not.
    for role in ('user', 'agent', 'system'):
        for message in ('', ' ', '[image omitted]'):
            for activity in ('none', 'reasoning', 'calls', 'results', 'empty', 'empty_results', 'metadata'):
                s = {'type': 'step', 'step_id': len(steps)*7+7, 'source': role,
                     'message': message, 'timestamp': SINCE, 'metrics': {'prompt_tokens': 999}}
                if activity == 'reasoning': s['extra'] = {'reasoning': 'private reasoning'}
                if activity == 'calls': s['tool_calls'] = deepcopy(template['tool_calls'])
                if activity == 'results':
                    s['observation'] = deepcopy(template['observation'])
                    s['observation']['results'][0]['extra'].update(record={'secret': 'hidden'}, orphan=True)
                if activity == 'empty': s.update(tool_calls=[], observation={})
                if activity == 'empty_results': s['observation'] = {'results': []}
                if activity == 'metadata': s['observation'] = {'record': 'retained metadata'}
                steps.append(s)
    doc = {'steps': deepcopy(steps), 'extra': {}}
    values = [rows()[0]] + [{'type': 'event', 'provenance': {'input': 1, 'line': i+2,
        'session_id': 'A<session>', 'step_id': s['step_id']}, 'step': s} for i,s in enumerate(steps)]
    path = write(tmp_path / 'matrix', values); original = path.read_bytes()
    real = renderer.markdown_step
    for view in ('conversation', 'tools', 'full'):
        projected = []
        def capture(s):
            projected.append(deepcopy(s)); return real(s)
        monkeypatch.setattr(renderer, 'markdown_step', capture)
        stream = io.StringIO()
        counts = renderer.render_merge(path, stream, view=view)
        expected = prepare(doc, view=view, redact='off', max_output_chars=0)['steps']
        expected_ids = [s['step_id'] for s in steps if (view == 'full' or
                        (view == 'conversation' and s['source'] in ('user', 'agent') and s['message']) or
                        (view == 'tools' and (s.get('tool_calls') or s.get('observation'))))]
        for s, sid in zip(expected, expected_ids): s['step_id'] = sid
        assert projected == expected
        assert counts['rendered_events'] == len(expected_ids)
        if view != 'full': assert counts['excluded_events'] == len(steps)-len(expected_ids)
        assert stream.getvalue().count('### Source / session') == len(expected_ids)
    assert path.read_bytes() == original and doc['steps'] == steps


@pytest.mark.parametrize('view', ['conversation', 'tools'])
def test_conjunction_ties_provenance_and_no_session_join(tmp_path, view):
    values = rows(); values[1:3] = reversed(values[1:3])
    values[3]['step']['tool_calls'] = deepcopy(values[1]['step']['tool_calls'])
    for event in values[1:]:
        event['step']['step_id'] *= 7; event['provenance']['step_id'] *= 7
    path = write(tmp_path / 'input', values)
    result = cli(path, '--view', view, '--source', 2, '--source', 1, '--source', 2,
                 '--since', SINCE, '--until', UNTIL)
    assert result.returncode == 0, result.stderr
    n = 2 if view == 'conversation' else 1
    assert diagnostics(result) == {'rendered_events': n, 'sources': 2, 'view': view,
        'selected_sources': [1,2], 'since': SINCE, 'until': UNTIL, 'excluded_events': 3-n}
    assert result.stdout.count(b'"step_id": 7') == n
    assert b'2026-01-01T11:00:00+01:00' in result.stdout
    assert b'999' not in result.stdout and b'Recorded summary' not in result.stdout
    if view == 'conversation':
        assert result.stdout.index(b'Independent reply') < result.stdout.index('Hello 雪'.encode())
        assert b'### Tool' not in result.stdout
    else:
        assert b'Independent reply' not in result.stdout and b'Hello' not in result.stdout
        assert result.stdout.count(b'shell [shared-call]') == 1
    # A view exclusion and source/time exclusion must count only once.
    result = cli(path, '--view', view, '--source', 1, '--until', UNTIL)
    assert result.returncode == 0, result.stderr
    assert diagnostics(result)['rendered_events'] == (1 if view == 'conversation' else 0)
    assert diagnostics(result)['excluded_events'] == (2 if view == 'conversation' else 3)


@pytest.mark.parametrize('view', ['conversation', 'tools'])
@pytest.mark.parametrize('empty', ['header', 'no_activity', 'selection'])
def test_empty_view_is_success(tmp_path, view, empty):
    values = rows()
    if empty == 'header': values = values[:1]
    elif empty == 'no_activity':
        values = values[:2]
        if view == 'conversation': values[1]['step'].update(source='system', message='system only')
    args = ['--source', 2] if empty == 'selection' else []
    if empty == 'selection': args += ['--since', SINCE, '--until', SINCE]
    result = cli(write(tmp_path / 'input', values), '--view', view, *args)
    assert result.returncode == 0, result.stderr
    assert diagnostics(result)['rendered_events'] == 0
    assert diagnostics(result)['excluded_events'] == len(values)-1
    assert b'No events matching the selected view and source/time selection recorded in this merged export.' in result.stdout
    assert b'### Source / session' not in result.stdout
    assert b'codex' in result.stdout and b'claude' in result.stdout


@pytest.mark.parametrize('view', ['conversation', 'tools'])
@pytest.mark.parametrize('excluded', [False, True])
@pytest.mark.parametrize('failure', ['reasoning', 'call', 'result', 'message', 'role', 'metrics',
                                    'identity', 'location', 'timestamp', 'decreasing', 'malformed', 'tail'])
@pytest.mark.parametrize('file_output', [False, True])
def test_hidden_or_excluded_late_errors_prevent_publication(tmp_path, view, excluded, failure, file_output):
    values = rows(); s = values[-1]['step']; p = values[-1]['provenance']
    if failure == 'reasoning': s['extra'] = {'reasoning': []}
    elif failure == 'call': s['tool_calls'] = [{'id': 'c', 'name': 'shell', 'arguments': []}]
    elif failure == 'result': s['observation']['results'][0]['content'] = None
    elif failure == 'message': s['message'] = None
    elif failure == 'role': s['source'] = 'SECRET'
    elif failure == 'metrics': s['metrics'] = []
    elif failure == 'identity': s['step_id'] = p['step_id'] = 1
    elif failure == 'location': p['line'] = 2
    elif failure == 'timestamp': s['timestamp'] = 'SECRET'
    elif failure == 'decreasing': s['timestamp'] = '2025-01-01T00:00:00Z'
    path = write(tmp_path / 'input', values)
    if failure in ('malformed','tail'): path.write_bytes(path.read_bytes()+b'{SECRET}'+(b'\n' if failure=='malformed' else b''))
    before = path.read_bytes(); root = tmp_path / 'scratch'; root.mkdir(mode=0o750)
    (root / 'keep').write_bytes(b'keep'); output = tmp_path / 'out'
    args = ['--source', 2, '--until', UNTIL] if excluded else []
    result = cli(path, '--view', view, *args, *(['-o', output] if file_output else ['--output-temp-dir', root]))
    assert result.returncode == 2 and not result.stdout and not output.exists()
    assert b'render-merge input line ' in result.stderr
    assert b'SECRET' not in result.stderr and b'Traceback' not in result.stderr
    assert path.read_bytes() == before
    assert [(p.name,p.read_bytes()) for p in root.iterdir()] == [('keep',b'keep')]
    assert root.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize('view', ['conversation', 'tools'])
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('file_output', [False, True])
def test_output_limit_includes_view_context_diagnostics_utf8_and_lf(tmp_path, view, empty, file_output):
    root = tmp_path / 'scratch'; root.mkdir()
    args = ['--view', view, '--source', 2, '--since', SINCE, '--until', SINCE if empty else UNTIL]
    baseline = cli(FIXTURE, *args); assert baseline.returncode == 0, baseline.stderr
    size = len(baseline.stdout); assert size > len(baseline.stdout.decode())
    for limit in (size,size-1,1):
        output = tmp_path / f'out-{limit}'
        result = cli(FIXTURE, *args, '--max-output-bytes', limit,
            *(['-o', output] if file_output else ['--output-temp-dir', root]))
        if limit == size:
            assert result.returncode == 0 and result.stderr == baseline.stderr
            assert (output.read_bytes() if file_output else result.stdout) == baseline.stdout
        else:
            assert result.returncode == 2 and not result.stdout and not output.exists()
            assert b'output byte limit exceeded' in result.stderr
        assert not list(root.iterdir())


@pytest.mark.parametrize('view', ['conversation', 'tools'])
@pytest.mark.parametrize('limit', ['file','line','records'])
def test_input_limits_cover_hidden_excluded_fields(tmp_path, monkeypatch, capsys, view, limit):
    values = rows(); values[-1]['step']['extra'] = {'reasoning': 'hidden'*2000}
    path = write(tmp_path / 'input', values)
    if limit == 'file': monkeypatch.setattr(renderer, 'MAX_FILE_BYTES', path.stat().st_size-1)
    elif limit == 'line': monkeypatch.setattr(renderer, 'MAX_LINE_BYTES', 2000)
    else: monkeypatch.setattr(renderer, 'MAX_RECORDS', 3)
    assert command.main(['render-merge', str(path), '--view', view, '--source', '2', '--until', UNTIL]) == 2
    captured = capsys.readouterr(); assert not captured.out and 'limit exceeded' in captured.err


def test_invalid_view_before_io(monkeypatch, capsys):
    def forbidden(*args, **kwargs): pytest.fail('invalid view opened input/output/staging')
    monkeypatch.setattr(command.os,'open',forbidden)
    monkeypatch.setattr(command.tempfile,'TemporaryFile',forbidden)
    with pytest.raises(SystemExit) as error:
        command.main(['render-merge','absent','--view','invalid','-o','absent-output'])
    assert error.value.code == 2 and not capsys.readouterr().out
    with pytest.raises(InputError, match='invalid view'):
        renderer.render_merge('absent', io.StringIO(), view='invalid')
