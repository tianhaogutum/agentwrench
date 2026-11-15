"""Port of test/proxy.test.js (real stdio exchanges through the proxy CLI)."""

import asyncio
import json
import re
import signal

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.proxy import lines, parse_message

from .conftest import PYTHON, fixture


def pagination_command(mode):
    return [PYTHON, fixture('pagination_server.py'), mode]


CANCELLATION_COMMAND = [PYTHON, fixture('cancellation_server.py')]
TIMEOUT_ERROR = {'code': -32001, 'message': 'agentwrench slim request timed out'}


def seen_cancellation(s, request_id):
    for m in s.messages:
        if m.get('method') == 'fixture/notification_seen':
            params = m['params'].get('params')
            if isinstance(params, dict) and 'requestId' in params and type(params['requestId']) is type(request_id) and params['requestId'] == request_id:
                return True
    return False


def cancel_held(s, request_id, method='tools/list', params=None):
    message = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params:
        message['params'] = params
    s.send(message)
    s.until(lambda: any(m.get('method') == 'fixture/held' and s.same_id({'id': m['params']['id']}, request_id) for m in s.messages))
    s.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': request_id, 'reason': 'test'}})
    s.until(lambda: seen_cancellation(s, request_id))


def test_e2e_initialize_pagination_calls_notifications_and_metadata(session):
    s = session('normal', {'allow': ['read_*', 'search_*'], 'deny': ['search_issue'], 'maxDescriptionChars': 30, 'stripSchemaAnnotations': ['description', 'title']})
    init = s.request(0, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
    assert init['result']['serverInfo']['name'] == 'synthetic-fixture'
    s.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    first = s.request('list1', 'tools/list')
    assert [t['name'] for t in first['result']['tools']] == ['read_issue']
    assert first['result']['nextCursor'] == 'page-2'
    assert first['result']['_meta'] == {'fixture': True}
    assert len(first['result']['tools'][0]['description']) <= 30
    assert 'description' not in first['result']['tools'][0]['inputSchema']['properties']['workspace']
    assert first['result']['tools'][0]['inputSchema']['required'] == ['workspace', 'id']
    second = s.request('list2', 'tools/list', {'cursor': first['result']['nextCursor']})
    assert len(second['result']['tools']) == 14
    assert 'nextCursor' not in second['result']
    s.until(lambda: len([m for m in s.messages if m.get('method') == 'notifications/tools/list_changed']) == 2)
    args = {'name': 'read_issue', 'arguments': {'id': 7, 'workspace': 'demo', 'description': 'a\nb', 'text': '$(never run)'}}
    call = s.request(2, 'tools/call', args)
    assert json.loads(call['result']['content'][0]['text']) == args
    hidden = s.request(3, 'tools/call', {'name': 'search_issue'})
    assert hidden['error'] == {'code': -32602, 'message': 'tool hidden by agentwrench slim'}
    never_listed = s.request(4, 'tools/call', {'name': 'delete_unknown'})
    assert never_listed['error']['code'] == -32602
    assert s.finish() == 0


def test_transparent_raw_response_server_requests_client_responses_notification_passthrough(session):
    s = session()
    s.request(1, 'fixture/raw')
    assert s.raw[0] == '{ "jsonrpc" : "2.0", "id": 1, "result" : { "raw" : true, "large":9007199254740993 } }'
    s.request('r', 'fixture/request')
    assert any(m.get('method') == 'sampling/createMessage' for m in s.messages)
    s.send({'jsonrpc': '2.0', 'id': 'server-id', 'result': {'content': []}})
    s.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 99, 'reason': 'test'}})
    s.until(lambda: any(m.get('method') == 'fixture/notification_seen' and m['params']['method'] == 'notifications/cancelled' for m in s.messages))
    assert s.finish() == 0


def test_modern_discover_per_request_metadata_and_list_complete_result_fields_preserved(session):
    s = session('normal', {'deny': ['delete_*']})
    meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28', 'io.modelcontextprotocol/clientCapabilities': {}}
    discover = s.request('discover', 'server/discover', {}, {'_meta': meta})
    assert discover['result']['supportedVersions'] == ['2026-07-28']
    echo = s.request('echo', 'echo', {'key': 'value'}, {'_meta': meta})
    assert echo['result']['received']['_meta'] == meta
    listed = s.request('list', 'tools/list', {}, {'_meta': meta})
    assert listed['result']['resultType'] == 'complete'
    assert listed['result']['ttlMs'] == 1000
    assert len(listed['result']['tools']) == 4
    assert s.finish() == 0


