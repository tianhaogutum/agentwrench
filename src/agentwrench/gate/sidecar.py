"""Bounded inspection and integrity verification; never interpret payload paths."""
import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import BinaryIO

from .model import InputError, integer, string
from .policy import keys
from .streams import MAX_LINES, MAX_STREAM_BYTES, _finite_float, _unique_object

# Serialized sidecars can be larger than their source due to JSON escaping/losses.
# Shared by generation and verification/restore so generated pairs fit the caps.
MAX_SIDECAR_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InputError(f"{label}: expected 64 lowercase SHA-256 hex characters")
    return value


@dataclass(frozen=True)
class VerifiedSidecar:
    source: str
    target: str
    original: bytes = field(repr=False)
    original_sha256: str
    output_sha256: str
    output_bytes: int

    def as_dict(self) -> dict:
        """Integrity metadata only; do not expose original payload or paths."""
        return {"schema_version": 1, "verified": True,
                "source": self.source, "target": self.target,
                "original": {"sha256": self.original_sha256, "bytes": len(self.original)},
                "output": {"sha256": self.output_sha256, "bytes": self.output_bytes}}


@dataclass(frozen=True)
class _DecodedSidecar:
    source: str
    target: str
    original: bytes = field(repr=False)
    original_sha256: str
    output_sha256: str
    loss_counts: dict[str, int]


def _decode_sidecar(sidecar: str | Path | BinaryIO) -> _DecodedSidecar:
    """Validate the bounded v1 envelope and original without any primary I/O."""
    data = bytearray()
    with (Path(sidecar).open("rb") if isinstance(sidecar, (str, Path)) else nullcontext(sidecar)) as handle:
        while chunk := handle.read(min(HASH_CHUNK_BYTES, MAX_SIDECAR_BYTES - len(data) + 1)):
            data.extend(chunk)
            if len(data) > MAX_SIDECAR_BYTES:
                raise InputError("sidecar exceeds 64 MiB")
    try:
        report = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_float=_finite_float,
                            parse_constant=lambda x: (_ for _ in ()).throw(InputError(f"invalid JSON number: {x}")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InputError(f"invalid sidecar JSON: {exc}") from exc
    fields = {"schema_version", "source", "target", "original", "output_sha256", "losses"}
    report = keys(report, fields, fields, "sidecar")
    if type(report["schema_version"]) is not int or report["schema_version"] != 1:
        raise InputError("sidecar.schema_version: expected integer 1")
    if report["source"] not in ("codex-exec", "claude-stream"):
        raise InputError("sidecar.source: unsupported source")
    if report["target"] not in ("normalize", "agentevals"):
        raise InputError("sidecar.target: unsupported target")
    original_fields = {"encoding", "jsonl", "sha256"}
    original = keys(report["original"], original_fields, original_fields, "sidecar.original")
    if original["encoding"] != "utf-8":
        raise InputError("sidecar.original.encoding: expected utf-8")
    if not isinstance(original["jsonl"], str):
        raise InputError("sidecar.original.jsonl: expected a string")
    raw = original["jsonl"].encode("utf-8")
    if len(raw) > MAX_STREAM_BYTES:
        raise InputError("sidecar original exceeds 64 MiB")
    original_hash = _sha256(original["sha256"], "sidecar.original.sha256")
    output_hash = _sha256(report["output_sha256"], "sidecar.output_sha256")
    if not isinstance(report["losses"], list):
        raise InputError("sidecar.losses: expected a list")
    loss_fields = {"source_line", "pointer", "reason", "disposition"}
    loss_counts = {"omitted": 0, "json_string": 0}
    for loss in report["losses"]:
        loss = keys(loss, loss_fields, loss_fields, "sidecar loss")
        line = integer(loss["source_line"], "loss.source_line")
        if not 1 <= line <= MAX_LINES:
            raise InputError("loss.source_line: expected 1..100000")
        pointer = loss["pointer"]
        if (not isinstance(pointer, str) or (pointer and not pointer.startswith("/"))
                or re.search(r"~(?![01])", pointer)):
            raise InputError("loss.pointer: expected RFC 6901 pointer")
        string(loss["reason"], "loss.reason")
        if loss["disposition"] not in ("omitted", "json_string"):
            raise InputError("loss.disposition: unsupported disposition")
        loss_counts[loss["disposition"]] += 1
    if hashlib.sha256(raw).hexdigest() != original_hash:
        raise InputError("original SHA-256 mismatch")
    return _DecodedSidecar(report["source"], report["target"], raw, original_hash,
                           output_hash, loss_counts)


def inspect_sidecar(sidecar: str | Path | BinaryIO) -> dict:
    """Return metadata only, explicitly leaving the expected primary unverified.

    Free-form reasons, pointers and JSON keys may contain payloads, so failures
    use a fixed diagnostic too. No embedded log parsing or attachment decoding.
    """
    try:
        decoded = _decode_sidecar(sidecar)
    except (InputError, OSError, UnicodeError, RecursionError):
        raise InputError("cannot inspect sidecar: invalid or unreadable input") from None
    return {"schema_version": 1, "sidecar_schema_version": 1,
            "source": decoded.source, "target": decoded.target,
            "original": {"encoding": "utf-8", "bytes": len(decoded.original),
                         "sha256": decoded.original_sha256, "verified": True},
            "primary": {"encoding": "utf-8", "expected_sha256": decoded.output_sha256},
            "primary_verified": False,
            "losses": {"total": sum(decoded.loss_counts.values()),
                       "by_disposition": decoded.loss_counts}}


def verify_sidecar(sidecar: str | Path | BinaryIO, primary: str | Path | BinaryIO) -> VerifiedSidecar:
    """Check the strict envelope and both hashes without writing any files.

    Integrity is relative to the supplied hashes, not proof of authenticity or
    semantic correspondence. No adapter, policy, attachment or logged tool runs.
    Borrowed binary streams are read from their current position without
    seeking or closing them. Sidecar validation precedes all primary reads.
    """
    decoded = _decode_sidecar(sidecar)
    # Hash primary bytes incrementally, with an extra byte to detect over-limit
    # inputs even if their size changes after opening. Never load/execute JSON.
    digest = hashlib.sha256()
    total = 0
    with (Path(primary).open("rb") if isinstance(primary, (str, Path)) else nullcontext(primary)) as handle:
        while chunk := handle.read(min(HASH_CHUNK_BYTES, MAX_OUTPUT_BYTES - total + 1)):
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                raise InputError("primary output exceeds 64 MiB")
            digest.update(chunk)
    if digest.hexdigest() != decoded.output_sha256:
        raise InputError("output SHA-256 mismatch")
    return VerifiedSidecar(decoded.source, decoded.target, decoded.original,
                           decoded.original_sha256, decoded.output_sha256, total)


def verified_original(sidecar: str | Path | BinaryIO, primary: str | Path | BinaryIO) -> bytes:
    """Return exact verified UTF-8 bytes for restoration."""
    return verify_sidecar(sidecar, primary).original
