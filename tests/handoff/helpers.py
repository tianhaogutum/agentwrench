"""Shared helpers for the `agentwrench handoff` tests (no optional dependencies)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SRC = Path(__file__).resolve().parents[2] / "src"


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=10,
    ).stdout


def subprocess_env() -> dict[str, str]:
    """Environment for `python -m agentwrench ...` children, also from a source checkout."""
    env = os.environ.copy()
    if SRC.is_dir():
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    return env
