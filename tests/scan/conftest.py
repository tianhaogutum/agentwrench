"""Synthetic home/project builders for `agentwrench scan` tests. Nothing touches the real ~."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentwrench.scan import cli

ROOT = Path(__file__).resolve().parents[2]
FAKE_SERVER = ROOT / "examples" / "slim" / "fake_server.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PY = sys.executable
SECRET = "sk-scanTEST0123456789secretvalue"  # must never appear in any output


def fake(mode: str = "static") -> dict:
    return {"command": PY, "args": [str(FAKE_SERVER), mode]}


@dataclass
class Setup:
    home: Path
    project: Path

    # ---- Claude Code
    def claude_json(self, servers: dict | None = None, projects: dict | None = None, **extra) -> None:
        data = dict(extra)
        if servers is not None:
            data["mcpServers"] = servers
        if projects is not None:
            data["projects"] = projects
        (self.home / ".claude.json").write_text(json.dumps(data), encoding="utf-8")

    def mcp_json(self, servers: dict) -> None:
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    def claude_session(self, name: str, records: list[dict], *, age: float = 3600, folder: str = "-proj") -> Path:
        path = self.home / ".claude" / "projects" / folder / f"{name}.jsonl"
        return write_jsonl(path, records, age)

    # ---- Codex
    def codex_config(self, text: str) -> None:
        (self.home / ".codex").mkdir(exist_ok=True)
        (self.home / ".codex" / "config.toml").write_text(text, encoding="utf-8")

    def codex_session(self, name: str, records: list[dict], *, age: float = 3600) -> Path:
        path = self.home / ".codex" / "sessions" / "2026" / "11" / "01" / f"rollout-{name}.jsonl"
        return write_jsonl(path, records, age)

    # ---- running
    def args(self, *extra: str) -> list[str]:
        return ["--home", str(self.home), "--project", str(self.project), *extra]


def write_jsonl(path: Path, records: list[dict], age: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    return Setup(home, project)


def run(capsys, argv: list[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys, argv: list[str]) -> tuple[int, dict]:
    code, out, _ = run(capsys, [*argv, "--json"])
    return code, json.loads(out)


# ---- session records (fictional)

def claude_record(index: int, kind: str, content, parent: int | None) -> dict:
    return {"type": kind, "uuid": f"u{index}", "parentUuid": None if parent is None else f"u{parent}",
            "sessionId": "s", "isSidechain": False, "message": {"role": kind, "content": content}}


def claude_clean() -> list[dict]:
    return [
        claude_record(1, "user", "hello", None),
        claude_record(2, "assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}], 1),
        claude_record(3, "user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}], 2),
        claude_record(4, "assistant", [{"type": "text", "text": "done"}], 3),
    ]


def claude_orphan(prompt_after: bool = True) -> list[dict]:
    records = [
        claude_record(1, "user", "hello", None),
        claude_record(2, "assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], 1),
    ]
    if prompt_after:
        records.append(claude_record(3, "user", "continue", 2))
    return records


def codex_call(cid: str) -> dict:
    return {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "{}",
                                                 "call_id": cid}}


def codex_output(cid: str) -> dict:
    return {"type": "response_item", "payload": {"type": "function_call_output", "output": "ok", "call_id": cid}}


def codex_prompt(text: str) -> dict:
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                 "content": [{"type": "input_text", "text": text}]}}


def codex_clean() -> list[dict]:
    return [{"type": "session_meta", "payload": {"id": "x"}}, codex_prompt("hi"), codex_call("c1"), codex_output("c1")]


def codex_orphan() -> list[dict]:
    return [{"type": "session_meta", "payload": {"id": "y"}}, codex_prompt("hi"), codex_call("c1"), codex_prompt("again")]
