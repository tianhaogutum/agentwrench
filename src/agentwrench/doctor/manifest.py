"""Versioned repair previews; apply only the reviewed input, policy and output bytes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from .engine import DoctorError, decode, scan
from .files import _fsync_dir, assert_unchanged, fingerprint, open_session, publish
from .repair import make_plan, render, validate_plan


def _absolute(path: Path) -> Path:
    # Resolve parent aliases at preview time, but never follow the final component.
    # The latter must still pass open_session's O_NOFOLLOW boundary.
    try:
        parent = path.parent.resolve(strict=True)
    except RuntimeError as exc:
        raise DoctorError("unsafe path: symlink loop") from exc
    if not parent.is_dir():
        raise DoctorError("path parent must be an existing directory")
    return parent / path.name


def _render_digest(source, plan) -> str:
    digest = hashlib.sha256()
    for chunk in render(source, plan):
        digest.update(chunk)
    return digest.hexdigest()


def _save_new(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".rollout-doctor-manifest-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=True, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        # Publish atomically without replacing an existing file or symlink.
        os.link(temp, path, follow_symlinks=False)
        temp.unlink()
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _load(path: Path) -> dict:
    with open_session(path) as source:
        return _read(source)


def _read(source) -> dict:
    """Validate a manifest from an already locked regular-file stream."""
    try:
        value = decode(source.read())
        if set(value) != {"version", "source", "output", "options", "report"}:
            raise ValueError
        if type(value["version"]) is not int or value["version"] not in (1, 2):
            raise ValueError
        for key in ("source", "output"):
            endpoint = value[key]
            if not isinstance(endpoint, dict) or set(endpoint) != {"path", "sha256"}:
                raise ValueError
            location, digest = endpoint["path"], endpoint["sha256"]
            if not isinstance(location, str) or not location or "\0" in location or not Path(location).is_absolute():
                raise ValueError
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError
        options = value["options"]
        option_keys = {"format", "strategy", "drop_invalid"}
        in_place = value["version"] == 2
        if in_place:
            option_keys |= {"in_place", "backup"}
        if not isinstance(options, dict) or set(options) != option_keys:
            raise ValueError
        if options["format"] not in ("auto", "codex", "claude") or options["strategy"] not in ("stub", "drop"):
            raise ValueError
        if type(options["drop_invalid"]) is not bool or not isinstance(value["report"], dict):
            raise ValueError
        if in_place and (options["in_place"] is not True or options["backup"] is not True):
            raise ValueError
        if (value["source"]["path"] == value["output"]["path"]) != in_place:
            raise ValueError
        report = value["report"]
        if (set(report) != {"file", "format", "issues", "changes"}
                or report["file"] != value["source"]["path"]
                or report["format"] not in ("codex", "claude", "empty")
                or not isinstance(report["issues"], list) or not isinstance(report["changes"], list)
                or options["format"] != "auto" and report["format"] != options["format"]):
            raise ValueError
        return value
    except (TypeError, ValueError, RecursionError) as exc:
        raise DoctorError("invalid repair manifest; create a new preview") from exc


def preview(path: Path, output: Path | None, manifest: Path, format="auto", strategy="stub", drop_invalid=False,
            *, in_place=False, backup=False) -> dict:
    if in_place and not backup:
        raise DoctorError("--in-place requires --backup")
    if backup and not in_place:
        raise DoctorError("--backup is only valid with --in-place")
    if (in_place and output is not None) or (not in_place and output is None):
        raise DoctorError("choose --out or --in-place --backup")
    output = path if in_place else output
    path, output, manifest = (_absolute(p) for p in (path, output, manifest))
    if output == manifest:
        raise DoctorError("manifest and output must be separate paths")
    for target in ((manifest,) if in_place else (output, manifest)):
        if os.path.lexists(target):
            raise DoctorError(f"{target}: preview requires a new path (refusing overwrite)")
    with open_session(path) as source:
        report = scan(source, str(path), format)
        plan = make_plan(report, strategy, drop_invalid)
        validate_plan(source, plan)
        value = {"version": 2 if in_place else 1,
                 "source": {"path": str(path), "sha256": report.digest},
                 "output": {"path": str(output), "sha256": _render_digest(source, plan)},
                 "options": {"format": format, "strategy": strategy, "drop_invalid": drop_invalid},
                 "report": report.as_dict()}
        if in_place:
            value["options"].update({"in_place": True, "backup": True})
        assert_unchanged(source, path, plan)
        _save_new(manifest, value)
    return {"status": "previewed", "manifest": str(manifest), **value}


def _validate_manifest_pin(pin):
    if pin is not None and (
            not isinstance(pin, str) or len(pin) != 64 or
            any(c not in "0123456789abcdef" for c in pin)):
        raise DoctorError("--expect-manifest-sha256 requires exactly 64 lowercase hex characters")


def apply(manifest: Path, *, expect_manifest_sha256: str | None = None) -> dict:
    _validate_manifest_pin(expect_manifest_sha256)
    if expect_manifest_sha256 is None:
        return _apply(manifest, _load(manifest))
    # Hash and parse the same descriptor, retaining its lock through all writes
    # and already-applied verification. Never check a path and reopen to parse.
    with open_session(manifest) as locked:
        start = os.fstat(locked.fileno())
        digest = fingerprint(locked)
        if digest != expect_manifest_sha256:
            raise DoctorError("manifest SHA-256 differs from --expect-manifest-sha256; no source opened")
        locked.seek(0)
        value = _read(locked)

        def verify_manifest():
            actual = fingerprint(locked)
            finish, current = os.fstat(locked.fileno()), manifest.lstat()
            if (actual != digest or
                    (start.st_size, start.st_mtime_ns, start.st_ctime_ns) !=
                    (finish.st_size, finish.st_mtime_ns, finish.st_ctime_ns) or
                    (current.st_dev, current.st_ino) != (start.st_dev, start.st_ino)):
                raise DoctorError("manifest changed during apply; no further writes performed")

        verify_manifest()
        return _apply(manifest, value, verify_manifest)


def _apply(manifest, value, verify_manifest=None):
    path, output = (Path(value[k]["path"]) for k in ("source", "output"))
    options = value["options"]
    in_place = value["version"] == 2
    expected = value["output"]["sha256"]
    backup = None
    with open_session(path, write=True) as source:
        current_digest = fingerprint(source)
        if in_place and current_digest == expected and current_digest != value["source"]["sha256"]:
            # The original bytes are gone after replacement. Verify the reviewed
            # result directly; never re-lock the same inode or create another backup.
            source.seek(0)
            # Drop can remove the last format marker; use the detected format
            # saved in the preview, as validate_plan does before publication.
            detected = value["report"]["format"]
            report = scan(source, str(path), detected if detected != "empty" else "auto")
            if report.digest != expected or report.issues:
                raise DoctorError("applied source differs from preview or is no longer clean")
            assert_unchanged(source, path, make_plan(report, options["strategy"], options["drop_invalid"]))
            if verify_manifest is not None:
                verify_manifest()
            return {"status": "already_applied", "manifest": str(manifest.absolute()), "backup": None, **value}
        if current_digest != value["source"]["sha256"]:
            raise DoctorError("stale input: source SHA-256 differs from preview; create a new preview")
        source.seek(0)
        report = scan(source, str(path), options["format"])
        if report.digest != value["source"]["sha256"]:
            raise DoctorError("stale input: source changed while applying manifest")
        plan = make_plan(report, options["strategy"], options["drop_invalid"])
        validate_plan(source, plan)
        # Canonical JSON also distinguishes booleans/floats from integer line numbers;
        # Python dictionary equality alone would accept True == 1 == 1.0.
        if (json.dumps(report.as_dict(), sort_keys=True) != json.dumps(value["report"], sort_keys=True)
                or _render_digest(source, plan) != expected):
            raise DoctorError("repair differs from preview; create a new preview with this implementation")
        assert_unchanged(source, path, plan)
        if verify_manifest is not None:
            verify_manifest()
        publication_checks = {"before_write": verify_manifest} if verify_manifest is not None else {}
        if in_place:
            if expected == current_digest:
                # A clean preview is also a no-op on the first apply.
                status = "already_applied"
            else:
                backup = publish(source, path, output, plan, True, expected_digest=expected, **publication_checks)
                status = "applied"
        elif os.path.lexists(output):
            # A regular, byte-identical output is the only accepted existing target.
            # Lock and identity checks also cover concurrent cooperating writers.
            with open_session(output) as existing:
                if fingerprint(existing) != expected:
                    raise DoctorError("output conflicts with preview; refusing overwrite")
                current, opened = output.lstat(), os.fstat(existing.fileno())
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise DoctorError("output changed during verification")
                assert_unchanged(source, path, plan)
            status = "already_applied"
        else:
            publish(source, path, output, plan, False, expected_digest=expected, **publication_checks)
            status = "applied"
        if verify_manifest is not None:
            verify_manifest()
    return {"status": status, "manifest": str(manifest.absolute()), "backup": backup, **value}
