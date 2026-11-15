#!/usr/bin/env python3
"""Synthetic legacy MCP discovery server; no network or tool execution."""
import json
import signal
import sys
import time

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGINT, signal.SIG_DFL)
mode = sys.argv[1] if len(sys.argv) > 1 else 'normal'
out = sys.stdout.buffer


def write(data):
    out.write(data.encode() if isinstance(data, str) else data)
    out.flush()


def dumps(message):
    return json.dumps(message, ensure_ascii=False, separators=(',', ':'))


def emit(message):
    write(dumps(message) + '\n')


def fail(message):
    sys.stderr.write(f'fixture error: {message}\n')
    sys.exit(1)


def tool(name):
    return {'name': name, 'description': 'Read. Extra instructions.', 'inputSchema': {
        'type': 'object', 'description': 'schema annotation',
        'properties': {'id': {'type': 'integer', 'description': 'field annotation'}}},
        'annotations': {'readOnlyHint': True}}


initialized = notified = stubborn = False
exit_code = 0
page = 0
for line in sys.stdin.buffer:
    m = json.loads(line)
    method = m.get('method')
    params = m.get('params') if isinstance(m.get('params'), dict) else {}
    if method == 'initialize':
        initialized = True
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {
            'protocolVersion': 'unsupported' if mode == 'version' else m['params']['protocolVersion'],
            'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}})
        continue
    if method == 'notifications/initialized' and initialized:
        notified = True
        continue
    if method != 'tools/list' or not notified:
        fail('unexpected discovery request')
    page += 1
    cursor = params.get('cursor')
    if page > 3 or (page == 1 and 'cursor' in params) or (page == 2 and cursor != '') or (page == 3 and cursor != 'tail\U0001f98a'):
        fail('incorrect cursor traversal')
    names = ['delete_first'] if page == 1 else ['read_z', 'read_admin', 'read_a'] if page == 2 else ['read_tail']
    result = {'tools': [tool(name) for name in names]}
    if page != 3:
        result['nextCursor'] = '' if page == 1 else 'tail\U0001f98a'
    result.update({'_meta': {'fixture': True}, 'resultType': 'complete', 'ttlMs': 1000})
    if page == 2 and mode == 'self':
        result['nextCursor'] = ''
    if page == 3:
        if mode == 'cycle':
            result['nextCursor'] = ''
        if mode == 'cursor':
            result['nextCursor'] = None
        if mode == 'badtool':
            result['tools'] = [{'name': 'read_invalid'}]
        if mode == 'cache':
            result = {'resultType': 'not_modified'}
        if mode == 'noncomplete-tools':
            result['resultType'] = 'input_required'
        if mode == 'duplicate':
            result['tools'] = [tool('read_a')]
        if mode == 'hidden-duplicate':
            result['tools'] = [tool('delete_first')]
        if mode == 'error':
            emit({'jsonrpc': '2.0', 'id': m['id'], 'error': {'code': -32602, 'message': 'PRIVATE cursor expired'}})
            continue
        if mode == 'timeout':
            continue
        if mode == 'early':
            sys.exit(0)
        if mode == 'crash':
            sys.exit(7)
        if mode == 'changed':
            emit({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
        if mode == 'request':
            emit({'jsonrpc': '2.0', 'id': 'server', 'method': 'sampling/createMessage', 'params': {}})
        if mode == 'noise':
            write('PRIVATE invalid stdout\n')
            continue
        if mode == 'wrongid':
            emit({'jsonrpc': '2.0', 'id': str(m['id']), 'result': result})
            continue
        if mode == 'unsafe':
            result['tools'][0]['inputSchema']['minimum'] = 'UNSAFE_NUMBER'
            write(dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': result}).replace('"UNSAFE_NUMBER"', '9007199254740993', 1) + '\n')
            continue
    if page == 1 and mode == 'backpressure':
        result['nextCursor'] = 'x' * (2 * 1024 * 1024)
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': result})
        # Reply speculatively without consuming the next (large) request. Its
        # response must not disable the deadline for the still-blocked write.
        time.sleep(0.01)
        emit({'jsonrpc': '2.0', 'id': m['id'] + 1, 'result': {'tools': []}})
        while True:
            time.sleep(10)
    emit({'jsonrpc': '2.0', 'id': m['id'], 'result': result})
    if page == 3 and mode == 'exit-after':
        exit_code = 7
    if page == 3 and mode == 'stubborn':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stubborn = True
if stubborn:
    while True:
        time.sleep(10)
sys.exit(exit_code)
