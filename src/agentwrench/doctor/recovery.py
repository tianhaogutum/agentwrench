"""Verify explicit manifest-bound bytes; optionally recover a backup to a new file."""
from contextlib import contextmanager
from collections import Counter
import hashlib
import os
from pathlib import Path
import tempfile

from .engine import DoctorError, FormatError, scan
from .files import _fsync_dir, fingerprint, open_session
from .manifest import _absolute, _read, _validate_manifest_pin


def _separate(paths):
    try:
        resolved = [path.resolve() for path in paths]
    except RuntimeError as exc:
        raise DoctorError("unsafe recovery path: symlink loop") from exc
    for index, path in enumerate(paths):
        for other_index, other in enumerate(paths[:index]):
            if resolved[index] == resolved[other_index] or (
                path.exists() and other.exists() and os.path.samefile(path, other)
            ):
                raise DoctorError("manifest, candidate/backup, protected source and recovery output must be separate files")


def _unchanged(stream, path, start, digest):
    # Hash again, then inspect identity/stat: copying alone would miss changes
    # to already-read bytes or replacement of the final path component.
    actual = fingerprint(stream)
    finish, current = os.fstat(stream.fileno()), path.lstat()
    if (actual != digest or
            (start.st_size, start.st_mtime_ns, start.st_ctime_ns) !=
            (finish.st_size, finish.st_mtime_ns, finish.st_ctime_ns) or
            (current.st_dev, current.st_ino) != (start.st_dev, start.st_ino)):
        raise DoctorError("recovery input changed during verification; no output published")


@contextmanager
def _manifest_inputs(manifest, candidate, extra_paths=(), *, repaired=False, expect_manifest_sha256=None):
    """Locked strict manifest and explicit bytes; source reads require explicit v2 opt-in."""
    paths = [manifest, candidate, *extra_paths]
    _separate(paths)
    with open_session(manifest) as plan:
        plan_start = os.fstat(plan.fileno())
        plan_digest = fingerprint(plan)
        if expect_manifest_sha256 is not None and plan_digest != expect_manifest_sha256:
            label = "candidate" if repaired else "backup"
            raise DoctorError(f"manifest SHA-256 differs from --expect-manifest-sha256; no {label} opened")
        plan.seek(0)
        value = _read(plan)
        if not repaired and value["version"] != 2:
            raise DoctorError("verification requires a version 2 in-place backup manifest")
        hash_key = "output" if repaired else "source"
        label = "candidate" if repaired else "backup"
        source = Path(value["source"]["path"])

        def separate_inputs():
            # The manifest is never allowed to alias the bound source, even for copies.
            _separate([manifest, source, *extra_paths])
            # Normalize parents only: a final symlink is not explicit source opt-in.
            explicit_source = (repaired and value["version"] == 2 and
                               candidate == source.parent.resolve() / source.name)
            _separate(paths if explicit_source else [*paths, source])

        separate_inputs()

        # Enforce the source-read policy before opening the explicit candidate.
        with open_session(candidate) as content:
            start = os.fstat(content.fileno())
            def verify_inputs(actual):
                if actual != value[hash_key]["sha256"]:
                    raise DoctorError(f"{label} SHA-256 differs from manifest {hash_key}; no output published")
                _unchanged(content, candidate, start, actual)
                _unchanged(plan, manifest, plan_start, plan_digest)
                separate_inputs()

            # Keep both locks held through verification and any recovery publication.
            yield value, content, verify_inputs


def _read_backup(original, target=None):
    # Stream arbitrary explicit bytes for backup and output verification alike.
    digest, count = hashlib.sha256(), 0
    while chunk := original.read(1024 * 1024):
        if target is not None:
            target.write(chunk)
        digest.update(chunk)
        count += len(chunk)
    return digest.hexdigest(), count


