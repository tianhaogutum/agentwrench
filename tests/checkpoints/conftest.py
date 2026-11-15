from pathlib import Path
import pytest
from .make_fixture import make_fixture


@pytest.fixture(autouse=True)
def disable_optional_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "fixture.db"
    make_fixture(path, threads=2, steps=4)
    return path
