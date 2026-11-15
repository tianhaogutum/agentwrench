"""Message depth is bounded before copying, even if redaction removes a subtree."""
import copy
import json
import sys

import pytest

from .conftest import cassette, request, response
from agentwrench.cassette.format import (CassetteError, MAX_MESSAGE_DEPTH, REDACTED, load_cassette, loads,
                                 parse_line, redact, validate_cassette, validate_message)
from agentwrench.cassette.record import Recorder
from agentwrench.cassette.replay import Replayer

LIMIT = 64
DIAGNOSTIC = 'message container nesting exceeds 64 levels'


def nested(depth, kind):
    value = '秘密 [ { \\"'
    for i in range(depth):
        value = {'child': value} if kind == 'objects' or (kind == 'mixed' and i % 2) else [value]
    return value


def message(depth, kind='arrays', field='params', rpc_id=7):
    # params/result wrapper is depth 2; error.data wrapper is depth 3.
    value = {'items': nested(depth - (3 if field == 'error' else 2), kind)}
    if field == 'params':
        return request('work', value, id=rpc_id)
    if field == 'result':
        return response(rpc_id, value)
    return {'jsonrpc': '2.0', 'id': rpc_id,
            'error': {'code': -32000, 'message': 'synthetic', 'data': value}}


def wire(msg):
    return (json.dumps(msg, ensure_ascii=False) + '\n').encode()


@pytest.mark.parametrize('paths', [[], ['/params/absent'], ['/params/items']])
def test_confirmed_depth600_redaction_rejected(paths):
    msg = message(602)
    # Demonstrate strict JSON decoding accepts the confirmed 600-array input.
    assert loads(wire(msg)) == msg
    with pytest.raises(CassetteError, match=DIAGNOSTIC):
        redact(msg, paths)


@pytest.mark.parametrize('kind', ['arrays', 'objects', 'mixed'])
@pytest.mark.parametrize('field', ['params', 'result', 'error'])
@pytest.mark.parametrize('depth', [20, LIMIT, LIMIT + 1])
def test_direct_and_wire_depth_boundary(kind, field, depth):
    msg = message(depth, kind, field)
    original = copy.deepcopy(msg)
    root = '/error/data' if field == 'error' else '/' + field
    assert MAX_MESSAGE_DEPTH == LIMIT
    old_limit = sys.getrecursionlimit()
    if depth > LIMIT:
        for call in (lambda: validate_message(msg), lambda: parse_line(wire(msg)),
                     lambda: redact(msg, []), lambda: redact(msg, [root + '/missing']),
                     lambda: redact(msg, [root + '/items']),
                     lambda: redact(msg, [root if field != 'params' else root + '/items'])):
            with pytest.raises(CassetteError, match=DIAGNOSTIC):
                call()
    else:
        assert validate_message(msg) is msg
        assert parse_line(wire(msg)) == msg
        assert redact(msg, []) == msg
        assert redact(msg, [root + '/missing']) == msg
        result = redact(msg, [root + '/items'])
        payload = result['error']['data'] if field == 'error' else result[field]
        assert payload == {'items': REDACTED}
        if field == 'result':
            assert redact(msg, ['/result'])['result'] == REDACTED
        if field == 'error':
            assert redact(msg, ['/error/data'])['error']['data'] == REDACTED
    assert msg == original
    assert sys.getrecursionlimit() == old_limit


@pytest.mark.parametrize('empty', [[], {}])
def test_empty_container_counts_at_boundary(empty):
    value = empty
    for _ in range(LIMIT - 1):
        value = {'child': value}
    assert redact(value, []) == value
    with pytest.raises(CassetteError, match=DIAGNOSTIC):
        redact({'child': value}, [])


def test_sibling_width_does_not_accumulate_depth_and_cycles_terminate():
    assert redact([{} for _ in range(1000)], []) == [{} for _ in range(1000)]
    cyclic = {}
    cyclic['self'] = cyclic
    with pytest.raises(CassetteError, match=DIAGNOSTIC):
        redact(cyclic, [])


@pytest.mark.parametrize('direction', ['client', 'server'])
@pytest.mark.parametrize('field', ['params', 'result', 'error'])
def test_recorder_rejection_preserves_typed_pending_prefix(direction, field):
    recorder = Recorder(['/params/items', '/result', '/error/data'])
    other = 'server' if direction == 'client' else 'client'
    request_direction = direction if field == 'params' else other
    for rpc_id in (7, '7'):
        recorder.append(request_direction, request('prefix', id=rpc_id))
    prefix = copy.deepcopy(recorder.events)
    pending = copy.deepcopy(recorder.pending)
    size = recorder.size
    with pytest.raises(CassetteError, match=DIAGNOSTIC):
        recorder.append(direction, message(LIMIT + 1, field=field, rpc_id=8 if field == 'params' else 7))
    assert recorder.events == prefix and recorder.pending == pending and recorder.size == size
    for rpc_id in ('7', 7):
        recorder.append(other if field == 'params' else direction, response(rpc_id))
    assert validate_cassette(recorder.document(0, []))['incomplete'] is False


