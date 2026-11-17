"""Strict unified-patch intake and exact in-memory application."""
from __future__ import annotations

import re

from .errors import FenceError, js_trim, require_that, utf16_slice
from .jsdiff import FilePatch, JsdiffError, apply_patch, parse_patch

MAX_PATCH_BYTES = 8 * 1024 * 1024
MAX_PATCH_FILES = 100
MAX_HUNKS_PER_FILE = 10_000
_MAX_SAFE_INTEGER = 2 ** 53 - 1

_UNSAFE_CHARS = re.compile(r"[\\\x00-\x1f\x7f]")
_DRIVE = re.compile(r"[A-Za-z]:")
_TYPESCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
_QUOTED_NAME = re.compile(r'"(?:[^"\\]|\\(?:["\\abfnrtv]|[0-7]{3}))*"')
_OCTAL_RUN = re.compile(r"(?:\\[0-7]{3})+")
_OCTAL = re.compile(r"\\([0-7]{3})")
# JavaScript's multiline '^' also matches after '\r', U+2028 and U+2029.
_UNSUPPORTED = re.compile("(?:^|(?<=[\n\r  ]))(?:GIT binary patch|diff --(?:cc|combined)|@@@)")
_HUNK_HEADER = re.compile("@@ -[0-9]+(?:,[0-9]+)? \\+[0-9]+(?:,[0-9]+)? @@[^\n\r  ]*")
_RULE = re.compile("=+")
_INDEX_NAME = re.compile("Index: [^\x00-\x1f]+")
_INDEX_LINE = re.compile("index [0-9a-f]{4,64}\\.\\.[0-9a-f]{4,64}(?: 100(?:644|755))?")
_EOF_MARKER = "\\ No newline at end of file"


def safe_relative(file) -> str:
    require_that(isinstance(file, str) and len(file) > 0 and not _UNSAFE_CHARS.search(file)
                 and not file.startswith("/") and not _DRIVE.match(file)
                 and all(part and part != "." and part != ".." and part.lower() != ".git"
                         for part in file.split("/")),
                 "unsafe_path", f"Unsafe repository-relative path: {_json_string(file)}")
    return file


def _json_string(value) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def is_typescript(file: str) -> bool:
    return file.endswith(_TYPESCRIPT_SUFFIXES)


def utf8_length(text: str) -> int:
    """UTF-8 byte length; an unpaired surrogate counts as three bytes, as in Node."""
    return len(text.encode("utf-8", "surrogatepass"))


