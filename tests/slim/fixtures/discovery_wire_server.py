#!/usr/bin/env python3
"""Deterministic raw UTF-8 discovery traffic; imported by tests for byte totals."""
import json
import signal
import sys
import time


def _line(value):
    return (' \t' + json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\r\n').encode()


def _reply(request_id, result):
    return _line({'jsonrpc': '2.0', 'id': request_id, 'result': result})


def _notice(text):
    return _line({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {'text': text}})


def wire_scenario(mode='normal'):
    init = _reply(1, {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'wire', 'version': '1'}})
    source = {'name': 'read_\U0001f98a', 'description': 'é\U0001f98a extra', 'inputSchema': {'type': 'object', 'description': 'strip'}, 'annotations': {'readOnlyHint': True}}
    if mode == 'empty':
        pages = [_reply(2, {'tools': []})]
    else:
        pages = [
            _notice('ignored é\U0001f98a') + _reply(2, {'tools': [{'name': 'hidden', 'inputSchema': {'type': 'object'}}], 'nextCursor': ''}),
            _reply(3, {'tools': [source], 'nextCursor': 'tail\U0001f98a', '_meta': {'ignored': True}}),
            _reply(4, {'tools': []}),
        ]
    tail = b'' if mode == 'empty' else _notice('after terminal é\U0001f98a')
    return {'init': init, 'pages': pages, 'tail': tail, 'bytes': len(init) + sum(len(page) for page in pages) + len(tail)}


def _main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    mode = sys.argv[1] if len(sys.argv) > 1 else 'normal'
    trace = sys.argv[2] if len(sys.argv) > 2 else None
    scenario = wire_scenario(mode)
    out = sys.stdout.buffer

    def write(data):
        out.write(data)
        out.flush()

    def record(text):
        if trace:
            with open(trace, 'a', encoding='utf-8') as handle:
                handle.write(f'{text}\n')

    def emit(data):
        if mode != 'split':
            return write(data)
        # Separate writes split each UTF-8 sequence between its leading/continuation
        # bytes. Delays allow separate pipe reads without relying on chunk sizes.
        start = 0
        for i, byte in enumerate(data):
            if byte < 0xC0:
                continue
            write(data[start:i + 1])
            time.sleep(0.015)
            start = i + 1
        write(data[start:])

    page = 0
    for raw in sys.stdin.buffer:
        message = json.loads(raw)
        method = message.get('method')
        record(method)
        if method == 'initialize':
            emit(scenario['init'])
            continue
        if method == 'notifications/initialized':
            continue
        if method != 'tools/list' or page >= len(scenario['pages']):
            sys.stderr.write('fixture error: unexpected discovery request\n')
            sys.exit(1)
        if mode in ('partial', 'stubborn', 'line-hard'):
            if mode == 'stubborn':
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            record('partial-without-newline')
            write(b' ' * (8 * 1024 * 1024 + 1 if mode == 'line-hard' else 256))
            # A newline never arrives; the incoming cap must fail before a deadline.
            while True:
                time.sleep(10)
        if mode == 'empty':
            sys.stderr.write('inherited stderr excluded\n' * 200)
            sys.stderr.flush()
        emit(scenario['pages'][page])
        page += 1
    emit(scenario['tail'])  # Budget remains active after terminal response and stdin EOF.
    if mode == 'extra':
        write(b' ')


if __name__ == '__main__':
    _main()
