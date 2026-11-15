#!/usr/bin/env python3
"""Synthetic EOF flushes and failures, with an optional trace of direct-child cleanup."""
import json
import signal
import sys
import time

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGINT, signal.SIG_DFL)
mode = sys.argv[1] if len(sys.argv) > 1 else 'immediate'
trace = sys.argv[2] if len(sys.argv) > 2 else None
out = sys.stdout.buffer


def record(text):
    if trace:
        with open(trace, 'a', encoding='utf-8') as handle:
            handle.write(f'{text}\n')


def write(data):
    out.write(data.encode())
    out.flush()


def reply(request_id, result):
    write(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result}, ensure_ascii=False, separators=(',', ':')) + '\n')


keep_alive = mode in ('hang', 'protocol', 'overflow')
if keep_alive:
    signal.signal(signal.SIGTERM, lambda *_: record('SIGTERM'))
for raw in sys.stdin.buffer:
    message = json.loads(raw)
    method = message.get('method')
    record(method)
    if method == 'initialize':
        reply(message['id'], {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'shutdown', 'version': '1'}})
    elif method == 'notifications/initialized':
        continue
    elif method == 'tools/list':
        if mode == 'slow-list':
            time.sleep(0.25)
        reply(message['id'], {'tools': [{'name': 'read_\U0001f98a', 'inputSchema': {'type': 'object'}}]})
    else:
        sys.stderr.write('fixture error: unexpected request\n')
        sys.exit(1)
record('EOF')
if mode.startswith('delay-'):
    time.sleep(int(mode[6:]) / 1000)
    write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {'text': 'flushed é\U0001f98a after EOF'}}, ensure_ascii=False, separators=(',', ':')) + '\n')
if mode == 'protocol':
    write('not JSON\n')
if mode == 'overflow':
    write(' ' * 1000)
if keep_alive:
    while True:
        time.sleep(10)
if mode == 'exit':
    sys.exit(7)
