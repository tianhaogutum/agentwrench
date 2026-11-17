from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pytest

# Every module here needs the optional `sift` extra (shrinkfit, tiktoken, ...).
for _module in ("shrinkfit", "tiktoken", "json_source_map", "jsonpointer", "strip_ansi"):
    pytest.importorskip(_module)

import jsonpointer
import tiktoken

from agentwrench.sift import BudgetError, InputError, Rules, recover, summarize

FIXTURES = Path(__file__).parent / "fixtures"


def assert_evidence(raw: bytes, result, budget: int):
    assert result.tokens == len(
        tiktoken.get_encoding(result.document["encoding"]).encode_ordinary(result.text)
    )
    assert result.tokens <= budget
    assert json.loads(result.text) == result.document
    assert result.document["source"] == {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    intervals = []
    for block in result.document["blocks"]:
        start, end = block["bytes"]
        assert raw[start:end].decode() == block["text"]
        assert recover(raw, result.document, start, end).decode() == block["text"]
        intervals.append((start, end))
    intervals.extend(map(tuple, result.document["omitted"]))
    cursor = 0
    for start, end in sorted(intervals):
        assert cursor == start
        assert end > start
        cursor = end
    assert cursor == len(raw)
    assert result.document["truncated"] == bool(result.document["omitted"])


def test_complete_small_json():
    raw = b' {"ok":true,"empty":[],"null":null} \r\n'
    result = summarize(raw, budget=500, format="json")
    assert_evidence(raw, result, 500)
    assert result.document["omitted"] == []
    assert result.document["blocks"][0]["text"] == raw.decode()


def test_pinned_unicode_and_escaped_keys():
    data = {
        "noise": ["irrelevant " * 20 for _ in range(300)],
        "a/b~c": {"注文": "café 😀"},
        "error": "EOF",
    }
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode()
    result = summarize(raw, budget=600, format="json", rules=Rules(keep=("/a~1b~0c/注文",)))
    assert_evidence(raw, result, 600)
    blocks = {b["pointer"]: b for b in result.document["blocks"]}
    assert json.loads(blocks["/a~1b~0c/注文"]["text"]) == "café 😀"
    assert json.loads(blocks["/error"]["text"]) == "EOF"
    assert {"pointer": "/noise", "type": "array", "size": 300} in result.document[
        "structure"
    ] or not any(b["pointer"].startswith("/noise/") for b in blocks.values())


def test_overlapping_pins_and_real_null():
    raw = b'{"a":{"b":null,"c":false},"padding":"' + b"x " * 2000 + b'"}'
    result = summarize(raw, budget=450, format="json", rules=Rules(keep=("/a/b", "/a", "/a")))
    assert_evidence(raw, result, 450)
    assert len([b for b in result.document["blocks"] if b["pointer"].startswith("/a")]) == 1
    assert json.loads(result.document["blocks"][0]["text"]) == {"b": None, "c": False}


def test_budget_cannot_drop_required_value():
    with pytest.raises(BudgetError) as error:
        summarize(
            json.dumps({"required": list(range(500))}),
            budget=300,
            format="json",
            rules=Rules(keep=("/required",)),
        )
    assert error.value.needed > 300


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b"{} trailing", b'{"x":NaN}', b'{"x":Infinity}', b'{"x":', b"\xff"]
)
def test_invalid_input(raw):
    with pytest.raises(InputError):
        summarize(raw, budget=500, format="json")


@pytest.mark.parametrize("budget", [0, -1, 1.5, True, 32769])
def test_invalid_budget(budget):
    with pytest.raises(InputError):
        summarize(b"{}", budget=budget, format="json")


def test_nonexistent_and_noncanonical_pointer():
    for pointer in ("/missing", "/a/01", "/a/-"):
        with pytest.raises(InputError):
            summarize(b'{"a":[1,2]}', budget=500, format="json", rules=Rules(keep=(pointer,)))


def test_log_errors_middle_tail_and_ranges():
    raw = (
        "starting\r\n"
        + "noise\r\n" * 100
        + "\x1b[31mValueError: checksum 😀\x1b[0m\r\n"
        + "noise\r\n" * 100
        + "FAILED test_invoice\r\n2 failed, 10 passed\r\n"
    ).encode()
    result = summarize(raw, budget=700, format="log", rules=Rules(context_lines=1))
    assert_evidence(raw, result, 700)
    text = "".join(b["text"] for b in result.document["blocks"])
    assert "ValueError: checksum 😀" in text
    assert "FAILED test_invoice" in text
    assert "2 failed, 10 passed" in text
    assert "\x1b[31m" in text


