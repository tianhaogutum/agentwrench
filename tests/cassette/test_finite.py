"""Direct JSON message numbers must be finite before copying or redaction."""
import copy
import json
import math

import pytest

from .conftest import cassette, request, response
from agentwrench.cassette.format import (CassetteError, REDACTED, canonical, parse_line, redact,
                                 validate_cassette, validate_message)
from agentwrench.cassette.record import Recorder
from agentwrench.cassette.replay import Replayer

DIAGNOSTIC = 'non-finite JSON numbers are not supported'
BAD = [pytest.param(float('nan'), id='nan'), pytest.param(float('inf'), id='infinity'),
       pytest.param(-float('inf'), id='negative-infinity')]
FIELDS = ['params', 'result', 'error.data', 'extension']


def message(value, field='params', rpc_id=7):
    if field == 'params':
        return request('work', {'number': value}, id=rpc_id)
    if field == 'result':
        return response(rpc_id, value)
    if field == 'error.data':
        return {'jsonrpc': '2.0', 'id': rpc_id,
                'error': {'code': -32000, 'message': 'synthetic', 'data': value}}
    msg = request('work', id=rpc_id)
    msg['extension'] = value
    return msg


def pointers(field, mode):
    # Unknown fields cannot be redacted; a valid unrelated pointer stays a no-op.
    root = {'params': '/params/number', 'result': '/result',
            'error.data': '/error/data', 'extension': '/params/number'}[field]
    return [] if mode == 'none' else [root + '/absent' if mode == 'absent' else root]


def stored(msg, paths):
    return cassette(*([('client', request('work', id=7)), ('server', msg)] if 'method' not in msg else
                     [('client', msg), ('server', response(7))]), paths=paths)


def assert_rejected(call, *, stored=False):
    with pytest.raises(CassetteError) as caught:
        call()
    assert str(caught.value) == DIAGNOSTIC
    assert caught.value.code == ('invalid_schema' if stored else 'nonfinite_number')
    if stored:
        assert caught.value.location in [
            {'path': '/events/0/message', 'frame_index': 0},
            {'path': '/events/1/message', 'frame_index': 1}]
    else:
        assert caught.value.location == {'path': ''}


@pytest.mark.parametrize('value', BAD)
@pytest.mark.parametrize('mode', ['none', 'absent', 'whole'])
def test_confirmed_direct_recorder_regression(value, mode):
    assert_rejected(lambda: Recorder(pointers('params', mode)).append('client', message(value)))


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', BAD)
@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('mode', ['none', 'absent', 'whole'])
def test_shared_admission_before_redaction(field, value, nested, mode):
    msg = message({'items': [None, {'number': value}]} if nested else value, field)
    before = json.dumps(msg)
    assert_rejected(lambda: validate_message(msg))
    assert_rejected(lambda: redact(msg, pointers(field, mode)))
    assert json.dumps(msg) == before


@pytest.mark.parametrize('direction', ['client', 'server'])
@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', BAD)
@pytest.mark.parametrize('mode', ['none', 'absent', 'whole'])
def test_recorder_rejection_is_transactional(direction, field, value, mode):
    recorder = Recorder(pointers(field, mode))
    other = 'server' if direction == 'client' else 'client'
    recorder.append('client', request('completed', id='prefix'))
    recorder.append('server', response('prefix'))
    requester = direction if field in ('params', 'extension') else other
    for rpc_id in (7, '7'):
        recorder.append(requester, request('pending', id=rpc_id))
    before = copy.deepcopy((recorder.events, recorder.pending, recorder.size, recorder.metadata_size))
    msg = message(value, field, rpc_id=8 if field in ('params', 'extension') else 7)
    assert_rejected(lambda: recorder.append(direction, msg))
    assert (recorder.events, recorder.pending, recorder.size, recorder.metadata_size) == before
    # Rejected responses leave both typed IDs pending; accepted continuations work.
    for rpc_id in ('7', 7):
        recorder.append(other if requester == direction else direction, response(rpc_id))
    assert validate_cassette(recorder.document(0, []))


@pytest.mark.parametrize('match', ['params', 'sequence'])
@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', BAD)
@pytest.mark.parametrize('mode', ['none', 'absent', 'whole'])
def test_stored_and_live_replay_rejection(field, value, mode, match):
    paths = pointers(field, mode)
    bad = message(value, field)
    data = stored(bad, paths)
    assert_rejected(lambda: validate_cassette(data), stored=True)
    assert_rejected(lambda: Replayer(data, match=match), stored=True)
    warnings = []
    player = Replayer(stored(message(1.5), paths), strict=True, match=match, warn=warnings.append)
    assert_rejected(lambda: player.handle(bad))
    assert player.unused_count == 1 and player.unmatched_count == 0
    assert player.unmatched == [] and warnings == [] and not player.interactions[0].used
    assert sum(map(len, player.queues.values())) == 1
    assert player.handle(message(1.5, rpc_id='live')) == [response('live')]
    assert player.finish() == 0


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', [1e308, -1e308, 1.5, 5e-324, 0.0, -0.0])
def test_finite_values_stay_whole_and_signed_zero(field, value):
    msg = message(value, field)
    for output in (validate_message(msg), redact(msg, []), parse_line((canonical(msg) + '\n').encode())):
        assert canonical(output) == canonical(msg)
    recorder = Recorder([])
    frames = stored(msg, [])['events']
    for event in frames:
        recorder.append(event['direction'], event['message'])
    assert canonical(recorder.events[-1 if field in ('result', 'error.data') else 0]['message']) == canonical(msg)
    for mode in ('params', 'sequence'):
        player = Replayer(recorder.document(0, []), strict=True, match=mode)
        output = player.handle(message(value, field, rpc_id='live') if 'method' in msg else request('work', id='live'))
        expected = copy.deepcopy(frames[1]['message'])
        expected['id'] = 'live'
        assert canonical(output) == canonical([expected])
        assert player.finish() == 0
    path = pointers(field, 'whole')
    out = redact(msg, path)
    if field != 'extension':
        assert REDACTED in canonical(out)
    else:
        assert canonical(out) == canonical(msg)
    if value == 0:
        assert math.copysign(1, json.loads(canonical(value))) == math.copysign(1, value)


def test_nonfinite_rejected_before_copy_or_serialization(monkeypatch):
    import agentwrench.cassette.format as fmt
    msg = message({'items': [float('nan')]})
    recorder = Recorder(['/params/number'])
    player = Replayer(stored(message(1.5), []))
    data = stored(msg, ['/params/number'])
    def forbidden(*args, **kwargs):
        pytest.fail('unsupported float reached copy or serialization')
    monkeypatch.setattr(fmt.copy, 'deepcopy', forbidden)
    monkeypatch.setattr(fmt.json, 'dumps', forbidden)
    for call in (lambda: validate_message(msg), lambda: redact(msg, ['/params/number']),
                 lambda: recorder.append('client', msg), lambda: player.handle(msg)):
        assert_rejected(call)
    assert_rejected(lambda: Replayer(data), stored=True)


@pytest.mark.parametrize('token', ['NaN', 'Infinity', '-Infinity', '1e999', '-1e999'])
def test_wire_nonfinite_stays_strict(token):
    raw = (json.dumps(message('NUMBER')).replace('"NUMBER"', token) + '\n').encode()
    with pytest.raises(CassetteError) as caught:
        parse_line(raw)
    assert caught.value.code == 'invalid_json'
    assert str(caught.value) == 'invalid JSON (UTF-8, finite numbers and unique keys required)'
