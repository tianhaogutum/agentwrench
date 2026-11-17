from __future__ import annotations

import sys

import pytest

pytest.importorskip("in_toto")
pytest.importorskip("pydantic")

import agentwrench.handoff.core as core
from agentwrench.handoff.core import capture, record, verify
from agentwrench.handoff.io import HandoffError


def build_log(repo, tmp_path):
    (repo / "build").mkdir(exist_ok=True)
    log = tmp_path / "build.link"
    record(
        repo=repo, argv=[sys.executable, "-c", "from pathlib import Path; Path('build/result').write_text('output')"],
        output=log, products=["build/result"],
    )
    return log


def test_receipt_only_ignored_product_is_frozen_in_handoff(repo, task, tmp_path):
    log = build_log(repo, tmp_path)
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    assert "build/result" in {item.path for item in package.artifacts}
    (repo / "build/result").write_text("changed after capture")
    result = verify(package, repo=repo, keys=[])
    assert not result["ok"]
    assert {"kind": "artifact", "path": "build/result"} in result["differences"]


def test_stale_imported_materials_are_valid_history_but_future_drift_is_detected(repo, task, tmp_path):
    log = build_log(repo, tmp_path)
    (repo / "build/result").write_text("already stale at capture")
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    first = verify(package, repo=repo, keys=[])
    assert first["ok"]
    assert not first["evidence"][0]["declared_files_match"]
    (repo / "build/result").write_text("another post-capture change")
    assert not verify(package, repo=repo, keys=[])["ok"]


def test_ignored_artifact_change_during_capture_is_rejected(repo, task, tmp_path, monkeypatch):
    log = build_log(repo, tmp_path)
    original = core.snapshot
    calls = 0

    def racing(root):
        nonlocal calls
        calls += 1
        if calls == 2:
            (repo / "build/result").write_text("concurrent capture edit")
        return original(root)

    monkeypatch.setattr(core, "snapshot", racing)
    with pytest.raises(HandoffError, match="artifact.*changed"):
        capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")


def test_ignored_artifact_change_during_verify_is_rejected(repo, task, tmp_path, monkeypatch):
    log = build_log(repo, tmp_path)
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    original = core.snapshot
    calls = 0

    def racing(root):
        nonlocal calls
        calls += 1
        if calls == 2:
            (repo / "build/result").write_text("concurrent verify edit")
        return original(root)

    monkeypatch.setattr(core, "snapshot", racing)
    with pytest.raises(HandoffError, match="artifact.*changed"):
        verify(package, repo=repo, keys=[])


def test_output_cannot_overwrite_artifact_referenced_only_by_log(repo, task, tmp_path):
    log = build_log(repo, tmp_path)
    with pytest.raises(HandoffError, match="artifact"):
        capture(repo=repo, task_path=task, logs=[log], output=repo / "build/result", force=True)
    assert (repo / "build/result").read_text() == "output"


def test_manifest_cannot_omit_observed_evidence_artifacts(repo, task, tmp_path):
    log = build_log(repo, tmp_path)
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    package.artifacts = [item for item in package.artifacts if item.path != "build/result"]
    package.seal()
    with pytest.raises(HandoffError, match="artifact scope"):
        verify(package, repo=repo, keys=[])


def test_absence_of_a_requested_ignored_product_is_also_frozen(repo, task, tmp_path):
    log = tmp_path / "missing.link"
    record(repo=repo, argv=[sys.executable, "-c", "pass"], output=log, products=["build/missing"])
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    assert next(item for item in package.artifacts if item.path == "build/missing").kind == "missing"
    (repo / "build").mkdir()
    (repo / "build/missing").write_text("appeared after capture")
    assert not verify(package, repo=repo, keys=[])["ok"]
