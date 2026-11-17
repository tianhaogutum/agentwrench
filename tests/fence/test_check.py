import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.check import check_patch
from agentwrench.fence.errors import FenceError
from agentwrench.fence.syntax import list_symbols, parse_source

from .support import FIXTURES, create_two_files_patch


def check(before, after, allow=("sample.ts::allowed",), *, file="sample.ts", context=3, ignore_formatting=False):
    return check_patch(patch=create_two_files_patch(f"a/{file}", f"b/{file}", before, after, context=context),
                       allow=list(allow), read_base=lambda _: before, ignore_formatting=ignore_formatting)


def read_fixture(name):
    return (FIXTURES / name).read_bytes().decode("utf-8")


BEFORE = "export function allowed(x: number) { return x + 1; }\nfunction adjacent() { return 4; }\n"


def test_allows_function_implementation_and_signature_changes_inside_its_exact_node():
    result = check(BEFORE, BEFORE.replace("x: number", "x: number, y = 2").replace("x + 1", "x + y"))
    assert result["ok"] is True
    assert len(result["accepted"]) == 1


def test_rejects_adjacent_logic_and_reports_both_accepted_and_outside_changes():
    result = check(BEFORE, BEFORE.replace("x + 1", "x + 2").replace("return 4", "return 9"))
    assert result["ok"] is False
    assert len(result["accepted"]) == 1
    assert any(change["reason"] == "outside_syntax" and change["text"] == "9" for change in result["violations"])


def test_outside_whitespace_fails_by_default_explicit_whitespace_mode_accepts():
    after = BEFORE.replace("adjacent() { return 4; }", "adjacent() {\n    return 4;\n}")
    assert check(BEFORE, after)["ok"] is False
    result = check(BEFORE, after, ignore_formatting=True)
    assert result["ok"] is True
    assert len(result["ignoredFormatting"]) == 1


@pytest.mark.parametrize("a, b", [
    ("function adjacent(){ return\n4; }", "function adjacent(){ return 4; }"),
    ('const text = "a b";', 'const text = "ab";'),
    ("const text = `a b`;", "const text = `ab`;"),
    ("// @ts-ignore\nconst x = 1;", "// @ts-expect-error\nconst x = 1;"),
])
def test_whitespace_mode_preserves_asi_structure_literals_templates_and_comments(a, b):
    prefix = "function allowed(){}\n"
    assert check(prefix + a, prefix + b, ignore_formatting=True)["ok"] is False


def test_export_modifiers_remain_outside_a_selected_function():
    assert check(BEFORE, BEFORE.replace("export ", ""))["ok"] is False


def test_top_level_insertion_and_deletion_cannot_hide_in_an_allowed_change():
    assert check(BEFORE, BEFORE + "doSomething();\n")["ok"] is False
    assert check(BEFORE, BEFORE.replace("function adjacent() { return 4; }\n", ""))["ok"] is False


def test_rejects_unknown_selectors_disappeared_symbols_and_parser_recovery():
    with pytest.raises(FenceError) as error:
        check(BEFORE, BEFORE, ["sample.ts::typo"])
    assert error.value.code == "unknown_symbol"
    with pytest.raises(FenceError) as error:
        check(BEFORE, BEFORE.replace("allowed(", "renamed("))
    assert error.value.code == "symbol_disappeared"
    with pytest.raises(FenceError) as error:
        check(BEFORE, BEFORE.replace("x: number)", "x: number"))
    assert error.value.code == "syntax_error"


def test_class_methods_nested_functions_arrows_and_duplicate_selectors_are_discoverable():
    source = ("class A { run(){return 1;} get x(){return 1;} set x(v:number){} } const arrow = () => 3; "
              "function outer(){function inner(){} }")
    symbols = list_symbols(parse_source(source))
    assert [symbol["selector"] for symbol in symbols] == ["A", "A.run", "A.x#1", "A.x#2", "arrow", "outer", "outer.inner"]
    assert check(source, source.replace("return 1", "return 2"), ["sample.ts::A.run"])["ok"] is True
    with pytest.raises(FenceError) as error:
        check(source, source, ["sample.ts::A.x"])
    assert error.value.code == "unknown_symbol"
    assert error.value.details["candidates"] == ["A.x#1", "A.x#2"]
    with pytest.raises(FenceError) as error:
        check(source, source, ["sample.ts::A", "sample.ts::A.run"])
    assert error.value.code == "overlapping_allow"


def test_unicode_ranges_remain_correct_before_a_selected_node():
    source = 'const π = "😀";\nfunction allowed(){ return "你好"; }\n'
    result = check(source, source.replace("你好", "世界"))
    assert result["ok"] is True
    assert result["accepted"][0]["baseline"]["start"]["line"] == 2