def is_well_formed(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validate_quoted_name(name: str) -> None:
    if not name.startswith('"'):
        return
    require_that(_QUOTED_NAME.fullmatch(name) is not None, "invalid_patch", "Malformed Git-quoted filename")
    for match in _OCTAL_RUN.finditer(name):
        values = [int(part, 8) for part in _OCTAL.findall(match.group(0))]
        require_that(all(value <= 255 for value in values), "invalid_patch", "Git filename escape exceeds one byte")
        try:
            bytes(values).decode("utf-8")
        except UnicodeDecodeError:
            raise FenceError("invalid_patch", "Git filename octal escapes are not valid UTF-8") from None


# jsdiff intentionally tolerates prose, unknown extended headers and malformed
# metadata. A gate must account for the whole input. Validate the envelope and
# consume each hunk body exactly as the upstream parser represented it.
def _validate_envelope(text: str, files: list[FilePatch]) -> None:
    hunks = [hunk for file in files for hunk in file.hunks]
    lines = text.split("\n")
    hunk_index = 0
    old_headers = new_headers = 0
    pending_old = False
    pending_git = None
    raw_old_name = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@"):
            require_that(not pending_old and _HUNK_HEADER.fullmatch(line) is not None,
                         "invalid_patch", f"Malformed hunk header at patch line {i + 1}")
            hunk = hunks[hunk_index] if hunk_index < len(hunks) else None
            hunk_index += 1
            require_that(hunk, "invalid_patch", f"Unaccounted hunk at patch line {i + 1}")
            for body in hunk.lines:
                i += 1
                require_that(i < len(lines) and lines[i] == body, "invalid_patch",
                             f"Unaccounted hunk content at patch line {i + 1}")
            i += 1
            continue
        if line.startswith("--- "):
            require_that(not pending_old and old_headers == new_headers, "invalid_patch", "Repeated old file header")
            # Unquoted filenames are trimmed by jsdiff. Refuse ambiguous whitespace
            # rather than checking a different baseline file than the patch names.
            name = line[4:].split("\t", 1)[0]
            require_that(name == js_trim(name), "invalid_patch",
                         "Use Git-quoted paths for filenames with leading/trailing whitespace")
            _validate_quoted_name(name)
            raw_old_name = name
            old_headers += 1
            pending_old = True
            i += 1
            continue
        if line.startswith("+++ "):
            require_that(pending_old, "invalid_patch", "New file header must follow an old file header")
            name = line[4:].split("\t", 1)[0]
            require_that(name == js_trim(name), "invalid_patch",
                         "Use Git-quoted paths for filenames with leading/trailing whitespace")
            _validate_quoted_name(name)
            require_that(not pending_git or pending_git == f"diff --git {raw_old_name} {name}", "invalid_patch",
                         "Git descriptive paths do not match the unified file headers")
            pending_git = None
            new_headers += 1
            pending_old = False
            i += 1
            continue
        require_that(not pending_old, "invalid_patch", "Old and new file headers must be adjacent")
        if line.startswith("diff --git "):
            require_that(not pending_git, "invalid_patch", "Git section lacks unified file headers")
            pending_git = line
            i += 1
            continue
        if line == "" or _RULE.fullmatch(line) or _INDEX_NAME.fullmatch(line):
            i += 1
            continue
        if line.startswith("index "):
            require_that(_INDEX_LINE.fullmatch(line) is not None, "unsupported_patch",
                         f"Unsupported index metadata at patch line {i + 1}")
            i += 1
            continue
        raise FenceError("unsupported_patch", f"Unrecognized patch content at line {i + 1}: {utf16_slice(line, 0, 100)}")
    require_that(not pending_old and not pending_git and old_headers == len(files) and new_headers == len(files)
                 and hunk_index == len(hunks),
                 "invalid_patch", "Patch has missing or unaccounted headers/hunks")


def parse_candidate(text) -> list[FilePatch]:
    """Parse a unified patch of existing-file text modifications, or raise FenceError."""
    require_that(isinstance(text, str) and utf8_length(text) <= MAX_PATCH_BYTES,
                 "patch_limit", "Patch must be text no larger than 8 MiB")
    require_that(is_well_formed(text), "invalid_encoding", "Patch contains an unpaired UTF-16 surrogate")
    require_that("\0" not in text, "invalid_patch", "Patch contains a NUL byte")
    if not js_trim(text):
        return []
    require_that(not _UNSUPPORTED.search(text), "unsupported_patch", "Binary and combined patches are not supported")
    try:
        files = parse_patch(text)
    except JsdiffError as error:
        raise FenceError("invalid_patch", f"Invalid unified patch: {error}") from None
    require_that(0 < len(files) <= MAX_PATCH_FILES, "patch_limit", "Expected between 1 and 100 patched files")
    seen = set()
    for file in files:
        require_that(file.old_file_name and file.new_file_name, "invalid_patch",
                     "Every patch section needs old and new file headers")
        prefixed = file.old_file_name.startswith("a/") and file.new_file_name.startswith("b/")
        old_name = file.old_file_name[2:] if prefixed else file.old_file_name
        new_name = file.new_file_name[2:] if prefixed else file.new_file_name
        require_that(file.old_file_name != "/dev/null" and file.new_file_name != "/dev/null"
                     and not file.is_create and not file.is_delete and not file.is_rename
                     and not file.is_copy and not file.is_binary and old_name == new_name,
                     "unsupported_patch",
                     "Only text modifications to existing files are supported; creations, deletions, "
                     "renames, copies and binaries require separate review")
        file.path = safe_relative(old_name)
        require_that(file.path not in seen, "invalid_patch", f"Duplicate patch section for {file.path}")
        seen.add(file.path)
        require_that(file.old_mode is None and file.new_mode is None, "unsupported_patch",
                     f"Mode metadata requires separate review: {file.path}")
        require_that(len(file.hunks) > 0 or not file.is_git, "unsupported_patch",
                     f"Git patch section has no text hunks: {file.path}")
        require_that(len(file.hunks) <= MAX_HUNKS_PER_FILE, "patch_limit", f"Too many hunks for {file.path}")
    _validate_envelope(text, files)
    return files


def _count_lines(text: str) -> int:
    if len(text) == 0:
        return 0
    return len(text.split("\n")) - (1 if text.endswith("\n") else 0)


def _safe_integer(value) -> bool:
    return isinstance(value, int) and -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER


# The jsdiff port supplies parsing and in-memory application. This preflight
# removes its offset-search behaviour from the accepted contract: all old/context
# lines must already match at the exact declared old positions, without line
# conversion.
def apply_exactly(source: str, patch: FilePatch) -> str:
    lines = source.split("\n")
    old_line_count = _count_lines(source)
    for hunk in patch.hunks:
        require_that(all(_safe_integer(value) for value in (hunk.old_start, hunk.new_start, hunk.old_lines, hunk.new_lines))
                     and hunk.old_start >= 1 and hunk.new_start >= 1 and hunk.old_lines >= 0 and hunk.new_lines >= 0,
                     "invalid_patch", f"Invalid hunk coordinates in {patch.path}")
    new_line_count = old_line_count + sum(hunk.new_lines - hunk.old_lines for hunk in patch.hunks)
    require_that(_safe_integer(new_line_count) and new_line_count >= 0, "invalid_patch",
                 f"Invalid resulting line count in {patch.path}")
    delta = 0
    previous_end = 0
    old_eof_touched = new_eof_touched = False
    old_eof_marked = new_eof_marked = False
    for hunk_index, hunk in enumerate(patch.hunks):
        start = hunk.old_start - 1
        require_that(start >= previous_end and start <= old_line_count and hunk.new_start == hunk.old_start + delta,
                     "invalid_patch", f"Unordered or inconsistent hunk coordinates in {patch.path}")
        cursor = start
        new_cursor = hunk.new_start - 1
        previous_line = None
        for line in hunk.lines:
            operation = " " if line == "" else line[0]
            if operation == "\\":
                require_that(line == _EOF_MARKER, "invalid_patch", f"Unknown patch marker in {patch.path}")
                require_that(previous_line is not None and not previous_line["marked"]
                             and hunk_index == len(patch.hunks) - 1,
                             "invalid_patch", f"EOF marker must follow one data line in the final hunk: {patch.path}")
                if previous_line["operation"] != "+":
                    require_that(previous_line["old_line"] == old_line_count, "invalid_patch",
                                 f"Old EOF marker is not at end of file: {patch.path}")
                    require_that(not source.endswith("\n"), "patch_mismatch",
                                 f"Old EOF marker contradicts baseline newline: {patch.path}")
                    old_eof_marked = True
                if previous_line["operation"] != "-":
                    require_that(previous_line["new_line"] == new_line_count, "invalid_patch",
                                 f"New EOF marker is not at end of file: {patch.path}")
                    new_eof_marked = True
                previous_line["marked"] = True
                continue
            if operation in (" ", "-"):
                require_that(cursor < old_line_count and lines[cursor] == line[1:], "patch_mismatch",
                             f"Patch does not match {patch.path} at old line {cursor + 1}")
                cursor += 1
                if cursor == old_line_count:
                    old_eof_touched = True
            if operation in (" ", "+"):
                new_cursor += 1
                if new_cursor == new_line_count:
                    new_eof_touched = True
            previous_line = {"operation": operation, "old_line": cursor, "new_line": new_cursor, "marked": False}
        require_that(cursor == start + hunk.old_lines and new_cursor == hunk.new_start - 1 + hunk.new_lines,
                     "invalid_patch", f"Hunk size mismatch in {patch.path}")
        previous_end = cursor
        delta += hunk.new_lines - hunk.old_lines
    require_that(not old_eof_touched or source.endswith("\n") or old_eof_marked, "invalid_patch",
                 f"Missing old EOF newline marker in {patch.path}")
    result = apply_patch(source, patch)
    require_that(result is not False, "patch_mismatch", f"Patch could not be applied exactly in memory: {patch.path}")
    require_that(_count_lines(result) == new_line_count and (not new_eof_marked or not result.endswith("\n"))
                 and (not new_eof_touched or result.endswith("\n") or new_eof_marked),
                 "invalid_patch", f"Candidate EOF newline evidence is inconsistent in {patch.path}")
    return result
