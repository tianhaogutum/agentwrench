"""Docs-derived CLAUDE.md discovery and import graph, never execution.

This reports dependencies, not Claude's exact prompt serialization.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from .core import Reader, Report, ScopeError, absolute, candidates, directory, entry, find_root

MAX_EDGES = 2048


def imports(text: str) -> list[tuple[str, int]]:
    """Parse @paths outside comments, fenced blocks and exact-run code spans.

    Paths end at whitespace; escaped spaces are supported as in public docs.
    Quoted paths are literals. No shell, variable or glob interpolation.
    """
    visible = list(text)

    def hide(start: int, end: int) -> None:
        for index in range(start, end):
            if visible[index] != "\n":
                visible[index] = " "

    fence = None
    offset = 0
    for line in io.StringIO(text):
        match = re.match(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if fence:
            hide(offset, offset + len(line))
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
        elif match and (match[1][0] != "`" or "`" not in match[2]):
            fence = (match[1][0], len(match[1]))
            hide(offset, offset + len(line))
        offset += len(line)
    # Mask spans before comments so a comment marker within code stays literal.
    cursor = 0
    masked = "".join(visible)
    while cursor < len(text):
        match = re.search(r"`+", masked[cursor:])
        if not match:
            break
        start = cursor + match.start()
        end = cursor + match.end()
        closing = re.search(r"(?<!`)" + re.escape(masked[start:end]) + r"(?!`)", masked[end:])
        if closing:
            finish = end + closing.end()
            hide(start, finish)
            cursor = finish
        else:
            cursor = end
    masked = "".join(visible)
    for match in re.finditer(r"<!--.*?(?:-->|\Z)", masked, re.DOTALL):
        hide(match.start(), match.end())
    masked = "".join(visible)
    result = []
    # Separate literal keeps the grammar readable: backslash-space or nonspace.
    pattern = r'''(?<![\w@\\"'])@((?:\\ |[^\s`"'<>])+)'''
    for match in re.finditer(pattern, masked):
        token = match[1]
        if token.startswith(("http:", "https:", "mailto:")):
            continue
        result.append((token.replace("\\ ", " "), text.count("\n", 0, match.start()) + 1))
    return result


def inspect_claude(cwd: Path, allow_roots: list[Path] | None = None) -> Report:
    cwd = directory(cwd)
    boundary = find_root(cwd, [".git"]) or cwd
    roots = [boundary.resolve(), *(directory(p).resolve() for p in (allow_roots or []))]
    report = Report("claude", str(cwd), str(boundary))
    report.warnings.append("docs-derived dependency graph; not an exact Claude prompt or subagent prediction")
    reader, visited, active = Reader(), set(), set()
    edges = 0

    def visit(path: Path, depth: int, source: Path | None = None, line: int | None = None) -> None:
        nonlocal edges
        edge = None
        if source is not None:
            edges += 1
            if edges > MAX_EDGES:
                raise ScopeError(f"import graph exceeds {MAX_EDGES} edges")
            edge = dict(source=str(source), target=str(path), line=line, depth=depth, status="loaded")
            report.edges.append(edge)
        try:
            canonical = path.resolve(strict=True)
            if source is not None and not any(canonical.is_relative_to(root) for root in roots):
                raise ScopeError("import outside allowed roots; use --allow-import-root explicitly")
            if canonical in active:
                raise ScopeError("import cycle")
            if depth > 4:
                raise ScopeError("import exceeds four-hop depth limit")
            if canonical in visited:
                if edge is not None:
                    edge["status"] = "already-seen"
                return
            data = reader.read(canonical)
        except (OSError, RuntimeError, ScopeError) as exc:
            message = str(exc) if isinstance(exc, ScopeError) else type(exc).__name__
            report.error(path, message)
            if edge is not None:
                edge["status"] = "error"
                edge["error"] = message
            report.entries.append(entry(path, "import" if source else "ancestor", None, "unreadable"))
            return
        visited.add(canonical)
        active.add(canonical)
        report.entries.append(entry(path, "import" if source else "ancestor", len(data), "loaded", len(data)))
        for token, import_line in imports(data.decode("utf-8", "replace")):
            # No home expansion from the host's credentials/config environment.
            if token.startswith("~") or "\x00" in token or "://" in token:
                report.error(path, f"unsupported import syntax at line {import_line}; use a literal local path")
                continue
            target = absolute(path.parent / token)
            visit(target, depth + 1, path, import_line)
        active.remove(canonical)

    for folder in reversed((cwd, *cwd.parents)):
        for path in candidates(folder, ["CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"], report, "ancestor"):
            visit(path, 0)
    return report
