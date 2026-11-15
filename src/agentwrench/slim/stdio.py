"""Asynchronous stdio plumbing: child processes, our own stdin and stdout/stderr.

The child is started with :class:`subprocess.Popen` (no shell) and its pipes are
attached to the running asyncio (selector) event loop. A helper thread waits for
the exit status. Our own stdin is read by a daemon thread with bounded
read-ahead, so regular files, pipes and terminals all work and backpressure is
preserved. Writes to our stdout are synchronous, like Node.js on Linux pipes.
"""

from __future__ import annotations

import asyncio
import collections
import os
import select
import signal
import subprocess
import sys
import threading
from typing import Any

CHUNK_SIZE = 65536


class StreamClosedError(Exception):
    """A write to a destroyed or ended stream."""


class _ReadPipe(asyncio.Protocol):
    """Chunked reader with bounded buffering (pauses the transport when full)."""

    HIGH_WATER = 1024 * 1024

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._chunks: collections.deque[bytes] = collections.deque()
        self._buffered = 0
        self._eof = False
        self._error: BaseException | None = None
        self._waiter: asyncio.Future[None] | None = None
        self._transport: asyncio.ReadTransport | None = None
        self._paused = False
        self.finished = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def data_received(self, data: bytes) -> None:
        self._chunks.append(data)
        self._buffered += len(data)
        if self._buffered > self.HIGH_WATER and not self._paused and self._transport is not None:
            self._paused = True
            self._transport.pause_reading()
        self._wake()

    def eof_received(self) -> bool:
        self._eof = True
        self._wake()
        return False

    def connection_lost(self, exc: BaseException | None) -> None:
        self._eof = True
        if exc is not None and self._error is None:
            self._error = exc
        self._wake()

    def _wake(self) -> None:
        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def read(self) -> bytes:
        """Next chunk, or ``b''`` at end of stream."""
        while not self._chunks:
            if self._eof:
                self.finished.set()
                if self._error is not None:
                    raise self._error
                return b''
            self._waiter = self._loop.create_future()
            await self._waiter
        chunk = self._chunks.popleft()
        self._buffered -= len(chunk)
        if self._paused and self._buffered < self.HIGH_WATER // 4 and self._transport is not None:
            self._paused = False
            self._transport.resume_reading()
        return chunk

    def destroy(self) -> None:
        """Stop reading and close the pipe (``stream.destroy()``)."""
        self._chunks.clear()
        self._eof = True
        if self._transport is not None:
            self._transport.close()
        self.finished.set()
        self._wake()


class _WritePipe(asyncio.Protocol):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._paused = False
        self._waiters: list[asyncio.Future[None]] = []
        self.lost = False

    def pause_writing(self) -> None:
        self._paused = True

    def resume_writing(self) -> None:
        self._paused = False
        self._wake(None)

    def connection_lost(self, exc: BaseException | None) -> None:
        self.lost = True
        self._wake(exc or BrokenPipeError('pipe closed'))

    def _wake(self, exc: BaseException | None) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                if exc is None:
                    waiter.set_result(None)
                else:
                    waiter.set_exception(exc)

    async def drain(self) -> None:
        if self.lost:
            raise BrokenPipeError('pipe closed')
        if not self._paused:
            return
        waiter = self._loop.create_future()
        self._waiters.append(waiter)
        await waiter


class ChildStdin:
    """Writable side of the child's stdin pipe."""

    def __init__(self, transport: asyncio.WriteTransport, protocol: _WritePipe) -> None:
        self._transport = transport
        self._protocol = protocol
        self.ended = False

    @property
    def destroyed(self) -> bool:
        return self._protocol.lost or self._transport.is_closing()

    def write(self, data: bytes) -> None:
        if self.destroyed or self.ended:
            raise StreamClosedError('stdio output closed')
        self._transport.write(data)

    async def drain(self) -> None:
        await self._protocol.drain()

    async def write_all(self, data: bytes) -> None:
        """Write, then wait while the transport is above its high-water mark."""
        self.write(data)
        await self.drain()

    def end(self) -> None:
        """Close after flushing buffered data (``stream.end()``)."""
        if not self.ended:
            self.ended = True
            self._transport.close()

    def abort(self) -> None:
        self.ended = True
        if not self._protocol.lost:
            self._transport.abort()


