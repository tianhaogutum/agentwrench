from __future__ import annotations

import sys

import pytest

pytest.importorskip("in_toto")
pytest.importorskip("pydantic")

import agentwrench.handoff.git as source_git
from agentwrench.handoff.core import capture, record
from agentwrench.handoff.git import snapshot
from agentwrench.handoff.io import MAX_FILE, HandoffError

from .helpers import git


def test_oversized_worktree_file_is_rejected_before_native_diff(repo, monkeypatch):
    with (repo / "retry.py").open("wb") as handle:
        handle.truncate(MAX_FILE + 1)  # sparse: no large allocation or disk payload
    original = source_git.git

    def guarded(root, *argv, **kwargs):
        assert argv[0] != "diff", "native diff ran before the source size guard"
        return original(root, *argv, **kwargs)

    monkeypatch.setattr(source_git, "git", guarded)
    with pytest.raises(HandoffError, match="file exceeds"):
        snapshot(repo)


def test_oversized_head_or_index_blob_is_rejected_before_native_diff(repo, monkeypatch):
    (repo / "history.bin").write_bytes(b"x" * 256)
    git(repo, "add", "history.bin")
    git(repo, "commit", "-qm", "bounded legacy blob fixture")
    (repo / "history.bin").unlink()
    monkeypatch.setattr(source_git, "MAX_FILE", 100)
    original = source_git.git

    def guarded(root, *argv, **kwargs):
        assert argv[0] != "diff", "native diff ran before the Git blob size guard"
        return original(root, *argv, **kwargs)

    monkeypatch.setattr(source_git, "git", guarded)
    with pytest.raises(HandoffError, match="Git blob exceeds"):
        snapshot(repo)


def test_oversized_blob_in_requested_base_is_bounded(repo, task, tmp_path, monkeypatch):
    (repo / "old.bin").write_bytes(b"x" * 256)
    git(repo, "add", "old.bin")
    git(repo, "commit", "-qm", "large prior base")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "rm", "old.bin")
    git(repo, "commit", "-qm", "remove old data")
    monkeypatch.setattr(source_git, "MAX_FILE", 100)
    with pytest.raises(HandoffError, match="Git blob exceeds"):
        capture(repo=repo, task_path=task, logs=[], output=tmp_path / "handoff.json", base=base)


def test_record_scope_cannot_exceed_what_capture_can_import(repo, tmp_path):
    # Exactly 10,000 real source files (the fixture has two tracked files).
    for index in range(9998):
        (repo / f"empty-{index:04}.txt").touch()
    (repo / "build").mkdir()
    (repo / "build/extra").write_text("ignored product")
    with pytest.raises(HandoffError, match="artifact scope exceeds 10000"):
        record(repo=repo, argv=[sys.executable, "-c", "pass"], output=tmp_path / "too-many.link", products=["build/extra"])
    assert not (tmp_path / "too-many.link").exists()
