"""Git and file observation for plain handoff packs (``relayproof-scope-v1``).

Port of the original relayproof ``snapshot.js``. This fingerprint is deliberately
separate from the in-toto receipts' ``git-tracked-and-nonignored-v1`` source
state (``git.py``): it omits sensitive paths, symlinks and files over 8 MiB so a
pack never carries their contents, records Git status letters and the branch,
and accepts an unborn HEAD. Both digests are stored on disk, so neither
definition can change without invalidating existing packs or receipts.

Git runs with optional locks, fsmonitor, the untracked cache and all configured
clean/smudge/process filters disabled, so inspection never writes the index or
runs repository-configured programs.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from typing import Any

from ..cache.jscompat import utf16_key
from .io import HandoffError
from .pack_safety import (
    SymlinkPathError,
    canonical,
    clipped,
    digest,
    inside,
    is_hash,
    js_trim,
    new_output,
    no_symlink_path,
    require,
    resolve,
    safe_name,
    sensitive_path,
    sha256_hex,
)

POLICY = "relayproof-scope-v1"
MAX_FILES = 20000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_GIT_OUTPUT = 32 * 1024 * 1024
GIT_TIMEOUT = 15
_filter_options: dict[str, list[str]] = {}


def git(repo: str, args: list[str], *, optional: bool = False, literal: bool = True) -> str | None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", LC_ALL="C")
    argv = [
        "git", "--no-pager", *(["--literal-pathspecs"] if literal else []),
        "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
        *_filter_options.get(repo, []), "-C", repo, *args,
    ]
    failed = "Git inspection failed (invalid repository, timeout, or output too large)"
    try:
        with tempfile.TemporaryFile() as out:
            result = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT, check=False)
            if out.tell() > MAX_GIT_OUTPUT:
                raise HandoffError(failed)
            out.seek(0)
            data = out.read()
    except FileNotFoundError:
        raise HandoffError("Git is required but was not found") from None
    except (subprocess.TimeoutExpired, OSError):
        raise HandoffError(failed) from None
    if result.returncode == 0:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HandoffError("Git output contains unsupported non-UTF-8 paths or metadata") from None
        return text[1:] if text.startswith("﻿") else text  # TextDecoder drops one BOM
    if optional:
        return None
    raise HandoffError(failed)


def repository(candidate: str) -> str:
    requested = no_symlink_path(candidate)
    require(stat.S_ISDIR(os.stat(requested).st_mode), "Repository path must be a directory")
    require(js_trim(git(requested, ["rev-parse", "--is-inside-work-tree"]) or "") == "true",
            "A non-bare Git worktree is required")
    root = no_symlink_path(re.sub(r"\n\Z", "", git(requested, ["rev-parse", "--show-toplevel"]) or ""))
    _disable_filters(root)
    return root


def _disable_filters(root: str) -> None:
    filters = git(root, ["config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
                  optional=True)
    names = dict.fromkeys(re.sub(r"\.(clean|smudge|process|required)\Z", "", key)
                          for key in (filters or "").split("\n") if key)
    _filter_options[root] = [option for name in names for option in (
        "-c", f"{name}.clean=", "-c", f"{name}.smudge=", "-c", f"{name}.process=", "-c", f"{name}.required=false")]


def output_path(candidate: str, repo: str) -> str:
    """A new output outside the worktree, or inside it at an untracked, Git-ignored path.

    The second form is the rule ``record`` and ``capture`` use: an ignored output
    cannot change the scope it describes.
    """
    absolute = no_symlink_path(candidate)
    if inside(repo, absolute):
        name = absolute[len(repo.rstrip("/")) + 1:]
        allowed = bool(name) and git(repo, ["ls-files", "--error-unmatch", "--", name], optional=True) is None
        allowed = allowed and git(repo, ["check-ignore", "-q", "--no-index", "--", name],
                                  optional=True, literal=False) is not None
        require(allowed, "Output must be outside the repository worktree or at a Git-ignored path inside it")
    return new_output(absolute)


def _parts(text: str | None) -> list[str]:
    return [part for part in (text or "").split("\0") if part]


def _read_file(absolute: str) -> bytes:
    fd = os.open(absolute, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        actual = os.fstat(fd)
        require(stat.S_ISREG(actual.st_mode) and actual.st_size <= MAX_FILE_BYTES, "File changed while reading")
        chunks, size = [], 0
        while size <= MAX_FILE_BYTES and (chunk := os.read(fd, 1024 * 1024)):
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    content = b"".join(chunks)
    require(len(content) <= MAX_FILE_BYTES, "File changed while reading")
    return content


def _scan(repo: str) -> dict[str, Any]:
    _disable_filters(repo)
    head = git(repo, ["rev-parse", "--verify", "HEAD"], optional=True)
    head = None if head is None else js_trim(head)
    branch = git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], optional=True)
    branch = None if branch is None else js_trim(branch)
    stages = []
    for line in _parts(git(repo, ["ls-files", "--stage", "-z"])):
        at = line.find("\t")
        require(at > 0, "Unsupported Git index entry")
        mode, obj, stage = (line[:at].split(" ") + [None, None])[:3]
        stages.append({"path": line[at + 1:], "mode": mode, "object": obj, "stage": stage})
    untracked = _parts(git(repo, ["ls-files", "--others", "--exclude-standard", "-z"]))
    status_text = git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
                             "--ignore-submodules=all"])
    status: dict[str, str] = {}
    for line in _parts(status_text):
        status[line[3:]] = line[:2]
    names = sorted(dict.fromkeys([*(entry["path"] for entry in stages), *untracked, *status]), key=utf16_key)
    require(len(names) <= MAX_FILES, f"Repository exceeds {MAX_FILES} files; narrow the worktree")
    index_by_path: dict[str, list[dict]] = {}
    for entry in stages:
        index_by_path.setdefault(entry["path"], []).append(
            {"mode": entry["mode"], "object": entry["object"], "stage": entry["stage"]})
    omitted: list[dict] = []
    files: list[dict] = []
    for name in names:
        absolute = resolve(repo, name)
        require(inside(repo, absolute) and name != "", "Unsafe repository path")
        display = safe_name(name)
        if sensitive_path(name):
            omitted.append({"path": display, "reason": "sensitive-path"})
            continue
        index = index_by_path.get(name, [])
        try:
            no_symlink_path(absolute)
            info = os.lstat(absolute)
        except FileNotFoundError:
            files.append({"path": display, "status": status.get(name, "  "), "index": index, "kind": "missing",
                          "sha256": None, "executable": None})
            continue
        except SymlinkPathError:
            omitted.append({"path": display, "reason": "symlink", "index": index})
            continue
        if not stat.S_ISREG(info.st_mode):
            reason = "directory-or-submodule" if stat.S_ISDIR(info.st_mode) else "non-regular-file"
            omitted.append({"path": display, "reason": reason, "index": index})
            continue
        if info.st_size > MAX_FILE_BYTES:
            omitted.append({"path": display, "reason": "file-over-8MiB", "index": index})
            continue
        content = _read_file(absolute)
        files.append({"path": display, "status": status.get(name, "  "), "index": index, "kind": "file",
                      "sha256": sha256_hex(content), "executable": bool(info.st_mode & 0o111)})
    state = {"policy": POLICY, "head": head, "branch": None if branch is None else safe_name(branch),
             "files": files, "omitted": omitted}
    return {**state, "fingerprint": digest(state)}


def snapshot(repo: str) -> dict[str, Any]:
    """Two observations that must agree; still not a transactional filesystem freeze."""
    first = _scan(repo)
    second = _scan(repo)
    require(first["fingerprint"] == second["fingerprint"],
            "Repository changed during snapshot; stop writers and retry")
    return second


_COMMIT = re.compile(r"[a-f0-9]{40,64}")
_MISSING = object()


def validate_snapshot(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict) and value.get("policy") == POLICY and is_hash(value.get("fingerprint"))
            and isinstance(value.get("files"), list) and isinstance(value.get("omitted"), list),
            "Invalid or unsupported snapshot")
    require(len(value["files"]) + len(value["omitted"]) <= MAX_FILES, "Snapshot exceeds file limit")
    head = value.get("head", _MISSING)
    require(head is None or (isinstance(head, str) and _COMMIT.fullmatch(head)), "Invalid commit")
    branch = value.get("branch", _MISSING)
    require(branch is None or isinstance(branch, str), "Invalid branch")
    for item in value["files"]:
        sha = item.get("sha256", _MISSING) if isinstance(item, dict) else _MISSING
        require(isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("status"), str)
                and isinstance(item.get("index"), list) and item.get("kind") in ("file", "missing")
                and (sha is None or is_hash(sha)), "Invalid file manifest")
    state = {key: item for key, item in value.items() if key != "fingerprint"}
    require(digest(state) == value["fingerprint"], "Snapshot integrity mismatch")
    return value


def changes(previous: dict, current: dict) -> list[str]:
    result = []
    if previous["head"] != current["head"]:
        result.append("HEAD changed")
    if previous["branch"] != current["branch"]:
        result.append("Branch changed")
    before = {item["path"]: item for item in previous["files"]}
    after = {item["path"]: item for item in current["files"]}
    for name in dict.fromkeys([*before, *after]):
        if canonical(before.get(name)) != canonical(after.get(name)):
            result.append(f"File/index changed: {name}")
    if canonical(previous["omitted"]) != canonical(current["omitted"]):
        result.append("Omitted scope changed")
    return result[:100]


def diffs(repo: str, state: dict) -> list[dict]:
    entries = []
    for item in [f for f in state["files"] if f["status"] != "  "][:100]:
        # A redacted path cannot be used as an input path. No untracked contents are copied.
        if "[path-id:" in item["path"] or item["status"] == "??":
            entries.append({"path": item["path"], "content": None, "reason": "manifest-only"})
            continue
        args = ["--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", "--src-prefix=a/",
                "--dst-prefix=b/", "--", item["path"]]
        try:
            parts = [git(repo, ["diff", "--cached", *args]), git(repo, ["diff", *args])]
        except (HandoffError, OSError):
            entries.append({"path": item["path"], "content": None, "reason": "diff-unavailable-or-too-large"})
            continue
        content = "\n".join(part for part in parts if part)
        entries.append({"path": item["path"], "content": clipped(content, 12000), "reason": "git-diff-redacted"})
    return entries
