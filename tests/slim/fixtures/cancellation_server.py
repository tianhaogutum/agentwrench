#!/usr/bin/env python3
"""Synthetic duplex fixture: hold requests until an explicit release, even after
cancellation. No timers, network access or production MCP endpoint involved."""
import json
import signal
import sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGINT, signal.SIG_DFL)
held = {}


def key(value):
    return (type(value).__name__, value)


def emit(message):
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
    sys.stdout.buffer.flush()


def tool(name):
    return {'name': name, 'description': 'Read. Extra instructions.', 'inputSchema': {
        'type': 'object', 'description': 'schema annotation',
        'properties': {'id': {'type': 'integer', 'description': 'field annotation'}},
        'required': ['id']}}


emit({'jsonrpc': '2.0', 'method': 'fixture/ready'})
for line in sys.stdin.buffer:
    m = json.loads(line)
    if 'id' not in m:
        emit({'jsonrpc': '2.0', 'method': 'fixture/notification_seen', 'params': m})
        continue
    method = m.get('method')
    params = m.get('params') if isinstance(m.get('params'), dict) else {}
    if method in ('tools/list', 'fixture/hold') or (method == 'initialize' and params.get('fixtureHold')):
        payload = {'result': {'held': True}}
        if method == 'tools/list':
            payload = {'result': {'tools': [tool(name) for name in ('read', 'read_admin', 'delete')],
                                  'nextCursor': 'tail', '_meta': {'fixture': True}, 'resultType': 'complete'}}
            result = params.get('fixtureResult')
            if result == 'invalid':
                payload['result']['tools'].append(None)
            if result == 'unsafe':
                payload['result']['_meta']['value'] = 1e100
            if result == 'cache':
                payload = {'result': {'resultType': 'not_modified', '_meta': {'fixture': True}}}
            if result == 'error':
                payload = {'error': {'code': -32603, 'message': 'fixture failure'}}
        held[key(m['id'])] = {'jsonrpc': '2.0', 'id': m['id'], **payload}
        emit({'jsonrpc': '2.0', 'method': 'fixture/held', 'params': {'id': m['id']}})
    else:
        if method == 'fixture/release':
            reply = held.pop(key(params.get('requestId')), None)
            if reply is not None:
                emit(reply)
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'received': m}})
