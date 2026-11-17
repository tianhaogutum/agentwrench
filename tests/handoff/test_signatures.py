from __future__ import annotations

import json
import sys

import pytest

pytest.importorskip("in_toto")
pytest.importorskip("pydantic")

from securesystemslib.signer import CryptoSigner

from agentwrench.handoff.core import capture, record, verify
from agentwrench.handoff.models import Blob


@pytest.mark.parametrize("algorithm", ["rsa", "ecdsa"])
@pytest.mark.parametrize("dsse", [False, True])
def test_pem_key_algorithms_cover_the_state_receipt(repo, task, tmp_path, algorithm, dsse):
    signer = CryptoSigner.generate_rsa(size=2048) if algorithm == "rsa" else CryptoSigner.generate_ecdsa()
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(signer.private_bytes)
    key_file.chmod(0o600)
    public = signer.public_key.to_dict()
    public["keyid"] = signer.public_key.keyid
    log = tmp_path / "signed.link"
    record(repo=repo, argv=[sys.executable, "-c", "pass"], output=log, signing_key=key_file, dsse=dsse)
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    result = verify(package, repo=repo, keys=[public], require_signed=True, require_current_pass=True)
    assert result["ok"]
    assert result["evidence"][0]["current_pass"]


def test_resealed_package_cannot_promote_a_tampered_signed_receipt(repo, task, tmp_path):
    signer = CryptoSigner.generate_ed25519()
    private = tmp_path / "key.pem"
    private.write_bytes(signer.private_bytes)
    private.chmod(0o600)
    public = signer.public_key.to_dict()
    public["keyid"] = signer.public_key.keyid
    log = tmp_path / "signed.link"
    record(repo=repo, argv=[sys.executable, "-c", "pass"], output=log, signing_key=private)
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    raw = json.loads(package.evidence[0].content.bytes())
    raw["signed"]["command"] = [sys.executable, "-c", "print('forged evidence')"]
    package.evidence[0].content = Blob.from_bytes(json.dumps(raw).encode())
    package.seal()  # An attacker can recompute unkeyed hashes, not this signature.
    result = verify(package, repo=repo, keys=[public], require_signed=True)
    assert not result["ok"]
    assert result["evidence"][0]["signature"] == "no_trusted_signature"
    assert not result["evidence"][0]["current_pass"]
