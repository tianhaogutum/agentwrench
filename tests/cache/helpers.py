"""Shared helpers for the ``agentwrench cache`` tests (ported from prefix-drift)."""

from __future__ import annotations

import contextlib
import copy
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentwrench.cache.cli import main
from agentwrench.cache.jscompat import stringify, to_fixed

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    """Absolute path of a fixture, e.g. ``fixture('compare/before.jsonl')``."""
    return str(FIXTURES / name)


CAPTURE = fixture("openai_chat_volatile_system.jsonl")
CONFIG = fixture("volatile-fields.json")
BEFORE = fixture("compare/before.jsonl")
AFTER = fixture("compare/after.jsonl")
LATER = fixture("compare/later-system.jsonl")


def js(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify``."""
    return stringify(value, indent)


def serialize(values: Iterable[Any]) -> str:
    return "\n".join(js(v) for v in values) + "\n"


def read_entries(path: str) -> list[Any]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").strip().split("\n")]


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def without(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in value.items() if k not in keys}


def score(value: float | None) -> str:
    return "n/a" if value is None else to_fixed(value, 6)


def read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def read_text(path: str) -> str:
    return Path(path).read_bytes().decode("utf-8")


@dataclass
class Result:
    status: int
    stdout: str
    stderr: str


def cli(*args: str) -> Result:
    """Run ``agentwrench cache`` in-process and capture its streams."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = main([str(a) for a in args])
    return Result(status, out.getvalue(), err.getvalue())


class Saver:
    """Write test inputs into a temporary directory and return their paths."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def __call__(self, name: str, text: str | bytes) -> str:
        path = self.dir / name
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8", newline="")
        return str(path)

    def entries(self, name: str, values: Iterable[Any]) -> str:
        return self(name, serialize(values))