class ChildProcess:
    """A spawned direct child with asynchronous stdin/stdout and inherited stderr."""

    def __init__(self, proc: subprocess.Popen[bytes], loop: asyncio.AbstractEventLoop) -> None:
        self.proc = proc
        self.pid = proc.pid
        self._loop = loop
        self.exited: asyncio.Future[int] = loop.create_future()
        self.stdin: ChildStdin
        self.stdout: _ReadPipe

    @classmethod
    async def spawn(cls, command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
                    stdin_high_water: int = 16384) -> ChildProcess:
        """Start ``command`` without a shell; raises ``OSError`` if it cannot start."""
        loop = asyncio.get_running_loop()
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                                cwd=cwd, env=env, close_fds=True)
        child = cls(proc, loop)
        threading.Thread(target=child._wait_for_exit, name=f'wait-{proc.pid}', daemon=True).start()
        write_transport, write_protocol = await loop.connect_write_pipe(lambda: _WritePipe(loop), proc.stdin)
        write_transport.set_write_buffer_limits(high=stdin_high_water, low=0)
        child.stdin = ChildStdin(write_transport, write_protocol)
        _, child.stdout = await loop.connect_read_pipe(lambda: _ReadPipe(loop), proc.stdout)
        return child

    def _wait_for_exit(self) -> None:
        code = self.proc.wait()
        try:
            self._loop.call_soon_threadsafe(self._set_exited, code)
        except RuntimeError:  # loop already closed
            pass

    def _set_exited(self, code: int) -> None:
        if not self.exited.done():
            self.exited.set_result(code)

    def kill(self, sig: int = signal.SIGTERM) -> None:
        """Send a signal unless the child has already been reaped."""
        if self.exited.done():
            return
        try:
            self.proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    async def closed(self) -> tuple[int | None, str | None]:
        """Wait for exit and the end of stdout; returns ``(code, signal name)``."""
        code = await self.exited
        await self.stdout.finished.wait()
        if code < 0:
            try:
                return None, signal.Signals(-code).name
            except ValueError:
                return None, f'SIG{-code}'
        return code, None

    def close_pipes(self) -> None:
        self.stdin.abort()
        self.stdout.destroy()


class StdinReader:
    """Our own stdin as chunks, read by a daemon thread with bounded read-ahead."""

    def __init__(self, fd: int | None = None, read_ahead: int = 16) -> None:
        self._loop = asyncio.get_running_loop()
        self._fd = sys.stdin.fileno() if fd is None else fd
        self._items: collections.deque[bytes | BaseException | None] = collections.deque()
        self._slots = threading.Semaphore(read_ahead)
        self._waiter: asyncio.Future[None] | None = None
        self._destroyed = False
        self._ended = False
        threading.Thread(target=self._run, name='stdin-reader', daemon=True).start()

    def _run(self) -> None:
        while True:
            self._slots.acquire()
            if self._destroyed:
                return
            item: bytes | BaseException | None
            try:
                item = os.read(self._fd, CHUNK_SIZE) or None
            except OSError as error:
                item = error
            try:
                self._loop.call_soon_threadsafe(self._push, item)
            except RuntimeError:
                return
            if not isinstance(item, bytes):
                return

    def _push(self, item: bytes | BaseException | None) -> None:
        self._items.append(item)
        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def read(self) -> bytes:
        """Next chunk or ``b''`` at EOF; raises once destroyed before EOF."""
        while True:
            if self._destroyed:
                raise StreamClosedError('premature close')
            if self._items:
                item = self._items.popleft()
                if isinstance(item, BaseException):
                    raise item
                if item is None:
                    self._ended = True
                    return b''
                self._slots.release()
                return item
            if self._ended:
                return b''
            self._waiter = self._loop.create_future()
            await self._waiter

    async def chunks(self):  # type: ignore[no-untyped-def]
        while chunk := await self.read():
            yield chunk

    def destroy(self) -> None:
        if self._ended:
            return
        self._destroyed = True
        self._slots.release()
        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


class SignalHandlers:
    """``process.on('SIGTERM'|'SIGINT')`` with exact restoration on removal."""

    def __init__(self, handlers: dict[int, Any]) -> None:
        self._loop = asyncio.get_running_loop()
        self._previous: dict[int, Any] = {}
        for signum, handler in handlers.items():
            try:
                previous = signal.getsignal(signum)
                self._loop.add_signal_handler(signum, handler)
                self._previous[signum] = previous
            except (RuntimeError, ValueError, NotImplementedError):  # not the main thread
                pass

    def remove(self) -> None:
        for signum, previous in self._previous.items():
            self._loop.remove_signal_handler(signum)
            if previous is not None:
                signal.signal(signum, previous)
        self._previous.clear()


def _redirect_broken_stdout() -> None:
    """After EPIPE, discard further output so interpreter shutdown stays quiet."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def write_stdout(data: bytes) -> None:
    """Synchronously write all bytes to stdout; raises ``BrokenPipeError`` on EPIPE."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, ValueError, OSError):  # e.g. an in-memory capture object
        sys.stdout.write(data.decode('utf-8', 'replace'))
        sys.stdout.flush()
        return
    view = memoryview(data)
    try:
        sys.stdout.flush()
        while view:
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                select.select([], [fd], [])
                continue
            view = view[written:]
    except BrokenPipeError:
        _redirect_broken_stdout()
        raise


def write_stderr(text: str) -> None:
    try:
        stream = getattr(sys.stderr, 'buffer', None)
        if stream is None:
            sys.stderr.write(text)
        else:
            stream.write(text.encode('utf-8', 'replace'))
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
