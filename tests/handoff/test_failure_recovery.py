from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time

import pytest

pytest.importorskip("in_toto")
pytest.importorskip("pydantic")

from securesystemslib.exceptions import StorageError

import agentwrench.handoff.cli as cli
import agentwrench.handoff.core as core
import agentwrench.handoff.io as local_io
from agentwrench.handoff.cli import main
from agentwrench.handoff.core import record
from agentwrench.handoff.io import HandoffError

from .helpers import subprocess_env


@pytest.mark.parametrize("stage", ["fsync", "replace", "link"])
def test_atomic_write_failure_leaves_no_partial_file(tmp_path, monkeypatch, stage):
    target = tmp_path / "handoff.json"
    force = stage != "link"
    if force:
        target.write_bytes(b"previous artifact")

    def out_of_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "simulated full disk")

    monkeypatch.setattr(local_io.os, stage, out_of_space)
    with pytest.raises(OSError) as error:
        local_io.atomic_json(target, {"new": "artifact"}, force=force)
    assert error.value.errno == errno.ENOSPC
    if force:
        assert target.read_bytes() == b"previous artifact"
    else:
        assert not target.exists()
    assert not list(tmp_path.glob(".handoff-*.tmp"))


def test_timeout_preserves_previous_receipt_when_force_was_requested(repo, tmp_path):
    output = tmp_path / "check.link"
    record(repo=repo, argv=[sys.executable, "-c", "pass"], output=output)
    previous = output.read_bytes()
    with pytest.raises(subprocess.TimeoutExpired):
        record(repo=repo, argv=[sys.executable, "-c", "import time; time.sleep(5)"], output=output, timeout=1, force=True)
    assert output.read_bytes() == previous


def test_unlaunchable_command_error_does_not_echo_arguments(repo, tmp_path, capsys):
    secret = "DO_NOT_ECHO_ARGUMENT_9ce21"
    result = main([
        "record", "--repo", str(repo), "--output", str(tmp_path / "missing.link"),
        "--", "nonexistent-handoff-command-92741", secret,
    ])
    assert result == 2
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "could not be executed" in output.err


def test_interrupt_cleans_command_group_and_emits_json(repo, tmp_path):
    (repo / "build").mkdir()
    marker = repo / "build/pids.json"
    command = (
        "import json,os,subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "Path('build/pids.tmp').write_text(json.dumps([os.getppid(),os.getpid(),p.pid])); "
        "Path('build/pids.tmp').replace('build/pids.json'); "
        "time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentwrench", "handoff", "record", "--repo", str(repo),
         "--output", str(tmp_path / "interrupt.link"), "--", sys.executable, "-c", command],
        cwd=tmp_path, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=subprocess_env(),
    )
    pids = []
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        assert marker.exists(), proc.communicate(timeout=2)
        pids = json.loads(marker.read_text())
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 130, (stdout, stderr)
        assert "interrupted" in json.loads(stderr)["error"]
        assert not (tmp_path / "interrupt.link").exists()
        for pid in pids:
            deadline = time.monotonic() + 1
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    pytest.fail(f"recorded process {pid} survived interruption")
                time.sleep(0.01)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)
        if pids:
            try:
                os.killpg(pids[0], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cli_reports_disk_full_without_echoing_input_paths(tmp_path, monkeypatch, capsys):
    marker = "PRIVATE_PATH_DO_NOT_ECHO"

    def disk_full(**kwargs):
        raise OSError(errno.ENOSPC, "No space left on device", marker)

    monkeypatch.setattr(cli, "capture", disk_full)
    assert main(["capture", "--repo", marker, "--task", "task.json", "--output", "handoff.json"]) == 2
    stderr = capsys.readouterr().err
    assert marker not in stderr
    result = json.loads(stderr)
    assert result["errno"] == errno.ENOSPC
    assert "No space left" in result["error"]


def test_upstream_hash_failure_restores_cwd_and_preserves_output(repo, tmp_path, monkeypatch):
    output = tmp_path / "existing.link"
    output.write_bytes(b"previous receipt")
    monkeypatch.chdir(tmp_path)
    original_cwd = os.getcwd()

    def upstream_failure(*args, **kwargs):
        os.chdir(repo)  # in-toto's resolver can change cwd before failing.
        raise StorageError("PRIVATE_UPSTREAM_INPUT_DETAIL")

    monkeypatch.setattr(core, "record_artifacts_as_dict", upstream_failure)
    try:
        with pytest.raises(HandoffError, match="in-toto"):
            record(repo=repo, argv=[sys.executable, "-c", "pass"], output=output, force=True)
        assert os.getcwd() == original_cwd
        assert output.read_bytes() == b"previous receipt"
    finally:
        os.chdir(original_cwd)