def test_pinned_log_lines():
    raw = b"a\nb\nc\nd\n"
    result = summarize(raw, budget=300, format="log", rules=Rules(keep_lines=((2, 3),)))
    assert_evidence(raw, result, 300)
    assert any("b\nc\n" in b["text"] for b in result.document["blocks"])


@pytest.mark.parametrize(
    "rules",
    [
        Rules(keep=("x",)),
        Rules(context_lines=-1),
        Rules(keep_lines=((0, 1),)),
        Rules(keep_lines=((1, 99),)),
    ],
)
def test_invalid_rules(rules):
    with pytest.raises(InputError):
        summarize(b"a\nb\n", budget=500, format="log", rules=rules)


def test_recovery_rejects_changed_source_and_split_unicode():
    raw = '"😀"'.encode()
    result = summarize(raw, budget=500, format="json")
    with pytest.raises(InputError, match="changed"):
        recover(b'"xxxx"', result.document, 0, 3)
    with pytest.raises(InputError, match="UTF-8"):
        recover(raw, result.document, 2, 3)


@pytest.mark.parametrize(
    "format,file", [("log", "pytest-color.log"), ("json", "pytest-result.json")]
)
def test_real_pytest_fixtures(format, file):
    raw = (FIXTURES / file).read_bytes()
    result = summarize(raw, budget=1600, format=format)
    assert_evidence(raw, result, 1600)
    assert result.document["truncated"]
    assert result.document["blocks"]
    if format == "log":
        shown = "".join(b["text"] for b in result.document["blocks"])
        assert "ConnectionError" in shown
        assert "failed" in shown


def test_deterministic():
    raw = (FIXTURES / "pytest-result.json").read_bytes()
    first = summarize(raw, budget=700, format="json")
    second = summarize(raw, budget=700, format="json")
    assert first.text == second.text


def test_source_map_key_collisions_and_json_escapes():
    raw = (
        rb'{"a/b":1,"a":{"b":2},"a~b":3,"\u0078":4,"":5,"quo\"te":6,"back\\slash":7,"padding":"'
        + b"x " * 1000
        + b'"}'
    )
    pointers = ("/a~1b", "/a/b", "/a~0b", "/x", "/", '/quo"te', "/back\\slash")
    result = summarize(raw, budget=1000, format="json", rules=Rules(keep=pointers))
    assert_evidence(raw, result, 1000)
    values = {
        b["pointer"]: json.loads(b["text"]) for b in result.document["blocks"] if b["complete"]
    }
    for index, pointer in enumerate(pointers, 1):
        assert values[pointer] == index


@pytest.mark.parametrize("raw", [b'{"x":1e9999}', rb'{"x":"\ud800"}', rb'{"\udfff":1}'])
def test_nonrepresentable_json_values_fail_cleanly(raw):
    with pytest.raises(InputError):
        summarize(raw, budget=500, format="json")


def test_large_json_error_string_has_honest_partial_ranges():
    raw = json.dumps(
        {"output": "noise " * 1000 + "ValueError: checksum mismatch" + "noise " * 1000}
    ).encode()
    result = summarize(raw, budget=600, format="json")
    assert_evidence(raw, result, 600)
    assert any("ValueError: checksum mismatch" in b["text"] for b in result.document["blocks"])
    for block in result.document["blocks"]:
        assert not block["complete"]
        assert (
            block["node_bytes"][0]
            <= block["bytes"][0]
            < block["bytes"][1]
            <= block["node_bytes"][1]
        )


def test_long_log_line_keeps_middle_error():
    raw = ("noise " * 3000 + "TypeError: invalid checksum" + " tail" * 3000 + "\n").encode()
    result = summarize(raw, budget=650, format="log")
    assert_evidence(raw, result, 650)
    assert any("TypeError: invalid checksum" in b["text"] for b in result.document["blocks"])
    assert all(not b["complete"] for b in result.document["blocks"])


def test_real_pytest_json_retains_failed_outcomes_and_exception():
    raw = (FIXTURES / "pytest-result.json").read_bytes()
    result = summarize(raw, budget=1600, format="json")
    assert_evidence(raw, result, 1600)
    shown = "".join(b["text"] for b in result.document["blocks"])
    assert "ConnectionError" in shown
    assert "AssertionError" in shown
    assert any(
        b["pointer"].endswith("/outcome") and b["text"] == '"failed"'
        for b in result.document["blocks"]
    )


@pytest.mark.parametrize(
    "rules",
    [
        Rules(keep=None),
        Rules(keep="/x"),
        Rules(keep_lines="1:2"),
        Rules(priority_keys="error"),
        Rules(priority_keys=(1,)),
    ],
)
def test_malformed_rule_collections(rules):
    with pytest.raises(InputError):
        summarize(b"{}", budget=500, format="json", rules=rules)


