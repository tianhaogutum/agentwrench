import pytest
from agentwrench.agentsmd.core import inspect_codex, Settings
from .conftest import put


@pytest.mark.parametrize("scope", ["project", "global"])
def test_shadowed_loop_does_not_invalidate_selected_override(tree, isolated_codex_home, scope):
    root, cwd = tree
    folder = root if scope == "project" else isolated_codex_home
    put(folder / "AGENTS.override.md", "winner")
    (folder / "AGENTS.md").symlink_to("AGENTS.md")
    result = inspect_codex(cwd, isolated_codex_home, Settings())
    assert result.text == "winner"
    assert not result.diagnostics


def test_unshadowed_loop_still_reports_error(tree, isolated_codex_home):
    root, cwd = tree
    (root / "AGENTS.md").symlink_to("AGENTS.md")
    result = inspect_codex(cwd, isolated_codex_home, Settings())
    assert result.diagnostics
