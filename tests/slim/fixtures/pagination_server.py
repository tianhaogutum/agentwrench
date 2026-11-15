#!/usr/bin/env python3
"""Synthetic cursor pages. fixture/* methods control replies, not MCP APIs."""
import json
import signal
import sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGINT, signal.SIG_DFL)
mode = sys.argv[1] if len(sys.argv) > 1 else 'normal'
held = {}
MISSING = object()


def key(value):
    return (type(value).__name__, value)


def emit(message):
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
    sys.stdout.buffer.flush()


def tool(name):
    return {'name': name, 'description': 'Read. Extra instructions.', 'inputSchema': {
        'type': 'object', 'description': 'schema annotation',
        'properties': {'id': {'type': 'integer', 'description': 'field annotation'}}}}


for line in sys.stdin.buffer:
    m = json.loads(line)
    if 'id' not in m:
        emit({'jsonrpc': '2.0', 'method': 'fixture/notification_seen', 'params': m})
        continue
    method = m.get('method')
    params = m.get('params') if isinstance(m.get('params'), dict) else {}
    if method == 'fixture/release':
        reply = held.pop(key(params.get('requestId')), None)
        if reply is not None:
            emit(reply)
    elif method == 'fixture/change':
        emit({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
    elif method == 'tools/list':
        cursor = params.get('cursor', MISSING)
        names, next_cursor = ['delete_first'], 'A'
        if cursor == 'A':
            names, next_cursor = ['read_z', 'read_admin', 'read_a'], 'B'
        if cursor == 'B':
            names, next_cursor = ['read_tail'], MISSING
        if mode == 'loop' and cursor == 'B':
            next_cursor = 'A'
        if mode == 'self':
            next_cursor = 'A' if cursor is MISSING else cursor
        if mode == 'empty':
            next_cursor = '' if cursor is MISSING else MISSING
        if mode == 'invalid':
            next_cursor = params.get('badCursor')
        if mode == 'context':
            next_cursor = 'B' if cursor == 'A' else 'A'
        if mode == 'chain':
            try:
                next_cursor = str(int(cursor) + 1)
            except (TypeError, ValueError):
                next_cursor = 'NaN'  # String(Number(undefined) + 1)
        if mode == 'cache' and cursor == 'B':
            emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'resultType': 'not_modified', '_meta': {'fixture': True}}})
            continue
        if mode == 'error' and cursor == 'B':
            emit({'jsonrpc': '2.0', 'id': m['id'], 'error': {'code': -32602, 'message': 'fixture cursor expired'}})
            continue
        result = {'tools': [tool(name) for name in names]}
        if next_cursor is not MISSING:
            result['nextCursor'] = next_cursor
        result.update({'_meta': {'fixture': True}, 'resultType': 'complete', 'ttlMs': 1000})
        reply = {'jsonrpc': '2.0', 'id': m['id'], 'result': result}
        if mode == 'late' and cursor == 'B':
            result['nextCursor'] = 'A'
            held[key(m['id'])] = reply
            emit({'jsonrpc': '2.0', 'method': 'fixture/held', 'params': {'id': m['id']}})
        else:
            emit(reply)
        continue
    emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'received': m}})