def test_unhashable_format_is_input_error():
    with pytest.raises(InputError):
        summarize(b"{}", budget=500, format=[])


def test_node_limit_checks_before_expanding_broad_container():
    with pytest.raises(InputError, match="nodes"):
        summarize(b"[" + b"0," * 50_000 + b"0]", budget=500, format="json")


def test_many_errors_cannot_crowd_out_final_summary():
    raw = ("ERROR repeated\n" * 1200 + "1234 failed, 23 passed\n").encode()
    result = summarize(raw, budget=400, format="log")
    assert_evidence(raw, result, 400)
    assert any("1234 failed, 23 passed" in b["text"] for b in result.document["blocks"])


@pytest.mark.parametrize("seed", range(80))
def test_random_json_source_partition_and_pointer_identity(seed):
    rng = random.Random(seed)

    def make(depth=0):
        if depth >= 4 or rng.random() < 0.45:
            return rng.choice(
                [
                    None,
                    True,
                    False,
                    rng.randrange(-10000, 10000),
                    rng.random(),
                    'café 😀 /~\\"\n' * rng.randrange(1, 35),
                ]
            )
        if rng.random() < 0.5:
            return [make(depth + 1) for _ in range(rng.randrange(1, 6))]
        return {
            key + str(i): make(depth + 1)
            for i, key in enumerate(
                rng.sample(
                    ["", "a/b", "a~b", "x", "error", 'quo"te', "注文", "\\path"],
                    rng.randrange(1, 6),
                )
            )
        }

    data = make()
    raw = (
        " \r\n"
        + json.dumps(data, ensure_ascii=bool(seed % 2), indent=(2 if seed % 3 else None))
        + "\t"
    ).encode()
    budget = rng.randrange(220, 1000)
    try:
        result = summarize(raw, budget=budget, format="json")
    except BudgetError as error:
        assert error.needed > budget
        return
    assert_evidence(raw, result, budget)
    for block in result.document["blocks"]:
        if block["complete"]:
            assert json.loads(block["text"]) == jsonpointer.resolve_pointer(data, block["pointer"])


@pytest.mark.parametrize(
    "file,essential",
    [
        ("node-tap.log", ("not ok 5", "not ok 10", "TypeError", "# fail 2")),
        ("unittest.log", ("ConnectionError", "AssertionError", "FAILED (failures=1, errors=1)")),
    ],
)
def test_real_runner_variants(file, essential):
    raw = (FIXTURES / file).read_bytes()
    result = summarize(raw, budget=1100, format="log")
    assert_evidence(raw, result, 1100)
    text = "".join(b["text"] for b in result.document["blocks"])
    for expected in essential:
        assert expected in text


@pytest.mark.parametrize("encoding", ["cl100k_base", "o200k_base"])
def test_special_token_spelling_is_ordinary_evidence(encoding):
    raw = json.dumps(
        {"error": "ValueError: <|endoftext|> <|fim_prefix|> café 😀 " * 300}, ensure_ascii=False
    ).encode()
    result = summarize(raw, budget=500, format="json", encoding=encoding)
    assert_evidence(raw, result, 500)
    assert result.document["encoding"] == encoding


def test_final_exit_status_and_all_failed_test_identities():
    raw = (FIXTURES / "pytest-result.json").read_bytes()
    result = summarize(raw, budget=1600, format="json")
    shown = {b["pointer"]: b["text"] for b in result.document["blocks"]}
    assert shown["/exit_code"] == "1"
    assert {"/reports/13/nodeid", "/reports/31/nodeid", "/reports/39/nodeid"} <= shown.keys()


def test_upstream_array_sampling_keeps_original_last_identity():
    raw = json.dumps(
        {"rows": [{"id": i, "name": f"row-{i}", "body": "noise " * 200} for i in range(250)]}
    ).encode()
    result = summarize(raw, budget=700, format="json")
    assert_evidence(raw, result, 700)
    shown = {b["pointer"]: b["text"] for b in result.document["blocks"]}
    assert shown["/rows/0/id"] == "0"
    assert shown["/rows/249/id"] == "249"
    assert {"pointer": "/rows", "type": "array", "size": 250} in result.document["structure"]


@pytest.mark.parametrize(
    "format,file", [("log", "pytest7-color.log"), ("json", "pytest7-result.json")]
)
def test_real_pytest7_variants(format, file):
    raw = (FIXTURES / file).read_bytes()
    result = summarize(raw, budget=1600, format=format)
    assert_evidence(raw, result, 1600)
    shown = "".join(b["text"] for b in result.document["blocks"])
    assert "ConnectionError" in shown
    assert "AssertionError" in shown


