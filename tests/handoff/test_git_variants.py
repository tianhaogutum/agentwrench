from __future__ import annotations

import os
import shlex
import subprocess
import sys

import pytest

pytest.importorskip("in_toto")
pytest.importorskip("pydantic")

from agentwrench.handoff.core import capture, verify
from agentwrench.handoff.git import repository, snapshot
from agentwrench.handoff.io import HandoffError, read_bytes

from .helpers import git


def test_git_clean_filters_are_not_executed_by_snapshot(repo, tmp_path):
    marker = tmp_path / "filter-executed"
    helper = tmp_path / "filter.py"
    helper.write_text(
        "from pathlib import Path\nimport sys\n"
        f"Path({str(marker)!r}).touch()\nsys.stdout.write(sys.stdin.read())\n"
    )
    (repo / ".gitattributes").write_text("*.py filter=probe\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "declare filter without installing it")
    git(repo, "config", "filter.probe.clean", shlex.join([sys.executable, str(helper)]))
    git(repo, "config", "filter.probe.required", "true")
    (repo / "retry.py").write_text("def attempts():\n    return 7\n")
    state = snapshot(repo)
    assert state.unstaged_diff.size > 0
    assert not marker.exists()


def test_patch_presentation_config_does_not_change_source_state(repo):
    (repo / "retry.py").write_text("def attempts():\n    return 7\n")
    first = snapshot(repo)
    for key, value in [
        ("diff.noprefix", "true"), ("diff.mnemonicprefix", "true"),
        ("diff.context", "0"), ("diff.algorithm", "histogram"),
        ("diff.indentheuristic", "true"), ("diff.suppressBlankEmpty", "true"),
    ]:
        git(repo, "config", key, value)
    assert snapshot(repo).state_sha256 == first.state_sha256


def test_linked_worktree_and_detached_head(repo, task, tmp_path):
    worktree = tmp_path / "linked-worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    try:
        assert (worktree / ".git").is_file()
        assert repository(worktree) == worktree
        state = snapshot(worktree)
        assert state.branch_hint is None
        assert state.state_sha256 == snapshot(repo).state_sha256
        package = capture(repo=worktree, task_path=task, logs=[], output=tmp_path / "handoff.json")
        assert verify(package, repo=repo, keys=[])["ok"]
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree))


def test_sha256_object_repository(tmp_path):
    root = tmp_path / "sha256"
    root.mkdir()
    git(root, "init", "-q", "--object-format=sha256")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "code.txt").write_text("real sha256 Git object format\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "sha256 baseline")
    state = snapshot(root)
    assert len(state.head) == 64
    assert all(len(entry.oid) == 64 for entry in state.index)


def test_separate_git_directory(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q", "--separate-git-dir", str(tmp_path / "git-data"))
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "file").write_text("source")
    git(root, "add", ".")
    git(root, "commit", "-qm", "separate directory")
    assert (root / ".git").is_file()
    assert snapshot(repository(root)).head


def test_unborn_repository_is_explicitly_rejected(tmp_path):
    git(tmp_path, "init", "-q")
    with pytest.raises(HandoffError):
        snapshot(tmp_path)


def test_unmerged_index_is_rejected(repo):
    original = git(repo, "symbolic-ref", "--short", "HEAD").decode().strip()
    git(repo, "checkout", "-qb", "other")
    (repo / "retry.py").write_text("other\n")
    git(repo, "commit", "-qam", "other side")
    git(repo, "checkout", "-q", original)
    (repo / "retry.py").write_text("main\n")
    git(repo, "commit", "-qam", "main side")
    result = subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True, timeout=10)
    assert result.returncode == 1
    with pytest.raises(HandoffError, match="unmerged"):
        snapshot(repo)


def test_submodule_index_mode_is_rejected(repo):
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},subrepo")
    with pytest.raises(HandoffError, match="submodules"):
        snapshot(repo)


def test_assume_unchanged_cannot_hide_changed_source(repo, task, tmp_path):
    package = capture(repo=repo, task_path=task, logs=[], output=tmp_path / "handoff.json")
    git(repo, "update-index", "--assume-unchanged", "retry.py")
    (repo / "retry.py").write_text("hidden from normal diff\n")
    assert git(repo, "diff") == b""
    result = verify(package, repo=repo, keys=[])
    assert not result["ok"]
    assert {"kind": "source_file", "path": "retry.py"} in result["differences"]


def test_fifo_input_does_not_block(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(HandoffError, match="regular file"):
        read_bytes(fifo)


def test_non_utf8_path_is_rejected(repo):
    # APFS refuses such worktree names. Git's real index can still contain one,
    # so generate the upstream format through its NUL-delimited index API.
    oid = git(repo, "rev-parse", "HEAD:retry.py").strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "-z", "--index-info"],
        input=b"100644 " + oid + b"\tinvalid-\xff\0", check=True, capture_output=True, timeout=10,
    )
    with pytest.raises(HandoffError, match="non-UTF-8"):
        snapshot(repo)


def test_repo_directory_with_trailing_newline(repo):
    moved = repo.with_name("repo\n")
    repo.rename(moved)
    assert repository(moved) == moved
    assert snapshot(moved).head
