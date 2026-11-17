"""Comment line placement outside allowed nodes.

The original suite also asserted, with the TypeScript compiler as an oracle, that
these exact edits change JSDoc attachment (``@deprecated``) and ``@ts-expect-error``
suppression. That oracle needs Node.js; the gate's verdicts are kept here.
"""
import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.check import check_patch

from .support import create_two_files_patch


def check(before, after, allow=()):
    return check_patch(patch=create_two_files_patch("a/a.ts", "b/a.ts", before, after), read_base=lambda _: before,
                       allow=list(allow), ignore_formatting=True)


def test_outside_comment_line_placement_is_significant_jsdoc_attachment():
    before = "const first=1; /** @deprecated */ const second=2;"
    after = "const first=1;\n/** @deprecated */ const second=2;"
    report = check(before, after)
    assert report["ok"] is False
    assert any(change["reason"] == "outside_comment_boundary" for change in report["violations"])


def test_outside_comment_line_placement_is_significant_error_suppression():
    before = '/* @ts-expect-error */ const x: number = "bad";'
    after = '/* @ts-expect-error */\nconst x: number = "bad";'
    report = check(before, after)
    assert report["ok"] is False
    assert all(change["category"] == "comment_boundary" for change in report["violations"])


def test_ordinary_comment_indentation_remains_ignorable_selected_node_directives_remain_authorized():
    before = "// explanation\nfunction f(){\n  // note\n  return 1;\n}\n"
    after = before.replace("// note", "  // note").replace("  return", "    return")
    assert check(before, after)["ok"] is True
    selected_before = 'function f(){ /* @ts-expect-error */ const x: number = "bad"; }'
    selected_after = selected_before.replace("*/ const", "*/\n const")
    assert check(selected_before, selected_after, ["a.ts::f"])["ok"] is True