@pytest.mark.parametrize(
    "raw", ["0", "-0", "1e308", "1e-308", str(10**150), '"\\u0000\\u2028\\ud83d\\ude00"']
)
def test_numeric_and_escaped_unicode_variants(raw):
    result = summarize(raw, budget=500, format="json")
    assert_evidence(raw.encode(), result, 500)


def test_recovery_empty_range_must_also_be_on_character_boundary():
    raw = '"😀"'.encode()
    result = summarize(raw, budget=500, format="json")
    with pytest.raises(InputError, match="UTF-8"):
        recover(raw, result.document, 2, 2)


def test_recovery_invalid_unicode_and_boolean_byte_length():
    result = summarize(b"0", budget=500, format="json")
    with pytest.raises(InputError, match="UTF-8"):
        recover("\ud800", result.document, 0, 0)
    result.document["source"]["bytes"] = True
    with pytest.raises(InputError):
        recover(b"0", result.document, 0, 1)


def test_nonempty_source_cannot_succeed_with_only_metadata():
    raw = json.dumps({"outer": {"inner": {"value": "注文" * 500}}}, ensure_ascii=False).encode()
    with pytest.raises(BudgetError):
        summarize(raw, budget=160, format="json")


def test_fragment_fallbacks_survive_many_oversized_error_fields():
    raw = json.dumps(
        {
            "error": {
                f"field{i}": "noise " * 800 + "ValueError: broken checksum" for i in range(1100)
            }
        }
    ).encode()
    result = summarize(raw, budget=600, format="json")
    assert_evidence(raw, result, 600)
    assert result.document["blocks"]
    assert any("ValueError: broken checksum" in b["text"] for b in result.document["blocks"])


def test_oversized_source_and_rule_pointers_fail_cleanly():
    with pytest.raises(InputError, match="pointer exceeds"):
        summarize(json.dumps({"x" * 4097: 1}), budget=500, format="json")
    with pytest.raises(InputError, match="pointer exceeds"):
        summarize(b"[]", budget=500, format="json", rules=Rules(keep=("/" + "9" * 5000,)))


def test_required_whole_log_line_is_not_labeled_partial_by_adjacent_fragment():
    raw = b"required line\n" + b"x" * 4000 + b"TypeError: invalid checksum\n"
    result = summarize(raw, budget=600, format="log", rules=Rules(keep_lines=((1, 1),)))
    assert_evidence(raw, result, 600)
    assert any(
        b["complete"] and b["bytes"][0] == 0 and b["text"] == "required line\n"
        for b in result.document["blocks"]
    )


def test_many_failed_records_preserve_cause_before_phase_metadata():
    raw = json.dumps(
        {
            "reports": [
                {
                    "outcome": "failed",
                    "when": "call",
                    "nodeid": f"test_case_{i}",
                    "message": f"ValueError: distinct checkout failure {i}",
                }
                for i in range(500)
            ]
        }
    ).encode()
    result = summarize(raw, budget=1200, format="json")
    assert any(
        "ValueError: distinct checkout failure" in b["text"] for b in result.document["blocks"]
    )


def test_mcp_iserror_does_not_turn_passed_records_into_failed_records():
    raw = json.dumps(
        {
            "isError": True,
            "content": [
                {"type": "text", "text": "ConnectionError: checkout database refused connection"}
            ],
            "structuredContent": {
                "reports": [
                    {"outcome": "passed", "when": "call", "nodeid": f"test_case_{i}"}
                    for i in range(500)
                ]
            },
        }
    ).encode()
    result = summarize(raw, budget=1000, format="json", rules=Rules(keep=("/isError",)))
    assert any(
        "ConnectionError: checkout database refused connection" in b["text"]
        for b in result.document["blocks"]
    )


@pytest.mark.parametrize(
    "body", ['{"a/b~c":"注文 😀","error":"EOF"}', ' \r\n {"x":1} \t', "null", '"café"']
)
def test_utf8_bom_json_keeps_original_byte_offsets(body):
    raw = body.encode("utf-8-sig")
    result = summarize(raw, budget=600, format="json")
    assert_evidence(raw, result, 600)
    assert result.document["omitted"] == [[0, 3]]
    assert result.document["blocks"][0]["bytes"][0] == 3
    assert json.loads(result.document["blocks"][0]["text"]) == json.loads(body)


def test_utf8_bom_real_report_fixture_and_escaped_pointer():
    # Standard-library utf-8-sig encoding of the actual upstream JSON fixture.
    raw = (FIXTURES / "pytest-result.json").read_text().encode("utf-8-sig")
    result = summarize(raw, budget=1600, format="json", rules=Rules(keep=("/exit_code",)))
    assert_evidence(raw, result, 1600)
    assert any(b["pointer"] == "/exit_code" and b["text"] == "1" for b in result.document["blocks"])
