"""Validation-only merged export contract: no originals, rendering or output I/O.

One regular merge-v1 file, same strict reader subset/bounds as render-merge.
Consume to EOF; success is stderr JSON {validated_events, sources}, empty stdout.
Header-only exports are valid; failures have safe physical-line diagnostics and
no success counts. Reject all presentation/output options before input I/O.
Counts describe this export, not whole sources or authentic provenance.
"""
import builtins
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agentwrench.export import cli as module
import agentwrench.export.render_merge as reader_module

FIXTURE = Path(__file__).parent / 'fixtures/merge/transcript.jsonl'


def rows():
    return [json.loads(line) for line in FIXTURE.read_bytes().split(b'\n') if line]


def encode(values):
    return ''.join(json.dumps(v, ensure_ascii=False) + '\n' for v in values).encode()


def test_source_free_validation_only_io_contract(tmp_path, monkeypatch, capsys):
    # Copy the shared export alone: no original normalized sources in this root.
    path = tmp_path / 'shared.jsonl'
    raw = FIXTURE.read_bytes()
    path.write_bytes(raw)
    keep = tmp_path / 'existing.md'
    keep.write_bytes(b'preserve user file')
    scratch = tmp_path / 'scratch'
    scratch.mkdir(mode=0o750)
    (scratch / 'keep').write_bytes(b'preserve root contents')
    opened, streams, descriptors = [], [], []
    original_open, original_fdopen, original_io_open = os.open, os.fdopen, io.open

    def forbidden(*args, **kwargs):
        raise AssertionError('validation attempted rendering, output or original-source I/O')

    def guarded_open(name, flags, *args, **kwargs):
        assert Path(name) == path
        assert flags == os.O_RDONLY | os.O_NONBLOCK
        opened.append((str(name), flags))
        fd = original_open(name, flags, *args, **kwargs)
        descriptors.append(fd)
        return fd

    def guarded_io_open(file, mode='r', *args, **kwargs):
        # os.fdopen delegates to io.open on the existing read-only descriptor.
        assert type(file) is int and file in descriptors and mode == 'rb'
        return original_io_open(file, mode, *args, **kwargs)

    def guarded_fdopen(fd, mode, *args, **kwargs):
        assert mode == 'rb'
        stream = original_fdopen(fd, mode, *args, **kwargs)
        streams.append(stream)
        return stream

    with monkeypatch.context() as guard:
        guard.setattr(os, 'open', guarded_open)
        guard.setattr(os, 'fdopen', guarded_fdopen)
        guard.setattr(builtins, 'open', forbidden)
        guard.setattr(io, 'open', guarded_io_open)
        for name in ('load_session', 'validate_session', 'merge_jsonl', 'render_merge',
                     'markdown', 'prepare', '_stdout_stage'):
            guard.setattr(module, name, forbidden)
        for name in ('markdown_step', 'fence'):
            guard.setattr(reader_module, name, forbidden)
        for name in ('TemporaryFile', 'NamedTemporaryFile', 'TemporaryDirectory', 'mkstemp'):
            guard.setattr(module.tempfile, name, forbidden)
        guard.setattr(module.shutil, 'copyfileobj', forbidden)
        guard.setattr(module.sqlite3, 'connect', forbidden)
        guard.setattr(sys.stdout, 'write', forbidden)
        assert module.main(['validate-merge', str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ''
    assert json.loads(captured.err.removeprefix('agentwrench export: ')) == {'validated_events': 3, 'sources': 2}
    assert len(opened) == 1 and len(streams) == 1 and streams[0].closed
    assert path.read_bytes() == raw and keep.read_bytes() == b'preserve user file'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['existing.md', 'scratch', 'shared.jsonl']
    assert [p.name for p in scratch.iterdir()] == ['keep']
    assert (scratch / 'keep').read_bytes() == b'preserve root contents'
    assert scratch.stat().st_mode & 0o777 == 0o750
    print(json.dumps({'observed_read_opens': opened, 'closed': streams[0].closed,
                      'forbidden_io_guards': 'render/projection/stage/copy/temp/index/other files/stdout writes'}))


@pytest.mark.parametrize('kind', ['header', 'normal', 'framing', 'more_sources'])
def test_exact_export_counts(tmp_path, capsys, kind):
    values = rows()
    if kind == 'header': values = values[:1]
    if kind == 'more_sources':
        # A declared source with no retained events still counts; source metrics
        # and unknown metadata are neither recomputed nor treated as event counts.
        h = json.loads(json.dumps(values[0]['sources'][0]))
        h['input'] = 3
        values[0]['sources'].append(h)
    raw = encode(values)
    if kind == 'framing': raw = b'\xef\xbb\xbf' + raw.rstrip(b'\n').replace(b'\n', b'\r\n \t\r\n')
    path = tmp_path / 'merged'
    path.write_bytes(raw)
    assert module.main(['validate-merge', str(path)]) == 0
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err.removeprefix('agentwrench export: ')) == {
        'validated_events': len(values)-1, 'sources': 3 if kind == 'more_sources' else 2}
    assert path.read_bytes() == raw


@pytest.mark.parametrize('change', ['header', 'ordinal', 'source', 'provenance', 'identity',
                                  'location', 'order', 'timestamp', 'integer', 'message',
                                  'reasoning', 'call', 'result', 'metrics'])
def test_reader_contract_errors(tmp_path, capsys, change):
    values = rows()
    event = values[-1]
    step, p = event['step'], event['provenance']
    expected_line = 4
    if change == 'header': values[0]['schema_version'] = 'SECRET'; expected_line = 1
    elif change == 'ordinal': values[0]['sources'][1]['input'] = True; expected_line = 1
    elif change == 'source': p['input'] = 99
    elif change == 'provenance': p['session_id'] = 'SECRET'
    elif change == 'identity': step['step_id'] = p['step_id'] = 1
    elif change == 'location': p['line'] = 2
    elif change == 'order': step['timestamp'] = '2000-01-01T00:00:00Z'
    elif change == 'timestamp': step['timestamp'] = 'SECRET'
    elif change == 'integer': p['step_id'] = float(p['step_id'])
    elif change == 'message': step['message'] = None
    elif change == 'reasoning': step['extra'] = {'reasoning': []}
    elif change == 'call': step['tool_calls'] = [{'id':'x','name':'x','arguments':[]}]
    elif change == 'result': step['observation']['results'][0]['content'] = None
    elif change == 'metrics': step['metrics'] = []
    path = tmp_path / 'SECRET-input'
    raw = encode(values)
    path.write_bytes(raw)
    assert module.main(['validate-merge', str(path)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and 'validated_events' not in captured.err
    assert f'validate-merge input line {expected_line}:' in captured.err
    assert 'SECRET' not in captured.err and 'Traceback' not in captured.err
    assert path.read_bytes() == raw


@pytest.mark.parametrize('tail', [b'{SECRET', b'{SECRET}\n', b'\xff', b'[]',
                                  b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'])
def test_strict_entire_tail_physical_lines(tmp_path, capsys, tail):
    path = tmp_path / 'SECRET-input'
    path.write_bytes(FIXTURE.read_bytes() + b' \t\r\n' + tail)
    assert module.main(['validate-merge', str(path)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and 'validated_events' not in captured.err
    assert 'validate-merge input line 6:' in captured.err
    assert 'SECRET' not in captured.err and 'Traceback' not in captured.err


@pytest.mark.parametrize('option', [['-o','existing'], ['--output','existing'],
    ['--output-temp-dir','scratch'], ['--max-output-bytes','1'], ['--source','1'],
    ['--since','2026-01-01T00:00:00Z'], ['--until','2026-01-01T00:00:00Z'],
    ['--view','full'], ['--redact','off'], ['--check'], ['--external-sort']])
def test_unsupported_options_before_io(monkeypatch, capsys, option):
    def forbidden(*args, **kwargs): raise AssertionError('option rejection must precede I/O')
    monkeypatch.setattr(os, 'open', forbidden)
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(io, 'open', forbidden)
    monkeypatch.setattr(module, '_stdout_stage', forbidden)
    with pytest.raises(SystemExit) as exc:
        module.main(['validate-merge', 'absent-input', *option])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out and 'unrecognized arguments:' in captured.err


@pytest.mark.parametrize('limit', ['file', 'line', 'record'])
def test_existing_bounds_exact_and_over(tmp_path, monkeypatch, capsys, limit):
    path = tmp_path / 'merged'
    raw = FIXTURE.read_bytes()
    path.write_bytes(raw)
    constant, exact = {'file': ('MAX_FILE_BYTES',len(raw)),
                       'line': ('MAX_LINE_BYTES',max(len(p)+1 for p in raw.split(b'\n')[:-1])),
                       'record': ('MAX_RECORDS',4)}[limit]
    monkeypatch.setattr(reader_module, constant, exact)
    assert module.main(['validate-merge', str(path)]) == 0
    assert not capsys.readouterr().out
    monkeypatch.setattr(reader_module, constant, exact-1)
    assert module.main(['validate-merge', str(path)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and f'{limit} ' in captured.err
    assert 'validated_events' not in captured.err


@pytest.mark.parametrize('kind', ['missing', 'empty', 'directory', 'fifo'])
def test_bad_input_safe_and_nonblocking(tmp_path, kind):
    path = tmp_path / 'SECRET-input'
    if kind == 'empty': path.touch()
    elif kind == 'directory': path.mkdir()
    elif kind == 'fifo': os.mkfifo(path)
    result = subprocess.run([sys.executable, '-m', 'agentwrench', 'export', 'validate-merge', str(path)],
                            capture_output=True, timeout=5)
    assert result.returncode == 2 and not result.stdout
    assert b'SECRET' not in result.stderr and b'Traceback' not in result.stderr
    assert b'validated_events' not in result.stderr
