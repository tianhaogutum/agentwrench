"""JSON Pointer array references stay bounded and preserve object-key semantics."""
import copy

import pytest

from agentwrench.cassette.format import REDACTED, pointer_tokens, redact
from agentwrench.cassette.record import Recorder
from agentwrench.cassette.replay import Replayer
from .conftest import request, response


ABSENT = [
    pytest.param('9' * 5000, id='giant-nonzero'),
    pytest.param('0' * 5000, id='giant-zero'),
    pytest.param('0' * 5000 + '1', id='giant-zero-prefix'),
    pytest.param('99999', id='short-absent'),
    pytest.param('01', id='leading-zero'),
    pytest.param('00', id='double-zero'),
    pytest.param('١', id='arabic-decimal'),
    pytest.param('１', id='fullwidth-decimal'),
    pytest.param('²', id='superscript'),
    pytest.param('', id='empty-token'),
    pytest.param('-1', id='negative'),
    pytest.param('+1', id='plus'),
    pytest.param(' 1', id='space'),
    pytest.param('-', id='append-token'),
    pytest.param('1.0', id='decimal-point'),
]


@pytest.mark.parametrize('token', ABSENT)
@pytest.mark.parametrize('traverse', [False, True], ids=['leaf', 'intermediate'])
@pytest.mark.parametrize('size', [0, 1, 3])
def test_array_absent_indices_are_noops(token, traverse, size):
    message = request(params={'items': [{'secret': ['keep', 7]} for _ in range(size)]})
    original = copy.deepcopy(message)
    pointer = '/params/items/' + token + ('/secret' if traverse else '')
    assert pointer_tokens(pointer)[3:] == (['secret'] if traverse else [])
    assert redact(message, [pointer]) == original
    assert message == original


@pytest.mark.parametrize('size', [1, 3, 11])
@pytest.mark.parametrize('traverse', [False, True], ids=['whole-value', 'nested-value'])
def test_array_exact_indices_and_first_out_of_range(size, traverse):
    message = request(params={'items': [{'a/b': {'~key': [7, 'secret']}} for _ in range(size)]})
    for index in range(size + 1):
        pointer = f'/params/items/{index}' + ('/a~1b/~0key/1' if traverse else '')
        expected = copy.deepcopy(message)
        if index < size:
            if traverse:
                expected['params']['items'][index]['a/b']['~key'][1] = REDACTED
            else:
                expected['params']['items'][index] = REDACTED
        assert redact(message, [pointer]) == expected
    assert message['params']['items'][0]['a/b']['~key'] == [7, 'secret']


@pytest.mark.parametrize('token', ABSENT)
@pytest.mark.parametrize('traverse', [False, True], ids=['leaf', 'intermediate'])
def test_object_keys_are_exact_strings_even_when_not_array_indices(token, traverse):
    message = request(params={'items': {token: {'a/b': {'~key': ['secret']}}}})
    pointer = '/params/items/' + token + ('/a~1b/~0key/0' if traverse else '')
    expected = copy.deepcopy(message)
    if traverse:
        expected['params']['items'][token]['a/b']['~key'][0] = REDACTED
    else:
        expected['params']['items'][token] = REDACTED
    assert redact(message, [pointer]) == expected


def test_array_traversal_stops_at_scalars_and_later_pointers_still_apply():
    message = request(params={'items': [None, 7, 'text', True, []]})
    paths = [f'/params/items/{i}/0' for i in range(5)] + ['/params/items/2']
    expected = request(params={'items': [None, 7, REDACTED, True, []]})
    assert redact(message, paths) == expected


@pytest.mark.parametrize('traverse', [False, True], ids=['leaf', 'intermediate'])
def test_long_indices_record_and_replay_with_typed_ids_and_valid_redactions(traverse):
    paths = ['/params/items/' + '9' * 5000 + ('/secret' if traverse else ''),
             '/params/items/' + '0' * 5000 + '1', '/params/items/99999',
             '/params/items/١', '/params/items/0/secret/1', '/result/items/0']
    recorder = Recorder(paths)
    for rpc_id in (7, '7'):
        recorder.append('client', request(params={'items': [{'secret': ['keep', {'token': rpc_id}]}]}, id=rpc_id))
    for rpc_id in ('7', 7):
        recorder.append('server', response(rpc_id, {'items': [{'whole': rpc_id}, 'keep']}))
    assert recorder.pending == {'client': set(), 'server': set()}
    data = recorder.document(0, [])
    assert data['redact_paths'] == paths
    assert not data['incomplete']
    assert data['events'][0]['message']['params']['items'][0]['secret'] == ['keep', REDACTED]
    assert data['events'][2]['message']['result']['items'] == [REDACTED, 'keep']
    for mode in ('params', 'sequence'):
        player = Replayer(data, strict=True, match=mode)
        for live_id in ('live', 9):
            msg = request(params={'items': [{'secret': ['keep', 'replacement']} ]}, id=live_id)
            assert player.handle(msg) == [response(live_id, {'items': [REDACTED, 'keep']})]
        assert player.finish() == 0
