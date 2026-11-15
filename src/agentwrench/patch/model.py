"""Public AST and structured errors. No filesystem effects."""
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
import re


class PatchError(ValueError):
    """Invalid syntax, unsafe path, or unsupported representation (CLI exit 2)."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)


class ApplyError(Exception):
    """Application/IO failure; no planned changes were committed (CLI exit 1)."""

    def __init__(self, message: str, diagnostics: list[dict] | None = None):
        self.diagnostics = diagnostics or []
        super().__init__(message)


def validate_patch_size(text: str, *, label: str = "patch") -> None:
    """Use the same UTF-8 byte and LF budget for patch input and conversion."""
    if len(text.encode("utf-8")) > 8 * 1024 * 1024 or text.count("\n") > 100_000:
        raise PatchError(f"{label} exceeds 8 MiB or 100,000-line limit")


def validate_path(path: str, line: int | None = None) -> str:
    if (not path or path != path.strip() or path.startswith(("/", "\\")) or "\\" in path
            or re.match(r"^[A-Za-z]:", path)
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(p in ("", ".", "..", ".git") for p in path.split("/"))):
        raise PatchError(f"unsafe or unsupported relative path: {path!r}", line)
    return path


@dataclass
class Hunk:
    lines: list[tuple[str, str]] = field(default_factory=list)
    anchor: str | None = None
    eof: bool = False
    line: int = 0
    end_line: int = 0  # Exclusive physical patch line; zero for an unlocated AST.

    @property
    def old(self) -> list[str]:
        return [s for op, s in self.lines if op != "+"]

    @property
    def new(self) -> list[str]:
        return [s for op, s in self.lines if op != "-"]


@dataclass
class FilePatch:
    kind: str
    path: str
    move_to: str | None = None
    contents: list[str] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)
    line: int = 0
    end_line: int = 0


@dataclass
class Patch:
    files: list[FilePatch] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_text(self) -> str:
        result = ["*** Begin Patch"]
        for f in self.files:
            result.append(f"*** {f.kind.title()} File: {f.path}")
            if f.kind == "add":
                result.extend("+" + s for s in f.contents)
            if f.move_to:
                result.append("*** Move to: " + f.move_to)
            for h in f.hunks:
                result.append("@@" + (" " + h.anchor if h.anchor is not None else ""))
                result.extend(op + s for op, s in h.lines)
                if h.eof:
                    result.append("*** End of File")
        return "\n".join(result + ["*** End Patch", ""])


def validate_patch_paths(patch: Patch) -> list[str]:
    """Validate the complete file path set without accessing a target tree."""
    if len(patch.files) > 1000:
        raise PatchError('at most 1000 file operations supported')
    paths = [path for f in patch.files for path in ([f.path, f.move_to] if f.move_to else [f.path])]
    seen = set()
    for path in paths:
        validate_path(path)
        if path in seen:
            raise PatchError(f'a path may be touched only once per patch: {path}')
        seen.add(path)
    ordered = sorted(paths)
    for path in paths:
        prefix = path + '/'
        # The first possible descendant suffices, even with intervening names
        # like node-other. Compare full segments, so node2/child stays valid.
        index = bisect_left(ordered, prefix)
        if index < len(ordered) and ordered[index].startswith(prefix):
            raise PatchError(f'conflicting file paths: {path} and {ordered[index]} (file/descendant overlap)')
    return paths
