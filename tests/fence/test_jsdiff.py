"""The pure-Python jsdiff subset; expected values were produced by npm ``diff`` 9.0.0."""
import pytest

from agentwrench.fence.jsdiff import JsdiffError, apply_patch, diff_arrays, parse_patch

from .support import create_two_files_patch

SOURCE = "function allowed(){return 1;}\nfunction other(){return 2;}\n"


def test_create_two_files_patch_matches_jsdiff_bytes():
    assert create_two_files_patch("a/a.ts", "b/a.ts", SOURCE, SOURCE.replace("return 1", "return 3")) == (
        "===================================================================\n--- a/a.ts\n+++ b/a.ts\n"
        "@@ -1,2 +1,2 @@\n-function allowed(){return 1;}\n+function allowed(){return 3;}\n"
        " function other(){return 2;}\n")
    assert create_two_files_patch("a.ts", "a.ts", SOURCE, SOURCE.replace("return 1", "return 3"), context=0) == (
        "Index: a.ts\n===================================================================\n--- a.ts\n+++ a.ts\n"
        "@@ -1,1 +1,1 @@\n-function allowed(){return 1;}\n+function allowed(){return 3;}\n")
    assert create_two_files_patch('a/雪 "x".ts', 'b/雪 "x".ts', SOURCE, SOURCE[:-1]) == (
        "===================================================================\n"
        '--- "a/\\351\\233\\252 \\"x\\".ts"\n+++ "b/\\351\\233\\252 \\"x\\".ts"\n'
        "@@ -1,2 +1,2 @@\n function allowed(){return 1;}\n-function other(){return 2;}\n"
        "+function other(){return 2;}\n\\ No newline at end of file\n")


def test_diff_arrays_is_jsdiffs_myers_diff():
    changes = diff_arrays(["a", "b", "c", "d"], ["a", "x", "c", "d", "e"])
    assert [(change.count, change.added, change.removed, change.value) for change in changes] == [
        (1, False, False, ["a"]), (1, False, True, ["b"]), (1, True, False, ["x"]),
        (2, False, False, ["c", "d"]), (1, True, False, ["e"])]
    assert diff_arrays(list(range(50)), list(range(50, 100)), max_edit_length=10) is None
    assert diff_arrays([], []) == []


def test_parse_patch_git_rename_and_errors():
    [index] = parse_patch("diff --git a/x.ts b/y.ts\nsimilarity index 90%\nrename from x.ts\nrename to y.ts\n"
                          "--- a/x.ts\n+++ b/y.ts\n@@ -1 +1 @@\n-a\n+b\n")
    assert (index.is_git, index.is_rename, index.old_file_name, index.new_file_name) == (True, True, "a/x.ts", "b/y.ts")
    assert (index.old_header, index.new_header) == ("", "")
    [hunk] = index.hunks
    assert (hunk.old_start, hunk.old_lines, hunk.new_start, hunk.new_lines, hunk.lines) == (1, 1, 1, 1, ["-a", "+b"])
    with pytest.raises(JsdiffError, match="^Hunk at line 3 contained invalid line $"):
        parse_patch("--- a/x\n+++ b/x\n@@ -1,2 +1 @@\n-a\n+b\n")
    with pytest.raises(JsdiffError, match=r'^Missing "\+\+\+ \.\.\." file header for a/x$'):
        parse_patch("--- a/x\n@@ -1 +1 @@\n-a\n+b\n")


def test_apply_patch_keeps_jsdiffs_offset_search_the_gate_preflight_forbids():
    def patch(text):
        return parse_patch(text)[0]

    assert apply_patch("a\nb\nc\n", patch("--- a/x\n+++ b/x\n@@ -2,1 +2,1 @@\n-b\n+B\n")) == "a\nB\nc\n"
    assert apply_patch("a\nb\nc\n", patch("--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-b\n+B\n")) == "a\nB\nc\n"
    assert apply_patch("a\nb\nc\n", patch("--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-z\n+B\n")) is False
