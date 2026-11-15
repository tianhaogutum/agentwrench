"""Linux, single-environment Codex semantics, pinned in docs/agentsmd/SEMANTICS.md.

Discovery, decoding and assembly are independently implemented from the
Apache-2.0 openai/codex behavior. See THIRD_PARTY.md for attribution.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

UPSTREAM_SHA = "54b90f98a5724b688bf78793de5c21aca29ecec2"
SEPARATOR = "\n\n--- project-doc ---\n\n"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 512
# Rust str::trim uses Unicode White_Space, unlike Python str.strip.
RUST_WHITESPACE = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


class ScopeError(ValueError):
    """Invalid input or an explicit first-version boundary."""


def absolute(path: Path) -> Path:
    # Keep lexical cwd spelling: upstream walks parents without canonicalizing.
    return Path(os.path.abspath(path.expanduser()))


def directory(path: Path) -> Path:
    path = absolute(path)
    try:
        if not path.is_dir():
            raise ScopeError(f"not a directory: {path}")
        path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScopeError(f"cannot resolve directory: {path}") from exc
    return path


@dataclass
class Reader:
    total: int = 0
    files: int = 0

    def read(self, path: Path) -> bytes:
        self.files += 1
        if self.files > MAX_FILES:
            raise ScopeError(f"inspection exceeds {MAX_FILES} file reads")
        # O_NONBLOCK prevents a file swapped for a FIFO from hanging the audit.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode):
                raise ScopeError(f"not a regular file: {path}")
            if meta.st_size > MAX_FILE_BYTES:
                raise ScopeError(f"file exceeds {MAX_FILE_BYTES}-byte inspection limit: {path}")
            data = stream.read(min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - self.total) + 1)
        self.total += len(data)
        if len(data) > MAX_FILE_BYTES or self.total > MAX_TOTAL_BYTES:
            raise ScopeError("inspection byte limit exceeded")
        return data


@dataclass
class Settings:
    max_bytes: int = 32768
    fallback: list[str] = field(default_factory=list)
    root_markers: list[str] = field(default_factory=lambda: [".git"])


def string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ScopeError(f"{name} must be an array of strings")
    if len(value) > 128:
        raise ScopeError(f"{name} exceeds the 128-entry inspection limit")
    return value


def filename(value: str) -> bool:
    # Linux Codex accepts ':' and '\\' in basenames; neither is a separator.
    return bool(value) and value not in (".", "..") and "/" not in value and "\0" not in value


def load_settings(home: Path, max_bytes: int | None = None,
                  fallback: list[str] | None = None,
                  markers: list[str] | None = None) -> tuple[Settings, list[str]]:
    config = home / "config.toml"
    try:
        raw = Reader().read(config)
    except FileNotFoundError:
        values = {}
    except (OSError, ScopeError) as exc:
        raise ScopeError(f"cannot read config: {config} ({type(exc).__name__})") from exc
    else:
        try:
            values = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            # Never echo source lines: unrelated config may contain secrets.
            raise ScopeError(f"malformed config.toml: {config}") from exc
    budget = values.get("project_doc_max_bytes", 32768) if max_bytes is None else max_bytes
    if type(budget) is not int or not 0 <= budget <= 2**63 - 1:
        raise ScopeError("project_doc_max_bytes must be an integer in [0, 2^63-1]")
    names = string_list(values.get("project_doc_fallback_filenames", []) if fallback is None else fallback,
                        "project_doc_fallback_filenames")
    roots = string_list(values.get("project_root_markers", [".git"]) if markers is None else markers,
                        "project_root_markers")
    # Marker path syntax is supported upstream. Reject it here to bound probes.
    if any(not filename(x) for x in roots):
        raise ScopeError("root markers must be nonempty basenames (path markers unsupported)")
    warnings, clean = [], []
    for name in names:
        name = name.strip(RUST_WHITESPACE)
        if not name:
            continue
        if not filename(name):
            warnings.append("ignored fallback containing path syntax")
        elif name not in clean and name not in ("AGENTS.override.md", "AGENTS.md"):
            clean.append(name)
    return Settings(budget, clean, roots), warnings


def find_root(cwd: Path, markers: list[str]) -> Path | None:
    for folder in (cwd, *cwd.parents):
        for marker in markers:
            try:
                (folder / marker).stat()
                return folder
            except OSError:
                pass  # Upstream FindUpErrorPolicy::Ignore.
    return None


def heading_positions(data: bytes) -> list[dict]:
    """ATX headings outside fenced code; positions always refer to raw bytes."""
    headings, offset, fence = [], 0, None
    for number, line in enumerate(io.BytesIO(data), 1):
        match = re.match(rb" {0,3}(`{3,}|~{3,})", line)
        if match:
            run = match[1]
            if fence is None:
                fence = (run[:1], len(run))
            elif run[:1] == fence[0] and len(run) >= fence[1] and not line[match.end():].strip():
                fence = None
        elif fence is None and re.match(rb" {0,3}#{1,6}(?:[ \t]|\r?$)", line):
            if len(headings) >= 4096:
                raise ScopeError("heading report exceeds the 4096-heading inspection limit")
            headings.append({"byte_offset": offset, "line": number,
                             "heading": line.decode("utf-8", "replace").strip()})
        offset += len(line)
    return headings


def cut_point(path: Path, data: bytes, offset: int) -> dict:
    headings = heading_positions(data)
    previous = [h for h in headings if h["byte_offset"] < offset]
    return {"path": str(path), "byte_offset": offset,
            "line": data[:offset].count(b"\n") + 1,
            "nearest_preceding_heading": previous[-1]["heading"] if previous else None,
            "headings_fully_dropped": [h for h in headings if h["byte_offset"] >= offset],
            "splits_utf8_character": _splits_utf8(data, offset)}


def _splits_utf8(data: bytes, offset: int) -> bool:
    # Only flag a cut through an otherwise valid character, not existing bad UTF-8.
    for start in range(max(0, offset - 3), offset):
        for end in range(offset + 1, min(len(data), start + 4) + 1):
            try:
                if len(data[start:end].decode("utf-8")) == 1:
                    return True
            except UnicodeDecodeError:
                pass
    return False


@dataclass
class Report:
    agent: str
    cwd: str
    root: str | None = None
    max_bytes: int | None = None
    project_bytes_used: int = 0
    entries: list[dict] = field(default_factory=list)
    truncations: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    text: str = ""
    edges: list[dict] = field(default_factory=list)

    @property
    def usage(self) -> float:
        return self.project_bytes_used / self.max_bytes if self.max_bytes else 0.0

    def to_dict(self) -> dict:
        result = asdict(self)
        # JSON explains scope without dumping potentially sensitive instructions.
        del result["text"]
        result.update(schema_version=1, usage=self.usage,
                      rendered_bytes=len(self.text.encode("utf-8")),
                      semantics="docs-derived" if self.agent == "claude" else UPSTREAM_SHA)
        return result

    def error(self, path: Path, exc: Exception | str) -> None:
        self.diagnostics.append({"path": str(path), "error": str(exc) if isinstance(exc, (str, ScopeError)) else type(exc).__name__})


def entry(path: Path, scope: str, size: int | None, status: str, loaded: int = 0,
          text_bytes: int = 0) -> dict:
    return dict(path=str(path), scope=scope, size=size, bytes_loaded=loaded,
                text_bytes=text_bytes, status=status, cumulative_bytes=0,
                project_cumulative_bytes=0)


def candidates(folder: Path, names: list[str], report: Report, scope: str,
               *, shadow_errors: bool = False) -> list[Path]:
    found = []
    for name in names:
        if len(report.entries) + len(found) >= MAX_FILES:
            raise ScopeError(f"discovery exceeds {MAX_FILES} instruction entries")
        path = folder / name
        try:
            meta = path.stat()
            if stat.S_ISREG(meta.st_mode):
                found.append(path)
            elif not stat.S_ISDIR(meta.st_mode):
                if shadow_errors and found:
                    continue
                report.error(path, "non-regular instruction file")
                report.entries.append(entry(path, scope, None, "unreadable"))
        except FileNotFoundError:
            pass
        except OSError as exc:
            if shadow_errors and found:
                # Optional metadata below the selected candidate cannot change
                # upstream instruction selection or invalidate its render.
                continue
            report.error(path, exc)
            report.entries.append(entry(path, scope, None, "unreadable"))
    return found


def shadowed(report: Report, path: Path, scope: str, status: str) -> None:
    # Shadowed contents are never opened, just as in upstream discovery.
    try:
        report.entries.append(entry(path, scope, path.stat().st_size, status))
    except OSError:
        report.entries.append(entry(path, scope, None, status))


def inspect_codex(cwd: Path, home: Path, settings: Settings,
                  warnings: list[str] | None = None, *, hash_files: bool = False) -> Report:
    cwd, home = directory(cwd), absolute(home)
    root = find_root(cwd, settings.root_markers)
    report = Report("codex", str(cwd), str(root) if root else None, settings.max_bytes,
                    warnings=list(warnings or []))
    reader, global_text, chunks = Reader(), "", []
    global_paths = (path for name in ["AGENTS.override.md", "AGENTS.md"]
                    for path in candidates(home, [name], report, "global"))
    for path in global_paths:
        if global_text:
            shadowed(report, path, "global", "shadowed-by-override")
            continue
        try:
            data = reader.read(path)
        except (OSError, ScopeError) as exc:
            report.error(path, exc)
            report.entries.append(entry(path, "global", None, "unreadable"))
            continue
        text = data.decode("utf-8", "replace").strip(RUST_WHITESPACE)
        status = "loaded" if text else "skipped-empty"
        report.entries.append(entry(path, "global", len(data), status,
                                    len(data) if text else 0, len(text.encode("utf-8"))))
        if hash_files:
            report.entries[-1]["sha256"] = hashlib.sha256(data).hexdigest()
        global_text = text
        if global_text:
            break
    folders = [cwd]
    if root:
        while folders[-1] != root:
            folders.append(folders[-1].parent)
        folders.reverse()
    else:
        report.warnings.append("no project root found; only cwd is searched")
    for folder in folders:
        paths = candidates(folder, ["AGENTS.override.md", "AGENTS.md", *settings.fallback], report, "project", shadow_errors=True)
        for index, path in enumerate(paths):
            if index:
                status = "shadowed-by-override" if paths[0].name == "AGENTS.override.md" else "shadowed-by-precedence"
                shadowed(report, path, "project", status)
                continue
            try:
                data = reader.read(path)
            except (OSError, ScopeError) as exc:
                report.error(path, exc)
                report.entries.append(entry(path, "project", None, "unreadable"))
                continue
            remaining = settings.max_bytes - report.project_bytes_used
            prefix = data[:remaining]
            text = prefix.decode("utf-8", "replace")
            nonempty = bool(text.strip(RUST_WHITESPACE))
            if remaining == 0:
                status = "dropped-over-budget"
            elif not nonempty:
                status = "skipped-empty"
            elif len(data) > remaining:
                status = "partial"
            else:
                status = "loaded"
            if len(data) > remaining:
                report.truncations.append(cut_point(path, data, remaining))
            if nonempty:
                report.project_bytes_used += len(prefix)
                chunks.append(text)
            report.entries.append(entry(path, "project", len(data), status,
                                        len(prefix) if nonempty else 0,
                                        len(text.encode("utf-8")) if nonempty else 0))
            if hash_files:
                report.entries[-1]["sha256"] = hashlib.sha256(data).hexdigest()
    project = "\n\n".join(chunks)
    report.text = global_text + (SEPARATOR if global_text and project else "") + project
    cumulative, project_used = 0, 0
    for row in report.entries:
        cumulative += row["bytes_loaded"]
        if row["scope"] == "project":
            project_used += row["bytes_loaded"]
        row["cumulative_bytes"] = cumulative
        row["project_cumulative_bytes"] = project_used
    return report
