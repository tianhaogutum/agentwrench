"""Trusted, read-only baseline readers: a directory or an immutable Git tree."""
from __future__ import annotations

import os
import re
import stat
import subprocess
import threading

from .errors import FenceError, SystemCallError, js_trim, require_that, utf16_slice
from .patch import safe_relative

MAX_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 10.0
_TREE_ID = re.compile(r"[0-9a-f]{40,64}")
_REGULAR_ENTRY = re.compile(r"100(?:644|755) blob ")

# Hooks and parent Git processes often export these. The explicit --repo
# argument, rather than an inherited object store or namespace, owns evidence.
_INHERITED_GIT_VARIABLES = (
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_PREFIX",
)


def decode_text(data: bytes, name: str) -> str:
    require_that(len(data) <= MAX_BYTES, "source_limit", f"{name} exceeds the 1 MiB source limit")
    try:
        # Strict UTF-8; a leading byte order mark is kept as U+FEFF.
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise FenceError("invalid_encoding", f"{name} is not valid UTF-8") from None


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path, strict=True)
    except OSError as error:
        # Name the first component that fails, as Node's realpathSync does.
        failing = os.path.abspath(path)
        current = os.path.abspath(os.sep)
        for part in failing.split(os.sep):
            if not part:
                continue
            current = os.path.join(current, part)
            try:
                os.lstat(current)
            except OSError as component_error:
                raise SystemCallError(component_error, "lstat", current) from None
        raise SystemCallError(error, "realpath", path) from None


class DirectoryBaseline:
    """Read baseline files below ``root``; symlinks anywhere on the path are refused."""

    def __init__(self, root: str):
        self.root = _realpath(os.fspath(root))
        try:
            is_directory = stat.S_ISDIR(os.stat(self.root).st_mode)
        except OSError as error:
            raise SystemCallError(error, "stat", self.root) from None
        require_that(is_directory, "invalid_baseline", "Baseline root must be a directory")
        self.identity = {"kind": "directory"}

    def __call__(self, file: str) -> str:
        safe_relative(file)
        current = self.root
        for segment in file.split("/"):
            current = os.path.join(current, segment)
            try:
                info = os.lstat(current)
            except OSError as error:
                raise SystemCallError(error, "lstat", current) from None
            require_that(not stat.S_ISLNK(info.st_mode), "unsafe_baseline", f"Baseline path traverses a symlink: {file}")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(current, flags)
        except OSError as error:
            raise SystemCallError(error, "open", current) from None
        try:
            info = os.fstat(fd)
            require_that(stat.S_ISREG(info.st_mode), "invalid_baseline", f"Baseline path is not a regular file: {file}")
            require_that(info.st_size <= MAX_BYTES, "source_limit", f"{file} exceeds the 1 MiB source limit")
            chunks = []
            total = 0
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    break  # grew while reading; decode_text reports the limit
            return decode_text(b"".join(chunks), file)
        except OSError as error:
            raise SystemCallError(error, "read", current) from None
        finally:
            os.close(fd)


def directory_baseline(root: str) -> DirectoryBaseline:
    return DirectoryBaseline(root)


def _git_environment() -> dict:
    env = {key: value for key, value in os.environ.items() if key not in _INHERITED_GIT_VARIABLES}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
                "GIT_ALLOW_PROTOCOL": ""})
    return env


def _git_failure(stderr: bytes | None, fallback: str) -> FenceError:
    text = fallback if stderr is None else stderr.decode("utf-8", "replace")
    return FenceError("git_baseline", f"Cannot read Git baseline: {utf16_slice(js_trim(text), 0, 500)}")


def git(repo: str, args: list[str]) -> bytes:
    """Run Git without a shell; stdout is bounded like the original's maxBuffer."""
    limit = MAX_BYTES + 1024
    try:
        process = subprocess.Popen(["git", "--no-pager", "-C", repo, *args], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_environment())
    except OSError as error:
        code = SystemCallError(error, "spawnSync git").code
        raise _git_failure(None, f"spawnSync git {code}") from None
    except ValueError as error:  # e.g. an argument containing NUL
        raise _git_failure(None, str(error)) from None
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    overflow = threading.Event()

    def drain_stdout():
        total = 0
        while chunk := process.stdout.read(64 * 1024):
            total += len(chunk)
            if total > limit:
                overflow.set()
                process.kill()
                break
            stdout.append(chunk)

    def drain_stderr():
        kept = 0
        while chunk := process.stderr.read(64 * 1024):
            if kept < limit:
                stderr.append(chunk)
                kept += len(chunk)

    readers = [threading.Thread(target=drain_stdout, daemon=True), threading.Thread(target=drain_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    try:
        status = process.wait(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        status = None
    for reader in readers:
        reader.join()
    process.stdout.close()
    process.stderr.close()
    if status != 0 or overflow.is_set():
        raise _git_failure(b"".join(stderr), "")
    return b"".join(stdout)


class GitBaseline:
    """Read blobs from one tree, resolved once from ``revision``. Never touches the index or worktree."""

    def __init__(self, repo: str, revision: str):
        self.repo = _realpath(os.fspath(repo))
        # Resolve once to an immutable tree. '--end-of-options' keeps revision text
        # from being interpreted as command flags.
        output = git(self.repo, ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{tree}}"])
        self.tree = js_trim(output.decode("ascii", "replace"))
        require_that(_TREE_ID.fullmatch(self.tree) is not None, "git_baseline", "Git revision did not resolve to a tree")
        self.identity = {"kind": "git", "tree": self.tree}

    def __call__(self, file: str) -> str:
        safe_relative(file)
        entry = git(self.repo, ["ls-tree", "--full-tree", "-z", self.tree, "--", f":(literal){file}"])
        require_that(_REGULAR_ENTRY.match(entry.decode("utf-8", "replace")) is not None, "unsafe_baseline",
                     f"Baseline entry is missing or is not a regular file: {file}")
        return decode_text(git(self.repo, ["cat-file", "blob", f"{self.tree}:{file}"]), file)


def git_baseline(repo: str, revision: str) -> GitBaseline:
    return GitBaseline(repo, revision)
