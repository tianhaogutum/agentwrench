import json

import pytest

# Every module here needs the optional `sift` extra (shrinkfit, tiktoken, ...).
for _module in ("shrinkfit", "tiktoken", "json_source_map", "jsonpointer", "strip_ansi"):
    pytest.importorskip(_module)

from agentwrench.sift import BudgetError, summarize, verify


@pytest.mark.parametrize("budget", [1, 100, 160, 240])
@pytest.mark.parametrize("format", ["json", "log"])
def test_suggested_budget_does_not_keep_growing_with_excerpt_width(budget, format):
    value = "注文" * 500
    raw = (
        json.dumps({"outer": {"inner": {"value": value}}}, ensure_ascii=False)
        if format == "json"
        else value + "\n"
    ).encode()
    try:
        result = summarize(raw, budget=budget, format=format)
    except BudgetError as error:
        result = summarize(raw, budget=error.needed, format=format)
        assert result.tokens <= error.needed
    assert result.document["blocks"]
    assert verify(raw, result.text) == result.tokens
