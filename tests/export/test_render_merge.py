"""Merged transcript contract (no raw adapter/import):

Require one merge-v1 header, 2..64 unique contiguous positive source ordinals,
known normalized trajectory headers and integer step IDs. Each event must match
its source session and exact integer step ID in provenance, with a positive
original physical line >1, required role/message/time, and well-shaped rendered
optional fields. Duplicate identities/locations and decreasing times fail.
Equal-time records retain file order; sessions/calls are never coalesced.
All parse/validation/output-limit failures publish no stdout/new output.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]  # repository root
FIXTURE = ROOT / 'tests/export/fixtures/merge/transcript.jsonl'
GOLDEN = FIXTURE.with_suffix('.md')


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'agentwrench', 'export', 'render-merge', *map(str, args)],
                          cwd=ROOT, capture_output=True, timeout=15)


def rows():
    return [json.loads(line) for line in FIXTURE.read_text().split('\n') if line]


def write(path, values):
    path.write_text(''.join(json.dumps(v, ensure_ascii=False) + '\n' for v in values))
    return path


def test_two_session_golden_transcript(tmp_path):
    result = cli(FIXTURE)
    assert result.returncode == 0, result.stderr
    assert result.stdout == GOLDEN.read_bytes()
    assert json.loads(result.stderr.removeprefix(b'agentwrench export: ')) == {'rendered_events': 3, 'sources': 2}
    output = tmp_path / 'transcript.md'
    result = cli(FIXTURE, '-o', output)
    assert result.returncode == 0 and result.stdout == b''
    assert output.read_bytes() == GOLDEN.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600


# All cases corrupt the last record where possible, after renderable content.
@pytest.mark.parametrize('change', [
    'version', 'raw_header', 'one_source', 'duplicate_ordinal', 'bool_ordinal',
    'gap_ordinal', 'source_session', 'source_agent', 'source_extra', 'source_type',
    'event_type', 'provenance', 'unknown_input', 'bool_input', 'wrong_session',
    'wrong_id', 'bool_id', 'string_id', 'float_provenance_id', 'missing_line',
    'bad_line', 'duplicate_line', 'step_type', 'role', 'message', 'missing_timestamp',
    'timestamp', 'decreasing', 'duplicate_identity', 'extra', 'reasoning', 'calls',
    'call_name', 'call_id', 'call_arguments', 'call_extra', 'observation', 'results',
    'content', 'result_id', 'result_extra', 'orphan', 'metrics',
])
@pytest.mark.parametrize('file_output', [False, True])
def test_invalid_envelopes_never_publish(tmp_path, change, file_output):
    values = rows()
    header, event = values[0], values[-1]
    source = header['sources'][1]
    step, p = event['step'], event['provenance']
    if change == 'version': header['schema_version'] = 'SECRET'
    elif change == 'raw_header': values[0] = source['trajectory']
    elif change == 'one_source': header['sources'].pop()
    elif change == 'duplicate_ordinal': source['input'] = 1
    elif change == 'bool_ordinal': source['input'] = True
    elif change == 'gap_ordinal': source['input'] = 3
    elif change == 'source_session': source['trajectory']['session_id'] = 'unknown'
    elif change == 'source_agent': source['trajectory']['agent'] = []
    elif change == 'source_extra': source['trajectory']['extra'] = None
    elif change == 'source_type': source['trajectory']['type'] = 'SECRET'
    elif change == 'event_type': event['type'] = 'SECRET'
    elif change == 'provenance': event['provenance'] = []
    elif change == 'unknown_input': p['input'] = 3
    elif change == 'bool_input': p['input'] = True
    elif change == 'wrong_session': p['session_id'] = 'SECRET'
    elif change == 'wrong_id': p['step_id'] = 99
    elif change == 'bool_id': step['step_id'] = p['step_id'] = True
    elif change == 'string_id': step['step_id'] = p['step_id'] = '2'
    elif change == 'float_provenance_id': p['step_id'] = 2.0
    elif change == 'missing_line': p.pop('line')
    elif change == 'bad_line': p['line'] = 1
    elif change == 'duplicate_line': p['line'] = 2
    elif change == 'step_type': step['type'] = 'SECRET'
    elif change == 'role': step['source'] = 'SECRET'
    elif change == 'message': step['message'] = None
    elif change == 'missing_timestamp': step.pop('timestamp')
    elif change == 'timestamp': step['timestamp'] = 'SECRET'
    elif change == 'decreasing': step['timestamp'] = '2025-01-01T00:00:00Z'
    elif change == 'duplicate_identity': step['step_id'] = p['step_id'] = 1
    elif change == 'extra': step['extra'] = []
    elif change == 'reasoning': step['extra'] = {'reasoning': 123}
    elif change == 'calls': step['tool_calls'] = {}
    elif change.startswith('call_'):
        step['tool_calls'] = [{'id':'c','name':'shell','arguments':{}}]
        step['tool_calls'][0][change.removeprefix('call_')] = None
    elif change == 'observation': step['observation'] = []
    elif change == 'results': step['observation']['results'] = {}
    elif change == 'content': step['observation']['results'][0]['content'] = None
    elif change == 'result_id': step['observation']['results'][0]['source_call_id'] = 1
    elif change == 'result_extra': step['observation']['results'][0]['extra'] = []
    elif change == 'orphan': step['observation']['results'][0]['extra']['orphan'] = 'yes'
    elif change == 'metrics': step['metrics'] = []
    path = write(tmp_path / 'SECRET-input', values)
    output = tmp_path / 'out.md'
    result = cli(path, *(['-o', output] if file_output else []))
    assert result.returncode == 2, result.stderr
    assert result.stdout == b'' and not output.exists()
    assert b'SECRET' not in result.stderr and b'Traceback' not in result.stderr
    assert b'render-merge input line ' in result.stderr


@pytest.mark.parametrize('tail', [b'{broken}\n', b'{broken}', b'{"x":1,"x":2}',
                                  b'{"x":NaN}\n', b'{"x":1e999}\n', b'[]\n', b'\xff\n'])
def test_malformed_tail_is_strict(tmp_path, tail):
    path = tmp_path / 'input'
    path.write_bytes(FIXTURE.read_bytes() + b'\n' + tail)
    for args in ([], ['-o', tmp_path / 'out']):
        result = cli(path, *args)
        assert result.returncode == 2 and not result.stdout
        assert b'line 6:' in result.stderr
        assert not (tmp_path / 'out').exists()


def test_metadata_only_and_framing(tmp_path):
    header = rows()[0]
    path = write(tmp_path / 'empty.jsonl', [header])
    result = cli(path)
    assert result.returncode == 0, result.stderr
    assert b'No events recorded in this merged export.' in result.stdout
    assert b'## User' not in result.stdout and b'## Assistant' not in result.stdout
    assert b'999' not in result.stdout  # no recomputed or relabelled source metrics
    assert json.loads(result.stderr.removeprefix(b'agentwrench export: ')) == {'rendered_events': 0, 'sources': 2}
    raw = FIXTURE.read_bytes()
    for framed in (b'\xef\xbb\xbf\r\n' + raw.replace(b'\n', b'\r\n'), raw.rstrip(b'\n')):
        path.write_bytes(framed)
        result = cli(path)
        assert result.returncode == 0, result.stderr
        assert result.stdout == GOLDEN.read_bytes()


def test_equal_time_record_order_and_session_isolation(tmp_path):
    values = rows()
    # Both first records have the same instant. Deliberately reverse ordinal order.
    values[1:3] = reversed(values[1:3])
    # Same call ID is legal in separate sessions; do not combine their results.
    values[-1]['step']['tool_calls'] = deepcopy(values[1]['step']['tool_calls'])
    result = cli(write(tmp_path / 'input', values))
    assert result.returncode == 0, result.stderr
    assert result.stdout.index(b'Independent reply') < result.stdout.index('Hello 雪'.encode())
    assert result.stdout.count(b'shell [shared-call]') == 2
    assert result.stdout.count(b'### Tool result\n') == 1


@pytest.mark.parametrize('existing', ['file', 'symlink', 'input'])
def test_never_replace_output(tmp_path, existing):
    path = tmp_path / 'input'
    path.write_bytes(FIXTURE.read_bytes())
    output = tmp_path / 'out'
    if existing == 'file': output.write_bytes(b'keep')
    elif existing == 'symlink': output.symlink_to(path)
    else: output = path
    before = output.read_bytes()
    result = cli(path, '-o', output)
    assert result.returncode == 2 and not result.stdout
    assert output.read_bytes() == before and path.read_bytes() == FIXTURE.read_bytes()
    if existing == 'symlink': assert output.is_symlink()


def test_output_byte_limit_includes_utf8_header_and_all_events(tmp_path):
    size = len(GOLDEN.read_bytes())
    for limit, success in [(size, True), (size - 1, False), (1, False)]:
        for args in ([], ['-o', tmp_path / f'out-{limit}']):
            result = cli(FIXTURE, '--max-output-bytes', limit, *args)
            assert result.returncode == (0 if success else 2), result.stderr
            if not success:
                assert result.stdout == b'' and not (tmp_path / f'out-{limit}').exists()
                assert b'render-merge output byte limit exceeded' in result.stderr
            elif not args: assert result.stdout == GOLDEN.read_bytes()


@pytest.mark.parametrize('value', ['0', '-1', 'abc'])
def test_bad_limit_before_io(tmp_path, value):
    result = cli(tmp_path / 'absent', '--max-output-bytes', value)
    assert result.returncode == 2 and not result.stdout
    assert b'positive integer' in result.stderr or b'invalid int' in result.stderr


def test_stage_root_conflict_and_unusable_roots(tmp_path):
    output = tmp_path / 'out'
    result = cli(tmp_path / 'absent', '-o', output, '--output-temp-dir', '')
    assert result.returncode == 2 and not result.stdout and not output.exists()
    assert b'cannot be combined with --output' in result.stderr
    for root in ('', str(tmp_path / 'absent'), str(FIXTURE)):
        result = cli(FIXTURE, '--output-temp-dir', root)
        assert result.returncode == 2 and not result.stdout
        assert b'output temporary directory must be an existing writable directory' in result.stderr


def test_real_merge_and_interval_exports(tmp_path):
    inputs = [ROOT / 'tests/export/fixtures/merge' / name for name in ('a.jsonl', 'b.jsonl')]
    for bounds in ([], ['--since','2026-01-01T10:02:00Z','--until','2026-01-01T10:02:00Z']):
        result = subprocess.run([sys.executable, '-m', 'agentwrench', 'export', 'merge', *map(str, inputs), *bounds],
                                capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        path = tmp_path / 'merged.jsonl'
        path.write_bytes(result.stdout)
        rendered = cli(path)
        assert rendered.returncode == 0, rendered.stderr
        count = 0 if bounds else 4
        assert rendered.stdout.count(b'### Source / session') == count
        assert json.loads(rendered.stderr.removeprefix(b'agentwrench export: '))['rendered_events'] == count


@pytest.mark.parametrize('failure', [None, 'late', 'limit', 'write', 'copy'])
def test_private_staging_and_cleanup(tmp_path, monkeypatch, capsys, failure):
    import os
    from agentwrench.export import cli as module
    path = tmp_path / 'input'
    path.write_bytes(FIXTURE.read_bytes() + (b'{broken}' if failure == 'late' else b''))
    root = tmp_path / 'scratch'
    root.mkdir(mode=0o750)
    (root / 'keep').write_bytes(b'preserve')
    root_link = tmp_path / 'root-link'
    root_link.symlink_to(root, target_is_directory=True)
    original = module.tempfile.TemporaryFile
    stages = []

    def stage(**kwargs):
        assert Path(kwargs['dir']) == root_link
        f = original(**kwargs)
        assert os.fstat(f.fileno()).st_mode & 0o777 == 0o600
        stages.append(f)
        if failure == 'write':
            class FailedWriter:
                def __enter__(self): return self
                def __exit__(self, *args): f.close()
                def write(self, text): raise OSError('SECRET injected stage failure')
            return FailedWriter()
        return f

    monkeypatch.setattr(module.tempfile, 'TemporaryFile', stage)
    if failure in (None, 'copy'):
        original_copy = module.shutil.copyfileobj
        def copy(source, target):
            # Publication begins only after final diagnostics and every input record.
            assert source.read() == GOLDEN.read_text()
            source.seek(0)
            if failure == 'copy': raise OSError('SECRET injected copy failure')
            original_copy(source, target)
        monkeypatch.setattr(module.shutil, 'copyfileobj', copy)
    args = ['render-merge', str(path), '--output-temp-dir', str(root_link)]
    if failure == 'limit': args += ['--max-output-bytes', '1']
    assert module.main(args) == (0 if failure is None else 2)
    captured = capsys.readouterr()
    assert captured.out == (GOLDEN.read_text() if failure is None else '')
    assert 'SECRET' not in captured.err
    assert len(stages) == 1 and stages[0].closed
    assert [p.name for p in root.iterdir()] == ['keep']
    assert (root / 'keep').read_bytes() == b'preserve'
    assert root.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize('limit', ['file', 'line', 'records'])
def test_reader_limits_and_exact_boundaries(tmp_path, monkeypatch, capsys, limit):
    from agentwrench.export import cli as module
    import agentwrench.export.render_merge as render
    path = tmp_path / 'input'
    raw = FIXTURE.read_bytes()
    path.write_bytes(raw)
    constant, exact = {'file':('MAX_FILE_BYTES',len(raw)),
                       'line':('MAX_LINE_BYTES', max(map(len,raw.splitlines(keepends=True)))),
                       'records':('MAX_RECORDS',4)}[limit]
    # LF framing: Unicode separators are not physical line boundaries.
    if limit == 'line': exact = max(len(part) + 1 for part in raw.split(b'\n')[:-1])
    monkeypatch.setattr(render, constant, exact)
    assert module.main(['render-merge', str(path)]) == 0
    assert capsys.readouterr().out == GOLDEN.read_text()
    monkeypatch.setattr(render, constant, exact - 1)
    for output in (None, tmp_path / 'out'):
        assert module.main(['render-merge', str(path), *(['-o', str(output)] if output else [])]) == 2
        captured = capsys.readouterr()
        assert captured.out == '' and f'{limit[:-1] if limit == "records" else limit}' in captured.err
        if output: assert not output.exists()


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'empty'])
def test_nonregular_and_empty_input(tmp_path, kind):
    import os
    path = tmp_path / 'input'
    if kind == 'fifo': os.mkfifo(path)
    elif kind == 'directory': path.mkdir()
    else: path.touch()
    result = cli(path)
    assert result.returncode == 2 and not result.stdout
    assert b'Traceback' not in result.stderr


def test_file_write_error_cleans_new_output(tmp_path, monkeypatch, capsys):
    from agentwrench.export import cli as module
    output = tmp_path / 'out'
    def fail(path, stream, **kwargs):
        stream.write('validated but unpublished new file prefix')
        raise OSError('SECRET injected write failure')
    monkeypatch.setattr(module, 'render_merge', fail)
    assert module.main(['render-merge', str(FIXTURE), '-o', str(output)]) == 2
    captured = capsys.readouterr()
    assert not output.exists() and not captured.out and 'SECRET' not in captured.err


def test_source_labels_escape_untrusted_session_names(tmp_path):
    values = rows()
    name = 'A\n```\n<script>\x1b'
    values[0]['sources'][0]['trajectory']['session_id'] = name
    for event in (values[1], values[3]): event['provenance']['session_id'] = name
    result = cli(write(tmp_path / 'input', values))
    assert result.returncode == 0, result.stderr
    assert b'<script>' not in result.stdout and b'\x1b' not in result.stdout
    assert result.stdout.count(b'&lt;script&gt;') == 4  # catalogue, two labels, original user message


def test_raw_adapter_is_never_called(monkeypatch, capsys):
    from agentwrench.export import cli as module
    def forbidden(*args, **kwargs): raise AssertionError('raw input path used')
    monkeypatch.setattr(module, 'load_session', forbidden)
    monkeypatch.setattr(module, 'prepare', forbidden)
    assert module.main(['render-merge', str(FIXTURE)]) == 0
    assert capsys.readouterr().out == GOLDEN.read_text()
