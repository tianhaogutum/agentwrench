#!/usr/bin/env python3
"""Synthetic paced discovery: each reply is quick, but the complete walk is long."""
import json
import signal
import sys
import time

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGINT, signal.SIG_DFL)
mode = sys.argv[1] if len(sys.argv) > 1 else 'many'
log = sys.argv[2] if len(sys.argv) > 2 else None


def record(value):
    if log:
        with open(log, 'a', encoding='utf-8') as handle:
            handle.write(f'{value}\n')


def emit(request_id, result):
    sys.stdout.buffer.write((json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result}, separators=(',', ':')) + '\n').encode())
    sys.stdout.buffer.flush()


def fail(message):
    sys.stderr.write(f'fixture error: {message}\n')
    sys.exit(1)


initialized = False
page = 0
if mode == 'stubborn':
    signal.signal(signal.SIGTERM, lambda *_: record('SIGTERM'))
for line in sys.stdin.buffer:
    m = json.loads(line)
    method = m.get('method')
    record(method)
    if method == 'initialize':
        if mode == 'initialize':
            time.sleep(2)
        emit(m['id'], {'protocolVersion': m['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'paced', 'version': '1'}})
    elif method == 'notifications/initialized':
        initialized = True
    elif method == 'tools/list' and initialized:
        params = m.get('params') if isinstance(m.get('params'), dict) else {}
        if params.get('cursor') != (str(page) if page else None):
            fail('incorrect cursor')
        time.sleep(0.08)
        tools = [] if mode == 'empty' else [{'name': f'hidden_{page}', 'inputSchema': {'type': 'object'}}]
        page += 1
        result = {'tools': tools}
        if page < (1 if mode == 'shutdown' else 100):
            result['nextCursor'] = str(page)
        emit(m['id'], result)
    else:
        fail('unexpected discovery request')
if mode == 'shutdown':
    time.sleep(0.9)
