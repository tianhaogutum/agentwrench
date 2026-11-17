from __future__ import annotations

import base64
import json
import os
import signal
import sys

import pytest

pytest.importorskip("in_toto")
pytest.importorskip("pydantic")

from agentwrench.handoff.core import capture, record, verify
from agentwrench.handoff.evidence import load_evidence
from agentwrench.handoff.io import HandoffError

from .helpers import FIXTURES, git


def test_existing_output_fails_before_command_executes(repo, tmp_path):
    log = tmp_path / "existing.link"
    log.write_text("original data")
    sentinel = repo / "build/should-not-run"
    sentinel.parent.mkdir()
    command = [sys.executable, "-c", "from pathlib import Path; Path('build/should-not-run').touch()"]
    with pytest.raises(HandoffError, match="already exists"):
        record(repo=repo, argv=command, output=log)
    assert not sentinel.exists()
    assert log.read_text() == "original data"


def test_output_symlink_alias_cannot_bypass_source_scope(repo, task, tmp_path):
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    with pytest.raises(HandoffError, match="Git-ignored"):
        capture(repo=repo, task_path=task, logs=[], output=alias / "handoff.json")
    assert not (repo / "handoff.json").exists()


def test_missing_requested_product_is_not_a_current_pass(repo, task, tmp_path):
    log = tmp_path / "missing-product.link"
    record(repo=repo, argv=[sys.executable, "-c", "pass"], output=log, products=["build/never-produced"])
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    result = verify(package, repo=repo, keys=[], require_current_pass=True)
    assert not result["ok"]
    assert result["evidence"][0]["missing_requested_products"] == ["build/never-produced"]


def test_dsse_payload_rejects_duplicate_keys(tmp_path):
    raw = json.loads((FIXTURES / "upstream-dsse.link").read_text())
    payload = base64.b64decode(raw["payload"]).decode()
    payload = payload[:-1] + ',"command":["different-command"]}'
    raw["payload"] = base64.b64encode(payload.encode()).decode()
    path = tmp_path / "duplicate-dsse.link"
    path.write_text(json.dumps(raw))
    with pytest.raises(HandoffError, match="duplicate"):
        load_evidence(path)


@pytest.mark.parametrize("argv", [[123], [None], [{"command": "not argv"}], ["bad\x00arg"]])
def test_upstream_loose_command_shape_is_rejected(tmp_path, argv):
    raw = json.loads((FIXTURES / "upstream-unsigned.link").read_text())
    raw["signed"]["command"] = argv
    path = tmp_path / "invalid-command.link"
    path.write_text(json.dumps(raw))
    with pytest.raises(HandoffError, match="command argv"):
        load_evidence(path)


def test_upstream_default_exclusions_do_not_omit_tracked_source(repo, tmp_path):
    (repo / "source.link").write_text("a tracked input, not a receipt")
    git(repo, "add", "source.link")
    git(repo, "commit", "-qm", "include source with upstream-excluded suffix")
    log = tmp_path / "record.link"
    record(repo=repo, argv=[sys.executable, "-c", "pass"], output=log)
    raw = json.loads(log.read_text())
    assert "source.link" in raw["signed"]["materials"]
    assert "source.link" in raw["signed"]["products"]


def test_record_kills_leftover_command_descendants(repo, tmp_path):
    (repo / "build").mkdir()
    child = "import time; time.sleep(30)"
    launcher = (
        "import subprocess,sys; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "Path('build/child.pid').write_text(str(p.pid))"
    )
    pid = None
    try:
        record(repo=repo, argv=[sys.executable, "-c", launcher], output=tmp_path / "descendant.link")
        pid = int((repo / "build/child.pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_output_cannot_overwrite_an_ignored_task_artifact(repo, task, tmp_path):
    artifact = repo / "build/result.json"
    artifact.parent.mkdir()
    artifact.write_text('{"build":true}')
    task.write_text(json.dumps({"goal": "Build", "artifacts": ["build/result.json"]}))
    with pytest.raises(HandoffError, match="artifact"):
        capture(repo=repo, task_path=task, logs=[], output=artifact, force=True)
    assert json.loads(artifact.read_text()) == {"build": True}