def test_modern_tools_list_non_complete_cache_result_is_forwarded(session):
    s = session('cache')
    assert s.request(1, 'tools/list')['result'] == {'resultType': 'not_modified', '_meta': {'opaque': 'keep'}}
    assert s.finish() == 0


def test_filter_empty_first_page_preserves_cursor_and_emits_zero_match_warning(session):
    s = session('normal', {'allow': ['read_attachment']})
    first = s.request(1, 'tools/list')
    assert first['result']['tools'] == []
    assert first['result']['nextCursor'] == 'page-2'
    second = s.request(2, 'tools/list', {'cursor': first['result']['nextCursor']})
    assert [t['name'] for t in second['result']['tools']] == ['read_attachment']
    s.finish()
    assert re.search('allow list matched zero tools', s.stderr())


def test_cursor_pagination_deterministic_filtering_through_empty_and_terminal_pages(session):
    s = session('normal', {'allow': ['read_*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']},
                pagination_command('normal'))
    first = s.request(1, 'tools/list')
    assert first['result']['tools'] == []
    assert first['result']['nextCursor'] == 'A'
    second = s.request(2, 'tools/list', {'cursor': first['result']['nextCursor']})
    assert [t['name'] for t in second['result']['tools']] == ['read_z', 'read_a']
    assert second['result']['nextCursor'] == 'B'
    assert second['result']['_meta'] == {'fixture': True}
    assert second['result']['ttlMs'] == 1000
    assert second['result']['resultType'] == 'complete'
    assert second['result']['tools'][0]['description'] == 'Read.'
    assert second['result']['tools'][0]['inputSchema'] == {'type': 'object', 'properties': {'id': {'type': 'integer'}}}
    retry = s.request(3, 'tools/list', {'cursor': 'A'})
    assert retry['result'] == second['result']
    last = s.request(4, 'tools/list', {'cursor': second['result']['nextCursor']})
    assert [t['name'] for t in last['result']['tools']] == ['read_tail']
    assert 'nextCursor' not in last['result']
    assert s.finish() == 0


def test_cursor_pagination_self_loops_and_multi_page_loops_fail_explicitly(session):
    for mode in ('self', 'loop'):
        s = session('normal', {'allow': []}, pagination_command(mode))
        s.request('start', 'tools/list')
        a = s.request('a', 'tools/list', {'cursor': 'A'})
        bad = a if mode == 'self' else s.request('b', 'tools/list', {'cursor': a['result']['nextCursor']})
        assert bad == {'jsonrpc': '2.0', 'id': 'a' if mode == 'self' else 'b', 'error': {
            'code': -32603, 'message': 'agentwrench slim rejected tools/list pagination loop'}}
        assert s.request('ping', 'ping')['result']['received']['method'] == 'ping'
        assert s.finish() == 0


def test_cursor_pagination_empty_string_is_an_opaque_cursor_malformed_cursors_are_rejected(session):
    s = session('normal', {}, pagination_command('empty'))
    assert s.request(1, 'tools/list')['result']['nextCursor'] == ''
    assert 'nextCursor' not in s.request(2, 'tools/list', {'cursor': ''})['result']
    for i, cursor in enumerate([None, 0, False, [], {}]):
        bad = s.request(f'invalid-{i}', 'tools/list', {'cursor': cursor})
        assert bad.get('error', {}).get('code') == -32602
    assert s.request('array', 'tools/list', [])['error']['code'] == -32602
    assert s.finish() == 0
    invalid = session('normal', {}, pagination_command('invalid'))
    for i, bad_cursor in enumerate([None, 0, False, [], {}]):
        assert invalid.request(i, 'tools/list', {'badCursor': bad_cursor}).get('error', {}).get('code') == -32603
    assert invalid.finish() == 0


