#!/usr/bin/env python3
"""Synthetic offline MCP server. Never accesses a network or real workspace."""
import json
import os
import signal
import sys
import time

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGINT, signal.SIG_DFL)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools-40.json'), encoding='utf-8') as handle:
    TOOLS = json.load(handle)['tools']
mode = sys.argv[1] if len(sys.argv) > 1 else 'normal'
out = sys.stdout.buffer


def write(data):
    out.write(data.encode() if isinstance(data, str) else data)
    out.flush()


def emit(message):
    write(json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n')


if mode == 'early':
    sys.exit(0)
if mode == 'stubborn':
    emit({'jsonrpc': '2.0', 'method': 'fixture/ready'})
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin.buffer:
    m = json.loads(line)
    if mode == 'crash':
        sys.exit(7)
    if mode == 'clean-pending':
        sys.exit(0)
    if mode == 'noise':
        write('server diagnostic (not JSON)\n{"secret":"not-a-message"}\n')
        sys.stderr.write('fixture stderr marker\n')
        sys.stderr.flush()
    if mode == 'bad-utf8':
        write(b'\xff\n')
    if mode in ('hold', 'stubborn'):
        continue
    if mode == 'partial':
        write('{"jsonrpc":"2.0"')
        sys.exit(3)
    if mode == 'oversize':
        write('x' * (8 * 1024 * 1024 + 1))
        continue
    if 'id' not in m:
        emit({'jsonrpc': '2.0', 'method': 'fixture/notification_seen', 'params': m})
        continue
    method = m.get('method')
    params = m.get('params') if isinstance(m.get('params'), dict) else {}
    if method == 'initialize':
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {'listChanged': True}}, 'serverInfo': {'name': 'synthetic-fixture', 'version': '1.0.0'}}})
    elif method == 'server/discover':
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'resultType': 'complete', 'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {'listChanged': True}}}})
    elif method == 'tools/list':
        if mode == 'unsafe-list':
            emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'tools': TOOLS, '_meta': {'value': 1e100}}})
            continue
        if mode == 'bare-list':
            emit({'jsonrpc': '2.0', 'id': m['id'], 'result': TOOLS})
            continue
        if mode == 'invalid-list':
            emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'tools': [None]}})
            continue
        if mode == 'cache':
            emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'resultType': 'not_modified', '_meta': {'opaque': 'keep'}}})
            continue
        cursor = params.get('cursor')
        result = {'resultType': 'complete', 'ttlMs': 1000} if m.get('_meta') else {}
        result['tools'] = TOOLS[5:] if cursor else TOOLS[:5]
        if not cursor:
            result['nextCursor'] = 'page-2'
        result['_meta'] = {'fixture': True}
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': result})
        if mode != 'static':  # 'static': a catalogue that never changes (discoverable)
            emit({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
    elif method == 'tools/call':
        text = json.dumps(m.get('params'), ensure_ascii=False, separators=(',', ':'))
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'content': [{'type': 'text', 'text': text}]}})
    elif method == 'fixture/raw':
        write(f'{{ "jsonrpc" : "2.0", "id": {json.dumps(m["id"])}, "result" : {{ "raw" : true, "large":9007199254740993 }} }}\n')
    elif method == 'fixture/request':
        emit({'jsonrpc': '2.0', 'id': 'server-id', 'method': 'sampling/createMessage', 'params': {}})
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {}})
    elif method == 'fixture/cancel':
        pass  # A cancelled request receives no response.
    else:
        emit({'jsonrpc': '2.0', 'id': m['id'], 'result': {'received': m}})
if mode == 'stubborn':
    while True:
        time.sleep(10)
