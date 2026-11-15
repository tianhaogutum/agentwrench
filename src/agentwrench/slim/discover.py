"""Opt-in discovery: collect one complete, slimmed tools/list catalogue over stdio."""

from __future__ import annotations

import asyncio
import math
import os
import re
import signal
import stat
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from . import timers
from .bounds import validate_json_tree
from .config import PROGRAM, VERSION, InputError
from .jsjson import MISSING, id_key, is_integer, stringify
from .jsonio import MAX_FILE_BYTES
from .pagination import MAX_PAGINATION_ENTRIES, PaginationTracker
from .proxy import MAX_LINE_BYTES, lines, parse_message
from .requests import MAX_TIMEOUT_MS, PendingRequest, RequestTracker, validate_request_timeout
from .slim import apply_config, extract_tools
from .stdio import ChildProcess, SignalHandlers

PROTOCOL_VERSION = '2025-11-25'
CLIENT_INFO = {'name': PROGRAM.replace(' ', '-'), 'version': VERSION}
MAX_SOURCE_TOOLS = 10000
DEFAULT_REQUEST_TIMEOUT_MS = 30000
DEFAULT_SHUTDOWN_TIMEOUT_MS = 1000
_PORTABLE_NAME = re.compile('[A-Za-z_][A-Za-z0-9_]*')
PROTOCOL_VERSION_PATTERN = re.compile('[0-9]{4}-[0-9]{2}-[0-9]{2}')

ListPage = Callable[[dict[str, Any] | None], Awaitable[Any]]


class DiscoveryError(Exception):
    @property
    def message(self) -> str:
        return str(self)


def validate_server_env(value: Any) -> dict[str, str] | None:
    """``NAME=VALUE`` overlay entries; ``None`` means omitted."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise InputError('--server-env: expected an array of NAME=VALUE strings')
    overlay: dict[str, str] = {}
    for entry in value:
        equals = entry.find('=') if isinstance(entry, str) else -1
        if equals < 1 or not _PORTABLE_NAME.fullmatch(entry[:equals]):
            raise InputError('--server-env: expected NAME=VALUE with a portable environment name')
        name, contents = entry[:equals], entry[equals + 1:]
        if '\0' in contents:
            raise InputError('--server-env: value must not contain NUL')
        if name in overlay:
            raise InputError('--server-env: duplicate name')
        overlay[name] = contents
    return overlay


def validate_server_unset_env(value: Any, overlay: dict[str, str] | None = None) -> list[str] | None:
    """Names to remove from the child environment; ``None`` means omitted."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise InputError('--server-unset-env: expected an array of environment names')
    names: list[str] = []
    for name in value:
        if not isinstance(name, str) or not _PORTABLE_NAME.fullmatch(name):
            raise InputError('--server-unset-env: expected a portable environment name')
        if name in names:
            raise InputError('--server-unset-env: duplicate name')
        if overlay is not None and name in overlay:
            raise InputError('--server-unset-env: name conflicts with --server-env')
        names.append(name)
    return names


