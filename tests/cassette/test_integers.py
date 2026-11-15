"""Integer admission is exact and precedes conversion, copying and redaction."""
import copy
import json
import sys

import pytest

from .conftest import cassette, request, response
from agentwrench.cassette.format import (CassetteError, MAX_INTEGER_DIGITS, REDACTED, loads, load_cassette,
                                 parse_line, redact, validate_cassette, validate_message)
from agentwrench.cassette.record import Recorder
from agentwrench.cassette.replay import Replayer

LIMIT = 4096
DIAGNOSTIC = 'integer exceeds supported 4096 decimal digits'


def message(number, field='params', rpc_id=7):
    if field == 'params':
        return request('work', {'number': number}, id=rpc_id)
    if field == 'result':
        return response(rpc_id, {'number': number})
    if field == 'id':
        return request('work', id=number)
    return {'jsonrpc': '2.0', 'id': rpc_id,
            'error': {'code': -32000, 'message': 'synthetic', 'data': {'number': number}}}


def wire(number_text, field='params', rpc_id=7):
    # Avoid materializing/serializing an unsupported integer in the test harness.
    return (json.dumps(message('INTEGER_SLOT', field, rpc_id)).replace('"INTEGER_SLOT"', number_text) + '\n').encode()


@pytest.mark.parametrize('path', [[], ['/params/absent'], ['/params/number']])
def test_confirmed_5000_digit_direct_rejected(path):
    msg = message(10 ** 4999)
    with pytest.raises(CassetteError, match=DIAGNOSTIC):
        Recorder(path).append('client', msg)


def test_confirmed_5000_digit_wire_numeric_diagnostic():
    with pytest.raises(CassetteError, match=DIAGNOSTIC) as caught:
        parse_line(wire('9' * 5000))
    assert caught.value.code == 'integer_too_large'


@pytest.mark.parametrize('field', ['params', 'result', 'error', 'id'])
@pytest.mark.parametrize('negative', [False, True])
@pytest.mark.parametrize('digits', [1, 1000, LIMIT, LIMIT + 1, 5000])
def test_direct_wire_and_redaction_boundary(field, negative, digits):
    text = ('-' if negative else '') + '9' * digits
    number = (10 ** digits - 1) * (-1 if negative else 1)
    msg = message(number, field)
    original = copy.deepcopy(msg)
    root = '/error/data' if field == 'error' else '/params' if field == 'id' else '/' + field
    paths = [[], [root + '/absent'], [root + '/number']]
    old_limits = sys.get_int_max_str_digits(), sys.getrecursionlimit()
    calls = [lambda: validate_message(msg), lambda: parse_line(wire(text, field)),
             *(lambda p=p: redact(msg, p) for p in paths)]
    if digits > LIMIT:
        for call in calls:
            with pytest.raises(CassetteError, match=DIAGNOSTIC) as caught:
                call()
            assert caught.value.code == 'integer_too_large'
            assert len(str(caught.value)) < 100
    else:
        assert validate_message(msg) is msg
        assert parse_line(wire(text, field)) == msg
        assert redact(msg, []) == msg
        assert redact(msg, [root + '/absent']) == msg
        if field != 'id':
            out = redact(msg, [root + '/number'])
            payload = out['error']['data'] if field == 'error' else out[field]
            assert payload['number'] == REDACTED
    assert msg == original
    assert old_limits == (sys.get_int_max_str_digits(), sys.getrecursionlimit())


@pytest.mark.parametrize('text', ['0', '-0'])
def test_zero_is_one_digit(text):
    assert MAX_INTEGER_DIGITS == LIMIT
    assert parse_line(wire(text)) == message(0)
    assert redact(message(0), []) == message(0)


def test_noninteger_values_and_unknown_fields():
    assert loads('true') is True
    assert loads('1.5') == 1.5
    assert loads('"' + '9' * 5000 + '"') == '9' * 5000
    msg = request()
    msg['extension'] = [10 ** LIMIT]
    with pytest.raises(CassetteError, match=DIAGNOSTIC):
        validate_message(msg)


@pytest.mark.parametrize('direction', ['client', 'server'])
@pytest.mark.parametrize('field', ['params', 'result', 'error', 'id'])
def test_recorder_failure_preserves_prefix_pending_and_budget(direction, field):
    recorder = Recorder(['/params/number', '/result', '/error/data'])
    other = 'server' if direction == 'client' else 'client'
    request_direction = direction if field in ('params', 'id') else other
    for rpc_id in (7, '7'):
        recorder.append(request_direction, request('prefix', id=rpc_id))
    before = copy.deepcopy((recorder.events, recorder.pending, recorder.size))
    with pytest.raises(CassetteError, match=DIAGNOSTIC):
        recorder.append(direction, message(-(10 ** LIMIT), field, 8 if field == 'params' else 7))
    assert (recorder.events, recorder.pending, recorder.size) == before
    for rpc_id in ('7', 7):
        recorder.append(other if field in ('params', 'id') else direction, response(rpc_id))
    validate_cassette(recorder.document(0, []))