def test_cursor_pagination_context_isolation_canonical_keys_and_changing_progress_tokens(session):
    s = session('normal', {}, pagination_command('context'))

    def params(cursor, tenant, progress_token):
        return {'cursor': cursor, 'tenant': tenant, '_meta': {'progressToken': progress_token, 'selector': {'x': 1, 'y': 2}}}

    assert s.request(1, 'tools/list', params('A', 'one', 1))['result']['nextCursor'] == 'B'
    # Opposite edges in different catalogues must not form a false loop.
    assert s.request(2, 'tools/list', params('B', 'two', 2))['result']['nextCursor'] == 'A'
    loop = s.request(3, 'tools/list', {'_meta': {'selector': {'y': 2, 'x': 1}, 'progressToken': 3}, 'tenant': 'one', 'cursor': 'B'})
    assert re.search('pagination loop', loop.get('error', {}).get('message', ''))
    assert s.finish() == 0


def test_cursor_pagination_fresh_discovery_and_list_changed_reset_history(session):
    s = session('normal', {}, pagination_command('context'))
    s.request(1, 'tools/list', {'cursor': 'A'})
    s.request(2, 'tools/list')
    assert s.request(3, 'tools/list', {'cursor': 'B'})['result']['nextCursor'] == 'A'
    s.request(4, 'fixture/change')
    assert s.request(5, 'tools/list', {'cursor': 'A'})['result']['nextCursor'] == 'B'
    assert s.finish() == 0


def test_cursor_pagination_late_cancelled_page_still_rejects_a_loop(session):
    s = session('normal', {'allow': []}, pagination_command('late'))
    s.request('a', 'tools/list', {'cursor': 'A'})
    cancel_held(s, 'late', 'tools/list', {'cursor': 'B'})
    s.request('barrier', 'fixture/release', {'requestId': 'late'})
    assert re.search('pagination loop', s.reply('late').get('error', {}).get('message', ''))
    assert s.finish() == 0


def test_cursor_pagination_errors_and_non_complete_results_stay_transparent(session):
    for mode in ('cache', 'error'):
        s = session('normal', {'allow': []}, pagination_command(mode))
        s.request(1, 'tools/list', {'cursor': 'A'})
        last = s.request(2, 'tools/list', {'cursor': 'B'})
        if mode == 'cache':
            assert last['result'] == {'resultType': 'not_modified', '_meta': {'fixture': True}}
        else:
            assert last['error'] == {'code': -32602, 'message': 'fixture cursor expired'}
        assert s.finish() == 0


