"""Shared helpers: a jsdiff-compatible ``create_two_files_patch`` and fixture paths.

The original suite generated candidate patches with jsdiff ``createTwoFilesPatch``;
this is a port of that function (line diff + hunk assembly + formatting) on top of
the gate's own Myers implementation, so patches are byte-identical to jsdiff's.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

from agentwrench.fence.jsdiff import diff_arrays

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples" / "fence"


def _tokenize_lines(value: str) -> list[str]:
    parts = re.split(r"(\n|\r\n)", value)
    if not parts[-1]:
        parts.pop()
    tokens: list[str] = []
    for i, part in enumerate(parts):
        if i % 2:
            tokens[-1] += part
        else:
            tokens.append(part)
    return [token for token in tokens if token]


def _split_lines(text: str) -> list[str]:
    trailing = text.endswith("\n")
    result = [line + "\n" for line in text.split("\n")]
    if trailing:
        result.pop()
    else:
        result[-1] = result[-1][:-1]
    return result


def _needs_quoting(name: str) -> bool:
    return any(ch < "\x20" or ch > "\x7e" or ch in '"\\' for ch in name)


def _quote(name: str) -> str:
    if not _needs_quoting(name):
        return name
    escapes = {0x07: "\\a", 0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0B: "\\v", 0x0C: "\\f", 0x0D: "\\r",
               0x22: '\\"', 0x5C: "\\\\"}
    out = ['"']
    for byte in name.encode("utf-8"):
        if byte in escapes:
            out.append(escapes[byte])
        elif 0x20 <= byte <= 0x7E:
            out.append(chr(byte))
        else:
            out.append("\\" + format(byte, "03o"))
    out.append('"')
    return "".join(out)


def create_two_files_patch(old_name: str, new_name: str, old: str, new: str, old_header: str = "",
                           new_header: str = "", *, context: int = 4) -> str:
    """Port of jsdiff 9.0.0 ``createTwoFilesPatch`` (default 4 lines of context)."""
    old_tokens, new_tokens = _tokenize_lines(old), _tokenize_lines(new)
    diff = [{"value": "".join(change.value), "added": change.added, "removed": change.removed}
            for change in diff_arrays(old_tokens, new_tokens)]
    diff.append({"value": "", "lines": [], "added": False, "removed": False})
    hunks = []
    old_range_start = new_range_start = 0
    current: list[str] = []
    old_line = new_line = 1
    for i, change in enumerate(diff):
        lines = change.get("lines")
        if lines is None:
            lines = change["lines"] = _split_lines(change["value"])
        if change["added"] or change["removed"]:
            if not old_range_start:
                previous = diff[i - 1] if i > 0 else None
                old_range_start, new_range_start = old_line, new_line
                if previous is not None:
                    current = [" " + line for line in previous["lines"][-context:]] if context > 0 else []
                    old_range_start -= len(current)
                    new_range_start -= len(current)
            current.extend(("+" if change["added"] else "-") + line for line in lines)
            if change["added"]:
                new_line += len(lines)
            else:
                old_line += len(lines)
        else:
            if old_range_start:
                if len(lines) <= context * 2 and i < len(diff) - 2:
                    current.extend(" " + line for line in lines)
                else:
                    size = min(len(lines), context)
                    current.extend(" " + line for line in lines[:size])
                    hunks.append({"oldStart": old_range_start, "oldLines": old_line - old_range_start + size,
                                  "newStart": new_range_start, "newLines": new_line - new_range_start + size,
                                  "lines": current})
                    old_range_start = new_range_start = 0
                    current = []
            old_line += len(lines)
            new_line += len(lines)
    for hunk in hunks:
        body = hunk["lines"]
        i = 0
        while i < len(body):
            if body[i].endswith("\n"):
                body[i] = body[i][:-1]
            else:
                body.insert(i + 1, "\\ No newline at end of file")
                i += 1
            i += 1
    out = []
    if old_name == new_name:
        out.append("Index: " + old_name)
    out.append("=" * 67)
    out.append("--- " + _quote(old_name) + ("\t" + old_header if old_header else ""))
    out.append("+++ " + _quote(new_name) + ("\t" + new_header if new_header else ""))
    for hunk in hunks:
        old_start = hunk["oldStart"] - 1 if hunk["oldLines"] == 0 else hunk["oldStart"]
        new_start = hunk["newStart"] - 1 if hunk["newLines"] == 0 else hunk["newStart"]
        out.append(f"@@ -{old_start},{hunk['oldLines']} +{new_start},{hunk['newLines']} @@")
        out.extend(hunk["lines"])
    return "\n".join(out) + "\n"


def cli(*args, input=None, env=None, cwd=None):
    """Run ``agentwrench fence`` in a subprocess (text mode, UTF-8)."""
    return subprocess.run([sys.executable, "-m", "agentwrench", "fence", *map(str, args)], input=input,
                          capture_output=True, text=True, encoding="utf-8", timeout=60, env=env,
                          cwd=cwd or REPO_ROOT)
