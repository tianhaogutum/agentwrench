from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from .helpers import SRC, git

if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test Fixture")
    git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text("__pycache__/\n.evidence/\nbuild/\n")
    (root / "retry.py").write_text("def attempts():\n    return 5\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "real Git fixture")
    return root


@pytest.fixture
def task(tmp_path: Path) -> Path:
    path = tmp_path / "task.json"
    path.write_text(json.dumps({
        "goal": "Cap retries at five", "claimed_done": ["Implemented"],
        "unresolved": ["Decide jitter policy"], "artifacts": ["retry.py"],
        "verification_commands": [["python", "-m", "pytest"]],
    }))
    return path
