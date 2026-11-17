import re

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.check import check_patch
from agentwrench.fence.errors import FenceError
from agentwrench.fence.patch import apply_exactly, parse_candidate

from .support import create_two_files_patch

BEFORE = "function allowed(){return 1;}\nfunction other(){return 2;}\n"
AFTER = BEFORE.replace("return 1", "return 3")
PLAIN = create_two_files_patch("a/a.ts", "b/a.ts", BEFORE, AFTER)
GIT = "diff --git a/a.ts b/a.ts\nindex 0123456..789abcd 100644\n" + re.sub(r"^=+\n", "", PLAIN, count=1)


def run(patch, source=BEFORE):
    return check_patch(patch=patch, read_base=lambda _: source, allow=["a.ts::allowed"])


def error_code(patch, source=BEFORE):
    with pytest.raises(FenceError) as error:
        run(patch, source)
    return error.value.code


@pytest.mark.parametrize("content", ["garbage\n", "Only in directory: secret.ts\n", "diff --cc a.ts\n",
                                     "GIT binary patch\n", "Property changes on: a.ts\n"])
def test_strict_envelope_rejects_content_upstream_jsdiff_intentionally_ignores(content):
    assert error_code(GIT + content) in ("unsupported_patch", "invalid_patch")


def test_strict_envelope_rejects_malformed_git_headers():
    assert error_code(GIT.replace("diff --git a/a.ts b/a.ts", 'diff --git "unterminated')) == "invalid_patch"
    assert error_code(GIT.replace("diff --git a/a.ts b/a.ts", "diff --git a/other.ts b/other.ts")) == "invalid_patch"


@pytest.mark.parametrize("metadata", ["old mode 100644", "new mode 120000", "old mode 100644\nnew mode 100755",
                                      "index 0123456..789abcd 120000"])
def test_partial_or_complete_mode_metadata_never_passes_with_an_allowed_body_edit(metadata):
    assert error_code(GIT.replace("index 0123456..789abcd 100644", metadata)) == "unsupported_patch"


def test_forged_hunk_context_cannot_authorize_a_change_in_an_adjacent_function():
    candidate = BEFORE.replace("return 2", "return 9")
    patch = re.sub(r"(@@ .* @@)", r"\1 function allowed()",
                   create_two_files_patch("a/a.ts", "b/a.ts", BEFORE, candidate, context=0), count=1)
    assert run(patch)["ok"] is False


@pytest.mark.parametrize("malformed", [
    PLAIN.replace("@@ -1,2 +1,2 @@", "@@ broken @@"),
    PLAIN.replace("@@ -1,2 +1,2 @@", "@@ -1,2 +7,2 @@"),
    PLAIN.replace("@@ -1,2 +1,2 @@", "@@ -0,2 +0,2 @@"),
    PLAIN.replace("function other(){return 2;}", "function other(){return 9;}"),
    PLAIN + "--- a/hidden.ts\n+++ b/hidden.ts\nnot a hunk\n",
])
def test_rejects_malformed_hunk_coordinates_context_and_ignored_extra_data(malformed):
    with pytest.raises(FenceError):
        run(malformed)


def test_crlf_content_is_preserved_exactly_eof_newline_addition_removal_stays_outside():
    source = BEFORE.replace("\n", "\r\n")
    candidate = source.replace("return 1", "return 3")
    patch = create_two_files_patch("a/a.ts", "b/a.ts", source, candidate)
    assert run(patch, source)["ok"] is True
    assert apply_exactly(source, parse_candidate(patch)[0]) == candidate
    remove = create_two_files_patch("a/a.ts", "b/a.ts", BEFORE, BEFORE[:-1])
    assert run(remove)["ok"] is False
    assert check_patch(patch=remove, read_base=lambda _: BEFORE, allow=["a.ts::allowed"],
                       ignore_formatting=True)["ok"] is True


@pytest.mark.parametrize("candidate", ["// before\n" + BEFORE, BEFORE.replace("\n", "\n// middle\n", 1),
                                       BEFORE + "// after\n"])
def test_zero_context_insertions_at_start_middle_and_eof_use_exact_coordinates(candidate):
    patch = create_two_files_patch("a/a.ts", "b/a.ts", BEFORE, candidate, context=0)
    assert apply_exactly(BEFORE, parse_candidate(patch)[0]) == candidate
    assert run(patch)["ok"] is False


