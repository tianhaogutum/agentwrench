"""Matching, diagnostics, and side-effect-free planning."""
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
import stat

from .model import ApplyError, FilePatch, Hunk, Patch, PatchError, validate_patch_paths, validate_path

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
PUNCTUATION = {}
for chars, replacement in [
    ("‐‑‒–—―−", "-"), ("‘’‚‛", "'"), ("“”„‟", '"'),
    ("\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000", " "),
]:
    PUNCTUATION.update({ord(c): replacement for c in chars})
TIERS = [("exact", lambda s: s), ("rstrip", str.rstrip), ("trim", str.strip),
         ("unicode", lambda s: s.strip().translate(PUNCTUATION))]


@dataclass
class Match:
    index: int
    tier: str
    candidates: list[int]
    eof_fallback: bool = False


def seek(lines: list[str], pattern: list[str], start: int = 0, eof: bool = False) -> Match | None:
    if not pattern:
        return Match(start, "exact", [start]) if start <= len(lines) else None
    end = len(lines) - len(pattern)
    if end < start:
        return None
    if len(pattern) * (end-start+1) > 10_000_000:
        raise PatchError("matching work exceeds 10 million line comparisons; use smaller hunks/files")
    # EOF preference is a full four-tier stage, followed by ordinary four-tier search.
    stages = [(range(end, end + 1), False), (range(start, end + 1), True)] if eof else [(range(start, end + 1), False)]
    for positions, fallback in stages:
        for name, normalize in TIERS:
            normalized = [normalize(x) for x in pattern]
            matches = []
            for i in positions:
                if all(normalize(lines[i+j]) == p for j, p in enumerate(normalized)):
                    matches.append(i)
                    if len(matches) >= 100:
                        break
            if matches:
                all_matches = matches
                if eof and not fallback:
                    all_matches = []
                    for i in range(start, end+1):
                        if all(normalize(lines[i+j]) == p for j, p in enumerate(normalized)):
                            all_matches.append(i)
                            if len(all_matches) >= 100:
                                break
                return Match(matches[0], name, all_matches, fallback)
    return None


def safe_path(root: Path, relative: str) -> Path:
    validate_path(relative)
    result = root / relative
    cursor = root
    for part in relative.split("/"):
        cursor = cursor / part
        if cursor.is_symlink():
            raise PatchError(f"symlink paths are forbidden: {relative}")
        if cursor.exists() and cursor != result and not cursor.is_dir():
            raise ApplyError(f"parent is not a directory: {relative}")
    if result.exists() and not result.is_file():
        raise PatchError(f"not a regular file: {relative}")
    return result


@dataclass
class Snapshot:
    data: bytes | None
    mode: int = 0o644


@dataclass
class Change:
    source: str
    destination: str | None
    before: bytes | None
    after: bytes | None
    mode: int
    kind: str


@dataclass
class Plan:
    root: Path
    changes: list[Change]
    snapshots: dict[str, Snapshot]
    diagnostics: list[dict]

    @property
    def ok(self) -> bool:
        return all(d["status"] != "failed" for d in self.diagnostics)


def validate_line_limit(text: str, path: str, *, kind: str = 'source') -> None:
    """Count logical newline endings, with CRLF pairs counting once."""
    if text.count("\n") + text.count("\r") - text.count("\r\n") > 100_000:
        raise PatchError(f"{path}: {kind} exceeds 100,000-line limit")


@dataclass
class TextFile:
    lines: list[str]
    endings: list[str]
    bom: bool
    preferred: str

    @classmethod
    def read(cls, data: bytes, path: str) -> 'TextFile':
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchError(f"{path}: only UTF-8 text supported") from exc
        if "\x00" in text:
            raise PatchError(f"{path}: binary/NUL content unsupported")
        bom = text.startswith("\ufeff")
        if bom:
            text = text[1:]
        validate_line_limit(text, path)
        parts = re.split(r"(\r\n|\n|\r)", text)
        lines, endings = [], []
        for i in range(0, len(parts)-1, 2):
            lines.append(parts[i])
            endings.append(parts[i+1])
        if parts[-1]:
            lines.append(parts[-1])
            endings.append("")
        return cls(lines, endings, bom, next((e for e in endings if e), "\n"))


