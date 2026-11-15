"""Transparent, filtering stdio MCP proxy for one direct child server."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from .bounds import validate_json_tree
from .config import PROGRAM, InputError, is_visible
from .jsjson import MISSING, decode_utf8, get, is_safe_integer, parse, stringify, truthy, utf16_length
from .jsonio import error_code
from .pagination import PaginationError, PaginationTracker
from .requests import MAX_PENDING, PendingRequest, RequestTracker
from .slim import apply_config, extract_tools
from .stdio import ChildProcess, SignalHandlers, StdinReader, StreamClosedError, write_stderr, write_stdout
from . import timers

MAX_LINE_BYTES = 8 * 1024 * 1024


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


async def lines(stream: AsyncIterable[bytes], max_bytes: int = MAX_LINE_BYTES) -> AsyncIterator[bytes]:
    """Split a byte stream into newline-terminated lines of bounded length."""
    parts: list[bytes] = []
    length = 0
    async for chunk in stream:
        start = 0
        while (end := chunk.find(b'\n', start)) != -1:
            part = chunk[start:end + 1]
            length += len(part)
            if length > max_bytes:
                raise InputError('stdio line exceeds 8 MiB')
            parts.append(part)
            yield b''.join(parts)
            parts = []
            length = 0
            start = end + 1
        if start < len(chunk):
            part = chunk[start:]
            parts.append(part)
            length += len(part)
            if length > max_bytes:
                raise InputError('stdio line exceeds 8 MiB')
    if length:
        raise InputError('stdio message missing final newline')


def parse_message(line: bytes) -> dict[str, Any]:
    """Decode and validate one JSON-RPC 2.0 message; raises ``ValueError``."""
    m = parse(decode_utf8(line))
    if not isinstance(m, dict) or m.get('jsonrpc') != '2.0':
        raise ValueError('invalid JSON-RPC envelope (batches unsupported)')
    if 'id' in m:
        request_id = m['id']
        if not ((isinstance(request_id, str) and utf16_length(request_id) <= 1024) or is_safe_integer(request_id) or request_id is None):
            raise ValueError('invalid JSON-RPC id')
    if 'method' in m:
        method = m['method']
        if not isinstance(method, str) or not method or utf16_length(method) > 1024 or 'result' in m or 'error' in m:
            raise ValueError('invalid JSON-RPC request')
        if 'params' in m and not isinstance(m['params'], (dict, list)):
            raise ValueError('invalid JSON-RPC params')
    elif 'id' not in m or ('result' in m) == ('error' in m):
        raise ValueError('invalid JSON-RPC response')
    return m


async def write(stream: Any, data: bytes) -> None:
    """Write to the child's stdin, honouring backpressure."""
    await stream.write_all(data)


async def _chunks(reader: Any) -> AsyncIterator[bytes]:
    while chunk := await reader.read():
        yield chunk


def _signal_number(name: str) -> int:
    try:
        return int(signal.Signals[name])
    except KeyError:
        return 1


