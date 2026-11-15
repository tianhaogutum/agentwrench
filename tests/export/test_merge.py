"""Synthetic integration contract for timestamp-ordered normalized exports."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]  # repository root


def header(session):
    return {'type': 'trajectory', 'schema_version': 'trajport/v1', 'session_id': session,
            'agent': {'name': 'synthetic', 'version': '1'}, 'extra': {'warnings': {}}}


def step(number, minute, message=None, **fields):
    return {'type': 'step', 'step_id': number, 'source': 'user',
            'timestamp': f'2026-01-01T10:{minute:02d}:00Z',
            'message': message or f'event {number}', 'extra': {}, **fields}


def write(path, session, rows, ending='\n'):
    path.write_bytes((ending.join(json.dumps(row, ensure_ascii=False)
                                 for row in [header(session), *rows]) + ending).encode())
    return path


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'agentwrench', 'export', 'merge', *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, timeout=15)


def test_merge_order_ties_and_lossless_provenance(tmp_path):
    a_rows = [step(1, 0, 'A1\u2028literal'), step(2, 2, 'A2'), step(3, 2, 'A3')]
    b_rows = [step(1, 1, 'B1'), step(2, 2, 'B2', timestamp='2026-01-01T11:02:00+01:00')]
    a = write(tmp_path / 'a.jsonl', 'A', a_rows)
    b = write(tmp_path / 'b.jsonl', 'B', b_rows, ending='\r\n')
    result = cli(a, b)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.split('\n') if line]
    assert rows[0] == {'type': 'merge', 'schema_version': 'trajport/merge-v1',
                       'sources': [{'input': 1, 'trajectory': header('A')},
                                   {'input': 2, 'trajectory': header('B')}]}
    expected = [(1, 'A', a_rows[0]), (2, 'B', b_rows[0]), (1, 'A', a_rows[1]),
                (1, 'A', a_rows[2]), (2, 'B', b_rows[1])]
    for event, (source, session, original) in zip(rows[1:], expected, strict=True):
        assert event == {'type': 'event', 'step': original, 'provenance': {
            'input': source, 'line': original['step_id'] + 1,
            'session_id': session, 'step_id': original['step_id']}}
    assert cli(a, b).stdout == result.stdout
    assert [json.loads(line)['step']['message'] for line in cli(b, a).stdout.split('\n')[1:-1]][-3:] == ['B2', 'A2', 'A3']
    assert json.loads(result.stderr.removeprefix('agentwrench export: ')) == {'merged_events': 5, 'sources': 2}


@pytest.mark.parametrize('same_payload', [True, False])
def test_duplicate_identity_fails_with_both_locations_and_no_output(tmp_path, same_payload):
    a = write(tmp_path / 'private-a.jsonl', 'SECRET-session', [step(1, 0), step(2, 2)])
    b = write(tmp_path / 'private-b.jsonl', 'SECRET-session',
              [step(2, 2, None if same_payload else 'SECRET-content')])
    for output in (None, tmp_path / 'out.jsonl'):
        result = cli(a, b, *(['-o', output] if output else []))
        assert result.returncode == 2
        assert not result.stdout
        assert 'duplicate event identity' in result.stderr
        assert 'input 1 line 3' in result.stderr and 'input 2 line 2' in result.stderr
        assert 'SECRET' not in result.stderr and 'private-' not in result.stderr
        if output:
            assert not output.exists()


@pytest.mark.parametrize('bad_timestamp', [None, 'yesterday', '2026-01-01',
    '2026-01-01T10:00:00', '2026-01-01T10:00:00.1234567Z', '2026-02-30T10:00:00Z',
    '2026-01-01T10:00:00+24:00', 123, '0001-01-01T00:00:00+01:00'])
def test_invalid_timestamps_fail_without_output(tmp_path, bad_timestamp):
    a = write(tmp_path / 'a', 'A', [step(1, 0), step(2, 2, timestamp=bad_timestamp)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    result = cli(a, b)
    assert result.returncode == 2 and not result.stdout
    assert 'input 1 line 3: timestamp requires RFC3339' in result.stderr


def test_out_of_order_input_is_rejected_late(tmp_path):
    a = write(tmp_path / 'a', 'A', [step(1, 2), step(2, 1)])
    b = write(tmp_path / 'b', 'B', [step(1, 0)])
    result = cli(a, b, '-o', tmp_path / 'out')
    assert result.returncode == 2 and not result.stdout
    assert 'input 1 line 3: timestamps are not nondecreasing' in result.stderr
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('tail', ['{broken}\n', '{broken}', '[1,2]\n',
                                  '{"x":1,"x":2}\n', '{"x":NaN}\n', '\xff\n'])
def test_malformed_late_record_fails_including_unterminated_tail(tmp_path, tail):
    a = write(tmp_path / 'a', 'A', [step(1, 0)])
    with a.open('ab') as stream:
        stream.write(tail.encode('latin1'))
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    result = cli(a, b)
    assert result.returncode == 2 and not result.stdout
    assert 'input 1 line 3:' in result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('change', ['schema', 'session', 'agent', 'empty', 'bool_id', 'source', 'message', 'record'])
def test_invalid_normalized_structure(tmp_path, change):
    a = write(tmp_path / 'a', 'A', [step(1, 0)])
    rows = [header('B'), step(1, 1)]
    if change == 'schema':
        rows[0]['schema_version'] = 'future'
    elif change == 'session':
        rows[0]['session_id'] = 'unknown'
    elif change == 'agent':
        rows[0]['agent'] = []
    elif change == 'empty':
        rows.pop()
    elif change == 'bool_id':
        rows[1]['step_id'] = True
    elif change == 'source':
        rows[1]['source'] = []
    elif change == 'message':
        rows[1]['message'] = {}
    else:
        rows[1]['type'] = 'trajectory'
    b = tmp_path / 'b'
    b.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = cli(a, b)
    assert result.returncode == 2 and not result.stdout
    assert 'merge input 2' in result.stderr


def test_same_session_disjoint_steps_and_tool_scoping_are_preserved(tmp_path):
    # Tool IDs in separate sessions are deliberately identical. No call/result
    # matching or ID renumbering may cross a source session boundary.
    tool = step(7, 0, source='agent', tool_calls=[{'id': 'call', 'name': 'read', 'arguments': {'path': 'x'}}],
                observation={'results': [{'source_call_id': 'call', 'content': 'inert', 'extra': {}}]},
                metrics={'prompt_tokens': 2})
    a = write(tmp_path / 'a', 'A', [tool])
    b = write(tmp_path / 'b', 'B', [tool])
    c = write(tmp_path / 'c', 'A', [step(8, 1)])
    result = cli(a, b, c, '-o', tmp_path / 'out')
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    events = [json.loads(line) for line in (tmp_path / 'out').read_text().split('\n')[1:-1]]
    assert events[0]['step'] == events[1]['step'] == tool
    assert events[2]['provenance']['session_id'] == 'A'
    assert (tmp_path / 'out').stat().st_mode & 0o777 == 0o600


def test_duplicate_within_one_file_reports_physical_lines(tmp_path):
    a = write(tmp_path / 'a', 'A', [step(1, 0)])
    with a.open('a') as stream:
        stream.write('\n' + json.dumps(step(1, 2)) + '\n')
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    result = cli(a, b)
    assert result.returncode == 2 and not result.stdout
    assert 'input 1 line 2 conflicts with input 1 line 4' in result.stderr


def test_output_existing_file_and_symlink_preserved(tmp_path):
    a = write(tmp_path / 'a', 'A', [step(1, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    existing = tmp_path / 'existing'
    existing.write_text('keep')
    link = tmp_path / 'link'
    link.symlink_to(existing)
    for out in (existing, link, a):
        before = out.read_bytes()
        result = cli(a, b, '-o', out)
        assert result.returncode == 2 and not result.stdout
        assert out.read_bytes() == before
    assert link.is_symlink()


def test_reader_is_lazy_and_closes_early(tmp_path, monkeypatch):
    from agentwrench.export import merge
    a = write(tmp_path / 'a', 'A', [step(i, i % 60, timestamp='2026-01-01T10:00:00Z') for i in range(1, 1001)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    reads = []
    original = merge._Source.record

    def tracked(source):
        reads.append(source.number)
        return original(source)

    monkeypatch.setattr(merge._Source, 'record', tracked)
    # Track the disk index lifecycle as well as lookahead. No timing benchmark.
    import tempfile
    original_temp = tempfile.TemporaryDirectory
    directories = []

    def temporary(**kwargs):
        directory = original_temp(dir=tmp_path, **kwargs)
        directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(merge.tempfile, 'TemporaryDirectory', temporary)
    rows = merge.merge_jsonl([a, b])
    assert next(rows)['type'] == 'merge'
    assert reads == [1, 2, 1, 2]
    assert next(rows)['provenance']['step_id'] == 1
    assert reads == [1, 2, 1, 2]
    assert next(rows)['provenance']['step_id'] == 2
    assert reads == [1, 2, 1, 2, 1]
    assert (directories[0] / 'identity.sqlite').is_file()
    rows.close()
    assert not directories[0].exists()


@pytest.mark.parametrize('limit', ['MAX_FILE_BYTES', 'MAX_LINE_BYTES', 'MAX_RECORDS'])
def test_streaming_reader_limits(tmp_path, monkeypatch, limit):
    from agentwrench.export import merge
    from agentwrench.export.model import InputError
    a = write(tmp_path / 'a', 'A', [step(1, 0)])
    b = write(tmp_path / 'b', 'B', [step(1, 1)])
    monkeypatch.setattr(merge, limit, 1)
    with pytest.raises(InputError, match='limit'):
        list(merge.merge_jsonl([a, b]))


def test_source_count_and_regular_file_boundaries(tmp_path):
    from agentwrench.export.merge import merge_jsonl
    from agentwrench.export.model import InputError
    a = write(tmp_path / 'a', 'A', [step(1, 0)])
    for inputs in ([a], [a] * 65):
        with pytest.raises(InputError, match='2 to 64'):
            list(merge_jsonl(inputs))
    fifo = tmp_path / 'fifo'
    import os
    os.mkfifo(fifo)
    result = cli(a, fifo)
    assert result.returncode == 2 and not result.stdout
    assert 'regular file' in result.stderr


def test_fractional_instants_bom_and_valid_unterminated_record(tmp_path):
    a = write(tmp_path / 'a', 'A', [step(1, 0, timestamp='2026-01-01T10:00:00.000002Z')])
    a.write_bytes(b'\xef\xbb\xbf' + a.read_bytes().rstrip(b'\n'))
    b = write(tmp_path / 'b', 'B', [step(1, 0, timestamp='2026-01-01T11:00:00.000001+01:00')])
    result = cli(a, b)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.split('\n')[1:-1]]
    assert [event['provenance']['input'] for event in events] == [2, 1]
    assert events[1]['step']['timestamp'] == '2026-01-01T10:00:00.000002Z'