def test_cursor_pagination_retained_page_limit_returns_an_explicit_error_without_losing_history(session):
    s = session('normal', {'allow': []}, pagination_command('chain'))
    # Seed one completed request so this tests pagination capacity independently
    # of the separate 10,000 in-flight-request limit, even with a slow child.
    s.request(0, 'tools/list', {'cursor': '0'})
    s.write(''.join(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': 'tools/list', 'params': {'cursor': str(i)}}) + '\n' for i in range(1, 10001)))
    s.until(lambda: s.reply(10000) is not None, timeout=60)
    assert s.reply(10000)['error'] == {'code': -32603, 'message': 'agentwrench slim pagination page limit reached'}
    assert len([m for m in s.messages if isinstance(m.get('result'), dict) and m['result'].get('nextCursor')]) == 10000
    retry = s.request('retry', 'tools/list', {'cursor': '0'})
    assert retry['result']['nextCursor'] == '1'
    s.request('restart', 'tools/list')
    assert s.request('after', 'tools/list', {'cursor': '10000'})['result']['nextCursor'] == '10001'
    assert s.finish() == 0


def test_cursor_pagination_context_limit_rejects_locally_and_list_changed_releases_history(session):
    s = session('normal', {'allow': []}, pagination_command('context'))
    s.request(0, 'tools/list', {'cursor': 'A', 'tenant': 0})
    s.write(''.join(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': 'tools/list', 'params': {'cursor': 'A', 'tenant': i}}) + '\n' for i in range(1, 10001)))
    # The local limit error can arrive before upstream pages; wait for all ids.
    s.until(lambda: len([m for m in s.messages if 'id' in m]) == 10001, timeout=60)
    assert s.reply(10000)['error'] == {'code': -32000, 'message': 'agentwrench slim pagination context limit reached'}
    assert re.search('pagination loop', s.request('loop', 'tools/list', {'cursor': 'B', 'tenant': 0})['error']['message'])
    s.request('change', 'fixture/change')
    assert s.request('after', 'tools/list', {'cursor': 'B', 'tenant': 10000})['result']['nextCursor'] == 'A'
    assert s.finish() == 0


def test_non_json_and_invalid_envelope_stdout_never_corrupt_stream_stderr_is_inherited(session):
    s = session('noise')
    assert s.request(1, 'ping')['result']['received']['method'] == 'ping'
    assert s.finish() == 0
    assert re.search('stdout line diverted to stderr', s.stderr())
    assert re.search('fixture stderr marker', s.stderr())
    assert len(s.messages) == 1
    assert not re.search('not-a-message', s.stderr())


def test_invalid_utf8_server_line_is_discarded_and_next_response_works(session):
    s = session('bad-utf8')
    s.request(1, 'ping')
    assert s.finish() == 0
    assert re.search('invalid server stdout', s.stderr())


def test_crash_returns_errors_for_all_pending_ids_and_propagates_exit_7(session):
    s = session('crash')
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    s.send({'jsonrpc': '2.0', 'id': '1', 'method': 'tools/list'})
    assert s.closed() == 7
    assert len(s.messages) == 2
    assert sorted((m['id'] for m in s.messages), key=repr) == sorted([1, '1'], key=repr)
    assert all(m['error']['code'] == -32000 for m in s.messages)


def test_clean_early_exit_with_pending_request_still_fails_no_unhandled_stream_errors(session):
    s = session('clean-pending')
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    assert s.closed() == 1
    assert s.messages[0]['error']['code'] == -32000
    assert not re.search('Traceback|Unhandled|ERR_STREAM', s.stderr())


def test_server_command_not_found_fails_without_exposing_command_args(session):
    s = session('normal', {}, ['/no/such/server', 'secret-argument'])
    assert s.closed() == 1
    assert re.search(r'cannot start server \(ENOENT\)', s.stderr())
    assert not re.search('secret-argument', s.stderr())


def test_invalid_tools_list_fails_locally_instead_of_exposing_unslimmed_tools(session):
    s = session('invalid-list')
    assert s.request(1, 'tools/list')['error']['code'] == -32603
    assert s.finish() == 0


def test_malformed_client_json_and_invalid_call_params_handled_locally(session):
    s = session()
    s.write('not-json\n')
    s.until(lambda: any(m.get('id', 0) is None and m.get('error', {}).get('code') == -32700 for m in s.messages))
    assert s.request(1, 'tools/call', {})['error']['code'] == -32602
    assert s.request('long-name', 'tools/call', {'name': 'x' * 1025})['error']['code'] == -32602
    assert s.request(2, 'ping')['result']['received']['method'] == 'ping'
    assert s.finish() == 0


def test_sigterm_shuts_down_server_and_returns_signal_derived_exit_code(session):
    s = session()
    s.request(1, 'ping')
    s.child.send_signal(signal.SIGTERM)
    assert s.closed() == 143


def test_eof_kills_an_uncooperative_server_within_bounded_grace(session):
    s = session('stubborn')
    s.until(lambda: any(m.get('method') == 'fixture/ready' for m in s.messages))
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    s.end_stdin()
    assert s.closed(timeout=6) == 137
    assert s.reply(1)['error']['code'] == -32000


def test_oversized_server_line_is_bounded_and_pending_call_fails(session):
    s = session('oversize')
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    assert s.closed(timeout=6) != 0
    assert re.search('8 MiB', s.stderr())
    assert s.messages[0]['error']['code'] == -32000


def test_unterminated_server_line_fails_explicitly_and_preserves_child_exit_code(session):
    s = session('partial')
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    assert s.closed() == 3
    assert re.search('missing final newline', s.stderr())
    assert s.messages[0]['error']['code'] == -32000


def test_bounded_line_decoder_handles_utf8_split_across_chunks_and_rejects_batches():
    async def chunks(*parts):
        for part in parts:
            yield part

    async def collect(stream, *args):
        return [line async for line in lines(stream, *args)]

    raw = '{"jsonrpc":"2.0","method":"你好"}\n'.encode()
    got = asyncio.run(collect(chunks(raw[:29], raw[29:])))
    assert parse_message(got[0])['method'] == '你好'
    with pytest.raises(ValueError):
        parse_message((json.dumps({'jsonrpc': '2.0', 'id': 'x' * 1025, 'result': {}}) + '\n').encode())
    with pytest.raises(ValueError):
        parse_message(b'[{"jsonrpc":"2.0","method":"ping"}]\n')
    with pytest.raises(ValueError):
        parse_message(b'{"jsonrpc":"2.0","id":9007199254740993,"result":{}}\n')
    with pytest.raises(InputError, match='8 MiB'):
        asyncio.run(collect(chunks(b'abcdef'), 5))
    with pytest.raises(InputError, match='newline'):
        asyncio.run(collect(chunks(b'{}')))


def test_cancellation_passes_through_and_removes_pending_exit_errors(session):
    s = session()
    s.send({'jsonrpc': '2.0', 'id': 10, 'method': 'fixture/cancel'})
    s.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 10}})
    s.until(lambda: any(m.get('method') == 'fixture/notification_seen' for m in s.messages))
    following = s.request(11, 'ping')
    assert following['result']['received']['method'] == 'ping'
    assert s.finish() == 0
    assert not any(s.same_id(m, 10) for m in s.messages)


def test_duplex_cancellation_late_tools_list_is_still_filtered_and_trimmed(session):
    s = session('normal', {'allow': ['read*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']},
                CANCELLATION_COMMAND)
    cancel_held(s, 'late')
    # Keep stdin open and release only after the server has observed cancellation.
    # The release response is a barrier proving the late response was processed.
    s.request('barrier', 'fixture/release', {'requestId': 'late'})
    reply = s.reply('late')
    assert reply['result'] == {
        'tools': [{'name': 'read', 'description': 'Read.', 'inputSchema': {
            'type': 'object', 'properties': {'id': {'type': 'integer'}}, 'required': ['id']}}],
        'nextCursor': 'tail', '_meta': {'fixture': True}, 'resultType': 'complete'}
    assert s.request('hidden', 'tools/call', {'name': 'delete'})['error']['code'] == -32602
    assert s.finish() == 0


def test_duplex_cancellation_invalid_late_lists_fail_closed(session):
    for fixture_result in ('invalid', 'unsafe'):
        s = session('normal', {'allow': ['read']}, CANCELLATION_COMMAND)
        cancel_held(s, 1, 'tools/list', {'fixtureResult': fixture_result})
        s.request('barrier', 'fixture/release', {'requestId': 1})
        assert s.reply(1) == {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32603, 'message': 'agentwrench slim rejected invalid tools/list result'}}
        assert s.finish() == 0


