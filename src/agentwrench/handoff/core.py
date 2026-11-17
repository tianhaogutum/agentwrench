"""Compose task claims, Git state and local in-toto receipts into one JSON."""

from __future__ import annotations

from contextlib import chdir
from datetime import UTC, datetime
from pathlib import Path

from in_toto.models.link import Link
from in_toto.models.metadata import Envelope, Metablock
from in_toto.runlib import record_artifacts_as_dict

from .evidence import RECEIPT_KEY, assess, declared_paths, load_evidence, load_signer
from .git import (
    DIFF,
    MAX_TOTAL,
    check_blob_sizes,
    git,
    inspect_file,
    repository,
    resolve_commit,
    safe_output,
    snapshot,
)
from .io import (
    MAX_LOG,
    HandoffError,
    atomic_bytes,
    atomic_json,
    command_argv,
    contained_file,
    digest,
    prepare_output,
    read_bytes,
    read_json,
    stamp,
)
from .models import Blob, ChangeRange, FileState, Manifest, Snapshot, Task, VerificationCommand
from .process import execute

LIMITATIONS = [
    "Task text and claimed completion are unverified statements, not execution evidence.",
    "Source scope is Git-tracked and nonignored untracked files; ignored dependencies, environment and external services are not captured.",
    "An unkeyed manifest digest detects accidental edits; it does not authenticate an author.",
    "Imported logs without a source-state receipt from `agentwrench handoff record` cannot establish which full source state was executed.",
    "Commands in this bundle are inert data. Verification never executes them.",
    "Snapshots are optimistic checks of a quiet working tree, not an atomic filesystem freeze.",
]


def observe_artifacts(
    root: Path, paths: set[str], source: Snapshot | None = None,
) -> tuple[list[FileState], dict[str, tuple[int, ...] | None]]:
    if len(paths) > 10000:
        raise HandoffError("combined artifact scope exceeds 10000 paths")
    observed = []
    fingerprints = {}
    total = 0
    source_files = {item.path: item for item in source.files} if source else {}
    for path in sorted(paths):
        cached = source._observed_stats.get(path) if source else None
        if cached is not None:
            fingerprint, size = cached
            try:
                actual = stamp(contained_file(root, path).lstat())
            except FileNotFoundError:
                actual = None
            if actual != fingerprint:
                raise HandoffError("a source artifact changed after the snapshot; retry")
            state = source_files[path]
        else:
            state, fingerprint, size = inspect_file(root, path)
        total += size
        if total > MAX_TOTAL:
            raise HandoffError("observed artifacts exceed 512 MiB total bytes")
        observed.append(state)
        fingerprints[path] = fingerprint
    return observed, fingerprints


def check_artifacts_stable(root: Path, fingerprints: dict) -> None:
    for path, fingerprint in fingerprints.items():
        try:
            actual = stamp(contained_file(root, path).lstat())
        except FileNotFoundError:
            actual = None
        if actual != fingerprint:
            raise HandoffError("an observed artifact changed during the operation; retry")


def record_hashes(root: Path, paths: list[str]) -> dict:
    try:
        # Upstream FileResolver mutates cwd and may fail before restoring it.
        with chdir(root):
            return record_artifacts_as_dict(paths, base_path=str(root), exclude_patterns=[".git"])
    except Exception as exc:
        raise HandoffError("in-toto artifact hashing failed; check file access and retry in a quiet working tree") from exc