def candidate(lines: list[str], pattern: list[str], start: int) -> tuple[int, int, float]:
    """Bound diagnostic work: at most 2,000 windows, 80 lines and 8K chars each."""
    width = min(len(pattern), 80)
    last = max(start, len(lines)-width)
    step = max(1, (last-start+1+1999)//2000)
    target = "\n".join(pattern[:80])[:8192]
    best = (min(start, len(lines)), min(start+width, len(lines)), 0.0)
    for i in range(start, min(last+1, len(lines)+1), step):
        value = "\n".join(lines[i:i+width])[:8192]
        score = SequenceMatcher(None, target, value).ratio()
        if score > best[2]:
            best = (i, min(i+width, len(lines)), score)
    return best


def patch_span(node: Hunk | FilePatch) -> dict | None:
    """Parsed physical patch lines, one-based and half-open (including markers)."""
    return {'start_line': node.line, 'end_line': node.end_line} if node.line and node.end_line else None


def file_span(path: str, start: int, count: int) -> dict:
    """Convert a zero-based logical line range to a one-based half-open span."""
    return {'path': path, 'start_line': start+1, 'end_line': start+count+1}


def diagnose(file: TextFile, path: str, number: int, pattern: list[str], start: int,
             match: Match | None, reason: str = "context not found") -> dict:
    if match:
        a, b, score = match.index, match.index + len(pattern), 1.0
    else:
        a, b, score = candidate(file.lines, pattern, start)
    actual = file.lines[a:b]
    flags = {
        "bom": file.bom,
        "crlf": "\r\n" in file.endings,
        "missing_final_newline": bool(file.endings and not file.endings[-1]),
        "trailing_whitespace": actual != pattern and [s.rstrip() for s in actual] == [s.rstrip() for s in pattern],
        "indentation_tabs_vs_spaces": actual != pattern and any("\t" in s for s in actual+pattern)
            and [s.expandtabs(8).rstrip() for s in actual] == [s.expandtabs(8).rstrip() for s in pattern],
        "context_drift": match is None,
        "ambiguous_match": bool(match and len(match.candidates) > 1),
    }
    fix = ("Refresh context from the candidate range; check the @@ anchor and hunk order."
           if match is None else "Match succeeds; original context bytes, BOM and line endings are preserved.")
    if flags["ambiguous_match"]:
        fix = "Add unique surrounding context or an @@ anchor; the first eligible match is selected (EOF prefers tail)."
    return {"path": path, "hunk": number, "status": "matched" if match else "failed",
            "reason": "context matched" if match else reason,
            "tier": match.tier if match else None,
            "candidate": {"start_line": a+1, "end_line": max(a+1, b), "similarity": round(score, 4)},
            "diagnosis": flags, "suggested_fix": fix,
            "matches": [i+1 for i in match.candidates[:100]] if match else [],
            "eof_fallback": bool(match and match.eof_fallback)}


def update(file: TextFile, operation: FilePatch) -> tuple[bytes, list[dict]]:
    replacements = []
    reports = []
    cursor = 0
    line_delta = 0

    def report(number: int, hunk: Hunk, pattern: list[str], match: Match | None,
               code: str, reason: str = 'context not found', new_count: int = 0) -> dict:
        diagnosis = diagnose(file, operation.path, number, pattern, cursor, match, reason)
        diagnosis.update(code=code, destination_path=operation.move_to or operation.path,
                         patch_span=patch_span(hunk),
                         source_span=file_span(operation.path, match.index, len(pattern)) if match else None,
                         result_span=file_span(operation.move_to or operation.path,
                                               match.index + line_delta, new_count) if match else None)
        return diagnosis

    for number, hunk in enumerate(operation.hunks, 1):
        if hunk.anchor is not None:
            anchor = seek(file.lines, [hunk.anchor], cursor)
            if anchor is None:
                reports.append(report(number, hunk, [hunk.anchor], None, 'ANCHOR_NOT_FOUND', '@@ anchor not found'))
                continue
            cursor = anchor.index + 1
        pattern = hunk.old
        ops = hunk.lines
        if not pattern:
            match = Match(len(file.lines), "exact", [len(file.lines)])
        else:
            match = seek(file.lines, pattern, cursor, hunk.eof)
            if match is None and pattern[-1] == "":
                # Upstream's final empty-context sentinel tolerance.
                pattern = pattern[:-1]
                ops = list(ops)
                old_tail = max(j for j, (op, _) in enumerate(ops) if op != '+')
                new_tail = next((j for j in range(len(ops)-1, -1, -1) if ops[j][0] != '-'), None)
                trimmed = []
                for j, (op, text) in enumerate(ops):
                    keep_old = op != '+' and j != old_tail
                    keep_new = op != '-' and not (j == new_tail and text == '')
                    if keep_old or keep_new:
                        trimmed.append((' ' if keep_old and keep_new else '-' if keep_old else '+', text))
                ops = trimmed
                match = seek(file.lines, pattern, cursor, hunk.eof)
        new_count = sum(op == ' ' for op, _ in ops)
        for op, text in ops:
            if op == '+':
                # A V4A payload may itself contain bare CRs. Count the actual
                # terminated text, including CRLF pairs, like TextFile.read.
                terminated = text + file.preferred
                new_count += terminated.count('\n') + terminated.count('\r') - terminated.count('\r\n')
        reports.append(report(number, hunk, pattern, match,
                              'HUNK_MATCHED' if match else 'CONTEXT_NOT_FOUND', new_count=new_count))
        if match is None:
            continue
        old_index = match.index
        segment_start = old_index
        removed = 0
        inserted = []
        for op, text in ops:
            if op == " ":
                if removed or inserted:
                    replacements.append((segment_start, removed, inserted))
                old_index += 1
                segment_start, removed, inserted = old_index, 0, []
            elif op == "-":
                old_index += 1
                removed += 1
            else:
                inserted.append(text)
        if removed or inserted:
            replacements.append((segment_start, removed, inserted))
        cursor = match.index + len(pattern)
        line_delta += new_count - len(pattern)
    if any(d["status"] == "failed" for d in reports):
        # No result exists for this file if even one hunk failed. Matched source
        # spans remain exact; candidate windows on failures are only suggestions.
        for d in reports:
            d['result_span'] = None
        return b"", reports
    result = []
    cursor = 0
    for start, count, inserted in sorted(replacements, key=lambda r: r[0]):
        if start < cursor:
            raise PatchError(f"{operation.path}: overlapping hunks unsupported")
        result.extend((file.lines[i], file.endings[i]) for i in range(cursor, start))
        result.extend((s, file.preferred) for s in inserted)
        cursor = start + count
    result.extend(zip(file.lines[cursor:], file.endings[cursor:]))
    text = ("\ufeff" if file.bom else "") + "".join(s + (e or file.preferred) for s, e in result)
    validate_line_limit(text, operation.move_to or operation.path, kind='result')
    return text.encode("utf-8"), reports


def plan(patch: Patch, root: str | Path) -> Plan:
    validate_patch_paths(patch)
    root = Path(root).resolve()
    if not root.is_dir():
        raise PatchError(f"root is not a directory: {root}")
    snapshots = {}
    total = 0
    # Check every path before content evaluation or writes.
    for f in patch.files:
        paths = [f.path] + ([f.move_to] if f.move_to else [])
        for relative in paths:
            path = safe_path(root, relative)
            if path.exists():
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    raise PatchError(f"{relative}: file exceeds 8 MiB limit")
                remaining = MAX_TOTAL_BYTES - total
                if size > remaining:
                    raise PatchError('total source data exceeds 64 MiB limit')
                # Stat is only an early refusal: bound post-stat growth too.
                with path.open('rb') as handle:
                    data = handle.read(min(MAX_FILE_BYTES, remaining) + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise PatchError(f"{relative}: file exceeds 8 MiB limit")
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise PatchError('total source data exceeds 64 MiB limit')
                snapshots[relative] = Snapshot(data, stat.S_IMODE(path.stat().st_mode))
            else:
                snapshots[relative] = Snapshot(None)
    changes, diagnostics = [], []
    total_after = 0
    for f in patch.files:
        before = snapshots[f.path]
        problem = None
        if f.kind == "add" and before.data is not None:
            problem = "add target already exists"
            code = 'ADD_TARGET_EXISTS'
        elif f.kind != "add" and before.data is None:
            problem = "source file does not exist"
            code = 'SOURCE_MISSING'
        elif f.move_to and snapshots[f.move_to].data is not None:
            problem = "move target already exists"
            code = 'MOVE_TARGET_EXISTS'
        if problem:
            diagnostics.append({"path": f.path, "hunk": None, "status": "failed", "reason": problem,
                                "code": code, "destination_path": f.move_to or (None if f.kind == 'delete' else f.path),
                                "patch_span": patch_span(f), "source_span": None, "result_span": None,
                                "suggested_fix": "Correct the path or refresh the patch against this tree."})
            continue
        if f.kind == "add":
            after = "".join(s + "\n" for s in f.contents).encode("utf-8")
            TextFile.read(after, f.path)
        elif f.kind == "delete":
            TextFile.read(before.data, f.path)
            after = None
        elif f.kind == "update":
            after, reports = update(TextFile.read(before.data, f.path), f)
            diagnostics.extend(reports)
        else:
            raise PatchError(f"unknown operation: {f.kind}")
        if after is not None and len(after) > MAX_FILE_BYTES:
            raise PatchError(f"{f.path}: result exceeds 8 MiB limit")
        total_after += len(after) if after is not None else 0
        if total_after > MAX_TOTAL_BYTES:
            raise PatchError("total result data exceeds 64 MiB limit")
        changes.append(Change(f.path, f.move_to or (f.path if after is not None else None),
                              before.data, after, before.mode, f.kind))
    return Plan(root, changes, snapshots, diagnostics)
