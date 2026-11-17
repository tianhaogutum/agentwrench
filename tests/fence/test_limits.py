import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.check import check_patch
from agentwrench.fence.errors import FenceError
from agentwrench.fence.syntax import parse_source

from .support import create_two_files_patch

SOURCE = "function allowed(){return 1;}\n"
NORMAL = create_two_files_patch("a/a.ts", "b/a.ts", SOURCE, SOURCE.replace("1", "2"))


def check(patch, **extra):
    return check_patch(patch=patch, read_base=lambda _: SOURCE, allow=["a.ts::allowed"], **extra)


def error_code(function, *args):
    with pytest.raises(FenceError) as error:
        function(*args)
    return error.value.code


def test_oversized_source_candidate_patch_and_surrogate_input_fail_with_typed_errors():
    assert error_code(parse_source, " " * (1024 * 1024 + 1)) == "source_limit"
    assert error_code(check, " " * (8 * 1024 * 1024 + 1)) == "patch_limit"
    assert error_code(parse_source, 'const x = "\ud800";') == "invalid_encoding"
    assert error_code(check, "\ud800") == "invalid_encoding"
    oversized = SOURCE.replace("return 1;", f"/*{'x' * (1024 * 1024)}*/ return 1;")
    assert error_code(check, create_two_files_patch("a/a.ts", "b/a.ts", SOURCE, oversized)) == "source_limit"
    assert check(NORMAL)["ok"] is True  # A rejected input leaves no global state.


def test_deep_but_parseable_typescript_is_bounded_before_recursive_comparison():
    deep = "const x = " + "(" * 600 + "0" + ")" * 600 + ";"
    assert error_code(parse_source, deep) == "syntax_limit"
    assert check(NORMAL)["ok"] is True


def test_cumulative_baseline_byte_budget_includes_all_checked_permission_files():
    one_mib = "/*" + "x" * (1024 * 1024 - 4) + "*/"
    patches = "".join(create_two_files_patch(f"a/f{i}.ts", f"b/f{i}.ts", one_mib, one_mib) for i in range(17))
    with pytest.raises(FenceError) as error:
        check_patch(patch=patches, read_base=lambda _: one_mib)
    assert error.value.code == "source_limit"


def test_large_reformat_gets_exact_bounded_diagnostics_without_an_edit_distance_timeout():
    before = "".join(f"function f{i}(){{ return {i}; }}\n" for i in range(300))
    candidate = before.replace("{ return", "{\n  return").replace("; }", ";\n}")
    report = check_patch(patch=create_two_files_patch("a/a.ts", "b/a.ts", before, candidate), read_base=lambda _: before)
    assert report["ok"] is False
    assert len(report["violations"]) == 100
    assert report["files"][0]["truncated"] is True
    assert report["files"][0]["outsideChanges"] > 100
    assert not report["files"][0].get("countIsLowerBound")
    assert all(change["reason"] == "outside_whitespace" for change in report["violations"])


def test_large_syntax_replacement_preserves_a_known_rejection_when_detailed_diff_is_bounded():
    count = 3000
    before = "".join(f"function f{i}(){{return 1;}}\n" for i in range(count))
    after = before.replace("function f", "function g").replace("return 1", "return 2")
    body = "\n".join(["-" + line for line in before.rstrip().split("\n")] + ["+" + line for line in after.rstrip().split("\n")])
    patch = f"--- a/a.ts\n+++ b/a.ts\n@@ -1,{count} +1,{count} @@\n{body}\n"
    report = check_patch(patch=patch, read_base=lambda _: before)
    assert report["ok"] is False
    assert len(report["violations"]) > 0
    assert report["files"][0]["truncated"] is True
    assert report["files"][0]["countIsLowerBound"] is True
    assert "first incompatible" in report["files"][0]["diagnosticLimit"]


def test_allow_entry_and_patch_file_limits():
    with pytest.raises(FenceError) as error:
        check_patch(patch="", read_base=lambda _: SOURCE, allow=[f"a.ts::f{i}" for i in range(1001)])
    assert error.value.code == "invalid_allow"
    patches = "".join(create_two_files_patch(f"a/f{i}.ts", f"b/f{i}.ts", SOURCE, SOURCE) for i in range(101))
    with pytest.raises(FenceError) as error:
        check_patch(patch=patches, read_base=lambda _: SOURCE)
    assert error.value.code == "patch_limit"
