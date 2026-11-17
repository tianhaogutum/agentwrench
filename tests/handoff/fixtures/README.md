# Fixture provenance

These files are real upstream in-toto metadata, not hand-written approximations.
Tests only read them; verification never executes a recorded command.

Every fixture was produced in a fresh local Git repository containing one
committed file, `retry.py`:

```python
def attempts():
    return 5
```

The recorded command is
`python3 -B -c "from retry import attempts; assert attempts() == 5; print('checks passed')"`
(the in-toto 2.3 fixture omits the final `print`). It ran for real against that
repository, so each Link's `retry.py` material/product hash matches the file the
test suite creates.

| File | Produced by |
| --- | --- |
| `upstream-unsigned.link` | in-toto 3.0.0 `in_toto_run` (streams recorded) |
| `upstream-signed.link` | the same Link, signed with `Metablock.create_signature` |
| `upstream-dsse.link` | the same Link wrapped by `Envelope.from_signable` and signed |
| `upstream-cli-run.link` | the in-toto 3.0.0 `in-toto-run` executable |
| `upstream-cli-record.link` | the split `in-toto-record start` / `stop` executables |
| `trusted-key.json` | public half of the ephemeral Ed25519 key used for the five files above |
| `upstream-v2.3.link` | in-toto 2.3.0 with securesystemslib 0.31.0, `in_toto_run` with a 2048-bit RSA key |
| `trusted-v2-key.json` | public half of that RSA key (`rsassa-pss-sha256`) |
| `provenance.json` | tool versions and methods recorded when the in-toto 3.0.0 fixtures were generated |

The split recorder stores no command and no return value, so the handoff must
report an unknown result for `upstream-cli-record.link` rather than invent a pass.
Private keys were generated in memory or in a temporary file that was removed
before the fixtures were saved; only public keys are kept. The in-toto 2.3.0
fixture was generated in a separate environment, without downgrading the
runtime dependencies. No in-toto 2.x Ed25519 coverage is claimed.

The test suite generates further real receipts at runtime (failures, DSSE and
signature variants with RSA/ECDSA/Ed25519 keys, source mutations and ignored
build products) using the installed in-toto version.