async def run_proxy(command: list[str], config: dict[str, Any], *, request_timeout_ms: int | None = None) -> int:
    """Run the proxy until the child closes; returns the process exit status."""
    def warn(message: str) -> None:
        write_stderr(f'{PROGRAM}: {message}\n')

    timeout_message = f'{PROGRAM} request timed out'
    state: dict[str, Any] = {'stopping': False, 'local_failure': False, 'forced_signal': None,
                             'shutdown_timer': None, 'stdout_closed': False}
    background: set[asyncio.Task[Any]] = set()
    child: ChildProcess | None = None

    def spawn_task(coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    def on_timeout(request_id: Any, request: PendingRequest) -> None:
        reported = False

        def job_failed() -> None:
            nonlocal reported
            if not reported:
                reported = True
                warn('timeout cancellation or client output failed')
                stop(signal.SIGTERM, True)

        try:
            send(_rpc_error(request_id, -32001, timeout_message))
        except Exception:
            job_failed()
        # MCP forbids cancellation of initialize and task-augmented requests via
        # this notification. A null JSON-RPC ID is not a valid MCP requestId either.
        if request_id is not None and request.method != 'initialize' and not request.task_augmented:
            notification = {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                            'params': {'requestId': request_id, 'reason': timeout_message}}

            async def drain_upstream() -> None:
                try:
                    await child.stdin.drain()
                except Exception:
                    job_failed()

            try:
                if child is None:
                    raise StreamClosedError('stdio output closed')
                # Queue the bytes now, in order with other upstream writes; only wait later.
                child.stdin.write(f'{stringify(notification)}\n'.encode())
            except Exception:
                job_failed()
            else:
                spawn_task(drain_upstream())

    def on_failure(error: Exception) -> None:
        warn(str(error))
        stop(signal.SIGTERM, True)

    requests = RequestTracker(request_timeout_ms, on_timeout, on_failure)
    pagination = PaginationTracker()

    def send(value: Any) -> None:
        """Serialize and write one line to stdout (synchronously, in order)."""
        data = value if isinstance(value, bytes) else f'{stringify(value)}\n'.encode()
        if len(data) > MAX_LINE_BYTES:
            raise InputError('stdio output line exceeds 8 MiB')
        if state['stdout_closed']:
            raise StreamClosedError('stdio output closed')
        try:
            write_stdout(data)
        except OSError:
            state['stdout_closed'] = True
            stop(signal.SIGTERM, True)
            raise

    def stop(sig: int | None = None, failed: bool = False) -> None:
        if failed:
            state['local_failure'] = True
        if state['stopping']:
            return
        state['stopping'] = True
        requests.stop_timers()
        if child is None:
            return
        if sig:
            child.kill(sig)
        else:
            child.stdin.end()

        def escalate() -> None:
            if sig:
                child.kill(signal.SIGKILL)
            else:
                child.kill(signal.SIGTERM)
                state['shutdown_timer'] = timers.set_timeout(lambda: child.kill(signal.SIGKILL), 1000)
                state['shutdown_timer'].unref()

        state['shutdown_timer'] = timers.set_timeout(escalate, 1000)
        state['shutdown_timer'].unref()

    def on_signal(name: str, sig: int) -> None:
        state['forced_signal'] = name
        stop(sig)

    handlers = SignalHandlers({signal.SIGTERM: lambda: on_signal('SIGTERM', signal.SIGTERM),
                                signal.SIGINT: lambda: on_signal('SIGINT', signal.SIGINT)})
    stdin = StdinReader()
    # No shell: arguments, paths, and tool data are never evaluated as code.
    try:
        child = await ChildProcess.spawn(command)
    except OSError as error:
        warn(f"cannot start server ({error_code(error) or 'spawn error'})")
        state['local_failure'] = True

    async def upstream() -> None:
        try:
            async for raw in lines(stdin.chunks()):
                if state['stopping']:
                    break
                try:
                    m = parse_message(raw)
                except Exception:
                    send(_rpc_error(None, -32700, 'invalid client JSON-RPC message'))
                    warn('rejected invalid client line (content omitted)')
                    continue
                method = m.get('method', MISSING)
                has_id = 'id' in m
                is_request = isinstance(method, str) and has_id
                if is_request and requests.has_pending(m['id']):
                    warn('duplicate in-flight client id; closing transport')
                    stop(signal.SIGTERM, True)
                    break
                if is_request and requests.has_cancelled(m['id']):
                    warn('unresolved cancelled client id reused; closing transport')
                    stop(signal.SIGTERM, True)
                    break
                params = m.get('params', MISSING)
                if method == 'tools/call':
                    name = get(params, 'name')
                    if not isinstance(name, str) or not name or utf16_length(name) > 1024:
                        if has_id:
                            send(_rpc_error(m['id'], -32602, 'tools/call requires params.name (1 to 1024 code units)'))
                        continue
                    if not is_visible(name, config):
                        if has_id:
                            send(_rpc_error(m['id'], -32602, f'tool hidden by {PROGRAM}'))
                        continue
                if is_request:
                    if len(requests.pending) >= MAX_PENDING:
                        send(_rpc_error(m['id'], -32000, f'{PROGRAM} pending request limit reached'))
                        continue
                    page = None
                    if method == 'tools/list':
                        try:
                            page = pagination.begin(m)
                        except Exception as error:
                            code = error.code if isinstance(error, PaginationError) else -32602
                            send(_rpc_error(m['id'], code, str(error) if isinstance(error, InputError) else 'invalid tools/list params'))
                            continue
                    requests.add(m['id'], PendingRequest(method, page=page, task_augmented=get(params, 'task') is not MISSING))
                if not has_id and method == 'notifications/cancelled':
                    request_id = get(params, 'requestId')
                    if request_id is not MISSING and requests.has_pending(request_id):
                        # Move and disarm the timer before writing: backpressure may yield.
                        requests.cancel(request_id)
                if child is None:
                    raise StreamClosedError('stdio output closed')
                await write(child.stdin, raw)
            stop()
        except Exception as error:
            warn(str(error) if isinstance(error, InputError) else 'client input or server stdin failed')
            stop(signal.SIGTERM, True)

    async def downstream() -> None:
        if child is None:
            return
        try:
            async for raw in lines(_chunks(child.stdout)):
                try:
                    m = parse_message(raw)
                except Exception:
                    warn('invalid server stdout line diverted to stderr (content omitted)')
                    continue
                if 'id' not in m and m.get('method') == 'notifications/tools/list_changed':
                    pagination.clear()
                if 'method' not in m and 'id' in m:
                    request = requests.take(m['id'])
                    # The local timeout already ended this request. Discard every late
                    # terminal payload (including lists) before forwarding or pagination.
                    if request is not None and request.timed_out:
                        continue
                    if request is not None and request.method == 'tools/list' and 'result' in m:
                        await _forward_list(m, raw, request)
                        continue
                send(raw)
        except Exception as error:
            warn(str(error) if isinstance(error, InputError) else 'server stdout or client output failed')
            stop(signal.SIGTERM, True)
        finally:
            if not child.stdout.finished.is_set():
                child.stdout.destroy()

    async def _forward_list(m: dict[str, Any], raw: bytes, request: PendingRequest) -> None:
        result = m['result']
        # Newer MCP non-complete results (input_required/not_modified) have
        # no tools. Preserve them just like all other non-list messages.
        result_type = get(result, 'resultType')
        if truthy(result_type) and result_type != 'complete' and 'tools' not in result:
            send(raw)
            return
        try:
            validate_json_tree(m)
            if not isinstance(result, dict):
                raise InputError('tools/list result: expected an object with tools')
            tools = extract_tools(result)
            output = f"{stringify({**m, 'result': {**result, 'tools': apply_config(tools, config, warn)}})}\n".encode()
            if len(output) > MAX_LINE_BYTES:
                raise InputError('stdio output line exceeds 8 MiB')
            pagination.observe(request.page, result)
            send(output)
        except Exception as error:
            warn(f"cannot slim tools/list: {str(error) if isinstance(error, InputError) else 'invalid result'}")
            paging = isinstance(error, PaginationError)
            send(_rpc_error(m['id'], error.code if paging else -32603,
                            str(error) if paging else f'{PROGRAM} rejected invalid tools/list result'))

    upstream_task = asyncio.ensure_future(upstream())
    downstream_task = asyncio.ensure_future(downstream())
    if child is not None:
        code, sig_name = await child.closed()
    else:
        code, sig_name = None, None
    timers.clear_timeout(state['shutdown_timer'])
    requests.stop_timers()
    # close occurs after the child's stdio closes; finish all downstream messages
    # before failing remaining requests, including an otherwise clean early exit.
    await downstream_task
    was_stopping = state['stopping']
    state['stopping'] = True
    stdin.destroy()
    await upstream_task
    unfinished = len(requests.pending)
    for request in list(requests.pending.values()):
        try:
            send(_rpc_error(request.id, -32000, f'{PROGRAM} upstream server exited before responding'))
        except Exception:
            break
    # Like Node.js, never wait for an unacknowledged cancellation write.
    for task in list(background):
        task.cancel()
    if child is not None:
        child.close_pipes()
    handlers.remove()
    if unfinished:
        warn(f'server exited with {unfinished} pending request(s)')
    if state['forced_signal']:
        return 128 + _signal_number(state['forced_signal'])
    if code is not None and code > 0:
        return code
    if sig_name:
        return 128 + _signal_number(sig_name)
    return 1 if state['local_failure'] or unfinished or not was_stopping else 0