def test_duplex_cancellation_late_errors_and_cache_results_retain_their_meaning(session):
    for fixture_result in ('error', 'cache'):
        s = session('normal', {'allow': []}, CANCELLATION_COMMAND)
        cancel_held(s, 'late', 'tools/list', {'fixtureResult': fixture_result})
        s.request('barrier', 'fixture/release', {'requestId': 'late'})
        reply = s.reply('late')
        if fixture_result == 'error':
            assert reply['error'] == {'code': -32603, 'message': 'fixture failure'}
        else:
            assert reply['result'] == {'resultType': 'not_modified', '_meta': {'fixture': True}}
        assert s.finish() == 0


def test_duplex_cancellation_unresolved_ids_cannot_be_reused_across_request_types(session):
    for old_method, next_method in (('tools/list', 'ping'), ('fixture/hold', 'tools/list'), ('tools/list', 'tools/call')):
        s = session('normal', {'allow': ['read']}, CANCELLATION_COMMAND)
        cancel_held(s, 1, old_method)
        s.send({'jsonrpc': '2.0', 'id': 1, 'method': next_method, 'params': {'name': 'delete'}})
        # Barrier also prevents an old implementation from hanging with a held list.
        s.send({'jsonrpc': '2.0', 'id': 'barrier', 'method': 'ping'})
        s.until(lambda: s.reply('barrier') is not None or re.search('cancelled client id', s.stderr()) is not None)
        assert re.search('unresolved cancelled client id', s.stderr())
        assert s.closed() != 0
        assert not any(s.same_id(m, 1) for m in s.messages)