def test_traversal_and_duplicate_patch_paths_fail_closed():
    assert error_code(PLAIN.replace("a/a.ts", "a/../a.ts").replace("b/a.ts", "b/../a.ts")) == "unsafe_path"
    assert error_code(PLAIN + PLAIN) == "invalid_patch"


@pytest.mark.parametrize("file", ["a/a.ts", "b/a.ts", "nested/a.ts"])
def test_bare_paths_beginning_with_a_or_b_are_preserved_without_a_paired_prefix(file):
    patch = create_two_files_patch(file, file, BEFORE, AFTER)

    def read(path):
        assert path == file
        return BEFORE

    assert check_patch(patch=patch, read_base=read, allow=[f"{file}::allowed"])["ok"] is True


def test_quoted_filename_junk_is_rejected_instead_of_silently_trimmed():
    assert error_code(PLAIN.replace("--- a/a.ts", '--- "a/a.ts"ignored')) == "invalid_patch"
    for escape in ("\\377", "\\777", "\\303"):
        patch = PLAIN.replace("--- a/a.ts", f'--- "a/{escape}.ts"').replace("+++ b/a.ts", f'+++ "b/{escape}.ts"')
        with pytest.raises(FenceError) as error:
            check_patch(patch=patch, read_base=lambda _: BEFORE, allow=[])
        assert error.value.code == "invalid_patch"


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
@pytest.mark.parametrize("target", ["", "\n", "\r\n"])
def test_all_nine_lf_crlf_no_final_newline_variants_apply_to_exactly_the_intended_bytes(ending, target):
    source = "function allowed(){return 1;}" + ending
    candidate = "function allowed(){return 3;}" + target
    patch = create_two_files_patch("a/a.ts", "b/a.ts", source, candidate)
    assert apply_exactly(source, parse_candidate(patch)[0]) == candidate
    assert run(patch, source)["ok"] is (ending == target)


def test_outside_whitespace_diagnostics_identify_each_exact_gap_rather_than_the_parent_node():
    report = run(create_two_files_patch("a/a.ts", "b/a.ts", BEFORE, BEFORE.replace("\n", "\r\n")))
    assert len(report["violations"]) == 4
    for change in report["violations"]:
        assert change["range"]["end"]["index"] - change["range"]["start"]["index"] <= 2
        assert change["range"]["start"]["column"] == (30 if change["range"]["start"]["line"] == 1 else 28)


def test_misplaced_duplicate_and_missing_eof_markers_cannot_alter_reconstructed_evidence():
    first = "function allowed(){return 1;} // trailing"
    source = first + "\nfunction protectedFn(){return 2;}"
    forged = (f"--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-{first}\n\\ No newline at end of file\n"
              f"+{first.replace('1', '3')}\n\\ No newline at end of file\n")
    # Git joins the marked first line to protectedFn, commenting it out; jsdiff
    # treated both markers as global EOF state and kept that newline.
    assert error_code(forged, source) == "invalid_patch"
    duplicate = create_two_files_patch("a/a.ts", "b/a.ts", BEFORE, BEFORE[:-1]) + "\\ No newline at end of file\n"
    assert error_code(duplicate) == "invalid_patch"
    missing = "--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-function allowed(){return 1;}\n+function allowed(){return 3;}\n"
    assert error_code(missing, "function allowed(){return 1;}") == "invalid_patch"


def test_carriage_return_terminated_headers_are_refused():
    patch = "--- a/a.ts\r\n+++ b/a.ts\r\n@@ -1 +1 @@\r\n-x\r\n+y\r\n"
    with pytest.raises(FenceError) as error:
        check_patch(patch=patch, read_base=lambda _: "x\r\n", allow=[])
    assert error.value.code == "invalid_patch"


def test_empty_patches_still_validate_every_selector():
    report = check_patch(patch="", read_base=lambda _: BEFORE, allow=["a.ts::allowed"])
    assert report["ok"] is True and report["files"] == [] and report["accepted"] == []
    with pytest.raises(FenceError) as error:
        check_patch(patch="   \n", read_base=lambda _: BEFORE, allow=["a.ts::nope"])
    assert error.value.code == "unknown_symbol"