def capture(
    *, repo: Path, task_path: Path, logs: list[Path], output: Path,
    base: str | None = None, supplied_diff: Path | None = None, force: bool = False,
) -> Manifest:
    root = repository(repo)
    safe_output(root, output)
    if output.resolve() in {path.resolve() for path in [task_path, *logs]}:
        raise HandoffError("output cannot overwrite the task or an imported command log")
    if len(logs) > 100:
        raise HandoffError("at most 100 command logs can be included")
    task = Task.model_validate(read_json(task_path, 1024 * 1024))
    if any(output.resolve() == (root / path).resolve() for path in task.artifacts):
        raise HandoffError("output cannot overwrite a task artifact")
    output = prepare_output(output, force=force)
    evidence = []
    total_logs = 0
    for path in logs:
        item = load_evidence(path)
        total_logs += item.content.size
        if total_logs > 8 * 1024 * 1024:
            raise HandoffError("combined command logs exceed 8 MiB")
        evidence.append(item)
    artifact_scope = set(task.artifacts) | declared_paths(evidence)
    if any(output.resolve() == (root / path).resolve() for path in artifact_scope):
        raise HandoffError("output cannot overwrite an observed evidence artifact")
    state = snapshot(root)
    changes = None
    if base is not None:
        base_oid = resolve_commit(root, base)
        check_blob_sizes(root, [base_oid, state.head])
        changes = ChangeRange(
            base=base_oid,
            committed_diff=Blob.from_bytes(git(root, "diff", *DIFF, base_oid, state.head, "--")),
        )
    diff = None
    if supplied_diff is not None:
        raw = read_bytes(supplied_diff, 8 * 1024 * 1024)
        if raw != git(root, "diff", *DIFF, state.head, "--"):
            raise HandoffError("supplied diff does not match the current HEAD-to-worktree binary diff")
        diff = Blob.from_bytes(raw)
    artifacts, artifact_fingerprints = observe_artifacts(root, artifact_scope, state)
    package = Manifest(
        created_at=datetime.now(UTC).isoformat(), task_claims=task, snapshot=state,
        change_range=changes, supplied_diff=diff, artifacts=artifacts, evidence=evidence,
        verification=[
            VerificationCommand(
                purpose="Check package integrity and current Git/file state without executing commands",
                argv=["agentwrench", "handoff", "verify", str(output), "--repo", str(root)],
            ),
            *[
                VerificationCommand(purpose="User-supplied command; review before manually running", argv=argv)
                for argv in task.verification_commands
            ],
        ],
        limitations=LIMITATIONS, integrity_sha256="0" * 64,
    )
    package.seal()
    package.check()
    # Evidence may have named additional files. Validate those paths before writing.
    observed = {item.path: item for item in artifacts}
    for item in evidence:
        assess(item, root, state, [], observed)
    if snapshot(root).state_sha256 != state.state_sha256:
        raise HandoffError("source changed while preparing the handoff; retry")
    check_artifacts_stable(root, artifact_fingerprints)
    atomic_json(output, package.model_dump(), force=force)
    return package


def read_manifest(path: Path) -> Manifest:
    manifest = Manifest.model_validate(read_json(path))
    manifest.check()
    return manifest


def export_diff(*, repo: Path, output: Path, force: bool = False) -> dict:
    root = repository(repo)
    safe_output(root, output)
    output = prepare_output(output, force=force)
    state = snapshot(root)
    raw = git(root, "diff", *DIFF, state.head, "--")
    if snapshot(root).state_sha256 != state.state_sha256:
        raise HandoffError("source changed during diff export; retry")
    atomic_bytes(output, raw, force=force, maximum=8 * 1024 * 1024)
    return {"output": str(output), "head": state.head, "sha256": digest(raw), "size": len(raw)}


def describe(manifest: Manifest) -> dict:
    checked = verify(manifest, repo=None, keys=[])
    return {
        "schema_version": manifest.schema_version,
        "created_at": manifest.created_at,
        "task_claims": manifest.task_claims.model_dump(),
        "head": manifest.snapshot.head,
        "base": manifest.change_range.base if manifest.change_range else None,
        "source_state_sha256": manifest.snapshot.state_sha256,
        "source_files": len(manifest.snapshot.files),
        "artifacts": [item.model_dump() for item in manifest.artifacts],
        "evidence": checked["evidence"],
        "verification_commands": [item.model_dump() for item in manifest.verification],
        "repository": "not_checked", "task_completion": "not_assessed",
    }


def verify(
    manifest: Manifest, *, repo: Path | None, keys: list[dict],
    require_signed: bool = False, require_current_pass: bool = False,
) -> dict:
    if require_signed and not keys:
        raise HandoffError("--require-signed requires at least one --key")
    manifest.check()
    required_artifacts = set(manifest.task_claims.artifacts) | declared_paths(manifest.evidence)
    if required_artifacts != {item.path for item in manifest.artifacts}:
        raise HandoffError("manifest observed artifact scope does not match task and receipt paths")
    root = repository(repo) if repo is not None else None
    current = snapshot(root) if root is not None else None
    observed_list, fingerprints = observe_artifacts(root, required_artifacts, current) if root is not None else ([], {})
    observed = {item.path: item for item in observed_list}
    differences: list[dict] = []
    if current is not None:
        old = manifest.snapshot
        if current.head != old.head:
            differences.append({"kind": "head", "expected": old.head, "actual": current.head})
        before = {item.path: item for item in old.files}
        after = {item.path: item for item in current.files}
        for path in sorted(before.keys() | after.keys()):
            if before.get(path) != after.get(path):
                differences.append({"kind": "source_file", "path": path})
        if old.index != current.index:
            differences.append({"kind": "index"})
        for name in ("staged_diff", "unstaged_diff"):
            if getattr(old, name).sha256 != getattr(current, name).sha256:
                differences.append({"kind": name})
        for item in manifest.artifacts:
            if observed[item.path] != item:
                differences.append({"kind": "artifact", "path": item.path})
    evidence = [assess(item, root, current, keys, observed) for item in manifest.evidence]
    policy_errors: list[str] = []
    if keys and any(item["signature"] == "no_trusted_signature" for item in evidence):
        policy_errors.append("a command receipt has no signature from a supplied trusted key")
    if require_signed:
        if not evidence or any(item["signature"] != "verified_with_supplied_key" for item in evidence):
            policy_errors.append("every command receipt must have a signature from a supplied trusted key")
    if require_current_pass and not any(item["current_pass"] for item in evidence):
        policy_errors.append("no passing invocation is bound to the current captured source state")
    if current is not None and snapshot(root).state_sha256 != current.state_sha256:
        raise HandoffError("source changed during verification; retry")
    if root is not None:
        check_artifacts_stable(root, fingerprints)
    return {
        "ok": not differences and not policy_errors,
        "integrity": "valid",
        "repository": "not_checked" if current is None else ("match" if not differences else "drifted"),
        "source_state_sha256": manifest.snapshot.state_sha256,
        "differences": differences,
        "evidence": evidence,
        "policy_errors": policy_errors,
        "task_completion": "not_assessed",
    }