def test_ranges_use_utf16_columns_and_indices():
    # The astral character counts as two UTF-16 code units, as in the Node.js binding.
    source = 'const s = "😀"; function allowed(){ return 1; }\n'
    symbol = next(item for item in list_symbols(parse_source(source)) if item["selector"] == "allowed")
    assert symbol["range"] == {"start": {"line": 1, "column": 17, "index": 16},
                               "end": {"line": 1, "column": 48, "index": 47}}


def test_uses_the_tsx_grammar_for_tsx_source():
    source = "export const View = () => <div>hello world</div>;\nconst other = 1;\n"
    result = check(source, source.replace("hello world", "hello friend"), ["view.tsx::View"], file="view.tsx")
    assert result["ok"] is True
    assert check(source, source.replace("hello world", "helloworld"), [], file="view.tsx",
                 ignore_formatting=True)["ok"] is False


def test_real_typescript_compiler_fixture_accepts_one_function_and_rejects_its_neighbor():
    source = read_fixture("upstream/typescript-path.ts")
    after = source.replace("return getEncodedRootLength(path) < 0;", "return getEncodedRootLength(path) <= -1;")
    assert after != source
    assert check(source, after, ["compiler/path.ts::isUrl"], file="compiler/path.ts")["ok"] is True
    leaked = after.replace("return getEncodedRootLength(path) > 0;", "return getEncodedRootLength(path) >= 0;")
    assert leaked != after
    assert check(source, leaked, ["compiler/path.ts::isUrl"], file="compiler/path.ts")["ok"] is False


def test_real_rxjs_overload_fixture_uses_explicit_occurrence_selectors():
    source = read_fixture("upstream/rxjs-pipe.ts")
    symbols = list_symbols(parse_source(source))
    implementation = next(symbol for symbol in symbols
                          if symbol["kind"] == "function_declaration" and symbol["name"] == "pipe")
    after = source.replace("return pipeFromArray(fns);", "return pipeFromArray([...fns]);")
    assert after != source
    assert check(source, after, [f"pipe.ts::{implementation['selector']}"], file="pipe.ts")["ok"] is True


def test_zero_context_hunks_and_exact_mismatches_are_handled():
    assert check(BEFORE, BEFORE.replace("return x + 1", "return x + 2"), context=0)["ok"] is True
    patch = create_two_files_patch("a/sample.ts", "b/sample.ts", BEFORE, BEFORE.replace("return x + 1", "return x + 2"))
    with pytest.raises(FenceError) as error:
        check_patch(patch=patch, allow=["sample.ts::allowed"], read_base=lambda _: "\n" + BEFORE)
    assert error.value.code == "patch_mismatch"


def test_unapproved_non_typescript_changes_reject_file_operations_fail_closed():
    assert check("one\n", "two\n", [], file="README.md")["ok"] is False
    patch = "diff --git a/a.ts b/a.ts\nnew file mode 100644\n--- /dev/null\n+++ b/a.ts\n@@ -0,0 +1 @@\n+function f(){}\n"
    with pytest.raises(FenceError) as error:
        check_patch(patch=patch, allow=[], read_base=lambda _: "")
    assert error.value.code == "unsupported_patch"


def test_report_shape_and_reader_failures():
    report = check(BEFORE, BEFORE.replace("x + 1", "x + 2"))
    assert list(report) == ["schemaVersion", "ok", "evidence", "policy", "allowed", "files", "accepted",
                            "violations", "ignoredFormatting"]
    assert report["schemaVersion"] == 1
    assert report["evidence"]["baseline"] == {"kind": "reader"}
    assert report["policy"] == "exact-outside-text"
    assert list(report["files"][0]) == ["file", "status", "baselineSha256", "candidateSha256", "outsideChanges",
                                        "truncated"]

    def failing(_):
        raise OSError("disk unavailable")

    patch = create_two_files_patch("a/sample.ts", "b/sample.ts", BEFORE, BEFORE.replace("x + 1", "x + 2"))
    with pytest.raises(FenceError) as error:
        check_patch(patch=patch, allow=[], read_base=failing)
    assert error.value.code == "baseline_read"
    with pytest.raises(FenceError) as error:
        check_patch(patch=patch, allow="sample.ts::allowed", read_base=lambda _: BEFORE)
    assert error.value.code == "invalid_allow"
    with pytest.raises(FenceError) as error:
        check_patch(patch=patch, allow=[], read_base=lambda _: BEFORE, ignore_formatting="yes")
    assert error.value.code == "invalid_options"