def test_duplex_cancellation_typed_ids_stay_distinct_and_correlation_clears_on_reply(session):
    s = session('normal', {'allow': ['read']}, CANCELLATION_COMMAND)
    cancel_held(s, 1)
    assert s.request('1', 'ping')['result']['received']['method'] == 'ping'
    # Repeated and unknown cancellations must not reserve extra ids.
    s.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 1}})
    s.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 'unused'}})
    s.request('barrier', 'fixture/release', {'requestId': 1})
    assert [t['name'] for t in s.reply(1)['result']['tools']] == ['read']
    seen = len(s.messages)
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    s.until(lambda: any(s.same_id(m, 1) for m in s.messages[seen:]))
    assert next(m for m in s.messages[seen:] if s.same_id(m, 1))['result']['received']['method'] == 'ping'
    s.request('unused', 'ping')
    assert s.finish() == 0


def test_duplex_cancellation_bounded_tracking_fails_closed_rather_than_evicting_correlation(session):
    s = session('hold')
    payload = ''.join(json.dumps(m) + '\n' for i in range(10001) for m in (
        {'jsonrpc': '2.0', 'id': i, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': i}}))
    s.write(payload)
    s.end_stdin()
    assert s.closed(timeout=60) != 0
    assert re.search('cancelled request limit reached', s.stderr())
    # Cancelled ids are excluded from shutdown errors; the overflowing request is
    # still pending and gets the ordinary upstream-exited error.
    assert [m['id'] for m in s.messages] == [10000]
    assert s.messages[0]['error']['code'] == -32000


def test_duplicate_live_client_ids_fail_transport_instead_of_corrupting_list_correlation(session):
    s = session('hold')
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    assert s.closed() != 0
    assert re.search('duplicate in-flight client id', s.stderr())
    assert len([m for m in s.messages if s.same_id(m, 1)]) == 1


def test_hiding_works_before_discovery_and_hidden_notifications_never_reach_server(session):
    s = session('normal', {'deny': ['delete_*']})
    assert s.request('hidden', 'tools/call', {'name': 'delete_issue'})['error']['code'] == -32602
    s.send({'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'delete_issue'}})
    s.request('ping', 'ping')
    s.finish()
    assert not any(m.get('method') == 'fixture/notification_seen' for m in s.messages)


def test_non_object_lists_and_unsafe_transformed_metadata_fail_without_silent_corruption(session):
    for mode in ('bare-list', 'unsafe-list'):
        s = session(mode)
        assert s.request(1, 'tools/list')['error']['code'] == -32603
        assert s.finish() == 0


def test_duplicate_id_cannot_bypass_request_correlation_using_a_locally_hidden_call(session):
    s = session('hold', {'deny': ['delete_*']})
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'delete_issue'}})
    assert s.closed() != 0
    assert len([m for m in s.messages if s.same_id(m, 1)]) == 1
    assert s.messages[0]['error']['code'] == -32000


@pytest.fixture
def timeout_session(session):
    def start(config=None):
        s = session('normal', config or {}, CANCELLATION_COMMAND, ['--request-timeout-ms', '200'])
        s.until(lambda: any(m.get('method') == 'fixture/ready' for m in s.messages))
        return s
    return start


def test_request_timeout_never_responding_server_receives_cancellation_and_proxy_stays_usable(timeout_session):
    s = timeout_session()
    assert s.request('never', 'fixture/hold')['error'] == TIMEOUT_ERROR
    s.until(lambda: seen_cancellation(s, 'never'))
    cancelled = next(m for m in s.messages if m.get('method') == 'fixture/notification_seen')['params']
    assert cancelled == {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {
        'requestId': 'never', 'reason': 'agentwrench slim request timed out'}}
    assert s.request('ping', 'ping')['result']['received']['method'] == 'ping'
    assert s.finish() == 0
    assert len([m for m in s.messages if s.same_id(m, 'never')]) == 1


def test_request_timeout_late_tools_list_definitions_are_discarded_and_fresh_lists_remain_filtered(timeout_session):
    s = timeout_session({'allow': ['read*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']})
    assert s.request('late', 'tools/list')['error'] == TIMEOUT_ERROR
    s.request('barrier', 'fixture/release', {'requestId': 'late'})
    assert len([m for m in s.messages if s.same_id(m, 'late')]) == 1
    # The late terminal reply frees the reservation. Test the same ID again.
    seen = len(s.messages)
    s.send({'jsonrpc': '2.0', 'id': 'late', 'method': 'tools/list'})
    s.until(lambda: any(m.get('method') == 'fixture/held' for m in s.messages[seen:]))
    s.request('release', 'fixture/release', {'requestId': 'late'})
    reply = next(m for m in s.messages[seen:] if s.same_id(m, 'late'))
    assert reply['result']['tools'] == [{'name': 'read', 'description': 'Read.', 'inputSchema': {
        'type': 'object', 'properties': {'id': {'type': 'integer'}}, 'required': ['id']}}]
    assert s.request('hidden', 'tools/call', {'name': 'delete'})['error']['code'] == -32602
    assert s.finish() == 0


def test_request_timeout_late_invalid_lists_cache_results_errors_and_ordinary_replies_add_no_terminal_response(timeout_session):
    s = timeout_session({'allow': ['read']})
    for fixture_result in ('invalid', 'unsafe', 'cache', 'error', 'ordinary'):
        method = 'fixture/hold' if fixture_result == 'ordinary' else 'tools/list'
        assert s.request(fixture_result, method, {'fixtureResult': fixture_result})['error'] == TIMEOUT_ERROR
        s.request(f'release-{fixture_result}', 'fixture/release', {'requestId': fixture_result})
        assert len([m for m in s.messages if s.same_id(m, fixture_result)]) == 1
    assert s.finish() == 0


def test_request_timeout_unresolved_timed_out_ids_cannot_be_reused_typed_ids_remain_distinct(timeout_session):
    s = timeout_session({'deny': ['delete']})
    assert s.request(1, 'tools/list')['error'] == TIMEOUT_ERROR
    assert s.request('1', 'ping')['result']['received']['method'] == 'ping'
    s.send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'delete'}})
    assert s.closed() != 0
    assert re.search('unresolved cancelled client id', s.stderr())
    assert len([m for m in s.messages if s.same_id(m, 1)]) == 1


