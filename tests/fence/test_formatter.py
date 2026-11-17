import json

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.check import check_patch

from .support import FIXTURES


def test_real_prettier_whole_file_formatting_spill_rejects_alongside_an_accepted_compiler_fix():
    base = (FIXTURES / "upstream" / "typescript-path.ts").read_bytes().decode("utf-8")
    patch = (FIXTURES / "formatter" / "compiler-prettier.patch").read_bytes().decode("utf-8")
    report = check_patch(patch=patch, read_base=lambda _: base, allow=["compiler/path.ts::isUrl"])
    assert report["ok"] is False
    assert [edit["selector"] for edit in report["accepted"]] == ["isUrl"]
    assert len(report["violations"]) > 0
    assert report["files"][0]["truncated"] is True
    provenance = json.loads((FIXTURES / "formatter" / "provenance.json").read_text(encoding="utf-8"))
    assert report["evidence"]["patchSha256"] == provenance["patchSha256"]
    assert report["files"][0]["baselineSha256"] == provenance["sourceSha256"]
    assert report["files"][0]["candidateSha256"] == provenance["candidateSha256"]
