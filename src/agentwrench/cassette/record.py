"""Bounded full-duplex stdio proxy with graceful EOF/signal finalization."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time

from .format import (CassetteError, MAX_CASSETTE, MAX_EVENTS, MAX_LINE,
                     canonical, cassette_bytes, id_key, parse_line, pointer_tokens, redact, save_cassette)
from .stdio import StdoutWriter, read_line, stdin_reader


MAX_REASONS = 8
MAX_REASON_JSON_BYTES = 256


def bounded_reasons(reasons):
    result = []
    for reason in reasons:
        if len(result) == MAX_REASONS:
            result[-1] = "additional recording failures omitted"
            break
        if len(json.dumps(reason, ensure_ascii=False).encode("utf-8")) > MAX_REASON_JSON_BYTES:
            reason = "recording failure (diagnostic omitted)"
        result.append(reason)
    return result


class Recorder:
    def __init__(self, paths):
        for path in paths:
            pointer_tokens(path)
        self.paths = list(paths)
        self.events = []
        self.pending = {"client": set(), "server": set()}
        self.started = time.monotonic()
        self.size = 0
        # Linux subprocess statuses fit signed 32 bits. Reserve the largest
        # bounded failure envelope, using the actual configured paths. This is
        # computed once, not by reserializing the growing cassette per event.
        self.metadata_size = max(
            len(cassette_bytes(self.document(-2147483648, []))),
            len(cassette_bytes(self.document(-2147483648,
                ["x" * (MAX_REASON_JSON_BYTES - 2)] * MAX_REASONS))))
        if self.metadata_size > MAX_CASSETTE:
            raise CassetteError("recording metadata exceeds 64 MiB finalization budget")

    def append(self, direction, msg):
        # Validate pairing before storing: a broken stream must never look complete.
        if "id" in msg:
            key = id_key(msg["id"])
            if "method" in msg:
                if key in self.pending[direction]:
                    raise CassetteError("duplicate in-flight request id")
            else:
                other = "server" if direction == "client" else "client"
                if key not in self.pending[other]:
                    raise CassetteError("response has no matching in-flight request")
        stored = redact(msg, self.paths)
        # The cassette validator bounds compact UTF-8 plus LF, not the live
        # line: redaction and numeric serialization can expand a valid input.
        if len(canonical(stored).encode("utf-8")) + 1 > MAX_LINE:
            raise CassetteError("recorded message exceeds 1 MiB after redaction (including newline)")
        event = {"seq": len(self.events), "ms": round((time.monotonic() - self.started) * 1000, 3),
                 "direction": direction, "message": stored}
        # Each array element has four extra spaces per line and a comma/LF.
        # Changing [] to a nonempty events array adds two more bytes overall.
        serialized = json.dumps(event, ensure_ascii=False, indent=2)
        size = len(serialized.encode("utf-8")) + 4 * (serialized.count("\n") + 1) + 2
        if len(self.events) >= MAX_EVENTS or self.metadata_size + self.size + size + 2 > MAX_CASSETTE:
            raise CassetteError("recording limit reached (64 MiB / 100000 events)")
        # Commit pairing only when the whole event passes every storage bound.
        if "id" in msg:
            if "method" in msg:
                self.pending[direction].add(key)
            else:
                self.pending[other].remove(key)
        self.size += size
        self.events.append(event)

    def document(self, code, reasons):
        if code is not None and (type(code) is not int or not -2147483648 <= code <= 2147483647):
            raise CassetteError("server exit code is outside signed 32-bit range")
        reasons = bounded_reasons(reasons)
        return {"version": 1, "incomplete": bool(reasons), "redact_paths": self.paths,
                "metadata": {"generator": "mcp-cassette/0.1.0", "server_exit_code": code,
                             "reasons": reasons}, "events": self.events}


async def record(path, command, *, paths=(), shutdown_timeout=2.0, skip_non_json=False, warn=print):
    recorder = Recorder(list(paths))
    reasons = []
    proc = None
    tasks = []
    transport = None
    writer = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=None, limit=MAX_LINE, start_new_session=True)
        except OSError:
            reasons.append("server could not be started")
            return 1
        reader, transport = await stdin_reader()
        writer = await StdoutWriter.open()

        async def incoming():
            while line := await read_line(reader):
                msg = parse_line(line)
                recorder.append("client", msg)
                proc.stdin.write(line)
                await proc.stdin.drain()

        async def outgoing():
            while line := await read_line(proc.stdout):
                if skip_non_json:
                    try:
                        # Syntax probe only: integer materialization belongs to
                        # the bounded strict parser below, even if Python's
                        # process-wide integer-string limit has been disabled.
                        json.loads(line, parse_int=lambda text: None)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        warn("skipped non-JSON server stdout line (content omitted)")
                        continue
                    except (ValueError, RecursionError):
                        # Let the strict parser report unsupported numbers or
                        # nesting; skip-non-json must not hide valid JSON frames.
                        pass
                msg = parse_line(line)
                recorder.append("server", msg)
                await writer.write(line)

        incoming_task = asyncio.create_task(incoming())
        outgoing_task = asyncio.create_task(outgoing())
        process_task = asyncio.create_task(proc.wait())
        stop_task = asyncio.create_task(stop.wait())
        tasks = [incoming_task, outgoing_task, process_task, stop_task]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stop_task:
                task.result()
        # EOF, process exit, or the client's termination signal all end the
        # session. Drain server output before writing the final cassette.
        if not incoming_task.done():
            incoming_task.cancel()
        proc.stdin.close()
        try:
            await asyncio.wait_for(asyncio.shield(process_task), shutdown_timeout)
        except asyncio.TimeoutError:
            reasons.append("server did not exit after stdin EOF")
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(process_task), shutdown_timeout)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await process_task
        try:
            await asyncio.wait_for(asyncio.shield(outgoing_task), shutdown_timeout)
        except asyncio.TimeoutError:
            reasons.append("server stdout did not close")
    except (CassetteError, OSError, ValueError, RecursionError) as exc:
        reasons.append(str(exc) if isinstance(exc, CassetteError) else "stdio I/O failed during forwarding")
    except BaseException:
        reasons.append("recording aborted unexpectedly")
        raise
    finally:
        if proc is not None:
            # Kill the owned process group, including descendants holding pipes.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), shutdown_timeout)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if proc.returncode != 0:
                reasons.append(f"server exited with code {proc.returncode}")
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if transport is not None:
            transport.close()
        if writer is not None:
            try:
                await writer.close()
            except CassetteError as exc:
                reasons.append(str(exc))
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        if any(recorder.pending.values()):
            reasons.append("unanswered requests remain")
        document = recorder.document(proc.returncode if proc else None, reasons)
        save_cassette(path, document)
        if document["metadata"]["reasons"]:
            warn("incomplete recording: " + "; ".join(document["metadata"]["reasons"]))
    return 1 if reasons else 0