def test_request_timeout_explicit_client_cancellation_disarms_the_deadline(timeout_session):
    s = timeout_session()
    cancel_held(s, 'cancelled')
    # A second held request expiring proves the first deadline has elapsed.
    assert s.request('clock', 'fixture/hold')['error'] == TIMEOUT_ERROR
    assert not any(s.same_id(m, 'cancelled') for m in s.messages)
    assert len([m for m in s.messages if m.get('method') == 'fixture/notification_seen'
                and m['params']['params'].get('requestId') == 'cancelled']) == 1
    s.request('barrier', 'fixture/release', {'requestId': 'cancelled'})
    assert [t['name'] for t in s.reply('cancelled')['result']['tools']] == ['read', 'read_admin', 'delete']
    assert s.finish() == 0


def test_request_timeout_initialize_task_augmented_and_null_id_requests_time_out_without_invalid_cancellation(timeout_session):
    s = timeout_session()
    s.send({'jsonrpc': '2.0', 'id': 'init', 'method': 'initialize', 'params': {'fixtureHold': True}})
    s.send({'jsonrpc': '2.0', 'id': 'task', 'method': 'fixture/hold', 'params': {'task': {'ttl': 1000}}})
    s.send({'jsonrpc': '2.0', 'id': None, 'method': 'fixture/hold'})
    s.until(lambda: all(s.reply(request_id) is not None for request_id in ('init', 'task', None)))
    s.request('barrier', 'ping')
    for request_id in ('init', 'task', None):
        assert s.reply(request_id)['error'] == TIMEOUT_ERROR
    assert not any(m.get('method') == 'fixture/notification_seen' for m in s.messages)
    assert s.finish() == 0


def test_request_timeout_responses_before_expiry_and_server_initiated_requests_stay_transparent(session):
    s = session('normal', {}, None, ['--request-timeout-ms', '1000'])
    assert s.request('ping', 'ping')['result']['received']['method'] == 'ping'
    s.request('request', 'fixture/request')
    assert any(m.get('id') == 'server-id' and m.get('method') == 'sampling/createMessage' for m in s.messages)
    s.send({'jsonrpc': '2.0', 'id': 'server-id', 'result': {'content': []}})
    assert s.finish() == 0
    assert not any(isinstance(m.get('error'), dict) and m['error'].get('code') == -32001 for m in s.messages)
