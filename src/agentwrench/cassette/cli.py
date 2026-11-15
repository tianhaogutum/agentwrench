"""Command-line interface. record/replay stdout is reserved for MCP traffic."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import signal
import sys

from .format import MAX_LINE, CassetteError, load_cassette, parse_line, save_cassette
from .faults import load_fault_scenario
from .record import record
from .stdio import StdoutWriter, read_line, stdin_reader
from .replay import Replayer, inspect_cassette
from .validation import validation_report


def diagnostic(message):
    print(f"agentwrench cassette: {message}", file=sys.stderr, flush=True)


async def emit(writer, messages):
    for message in messages:
        frame = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        # Live IDs/tokens and generated errors can outgrow valid stored/input
        # frames. Bound the actual wire bytes before writing any of this frame.
        if len(frame) > MAX_LINE:
            raise CassetteError("replay output line exceeds 1 MiB (including newline)")
        await writer.write(frame)


async def replay_stdio(replayer):
    reader, transport = await stdin_reader()
    writer = await StdoutWriter.open()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stop_task = asyncio.create_task(stop.wait())
    line_task = None
    output_task = None

    async def send(messages):
        nonlocal output_task
        output_task = asyncio.create_task(emit(writer, messages))
        done, _ = await asyncio.wait([output_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
        if output_task in done:
            output_task.result()
        else:
            raise CassetteError("client terminated while replay output was pending")

    try:
        await send(replayer.startup)
        while True:
            line_task = asyncio.create_task(read_line(reader))
            done, _ = await asyncio.wait([line_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
            if line_task in done:
                line = line_task.result()
                if not line:
                    break
                messages = replayer.handle(parse_line(line))
                if replayer.fault_triggered:
                    if replayer.fault_scenario["kind"] == "timeout":
                        # Do not read further input or emit target-owned output.
                        # The finite watchdog also ends a session whose client
                        # keeps stdin open. Signals can end the stall early.
                        await asyncio.wait([stop_task], timeout=replayer.fault_scenario["duration_ms"] / 1000)
                    break
                await send(messages)
            else:
                break
    finally:
        for task in (line_task, output_task, stop_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (line_task, output_task, stop_task) if t is not None), return_exceptions=True)
        if transport is not None:
            transport.close()
        try:
            await writer.close()
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
    return replayer.finish()


def positive_seconds(text):
    import math
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected positive seconds") from exc
    if not math.isfinite(value) or not 0 < value <= 60:
        raise argparse.ArgumentTypeError("timeout must be > 0 and <= 60 seconds")
    return value


def parser():
    result = argparse.ArgumentParser(prog="agentwrench cassette", description="Record/replay newline-framed MCP stdio JSON-RPC")
    sub = result.add_subparsers(dest="action", required=True)
    rec = sub.add_parser("record", help="proxy a real server and atomically save a cassette")
    rec.add_argument("--cassette", required=True, type=Path)
    rec.add_argument("--redact-path", action="append", default=[], help="JSON Pointer relative to each JSON-RPC message")
    rec.add_argument("--shutdown-timeout", default=2.0, type=positive_seconds)
    rec.add_argument("--skip-non-json", action="store_true", help="explicitly discard non-JSON server stdout lines with warnings")
    rec.add_argument("--report", type=Path, help="write a machine-readable exit report (used by pytest)")
    rec.add_argument("command", nargs=argparse.REMAINDER)
    rep = sub.add_parser("replay", help="serve recorded responses without executing a server")
    rep.add_argument("--cassette", required=True, type=Path)
    rep.add_argument("--strict", action="store_true", help="fail on unmatched, unused or protocol-version mismatch")
    rep.add_argument("--match", choices=["params", "sequence"], default="params")
    rep.add_argument("--fault-scenario", type=Path, help="explicit opt-in JSON transport fault scenario (v1)")
    rep.add_argument("--report", type=Path)
    ins = sub.add_parser("inspect", help="print method/tool counts or one frame and its peer as JSON")
    ins.add_argument("cassette", type=Path)
    ins.add_argument("--frame", metavar="N", help="inspect a zero-based frame index without replay")
    val = sub.add_parser("validate", help="print versioned JSON integrity diagnostics with frame locations")
    val.add_argument("cassette", type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    code = 2
    replay = None
    try:
        if args.action == "validate" or (args.action == "inspect" and args.frame is not None):
            report, code = validation_report(args.cassette, frame=getattr(args, "frame", None))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return code
        if args.action == "inspect":
            print(json.dumps(inspect_cassette(load_cassette(args.cassette)), ensure_ascii=False, indent=2))
            return 0
        if args.report and args.report.resolve() == args.cassette.resolve():
            raise CassetteError("report path must differ from cassette path")
        scenario_path = getattr(args, "fault_scenario", None)
        if args.report and scenario_path and args.report.resolve() == scenario_path.resolve():
            raise CassetteError("report path must differ from fault scenario path")
        if args.action == "record":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise CassetteError("record requires -- <server command...>")
            code = asyncio.run(record(args.cassette, command, paths=args.redact_path,
                                      shutdown_timeout=args.shutdown_timeout,
                                      skip_non_json=args.skip_non_json, warn=diagnostic))
        else:
            scenario = load_fault_scenario(scenario_path) if scenario_path is not None else None
            if scenario_path is not None and scenario is None:
                raise CassetteError("fault scenario must be an object")
            replay = Replayer(load_cassette(args.cassette), strict=args.strict, match=args.match, warn=diagnostic,
                              fault_scenario=scenario)
            code = asyncio.run(replay_stdio(replay))
    except (CassetteError, OSError, ValueError, RecursionError) as exc:
        diagnostic(str(exc) if isinstance(exc, CassetteError) else "I/O or input error")
        code = 2
    finally:
        if (getattr(args, "report", None) and args.report.resolve() != args.cassette.resolve()
                and (not getattr(args, "fault_scenario", None)
                     or args.report.resolve() != args.fault_scenario.resolve())):
            try:
                report = {"exit_code": code,
                    "unmatched": replay.unmatched_count if replay else 0,
                    "unused": replay.unused_count if replay else 0}
                if replay is not None and replay.fault_scenario is not None:
                    report["fault"] = {**replay.fault_scenario, "triggered": replay.fault_triggered}
                save_cassette(args.report, report)
            except (OSError, CassetteError):
                diagnostic("could not write exit report")
                code = 2
    return code
