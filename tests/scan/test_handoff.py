"""Section 4: plain handoff packs inside the project are checked for staleness, read-only."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import jsonschema

from agentwrench.handoff.pack import capture_pack, task_input
from agentwrench.handoff.pack_scope import output_path, repository

from .conftest import ROOT, run, run_json

SCHEMA_FILE = ROOT / "docs" / "scan" / "scan.schema.json"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30)


def project_with_pack(project: Path) -> Path:
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.name", "Sample Fixture")
    git(project, "config", "user.email", "sample@example.invalid")
    git(project, "config", "commit.gpgsign", "false")
    (project / ".gitignore").write_text(".handoff/\n")
    (project / "app.py").write_text("print('v1')\n")
    git(project, "add", ".")
    git(project, "commit", "-qm", "base")
    (project / ".handoff").mkdir()
    repo = repository(str(project))
    output = output_path(str(project / ".handoff" / "pack-01"), repo)
    capture_pack(repo, task_input({"goal": "Sample: continue the refactor"}), [], output)
    return Path(output)


def digest_worktree(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts and path.name != "index":
            continue
        info = path.lstat()
        data = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        result[str(path)] = (info.st_mtime_ns, data)
    return result


def test_no_pack_means_no_section_and_no_git(setup, capsys):
    code, doc = run_json(capsys, setup.args())
    assert code == 0 and doc["handoff"] == {"repository": None, "packs": []}
    _, out, _ = run(capsys, setup.args("--no-color"))
    assert "Handoff packs" not in out


def test_current_pack_is_reported_without_findings(setup, capsys):
    pack = project_with_pack(setup.project)
    code, doc = run_json(capsys, setup.args())
    jsonschema.validate(doc, json.loads(SCHEMA_FILE.read_text()))
    assert code == 0
    [entry] = doc["handoff"]["packs"]
    assert entry["path"] == str(pack) and entry["status"] == "matches_captured_scope"
    assert entry["evidence"] == {"records": 0, "matching": 0} and entry["error"] is None
    assert not [f for f in doc["findings"] if f["area"] == "handoff"]
    _, out, _ = run(capsys, setup.args("--no-color"))
    assert "4. Handoff packs" in out and "current" in out


def test_stale_pack_is_a_warning_with_the_verify_command_and_scan_stays_read_only(setup, capsys):
    project_with_pack(setup.project)
    (setup.project / "app.py").write_text("print('v2')\n")
    before = digest_worktree(setup.project)
    code, doc = run_json(capsys, setup.args())
    assert code == 0
    [entry] = doc["handoff"]["packs"]
    assert entry["status"] == "stale_or_unbound" and "File/index changed: app.py" in entry["changes"]
    [finding] = [f for f in doc["findings"] if f["area"] == "handoff"]
    assert finding["severity"] == "warning" and "is stale" in finding["message"]
    assert finding["command"][0].startswith("agentwrench handoff verify ")
    assert digest_worktree(setup.project) == before


def test_tampered_pack_is_an_error(setup, capsys):
    pack = project_with_pack(setup.project)
    data = (pack / "handoff.json").read_text(encoding="utf-8").replace("continue the refactor", "it is done")
    (pack / "handoff.json").write_text(data, encoding="utf-8")
    _, doc = run_json(capsys, setup.args())
    assert doc["handoff"]["packs"][0]["status"] == "invalid"
    [finding] = [f for f in doc["findings"] if f["area"] == "handoff"]
    assert finding["severity"] == "error" and "integrity mismatch" in finding["message"]


def test_pack_outside_a_git_worktree_is_not_checked(setup, capsys, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    pack = project_with_pack(source)
    target = setup.project / "received"
    target.mkdir()
    for name in ("handoff.json", "handoff.md"):
        (target / name).write_bytes((pack / name).read_bytes())
    _, doc = run_json(capsys, setup.args())
    assert doc["handoff"]["packs"][0]["status"] == "not_checked"
    [finding] = [f for f in doc["findings"] if f["area"] == "handoff"]
    assert "not inside a usable Git worktree" in finding["message"]