def validate_server_cwd(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or '\0' in value:
        raise InputError('--server-cwd: expected a nonempty directory path without NUL')
    # Anchor relative paths without collapsing symlink/.. or trailing separators.
    # Only the child changes directory; config and output keep the caller's cwd.
    cwd = value if value.startswith('/') else f'{os.getcwd()}/{value}'
    try:
        info = os.stat(cwd)
    except (OSError, ValueError):
        raise InputError('--server-cwd: expected an accessible existing directory') from None
    if not stat.S_ISDIR(info.st_mode):
        raise InputError('--server-cwd: expected an accessible existing directory')
    return cwd


def _bounded(value: Any, default: Any, low: int, high: int, message: str) -> Any:
    if value is None:
        value = default
    if value is None:
        return None
    if not is_integer(value) or value < low or value > high:
        raise InputError(message)
    return int(value)


def validate_shutdown_timeout(value: Any = None) -> int:
    return _bounded(value, DEFAULT_SHUTDOWN_TIMEOUT_MS, 1, 30000, '--shutdown-timeout-ms: expected an integer from 1 to 30000')


def validate_discovery_timeout(value: Any) -> int | None:
    return _bounded(value, None, 1, MAX_TIMEOUT_MS, f'--discovery-timeout-ms: expected an integer from 1 to {MAX_TIMEOUT_MS}')


def validate_max_pages(value: Any = None) -> int:
    return _bounded(value, MAX_PAGINATION_ENTRIES, 1, MAX_PAGINATION_ENTRIES, f'--max-pages: expected an integer from 1 to {MAX_PAGINATION_ENTRIES}')


def validate_max_tools(value: Any = None) -> int:
    return _bounded(value, MAX_SOURCE_TOOLS, 1, MAX_SOURCE_TOOLS, f'--max-tools: expected an integer from 1 to {MAX_SOURCE_TOOLS}')


def validate_max_output_bytes(value: Any = None) -> int:
    return _bounded(value, MAX_FILE_BYTES, 13, MAX_FILE_BYTES, f'--max-output-bytes: expected an integer from 13 to {MAX_FILE_BYTES}')


def validate_max_wire_bytes(value: Any) -> int | None:
    return _bounded(value, None, 1, 2147483647, '--max-wire-bytes: expected an integer from 1 to 2147483647')


class DiscoveryDeadline:
    """One monotonic budget for the whole walk. Checks also cover synchronous work
    and continuations that can run before an overdue timer callback."""

    def __init__(self, timeout_ms: int | None, on_expiry: Callable[[], Any]) -> None:
        validate_discovery_timeout(timeout_ms)
        self.on_expiry = on_expiry
        self.expired = False
        self.timer: Any = None
        self.expires_at = math.inf if timeout_ms is None else timers.now() + timeout_ms
        if timeout_ms is not None:
            self.timer = timers.set_timeout(self.expire, timeout_ms)
            self.timer.unref()

    def expire(self) -> None:
        if self.expired:
            return
        self.expired = True
        self.stop()
        self.on_expiry()

    def check(self) -> None:
        if timers.now() >= self.expires_at:
            self.expire()

    def stop(self) -> None:
        timers.clear_timeout(self.timer)


async def collect_catalogue(list_page: ListPage, config: dict[str, Any], *, max_pages: int | None = None,
                            max_tools: int | None = None, max_output_bytes: int | None = None) -> str:
    """Keep the complete snapshot private until every page and aggregate bound passes.
    ``list_page`` is called sequentially with ``None`` or ``{'cursor': ...}``."""
    max_pages = validate_max_pages(max_pages)
    max_tools = validate_max_tools(max_tools)
    max_output_bytes = validate_max_output_bytes(max_output_bytes)
    pagination = PaginationTracker()
    names: set[str] = set()
    tools: list[dict[str, Any]] = []
    cursor: Any = MISSING
    size = len(b'{"tools":[]}\n')
    for _ in range(max_pages):
        params = None if cursor is MISSING else {'cursor': cursor}
        descriptor = pagination.begin({} if params is None else {'params': params})
        result = await list_page(params)
        if not isinstance(result, dict) or ('resultType' in result and result['resultType'] != 'complete'):
            raise DiscoveryError('discovery requires a complete tools/list result')
        batch = extract_tools(result)
        pagination.observe(descriptor, result)
        for tool in batch:
            if tool['name'] in names:
                raise DiscoveryError('discovery catalogue contains duplicate tool names')
            if len(names) >= max_tools:
                raise DiscoveryError(f'discovery catalogue exceeds {max_tools} source tools')
            names.add(tool['name'])
        for tool in apply_config(batch, config):
            size += len(stringify(tool).encode('utf-8')) + (1 if tools else 0)
            if size > max_output_bytes:
                raise DiscoveryError('discovery catalogue exceeds 16 MiB' if max_output_bytes == MAX_FILE_BYTES
                                     else f'discovery catalogue exceeds {max_output_bytes} output bytes')
            tools.append(tool)
        if 'nextCursor' not in result:
            return f"{stringify({'tools': tools})}\n"
        cursor = result['nextCursor']
    raise DiscoveryError(f'discovery exceeds {max_pages} page limit')


class _State:
    failure: DiscoveryError | None = None
    finishing = False
    aborting = False
    child_closed = False
    shutdown_deadline: DiscoveryDeadline | None = None
    kill_timer: Any = None
    active: tuple[int, asyncio.Future[Any]] | None = None
    reader_gate: asyncio.Future[None] | None = None
    next_id = 0


def validate_protocol_versions(value: Any) -> list[str] | None:
    """Extra ``YYYY-MM-DD`` MCP protocol revisions to accept; ``None`` means omitted."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise InputError('--accept-protocol-version: expected an array of YYYY-MM-DD strings')
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str) or not PROTOCOL_VERSION_PATTERN.fullmatch(entry):
            raise InputError('--accept-protocol-version: expected an MCP protocol revision such as 2025-06-18')
        if entry in seen:
            raise InputError('--accept-protocol-version: duplicate version')
        seen.add(entry)
    return list(value)


def _initialize_error(version: Any, accepted: frozenset[str]) -> str:
    listed = ', '.join(sorted(accepted)) or 'none'
    message = f'discovery requires tools capability and protocol {listed}'
    # Only a well-formed revision is echoed; other remote content stays out of diagnostics.
    if isinstance(version, str) and PROTOCOL_VERSION_PATTERN.fullmatch(version) and version not in accepted:
        message += f' (server negotiated {version}; to accept it, add --accept-protocol-version {version})'
    return message


async def run_discover(command: list[str], config: dict[str, Any], *, server_env: list[str] | None = None,
                       server_unset_env: list[str] | None = None, server_cwd: str | None = None,
                       request_timeout_ms: int | None = None, discovery_timeout_ms: int | None = None,
                       shutdown_timeout_ms: int | None = None, max_pages: int | None = None,
                       max_tools: int | None = None, max_output_bytes: int | None = None,
                       max_wire_bytes: int | None = None, protocol_versions: Iterable[str] | None = None,
                       server_stderr: Any = None,
                       on_protocol_version: Callable[[str], Any] | None = None) -> str:
    """Start ``command``, walk every tools/list page and return the catalogue JSON line.

    Options left as ``None`` use their defaults (30 s per request, no overall
    deadline, 1 s shutdown grace, 10,000 pages/tools, 16 MiB, no wire budget).
    ``protocol_versions`` lists the negotiated protocol versions to accept
    (default: only :data:`PROTOCOL_VERSION`); ``server_stderr`` is passed to the
    child process (default: inherit ours, e.g. ``subprocess.DEVNULL`` to discard).
    ``on_protocol_version`` is called with the accepted negotiated version.
    """
    accepted = frozenset((PROTOCOL_VERSION,) if protocol_versions is None else protocol_versions)
    request_timeout_ms = validate_request_timeout(DEFAULT_REQUEST_TIMEOUT_MS if request_timeout_ms is None else request_timeout_ms)
    validate_discovery_timeout(discovery_timeout_ms)
    shutdown_timeout_ms = validate_shutdown_timeout(shutdown_timeout_ms)
    validate_max_pages(max_pages)
    validate_max_tools(max_tools)
    validate_max_output_bytes(max_output_bytes)
    validate_max_wire_bytes(max_wire_bytes)
    overlay = validate_server_env(server_env)
    unset = validate_server_unset_env(server_unset_env, overlay)
    cwd = validate_server_cwd(server_cwd)
    env: dict[str, str] | None = None
    if overlay is not None or unset is not None:
        env = {**os.environ, **(overlay or {})}
        for name in unset or []:
            env.pop(name, None)

    loop = asyncio.get_running_loop()
    state = _State()
    failed: asyncio.Future[None] = loop.create_future()
    # A stream can fail between requests; keep the rejection observed until raced.
    failed.add_done_callback(lambda future: future.cancelled() or future.exception())
    deadline: DiscoveryDeadline | None = None
    child: ChildProcess

    def release_reader() -> None:
        gate, state.reader_gate = state.reader_gate, None
        if gate is not None and not gate.done():
            gate.set_result(None)

    def fail(message: str) -> None:
        release_reader()
        if state.failure is None:
            state.failure = DiscoveryError(message)
            if deadline is not None:
                deadline.stop()
            failed.set_exception(state.failure)
            if state.finishing:
                abort_shutdown()

    def abort_shutdown() -> None:
        if state.child_closed or state.aborting:
            return
        state.aborting = True
        if state.shutdown_deadline is not None:
            state.shutdown_deadline.stop()
        child.kill(signal.SIGTERM)
        state.kill_timer = timers.set_timeout(lambda: child.kill(signal.SIGKILL), 1000)

    deadline = DiscoveryDeadline(discovery_timeout_ms, lambda: fail('discovery duration limit exceeded'))
    try:
        child = await ChildProcess.spawn(command, cwd=cwd, env=env, stdin_high_water=0, stderr=server_stderr)
    except OSError:
        fail('cannot start server for discovery')
        deadline.check()
        deadline.stop()
        raise state.failure from None
    except BaseException:
        deadline.stop()
        raise
    requests = RequestTracker(request_timeout_ms, lambda *_: fail('discovery request timed out'),
                              lambda *_: fail('discovery request tracking failed'))
    closed: asyncio.Future[None] = loop.create_future()

    async def watch_close() -> None:
        code, sig_name = await child.closed()
        state.child_closed = True
        # Check at observed close, before clearing the grace timer. Exact equality
        # expires even when the event loop has not delivered its callback yet.
        if state.shutdown_deadline is not None:
            state.shutdown_deadline.check()
            state.shutdown_deadline.stop()
        if not state.finishing:
            fail('discovery server exited before completion')
        if code != 0 or sig_name:
            fail('discovery server exited unsuccessfully')
        closed.set_result(None)

    def interrupt(name: str, sig: int) -> None:
        fail(f'discovery interrupted by {name}')
        child.kill(sig)

    close_task = asyncio.ensure_future(watch_close())
    handlers = SignalHandlers({signal.SIGTERM: lambda: interrupt('SIGTERM', signal.SIGTERM),
                               signal.SIGINT: lambda: interrupt('SIGINT', signal.SIGINT)})

    async def incoming():  # type: ignore[no-untyped-def]
        received = 0
        while chunk := await child.stdout.read():
            if max_wire_bytes is not None:
                received += len(chunk)
                if received > max_wire_bytes:
                    # Fail before yielding any line from this chunk, including when a
                    # response and overflow arrive together. Sends check this same failure.
                    fail(f'discovery exceeds {max_wire_bytes} incoming wire bytes')
                    raise state.failure
            yield chunk

    async def read_loop() -> None:
        try:
            async for raw in lines(incoming()):
                try:
                    message = parse_message(raw)
                except Exception:
                    raise DiscoveryError('discovery received invalid JSON-RPC stdout (content omitted)') from None
                if 'method' in message:
                    if 'id' in message:
                        raise DiscoveryError('discovery does not support server-initiated requests')
                    if message['method'] == 'notifications/tools/list_changed':
                        raise DiscoveryError('discovery catalogue changed during collection')
                    continue
                active = state.active
                if not requests.has_pending(message['id']) or active is None or id_key(message['id']) != id_key(active[0]):
                    raise DiscoveryError('discovery received an unexpected response ID')
                state.active = None
                if not active[1].done():
                    # Like Node.js microtasks, let the requester consume this reply and
                    # run until its next suspension before handling further lines.
                    state.reader_gate = loop.create_future()
                    active[1].set_result(message)
                    await state.reader_gate
            if not state.finishing:
                fail('discovery server stdout closed before completion')
        except Exception as error:
            fail(str(error) if isinstance(error, (DiscoveryError, InputError)) else 'discovery server stdout failed')
        finally:
            if not child.stdout.finished.is_set():
                child.stdout.destroy()

    reader = asyncio.ensure_future(read_loop())

    def check_deadline() -> None:
        deadline.check()
        if state.failure is not None:
            raise state.failure

    async def race(awaitable: Awaitable[Any]) -> Any:
        """``Promise.race([awaitable, failed])``; the awaitable wins ties."""
        task = asyncio.ensure_future(awaitable)
        release_reader()
        await asyncio.wait({task, failed}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            return task.result()
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise state.failure

    async def send(message: dict[str, Any]) -> None:
        check_deadline()
        data = f'{stringify(message)}\n'.encode()
        if len(data) > MAX_LINE_BYTES:
            raise DiscoveryError('discovery request exceeds 8 MiB wire limit')
        try:
            child.stdin.write(data)
            await child.stdin.drain()
        except Exception:
            raise DiscoveryError('discovery server stdin failed') from None

    async def request(method: str, params: dict[str, Any] | None = None) -> Any:
        check_deadline()
        state.next_id += 1
        request_id = state.next_id
        response: asyncio.Future[Any] = loop.create_future()
        state.active = (request_id, response)
        requests.add(request_id, PendingRequest(method))
        message: dict[str, Any] = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
        if params is not None:
            message['params'] = params

        async def write_and_reply() -> Any:
            await send(message)
            return await response

        reply = await race(write_and_reply())
        # A server can reply before consuming a backpressured request. Keep the
        # total deadline armed until both the write and matching reply complete.
        requests.take(request_id)
        if 'error' in reply:
            raise DiscoveryError(f'discovery {method} returned an upstream error (content omitted)')
        try:
            validate_json_tree(reply['result'])
        except Exception:
            raise DiscoveryError(f'discovery {method} returned invalid JSON data') from None
        return reply['result']

    snapshot: str | None = None
    try:
        init = await request('initialize', {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {}, 'clientInfo': dict(CLIENT_INFO)})
        capabilities = init.get('capabilities') if isinstance(init, dict) else None
        server_info = init.get('serverInfo') if isinstance(init, dict) else None
        version = init.get('protocolVersion') if isinstance(init, dict) else None
        if (not isinstance(init, dict) or not isinstance(version, str) or version not in accepted
                or not isinstance(capabilities, dict) or not isinstance(capabilities.get('tools'), dict)
                or not isinstance(server_info, dict) or not isinstance(server_info.get('name'), str)
                or not isinstance(server_info.get('version'), str)):
            raise DiscoveryError(_initialize_error(version, accepted))
        if on_protocol_version is not None:
            on_protocol_version(version)
        await race(send({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        snapshot = await collect_catalogue(lambda params: request('tools/list', params), config,
                                           max_pages=max_pages, max_tools=max_tools, max_output_bytes=max_output_bytes)
    except Exception as error:
        fail(str(error) if isinstance(error, (DiscoveryError, InputError)) else 'discovery failed')
    finally:
        release_reader()
        state.finishing = True
        requests.stop_timers()
        # Only successful collection starts EOF grace. Any failure during that
        # grace switches immediately to bounded signal cleanup, independently of
        # the selected success grace and the still-active overall deadline.
        if state.failure is not None:
            abort_shutdown()
        else:
            state.shutdown_deadline = DiscoveryDeadline(shutdown_timeout_ms,
                                                        lambda: fail('discovery server shutdown exceeded grace period'))
            child.stdin.end()
        await closed
        if state.shutdown_deadline is not None:
            state.shutdown_deadline.stop()
        timers.clear_timeout(state.kill_timer)
        await reader
        await close_task
        child.close_pipes()
        handlers.remove()
        deadline.check()
        deadline.stop()
    if state.failure is not None:
        raise state.failure
    return snapshot