@pytest.mark.parametrize('field', ['params', 'result', 'error', 'id'])
def test_stored_integer_boundary(tmp_path, field):
    for digits in (LIMIT, LIMIT + 1, 5000):
        msg = message(10 ** (digits - 1), field)
        rpc_id = msg['id']
        frames = [('client', msg), ('server', response(rpc_id))] if field in ('params', 'id') else [
            ('client', request(id=rpc_id)), ('server', msg)]
        data = cassette(*frames)
        if digits > LIMIT:
            for call in (lambda: validate_cassette(data), lambda: Replayer(data)):
                with pytest.raises(CassetteError, match=DIAGNOSTIC):
                    call()
        else:
            path = tmp_path / 'stored.json'
            path.write_text(json.dumps(data))
            assert load_cassette(path) == data
            Replayer(data)


@pytest.mark.parametrize('match', ['params', 'sequence'])
@pytest.mark.parametrize('paths', [[], ['/params/absent'], ['/params/number']])
def test_live_rejection_preserves_interaction_and_typed_exact_ids(match, paths):
    number = -(10 ** LIMIT - 1)
    data = cassette(('client', message(number)), ('server', message(number, 'result')), paths=paths)
    player = Replayer(data, strict=True, match=match)
    for field in ('params', 'id'):
        with pytest.raises(CassetteError, match=DIAGNOSTIC):
            player.handle(message(10 ** 4999, field))
        assert player.unused_count == 1 and player.unmatched_count == 0
    assert player.handle(message(number, rpc_id=number)) == [message(number, 'result', rpc_id=number)]
    assert player.finish() == 0
    typed = Replayer(cassette(('client', message(0, rpc_id=number)), ('server', response(number))))
    assert typed.handle(message(0, rpc_id=str(number))) == [response(str(number))]


def test_token_length_checked_before_conversion(monkeypatch):
    import agentwrench.cassette.format as fmt
    def forbidden(text):
        pytest.fail('oversized integer reached decimal conversion')
    monkeypatch.setattr(fmt, 'int', forbidden, raising=False)
    for text in ('9' * (LIMIT + 1), '-' + '9' * 5000):
        with pytest.raises(CassetteError, match=DIAGNOSTIC):
            fmt._parse_integer(text)


@pytest.mark.parametrize('skip', [False, True])
def test_server_5000_digit_integer_is_never_skipped(cli, tmp_path, skip):
    source, path = tmp_path / 'wire', tmp_path / 'record.json'
    source.write_bytes(wire('9' * 5000))
    options = ['--skip-non-json'] if skip else []
    result = cli('record', '--cassette', path, *options, '--', sys.executable, '-c',
                 'import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); sys.stdout.flush(); sys.stdin.read()', source)
    assert result.returncode == 1 and result.stdout == b''
    assert DIAGNOSTIC.encode() in result.stderr
    assert b'stdio I/O' not in result.stderr and b'skipped' not in result.stderr
    assert load_cassette(path)['events'] == []


@pytest.mark.parametrize('field', ['params', 'result', 'error', 'id'])
@pytest.mark.parametrize('negative', [False, True])
@pytest.mark.parametrize('digits', [LIMIT, LIMIT + 1])
def test_record_boundary_preserves_completed_prefix_and_wire(tmp_path, field, negative, digits):
    import subprocess
    from .test_record import _readline
    number = ('-' if negative else '') + '9' * digits
    path, report, captured = tmp_path / 'record.json', tmp_path / 'report.json', tmp_path / 'forwarded'
    target = tmp_path / 'target-wire'
    target.write_bytes(b'  ' + wire(number, field, rpc_id='7').rstrip(b'\n') + b'\r\n')
    server = '''import json, pathlib, sys
with open(sys.argv[1], 'wb') as captured:
    for line in sys.stdin.buffer:
        captured.write(line); captured.flush()
        req = json.loads(line)
        out = pathlib.Path(sys.argv[2]).read_bytes() if req['method'] == 'work' and sys.argv[3] in ('result','error') else (
            json.dumps({'jsonrpc':'2.0', 'id':req['id'], 'result':{}}) + '\\n').encode()
        sys.stdout.buffer.write(out); sys.stdout.buffer.flush()
'''
    argv = [sys.executable, '-m', 'agentwrench', 'cassette', 'record', '--cassette', str(path), '--report', str(report),
            '--redact-path', '/params/number', '--redact-path', '/result', '--redact-path', '/error/data',
            '--', sys.executable, '-c', server, str(captured), str(target), field]
    prefix = (json.dumps(request('prefix', id=7)) + '\n').encode()
    incoming = target.read_bytes() if field in ('params', 'id') else (json.dumps(request('work', id='7')) + '\n').encode()
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        proc.stdin.write(prefix); proc.stdin.flush()
        assert json.loads(_readline(proc.stdout)) == response(7)
        output, error = proc.communicate(incoming, timeout=10)
    rejected = digits > LIMIT
    assert proc.returncode == int(rejected), error
    assert json.loads(report.read_bytes()) == {'exit_code': int(rejected), 'unmatched': 0, 'unused': 0}
    data = load_cassette(path)
    assert data['incomplete'] is rejected
    assert len(data['events']) == (2 if rejected and field in ('params', 'id') else 3 if rejected else 4)
    assert captured.read_bytes() == prefix + (b'' if rejected and field in ('params', 'id') else incoming)
    if rejected:
        assert output == b''
        assert DIAGNOSTIC.encode() in error and b'stdio I/O' not in error and b'Traceback' not in error
        assert ('unanswered requests remain' in data['metadata']['reasons']) is (field in ('result', 'error'))
    else:
        expected = target.read_bytes() if field in ('result', 'error') else (
            json.dumps(response(int(number) if field == 'id' else '7')) + '\n').encode()
        assert output == expected and error == b''
        stored = data['events'][2 if field in ('params', 'id') else 3]['message']
        if field == 'params': assert stored['params']['number'] == REDACTED
        elif field == 'result': assert stored['result'] == REDACTED
        elif field == 'error': assert stored['error']['data'] == REDACTED
        else: assert stored['id'] == int(number) and type(stored['id']) is int