@pytest.mark.parametrize('field', ['params', 'result', 'error'])
@pytest.mark.parametrize('kind', ['arrays', 'objects', 'mixed'])
def test_stored_message_depth_ignores_cassette_wrappers(tmp_path, field, kind):
    for depth in (LIMIT, LIMIT + 1, 602):
        msg = message(depth, kind, field)
        messages = [('client', msg), ('server', response(7))] if field == 'params' else [
            ('client', request(id=7)), ('server', msg)]
        data = cassette(*messages, paths=['/params/items', '/result', '/error/data'])
        path = tmp_path / 'stored.json'
        path.write_bytes(json.dumps(data, ensure_ascii=False).encode())
        if depth > LIMIT:
            for call in (lambda: validate_cassette(data), lambda: load_cassette(path), lambda: Replayer(data)):
                with pytest.raises(CassetteError, match=DIAGNOSTIC):
                    call()
        else:
            assert load_cassette(path) == data
            Replayer(data)


@pytest.mark.parametrize('match', ['params', 'sequence'])
@pytest.mark.parametrize('paths', [[], ['/params/missing'], ['/params/items']])
def test_live_rejection_does_not_consume_and_exact_limit_matches(match, paths):
    data = cassette(('client', message(LIMIT)), ('server', message(LIMIT, field='result')), paths=paths)
    player = Replayer(data, strict=True, match=match)
    for depth in (LIMIT + 1, 602):
        with pytest.raises(CassetteError, match=DIAGNOSTIC):
            player.handle(message(depth, rpc_id='live'))
        assert player.unused_count == 1 and player.unmatched_count == 0
    assert player.handle(message(LIMIT, rpc_id='live')) == [message(LIMIT, field='result', rpc_id='live')]
    assert player.finish() == 0


def test_parser_recursion_failure_uses_explicit_nesting_diagnostic():
    # Python 3.14's C JSON parser accepts 2000 levels; 100000 overflows on every supported version.
    depth = 100_000
    raw = b'{"jsonrpc":"2.0","method":"note","params":{"items":' + b'[' * depth + b'0' + b']' * depth + b'}}\n'
    with pytest.raises(CassetteError, match=DIAGNOSTIC) as caught:
        parse_line(raw)
    assert caught.value.code == 'invalid_json'


@pytest.mark.parametrize('skip', [False, True])
def test_server_parser_recursion_is_not_skipped(cli, tmp_path, skip):
    raw = b'{"jsonrpc":"2.0","method":"note","params":{"items":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}}\n'
    source, path = tmp_path / 'wire', tmp_path / 'record.json'
    source.write_bytes(raw)
    options = ['--skip-non-json'] if skip else []
    result = cli('record', '--cassette', path, *options, '--', sys.executable, '-c',
                 'import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); sys.stdout.flush(); sys.stdin.read()', source)
    assert result.returncode == 1 and result.stdout == b''
    assert DIAGNOSTIC.encode() in result.stderr
    assert b'stdio I/O' not in result.stderr and b'skipped' not in result.stderr
    assert load_cassette(path)['events'] == []


@pytest.mark.parametrize('kind', ['arrays', 'objects', 'mixed'])
@pytest.mark.parametrize('field', ['params', 'result', 'error'])
@pytest.mark.parametrize('depth', [LIMIT, LIMIT + 1])
def test_record_live_boundary_and_completed_prefix(tmp_path, kind, field, depth):
    import subprocess
    from .test_record import _readline
    path, report, captured = tmp_path / 'record.json', tmp_path / 'report.json', tmp_path / 'forwarded'
    msg = message(depth, kind, field, rpc_id='7')
    target = tmp_path / 'target-wire'
    target.write_bytes(b'  ' + wire(msg).rstrip(b'\n') + b'\r\n')
    server = '''import json, pathlib, sys
with open(sys.argv[1], 'wb') as captured:
    for line in sys.stdin.buffer:
        captured.write(line); captured.flush()
        req = json.loads(line)
        out = pathlib.Path(sys.argv[2]).read_bytes() if req['method'] == 'work' and sys.argv[3] != 'params' else (
            json.dumps({'jsonrpc':'2.0', 'id':req['id'], 'result':{}}) + '\\n').encode()
        sys.stdout.buffer.write(out); sys.stdout.buffer.flush()
'''
    argv = [sys.executable, '-m', 'agentwrench', 'cassette', 'record', '--cassette', str(path), '--report', str(report),
            '--redact-path', '/params/items', '--redact-path', '/result/items', '--redact-path', '/error/data/items',
            '--', sys.executable, '-c', server, str(captured), str(target), field]
    prefix = wire(request('prefix', id=7))
    incoming = target.read_bytes() if field == 'params' else wire(request('work', id='7'))
    expected_wire = wire(response('7')) if field == 'params' else target.read_bytes()
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        proc.stdin.write(prefix); proc.stdin.flush()
        assert json.loads(_readline(proc.stdout)) == response(7)
        output, error = proc.communicate(incoming, timeout=10)
    rejected = depth > LIMIT
    assert proc.returncode == int(rejected), error
    assert output == (b'' if rejected else expected_wire)
    assert json.loads(report.read_bytes()) == {'exit_code': int(rejected), 'unmatched': 0, 'unused': 0}
    data = load_cassette(path)
    assert data['incomplete'] is rejected
    assert len(data['events']) == (2 if rejected and field == 'params' else 3 if rejected else 4)
    assert captured.read_bytes() == prefix + (b'' if rejected and field == 'params' else incoming)
    if rejected:
        assert DIAGNOSTIC.encode() in error and b'stdio I/O' not in error and b'Traceback' not in error
        assert ('unanswered requests remain' in data['metadata']['reasons']) is (field != 'params')
    else:
        assert error == b''
        stored = data['events'][2 if field == 'params' else 3]['message']
        payload = stored['error']['data'] if field == 'error' else stored[field]
        assert payload == {'items': REDACTED}
        assert type(data['events'][0]['message']['id']) is int
        assert type(data['events'][2]['message']['id']) is str


