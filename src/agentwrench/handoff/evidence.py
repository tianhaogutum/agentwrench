"""Read real in-toto metadata; never execute imported command text."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from in_toto.models.link import Link
from in_toto.models.metadata import Metadata
from pydantic import Field, field_validator
from securesystemslib.signer import CryptoSigner

from .git import inspect_file
from .io import (
    MAX_LOG,
    HandoffError,
    command_argv,
    parse_json,
    read_bytes,
    read_json,
    relative_path,
)
from .models import Blob, FileState, Hash, LocalEvidence, Oid, Snapshot, StrictModel

# in-toto Link environment key holding the source-state receipt (stable on-disk name).
RECEIPT_KEY = "anchored_handoff"


class StateReceipt(StrictModel):
    schema_version: Literal["1"]
    scope: Literal["git-tracked-and-nonignored-v1"]
    input_state_sha256: Hash
    output_state_sha256: Hash
    input_commit: Oid
    output_commit: Oid
    requested_products: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("requested_products")
    @classmethod
    def product_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            relative_path(value)
        return values


def state_receipt(link: Link) -> StateReceipt | None:
    raw = link.environment.get(RECEIPT_KEY)
    if raw is None:
        return None
    try:
        return StateReceipt.model_validate(raw)
    except Exception as exc:
        raise HandoffError("invalid handoff source-state receipt in in-toto environment") from exc


def metadata(data: bytes) -> tuple[Metadata, Link]:
    if len(data) > MAX_LOG:
        raise HandoffError(f"command log exceeds {MAX_LOG} bytes")
    raw = parse_json(data)
    if isinstance(raw, dict) and "payload" in raw:
        try:
            payload = base64.b64decode(raw["payload"], validate=True)
        except (ValueError, TypeError) as exc:
            raise HandoffError("invalid DSSE payload encoding") from exc
        # Upstream parses this JSON separately. Apply the same ambiguity checks
        # to the signed bytes instead of accepting its last duplicate key.
        parse_json(payload)
    try:
        if not isinstance(raw, dict):
            raise ValueError
        meta = Metadata.from_dict(raw)
        link = meta.get_payload()
        if not isinstance(link, Link):
            raise ValueError
        link.validate()
    except Exception as exc:
        raise HandoffError("invalid in-toto Link metadata (content omitted)") from exc
    for mapping in (link.materials, link.products):
        if len(mapping) > 10000:
            raise HandoffError("command log exceeds 10000 declared artifact paths")
        for path in mapping:
            relative_path(path)
    if not isinstance(link.name, str) or not link.name.strip() or len(link.name) > 256:
        raise HandoffError("in-toto step name must contain 1 to 256 characters")
    command_argv(link.command, allow_empty=True)
    state_receipt(link)
    return meta, link


def load_evidence(path: Path) -> LocalEvidence:
    raw = read_bytes(path, MAX_LOG)
    metadata(raw)
    return LocalEvidence(source_hint=path.name, content=Blob.from_bytes(raw))


def declared_paths(evidence: list[LocalEvidence]) -> set[str]:
    paths: set[str] = set()
    for item in evidence:
        _, link = metadata(item.content.bytes())
        paths.update(link.materials)
        paths.update(link.products)
        receipt = state_receipt(link)
        if receipt:
            paths.update(receipt.requested_products)
        if len(paths) > 10000:
            raise HandoffError("combined evidence artifact scope exceeds 10000 paths")
    return paths


def load_keys(paths: list[Path]) -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = []
    for path in paths:
        key = read_json(path, 64 * 1024)
        if not isinstance(key, dict) or not isinstance(key.get("keyid"), str):
            raise HandoffError("trusted key must be an in-toto public key object with keyid")
        keys.append(key)
    return keys


def signature_status(meta: Metadata, keys: list[dict[str, Any]]) -> str:
    if not keys:
        return "unverified" if meta.signatures else "unsigned"
    for key in keys:
        try:
            meta.verify_signature(key)
            return "verified_with_supplied_key"
        except Exception:
            continue
    return "no_trusted_signature"


def _matches(
    root: Path, expected: dict[str, dict[str, str]], observed: dict[str, FileState] | None,
) -> list[dict]:
    result = []
    for path, hashes in sorted(expected.items()):
        state = observed[path] if observed is not None else inspect_file(root, path)[0]
        if state.kind == "missing":
            status = "missing"
        elif state.kind != "file":
            status = "unsupported_file_kind"
        elif "sha256" not in hashes:
            status = "unsupported_hash_algorithm"
        elif hashes["sha256"] == state.sha256:
            status = "match"
        else:
            status = "mismatch"
        result.append({"path": path, "status": status})
    return result


def assess(
    evidence: LocalEvidence, root: Path | None, current: Snapshot | None,
    keys: list[dict[str, Any]],
    observed: dict[str, FileState] | None = None,
) -> dict:
    meta, link = metadata(evidence.content.bytes())
    signature = signature_status(meta, keys)
    value = link.byproducts.get("return-value")
    outcome = "unknown"
    if type(value) is int:
        outcome = "passed" if value == 0 else "failed"
    binding = "unbound"
    receipt = state_receipt(link)
    missing_products: list[str] = []
    if receipt is not None:
        missing_products = sorted(set(receipt.requested_products) - set(link.products))
        if receipt.input_state_sha256 != receipt.output_state_sha256:
            binding = "source_changed_during_command"
        elif current is None:
            binding = "not_checked"
        elif receipt.input_state_sha256 != current.state_sha256:
            binding = "different_source_state"
        elif receipt.input_commit != current.head or receipt.output_commit != current.head:
            binding = "inconsistent_commit"
        else:
            binding = "same_source_state"
    materials = _matches(root, link.materials, observed) if root is not None else []
    products = _matches(root, link.products, observed) if root is not None else []
    checks = materials + products
    declared = set(link.materials) | set(link.products)
    return {
        "source_hint": evidence.source_hint,
        "name": link.name,
        "command_argv": link.command,
        "reported_result": outcome,
        "signature": signature,
        "source_binding": binding,
        "missing_requested_products": missing_products,
        "materials": materials,
        "products": products,
        "declared_files_match": bool(checks) and all(c["status"] == "match" for c in checks),
        "undeclared_source_files": (
            sum(f.path not in declared for f in current.files) if current else None
        ),
        "current_pass": (
            binding == "same_source_state" and outcome == "passed" and bool(link.command)
            and not missing_products and bool(checks) and all(c["status"] == "match" for c in checks)
            and (not keys or signature == "verified_with_supplied_key")
        ),
    }


def load_signer(path: Path | None) -> CryptoSigner | None:
    if path is None:
        return None
    try:
        return CryptoSigner(load_pem_private_key(read_bytes(path, 64 * 1024), password=None))
    except Exception as exc:
        raise HandoffError("cannot load PEM signing key (use an unencrypted PKCS8 key)") from exc
