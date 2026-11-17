"""Shared helpers: locations, token estimates, findings and safe path display."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

# Same estimator as `agentwrench slim`: ceil(UTF-8 bytes / 4); not a model tokenizer.
ESTIMATOR = "ceil(UTF-8 bytes / 4); not a model tokenizer"


def tokens(size: int) -> int:
    return -(-size // 4)


@dataclass(frozen=True)
class Locations:
    """Where each client keeps its files. ``--home`` replaces ``~`` for all of them."""
    home: Path
    project: Path
    claude_json: Path
    claude_dir: Path
    codex_home: Path

    @classmethod
    def resolve(cls, home: Path | None, project: Path) -> "Locations":
        if home is None:
            real = Path.home()
            claude_env = os.environ.get("CLAUDE_CONFIG_DIR")
            codex_env = os.environ.get("CODEX_HOME")
            claude_dir = Path(claude_env).expanduser() if claude_env else real / ".claude"
            claude_json = claude_dir / ".claude.json" if claude_env else real / ".claude.json"
            codex_home = Path(codex_env).expanduser() if codex_env else real / ".codex"
            return cls(real, project, claude_json, claude_dir, codex_home)
        return cls(home, project, home / ".claude.json", home / ".claude", home / ".codex")


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    area: str      # "mcp" | "instructions" | "sessions" | "handoff"
    client: str | None
    message: str
    command: list[str] = field(default_factory=list)  # one shell command per line
    tokens_saved: int = 0
    # Internal tag, not serialized: "config_unreadable" marks a client config that failed to load.
    kind: str = ""

    def to_dict(self) -> dict:
        return {"severity": self.severity, "area": self.area, "client": self.client,
                "message": self.message, "command": list(self.command),
                "tokens_saved": self.tokens_saved}


CLIENT_LABELS = {"claude-code": "Claude Code", "codex": "Codex"}


def show_path(path: Path | str, cwd: Path | None = None) -> str:
    """Short, stable display: relative to the cwd if inside it, else ``~/...`` if under $HOME."""
    text = os.path.abspath(str(path))
    base = os.path.abspath(str(cwd if cwd is not None else Path.cwd()))
    if text == base:
        return "."
    if text.startswith(base.rstrip("/") + "/"):
        return text[len(base.rstrip("/")) + 1:]
    home = os.path.abspath(str(Path.home()))
    if text == home:
        return "~"
    if text.startswith(home.rstrip("/") + "/"):
        return "~/" + text[len(home.rstrip("/")) + 1:]
    return text


def quote(path_or_word: str) -> str:
    """Shell-quote, but keep a leading ``~/`` expandable."""
    if path_or_word.startswith("~/"):
        rest = path_or_word[2:]
        return "~/" + (rest if shlex.quote(rest) == rest else shlex.quote(rest))
    return shlex.quote(path_or_word)