@pytest.mark.parametrize('match', ['params', 'sequence'])
@pytest.mark.parametrize('depth', [LIMIT, LIMIT + 1, 602])
@pytest.mark.parametrize('stored', [False, True])
def test_replay_cli_nesting_contract(cli, tmp_path, match, depth, stored):
    req = message(depth if stored else LIMIT)
    path, report = tmp_path / 'cassette.json', tmp_path / 'report.json'
    path.write_text(json.dumps(cassette(('client', req), ('server', response(7)), paths=['/params/items'])))
    live = message(depth, rpc_id='live')
    result = cli('replay', '--cassette', path, '--report', report, '--strict', '--match', match, raw=wire(live))
    rejected = depth > LIMIT
    code = 2 if rejected else 0
    assert result.returncode == code, result.stderr
    assert json.loads(report.read_bytes()) == {'exit_code': code, 'unmatched': 0, 'unused': int(rejected and not stored)}
    assert result.stdout == (b'' if rejected else b'{"jsonrpc":"2.0","id":"live","result":{}}\n')
    assert result.stderr == ((f'agentwrench cassette: {DIAGNOSTIC}\n').encode() if rejected else b'')


@pytest.mark.parametrize('mode', ['all', 'none'])
@pytest.mark.parametrize('depth', [LIMIT, LIMIT + 1])
def test_pytest_fixture_observes_nesting_status(pytester, mode, depth):
    note = message(LIMIT)
    del note['id']
    directory = pytester.path / 'cassettes'
    directory.mkdir()
    (directory / 'nest.json').write_text(json.dumps(cassette(('client', redact(note, ['/params/items'])), ('server', redact(note, ['/params/items'])), paths=['/params/items'])))
    incoming = message(depth)
    del incoming['id']
    (pytester.path / 'input').write_bytes(wire(incoming))
    pytester.makepyfile('''
import json, subprocess, sys
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette('nest', command=[sys.executable, '-c',
    'import sys\\nfor line in sys.stdin.buffer:\\n sys.stdout.buffer.write(line); sys.stdout.buffer.flush()'],
    redact_paths=['/params/items'])
def test_client(mcp_cassette):
    raw = Path('input').read_bytes()
    result = subprocess.run(mcp_cassette.argv, input=raw, capture_output=True, timeout=10)
    assert result.returncode == EXPECTED_CODE, result.stderr
    if EXPECTED_CODE:
        assert result.stdout == b''
        assert b'message container nesting exceeds 64 levels' in result.stderr
    elif mcp_cassette.recording:
        assert result.stdout == raw
    else:
        assert json.loads(result.stdout)['params']['items'] == '<REDACTED>'
'''.replace('EXPECTED_CODE', str((1 if mode == 'all' else 2) if depth > LIMIT else 0)))
    result = pytester.runpytest_subprocess('-q', '--tb=line', '-p', 'no:cacheprovider', '--mcp-record=' + mode)
    result.assert_outcomes(passed=1, errors=int(depth > LIMIT))
    if depth > LIMIT:
        result.stdout.fnmatch_lines(['*MCP cassette process failed*'])


@pytest.mark.parametrize('action', ['validate', 'inspect'])
def test_nesting_validation_location_and_frame_selection(cli, tmp_path, action):
    data = cassette(('client', message(LIMIT + 1)), ('server', response(7)))
    path = tmp_path / 'stored.json'
    path.write_text(json.dumps(data))
    args = ['--frame', '1'] if action == 'inspect' else []
    result = cli(action, path, *args)
    assert result.returncode == 1 and result.stderr == b''
    report = json.loads(result.stdout)
    assert report['diagnostics'] == [{'code': 'invalid_schema', 'message': DIAGNOSTIC,
        'location': {'path': '/events/0/message', 'frame_index': 0}}]
    if action == 'inspect':
        assert report['frame'] is None and report['peer'] is None