def record(
    *, repo: Path, argv: list[str], output: Path, name: str = "check",
    products: list[str] | None = None, timeout: int = 30,
    signing_key: Path | None = None, dsse: bool = False, force: bool = False,
) -> tuple[int, dict]:
    root = repository(repo)
    safe_output(root, output)
    command_argv(argv)
    if not 1 <= timeout <= 300:
        raise HandoffError("command timeout must be between 1 and 300 seconds")
    if not name.strip() or len(name) > 256:
        raise HandoffError("record name must contain 1 to 256 characters")
    output = prepare_output(output, force=force)
    before = snapshot(root)
    if any(item.kind == "symlink" for item in before.files):
        raise HandoffError("record does not follow source symlinks; import existing logs with capture instead")
    source = [item.path for item in before.files if item.kind == "file"]
    requested = sorted(set(products or []))
    if len(requested) > 1000:
        raise HandoffError("at most 1000 requested product paths can be recorded")
    for path in requested:
        if (root / path).resolve() == output:
            raise HandoffError("output cannot overwrite a requested product artifact")
        if inspect_file(root, path)[0].kind == "symlink":
            raise HandoffError("record products cannot be symlinks")
    if len(set(source) | set(requested)) > 10000:
        raise HandoffError("combined record artifact scope exceeds 10000 paths")
    signer = load_signer(signing_key)
    # Compose upstream primitives in phases so we can check source state and
    # product kinds before each hashing step. Override its *.link/*.pyc defaults:
    # tracked source files with those suffixes are still part of our source scope.
    materials = record_hashes(root, source)
    expected_inputs = {item.path: {"sha256": item.sha256} for item in before.files if item.kind == "file"}
    if materials != expected_inputs:
        raise HandoffError("source changed before command execution; retry")
    byproducts = execute(argv, root, timeout)
    after = snapshot(root)
    if any(item.kind == "symlink" for item in after.files):
        raise HandoffError("command produced a source symlink; cannot safely record products")
    product_names = sorted(set(source + requested))
    expected_products = {}
    for path in product_names:
        state = inspect_file(root, path)[0]
        if state.kind == "symlink":
            raise HandoffError("command produced a product symlink; cannot safely record products")
        if state.kind == "file":
            expected_products[path] = {"sha256": state.sha256}
    product_hashes = record_hashes(root, product_names)
    if product_hashes != expected_products or snapshot(root).state_sha256 != after.state_sha256:
        raise HandoffError("products or source changed while completing the receipt; retry")
    link = Link(name=name, materials=materials, products=product_hashes, command=argv, byproducts=byproducts)
    # The environment key is part of the on-disk receipt format; keep it stable.
    link.environment[RECEIPT_KEY] = {
        "schema_version": "1", "scope": before.scope,
        "input_state_sha256": before.state_sha256,
        "output_state_sha256": after.state_sha256,
        "input_commit": before.head, "output_commit": after.head,
        "requested_products": requested,
    }
    meta = Envelope.from_signable(link) if dsse else Metablock(signed=link)
    if signer:
        meta.create_signature(signer)
    raw = meta.to_dict()
    atomic_json(output, raw, force=force, maximum=MAX_LOG)
    code = link.byproducts["return-value"]
    return code, {
        "output": str(output), "command_exit_code": code,
        "source_unchanged": before.state_sha256 == after.state_sha256,
        "missing_requested_products": sorted(set(requested) - set(product_hashes)),
    }
