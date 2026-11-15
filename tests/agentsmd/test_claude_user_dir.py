"""inspect_claude(user_dir=...): ~/.claude/CLAUDE.md user memory, visited before ancestors."""
from pathlib import Path

from agentwrench.agentsmd.claude import inspect_claude


def test_user_memory_is_optional_and_visited_first(tmp_path: Path):
    project, user = tmp_path / "project", tmp_path / "home" / ".claude"
    project.mkdir()
    user.mkdir(parents=True)
    (project / "CLAUDE.md").write_text("project\n")
    (user / "CLAUDE.md").write_text("user\n@rules.md\n")
    (user / "rules.md").write_text("rules\n")

    default = inspect_claude(project)
    assert [e["scope"] for e in default.entries] == ["ancestor"]

    report = inspect_claude(project, user_dir=user)
    assert [(Path(e["path"]).name, e["scope"], e["status"]) for e in report.entries] == [
        ("CLAUDE.md", "user", "loaded"), ("rules.md", "import", "loaded"), ("CLAUDE.md", "ancestor", "loaded")]
    assert report.edges[0]["status"] == "loaded" and not report.diagnostics


def test_missing_user_dir_is_ignored(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("x\n")
    report = inspect_claude(tmp_path, user_dir=tmp_path / "nope")
    assert [e["scope"] for e in report.entries] == ["ancestor"] and not report.diagnostics
