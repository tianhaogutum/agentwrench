"""V4A grammar, independently implemented against the pinned Codex sources.

See THIRD_PARTY.md for semantics provenance and deliberate safety differences.
"""
from .model import FilePatch, Hunk, Patch, PatchError, validate_patch_size, validate_path


BEGIN = "*** Begin Patch"
END = "*** End Patch"
HEADERS = {f"*** {kind.title()} File: ": kind for kind in ("add", "delete", "update")}


def parse(text: str) -> Patch:
    if "\x00" in text:
        raise PatchError("NUL is not supported", text[:text.index('\x00')].count('\n') + 1)
    validate_patch_size(text)
    numbered = list(enumerate(text.replace("\r\n", "\n").split("\n"), 1))
    while numbered and not numbered[0][1].strip():
        numbered.pop(0)
    while numbered and not numbered[-1][1].strip():
        numbered.pop()
    if numbered and numbered[0][1].strip() in ("<<EOF", "<<'EOF'", '<<"EOF"'):
        if len(numbered) < 4 or not numbered[-1][1].rstrip().endswith("EOF"):
            raise PatchError("unclosed EOF heredoc wrapper", numbered[0][0])
        numbered = numbered[1:-1]
    if not numbered or numbered[0][1].strip() != BEGIN:
        raise PatchError(f"expected {BEGIN!r}", numbered[0][0] if numbered else 1)
    if numbered[-1][1].strip() != END:
        raise PatchError(f"expected {END!r}", numbered[-1][0])
    patch = Patch()
    current = None

    def finish(at: int) -> None:
        if current and current.kind == "update":
            if not current.hunks or not current.hunks[-1].lines:
                raise PatchError("update must contain a nonempty hunk", at)

    for number, raw in numbered[1:-1]:
        # Upstream trims headers outside updates; in updates a leading space is context.
        marker = raw.rstrip() if current and current.kind == "update" else raw.strip()
        found = next(((prefix, kind) for prefix, kind in HEADERS.items()
                      if marker.startswith(prefix)), None)
        if found:
            finish(number)
            prefix, kind = found
            original_path = raw.lstrip()[len(prefix):]
            if original_path != original_path.strip():
                raise PatchError("leading/trailing whitespace in paths is unsupported", number)
            path = validate_path(marker[len(prefix):], number)
            current = FilePatch(kind, path, line=number, end_line=number+1)
            patch.files.append(current)
            continue
        if current is None:
            raise PatchError("expected Add/Delete/Update File header (environment routing unsupported)", number)
        if current.kind == "add":
            if not raw.startswith("+"):
                raise PatchError("add content must start with '+'", number)
            current.contents.append(raw[1:])
            current.end_line = number+1
            continue
        if current.kind == "delete":
            raise PatchError("delete does not accept content", number)
        if marker.startswith("*** Move to: ") and not current.hunks and current.move_to is None:
            original_path = raw[len("*** Move to: "):]
            current.move_to = validate_path(original_path, number)
            current.end_line = number+1
        elif marker == "@@" or marker.startswith("@@ "):
            if current.hunks and not current.hunks[-1].lines:
                raise PatchError("empty update hunk", number)
            current.hunks.append(Hunk(anchor=marker[3:] if marker.startswith("@@ ") else None,
                                      line=number, end_line=number+1))
            current.end_line = number+1
        elif marker == "*** End of File":
            if not current.hunks or not current.hunks[-1].lines or current.hunks[-1].eof:
                raise PatchError("EOF marker requires a nonempty hunk", number)
            current.hunks[-1].eof = True
            current.hunks[-1].end_line = current.end_line = number+1
        elif current.hunks and current.hunks[-1].eof:
            if marker:
                raise PatchError("expected @@ or file header after EOF marker", number)
        elif raw == "" or raw[0] in " +-":
            if not current.hunks:
                current.hunks.append(Hunk(line=number))
            current.hunks[-1].lines.append((raw[0], raw[1:]) if raw else (" ", ""))
            current.hunks[-1].end_line = current.end_line = number+1
        else:
            raise PatchError("expected context ' ', addition '+', deletion '-', or @@", number)
    finish(numbered[-1][0])
    return patch
