"""Run the upstream executor with bounded output and process-group cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from .io import HandoffError, canonical, parse_json


def execute(argv: list[str], root: Path, timeout: int) -> dict:
    if os.name != "posix":
        raise HandoffError("record currently requires POSIX process groups; capture/verify can import logs")
    # Launched by file path (not `-m`), so the child needs only in-toto importable,
    # not this package; the worker has no package-relative imports.
    worker = Path(__file__).with_name("_execute_worker.py")
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            [sys.executable, str(worker)], cwd=root, start_new_session=True,
            stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
        )
        try:
            try:
                proc.communicate(canonical({"argv": argv, "timeout": timeout}), timeout=timeout + 5)
            except subprocess.TimeoutExpired as exc:
                raise subprocess.TimeoutExpired(argv, timeout) from exc
        finally:
            # Also remove grandchildren left after a successful shell/launcher.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
        if stdout.tell() > 65536:
            raise HandoffError("command worker returned oversized metadata")
        stdout.seek(0)
        result = parse_json(stdout.read(65537))
        if result.get("error") == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if proc.returncode != 0 or "byproducts" not in result:
            raise HandoffError("command could not be executed; no completed receipt was written")
        return result["byproducts"]
