"""Use native Git formats and diffs; hash worktree bytes without following links."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from .io import MAX_FILE, HandoffError, canonical, contained_file, digest, relative_path, stamp
from .models import Blob, FileState, IndexEntry, Snapshot

MAX_GIT = 8 * 1024 * 1024
MAX_FILES = 10000
MAX_TOTAL = 512 * 1024 * 1024


def git(root: Path, *args: str, allow_failure: bool = False, input_data: bytes | None = None) -> bytes:
    env = os.environ.copy()
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_DIFF_OPTS", "GIT_EXTERNAL_DIFF",
    ):
        env.pop(name, None)
    env.update(
        GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
        GIT_ATTR_NOSYSTEM="1",
    )
    filter_flags: list[str] = []
    if args[0] == "diff":
        # --no-textconv does not disable clean/process filters. Read names only,
        # then override their commands so inspection cannot execute a filter.
        raw = git(
            root, "config", "--null", "--name-only", "--get-regexp",
            r"^filter\..*\.(clean|smudge|process|required)$", allow_failure=True,
        )
        stems = {decode(key).rsplit(".", 1)[0] for key in raw.split(b"\0") if key}
        if len(stems) > 100 or any("=" in key or len(key) > 256 for key in stems):
            raise HandoffError("repository filter names exceed safe command-line bounds")
        for stem in sorted(stems):
            for suffix, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
                filter_flags.extend(["-c", f"{stem}.{suffix}={value}"])
    argv = [
        "git", "--no-optional-locks", *([] if args[0] == "check-ignore" else ["--literal-pathspecs"]), "-C", str(root),
        "-c", "core.fsmonitor=false", "-c", "diff.external=",
        "-c", "core.quotePath=true", "-c", "core.attributesFile=" + os.devnull,
        "-c", "diff.noprefix=false", "-c", "diff.mnemonicprefix=false",
        "-c", "diff.suppressBlankEmpty=false", *filter_flags, *args,
    ]
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result = subprocess.run(argv, input=input_data, env=env, stdout=out, stderr=err, timeout=20, check=False)
            if result.returncode:
                if allow_failure and result.returncode == 1:
                    return b""
                # Git's error can contain source paths, configs or credential-bearing URLs.
                raise HandoffError(f"Git {args[0]} failed (exit {result.returncode})")
            if out.tell() > MAX_GIT:
                raise HandoffError(f"Git output exceeds {MAX_GIT} bytes; narrow the repository")
            out.seek(0)
            return out.read(MAX_GIT + 1)
    except subprocess.TimeoutExpired as exc:
        raise HandoffError("Git command exceeded 20 seconds") from exc
    except OSError as exc:
        raise HandoffError(f"cannot run Git ({exc.strerror})") from exc


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise HandoffError("non-UTF-8 Git paths are not supported") from exc


def repository(path: Path) -> Path:
    if not path.is_dir():
        raise HandoffError("--repo must name an existing directory")
    # Git appends one LF; strip that delimiter, not LFs belonging to a directory.
    root = Path(decode(git(path.resolve(), "rev-parse", "--show-toplevel")).removesuffix("\n"))
    if git(root, "rev-parse", "--is-bare-repository").strip() != b"false":
        raise HandoffError("a non-bare working tree is required")
    return root.resolve()


def resolve_commit(root: Path, ref: str) -> str:
    if not ref or ref.startswith("-") or "\x00" in ref:
        raise HandoffError("invalid commit reference")
    value = decode(git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}", allow_failure=True)).strip()
    if not value:
        raise HandoffError("repository needs a committed HEAD" if ref == "HEAD" else "base must resolve to a locally available commit")
    if len(value) not in (40, 64) or set(value) - set("0123456789abcdef"):
        raise HandoffError("could not resolve a committed Git object")
    return value


def inspect_file(root: Path, name: str) -> tuple[FileState, tuple[int, ...] | None, int]:
    path = contained_file(root, name)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return FileState(path=name, kind="missing", sha256=None), None, 0
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(path)
        after = path.lstat()
        if stamp(before) != stamp(after):
            raise HandoffError("symlink changed during capture; retry")
        data = os.fsencode(target)
        return FileState(path=name, kind="symlink", sha256=digest(data)), stamp(after), len(data)
    if not stat.S_ISREG(before.st_mode):
        raise HandoffError("repository contains a directory, special file or submodule as a file")
    if before.st_size > MAX_FILE:
        raise HandoffError(f"repository file exceeds {MAX_FILE} bytes")
    total = 0
    hasher = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if stamp(before) != stamp(opened):
                raise HandoffError("file changed before hashing; retry")
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_FILE:
                    raise HandoffError(f"repository file exceeds {MAX_FILE} bytes")
                hasher.update(chunk)
            after = os.fstat(handle.fileno())
        if stamp(before) != stamp(after) or stamp(after) != stamp(path.lstat()):
            raise HandoffError("file changed during hashing; retry")
    except OSError as exc:
        raise HandoffError(f"cannot hash repository file ({exc.strerror})") from exc
    return (
        FileState(
            path=name, kind="file", sha256=hasher.hexdigest(),
            executable=bool(before.st_mode & stat.S_IXUSR),
        ),
        stamp(after),
        total,
    )


DIFF = (
    "--patch", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
    "--no-color", "--no-renames", "--no-relative", "--src-prefix=a/", "--dst-prefix=b/",
    "--line-prefix=", "--diff-algorithm=myers", "--no-indent-heuristic",
    "--unified=3", "--inter-hunk-context=0",
)


def check_blob_sizes(root: Path, commits: list[str], extra_oids: list[str] | None = None) -> None:
    """Read native object headers before diff could allocate a large blob/delta."""
    oids = set(extra_oids or [])
    for commit in commits:
        for entry in git(root, "ls-tree", "-r", "-z", commit).split(b"\0"):
            if not entry:
                continue
            header, _ = entry.split(b"\t", 1)
            _, kind, oid = header.decode("ascii").split(" ")
            if kind == "blob":
                oids.add(oid)
    if len(oids) > 20000:
        raise HandoffError("diff input exceeds 20000 unique Git blobs")
    if not oids:
        return
    result = git(
        root, "cat-file", "--batch-check=%(objecttype) %(objectsize)",
        input_data="".join(oid + "\n" for oid in sorted(oids)).encode("ascii"),
    ).splitlines()
    if len(result) != len(oids):
        raise HandoffError("Git object size information is incomplete")
    total = 0
    for line in result:
        fields = line.split()
        if len(fields) != 2 or fields[0] != b"blob" or not fields[1].isdigit():
            raise HandoffError("a required Git blob is unavailable or invalid")
        size = int(fields[1])
        if size > MAX_FILE:
            raise HandoffError(f"Git blob exceeds {MAX_FILE} bytes; binary diff was not started")
        total += size
        if total > MAX_TOTAL:
            raise HandoffError("diff input Git blobs exceed 512 MiB total bytes")


def _git_inventory(root: Path) -> tuple[str, bytes, bytes]:
    head = resolve_commit(root, "HEAD")
    index = git(root, "ls-files", "--stage", "-z")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return head, index, untracked


def snapshot(root: Path) -> Snapshot:
    before = _git_inventory(root)
    head, raw_index, raw_untracked = before
    entries: list[IndexEntry] = []
    names: set[str] = set()
    for entry in raw_index.split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, oid, stage = header.decode("ascii").split(" ")
        name = relative_path(decode(raw_path))
        if mode == "160000":
            raise HandoffError("submodules are not supported; capture their repositories separately")
        if stage != "0":
            raise HandoffError("unmerged index entries are not supported; resolve conflicts first")
        entries.append(IndexEntry(path=name, mode=mode, oid=oid))
        names.add(name)
    for raw in raw_untracked.split(b"\0"):
        if raw:
            names.add(relative_path(decode(raw)))
    if len(names) > MAX_FILES:
        raise HandoffError(f"repository exceeds {MAX_FILES} source files")
    states: list[FileState] = []
    stamps: dict[str, tuple[int, ...] | None] = {}
    observations = {}
    total = 0
    for name in sorted(names):
        state, fingerprint, size = inspect_file(root, name)
        total += size
        if total > MAX_TOTAL:
            raise HandoffError(f"source files exceed {MAX_TOTAL} total bytes")
        states.append(state)
        stamps[name] = fingerprint
        observations[name] = (fingerprint, size)
    # Bound both current files and the old/index objects before native diff.
    check_blob_sizes(root, [head], [entry.oid for entry in entries])
    staged = git(root, "diff", *DIFF, "--cached", head, "--")
    unstaged = git(root, "diff", *DIFF, "--")
    if _git_inventory(root) != before:
        raise HandoffError("Git state changed during capture; retry in a quiet working tree")
    for name, fingerprint in stamps.items():
        try:
            actual = stamp(contained_file(root, name).lstat())
        except FileNotFoundError:
            actual = None
        if actual != fingerprint:
            raise HandoffError("source file changed during capture; retry in a quiet working tree")
    branch = decode(git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)).strip()
    result = Snapshot(
        head=head, branch_hint=branch or None, files=states,
        index=sorted(entries, key=lambda e: e.path), staged_diff=Blob.from_bytes(staged),
        unstaged_diff=Blob.from_bytes(unstaged), state_sha256="0" * 64,
    )
    result.state_sha256 = digest(canonical(result.state_data()))
    result._observed_stats = observations
    result.check()
    return result


def safe_output(root: Path, path: Path) -> None:
    """Outputs must not change the very source state that they describe."""
    if path.is_symlink():
        raise HandoffError("output cannot be a symlink")
    absolute = path.parent.resolve() / path.name
    try:
        name = absolute.relative_to(root).as_posix()
    except ValueError:
        return
    relative_path(name)
    contained_file(root, name)
    if git(root, "ls-files", "--error-unmatch", "--", name, allow_failure=True):
        raise HandoffError("output is tracked by Git; select an ignored or outside-repo output")
    if not git(root, "check-ignore", "--no-index", "--", name, allow_failure=True):
        raise HandoffError("output inside the repository must be Git-ignored to avoid self-reference")