@pytest.mark.parametrize('match', ['params', 'sequence'])
@pytest.mark.parametrize('digits', [1000, LIMIT, LIMIT + 1, 5000])
@pytest.mark.parametrize('stored', [False, True])
def test_replay_cli_integer_contract(cli, tmp_path, match, digits, stored):
    req = message('INTEGER_SLOT')
    data = cassette(('client', req), ('server', message('INTEGER_SLOT', 'result')), paths=['/params/number'])
    path, report = tmp_path / 'cassette.json', tmp_path / 'report.json'
    path.write_text(json.dumps(data).replace('"INTEGER_SLOT"', '-' + '9' * (digits if stored else LIMIT)))
    result = cli('replay', '--cassette', path, '--report', report, '--strict', '--match', match,
                 raw=wire('-' + '9' * digits, rpc_id='live'))
    rejected = digits > LIMIT
    code = 2 if rejected else 0
    assert result.returncode == code, result.stderr
    assert json.loads(report.read_bytes()) == {'exit_code': code, 'unmatched': 0, 'unused': int(rejected and not stored)}
    if rejected:
        assert result.stdout == b''
        assert result.stderr == (f'agentwrench cassette: {DIAGNOSTIC}\n').encode()
    else:
        expected = message(-(10 ** (digits if stored else LIMIT) - 1), 'result', rpc_id='live')
        assert parse_line(result.stdout) == expected and result.stderr == b''


@pytest.mark.parametrize('action', ['validate', 'inspect'])
def test_integer_validation_location_and_null_frame(cli, tmp_path, action):
    data = cassette(('client', message('INTEGER_SLOT')), ('server', response(7)))
    path = tmp_path / 'stored.json'
    path.write_text(json.dumps(data).replace('"INTEGER_SLOT"', '9' * (LIMIT + 1)))
    result = cli(action, path, *(['--frame', '1'] if action == 'inspect' else []))
    assert result.returncode == 2 and result.stderr == b''
    report = json.loads(result.stdout)
    assert report['diagnostics'] == [{'code': 'integer_too_large', 'message': DIAGNOSTIC, 'location': {'path': ''}}]
    if action == 'inspect': assert report['frame'] is None and report['peer'] is None


@pytest.mark.parametrize('mode', ['all', 'none'])
@pytest.mark.parametrize('digits', [1000, 5000])
def test_pytest_fixture_integer_exit_reports(pytester, mode, digits):
    note = message(REDACTED)
    del note['id']
    directory = pytester.path / 'cassettes'
    directory.mkdir()
    (directory / 'integer.json').write_text(json.dumps(cassette(('client', note), ('server', note), paths=['/params/number'])))
    incoming = wire('9' * digits).replace(b'"id": 7, ', b'')
    (pytester.path / 'input').write_bytes(incoming)
    pytester.makepyfile('''
import json, subprocess, sys
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette('integer', command=[sys.executable, '-c',
    'import sys\\nfor line in sys.stdin.buffer:\\n sys.stdout.buffer.write(line); sys.stdout.buffer.flush()'],
    redact_paths=['/params/number'])
def test_client(mcp_cassette):
    raw = Path('input').read_bytes()
    result = subprocess.run(mcp_cassette.argv, input=raw, capture_output=True, timeout=10)
    assert result.returncode == EXPECTED_CODE, result.stderr
    if EXPECTED_CODE:
        assert result.stdout == b''
        assert b'integer exceeds supported 4096 decimal digits' in result.stderr
    elif mcp_cassette.recording:
        assert result.stdout == raw
    else:
        assert json.loads(result.stdout)['params']['number'] == '<REDACTED>'
'''.replace('EXPECTED_CODE', str((1 if mode == 'all' else 2) if digits > LIMIT else 0)))
    result = pytester.runpytest_subprocess('-q', '--tb=line', '-p', 'no:cacheprovider', '--mcp-record=' + mode)
    result.assert_outcomes(passed=1, errors=int(digits > LIMIT))
    if digits > LIMIT: result.stdout.fnmatch_lines(['*MCP cassette process failed*'])
