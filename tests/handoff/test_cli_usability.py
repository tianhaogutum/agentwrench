from __future__ import annotations

import json
import shutil
import sys

import pytest

pytest.importorskip("in_toto")
pytest.importorskip("pydantic")

from pydantic import ValidationError

import agentwrench.handoff.git as source_git
from agentwrench.handoff.cli import main
from agentwrench.handoff.core import capture, export_diff, record
from agentwrench.handoff.evidence import load_evidence
from agentwrench.handoff.io import HandoffError
from agentwrench.handoff.models import Task

from .helpers import FIXTURES, git


def test_empty_argument_values_are_preserved_but_executable_must_exist():
    task = Task(goal="Valid goal", verification_commands=[["tool", "--value", ""]])
    assert task.verification_commands[0][-1] == ""
    with pytest.raises(HandoffError, match="command argv"):
        Task(goal="Bad command", verification_commands=[[""]])
    with pytest.raises(ValidationError):
        Task(goal="  \n ")


def test_record_argv_limits_fail_before_execution(repo, tmp_path):
    marker = repo / "build/should-not-run"
    marker.parent.mkdir()
    argv = [sys.executable, "-c", "from pathlib import Path; Path('build/should-not-run').touch()", *(["x"] * 1001)]
    with pytest.raises(HandoffError, match="command argv"):
        record(repo=repo, argv=argv, output=tmp_path / "record.link")
    assert not marker.exists()


def test_inspect_reads_claims_without_any_repository_access(repo, task, tmp_path, monkeypatch, capsys):
    output = tmp_path / "handoff.json"
    capture(repo=repo, task_path=task, logs=[], output=output)
    shutil.rmtree(repo)

    def no_git(*args, **kwargs):
        raise AssertionError("inspect must not access a repository")

    monkeypatch.setattr(source_git, "git", no_git)
    assert main(["inspect", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["task_claims"]["goal"] == "Cap retries at five"
    assert result["task_claims"]["unresolved"] == ["Decide jitter policy"]
    assert result["repository"] == "not_checked"
    assert result["task_completion"] == "not_assessed"


def test_exported_diff_matches_capture_under_nondefault_git_settings(repo, task, tmp_path, capsys):
    (repo / "binary.bin").write_bytes(bytes(range(256)))
    git(repo, "add", "binary.bin")
    git(repo, "commit", "-qm", "binary input")
    (repo / "binary.bin").write_bytes(b"\x00updated\xff")
    git(repo, "config", "diff.noprefix", "true")
    git(repo, "config", "diff.context", "0")
    patch = tmp_path / "change.patch"
    assert main(["diff", "--repo", str(repo), "--output", str(patch)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert b"GIT binary patch" in patch.read_bytes()
    assert patch.stat().st_mode & 0o777 == 0o600
    package = capture(repo=repo, task_path=task, logs=[], output=tmp_path / "handoff.json", supplied_diff=patch)
    assert package.supplied_diff.sha256 == exported["sha256"]
    with pytest.raises(HandoffError, match="already exists"):
        export_diff(repo=repo, output=patch)


def test_upstream_empty_executable_is_invalid(tmp_path):
    raw = json.loads((FIXTURES / "upstream-unsigned.link").read_text())
    raw["signed"]["command"] = [""]
    path = tmp_path / "bad.link"
    path.write_text(json.dumps(raw))
    with pytest.raises(HandoffError, match="command argv"):
        load_evidence(path)


def test_unborn_repo_error_has_an_actionable_message(tmp_path, capsys):
    git(tmp_path, "init", "-q")
    output = tmp_path.parent / "unborn.patch"
    assert main(["diff", "--repo", str(tmp_path), "--output", str(output)]) == 2
    assert "committed HEAD" in capsys.readouterr().err
    assert not output.exists()


def test_unknown_json_keys_are_not_echoed_and_schema_errors_are_bounded(repo, tmp_path, capsys):
    marker = "PRIVATE_UNKNOWN_KEY_" + "x" * 4000
    task = tmp_path / "bad-task.json"
    task.write_text(json.dumps({"goal": "Valid goal", marker: True}))
    assert main(["capture", "--repo", str(repo), "--task", str(task), "--output", str(tmp_path / "handoff.json")]) == 2
    stderr = capsys.readouterr().err
    assert marker not in stderr
    assert len(stderr) < 1024
    error = json.loads(stderr)
    assert error["fields"] == ["<unknown>"]
    assert error["issue_types"] == ["extra_forbidden"]
