from __future__ import annotations

import json

import pytest

# Every module here needs the optional `sift` extra (shrinkfit, tiktoken, ...).
for _module in ("shrinkfit", "tiktoken", "json_source_map", "jsonpointer", "strip_ansi"):
    pytest.importorskip(_module)

from agentwrench.sift import BudgetError, InputError, Rules, summarize, verify


def raw_rows():
    return json.dumps(
        {"rows": [{"id": i, "a/b~c": "注文" + str(i), "body": "noise " * 500} for i in range(4)]},
        ensure_ascii=False,
    ).encode()


def test_pattern_pins_each_original_record_identity():
    raw = raw_rows()
    result = summarize(raw, budget=900, format="json", rules=Rules(keep_patterns=("/rows/*/id",)))
    shown = {b["pointer"]: b for b in result.document["blocks"]}
    for i in range(4):
        assert json.loads(shown[f"/rows/{i}/id"]["text"]) == i
        assert shown[f"/rows/{i}/id"]["complete"]
    assert verify(raw, result.text, budget=900) == result.tokens


def test_pattern_decodes_rfc6901_escapes_and_combines_with_exact_pin():
    raw = raw_rows()
    rules = Rules(keep=("/rows/0/id",), keep_patterns=("/rows/*/a~1b~0c",))
    result = summarize(raw, budget=900, format="json", rules=rules)
    shown = {b["pointer"]: b["text"] for b in result.document["blocks"]}
    assert shown["/rows/0/id"] == "0"
    for i in range(4):
        assert json.loads(shown[f"/rows/{i}/a~1b~0c"]) == "注文" + str(i)


def test_exact_pointer_can_address_literal_asterisk():
    raw = json.dumps({"*": 7, "noise": "x " * 3000}).encode()
    result = summarize(raw, budget=400, format="json", rules=Rules(keep=("/*",)))
    assert any(b["pointer"] == "/*" and b["text"] == "7" for b in result.document["blocks"])


def test_parent_and_pattern_children_do_not_duplicate_required_bytes():
    raw = json.dumps({"a": {"x": 1, "y": 2}, "noise": "x " * 3000}).encode()
    result = summarize(
        raw, budget=400, format="json", rules=Rules(keep=("/a",), keep_patterns=("/a/*",))
    )
    assert len([b for b in result.document["blocks"] if b["pointer"].startswith("/a")]) == 1
    assert verify(raw, result.text, budget=400) == result.tokens


def test_protected_pattern_container_is_never_partially_returned():
    with pytest.raises(BudgetError):
        summarize(raw_rows(), budget=400, format="json", rules=Rules(keep_patterns=("/rows/*",)))


@pytest.mark.parametrize("pattern", ["/missing/*", "/rows/*/missing", "/rows/01/id"])
def test_unmatched_hard_pattern_fails_explicitly(pattern):
    with pytest.raises(InputError, match="matched no"):
        summarize(raw_rows(), budget=500, format="json", rules=Rules(keep_patterns=(pattern,)))


def test_patterns_are_bounded_and_json_only():
    for rules in [
        Rules(keep_patterns="/*"),
        Rules(keep_patterns=("/*",) * 65),
        Rules(keep_patterns=(1,)),
    ]:
        with pytest.raises(InputError):
            summarize(b"{}", budget=500, format="json", rules=rules)
    with pytest.raises(InputError):
        summarize(b"hello\n", budget=500, format="log", rules=Rules(keep_patterns=("/*",)))