def verify_backup(manifest: Path, backup: Path, *, expect_manifest_sha256: str | None = None) -> dict:
    _validate_manifest_pin(expect_manifest_sha256)
    manifest, backup = (_absolute(p) for p in (manifest, backup))
    with _manifest_inputs(manifest, backup, expect_manifest_sha256=expect_manifest_sha256) as (value, original, verify_inputs):
        actual, count = _read_backup(original)
        verify_inputs(actual)
    return {"status": "verified", "version": 2, "manifest": str(manifest), "backup": str(backup),
            "expected_sha256": value["source"]["sha256"], "actual_sha256": actual, "verified_bytes": count}


class PairingRefusal(DoctorError):
    """Fixed refusal text with optional counts; never carries a report or IDs."""

    def __init__(self, reason, counts, include_summary):
        super().__init__("candidate pairing check failed; no output published")
        self.diagnostic_summary = ({"reason": reason, "issue_counts": dict(sorted(counts.items()))}
                                   if include_summary else None)


def verify_output(manifest: Path, candidate: Path, *, check_pairing: bool = False,
                  diagnostic_summary: bool = False, expect_manifest_sha256: str | None = None) -> dict:
    _validate_manifest_pin(expect_manifest_sha256)
    if diagnostic_summary and not check_pairing:
        raise DoctorError("--diagnostic-summary requires --check-pairing")
    manifest, candidate = (_absolute(p) for p in (manifest, candidate))
    with _manifest_inputs(manifest, candidate, repaired=True,
                          expect_manifest_sha256=expect_manifest_sha256) as (value, output, verify_inputs):
        actual, count = _read_backup(output)
        verify_inputs(actual)
        pairing = {}
        if check_pairing:
            # Drop may remove every format marker. Use only the saved detected
            # format, never reopen the source or replay the historical repair.
            detected = value["report"]["format"]
            output.seek(0)
            error = "candidate pairing check failed; no output published"
            try:
                report = scan(output, str(candidate), detected if detected != "empty" else "auto")
            except FormatError as exc:
                # A format error may stop the scan early: do not claim partial
                # counts, and still refuse changed inputs before explaining it.
                verify_inputs(actual)
                raise PairingRefusal("format_refusal", {}, diagnostic_summary) from exc
            except DoctorError as exc:
                raise DoctorError(error) from exc
            if report.digest != actual:
                raise DoctorError("candidate changed during pairing verification; no output published")
            verify_inputs(actual)
            if report.format != detected:
                raise PairingRefusal("format_refusal", {}, diagnostic_summary)
            if report.issues:
                counts = Counter(issue.code for issue in report.issues)
                reason = "malformed_records" if counts.keys() & {"RD001", "RD002", "RD003"} else "pairing_findings"
                raise PairingRefusal(reason, counts, diagnostic_summary)
            pairing = {"pairing": {"format": report.format, "clean": True}}
    return {"status": "verified", "version": value["version"], "manifest": str(manifest), "candidate": str(candidate),
            "expected_sha256": value["output"]["sha256"], "actual_sha256": actual, "verified_bytes": count,
            **pairing}


def recover(manifest: Path, backup: Path, destination: Path, *, expect_manifest_sha256: str | None = None) -> dict:
    _validate_manifest_pin(expect_manifest_sha256)
    manifest, backup, destination = (_absolute(p) for p in (manifest, backup, destination))
    _separate([manifest, backup, destination])
    if os.path.lexists(destination):
        raise DoctorError("recovery --out must be a new path (refusing overwrite)")
    with _manifest_inputs(manifest, backup, (destination,),
                          expect_manifest_sha256=expect_manifest_sha256) as (value, original, verify_inputs):
        expected = value["source"]["sha256"]
        fd, name = tempfile.mkstemp(prefix=".rollout-doctor-recovery-", dir=destination.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                actual, count = _read_backup(original, target)
                target.flush()
                os.fsync(target.fileno())
            verify_inputs(actual)
            os.link(temp, destination, follow_symlinks=False)
            temp.unlink()
            _fsync_dir(destination.parent)
        finally:
            temp.unlink(missing_ok=True)
    return {"status": "recovered", "version": 2, "manifest": str(manifest), "backup": str(backup),
            "source": value["source"], "output": {"path": str(destination), "sha256": expected},
            "restored_bytes": count}
