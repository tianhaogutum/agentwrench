from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "repo"
    cwd = root / "pkg" / "api"
    cwd.mkdir(parents=True)
    (root / ".git").write_text("gitdir: synthetic\n")
    return root, cwd


def put(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path
