"""Bounded newline reads and backpressured writes for Linux pipes and files."""
from __future__ import annotations

import asyncio
import os
import sys

from .format import CassetteError, MAX_LINE


class FileReader:
    def __init__(self, stream):
        self.stream = stream

    async def readline(self):
        await asyncio.sleep(0)
        return self.stream.readline(MAX_LINE + 1)


async def stdin_reader():
    # epoll cannot watch regular files or /dev/null; they can be read directly.
    if sys.stdin.buffer.seekable():
        return FileReader(sys.stdin.buffer), None
    reader = asyncio.StreamReader(limit=MAX_LINE)
    protocol = asyncio.StreamReaderProtocol(reader)
    pipe = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, pipe)
    except BaseException:
        pipe.close()
        raise
    return reader, transport


async def read_line(reader):
    try:
        line = await reader.readline()
    except ValueError as exc:
        raise CassetteError("stdio line exceeds 1 MiB") from exc
    if len(line) > MAX_LINE:
        raise CassetteError("stdio line exceeds 1 MiB")
    return line


class WriteProtocol(asyncio.Protocol):
    def __init__(self):
        self.ready = asyncio.Event()
        self.ready.set()
        self.closed = asyncio.get_running_loop().create_future()
        self.error = None

    def pause_writing(self):
        self.ready.clear()

    def resume_writing(self):
        self.ready.set()

    def connection_lost(self, exc):
        self.error = exc
        self.ready.set()
        if not self.closed.done():
            self.closed.set_result(None)


class StdoutWriter:
    def __init__(self, transport=None, protocol=None):
        self.transport = transport
        self.protocol = protocol

    @classmethod
    async def open(cls):
        if sys.stdout.buffer.seekable():
            return cls()
        pipe = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        protocol = WriteProtocol()
        try:
            transport, _ = await asyncio.get_running_loop().connect_write_pipe(lambda: protocol, pipe)
        except BaseException:
            pipe.close()
            raise
        return cls(transport, protocol)

    async def write(self, data):
        if self.transport is None:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            if self.protocol.closed.done():
                raise BrokenPipeError("client output closed")
            self.transport.write(data)
            await self.protocol.ready.wait()
            if self.protocol.error is not None:
                raise BrokenPipeError("client output closed")

    async def close(self):
        if self.transport is not None:
            self.transport.close()
            try:
                await asyncio.wait_for(asyncio.shield(self.protocol.closed), 1.0)
            except asyncio.TimeoutError:
                self.transport.abort()
                raise CassetteError("client did not drain stdout during shutdown")
