from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

# Every module here needs the optional `sift` extra (shrinkfit, tiktoken, ...).
for _module in ("shrinkfit", "tiktoken", "json_source_map", "jsonpointer", "strip_ansi"):
    pytest.importorskip(_module)

import shrinkfit

from agentwrench.sift import BudgetError, InputError, Rules, summarize, verify

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "file,format",
    [
        ("pytest-color.log", "log"),
        ("pytest7-color.log", "log"),
        ("node-tap.log", "log"),
        ("unittest.log", "log"),
        ("pytest-result.json", "json"),
        ("pytest7-result.json", "json"),
    ],
)
def test_verify_actual_fixtures(file, format):
    raw = (FIXTURES / file).read_bytes()
    result = summarize(raw, budget=1600, format=format)
    assert verify(raw, result.text, budget=1600) == result.tokens


@pytest.mark.parametrize(
    "raw,format",
    [
        (b"", "log"),
        (b"a\r\nb\r\n", "log"),
        ("a\u2028b\nc".encode(), "log"),
        (b' \r\n {"a":1} \t', "json"),
        (b"null", "json"),
    ],
)
def test_verify_complete_whitespace_and_line_variants(raw, format):
    result = summarize(raw, budget=500, format=format)
    assert verify(raw, result.text) == result.tokens


@pytest.mark.parametrize("format", ["json", "log"])
def test_verify_partial_original_boundaries(format):
    value = "noise " * 1000 + "ValueError: invalid checksum 😀" + " more" * 1000
    raw = (
        json.dumps({"output": value}, ensure_ascii=False) if format == "json" else value + "\n"
    ).encode()
    result = summarize(raw, budget=600, format=format)
    assert any(not b["complete"] for b in result.document["blocks"])
    assert verify(raw, result.text, budget=600) == result.tokens


def test_verifier_does_not_invoke_upstream_selection(monkeypatch):
    raw = (FIXTURES / "pytest-result.json").read_bytes()
    result = summarize(raw, budget=700, format="json")

    def forbidden(*args, **kwargs):
        raise AssertionError("verifier must not invoke selection")

    monkeypatch.setattr(shrinkfit, "shrink_with_report", forbidden)
    assert verify(raw, result.text) == result.tokens


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["source"].update(bytes=True),
        lambda d: d["source"].update(sha256="0" * 64),
        lambda d: d.update(truncated=False),
        lambda d: d["blocks"][0].update(text="not original"),
        lambda d: d["blocks"][0].update(bytes=[0, 1]),
        lambda d: d["blocks"][0].update(pointer="/missing"),
        lambda d: d["omitted"].pop(),
        lambda d: d["omitted"].append([0, 1]),
        lambda d: d["structure"][0].update(size=False),
        lambda d: d.update(notice="Nothing was omitted"),
        lambda d: d.update(offsets="characters"),
        lambda d: d.update(extra="unrecognized"),
        lambda d: d["blocks"][0].update(text="\ud800"),
    ],
)
def test_corrupted_evidence_is_rejected(mutate):
    raw = json.dumps({"error": "ValueError: checksum", "noise": list(range(500))}).encode()
    result = summarize(raw, budget=450, format="json")
    document = copy.deepcopy(result.document)
    mutate(document)
    with pytest.raises(InputError):
        verify(raw, json.dumps(document))


def test_identical_values_cannot_be_reanchored_to_another_pointer():
    raw = b'{"a":"same","b":"same","padding":"' + b"x " * 3000 + b'"}'
    result = summarize(raw, budget=400, format="json", rules=Rules(keep=("/a",)))
    document = copy.deepcopy(result.document)
    next(b for b in document["blocks"] if b["pointer"] == "/a")["pointer"] = "/b"
    with pytest.raises(InputError, match="boundary"):
        verify(raw, json.dumps(document))


def test_wrong_log_lines_and_complete_flag_are_rejected():
    raw = ("noise " * 1000 + "TypeError: broken\n").encode()
    result = summarize(raw, budget=500, format="log")
    for key, value in [("lines", [2, 2]), ("complete", True)]:
        document = copy.deepcopy(result.document)
        document["blocks"][0][key] = value
        with pytest.raises(InputError):
            verify(raw, json.dumps(document))


def test_token_check_measures_supplied_whitespace():
    raw = b'{"ok":true}'
    result = summarize(raw, budget=500, format="json")
    pretty = json.dumps(result.document, ensure_ascii=False, indent=2)
    tokens = verify(raw, pretty)
    assert tokens > result.tokens
    assert verify(raw, pretty, budget=tokens) == tokens
    with pytest.raises(BudgetError) as error:
        verify(raw, pretty, budget=tokens - 1)
    assert error.value.needed == tokens


@pytest.mark.parametrize(
    "raw",
    [
        ' \r\n {"a/b~c":"注文 😀"} \t'.encode("utf-8-sig"),
        (FIXTURES / "pytest-result.json").read_text().encode("utf-8-sig"),
    ],
)
def test_verify_bom_prefixed_original_and_serialized_summary(raw):
    result = summarize(raw, budget=800, format="json")
    assert verify(raw, result.text, budget=800) == result.tokens
    assert verify(raw, result.text.encode("utf-8-sig")) >= result.tokens
